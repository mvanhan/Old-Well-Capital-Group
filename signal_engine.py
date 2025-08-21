# signal_engine.py — build maker tickets and compute PnL scenarios
from dataclasses import dataclass
from typing import List, Dict, Any
import config as C
from sizing import compute_order_for_pair

@dataclass
class Ticket:
    coin_id: str
    kraken_pair: str
    side: str          # "buy" (long) for now
    price: float       # entry
    qty: float
    stop: float
    take_profit: float
    mid: float
    spread_median_bps: float
    rv_1d_pct: float
    inefficiency_score: float
    status: str = "SUGGESTED"

def make_maker_tickets(row: Dict[str, Any]) -> List[Ticket]:
    """
    row: dict with keys: coin_id, symbol, kraken_pair, mid, spread_median_bps, atr_5m, rv_1d_pct, inefficiency_score
    Returns 0 or 1 Ticket (long-only) sized via compute_order_for_pair.
    """
    pair = row["kraken_pair"]
    mid = float(row["mid"])
    spread_bps = float(row.get("spread_median_bps", 0.0))
    atr_5m = float(row.get("atr_5m", 0.0))

    # target maker entry ~ bid (mid - 0.5 * spread)
    spread_abs = mid * (spread_bps / 10000.0)
    entry = mid - 0.5 * spread_abs

    order = compute_order_for_pair(pair, entry_price=entry, atr_5m=atr_5m, spread_median_bps=spread_bps)
    if order is None:
        return []

    t = Ticket(
        coin_id=row["coin_id"],
        kraken_pair=pair,
        side="buy",
        price=round(entry, order["pair_decimals"]),
        qty=order["qty"],
        stop=order["stop"],
        take_profit=order["target"],
        mid=mid,
        spread_median_bps=spread_bps,
        rv_1d_pct=float(row.get("rv_1d_pct", float("nan"))),
        inefficiency_score=float(row.get("inefficiency_score", float("nan"))),
    )
    return [t]

def ticket_pnl_scenarios(t: Ticket, maker_bps: float, taker_bps: float, slippage_out_bps: float):
    """Compute naive fees and PnL if TP or Stop hit (USD terms)."""
    entry_fee = t.price * t.qty * (maker_bps / 10000.0)
    tp_fee    = t.take_profit * t.qty * ((maker_bps if t.take_profit > t.price else taker_bps) / 10000.0)
    stop_fee  = t.stop * t.qty * (taker_bps / 10000.0)

    # Slippage on exit only (sell)
    sl_out_abs = t.price * (slippage_out_bps / 10000.0)
    tp_exec   = t.take_profit - sl_out_abs
    stop_exec = t.stop - sl_out_abs

    pnl_tp   = (tp_exec - t.price) * t.qty - entry_fee - tp_fee
    pnl_stop = (stop_exec - t.price) * t.qty - entry_fee - stop_fee

    return {
        "entry_fee_usd": entry_fee,
        "exit_tp_fee_usd": tp_fee,
        "exit_stop_fee_usd": stop_fee,
        "pnl_if_tp_usd": pnl_tp,
        "pnl_if_stop_usd": pnl_stop,
    }
