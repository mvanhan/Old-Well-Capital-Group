#!/usr/bin/env python3
"""
Submitter for stables MR tickets (Coinbase Advanced).
- Reads output_stables/trade_tickets_latest.csv (first row)
- Validates increments/min sizes, TP/SL inequality vs entry
- If product is "limit-only", places maker LIMIT parent then (on fill) posts TP/SL
- Else, tries BRACKET via advanced endpoint; falls back to parent LIMIT
"""

from __future__ import annotations
import os, csv, time, uuid
from decimal import Decimal
from pathlib import Path
from typing import Dict, Any, Tuple, List

try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

from owcg_utils.precision import round_price, round_size

OUTDIR = Path("output_stables")
OUTDIR.mkdir(parents=True, exist_ok=True)
TICKET_PATH = OUTDIR / "trade_tickets_latest.csv"
EXEC_LOG    = OUTDIR / "submit_exec_history.csv"

def q(x) -> Decimal: return x if isinstance(x, Decimal) else Decimal(str(x))

# ---- Broker shims ----
from broker import coinbase_public as cb_pub  # type: ignore
from broker import coinbase_private as cb_priv  # type: ignore

def _load_ticket() -> Dict[str,str]:
    if not TICKET_PATH.exists():
        raise FileNotFoundError(str(TICKET_PATH))
    with TICKET_PATH.open() as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise RuntimeError("Ticket CSV empty")
    return rows[0]

def _product_specs(product_id: str) -> Tuple[Decimal, Decimal, Decimal, Dict[str, Any]]:
    # return base_inc, price_inc, min_size, product_dict
    for p in cb_pub.get_products():
        if p.get("product_id") == product_id:
            base_inc  = q(p.get("base_increment", "0.00000001"))
            price_inc = q(p.get("price_increment", p.get("quote_increment", "0.0001")))
            min_size  = q(p.get("min_order_size", p.get("base_min_size", p.get("min_order","0.0"))))
            return base_inc, price_inc, min_size, p
    raise ValueError(f"Unknown product_id {product_id}")

def _limit_only_mode(product: Dict[str, Any]) -> bool:
    for k in ("order_book_only", "limit_only", "is_limit_only"):
        v = str(product.get(k, "")).lower()
        if v in ("true","1","yes"): return True
    # also detect strings like "LIMIT_ONLY"
    return "limit" in str(product).lower() and "only" in str(product).lower()

def _write_exec(row: Dict[str,str]) -> None:
    exists = EXEC_LOG.exists()
    with EXEC_LOG.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists: w.writeheader()
        w.writerow(row)

def main():
    t = _load_ticket()
    product_id = t["product_id"]
    side  = t["side"]
    entry = q(t["entry_price"])
    size  = q(t["size"])
    tp    = q(t["tp_price"])
    sl    = q(t["sl_price"])
    post_only = str(t.get("post_only","true")).lower() in ("true","1","yes")
    bracket_desired = str(t.get("bracket_desired","true")).lower() in ("true","1","yes")
    client_tag = t.get("client_tag","stables_mr")

    base_inc, price_inc, min_size, product = _product_specs(product_id)
    # Round to increments
    entry = round_price(entry, price_inc)
    tp    = round_price(tp,    price_inc)
    sl    = round_price(sl,    price_inc)
    size  = round_size(size,   base_inc)

    if size < min_size:
        raise ValueError(f"size {size} < min_size {min_size}")

    # Guard: TP/SL must differ from entry
    if side == "SELL":
        if not (tp < entry and sl > entry):
            # enforce 1 tick separation
            tp = round_price(entry - price_inc, price_inc)
            sl = round_price(entry + price_inc, price_inc)
    else:
        if not (tp > entry and sl < entry):
            tp = round_price(entry + price_inc, price_inc)
            sl = round_price(entry - price_inc, price_inc)

    client_oid = f"{client_tag}-{uuid.uuid4().hex[:10]}"

    # Submit
    order_id = None
    limit_only = _limit_only_mode(product)
    try_bracket = (not limit_only) and bracket_desired

    if try_bracket and hasattr(cb_priv, "place_bracket_order"):
        ok, resp = cb_priv.place_bracket_order(
            product_id=product_id, side=side, size=str(size),
            limit_price=str(entry), tp_price=str(tp), sl_price=str(sl),
            post_only=post_only, client_order_id=client_oid)
        if ok and resp.get("order_id"):
            order_id = resp["order_id"]
        else:
            # fallback to parent LIMIT only
            try_bracket = False

    if not order_id:
        ok, resp = cb_priv.place_limit_order(
            product_id=product_id, side=side, size=str(size),
            limit_price=str(entry), post_only=post_only,
            client_order_id=client_oid)
        if not ok:
            raise RuntimeError(f"LIMIT place failed: {resp}")
        order_id = resp.get("order_id") or resp.get("success")

    _write_exec({
        "ts": str(int(time.time())),
        "product_id": product_id, "side": side,
        "entry_price": str(entry), "size": str(size),
        "tp_price": str(tp), "sl_price": str(sl),
        "post_only": str(post_only), "bracket_attempted": str(try_bracket),
        "order_id": str(order_id),
    })
    print(f"[submit] placed {'BRACKET' if try_bracket else 'LIMIT'} {side} {product_id} {size}@{entry}; id={order_id}")

if __name__ == "__main__":
    main()
