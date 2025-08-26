from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, getcontext
from statistics import mean, pstdev
from typing import Dict, List, Optional, Tuple
from datetime import datetime

getcontext().prec = 28  # price math precision

@dataclass
class Orderbook:
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    bid_levels: List[Tuple[Decimal, Decimal]]  # [(price, size), ...]
    ask_levels: List[Tuple[Decimal, Decimal]]

@dataclass
class VenueQuote:
    venue: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal

@dataclass
class FundingPoint:
    ts: datetime
    rate: Decimal  # raw funding per window (8h or per h)

def _zscore(xs: List[float], x: float) -> float:
    if len(xs) < 3:
        return 0.0
    mu = mean(xs)
    sd = pstdev(xs)
    if sd == 0:
        return 0.0
    return (x - mu) / sd

def _bps(x: Decimal) -> float:
    return float(x * Decimal(10000))

def realized_vol_bps(atr_abs: Decimal, mid: Decimal) -> float:
    if mid <= 0:
        return 0.0
    return _bps(atr_abs / mid)

def maker_edge_bps(
    book: Orderbook,
    maker_fee_bps: float,
    rv_bps: float,
    our_order_usd: float,
) -> float:
    """
    Expected maker edge if we join best:
      half_spread - maker_fee - impact_penalty - adverse_selection_penalty
    """
    if book.bid <= 0 or book.ask <= 0 or book.ask <= book.bid:
        return 0.0

    mid = (book.bid + book.ask) / Decimal(2)
    half_spread_bps = _bps((book.ask - book.bid) / (mid * 2))

    top_notional_bid = float(book.bid * book.bid_size)
    top_notional_ask = float(book.ask * book.ask_size)
    top_notional = max(min(top_notional_bid, top_notional_ask), 1e-9)

    impact_penalty_bps = min(half_spread_bps, (our_order_usd / top_notional) * half_spread_bps)
    adverse_penalty_bps = 0.25 * rv_bps

    edge = half_spread_bps - maker_fee_bps - impact_penalty_bps - adverse_penalty_bps
    return max(edge, 0.0)

def liquidity_fragility(book: Orderbook, levels: int = 5) -> float:
    """
    Fragility ↑ if best depth is a small fraction of near-book depth.
    """
    lv = max(1, min(levels, len(book.bid_levels), len(book.ask_levels)))
    bid_top = float(book.bid_size)
    ask_top = float(book.ask_size)
    bid_sum = float(sum([sz for _, sz in book.bid_levels[:lv]]))
    ask_sum = float(sum([sz for _, sz in book.ask_levels[:lv]]))

    def _frag(top, total):
        if total <= 0:
            return 10.0
        frac = max(min(top / total, 1.0), 1e-9)
        return (1.0 / frac) - 1.0

    return 0.5 * (_frag(bid_top, bid_sum) + _frag(ask_top, ask_sum))

def fragmentation_bps(venue_quotes: List[VenueQuote]) -> float:
    """
    Cross-venue mid dispersion (bps). 0 if single venue.
    """
    if len(venue_quotes) < 2:
        return 0.0
    mids = [float((q.bid + q.ask) / Decimal(2)) for q in venue_quotes if q.bid > 0 and q.ask > 0]
    if len(mids) < 2:
        return 0.0
    mx, mn = max(mids), min(mids)
    med = sorted(mids)[len(mids)//2]
    if med <= 0:
        return 0.0
    return 10000.0 * (mx - mn) / med

def funding_crowding_z(funding: List[FundingPoint]) -> float:
    """
    Z of most recent funding vs history. Positive => longs crowded.
    """
    if not funding:
        return 0.0
    xs = [float(p.rate) for p in funding]
    return _zscore(xs, xs[-1])

def legacy_score(spread_bps_med: float, rv_bps_1d: float) -> float:
    """
    Preserve intent of old score while damping raw RV emphasis.
    """
    return max(0.0, (10.0 - spread_bps_med)) + 0.1 * rv_bps_1d

def normalize(vals: List[float], mode: str) -> List[float]:
    if mode == "zscore":
        mu = mean(vals) if vals else 0.0
        sd = pstdev(vals) if len(vals) > 1 else 1.0
        sd = sd or 1.0
        return [(v - mu) / sd for v in vals]
    if mode == "minmax":
        if not vals:
            return vals
        mn, mx = min(vals), max(vals)
        if mx == mn:
            return [0.0 for _ in vals]
        return [(v - mn) / (mx - mn) for v in vals]
    return vals
