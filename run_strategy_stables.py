#!/usr/bin/env python3
"""
Stable-pair mean-reversion screener for Coinbase Advanced.

Hands-off upgrades:
- **Bankroll-aware sizing**: target notional is a fraction of live bankroll
  (USD + USDT + USDC ≈ $1 each), so profits auto-reinvest.
- **Balance caps**: SELL is capped by BASE on-hand, BUY by QUOTE on-hand
  (so you never get INSUFFICIENT).
- **UTF-8 CSV writes** (Windows-safe).
- Tunable via env vars or CLI flags.

Default universe: USDT-USDC (you can add more later).

Env knobs (all optional):
  STABLES_PCT=0.06           # 6% of bankroll per trade (default 0.06)
  STABLES_MIN_NOTIONAL=8     # floor per trade in $ (default 8)
  STABLES_MAX_NOTIONAL=20    # ceiling per trade in $ (default 20)
  STABLES_ENTRY_BPS=1        # 1.0 bps entry threshold
  STABLES_EXIT_BPS=0.5       # 0.5 bps take-profit
  STABLES_SAFETY_BPS=0.5
  STABLES_SLIPPAGE_BPS=0.3
  STABLES_TAKER_BPS=0
  STABLES_HOLD_MINUTES=180
"""

from __future__ import annotations
import os, csv, time
from decimal import Decimal
from pathlib import Path
from typing import List, Dict, Any, Tuple

# Optional: auto-load .env so Windows users don't need the CLI wrapper
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"), override=True)
except Exception:
    pass

from broker import coinbase_public as cb_pub
from broker import coinbase_private as cb_priv
from owcg_utils.precision import q, round_price, round_size

OUTPUT_DIR = Path("output_stables")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# --- strategy params (bps = basis points) ---
ENTRY_BPS     = Decimal(os.getenv("STABLES_ENTRY_BPS", "1"))
EXIT_BPS      = Decimal(os.getenv("STABLES_EXIT_BPS", "0.5"))
SAFETY_BPS    = Decimal(os.getenv("STABLES_SAFETY_BPS", "0.5"))
SLIPPAGE_BPS  = Decimal(os.getenv("STABLES_SLIPPAGE_BPS", "0.3"))
TAKER_BPS     = Decimal(os.getenv("STABLES_TAKER_BPS", "0"))
HOLD_MINUTES  = int(os.getenv("STABLES_HOLD_MINUTES", "180"))

# bankroll-aware sizing
PCT_OF_BANKROLL   = Decimal(os.getenv("STABLES_PCT", "0.06"))      # 6% default
MIN_NOTIONAL      = Decimal(os.getenv("STABLES_MIN_NOTIONAL", "8"))
MAX_NOTIONAL      = Decimal(os.getenv("STABLES_MAX_NOTIONAL", "20"))

UNIVERSE = ["USDT-USDC"]  # keep it simple and reliable for your current balances

def _bps_to_frac(bps: Decimal) -> Decimal:
    return bps / Decimal(10_000)

def _best_bid_ask(product_id: str) -> Tuple[Decimal, Decimal]:
    bid, ask = cb_pub.get_best_bid_ask(product_id)
    return q(bid), q(ask)

