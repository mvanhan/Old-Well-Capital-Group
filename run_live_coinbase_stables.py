# run_live_coinbase_stables.py
"""
Live runner for the stables mean-reversion strategy (autonomous).
- Scans the stable-pair universe, logs diagnostics, and if a candidate passes, places a
  post-only LIMIT BUY with attached TP/SL bracket (OCO behavior) in ONE API call.
- Does NOT rely on a pre-written ticket or any CSV artifacts.

Logs:
- Writes a timestamped log to logs/stables_live_*.log with the best candidate details
  and a summary table of the top candidates by inefficiency.
"""

import os
import time
from pathlib import Path
from decimal import Decimal

from dotenv import load_dotenv
load_dotenv()

import yaml

from broker.coinbase_public import CoinbasePublic
from broker.coinbase_private import CoinbasePrivate
from strategies.stables_mean_reversion import StableParams, build_signal

LOGDIR = Path("logs")
LOGDIR.mkdir(exist_ok=True)

# ---------- Helpers ----------
def env_or_fail(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Missing required env var: {name}")
    return v

def fmt(x) -> str:
    return f"{x.normalize()}" if isinstance(x, Decimal) else str(x)

def bps(x: Decimal) -> str:
    return f"{x:.2f} bps"

# ---------- Load params (strict by default) ----------
def load_params() -> StableParams:
    p = StableParams()
    # Defaults for live: keep strict; no auto-relax unless you enable it in config.
    p.auto_relax = False

    cfg_file = Path("config.yaml")
    if cfg_file.exists():
        with cfg_file.open("r") as f:
            raw = yaml.safe_load(f) or {}

        cb = raw.get("coinbase") or {}
        if cb.get("quote_asset"):
            p.quote_asset = cb["quote_asset"]

        st = raw.get("stables") or {}
        # All optional; safe if absent
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
        # Only enable auto relax if explicitly asked in config
        if "auto_relax" in st:
            p.auto_relax   = bool(st.get("auto_relax"))

        # Risk → derive target_notional_usd if provided
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

    # env override for quote if present
    p.quote_asset = os.environ.get("COINBASE_QUOTE_ASSET", p.quote_asset)
    return p

def main():
    api_key = env_or_fail("COINBASE_API_KEY")
    api_secret = env_or_fail("COINBASE_API_SECRET")

    params = load_params()
    pub = CoinbasePublic(api_key=api_key, api_secret=api_secret)
    prv = CoinbasePrivate(api_key=api_key, api_secret=api_secret)

    # Build signal & diagnostics (no CSV; just logs)
    best, rows = build_signal(api_key, api_secret, params, pub)

    ts = time.strftime("%Y%m%d_%H%M%S")
    logp = LOGDIR / f"stables_live_{ts}.log"
    with logp.open("w") as log:
        def L(s): 
            print(s); log.write(s + "\n")

        L("=== STABLES MEAN-REVERSION LIVE SCAN ===")
        L(f"quote_asset: {params.quote_asset}")
        L(f"granularity={params.granularity} roll_window={params.roll_window} "
          f"z_entry={params.z_entry} rr_min={params.rr_min} hl_min={params.hl_min} "
          f"min_std_ticks={params.min_std_ticks} max_spread_bps={params.max_spread_bps} "
          f"auto_relax={params.auto_relax}")
        L(f"target_notional_usd={params.target_notional_usd}")
        L("")

        if not rows:
            L("[stables-live] No usable pairs/candles/depth were found right now. Nothing placed.")
            L(f"[stables-live] Log saved: {logp}")
            return

        # Sort diagnostics by inefficiency (desc) and show top 10
        rows_sorted = sorted(rows, key=lambda r: r["ineff"], reverse=True)
        show = rows_sorted[:10]
        L("--- Top candidates (by inefficiency) ---")
        header = f"{'product':12s} {'z':>8s} {'hl_m':>6s} {'spread_bps':>11s} {'std_ticks':>10s} {'RR':>7s} {'ineff':>9s}"
        L(header)
        for r in show:
            std_ticks = (r["std"] / r["tick"]) if r["tick"] > 0 else Decimal(0)
            L(f"{r['product_id']:12s} {fmt(r['z']):>8s} "
              f"{(f'{r['hl']:.2f}' if r['hl'] is not None else ''):>6s} "
              f"{float(r['spread_bps']):11.3f} {float(std_ticks):10.2f} "
              f"{float(r['rr']):7.2f} {float(r['ineff']):9.2f}")
        L("")

        if not best:
            L("[stables-live] No candidate passed strict filters. Nothing placed.")
            L(f"[stables-live] Log saved: {logp}")
            return

        # ----- Place order for best candidate -----
        pid = best["product_id"]
        entry = best["entry"]
        tp = best["tp"]
        sl = best["sl"]

        # Re-round & enforce TP>entry, SL<entry by at least one tick
        entry_r = pub.round_price(pid, entry)
        size = Decimal(str(params.target_notional_usd)) / entry_r if entry_r > 0 else Decimal(0)
        size_r = pub.round_size(pid, size)
        if size_r <= 0:
            size_r = pub.round_size(pid, pub.base_increment(pid))
        tp_r = pub.round_price(pid, tp)
        sl_r = pub.round_price(pid, sl)
        tick = pub.quote_increment(pid)
        if tp_r <= entry_r:
            tp_r = pub.round_price(pid, entry_r + tick)
        if sl_r >= entry_r:
            sl_r = pub.round_price(pid, entry_r - tick)
        if sl_r <= 0:
            sl_r = pub.round_price(pid, tick)

        # Market snapshot for logs
        try:
            bid, ask = pub.best_bid_ask(pid)
            mid = (bid + ask) / 2
            spread = (ask - bid)
            spread_bps = (spread / mid) * 10000 if mid > 0 else Decimal(0)
            tick_bps = (tick / mid) * 10000 if mid > 0 else Decimal(0)
        except Exception:
            bid = ask = mid = spread = spread_bps = tick_bps = Decimal(0)

        reward_bps = ((tp_r - entry_r) / entry_r) * 10000 if entry_r > 0 else Decimal(0)
        risk_bps   = ((entry_r - sl_r) / entry_r) * 10000 if entry_r > 0 else Decimal(0)
        rr_ratio   = (reward_bps / risk_bps) if risk_bps > 0 else Decimal(0)
        denom      = spread_bps + (tick_bps / 2) if (spread_bps + tick_bps) > 0 else Decimal(1)
        ineff      = reward_bps / denom

        L("--- Selected candidate ---")
        L(f"product_id: {pid}")
        L(f"bid={fmt(best['bid'])} ask={fmt(best['ask'])} "
          f"spread={fmt(best['spread'])} ({bps(best['spread_bps'])}) tick={fmt(best['tick'])}")
        L(f"mean={fmt(best['mean'])} std={fmt(best['std'])} z={fmt(best['z'])} hl_min={best['hl']:.2f}")
        L("")
        L("--- Order Plan ---")
        L(f"entry={fmt(entry_r)} size={fmt(size_r)} (~${params.target_notional_usd}) post_only=True")
        L(f"TP={fmt(tp_r)}  SL={fmt(sl_r)}")
        L("")
        L("--- Risk/Reward ---")
        L(f"reward={bps(reward_bps)}  risk={bps(risk_bps)}  RR={float(rr_ratio):.2f}x   ineff={float(ineff):.2f}")
        L("")

        # Place parent + attached bracket
        resp = prv.create_limit_buy_with_bracket(
            product_id=pid,
            base_size=str(size_r),
            limit_price=str(entry_r),
            post_only=True,
            tp_limit_price=str(tp_r),
            sl_stop_trigger_price=str(sl_r),
        )
        ok = resp.get("success", False)
        if not ok:
            L(f"[stables-live] Create order failed: {resp}")
            L(f"[stables-live] Log saved: {logp}")
            raise SystemExit(1)

        sr = resp.get("success_response", {}) or {}
        order_id = sr.get("order_id") or "<unknown>"
        L(f"[ok] order_id={order_id} product_id={pid}")
        L(f"[stables-live] Log saved: {logp}")

if __name__ == "__main__":
    main()
