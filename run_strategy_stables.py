#!/usr/bin/env python3
"""
Stable-pair mean-reversion screener (Coinbase Advanced)

- Discovers the stable-pair universe programmatically (fx_stablecoin==True)
- Computes deviation from $1.0000 (USD-quoted) or from parity on stable-stable crosses
- Selects the best candidate that clears cost gates and risk filters
- Writes:
    output_stables/screen_latest.csv
    output_stables/trade_tickets_latest.csv   # consumed by run_live_coinbase_stables.py

Key fixes vs prior version:
- Guarantees TP > entry and SL < entry after rounding (tick nudge)
- Uses post-only assumptions; requires |Δ| >= entry_bps + safety_margin
- Pre-flights product increments and min sizes before ticket creation
- No Kraken imports; Coinbase-only code path

NOTE: This is a *scanner*. It does NOT place orders.
"""

from __future__ import annotations
import csv
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

# --- Local modules (new small helpers) ---
from utils.precision import q, round_price, round_size
from risk.healthchecks import ok_to_trade_now

# ---- Your existing public broker expected surface ----
# Must provide:
#   - get_products(): List[dict] each with product_id, base_increment, quote_increment, min_order_size, fx_stablecoin (bool), base_name, quote_name
#   - get_best_bid_ask(product_id) -> Tuple[Decimal, Decimal]
from broker import coinbase_public as cb_pub

OUTPUT_DIR = Path("output_stables")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- Strategy params (tweak here or move to config.stables.yaml) ----
ENTRY_BPS = q("10")         # enter when discount <= -10 bps (or ratio deviation ≥ +10 bps for the short leg)
EXIT_BPS  = q("2")          # target exit near parity (+2 bps room to be lifted)
SAFETY_BPS = q("5")         # extra buffer above cost floor
KILL_BPS  = q("100")        # flatten if deviation widens beyond -100 bps (true stress)
HOLD_MINUTES = 180          # time budget; runner will enforce
TARGET_NOTIONAL_USD = q("25")  # small tickets while validating live behavior

# Costs/guards for gate calculation
TAKER_BPS = q("0")          # assume 0 if you truly avoid taker; scanner still adds safety buffer
SLIPPAGE_BPS = q("1")       # conservative one-tick-ish cushion

# Universe filters
ALLOW_STABLE_STABLE = True  # allow USDC/DAI etc if listed as a product
USD_LIKE = {"USD"}          # treat USD/USDC books as absolute-par targets


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


def _list_stable_products() -> List[ProductSpec]:
    specs = []
    for p in cb_pub.get_products():
        if not p.get("fx_stablecoin"):
            continue
        product_id = p["product_id"]
        base = p.get("base_name") or p.get("base_currency") or product_id.split("-")[0]
        quote = p.get("quote_name") or p.get("quote_currency") or product_id.split("-")[1]
        base_inc = q(p.get("base_increment", "0.00000001"))
        quote_inc = q(p.get("quote_increment", "0.00000001"))
        min_size = q(p.get("min_order_size", p.get("base_min_size", "0.0")) or "0.0")
        specs.append(ProductSpec(product_id, base, quote, base_inc, quote_inc, min_size, True))
    return specs


def _deviation_bps(product: ProductSpec, bid: Decimal, ask: Decimal) -> Decimal:
    """
    For USD-quoted: deviation from $1 mid in bps (mid-1)*1e4
    For stable-stable crosses: deviation from parity (mid - parity)*1e4, parity≈1
    """
    mid = (bid + ask) / 2
    if product.quote in USD_LIKE and product.base.upper() in {"USDC","DAI","USDT","PYUSD","USDP"}:
        return (mid - q("1")) * q("10000")
    # generic parity target for stable-stable (ratio)
    return (mid - q("1")) * q("10000")


