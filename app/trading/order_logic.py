"""
order_logic.py — Translates TradingView alert actions into Alpaca orders.

Action mapping
──────────────
buy              → BUY qty shares
sell             → SELL qty shares
close_long       → close any open long position (all shares)
close_short      → close any open short position (buy to cover)
reverse_to_long  → close short (if any) then BUY qty shares
reverse_to_short → close long  (if any) then SELL qty shares

Kimi strategy actions
──────────────────────
base_entry       → ignored (you place the base order manually on Alpaca)
add_leverage     → query Alpaca buying power, calculate DD qty, place BUY
remove_leverage  → close only the "Leverage" position on Alpaca
stop_loss        → close ALL open positions on Alpaca
"""

import logging
import math
from typing import Optional

from alpaca.trading.enums import OrderSide
from alpaca.trading.models import Order

from app.models import AlertPayload, TradingAction
from app.trading import alpaca_client as ac

log = logging.getLogger(__name__)

# Kimi leverage_factor — must match TradingView script (default 0.5)
LEVERAGE_FACTOR = 0.5

# In-memory DD qty tracker — resets on redeploy (acceptable for intraday)
_dd_qty: dict[str, int] = {}


# ── Entry point ───────────────────────────────────────────────────────────────

async def execute_action(payload: AlertPayload) -> dict:
    """
    Dispatch the alert action and return a summary dict for the HTTP response.
    """
    action = payload.action
    ticker = payload.ticker
    qty    = payload.contracts

    log.info(
        "Executing action",
        extra={"action": action, "ticker": ticker, "qty": qty},
    )

    result: dict = {"action": action, "ticker": ticker, "orders": []}

    # ── Legacy actions ────────────────────────────────────────────────────────

    if action == TradingAction.BUY:
        order = _require_qty_then_order(ticker, OrderSide.BUY, qty)
        result["orders"].append(_order_summary(order))

    elif action == TradingAction.SELL:
        order = _require_qty_then_order(ticker, OrderSide.SELL, qty)
        result["orders"].append(_order_summary(order))

    elif action == TradingAction.CLOSE_LONG:
        order = _close_if_long(ticker)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = "No long position to close."

    elif action == TradingAction.CLOSE_SHORT:
        order = _close_if_short(ticker)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = "No short position to close."

    elif action == TradingAction.REVERSE_TO_LONG:
        close_order = _close_if_short(ticker)
        if close_order:
            result["orders"].append(_order_summary(close_order))
        long_order = _require_qty_then_order(ticker, OrderSide.BUY, qty)
        result["orders"].append(_order_summary(long_order))

    elif action == TradingAction.REVERSE_TO_SHORT:
        close_order = _close_if_long(ticker)
        if close_order:
            result["orders"].append(_order_summary(close_order))
        short_order = _require_qty_then_order(ticker, OrderSide.SELL, qty)
        result["orders"].append(_order_summary(short_order))

    # ── Kimi strategy actions ─────────────────────────────────────────────────

    elif action == TradingAction.BASE_ENTRY:
        order = _kimi_base_entry(ticker, payload.price, payload.limit)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = "Base entry skipped — insufficient buying power."

    elif action == TradingAction.ADD_LEVERAGE:
        order = _kimi_add_leverage(ticker, payload.price, payload.limit)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = "DD order skipped — insufficient buying power."

    elif action == TradingAction.REMOVE_LEVERAGE:
        order = _kimi_remove_leverage(ticker, payload.limit)
        if order:
            result["orders"].append(_order_summary(order))
        else:
            result["note"] = "No leverage position to close."

    elif action == TradingAction.TAKE_PROFIT:
        orders = _kimi_close_all(ticker)
        result["orders"].extend([_order_summary(o) for o in orders if o])
        if not result["orders"]:
            result["note"] = "No open positions to close."

    elif action == TradingAction.STOP_LOSS:
        orders = _kimi_close_all(ticker)
        result["orders"].extend([_order_summary(o) for o in orders if o])
        if not result["orders"]:
            result["note"] = "No open positions to close."

    else:
        raise ValueError(f"Unknown action: {action}")

    return result


# ── Kimi-specific helpers ─────────────────────────────────────────────────────

