from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Tuple
from datetime import datetime, timezone

from alpha_factors import (
    Orderbook, VenueQuote, FundingPoint,
    maker_edge_bps, liquidity_fragility, fragmentation_bps,
    funding_crowding_z, legacy_score, realized_vol_bps, normalize
)
from overlays import OverlayConfig, apply_event_overlay, apply_funding_bias

# ---------- ADAPTERS (now using our REST adapter) ----------

def _kraken_symbol(symbol: str) -> str:
    return f"{symbol.upper()}/USD"

def get_orderbook(symbol: str, levels: int) -> Orderbook:
    """
    Uses kraken_public_adapter.get_orderbook_levels
    """
    from kraken_public_adapter import get_orderbook_levels
    pair = _kraken_symbol(symbol)
    ob = get_orderbook_levels(pair, levels)
    bid_px, bid_sz = ob["bids"][0]
    ask_px, ask_sz = ob["asks"][0]
    return Orderbook(
        bid=Decimal(str(bid_px)), ask=Decimal(str(ask_px)),
        bid_size=Decimal(str(bid_sz)), ask_size=Decimal(str(ask_sz)),
        bid_levels=[(Decimal(str(p)), Decimal(str(s))) for p, s in ob["bids"]],
        ask_levels=[(Decimal(str(p)), Decimal(str(s))) for p, s in ob["asks"]],
    )

def get_venue_quotes(symbol: str, venues: List[str]) -> List[VenueQuote]:
    quotes: List[VenueQuote] = []
    if "kraken" in venues:
        ob = get_orderbook(symbol, levels=1)
        quotes.append(VenueQuote("kraken", ob.bid, ob.ask, ob.bid_size, ob.ask_size))
    return quotes

def get_atr_and_spread_stats(symbol: str, minutes: int) -> Tuple[Decimal, float]:
    """
    Light ATR proxy from 5m closes; spread median bps from data if available else fallback.
    """
    try:
        from coingecko_client import fetch_prices_1d_5m
        df = fetch_prices_1d_5m(symbol)
    except Exception:
        df = None

    if df is None or len(df) < 10 or "close" not in df:
        return Decimal("0"), 5.0

    closes = df["close"].astype(float).tolist()
    atr = 0.0
    for i in range(1, len(closes)):
        atr += abs(closes[i] - closes[i-1])
    atr /= max(1, len(closes)-1)

    spread_bps_med = float(df.get("spread_bps_med", 5.0))
    return Decimal(str(atr)), spread_bps_med

def get_funding_history(symbol: str, lookback_hours: int) -> List[FundingPoint]:
    try:
        from venues.kraken_futures_public import get_funding_series
        pts = get_funding_series(symbol, hours=lookback_hours)
        return [FundingPoint(ts=p["ts"], rate=Decimal(str(p["rate"]))) for p in pts]
    except Exception:
        return []

# ---------- ALPHA / RANKING ----------

@dataclass
class AlphaInputs:
    weights: Dict[str, float]
    normalize: str
    min_score: float
    take: int
    rv_window_minutes: int
    atr_window_minutes: int
    orderbook_levels: int
    our_order_usd: float
    maker_fee_bps: float
    taker_fee_bps: float
    funding_enable: bool
    funding_fade_th: float
    funding_carry_th: float
    funding_lookback_h: int
    venues: List[str]
    min_cross_venue_spread_bps: float

def compute_alpha_for_universe(symbols: List[str], cfg: AlphaInputs) -> Dict[str, Dict[str, float]]:
    rows: Dict[str, Dict[str, float]] = {}
    maker_edges, fragilities, frags, legacies, fundings = [], [], [], [], []
    tmp_store: Dict[str, Dict[str, float]] = {}

    for sym in symbols:
        ob = get_orderbook(sym, cfg.orderbook_levels)
        atr_abs, spread_med_bps = get_atr_and_spread_stats(sym, cfg.atr_window_minutes)
        rv_bps = realized_vol_bps(atr_abs, (ob.bid + ob.ask) / Decimal(2))
        me = maker_edge_bps(ob, cfg.maker_fee_bps, rv_bps, cfg.our_order_usd)
        frag = liquidity_fragility(ob, cfg.orderbook_levels)
        fz = 0.0
        if cfg.funding_enable:
            fz = funding_crowding_z(get_funding_history(sym, cfg.funding_lookback_h))
        venues = get_venue_quotes(sym, cfg.venues)
        fragm = fragmentation_bps(venues)
        leg = legacy_score(spread_med_bps, rv_bps)

        tmp_store[sym] = {
            "maker_edge": me,
            "liquidity_fragility": frag,
            "fragmentation": fragm,
            "legacy": leg,
            "funding_z": fz
        }
        maker_edges.append(me)
        fragilities.append(frag)
        frags.append(fragm)
        legacies.append(leg)
        fundings.append(fz)

    me_n = normalize(maker_edges, cfg.normalize)
    frag_n = normalize(fragilities, cfg.normalize)
    frg_n = normalize(frags, cfg.normalize)
    leg_n = normalize(legacies, cfg.normalize)
    fz_n  = normalize(fundings, "zscore")

    for i, sym in enumerate(symbols):
        w = cfg.weights
        score = (
            w.get("maker_edge", 0.0)       * me_n[i] +
            w.get("liquidity_fragility",0) * (-frag_n[i]) +
            w.get("fragmentation", 0.0)    * frg_n[i] +
            w.get("legacy", 0.0)           * leg_n[i] +
            w.get("funding_crowding",0.0)  * (-abs(fz_n[i]))
        )
        rows[sym] = {**tmp_store[sym], "score_raw": float(score)}

    return rows

