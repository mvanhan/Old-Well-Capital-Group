# run_strategy_stables.py
"""
Offline/preview scanner for the stables mean-reversion strategy (logs only).
- Scans the universe, logs a diagnostics table + best candidate (if any), and writes
  a timestamped log to logs/stables_scan_*.log.
- Does NOT write any CSVs.
- Does NOT need to be run before live; live runner can scan and trade on its own.
"""

import os
import time
from pathlib import Path
from decimal import Decimal

from dotenv import load_dotenv
load_dotenv()

import yaml

from broker.coinbase_public import CoinbasePublic
from strategies.stables_mean_reversion import StableParams, build_signal

LOGDIR = Path("logs")
LOGDIR.mkdir(exist_ok=True)

def env_or_fail(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Missing required env var: {name}")
    return v

def fmt(x) -> str:
    return f"{x.normalize()}" if isinstance(x, Decimal) else str(x)

def load_params() -> StableParams:
    p = StableParams()
    # Keep defaults for preview; allow auto_relax here if desired
    cfg_file = Path("config.yaml")
    if cfg_file.exists():
        with cfg_file.open("r") as f:
            raw = yaml.safe_load(f) or {}

        cb = raw.get("coinbase") or {}
        if cb.get("quote_asset"):
            p.quote_asset = cb["quote_asset"]

        st = raw.get("stables") or {}
        p.granularity     = st.get("granularity", p.granularity)
        p.alt_granularity = st.get("alt_granularity", p.alt_granularity)
        p.lookback        = int(st.get("lookback", p.lookback))
        p.roll_window     = int(st.get("roll_window", p.roll_window))
        p.z_entry         = float(st.get("z_entry", p.z_entry))
        p.z_stop          = float(st.get("z_stop", p.z_stop))
        p.rr_min          = float(st.get("rr_min", p.rr_min))
        p.hl_min          = float(st.get("hl_min", p.hl_min))
        p.min_std_ticks   = int(st.get("min_std_ticks", p.min_std_ticks))
        p.max_spread_bps  = float(st.get("max_spread_bps", p.max_spread_bps))
        if "auto_relax" in st:
            p.auto_relax   = bool(st["auto_relax"])

        # Risk sizing optional; shown in logs only
        risk = raw.get("risk") or {}
        bankroll = risk.get("bankroll_usd")
        max_pct = risk.get("max_trade_pct")
        if bankroll is not None and max_pct is not None:
            from decimal import ROUND_HALF_UP
            tn = Decimal(str(bankroll)) * (Decimal(str(max_pct)) / Decimal(100))
            p.target_notional_usd = float(tn.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        else:
            br = raw.get("brackets") or {}
            if br.get("target_notional_usd") is not None:
                p.target_notional_usd = float(br["target_notional_usd"])
    p.quote_asset = os.environ.get("COINBASE_QUOTE_ASSET", p.quote_asset)
    return p

def main():
    api_key = env_or_fail("COINBASE_API_KEY")
    api_secret = env_or_fail("COINBASE_API_SECRET")

    params = load_params()
    pub = CoinbasePublic(api_key=api_key, api_secret=api_secret)

    best, rows = build_signal(api_key, api_secret, params, pub)

    ts = time.strftime("%Y%m%d_%H%M%S")
    logp = LOGDIR / f"stables_scan_{ts}.log"
    with logp.open("w") as log:
        def L(s): 
            print(s); log.write(s + "\n")

        L("=== STABLES MEAN-REVERSION SCAN (preview) ===")
        L(f"quote_asset: {params.quote_asset}")
        L(f"granularity={params.granularity} roll_window={params.roll_window} "
          f"z_entry={params.z_entry} rr_min={params.rr_min} hl_min={params.hl_min} "
          f"min_std_ticks={params.min_std_ticks} max_spread_bps={params.max_spread_bps} "
          f"auto_relax={params.auto_relax}")
        L(f"target_notional_usd={params.target_notional_usd}")
        L("")

        if not rows:
            L("[stables-scan] No usable pairs/candles/depth were found right now.")
            L(f"[stables-scan] Log saved: {logp}")
            return

        rows_sorted = sorted(rows, key=lambda r: r["ineff"], reverse=True)
        show = rows_sorted[:10]
        L("--- Top candidates (by inefficiency) ---")
        header = f"{'product':12s} {'z':>8s} {'hl_m':>6s} {'spread_bps':>11s} {'std_ticks':>10s} {'RR':>7s} {'ineff':>9s}"
        L(header)
        for r in show:
            from decimal import Decimal as D
            std_ticks = (r["std"] / r["tick"]) if r["tick"] > 0 else D(0)
            L(f"{r['product_id']:12s} {fmt(r['z']):>8s} "
              f"{(f'{r['hl']:.2f}' if r['hl'] is not None else ''):>6s} "
              f"{float(r['spread_bps']):11.3f} {float(std_ticks):10.2f} "
              f"{float(r['rr']):7.2f} {float(r['ineff']):9.2f}")
        L("")

        if not best:
            L("[stables-scan] No candidate passed current filters.")
            L(f"[stables-scan] Log saved: {logp}")
            return

        L("--- Selected candidate ---")
        L(f"product_id: {best['product_id']}")
        L(f"bid={fmt(best['bid'])} ask={fmt(best['ask'])} "
          f"spread={fmt(best['spread'])} ({float(best['spread_bps']):.2f} bps) tick={fmt(best['tick'])}")
        L(f"mean={fmt(best['mean'])} std={fmt(best['std'])} z={fmt(best['z'])} hl_min={best['hl']:.2f}")
        L(f"[stables-scan] Log saved: {logp}")

if __name__ == "__main__":
    main()
