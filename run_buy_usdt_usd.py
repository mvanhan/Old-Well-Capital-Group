#!/usr/bin/env python3
"""
run_buy_usdt_usd.py
One-off helper to buy USDT on the USDT-USD pair using your existing
(ticket -> run_live_coinbase_stables.place_from_ticket()) pipeline.

Default behavior:
  - Buys ~$130 (USD notional) of USDT on USDT-USD
  - Creates a post-only LIMIT parent with a tiny TP and wider SL (kill),
    just to reuse the same bracket path as your live submitter.
  - Records to output_stables/state.jsonl so the controller sees it.

Examples:
  python -m dotenv run -- python run_buy_usdt_usd.py
  python -m dotenv run -- python run_buy_usdt_usd.py --amount 200
  python -m dotenv run -- python run_buy_usdt_usd.py --amount 75 --hold 45 --kill-bps 100
"""

from __future__ import annotations
import argparse
import csv
import json
import time
from decimal import Decimal
from pathlib import Path

# Try to autoload .env from repo root; safe if missing
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"), override=True)
except Exception:
    pass

from owcg_utils.precision import q
from broker import coinbase_public as cb_pub
from broker import coinbase_private as cb_priv
import run_live_coinbase_stables as submitter

OUTDIR = Path("output_stables")
OUTDIR.mkdir(parents=True, exist_ok=True)
TICKET_PATH = OUTDIR / "trade_tickets_latest.csv"
STATE_PATH = OUTDIR / "state.jsonl"

def _write_state(entry: dict) -> None:
    with STATE_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")

def _ticket_key(product_id: str, side: str, entry_price: str) -> str:
    return f"{product_id}|{side}|{entry_price}"

def main():
    ap = argparse.ArgumentParser(description="Buy USDT on USDT-USD via maker bracket")
    ap.add_argument("--amount", type=str, default="130", help="USD notional to buy (default: 130)")
    ap.add_argument("--hold", type=int, default=60, help="Hold minutes before controller would cancel (default: 60)")
    ap.add_argument("--kill-bps", type=str, default="100", help="Stop distance in bps (default: 100)")
    args = ap.parse_args()

    product_id = "USDT-USD"

    # Verify product exists
    products = {p["product_id"] for p in cb_pub.get_products()}
    if product_id not in products:
        raise RuntimeError(f"Product {product_id} not found on Coinbase Advanced.")

    # Quotes and size from quote-notional (USD)
    bid, ask = cb_pub.get_best_bid_ask(product_id)
    if bid <= 0 or ask <= 0:
        raise RuntimeError(f"Bad quotes for {product_id}: bid={bid} ask={ask}")

    mid = (bid + ask) / Decimal("2")
    usd_notional = q(args.amount)
    size = usd_notional / mid  # base units (USDT)

    # Best-effort balance check for USD
    avail_usd = cb_priv.get_available("USD")
    if avail_usd < usd_notional:
        print(f"[warn] USD available={avail_usd} < requested {usd_notional}. Attempting anyway...")

    # Build BUY ticket:
    # entry ~ bid (maker), TP one tiny tick above, SL below by kill_bps
    tiny = (ask - bid) if (ask - bid) > 0 else Decimal("0.0001")
    entry = bid
    tp    = entry + tiny
    sl    = entry * (Decimal("1.0") - q(args.kill_bps) / Decimal("10000"))

    # Write ticket CSV
    with TICKET_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "ts","product_id","side","entry_price","tp_price","stop_trigger","base_size","hold_minutes","kill_bps"
        ])
        w.writeheader()
        w.writerow({
            "ts": int(time.time()),
            "product_id": product_id,
            "side": "BUY",
            "entry_price": f"{entry:f}",
            "tp_price": f"{tp:f}",
            "stop_trigger": f"{sl:f}",
            "base_size": f"{size:f}",
            "hold_minutes": int(args.hold),
            "kill_bps": f"{q(args.kill_bps):f}",
        })

    print(f"[buy-usdt] Ticket -> {product_id} BUY ~${usd_notional} USD (size≈{size} USDT) @~{entry} (TP {tp}, SL {sl}). Submitting...")
    res = submitter.place_from_ticket()
    print("[buy-usdt] Submit result:", res)

    # Record to state so controller won't duplicate
    entry_rec = {
        "ts": int(time.time()),
        "key": _ticket_key(product_id, "BUY", f"{entry:f}"),
        "product_id": product_id,
        "side": "BUY",
        "entry_price": f"{entry:f}",
        "tp_price": f"{tp:f}",
        "stop_trigger": f"{sl:f}",
        "base_size": f"{size:f}",
        "hold_minutes": int(args.hold),
        "client_order_id": res.get("client_order_id"),
        "order_id": res.get("order_id"),
        "status": (res.get("status") or "NEW").upper(),
    }
    _write_state(entry_rec)
    print(f"[buy-usdt] State recorded to {STATE_PATH}")

if __name__ == "__main__":
    main()
