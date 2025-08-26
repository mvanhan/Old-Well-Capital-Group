# signal_engine.py — screen, score, and build tickets per new config
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Dict, Tuple

import numpy as np
import pandas as pd

import config as C
from coingecko_client import fetch_prices_1d_5m, coin_id_for_symbol
from kraken_public import find_usd_pairs_for_symbol, ticker_info
from utils import annualized_rv_from_series, atr_from_close
from sizing import compute_order_for_pair


@dataclass
class Candidate:
    symbol: str
    coin_id: str | None
    pair: str
    bid: float
    ask: float
    last: float
    vol24h_base: float
    vol24h_usd: float
    spread_bps: float
    rv_bps: float
    atr_pct: float
    score: float


def _maker_edge_bps(bid: float, ask: float) -> float:
    if bid <= 0 or ask <= 0 or ask <= bid:
        return 0.0
    spread = ask - bid
    mid = (ask + bid) / 2.0
    return (spread / mid) * 1e4  # bps


def _resolve_universe() -> List[Tuple[str, str | None]]:
    """
    Returns list of (symbol, coin_id or None). Supports either:
    - new schema: symbols via C.SCREEN_UNIVERSE_SYMBOLS
    - old tuples: C.UNIVERSE_TUPLES = [(coin_id, "SYM"), ...]
    """
    out: List[Tuple[str, str | None]] = []
    if C.SCREEN_UNIVERSE_SYMBOLS:
        for sym in C.SCREEN_UNIVERSE_SYMBOLS:
            cid = coin_id_for_symbol(sym)
            out.append((str(sym).upper(), cid))
        return out
    if C.UNIVERSE_TUPLES:
        for cid, sym in C.UNIVERSE_TUPLES:
            out.append((str(sym).upper(), str(cid)))
    return out


def screen_and_build_candidates() -> List[Candidate]:
    cands: List[Candidate] = []
    for sym, coin_id in _resolve_universe():
        pairs = find_usd_pairs_for_symbol(sym)
        if not pairs:
            continue
        pair = pairs[0]

        tk = ticker_info(pair)
        bid = float(tk["b"][0])
        ask = float(tk["a"][0])
        last = float(tk["c"][0])
        vol24_base = float(tk["v"][1])  # 24h base volume
        vol24_usd = vol24_base * last
        if C.MIN_VOL_USD_24H and vol24_usd < C.MIN_VOL_USD_24H:
            continue

        me_bps = _maker_edge_bps(bid, ask)

        # Vol/ATR from CoinGecko if coin_id is known
        rv_bps, atr_pct = 0.0, 0.0
        if coin_id:
            px = fetch_prices_1d_5m(coin_id)
            if not px.empty:
                rv = annualized_rv_from_series(px["close"])
                rv_bps = float(np.sqrt(1 / (365 * 288)) * rv * 1e4)
                atr_pct = atr_from_close(px, lookback=C.ATR_LOOKBACK_BARS)

        # simple score (weight knobs can be enriched later)
        w = C.ALPHA_WEIGHTS or {}
        w_maker = float(w.get("maker_edge", 1.0))
        # treat rv as penalty
        score = (w_maker * me_bps) - (0.5 * rv_bps)

        cands.append(
            Candidate(
                symbol=sym,
                coin_id=coin_id,
                pair=pair,
                bid=bid,
                ask=ask,
                last=last,
                vol24h_base=vol24_base,
                vol24h_usd=vol24_usd,
                spread_bps=me_bps,
                rv_bps=rv_bps,
                atr_pct=atr_pct,
                score=float(score),
            )
        )

    # rank and take top N
    cands = sorted(cands, key=lambda x: x.score, reverse=True)[: int(C.TAKE)]
    return cands


def make_maker_tickets(cands: List[Candidate]) -> pd.DataFrame:
    """
    Join the bid (buy). TP uses config 'brackets.tp_offset_bps' as a baseline.
    Stop uses max(MIN_STOP_PCT, ATR * STOP_MULT).
    """
    rows = []
    for c in cands:
        mid = (c.bid + c.ask) / 2.0
        stop_pct = max(C.MIN_STOP_PCT, c.atr_pct * C.STOP_ATR_MULT)

        sz = compute_order_for_pair(mid, stop_pct)
        if sz.qty <= 0:
            continue

        entry = c.bid  # post-only join best bid
        # TP offset from config (bps)
        tp_offset = max(1.0, float(C.TP_OFFSET_BPS)) / 1e4
        tp = entry * (1.0 + tp_offset)
        stop = entry * (1.0 - stop_pct)

        rows.append(
            {
                "symbol": c.symbol,
                "kraken_pair": c.pair,
                "side": "buy",
                "entry_price": round(entry, 10),
                "qty": round(sz.qty, 8),
                "take_profit": round(tp, 10),
                "stop": round(stop, 10),
                "maker_edge_bps": round(c.spread_bps, 2),
                "rv_bps": round(c.rv_bps, 2),
                "atr_pct": round(c.atr_pct * 100.0, 3),
                "score": round(c.score, 2),
                "vol24h_usd": round(c.vol24h_usd, 2),
            }
        )
    return pd.DataFrame(rows)


def ticket_pnl_scenarios(t: pd.Series) -> Tuple[float, float]:
    qty = float(t["qty"])
    entry = float(t["entry_price"])
    tp = float(t["take_profit"])
    st = float(t["stop"])
    pnl_tp = qty * (tp - entry) * (1 - C.TAKER_BPS / 1e4)
    pnl_st = qty * (st - entry) * (1 - C.TAKER_BPS / 1e4)
    return float(pnl_tp), float(pnl_st)
