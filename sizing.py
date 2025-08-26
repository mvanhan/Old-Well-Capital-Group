# sizing.py — position sizing with absolute-dollar or % risk
from __future__ import annotations
from dataclasses import dataclass

import config as C


@dataclass
class OrderSizing:
    qty: float
    notional_usd: float
    stop_pct: float
    risk_dollars: float


def compute_order_for_pair(mid_px: float, stop_pct: float) -> OrderSizing:
    """
    If risk.risk_per_trade_usd is set, target that $ loss at the stop.
    Otherwise, use NAV * RISK_PER_TRADE_PCT * MIN_REALIZED_RISK_FRAC as risk dollars.
    Enforce SINGLE_TRADE_CAP_USD notional cap.
    """
    if mid_px <= 0:
        return OrderSizing(0.0, 0.0, float(stop_pct), 0.0)

    # risk dollars
    if C.RISK_PER_TRADE_USD is not None:
        risk_dollars = float(C.RISK_PER_TRADE_USD)
    else:
        risk_dollars = float(C.NAV_USD * C.RISK_PER_TRADE_PCT * C.MIN_REALIZED_RISK_FRAC)

    # compute qty from stop distance
    denom = max(1e-12, stop_pct * mid_px)
    qty = risk_dollars / denom
    notional = qty * mid_px

    # enforce notional cap
    cap = float(C.SINGLE_TRADE_CAP_USD)
    if notional > cap:
        scale = cap / notional
        qty *= scale
        notional = cap

    return OrderSizing(qty=float(qty), notional_usd=float(notional), stop_pct=float(stop_pct), risk_dollars=float(risk_dollars))
