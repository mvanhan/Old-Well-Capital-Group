from __future__ import annotations
import datetime as dt
import os

def ok_to_trade_now() -> bool:
    """
    Basic always-on guardrail.
    Extend with withdrawal/issuer/maintenance signals as you wire them.
    """
    if os.getenv("OWCG_TRADING_PAUSED", "0") == "1":
        return False

    # Example quiet window — tweak or remove if unwanted
    now = dt.datetime.utcnow()
    if now.weekday() >= 5 and 2 <= now.hour < 6:
        return False

    return True
