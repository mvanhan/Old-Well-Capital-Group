# risk/healthchecks.py
from __future__ import annotations

from decimal import Decimal
import time

# Public data — ADAPT ME to your market data wrapper
try:
    from broker import coinbase_public as pub
except Exception:
    pub = None

# Thin-edge circuit breakers
MAX_ABS_DEV_BPS = Decimal("150.0")   # pause if |price - 1.0| > 150 bps
MAX_SPREAD_BPS  = Decimal("8.0")     # pause if best spread > 8 bps
QUIET_WINDOWS   = []  # e.g., [("23:55","00:05")]

def _bps(a: Decimal, b: Decimal) -> Decimal:
    return (a - b) / b * Decimal(10000)

def _in_quiet_window() -> bool:
    if not QUIET_WINDOWS:
        return False
    hhmm = time.strftime("%H:%M")
    for start, end in QUIET_WINDOWS:
        if start <= hhmm <= end:
            return True
    return False

def trading_allowed(product_id: str) -> bool:
    if _in_quiet_window():
        return False
    if pub is None:
        return True
    try:
        q = pub.get_best_bid_ask(product_id)
        bid = Decimal(q["bid"])
        ask = Decimal(q["ask"])
        mid = (bid + ask) / 2
        dev = abs(_bps(mid, Decimal("1.0000")))
        spread = _bps(ask, bid)
        if dev > MAX_ABS_DEV_BPS:
            return False
        if spread > MAX_SPREAD_BPS:
            return False
        return True
    except Exception:
        # If we cannot fetch data, be safe and pause
        return False
