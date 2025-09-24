#!/usr/bin/env python3
"""
Stable-pair mean-reversion live submitter (Coinbase Advanced)

- Loads ticket CSV
- Validates increments/min sizes and inequalities
- Preflights balances (USD for BUY; base coin for SELL)
- Tries PARENT+BRACKET; if limit-only, falls back to LIMIT_ONLY
- If Coinbase returns an unexpected body (no order_id / success=False), prints RAW response and raises
"""

from __future__ import annotations
import csv
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Dict, Any, Tuple

# Auto-load .env on Windows so you don't need the CLI runner
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"), override=True)
except Exception:
    pass

from owcg_utils.precision import q, round_price, round_size
from broker import coinbase_public as cb_pub
from broker import coinbase_private as cb_priv
from requests import HTTPError

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

def _product_specs(product_id: str) -> Tuple[Decimal, Decimal, Decimal, Dict[str, Any]]:
    # Return base_inc, quote_inc, min_size, product_dict
    for p in cb_pub.get_products():
        if p.get("product_id") == product_id:
            base_inc = q(p.get("base_increment", "0.00000001"))
            quote_inc = q(p.get("quote_increment", "0.00000001"))
            # product schema sometimes uses "min_order" instead of "min_order_size"
            min_size = q(p.get("min_order_size", p.get("base_min_size", p.get("min_order", "0"))) or "0")
            return base_inc, quote_inc, min_size, p
    raise ValueError(f"Unknown product_id {product_id}")

def _limit_only_mode(product: Dict[str, Any]) -> bool:
    # Coinbase schemas vary; check common flags/strings
    for k in ("order_book_only", "limit_only", "is_limit_only"):
        v = str(product.get(k, "")).lower()
        if v in ("true", "1"):
            return True
    status = str(product.get("status", "")).lower()
    if "limit" in status and "only" in status:
        return True
    return False

def _validate_and_nudge(product_id: str, side: str, entry: Decimal, tp: Decimal, sl: Decimal, size: Decimal):
    base_inc, quote_inc, min_size, _ = _product_specs(product_id)

    # Round size DOWN to increment
    size = round_size(size, base_inc, mode="down")
    if size < min_size:
        raise ValueError(f"Order size {size} < min_size {min_size} for {product_id}")

    # Round prices to tick
    entry = round_price(entry, quote_inc, mode="nearest")
    tp    = round_price(tp,    quote_inc, mode="nearest")
    sl    = round_price(sl,    quote_inc, mode="nearest")

    # Keep inequalities post-rounding and ensure maker limits don't cross
    bid, ask = cb_pub.get_best_bid_ask(product_id)
    if side == "BUY":
        if entry > bid:  # maker buy must be <= bid to rest on bid side
            entry = round_price(bid, quote_inc, mode="down")
        if tp <= entry:
            tp = round_price(entry + quote_inc, quote_inc, mode="nearest")
        if sl >= entry:
            sl = round_price(entry - quote_inc, quote_inc, mode="nearest")
    else:  # SELL
        if entry < ask:  # maker sell must be >= ask to rest on ask side
            entry = round_price(ask, quote_inc, mode="up")
        if tp >= entry:
            tp = round_price(entry - quote_inc, quote_inc, mode="nearest")
        if sl <= entry:
            sl = round_price(entry + quote_inc, quote_inc, mode="nearest")

    return entry, tp, sl, size

def _balance_preflight(product_id: str, side: str, size: Decimal, entry: Decimal) -> None:
    base, _quote = product_id.split("-")
    if side == "BUY":
        usd_need = size * entry
        usd = cb_priv.get_available("USD")
        if usd < usd_need:
            raise RuntimeError(f"Insufficient USD: need {usd_need}, have {usd}")
    else:
        bal = cb_priv.get_available(base)
        if bal < size:
            raise RuntimeError(f"Insufficient {base}: need {size}, have {bal}")

def _extract_order_fields(resp: Any) -> Dict[str, Any]:
    d = resp if isinstance(resp, dict) else (resp.to_dict() if hasattr(resp, "to_dict") else {})
    # Try multiple shapes/keys
    order_id = (
        d.get("order_id")
        or (d.get("order") or {}).get("order_id")
        or d.get("orderId")
        or (d.get("success_response") or {}).get("order_id")
    )
    status = (
        (d.get("status") or (d.get("order") or {}).get("status") or d.get("orderStatus") or "")
        .upper()
    )
    success = d.get("success")
    return {"raw": d, "order_id": order_id, "status": status, "success": success}

def place_from_ticket() -> Dict[str, Any]:
    t = _load_ticket()
    product_id = t["product_id"]
    side = t["side"].upper()
    entry = q(t["entry_price"])
    tp    = q(t["tp_price"])
    sl    = q(t["stop_trigger"])
    size  = q(t["base_size"])

    entry, tp, sl, size = _validate_and_nudge(product_id, side, entry, tp, sl, size)

    # Maker sanity relative to current book (double-check after nudge)
    bid, ask = cb_pub.get_best_bid_ask(product_id)
    if side == "BUY" and entry > bid:
        entry = bid
    if side == "SELL" and entry < ask:
        entry = ask

    # Balances (preflight)
    _balance_preflight(product_id, side, size, entry)

    # Product mode (if we can detect it, skip bracket up front)
    _, _, _, product = _product_specs(product_id)
    limit_only = _limit_only_mode(product)

    client_order_id = f"owcg:{product_id}:{side}:{int(time.time())}:{uuid.uuid4().hex[:8]}"

    used_mode = "PARENT+BRACKET"
    resp = None
    try:
        if limit_only:
            # Known limit-only — go straight to parent LIMIT
            used_mode = "LIMIT_ONLY"
            resp = cb_priv.add_order_limit_only(
                product_id=product_id,
                side=side,
                base_size=f"{size:f}",
                limit_price=f"{entry:f}",
                post_only=True,
                client_order_id=client_order_id,
            )
        else:
            # Try bracket first
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
    except HTTPError as e:
        msg = str(e)
        if "attached orders are not allowed" in msg.lower() or "limit only mode" in msg.lower():
            used_mode = "LIMIT_ONLY"
            resp = cb_priv.add_order_limit_only(
                product_id=product_id,
                side=side,
                base_size=f"{size:f}",
                limit_price=f"{entry:f}",
                post_only=True,
                client_order_id=client_order_id,
            )
        else:
            raise

    fields = _extract_order_fields(resp)
    order_id = fields["order_id"]
    status   = fields["status"]
    success  = fields["success"]

    if not order_id or (success is False):
        # Print RAW response for diagnosis and raise (so the controller sees it too)
        print(f"[stables-live][RAW_RESPONSE] mode={used_mode} response={fields['raw']}")
        raise RuntimeError("Order was not accepted (no order_id or success=False). See RAW_RESPONSE above.")

    print(
        f"[stables-live] Submitted {product_id} {side} size={size:f} entry={entry:f} "
        f"mode={used_mode} tp={tp:f} sl={sl:f} oid={order_id} status={status}"
    )
    return {
        "product_id": product_id,
        "side": side,
        "mode": used_mode,
        "size": f"{size:f}",
        "entry": f"{entry:f}",
        "tp": f"{tp:f}",
        "sl": f"{sl:f}",
        "client_order_id": client_order_id,
        "order_id": order_id,
        "status": status,
    }

def main():
    place_from_ticket()

if __name__ == "__main__":
    main()