def apply_overlays_and_rank(
    rows: Dict[str, Dict[str, float]],
    cfg: AlphaInputs,
    overlays_cfg: OverlayConfig,
    now_utc: datetime,
) -> List[Tuple[str, Dict[str, float]]]:
    base = {sym: r["score_raw"] for sym, r in rows.items()}
    funding_z = {sym: r.get("funding_z", 0.0) for sym, r in rows.items()}

    biased = apply_funding_bias(
        base,
        funding_z,
        fade_threshold_z=cfg.funding_fade_th,
        carry_threshold_z=cfg.funding_carry_th,
    )

    final_scores = apply_event_overlay(
        biased,
        now_utc,
        overlays_cfg,
    )

    items = [(sym, {**rows[sym], "score": sc}) for sym, sc in final_scores.items() if sc >= cfg.min_score]
    items.sort(key=lambda kv: kv[1]["score"], reverse=True)
    return items[: cfg.take]

def decide_strategy_for_symbol(sym: str, row: Dict[str, float], venues: List[str]) -> Dict[str, str]:
    strat = "maker_spread_capture"
    venue = "kraken"
    if row.get("fragmentation", 0.0) >= 2.0 and len(venues) > 1:
        strat = "venue_dispersion_switch"
    return {"strategy": strat, "venue": venue}

def screen_and_build_candidates(symbols: List[str], config: dict) -> List[Dict]:
    alpha_cfg = AlphaInputs(
        weights=config["alpha"]["weights"],
        normalize=config["alpha"].get("normalize","zscore"),
        min_score=float(config["alpha"]["min_score"]),
        take=int(config["alpha"]["take"]),
        rv_window_minutes=int(config["alpha"]["rv_window_minutes"]),
        atr_window_minutes=int(config["alpha"]["atr_window_minutes"]),
        orderbook_levels=int(config["alpha"]["orderbook_levels"]),
        our_order_usd=float(config["alpha"]["our_order_usd"]),
        maker_fee_bps=float(config["alpha"]["fees_bps"]["maker"]),
        taker_fee_bps=float(config["alpha"]["fees_bps"]["taker"]),
        funding_enable=bool(config["alpha"]["funding"]["enable"]),
        funding_fade_th=float(config["alpha"]["funding"]["fade_threshold_z"]),
        funding_carry_th=float(config["alpha"]["funding"]["carry_threshold_z"]),
        funding_lookback_h=int(config["alpha"]["funding"]["lookback_hours"]),
        venues=list(config["alpha"]["fragmentation"]["venues"]),
        min_cross_venue_spread_bps=float(config["alpha"]["fragmentation"]["min_cross_venue_spread_bps"]),
    )
    overlays_cfg = OverlayConfig(
        events_file=config["overlays"].get("events_file"),
        block_during_events=bool(config["overlays"].get("block_during_events", True)),
        pre_event_minutes=int(config["overlays"].get("pre_event_minutes", 5)),
        post_event_minutes=int(config["overlays"].get("post_event_minutes", 10)),
    )

    rows = compute_alpha_for_universe(symbols, alpha_cfg)
    ranked = apply_overlays_and_rank(rows, alpha_cfg, overlays_cfg, datetime.now(timezone.utc))

    tickets: List[Dict] = []
    for sym, row in ranked:
        strat_info = decide_strategy_for_symbol(sym, row, alpha_cfg.venues)
        tickets.append({
            "symbol": sym,
            "score": row["score"],
            "factors": {
                "maker_edge": row["maker_edge"],
                "funding_z": row["funding_z"],
                "liquidity_fragility": row["liquidity_fragility"],
                "fragmentation": row["fragmentation"],
                "legacy": row["legacy"],
            },
            "strategy": strat_info["strategy"],
            "venue": strat_info["venue"],
            "intent": "join_bid_and_take_half_spread",
        })
    return tickets
