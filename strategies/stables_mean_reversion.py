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

# NOTE: The original file in your upload is redacted in multiple sections with "..."
# The parameter block below has been relaxed as requested.

@dataclass
class Params:
    # Universe / data
    quote_asset: str = "USD"
    # Primary + fallback candle granularities
    granularity: str = "ONE_MINUTE"
    alt_granularity: str = "FIVE_MINUTE"
    lookback: int = 240               # ~4h of 1m bars
    roll_window: int = 45             # stats window (minutes)  (60 -> 45)
    # Entry filters
    z_entry: float = 1.0              # enter when z <= -z_entry (buy-the-dip)  (1.5 -> 1.0)
    z_stop: float = 2.5               # SL at mean - z_stop*std
    rr_min: float = 1.04              # min reward/risk  (1.10 -> 1.04)
    hl_min: float = 3.0               # half-life minimum (minutes)  (5.0 -> 3.0)
    min_std_ticks: int = 2            # require std >= N ticks  (3 -> 2)
    max_spread_bps: float = 10.0       # skip if instantaneous spread > threshold  (5.0 -> 10.0)
    target_notional_usd: float = 5.0  # runner sizes from this
    # Robustness
    auto_relax: bool = True           # try bounded relax if nothing passes
    relax_steps: int = 3              # how many relax rounds to try
    # Diagnostics
    debug: bool = False               # if True, include more verbose per-pair notes


# -------------------------- Helpers --------------------------

def _to_dict(obj):
    return obj.to_dict() if hasattr(obj, "to_dict") else obj

...

# Many implementation details in your uploaded file are redacted with "..." lines.
# Below we preserve the same structure and return protocol that your runner expects.

def compute_row(pub, product_id: str, candles, roll_window: int) -> Dict[str, Any]:
    """
    Compute stats for a product_id from candles:
    - deviation_bps, entry_price, tp_price, sl_trigger, base_size, rr, etc.
    (Implementation redacted in the uploaded file.)
    """
    ...

def filter_with_relax(rows: List[Dict[str, Any]], params: Params) -> Optional[Any]:
    """
    Pick best candidate per thresholds with bounded auto-relax.
    Returns best candidate (object/dict) or None.
    (Implementation redacted in the uploaded file.)
    """
    ...

def stable_universe(pub, quote_asset: str) -> List[str]:
    """
    Build universe of stable pairs for the given quote asset.
    (Implementation redacted in the uploaded file.)
    """
    ...

def fetch_candles_any(client, product_id: str, primary: str, fallback: str, lookback: int):
    """
    Fetch candles with primary granularity, fallback to alternate if needed.
    (Implementation redacted in the uploaded file.)
    """
    ...

def screen(pub, api_key: str, api_secret: str, params: Params) -> Tuple[Optional[Any], List[Dict[str, Any]]]:
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
