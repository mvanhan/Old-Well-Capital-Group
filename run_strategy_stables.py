#!/usr/bin/env python3
"""
Stable-pair mean-reversion screener (Coinbase Advanced)

- Discovers the stable-pair universe programmatically (fx_stablecoin==True),
  with a robust heuristic fallback (USD-quoted stables, stable-stable crosses, WBTC/BTC).
- Computes deviation from $1.0000 (USD-quoted) or from parity on stable-stable crosses.
- Selects the best candidate that clears cost gates and risk filters.
- Writes:
    output_stables/screen_latest.csv
    output_stables/trade_tickets_latest.csv   # consumed by run_live_coinbase_stables.py

Key behavior:
- Uses owcg_utils.precision (avoids 'utils' module shadowing).
- Guarantees TP > entry and SL < entry (long) after rounding/nudging (and vice versa for shorts).
- Post-only assumptions; requires |Δ| >= entry_bps + safety/cost gate.
- Coinbase-only; no Kraken imports.
- CLI overrides for thresholds (no need to edit the file).

Example:
  python run_strategy_stables.py --entry 3 --exit 1 --safety 0.5 --slip 0.3 --kill 100 --hold 180 --notional 25
"""

from __future__ import annotations
import argparse
import csv
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

# --- Ensure project root is on sys.path when running as a script ---
import sys, os as _os
_PROJECT_ROOT = _os.path.dirname(_os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# --- Local helpers ---
from owcg_utils.precision import q, round_price, round_size
from risk.healthchecks import ok_to_trade_now

# ---- Public broker surface (must exist in your repo) ----
#   - get_products(): List[dict] with product_id, base_increment, quote_increment, min_order_size, fx_stablecoin, base_name, quote_name
#   - get_best_bid_ask(product_id) -> (Decimal bid, Decimal ask)
from broker import coinbase_public as cb_pub

OUTPUT_DIR = Path("output_stables")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- DEFAULT strategy params: Option A (gate ≈ 3.8 bps) ----
DEFAULT_ENTRY_BPS        = q("3")      # enter when |deviation| ≥ 3 bps
DEFAULT_EXIT_BPS         = q("1")      # rest TP ~ +1 bp (long) / -1 bp (short)
DEFAULT_SAFETY_BPS       = q("0.5")    # cushion above cost floor
DEFAULT_KILL_BPS         = q("100")    # stop trigger at 100 bps from parity
DEFAULT_HOLD_MINUTES     = 180
DEFAULT_TARGET_NOTIONAL  = q("25")
DEFAULT_TAKER_BPS        = q("0")      # maker-only assumption
DEFAULT_SLIPPAGE_BPS     = q("0.3")    # tiny buffer for a tick of noise

# Universe behavior
ALLOW_STABLE_STABLE_HEURISTIC = True   # include stable-stable crosses even if fx_stablecoin flag is absent
ALLOW_WBTC_BTC                = True   # treat WBTC/BTC as parity pair
USD_LIKE = {"USD"}
STABLE_SET = {"USDC", "USDT", "DAI", "PYUSD", "USDP"}


@dataclass
class ProductSpec:
    product_id: str
    base: str
    quote: str
    base_inc: Decimal
    quote_inc: Decimal
    min_size: Decimal
    fx_stablecoin: bool


@dataclass
class Candidate:
    product_id: str
    side: str  # "BUY" or "SELL"
    entry_price: Decimal
    tp_price: Decimal
    sl_trigger: Decimal
    base_size: Decimal
    deviation_bps: Decimal
    rr: Decimal


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stable-pair mean-reversion screener")
    p.add_argument("--entry", type=str, default=str(DEFAULT_ENTRY_BPS), help="Entry threshold in bps (abs deviation)")
    p.add_argument("--exit", type=str, default=str(DEFAULT_EXIT_BPS), help="Exit target in bps from parity")
    p.add_argument("--safety", type=str, default=str(DEFAULT_SAFETY_BPS), help="Safety cushion (bps) added to cost gate")
    p.add_argument("--slip", type=str, default=str(DEFAULT_SLIPPAGE_BPS), help="Expected slippage (bps)")
    p.add_argument("--taker", type=str, default=str(DEFAULT_TAKER_BPS), help="Taker cost (bps) assumed in cost gate")
    p.add_argument("--kill", type=str, default=str(DEFAULT_KILL_BPS), help="Kill-zone stop distance (bps)")
    p.add_argument("--hold", type=int, default=int(DEFAULT_HOLD_MINUTES), help="Hold time budget (minutes)")
    p.add_argument("--notional", type=str, default=str(DEFAULT_TARGET_NOTIONAL), help="Target notional USD per trade")
    p.add_argument("--show-gate", action="store_true", help="Print the active gate (Δ* and total bps required)")
    return p.parse_args()


def _should_include_product(p: dict) -> bool:
    """Return True if product belongs in the stable-pair universe."""
    if p.get("fx_stablecoin"):
        return True
    if not ALLOW_STABLE_STABLE_HEURISTIC:
        return False

    base = (p.get("base_name") or "").upper()
    quote = (p.get("quote_name") or "").upper()
    pid = p.get("product_id", "").upper()

    # Heuristic 1: USD-quoted stables
    if quote in USD_LIKE and base in STABLE_SET:
        return True

    # Heuristic 2: stable-stable crosses (e.g., USDC/DAI if listed)
    if base in STABLE_SET and quote in STABLE_SET:
        return True

    # Heuristic 3: WBTC/BTC pair
    if ALLOW_WBTC_BTC and (
        (base == "WBTC" and quote == "BTC") or (base == "BTC" and quote == "WBTC") or ("WBTC-BTC" in pid)
    ):
        return True

    return False


def _list_stable_products() -> List[ProductSpec]:
    specs: List[ProductSpec] = []
    for p in cb_pub.get_products():
        if not _should_include_product(p):
            continue
        product_id = p["product_id"]
        base = p.get("base_name") or p.get("base_currency") or product_id.split("-")[0]
        quote = p.get("quote_name") or p.get("quote_currency") or product_id.split("-")[1]
        base_inc = q(p.get("base_increment", "0.00000001"))
        quote_inc = q(p.get("quote_increment", "0.00000001"))
        min_size = q(p.get("min_order_size", p.get("base_min_size", "0.0")) or "0.0")
        specs.append(ProductSpec(product_id, base, quote, base_inc, quote_inc, min_size, bool(p.get("fx_stablecoin"))))
    return specs


def _deviation_bps(product: ProductSpec, bid: Decimal, ask: Decimal) -> Decimal:
    mid = (bid + ask) / 2
    # For USD-quoted stables, parity is 1.0000 USD
    if product.quote.upper() in USD_LIKE and product.base.upper() in (STABLE_SET | {"WBTC", "BTC"}):
        return (mid - q("1")) * q("10000")
    # Generic parity for stable-stable crosses (including WBTC/BTC treated as parity)
    return (mid - q("1")) * q("10000")


def _price_targets(
    product: ProductSpec, side: str, bid: Decimal, ask: Decimal,
    ENTRY_BPS: Decimal, EXIT_BPS: Decimal, KILL_BPS: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Compute entry, TP (near parity), SL (kill) with tick rounding and inequality nudges.
    """
    if side == "BUY":
        raw_entry = min(bid, q("1") - ENTRY_BPS / q("10000"))
        entry = round_price(raw_entry, product.quote_inc, mode="down")
        raw_tp = q("1") + EXIT_BPS / q("10000")
        tp = round_price(raw_tp, product.quote_inc, mode="up")
        if tp <= entry:
            tp = round_price(entry + product.quote_inc, product.quote_inc, mode="up")
        raw_sl = q("1") - KILL_BPS / q("10000")
        sl = round_price(raw_sl, product.quote_inc, mode="down")
        if sl >= entry:
            sl = round_price(entry - product.quote_inc, product.quote_inc, mode="down")
        return entry, tp, sl
    else:
        raw_entry = max(ask, q("1") + ENTRY_BPS / q("10000"))
        entry = round_price(raw_entry, product.quote_inc, mode="up")
        raw_tp = q("1") - EXIT_BPS / q("10000")
        tp = round_price(raw_tp, product.quote_inc, mode="down")
        if tp >= entry:
            tp = round_price(entry - product.quote_inc, product.quote_inc, mode="down")
        raw_sl = q("1") + KILL_BPS / q("10000")
        sl = round_price(raw_sl, product.quote_inc, mode="up")
        if sl <= entry:
            sl = round_price(entry + product.quote_inc, product.quote_inc, mode="up")
        return entry, tp, sl


def _size_from_notional(product: ProductSpec, price: Decimal, target_notional_usd: Decimal) -> Decimal:
    if price <= 0:
        return q("0")
    raw = target_notional_usd / price
    sized = round_size(raw, product.base_inc, mode="down")
    if product.min_size > 0 and sized < product.min_size:
        sized = round_size(product.min_size, product.base_inc, mode="up")
    return sized


def _clears_cost_gate(dev_bps: Decimal, ENTRY_BPS: Decimal, SAFETY_BPS: Decimal, TAKER_BPS: Decimal, SLIPPAGE_BPS: Decimal) -> bool:
    delta_star = TAKER_BPS + SLIPPAGE_BPS + SAFETY_BPS
    return abs(dev_bps) >= (ENTRY_BPS + delta_star)


def _screen(ENTRY_BPS: Decimal, EXIT_BPS: Decimal, SAFETY_BPS: Decimal, KILL_BPS: Decimal,
            HOLD_MINUTES: int, TARGET_NOTIONAL_USD: Decimal, TAKER_BPS: Decimal, SLIPPAGE_BPS: Decimal):
    rows = []
    best: Optional[Candidate] = None

    if not ok_to_trade_now():
        return rows, None

    for spec in _list_stable_products():
        try:
            bid, ask = cb_pub.get_best_bid_ask(spec.product_id)
            dev = _deviation_bps(spec, bid, ask)
        except Exception as e:
            rows.append({"product_id": spec.product_id, "status": f"skip: {e}"})
            continue

        side = "BUY" if dev <= -ENTRY_BPS else ("SELL" if dev >= ENTRY_BPS else "")
        if not side:
            rows.append({"product_id": spec.product_id, "deviation_bps": f"{dev:.4f}", "status": "no-signal"})
            continue

        if not _clears_cost_gate(dev, ENTRY_BPS, SAFETY_BPS, TAKER_BPS, SLIPPAGE_BPS):
            rows.append({"product_id": spec.product_id, "deviation_bps": f"{dev:.4f}", "status": "fails Δ* gate"})
            continue

        entry, tp, sl = _price_targets(spec, side, bid, ask, ENTRY_BPS, EXIT_BPS, KILL_BPS)
        size = _size_from_notional(spec, entry, TARGET_NOTIONAL_USD)
        rr = abs(tp - entry) / max(q("0.00000001"), abs(entry - sl))

        rows.append({
            "product_id": spec.product_id, "side": side,
            "bid": f"{bid:f}", "ask": f"{ask:f}",
            "deviation_bps": f"{dev:.4f}",
            "entry": f"{entry:f}", "tp": f"{tp:f}", "sl": f"{sl:f}",
            "base_size": f"{size:f}", "rr": f"{rr:.3f}", "status": "candidate",
        })

        if size > 0 and (best is None or abs(dev) > abs(best.deviation_bps)):
            best = Candidate(spec.product_id, side, entry, tp, sl, size, dev, rr)

    return rows, best


def _write_csv(path: Path, fieldnames: List[str], rows: List[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> None:
    args = parse_args()

    ENTRY_BPS        = q(args.entry)
    EXIT_BPS         = q(args.exit)
    SAFETY_BPS       = q(args.safety)
    SLIPPAGE_BPS     = q(args.slip)
    TAKER_BPS        = q(args.taker)
    KILL_BPS         = q(args.kill)
    HOLD_MINUTES     = int(args.hold)
    TARGET_NOTIONAL_USD = q(args.notional)

    if args.show_gate:
        delta_star = TAKER_BPS + SLIPPAGE_BPS + SAFETY_BPS
        total_gate = ENTRY_BPS + delta_star
        print(f"[stables] Active gate: Δ*={delta_star:.3f} bps; require |Δ| ≥ {total_gate:.3f} bps  "
              f"(entry={ENTRY_BPS} safety={SAFETY_BPS} slip={SLIPPAGE_BPS} taker={TAKER_BPS})")

    rows, best = _screen(
        ENTRY_BPS, EXIT_BPS, SAFETY_BPS, KILL_BPS,
        HOLD_MINUTES, TARGET_NOTIONAL_USD, TAKER_BPS, SLIPPAGE_BPS
    )

    diag_fields = ["product_id", "side", "bid", "ask", "deviation_bps", "entry", "tp", "sl", "base_size", "rr", "status"]
    _write_csv(OUTPUT_DIR / "screen_latest.csv", diag_fields, rows)

    ticket_path = OUTPUT_DIR / "trade_tickets_latest.csv"
    if not best:
        print("[stables] No candidate passed filters. See diagnostics:", OUTPUT_DIR / "screen_latest.csv")
        if ticket_path.exists():
            ticket_path.unlink()
        return

    ticket_fields = ["ts", "product_id", "side", "entry_price", "tp_price", "stop_trigger", "base_size", "hold_minutes", "kill_bps"]
    _write_csv(ticket_path, ticket_fields, [{
        "ts": int(time.time()),
        "product_id": best.product_id,
        "side": best.side,
        "entry_price": f"{best.entry_price:f}",
        "tp_price": f"{best.tp_price:f}",
        "stop_trigger": f"{best.sl_trigger:f}",
        "base_size": f"{best.base_size:f}",
        "hold_minutes": HOLD_MINUTES,
        "kill_bps": f"{KILL_BPS:f}",
    }])

    print("[stables] Candidate:", best.product_id, best.side,
          "entry=", best.entry_price, "tp=", best.tp_price, "sl=", best.sl_trigger,
          "size=", best.base_size, "dev_bps=", f"{best.deviation_bps:.2f}", "RR=", f"{best.rr:.2f}")
    print("[stables] Files written to", OUTPUT_DIR)


if __name__ == "__main__":
    main()