def _price_targets(product: ProductSpec, side: str, bid: Decimal, ask: Decimal) -> Tuple[Decimal, Decimal]:
    """
    Compute candidate entry price (maker), TP near parity, and SL trigger at KILL_BPS.
    Enforce tick rounding + one-tick nudge to guarantee TP>entry (long) or TP<entry (short).
    """
    # Entry target at best bid (BUY) or best ask (SELL), then nudge *inside* as maker
    if side == "BUY":
        raw_entry = min(bid, q("1") - ENTRY_BPS / q("10000"))
        entry = round_price(raw_entry, product.quote_inc, mode="down")
        # TP near 1.0000 + EXIT_BPS (resting ask)
        raw_tp = q("1") + EXIT_BPS / q("10000")
        tp = round_price(raw_tp, product.quote_inc, mode="up")
        if tp <= entry:
            tp = round_price(entry + product.quote_inc, product.quote_inc, mode="up")
        # SL trigger at 1.0000 - KILL_BPS
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


def _clears_cost_gate(dev_bps: Decimal) -> bool:
    delta_star = TAKER_BPS + SLIPPAGE_BPS + SAFETY_BPS
    return abs(dev_bps) >= (ENTRY_BPS + delta_star)


def _screen() -> Tuple[List[dict], Optional[Candidate]]:
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

        if not _clears_cost_gate(dev):
            rows.append({"product_id": spec.product_id, "deviation_bps": f"{dev:.4f}", "status": "fails Δ* gate"})
            continue

        entry, tp, sl = _price_targets(spec, side, bid, ask)
        size = _size_from_notional(spec, entry, TARGET_NOTIONAL_USD)

        rr = abs(tp - entry) / max(q("0.00000001"), abs(entry - sl))

        rows.append({
            "product_id": spec.product_id,
            "side": side,
            "bid": f"{bid:f}",
            "ask": f"{ask:f}",
            "deviation_bps": f"{dev:.4f}",
            "entry": f"{entry:f}",
            "tp": f"{tp:f}",
            "sl": f"{sl:f}",
            "base_size": f"{size:f}",
            "rr": f"{rr:.3f}",
            "status": "candidate",
        })

        # choose most extreme deviation
        if size > 0 and (best is None or abs(dev) > abs(best.deviation_bps)):
            best = Candidate(
                product_id=spec.product_id,
                side=side,
                entry_price=entry,
                tp_price=tp,
                sl_trigger=sl,
                base_size=size,
                deviation_bps=dev,
                rr=rr
            )

    return rows, best


def _write_csv(path: Path, fieldnames: List[str], rows: List[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> None:
    rows, best = _screen()

    # diagnostics table
    diag_fields = ["product_id", "side", "bid", "ask", "deviation_bps", "entry", "tp", "sl", "base_size", "rr", "status"]
    _write_csv(OUTPUT_DIR / "screen_latest.csv", diag_fields, rows)

    if not best:
        print("[stables] No candidate passed filters (Δ* gate / signal). See diagnostics:", OUTPUT_DIR / "screen_latest.csv")
        # clear any stale ticket
        ticket = OUTPUT_DIR / "trade_tickets_latest.csv"
        if ticket.exists():
            ticket.unlink()
        return

    # trade ticket for live runner
    ticket_fields = ["ts", "product_id", "side", "entry_price", "tp_price", "stop_trigger", "base_size", "hold_minutes", "kill_bps"]
    _write_csv(
        OUTPUT_DIR / "trade_tickets_latest.csv",
        ticket_fields,
        [{
            "ts": int(time.time()),
            "product_id": best.product_id,
            "side": best.side,
            "entry_price": f"{best.entry_price:f}",
            "tp_price": f"{best.tp_price:f}",
            "stop_trigger": f"{best.sl_trigger:f}",
            "base_size": f"{best.base_size:f}",
            "hold_minutes": HOLD_MINUTES,
            "kill_bps": f"{KILL_BPS:f}",
        }],
    )

    print("[stables] Candidate:", best.product_id, best.side,
          "entry=", best.entry_price, "tp=", best.tp_price, "sl=", best.sl_trigger,
          "size=", best.base_size, "dev_bps=", f"{best.deviation_bps:.2f}", "RR=", f"{best.rr:.2f}")
    print("[stables] Files written to", OUTPUT_DIR)


if __name__ == "__main__":
    main()
