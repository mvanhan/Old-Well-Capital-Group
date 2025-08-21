# sizing.py — shared position-sizing logic (reads config.py)
from typing import Optional, Dict
import config as C
from kraken_public import list_asset_pairs

def _pair_meta(pair: str):
    """Return (pair_decimals, lot_decimals, ordermin) for Kraken pair altname, defaulting conservatively."""
    try:
        res = list_asset_pairs()
        for _, v in res.items():
            alt = v.get("altname") or ""
            if alt == pair:
                return int(v.get("pair_decimals", 2)), int(v.get("lot_decimals", 8)), float(v.get("ordermin", "0.0001"))
    except Exception:
        pass
    return 2, 8, 0.0001

def _round_qty(q: float, lot_decimals: int) -> float:
    return float(f"{q:.{lot_decimals}f}")

def compute_order_for_pair(pair: str, entry_price: float, atr_5m: float,
                           spread_median_bps: float = 0.0) -> Optional[Dict]:
    """
    Risk sizing:
      intended_risk = NAV * (pct/100)
      stop_dist = max(STOP_ATR_MULT * atr_5m, MIN_STOP_PCT * entry)
      qty_raw = intended_risk / stop_dist
      qty_cap = SINGLE_TRADE_CAP_USD / entry
      qty = min(qty_raw, qty_cap)  -> round to lot_decimals, enforce ordermin
    Returns dict or None to skip if too small.
    """
    if entry_price <= 0 or atr_5m <= 0:
        return None

    pair_dec, lot_dec, ordermin = _pair_meta(pair)

    intended_risk = C.NAV_USD * (C.RISK_PER_TRADE_PCT / 100.0)
    stop_dist = max(C.STOP_ATR_MULT * atr_5m, C.MIN_STOP_PCT * entry_price)

    qty_raw = intended_risk / stop_dist
    qty_cap = C.SINGLE_TRADE_CAP_USD / entry_price
    qty = _round_qty(min(qty_raw, qty_cap), lot_dec)
    if qty < ordermin:
        return None

    stop   = round(entry_price - stop_dist, pair_dec)
    target = round(entry_price + C.TP_ATR_MULT * atr_5m, pair_dec)

    realized_risk = qty * stop_dist
    if C.MIN_REALIZED_RISK_FRAC > 0 and realized_risk < C.MIN_REALIZED_RISK_FRAC * intended_risk:
        return None

    maker_edge_bps = C.MAKER_SPREAD_FRACTION * float(spread_median_bps)
    cost_bps = C.MAKER_BPS + C.TAKER_BPS + C.SLIPPAGE_OUT_BPS

    return {
        "qty": qty,
        "stop": stop,
        "target": target,
        "realized_risk_usd": realized_risk,
        "intended_risk_usd": intended_risk,
        "pair_decimals": pair_dec,
        "lot_decimals": lot_dec,
        "ordermin": ordermin,
        "note": f"edge_bps≈{maker_edge_bps:.1f} vs cost_bps≈{cost_bps:.1f}",
    }
