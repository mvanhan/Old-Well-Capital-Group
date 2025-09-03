# strategies/stables_mean_reversion.py
"""
Stablepairs mean-reversion signal generator (additive module).

- Focus on Coinbase stable pairs with very low/zero maker fees.
- Select pairs that exhibit mean reversion but not so fast that you need HFT.
- Produce:
    • a best candidate ticket (if any pass thresholds, or after bounded auto-relax),
    • a full diagnostics table (rows) for every candidate scanned.

This module is used by run_strategy_stables.py and does NOT modify your main strategy.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal, ROUND_DOWN

from coinbase.rest import RESTClient
from broker.coinbase_public import CoinbasePublic

# -------------------------- Config dataclass --------------------------

@dataclass
class StableParams:
    quote_asset: str = "USD"
    # Primary + fallback candle granularities
    granularity: str = "ONE_MINUTE"
    alt_granularity: str = "FIVE_MINUTE"
    lookback: int = 240               # ~4h of 1m bars
    roll_window: int = 60             # stats window (minutes)
    # Entry filters
    z_entry: float = 1.5              # enter when z <= -z_entry (buy-the-dip)
    z_stop: float = 2.5               # SL at mean - z_stop*std
    rr_min: float = 1.10              # min reward/risk
    hl_min: float = 5.0               # half-life minimum (minutes)
    min_std_ticks: int = 3            # require std >= N ticks
    max_spread_bps: float = 5.0       # skip if instantaneous spread > threshold
    target_notional_usd: float = 5.0  # runner sizes from this
    # Robustness
    auto_relax: bool = True           # try bounded relax if nothing passes
    relax_steps: int = 3              # how many relax rounds to try
    # Diagnostics
    debug: bool = False               # if True, include more verbose per-pair notes


# -------------------------- Helpers --------------------------

def _to_dict(obj):
    return obj.to_dict() if hasattr(obj, "to_dict") else obj

def dec(x) -> Decimal:
    return Decimal(str(x))

# -------------------------- Candle helpers --------------------------

def fetch_candles_any(
    client: RESTClient,
    product_id: str,
    granularity: str,
    alt_granularity: str,
    lookback: int
) -> Optional[List[Dict[str, Any]]]:
    """
    Try primary granularity, then fallback to alt granularity.
    Normalize to list of dicts with keys: start, open, high, low, close, volume.
    """
    # 1) Primary granularity via SDK
    for g in (granularity, alt_granularity):
        try:
            resp = _to_dict(client.get_product_candles(product_id=product_id, granularity=g))
            raw = resp.get("candles") or resp.get("data") or resp
            if isinstance(raw, list) and raw:
                # ensure sorted ascending by start
                raw = sorted(raw, key=lambda c: c.get("start", 0))
                return raw[-lookback:]
        except Exception:
            pass
        # 2) Raw GET fallback for that same granularity
        try:
            path = f"/api/v3/brokerage/products/{product_id}/candles"
            resp = _to_dict(client.get(path, params={"granularity": g}))
            raw = resp.get("candles") or resp.get("data") or []
            if isinstance(raw, list) and raw:
                raw = sorted(raw, key=lambda c: c.get("start", 0))
                return raw[-lookback:]
        except Exception:
            pass
    return None

# -------------------------- Stats --------------------------

def rolling_stats(closes: List[Decimal], window: int) -> Tuple[Decimal, Decimal, Decimal]:
    """
    Return (mean, std, last) over the tail window.
    """
    tail = list(map(dec, closes[-window:]))
    n = len(tail)
    if n == 0:
        return Decimal(0), Decimal(0), Decimal(0)
    mean = sum(tail) / Decimal(n)
    var = sum((x - mean) * (x - mean) for x in tail) / Decimal(n)
    std = var.sqrt()
    last = tail[-1]
    return mean, std, last

def ar1_halflife(series: List[Decimal]) -> Optional[float]:
    """
    AR(1) half-life (bars). Allows mild oscillation (phi < 0) as long as |phi|<1.
    phi = Cov(z_t, z_{t-1}) / Var(z_{t-1})
    halflife = -ln(2)/ln(|phi|) if 0 < |phi| < 1; else None
    """
    if len(series) < 10:
        return None
    z = [dec(x) for x in series]
    z_lag = z[:-1]
    z_now = z[1:]
    n = len(z_lag)
    mean_lag = sum(z_lag) / Decimal(n)
    mean_now = sum(z_now) / Decimal(n)
    cov = sum((z_lag[i] - mean_lag) * (z_now[i] - mean_now) for i in range(n)) / Decimal(n)
    var = sum((v - mean_lag) * (v - mean_lag) for v in z_lag) / Decimal(n)
    if var == 0:
        return None
    phi = cov / var
    try:
        fphi = abs(float(phi))
    except Exception:
        return None
    if fphi <= 0.0 or fphi >= 1.0:
        return None
    import math
    return -math.log(2.0) / math.log(fphi)

# -------------------------- Universe --------------------------

STABLE_BASES = {"USDC", "USDT", "DAI", "PYUSD", "EUROC"}

def stable_universe(pub: CoinbasePublic, quote_asset: str) -> List[str]:
    prods = pub.list_products()
    out = []
    for p in prods:
        pid = p.get("product_id") or p.get("id")
        if not pid or not pid.endswith(f"-{quote_asset.upper()}"):
            continue
        base = pid.split("-")[0]
        if base in STABLE_BASES and not p.get("trading_disabled") and p.get("status") == "online":
            out.append(pid)
    # unique, stable order
    seen = set(); uniq = []
    for pid in out:
        if pid not in seen:
            uniq.append(pid); seen.add(pid)
    return uniq

# -------------------------- Core scan --------------------------

def compute_row(pub: CoinbasePublic, pid: str, candles: List[Dict[str, Any]], roll_window: int) -> Dict[str, Any]:
    bid, ask = pub.best_bid_ask(pid)
    mid = (bid + ask) / 2
    spread = (ask - bid)
    spread_bps = (spread / mid) * 10000 if mid > 0 else Decimal(0)
    tick = pub.quote_increment(pid)
    tick_bps = (tick / mid) * 10000 if mid > 0 else Decimal(0)

    closes = [dec(c.get("close")) for c in candles if c.get("close") is not None]
    mean, std, last = rolling_stats(closes, roll_window)
    z = (last - mean) / std if std > 0 else Decimal(0)

    # build z-series on the tail window for half-life
    zs = []
    if std > 0:
        tail = closes[-roll_window:]
        m, s, _ = rolling_stats(closes, roll_window)
        s = s if s > 0 else Decimal("1")
        for v in tail:
            zs.append((v - m) / s)
    hl = ar1_halflife(zs) if zs else None

    # Candidate TP/SL (pre-filter)
    entry = pub.round_price(pid, bid)
    tp = pub.round_price(pid, mean)
    if tp <= entry:
        tp = pub.round_price(pid, entry + tick)
    sl = pub.round_price(pid, mean - (dec(2.5) * std))
    if sl >= entry:
        sl = pub.round_price(pid, entry - tick)
    if sl <= 0:
        sl = tick
    reward_bps = ((tp - entry) / entry) * 10000 if entry > 0 else Decimal(0)
    risk_bps   = ((entry - sl) / entry) * 10000 if entry > 0 else Decimal(0)
    rr = (reward_bps / risk_bps) if risk_bps > 0 else Decimal(0)
    denom = spread_bps + (tick_bps / 2) if (spread_bps + tick_bps) > 0 else Decimal(1)
    ineff = reward_bps / denom

    return dict(
        product_id=pid,
        bid=bid, ask=ask, mid=mid, spread=spread, spread_bps=spread_bps,
        tick=tick, tick_bps=tick_bps,
        mean=mean, std=std, last=last, z=z, hl=hl,
        entry=entry, tp=tp, sl=sl,
        reward_bps=reward_bps, risk_bps=risk_bps, rr=rr, ineff=ineff
    )

def passes(row: Dict[str, Any], z_entry: float, rr_min: float, hl_min: float,
           min_std_ticks: int, max_spread_bps: float) -> bool:
    if row["mid"] <= 0: return False
    if float(row["spread_bps"]) > max_spread_bps: return False
    if row["std"] <= 0: return False
    std_ticks = row["std"] / row["tick"] if row["tick"] > 0 else Decimal(0)
    if float(std_ticks) < min_std_ticks: return False
    # We buy only when z <= -z_entry
    if float(row["z"]) > -z_entry: return False
    if row["hl"] is None or float(row["hl"]) < hl_min: return False
    if float(row["rr"]) < rr_min: return False
    return True

def filter_with_relax(rows: List[Dict[str, Any]], params: StableParams) -> Optional[Dict[str, Any]]:
    """
    Try strict thresholds first; if none pass and auto_relax is enabled, relax stepwise.
    """
    # strict pass
    winners = [r for r in rows if passes(r, params.z_entry, params.rr_min, params.hl_min,
                                         params.min_std_ticks, params.max_spread_bps)]
    if winners:
        # choose best inefficiency
        return max(winners, key=lambda r: r["eff"] if "eff" in r else r["ineff"])

    if not params.auto_relax:
        return None

    # stepwise relax toward safe floors
    for i in range(1, params.relax_steps + 1):
        z_entry = max(0.5, params.z_entry - 0.3 * i)
        rr_min  = max(0.8, params.rr_min  - 0.1 * i)
        hl_min  = max(2.0, params.hl_min  - 1.0 * i)
        min_ticks = max(1, params.min_std_ticks - i)

        step_pass = [r for r in rows if passes(r, z_entry, rr_min, hl_min, min_ticks, params.max_spread_bps)]
        if step_pass:
            return max(step_pass, key=lambda r: r["ineff"])

    return None

# -------------------------- Public API --------------------------

def build_signal(
    api_key: str,
    api_secret: str,
    params: StableParams,
    pub: CoinbasePublic
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Returns (best_ticket_or_None, diagnostics_rows)
    Each diagnostics row contains full metrics for later CSV/reporting.
    """
    client = RESTClient(api_key=api_key, api_secret=api_secret, timeout=10)
    universe = stable_universe(pub, params.quote_asset)

    rows: List[Dict[str, Any]] = []
    for pid in universe:
        # candles (primary or fallback granularity)
        candles = fetch_candles_any(client, pid, params.granularity, params.alt_granularity, params.lookback)
        if not candles or len(candles) < params.roll_window + 5:
            continue
        try:
            row = compute_row(pub, pid, candles, params.roll_window)
            rows.append(row)
        except Exception:
            # skip this pair quietly; diagnostics are per successful compute
            continue

    if not rows:
        return None, []

    # pick the best per thresholds with auto-relax
    best = filter_with_relax(rows, params)
    return best, rows
