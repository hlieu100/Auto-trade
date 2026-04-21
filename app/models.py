"""
models.py — Pydantic models for incoming TradingView webhook payloads.
"""

from typing import Optional
from enum import Enum

from pydantic import BaseModel, field_validator


class TradingSignal(str, Enum):
    """
    Raw signal types sent from Pine.
    Render decides what to do after checking Alpaca.
    """
    BASE_ENTRY = "base_entry"
    ADD_LEVERAGE = "add_leverage"
    REMOVE_LEVERAGE = "remove_leverage"
    STOP_LOSS = "stop_loss"
    SUPPORT_NOTICE = "support_notice"


class AlertPayload(BaseModel):
    """
    Incoming TradingView webhook payload.

    Pine should send raw intent only.
    Render will use Alpaca as the source of truth.
    """
    # Auth — must match WEBHOOK_SECRET env var
    secret: str

    # Symbol, e.g. "SPY"
    ticker: str

    # Raw signal from Pine
    signal: TradingSignal

    # Optional qty from Pine. Render may use it or override it.
    qty: Optional[float] = None

    # Current bar close price
    price: Optional[float] = None

    # Optional limit price
    limit: Optional[float] = None

    # TradingView idempotency key if you include one
    order_id: Optional[str] = None

    # TradingView timestamp
    timestamp: Optional[str] = None

    # Optional context fields from TradingView
    market_position: Optional[str] = None
    market_position_size: Optional[float] = None
    prev_market_position: Optional[str] = None
    prev_market_position_size: Optional[float] = None

    # Backward-compatible aliases if old Pine is still sending them
    action: Optional[str] = None
    contracts: Optional[float] = None

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("ticker", mode="before")
    @classmethod
    def clean_ticker(cls, v: str) -> str:
        """Strip exchange prefix like 'NASDAQ:AAPL' -> 'AAPL'."""
        if ":" in v:
            v = v.split(":")[-1]
        return v.strip().upper()

    @field_validator("signal", mode="before")
    @classmethod
    def normalise_signal(cls, v: str) -> str:
        """
        Accept mixed-case strings and some older plain-English forms.
        """
        if v is None:
            raise ValueError("signal is required")

        v = v.strip().lower()
        mapping = {
            "base entry": "base_entry",
            "add leverage": "add_leverage",
            "remove leverage": "remove_leverage",
            "stop loss": "stop_loss",
            "support notice": "support_notice",
        }
        return mapping.get(v, v)

    @field_validator("qty", mode="before")
    @classmethod
    def parse_qty(cls, v):
        if v is None or v == "" or v == "NaN":
            return None
        return float(v)

    @field_validator("contracts", mode="before")
    @classmethod
    def parse_contracts(cls, v):
        if v is None or v == "" or v == "NaN":
            return None
        return float(v)

    model_config = {"extra": "ignore"}
