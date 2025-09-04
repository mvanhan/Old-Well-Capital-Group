from __future__ import annotations
import datetime as dt
import os

# Lightweight, always-on checks. Expand as needed (withdrawals enabled, maintenance windows, bank holidays, etc.)

def ok_to_trade_now() -> bool:
    """
    Return False to block entries when you *know* MR assumptions are weak:
    - Global maintenance flag (ENV), e.g., during deploys
    - Obvious holiday/roll windows if you rely on fiat redemptions (weekends/holidays)
    """
    if os.getenv("OWCG_TRADING_PAUSED", "0") == "1":
        return False

    # Example: Block Saturday/Sunday 02:00–06:00 UTC for maintenance windows
    now = dt.datetime.utcnow()
    if now.weekday() >= 5 and 2 <= now.hour < 6:
        return False

    return True
