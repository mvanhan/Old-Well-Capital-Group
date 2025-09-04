#!/usr/bin/env python3
"""
Stable-pair mean-reversion live submitter (Coinbase Advanced)

- Reads output_stables/trade_tickets_latest.csv
- Validates product increments and sizes
- Submits a post-only LIMIT entry with an attached trigger_bracket_gtc (TP limit + SL trigger)
- Ensures TP>entry and SL<entry (long) after rounding / nudging
- Uses idempotent client_order_id

This runner assumes your private client exposes ONE of these call surfaces:

1) broker.coinbase_private.trigger_bracket_limit_maker(
       product_id, side, base_size, entry_price, tp_limit_price, stop_trigger_price, client_order_id
   ) -> dict

2) broker.coinbase_private.add_order_limit_with_bracket(
       product_id=..., side=..., base_size=..., limit_price=..., tp_limit_price=..., stop_trigger_price=..., post_only=True, client_order_id=...
   ) -> dict

If your method name differs, add a small adapter below.
"""

from __future__ import annotations
import csv
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Tuple

from utils.precision import q, round_price, round_size
from risk.healthchecks import ok_to_trade_now
from broker import coinbase_public as cb_pub

# Try to import a placement function from your private client
try:
    from broker.coinbase_private import trigger_bracket_limit_maker as _submit_bracket
except Exception:
    _submit_bracket = None
if _submit_bracket is None:
    try:
        from broker.coinbase_private import add_order_limit_with_bracket as _submit_bracket  # type: ignore
    except Exception:
        _submit_bracket = None

TICKET = Path("output_stables/trade_tickets_latest.csv")


def _load_ticket() -> dict:
    if not TICKET.exists():
        raise FileNotFoundError(f"No ticket found at {TICKET}")
    with TICKET.open() as f:
        r = list(csv.DictReader(f))
    if not r:
        raise RuntimeError("Ticket CSV is empty")
    return r[0]


def _product_specs(product_id: str) -> Tuple[Decimal, Decimal, Decimal]:
    """
    Return (base_increment, quote_increment, min_size)
    """
    prods = cb_pub.get_products()
    for p in prods:
        if p["product_id"] == product_id:
            base_inc = q(p.get("base_increment", "0.00000001"))
            quote_inc = q(p.get("quote_increment", "0.00000001"))
            min_size = q(p.get("min_order_size", p.get("base_min_size", "0.0")) or "0.0")
            return base_inc, quote_inc, min_size
    raise KeyError(f"Unknown product_id: {product_id}")


def _validate_and_nudge(product_id: str, side: str, entry: Decimal, tp: Decimal, sl: Decimal, size: Decimal):
    base_inc, quote_inc, min_size = _product_specs(product_id)

    # Rounding + nudges
    entry = round_price(entry, quote_inc, "down" if side == "BUY" else "up")
    tp    = round_price(tp, quote_inc, "up" if side == "BUY" else "down")
    sl    = round_price(sl, quote_inc, "down" if side == "BUY" else "up")
    size  = round_size(size, base_inc, "down")

    # Ensure inequalities post rounding
    if side == "BUY":
        if tp <= entry:
            tp = round_price(entry + quote_inc, quote_inc, "up")
        if sl >= entry:
            sl = round_price(entry - quote_inc, quote_inc, "down")
    else:
        if tp >= entry:
            tp = round_price(entry - quote_inc, quote_inc, "down")
        if sl <= entry:
            sl = round_price(entry + quote_inc, quote_inc, "up")

    # Min size
    if min_size > 0 and size < min_size:
        size = round_size(min_size, base_inc, "up")

    return entry, tp, sl, size


def main() -> None:
    if _submit_bracket is None:
        print("[stables-live] ERROR: No placement function found in broker.coinbase_private.", file=sys.stderr)
        sys.exit(2)

    if not ok_to_trade_now():
        print("[stables-live] Trading paused by health checks")
        sys.exit(0)

    t = _load_ticket()
    product_id = t["product_id"]
    side = t["side"].upper()
    entry = q(t["entry_price"])
    tp = q(t["tp_price"])
    sl = q(t["stop_trigger"])
    size = q(t["base_size"])
    hold_minutes = int(t.get("hold_minutes", "180"))

    # Pre-flight validate increments / size and nudge if needed
    entry, tp, sl, size = _validate_and_nudge(product_id, side, entry, tp, sl, size)

    # Confirm current bid/ask still compatible (avoid chasing)
    bid, ask = cb_pub.get_best_bid_ask(product_id)
    if side == "BUY" and entry > bid:
        print(f"[stables-live] Entry {entry} > best bid {bid}; will rest and wait (post-only).")
    if side == "SELL" and entry < ask:
        print(f"[stables-live] Entry {entry} < best ask {ask}; will rest and wait (post-only).")

    client_order_id = f"stables-{product_id}-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    print("[stables-live] Submitting:",
          dict(product_id=product_id, side=side, base_size=str(size),
               entry_price=str(entry), tp_price=str(tp), stop_trigger=str(sl),
               client_order_id=client_order_id))

    try:
        resp = _submit_bracket(
            product_id=product_id,
            side=side,
            base_size=str(size),
            limit_price=str(entry),
            tp_limit_price=str(tp),
            stop_trigger_price=str(sl),
            post_only=True,
            client_order_id=client_order_id,
        )
        print("[stables-live] Order response:", resp)
        print(f"[stables-live] Hold budget: {hold_minutes} minutes (runner/ops should manage time-out exits).")
    except Exception as e:
        print("[stables-live] ERROR placing order:", repr(e), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