def _write_csv(path: Path, fieldnames: List[str], rows: List[Dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

def _product_specs(product_id: str) -> Tuple[Decimal, Decimal, Decimal]:
    for p in cb_pub.get_products():
        if p.get("product_id") == product_id:
            base_inc  = q(p.get("base_increment", "0.00000001"))
            quote_inc = q(p.get("quote_increment", "0.00000001"))
            min_size  = q(p.get("min_order_size", p.get("base_min_size", p.get("min_order","0")) or "0"))
            return base_inc, quote_inc, min_size
    return q("0.00000001"), q("0.00000001"), q("0")

def _bankroll() -> Decimal:
    # Treat USD/USDT/USDC ~ $1 each for sizing
    usd  = cb_priv.get_available("USD")
    usdt = cb_priv.get_available("USDT")
    usdc = cb_priv.get_available("USDC")
    return usd + usdt + usdc

def _target_notional_from_bankroll() -> Decimal:
    raw = _bankroll() * PCT_OF_BANKROLL
    # clamp to min/max (so size stays sensible with small bankroll)
    return max(MIN_NOTIONAL, min(MAX_NOTIONAL, raw))

def _cap_by_balance(product_id: str, side: str, desired_base: Decimal, entry: Decimal) -> Decimal:
    """
    Cap base size by balances:
      - SELL needs BASE
      - BUY  needs QUOTE (not always USD)
    """
    base, quote = product_id.split("-")
    if side == "SELL":
        have_base = cb_priv.get_available(base)
        return min(desired_base, have_base)
    else:
        have_quote = cb_priv.get_available(quote)
        if entry <= 0:
            return Decimal("0")
        max_buy_base = have_quote / entry
        return min(desired_base, max_buy_base)

def _diag_header() -> List[str]:
    return ["product_id","side","entry_price","tp_price","stop_trigger","base_size","dev_bps","rr","ts","hold_minutes"]

def _make_ticket(product_id: str, side: str, entry: Decimal, tp: Decimal, sl: Decimal,
                 base_size: Decimal, hold_minutes: int) -> Dict[str, Any]:
    return {
        "product_id": product_id,
        "side": side,
        "entry_price": f"{entry:f}",
        "tp_price": f"{tp:f}",
        "stop_trigger": f"{sl:f}",
        "base_size": f"{base_size:f}",
        "dev_bps": "",
        "rr": "",
        "ts": int(time.time()),
        "hold_minutes": hold_minutes,
    }

def main():
    # Diagnostics header with the active “gate”
    gate_req = ENTRY_BPS + SAFETY_BPS + SLIPPAGE_BPS + TAKER_BPS
    print(f"[stables] Active gate: Δ*={(SAFETY_BPS+SLIPPAGE_BPS):.3f} bps; require |Δ| ≥ {gate_req:.3f} bps  "
          f"(entry={ENTRY_BPS} safety={SAFETY_BPS} slip={SLIPPAGE_BPS} taker={TAKER_BPS})")

    rows_diag, ticket_rows = [], []
    for pid in UNIVERSE:
        bid, ask = _best_bid_ask(pid)
        if not (bid and ask):
            continue

        # deviation from $1.0000 (for stable-stable)
        dev_bps = abs((ask - Decimal("1")) / Decimal("1")) * Decimal(10_000)
        should_sell = ask > Decimal("1") + _bps_to_frac(ENTRY_BPS)
        should_buy  = bid < Decimal("1") - _bps_to_frac(ENTRY_BPS)

        # We’ll only signal one side per cycle; prefer whichever actually passes balance caps.
        sides = []
        if should_sell: sides.append("SELL")
        if should_buy:  sides.append("BUY")

        for side in sides:
            # maker entry at top of book on the correct side
            entry = ask if side == "SELL" else bid
            tp = entry * (Decimal("1") - _bps_to_frac(EXIT_BPS)) if side == "SELL" else entry * (Decimal("1") + _bps_to_frac(EXIT_BPS))
            # “sl” is informational in limit-only flow (we use time-based cancel)
            sl = entry * (Decimal("1") + _bps_to_frac(3)) if side == "SELL" else entry * (Decimal("1") - _bps_to_frac(3))

            # bankroll-aware notional (compounds as bankroll grows)
            target_notional = _target_notional_from_bankroll()
            desired_base = (target_notional / entry) if entry > 0 else Decimal("0")

            # cap by balances so we never over-size
            base_capped = _cap_by_balance(pid, side, desired_base, entry)

            # round to increments and ensure min size
            base_inc, quote_inc, min_size = _product_specs(pid)
            base_final = round_size(base_capped, base_inc, mode="down")
            if base_final < min_size:
                # if SELL cap made it too small, try the other side this cycle
                continue

            entry = round_price(entry, quote_inc, mode="nearest")
            tp    = round_price(tp,    quote_inc, mode="nearest")
            sl    = round_price(sl,    quote_inc, mode="nearest")

            rr = (entry - tp) / (sl - entry) if side == "SELL" and sl > entry else \
                 (tp - entry) / (entry - sl) if side == "BUY"  and sl < entry else Decimal("0")

            row = {
                "product_id": pid,
                "side": side,
                "entry_price": f"{entry:f}",
                "tp_price": f"{tp:f}",
                "stop_trigger": f"{sl:f}",
                "base_size": f"{base_final:f}",
                "dev_bps": f"{dev_bps:.2f}",
                "rr": f"{rr:.2f}",
                "ts": int(time.time()),
                "hold_minutes": HOLD_MINUTES,
            }
            rows_diag.append(row)
            ticket_rows.append(_make_ticket(pid, side, entry, tp, sl, base_final, HOLD_MINUTES))
            # only one ticket per cycle to keep it simple & consistent
            break

    # write outputs
    diag_fields = _diag_header()
    _write_csv(OUTPUT_DIR / "screen_latest.csv", diag_fields, rows_diag)
    if ticket_rows:
        _write_csv(OUTPUT_DIR / "trade_tickets_latest.csv", diag_fields, ticket_rows)
        r = rows_diag[0]
        print("[stables] Candidate:", r["product_id"], r["side"],
              "entry=", r["entry_price"], "tp=", r["tp_price"],
              "sl=", r["stop_trigger"], "size=", r["base_size"],
              "dev_bps=", r["dev_bps"], "RR=", r["rr"])
        print("[stables] Files written to output_stables")
    else:
        # if nothing, clear stale ticket
        t = OUTPUT_DIR / "trade_tickets_latest.csv"
        if t.exists():
            try: t.unlink()
            except Exception: pass
        print("[stables] No candidate")

if __name__ == "__main__":
    main()
