# signal_engine.py — makes maker spread-capture tickets + PnL scenarios
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List
from utils import bps_to_abs, round_to_decimals

@dataclass
class Ticket:
    coin_id: str
    kraken_pair: str
    side: str               # "buy" or "sell"
    price: float            # entry price (limit)
    qty: float
    stop: float
    take_profit: float
    mid: float
    spread_median_bps: float
    rv_1d_pct: float
    notes: str

def make_maker_tickets(*, coin_id: str, pair: str, mid: float, spread_median_bps: float,
                       price_decimals: int, qty_usd_cap: float, atr_abs: float,
                       stop_mult: float, tp_mult: float, maker_fraction: float) -> List[Ticket]:
    edge_abs = bps_to_abs(spread_median_bps * maker_fraction, mid)
    buy_px  = round_to_decimals(mid - edge_abs, price_decimals)
    sell_px = round_to_decimals(mid + edge_abs, price_decimals)

    # Position size by ATR distance (risk = qty * ATR ≈ per-side risk)
    # Use USD cap as a backstop
    if atr_abs and atr_abs > 0:
        qty = min(qty_usd_cap / max(mid, 1e-8), (qty_usd_cap / atr_abs) * 0.25)  # conservative
    else:
        qty = qty_usd_cap / max(mid, 1e-8)

    stop_buy  = round_to_decimals(buy_px  - stop_mult * atr_abs, price_decimals) if atr_abs else round_to_decimals(buy_px*0.99, price_decimals)
    tp_buy    = round_to_decimals(buy_px  + tp_mult  * atr_abs, price_decimals) if atr_abs else round_to_decimals(buy_px*1.01, price_decimals)
    stop_sell = round_to_decimals(sell_px + stop_mult * atr_abs, price_decimals) if atr_abs else round_to_decimals(sell_px*1.01, price_decimals)
    tp_sell   = round_to_decimals(sell_px - tp_mult  * atr_abs, price_decimals) if atr_abs else round_to_decimals(sell_px*0.99, price_decimals)

    return [
        Ticket(coin_id, pair, "buy",  buy_px,  qty, stop_buy,  tp_buy,  mid, spread_median_bps, 0.0, "maker spread capture"),
        Ticket(coin_id, pair, "sell", sell_px, qty, stop_sell, tp_sell, mid, spread_median_bps, 0.0, "maker spread capture"),
    ]

def ticket_pnl_scenarios(t: Ticket, *, maker_bps: float, taker_bps: float, slippage_out_bps: float):
    """
    Compute PnL at TP and at Stop, assuming maker entry and taker+slippage exit.
    Returns dict with per-trade net PnL USD for TP and Stop.
    """
    # Fees
    entry_fee = maker_bps / 10000.0 * t.price * t.qty
    # Exit notional varies with exit price
    if t.side == "buy":
        pnl_tp_gross   = (t.take_profit - t.price) * t.qty
        pnl_stop_gross = (t.stop - t.price) * t.qty
        exit_tp_fee    = (taker_bps + slippage_out_bps) / 10000.0 * t.take_profit * t.qty
        exit_stop_fee  = (taker_bps + slippage_out_bps) / 10000.0 * t.stop * t.qty
    else:  # sell
        pnl_tp_gross   = (t.price - t.take_profit) * t.qty
        pnl_stop_gross = (t.price - t.stop) * t.qty
        exit_tp_fee    = (taker_bps + slippage_out_bps) / 10000.0 * t.take_profit * t.qty
        exit_stop_fee  = (taker_bps + slippage_out_bps) / 10000.0 * t.stop * t.qty

    pnl_tp_net   = pnl_tp_gross   - entry_fee - exit_tp_fee
    pnl_stop_net = pnl_stop_gross - entry_fee - exit_stop_fee
    return {
        "entry_fee_usd": entry_fee,
        "exit_tp_fee_usd": exit_tp_fee,
        "exit_stop_fee_usd": exit_stop_fee,
        "pnl_if_tp_usd": pnl_tp_net,
        "pnl_if_stop_usd": pnl_stop_net
    }