def _effective_price(ticker: str, price: Optional[float], limit: Optional[float]) -> float:
    """Return limit price if set, else price, else fetch from Alpaca."""
    p = limit or price
    if p and p > 0:
        return p
    fetched = ac.get_latest_price(ticker)
    if not fetched:
        raise ValueError(f"Could not determine price for {ticker}")
    return fetched


def _kimi_base_entry(
    ticker: str,
    price: Optional[float],
    limit: Optional[float],
) -> Optional[Order]:
    """Buy 100% of buying power at limit (mid) price."""
    account      = ac.get_account()
    buying_power = float(account.buying_power)
    exec_price   = _effective_price(ticker, price, limit)

    qty = math.floor(buying_power / exec_price)
    if qty <= 0:
        log.warning("Base entry qty=0", extra={"ticker": ticker, "buying_power": buying_power})
        return None

    log.info("Kimi base entry", extra={"ticker": ticker, "qty": qty, "limit": exec_price})
    _dd_qty[ticker] = 0   # reset DD tracker on new base entry

    if limit and limit > 0:
        return ac.place_limit_order(ticker, OrderSide.BUY, qty, limit)
    return ac.place_market_order(ticker, OrderSide.BUY, qty)


def _kimi_add_leverage(
    ticker: str,
    price: Optional[float],
    limit: Optional[float],
) -> Optional[Order]:
    """Buy 50% of buying power at limit (mid) price and track the DD qty."""
    account      = ac.get_account()
    buying_power = float(account.buying_power)
    exec_price   = _effective_price(ticker, price, limit)

    qty = math.floor(buying_power * LEVERAGE_FACTOR / exec_price)
    if qty <= 0:
        log.warning("DD qty=0", extra={"ticker": ticker, "buying_power": buying_power})
        return None

    log.info("Kimi DD buy", extra={"ticker": ticker, "qty": qty, "limit": exec_price})
    _dd_qty[ticker] = qty   # remember exact DD qty for remove_leverage

    if limit and limit > 0:
        return ac.place_limit_order(ticker, OrderSide.BUY, qty, limit)
    return ac.place_market_order(ticker, OrderSide.BUY, qty)


def _kimi_remove_leverage(
    ticker: str,
    limit: Optional[float],
) -> Optional[Order]:
    """Sell exactly the DD qty tracked when add_leverage fired."""
    qty = _dd_qty.get(ticker, 0)

    if qty <= 0:
        log.warning("No tracked DD qty to remove", extra={"ticker": ticker})
        return None

    log.info("Kimi DD sell", extra={"ticker": ticker, "qty": qty, "limit": limit})
    _dd_qty[ticker] = 0

    if limit and limit > 0:
        return ac.place_limit_order(ticker, OrderSide.SELL, qty, limit)
    return ac.place_market_order(ticker, OrderSide.SELL, qty)


def _kimi_close_all(ticker: str) -> list:
    """Close the full position for ticker (take profit or stop loss)."""
    _dd_qty[ticker] = 0
    order = ac.close_position(ticker)
    return [order] if order else []


# ── Private helpers ───────────────────────────────────────────────────────────

def _require_qty_then_order(
    ticker: str,
    side: OrderSide,
    qty: Optional[float],
) -> Order:
    if qty is None or qty <= 0:
        raise ValueError(
            f"Action '{side.value}' requires a positive 'contracts' value, "
            f"got: {qty!r}. Check your TradingView alert message template."
        )
    return ac.place_market_order(ticker, side, qty)


def _close_if_long(ticker: str) -> Optional[Order]:
    position = ac.get_position(ticker)
    if position is None:
        return None
    if str(position.side).lower() != "long":
        log.info("Skipping close_long — position is not long", extra={"ticker": ticker})
        return None
    return ac.close_position(ticker)


def _close_if_short(ticker: str) -> Optional[Order]:
    position = ac.get_position(ticker)
    if position is None:
        return None
    if str(position.side).lower() != "short":
        log.info("Skipping close_short — position is not short", extra={"ticker": ticker})
        return None
    return ac.close_position(ticker)


def _order_summary(order: Order) -> dict:
    return {
        "alpaca_order_id": str(order.id),
        "symbol":          order.symbol,
        "side":            str(order.side),
        "qty":             str(order.qty),
        "type":            str(order.order_type),
        "status":          str(order.status),
    }
