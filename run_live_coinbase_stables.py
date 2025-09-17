#!/usr/bin/env python3
"""
Stable-pair mean-reversion live submitter (Coinbase Advanced)

- Reads output_stables/trade_tickets_latest.csv
- Validates increments/min sizes and nudges prices to keep inequalities post-rounding
- Submits post-only LIMIT parent with attached trigger_bracket_gtc (TP limit + SL trigger)
- Idempotent client_order_id
- Exposes a callable `place_from_ticket()` so other modules (like the controller) can reuse the logic.

Fixes:
- Avoid 'utils' package shadowing by using owcg_utils.precision
"""

from __future__ import annotations
import csv
import os
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Dict, Any, Tuple

from owcg_utils.precision import q, round_price, round_size
from broker import coinbase_public as cb_pub
from broker import coinbase_private as cb_priv

OUTDIR = Path("output_stables")
LOGDIR = Path("logs")
LOGDIR.mkdir(exist_ok=True)
OUTDIR.mkdir(parents=True, exist_ok=True)

TICKET_PATH = OUTDIR / "trade_tickets_latest.csv"

def _load_ticket() -> Dict[str, str]:
    if not TICKET_PATH.exists():
        raise FileNotFoundError(f"Ticket not found: {TICKET_PATH}")
    with TICKET_PATH.open() as f:
        r = csv.DictReader(f)
        rows = list(r)
    if not rows:
        raise RuntimeError("Ticket CSV is empty.")
    return rows[0]

def _product_specs(product_id: str) -> Tuple[Decimal, Decimal, Decimal]:
    """
    Return (base_increment, quote_increment, min_order_size) for product.
    """
    for p in cb_pub.get_products():
        if p.get("product_id") == product_id:
            base_inc = q(p.get("base_increment", "0.00000001"))
            quote_inc = q(p.get("quote_increment", "0.00000001"))
            min_size = q(p.get("min_order_size", p.get("base_min_size", "0")) or "0")
            return base_inc, quote_inc, min_size
    raise ValueError(f"Unknown product_id {product_id}")

def _validate_and_nudge(product_id: str, side: str, entry: Decimal, tp: Decimal, sl: Decimal, size: Decimal):
    base_inc, quote_inc, min_size = _product_specs(product_id)

    # Round size DOWN to increment
    size = round_size(size, base_inc, mode="down")
    if size < min_size:
        raise ValueError(f"Order size {size} < min_size {min_size} for {product_id}")

    # Round entry to nearest tick
    entry = round_price(entry, quote_inc, mode="nearest")
    tp    = round_price(tp,    quote_inc, mode="nearest")
    sl    = round_price(sl,    quote_inc, mode="nearest")

    # Ensure bracket inequalities hold after rounding
    # BUY: tp > entry and sl < entry
    # SELL: tp < entry and sl > entry (mirror)
    if side == "BUY":
        bid, ask = cb_pub.get_best_bid_ask(product_id)
        if entry > bid:
            entry = round_price(bid, quote_inc, mode="down")
        if tp <= entry:
            tp = round_price(entry + quote_inc, quote_inc, mode="nearest")
        if sl >= entry:
            sl = round_price(entry - quote_inc, quote_inc, mode="nearest")
    else:
        bid, ask = cb_pub.get_best_bid_ask(product_id)
        if entry < ask:
            entry = round_price(ask, quote_inc, mode="up")
        if tp >= entry:
            tp = round_price(entry - quote_inc, quote_inc, mode="nearest")
        if sl <= entry:
            sl = round_price(entry + quote_inc, quote_inc, mode="nearest")

    return entry, tp, sl, size

def place_from_ticket() -> Dict[str, Any]:
    """
    Load the current ticket and submit a post-only limit with trigger bracket.
    Returns a dict with keys: order_id, client_order_id, status, product_id, side, size, entry, tp, sl
    """
    t = _load_ticket()
    product_id = t["product_id"]
    side = t["side"].upper()
    entry = q(t["entry_price"])
    tp    = q(t["tp_price"])
    sl    = q(t["stop_trigger"])
    size  = q(t["base_size"])
    hold_minutes = int(t.get("hold_minutes", "180"))

    entry, tp, sl, size = _validate_and_nudge(product_id, side, entry, tp, sl, size)

    # Final maker sanity relative to best bid/ask
    bid, ask = cb_pub.get_best_bid_ask(product_id)
    if side == "BUY" and entry > bid:
        entry = bid
    if side == "SELL" and entry < ask:
        entry = ask

    client_order_id = f"owcg:{product_id}:{side}:{int(time.time())}:{uuid.uuid4().hex[:8]}"
    resp = cb_priv.add_order_limit_with_bracket(
        product_id=product_id,
        side=side,
        base_size=f"{size:f}",
        limit_price=f"{entry:f}",
        tp_limit_price=f"{tp:f}",
        stop_trigger_price=f"{sl:f}",
        post_only=True,
        client_order_id=client_order_id,
    )

    out = {
        "product_id": product_id,
        "side": side,
        "size": f"{size:f}",
        "entry": f"{entry:f}",
        "tp": f"{tp:f}",
        "sl": f"{sl:f}",
        "client_order_id": client_order_id,
        "order_id": None,
        "status": "",
    }

    try:
        d = resp if isinstance(resp, dict) else (resp.to_dict() if hasattr(resp, "to_dict") else {})
        out["order_id"] = d.get("order_id") or d.get("order", {}).get("order_id")
        out["status"]   = (d.get("status") or d.get("order", {}).get("status") or "").upper()
    except Exception:
        pass

    print(f"[stables-live] Submitted {product_id} {side} size={out['size']} entry={out['entry']} tp={out['tp']} sl={out['sl']} oid={out['order_id']} status={out['status']}")
    return out

def main():
    place_from_ticket()

if __name__ == "__main__":
    main()
