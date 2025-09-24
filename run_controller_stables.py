#!/usr/bin/env python3
"""
Indefinite controller for the stable-pair mean-reversion strategy.

Key features:
- One-line logs each cycle (timestamped).
- Places parent entry as post-only maker (submitter auto-falls back to LIMIT_ONLY on limit-only books).
- Emulated bracket: when entry FILLS, we auto-place a maker exit at tp_price.
- Cancels stale orders after `hold_minutes`.
- BUY balance check uses the quote currency (not always USD).
- Robust exit placement: parse success_response.order_id, log RAW response if missing, and retry once.
"""

from __future__ import annotations
import csv, os, json, time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone
from decimal import Decimal

# Auto-load .env (Windows-friendly)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"), override=True)
except Exception:
    pass

from owcg_utils.precision import q, round_price, round_size
from risk.healthchecks import ok_to_trade_now
from broker import coinbase_private as cb_priv
from broker import coinbase_public as cb_pub

import run_strategy_stables as screener
import run_live_coinbase_stables as submitter

OUTDIR = Path("output_stables")
LOGDIR = Path("logs")
STATE_PATH = OUTDIR / "state.jsonl"
TICKET_PATH = OUTDIR / "trade_tickets_latest.csv"
LOG_PATH = LOGDIR / "stables_controller.log"

LOGDIR.mkdir(exist_ok=True)
OUTDIR.mkdir(parents=True, exist_ok=True)

POLL_SEC = int(os.getenv("STABLES_POLL_SEC", "15"))
DEDUPE_MIN = int(os.getenv("STABLES_DEDUPE_MIN", "60"))

def _now_ts() -> int:
    return int(time.time())

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _log(line: str) -> None:
    msg = f"{_iso_now()} {line}"
    print(msg, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")

def _write_state(entry: Dict[str, Any]) -> None:
    with STATE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

def _iter_state() -> List[Dict[str, Any]]:
    if not STATE_PATH.exists(): return []
    rows = []
    with STATE_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try: rows.append(json.loads(line))
                except Exception: pass
    return rows

def _persist_all(rows: List[Dict[str, Any]]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows: f.write(json.dumps(r)+"\n")
    tmp.replace(STATE_PATH)

def _latest_open_by_product() -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for row in _iter_state():
        if row.get("status") in {"NEW","OPEN"} and not row.get("is_exit"):
            latest[row["product_id"]] = row
    return latest

def _load_ticket() -> Optional[Dict[str, str]]:
    if not TICKET_PATH.exists(): return None
    with TICKET_PATH.open(encoding="utf-8") as f:
        r = csv.DictReader(f); rows = list(r)
    return rows[0] if rows else None

def _ticket_age_sec(_: Dict[str, str]) -> int:
    try: return max(0, _now_ts() - int(TICKET_PATH.stat().st_mtime))
    except Exception: return 999999

def _is_ticket_fresh(ticket: Dict[str,str], max_age_sec: int) -> bool:
    return _ticket_age_sec(ticket) <= max_age_sec

def _ticket_key(ticket: Dict[str,str]) -> str:
    return f"{ticket['product_id']}|{ticket['side'].upper()}|{ticket['entry_price']}"

def _has_recent_duplicate(ticket: Dict[str,str], minutes: int) -> bool:
    cutoff = _now_ts() - minutes * 60
    key = _ticket_key(ticket)
    for row in reversed(_iter_state()):
        if row.get("key") == key and row.get("ts", 0) >= cutoff:
            if row.get("status") in {"NEW","OPEN","FILLED"}:
                return True
    return False

def _balances_ok(ticket: Dict[str,str]) -> bool:
    side = ticket["side"].upper()
    product_id = ticket["product_id"]
    base, quote = product_id.split("-")
    size = q(ticket["base_size"])
    entry = q(ticket["entry_price"])
    if side == "BUY":
        # BUY needs QUOTE (not always USD)
        have_quote = cb_priv.get_available(quote)
        need_quote = size * entry
        return have_quote >= need_quote
    else:
        have_base = cb_priv.get_available(base)
        return have_base >= size

# ---- helpers for exit placement ----

def _product_specs(product_id: str) -> Tuple[Decimal, Decimal, Decimal]:
    for p in cb_pub.get_products():
        if p.get("product_id") == product_id:
            base_inc = q(p.get("base_increment","0.00000001"))
            quote_inc = q(p.get("quote_increment","0.00000001"))
            min_size = q(p.get("min_order_size", p.get("base_min_size", p.get("min_order","0")) or "0"))
            return base_inc, quote_inc, min_size
    return q("0.00000001"), q("0.00000001"), q("0")

def _round_order(product_id: str, side: str, price: Decimal, size: Decimal) -> Tuple[Decimal, Decimal]:
    base_inc, quote_inc, min_size = _product_specs(product_id)
    size = round_size(size, base_inc, mode="down")
    price = round_price(price, quote_inc, mode="nearest")
    if size < min_size:
        raise ValueError(f"size {size} < min_size {min_size} for {product_id}")
    bid, ask = cb_pub.get_best_bid_ask(product_id)
    if side == "BUY" and price > bid:
        price = round_price(bid, quote_inc, mode="down")
    if side == "SELL" and price < ask:
        price = round_price(ask, quote_inc, mode="up")
    return price, size

def _extract_oid(resp: Any) -> Tuple[Optional[str], Dict[str, Any]]:
    d = resp if isinstance(resp, dict) else (resp.to_dict() if hasattr(resp, "to_dict") else {})
    oid = (
        d.get("order_id")
        or (d.get("order") or {}).get("order_id")
        or (d.get("success_response") or {}).get("order_id")
    )
    return oid, d

def _place_exit_with_retry(prod: str, side_out: str, base_size: Decimal, tp: Decimal, client_tag: str) -> Optional[str]:
    """Place exit maker limit; parse multiple response shapes, log RAW if missing, retry once."""
    # First attempt at maker-safe price
    price1, size1 = _round_order(prod, side_out, tp, base_size)
    resp1 = cb_priv.add_order_limit_only(
        product_id=prod,
        side=side_out,
        base_size=f"{size1:f}",
        limit_price=f"{price1:f}",
        post_only=True,
        client_order_id=f"{client_tag}|exit",
    )
    oid1, raw1 = _extract_oid(resp1)
    if oid1:
        _log(f"[exit] product={prod} side={side_out} size={size1:f} price={price1:f} oid={oid1}")
        return oid1

    _log(f"[exit][RAW_RESPONSE] product={prod} side={side_out} size={size1:f} price={price1:f} resp={raw1}")

    # Retry with refreshed book (book may have moved by a tick)
    bid2, ask2 = cb_pub.get_best_bid_ask(prod)
    price2 = q(bid2) if side_out == "SELL" else q(ask2)  # maker-safe on second side
    price2, size2 = _round_order(prod, side_out, price2, base_size)
    resp2 = cb_priv.add_order_limit_only(
        product_id=prod,
        side=side_out,
        base_size=f"{size2:f}",
        limit_price=f"{price2:f}",
        post_only=True,
        client_order_id=f"{client_tag}|exit|retry",
    )
    oid2, raw2 = _extract_oid(resp2)
    if oid2:
        _log(f"[exit] product={prod} side={side_out} size={size2:f} price={price2:f} oid={oid2}")
        return oid2

    _log(f"[exit][RAW_RESPONSE] product={prod} side={side_out} size={size2:f} price={price2:f} resp={raw2}")
    return None

def _monitor_and_housekeep() -> None:
    rows = _iter_state()
    updated: List[Dict[str, Any]] = []
    modified = False

    for row in rows:
        # monitor entry
        if row.get("status") in {"NEW","OPEN"}:
            oid = row.get("order_id")
            if oid:
                try:
                    info = cb_priv.get_order(oid) or {}
                    status = (info.get("order", {}).get("status") or info.get("status") or "").upper()
                    if status and status != row["status"]:
                        row["status"] = status
                        row["status_ts"] = _now_ts()
                        modified = True
                        _log(f"[monitor] order_id={oid} product={row['product_id']} status={row['status']}")
                except Exception as e:
                    row.setdefault("errors", []).append(f"get_order_error:{e}")

            # expire after hold_minutes
            started = row.get("ts", _now_ts())
            hold_min = int(row.get("hold_minutes", 180))
            if _now_ts() - started > hold_min * 60 and row.get("status") in {"NEW","OPEN"}:
                if oid:
                    try:
                        cb_priv.cancel_order(oid)
                        _log(f"[expire] order_id={oid} product={row['product_id']} -> CANCELLED (hold_minutes={hold_min})")
                    except Exception as e:
                        row.setdefault("errors", []).append(f"cancel_error:{e}")
                row["status"] = "EXPIRED"
                row["status_ts"] = _now_ts()
                modified = True

        # when ENTRY fills and no exit yet, place exit maker limit at tp_price
        if (not row.get("is_exit")) and row.get("status") == "FILLED" and not row.get("exit_order_id"):
            try:
                prod = row["product_id"]
                base_size = q(row["base_size"])
                tp = q(row["tp_price"])
                side_in = row["side"].upper()
                side_out = "SELL" if side_in == "BUY" else "BUY"

                # preflight for exit
                base, quote = prod.split("-")
                if side_out == "SELL":
                    have = cb_priv.get_available(base)
                    base_size = min(base_size, have)
                else:
                    need_quote = base_size * tp
                    have_quote = cb_priv.get_available(quote)
                    if have_quote < need_quote:
                        _log(f"[exit-block] product={prod} side={side_out} need_{quote}={need_quote} have_{quote}={have_quote}")
                        updated.append(row); continue

                exit_oid = _place_exit_with_retry(prod, side_out, base_size, tp, row.get('client_order_id','owcg'))
                row["exit_order_id"] = exit_oid
                row["exit_side"] = side_out
                row["exit_price"] = f"{tp:f}"  # store target; actual placed price is logged above
                row["exit_size"] = f"{base_size:f}"
                row["exit_ts"] = _now_ts()
                modified = True
            except Exception as e:
                row.setdefault("errors", []).append(f"exit_error:{e}")
                _log(f"[error] exit_place {type(e).__name__}: {e}")

        # monitor exit; mark round trip closed
        if row.get("exit_order_id") and row.get("exit_status") in (None, "NEW", "OPEN"):
            oid = row["exit_order_id"]
            try:
                info = cb_priv.get_order(oid) or {}
                status = (info.get("order", {}).get("status") or info.get("status") or "").upper()
                if status:
                    row["exit_status"] = status
                    row["exit_status_ts"] = _now_ts()
                    modified = True
                    _log(f"[monitor-exit] order_id={oid} product={row['product_id']} status={status}")
                    if status == "FILLED":
                        row["closed_ts"] = _now_ts()
                        row["status_summary"] = "ROUND_TRIP_CLOSED"
            except Exception as e:
                row.setdefault("errors", []).append(f"exit_get_error:{e}")

        updated.append(row)

    if modified:
        _persist_all(updated)

def main():
    _log(f"[start] controller poll={POLL_SEC}s dedupe={DEDUPE_MIN}m")
    while True:
        try:
            _monitor_and_housekeep()

            if not ok_to_trade_now():
                _log("[cycle] screen=SKIP reason=RISK_OFF")
                time.sleep(POLL_SEC)
                continue

            # 1) Run screener (writes a fresh ticket if a candidate exists)
            screener.main()

            # 2) Ticket checks
            t = _load_ticket()
            if not t:
                _log("[cycle] screen=NONE")
                time.sleep(POLL_SEC); continue

            if not _is_ticket_fresh(t, max_age_sec=max(60, POLL_SEC*2)):
                _log(f"[cycle] screen=STALE age_s={_ticket_age_sec(t)}")
                time.sleep(POLL_SEC); continue

            prod = t["product_id"]; side = t["side"].upper()
            open_by_prod = _latest_open_by_product()
            if prod in open_by_prod:
                _log(f"[cycle] screen=BLOCKED reason=OPEN_ORDER product={prod}")
                time.sleep(POLL_SEC); continue

            if _has_recent_duplicate(t, DEDUPE_MIN):
                _log(f"[cycle] screen=DUP minutes<{DEDUPE_MIN} key={_ticket_key(t)}")
                time.sleep(POLL_SEC); continue

            if not _balances_ok(t):
                _log(f"[cycle] screen=INSUFFICIENT product={prod} side={side}")
                time.sleep(POLL_SEC); continue

            # 3) Submit parent (submitter handles bracket vs limit-only)
            res = submitter.place_from_ticket()
            state_entry = {
                "ts": _now_ts(),
                "key": _ticket_key(t),
                "product_id": prod,
                "side": side,
                "entry_price": t["entry_price"],
                "tp_price": t["tp_price"],
                "stop_trigger": t["stop_trigger"],
                "base_size": t["base_size"],
                "hold_minutes": int(t.get("hold_minutes","180")),
                "client_order_id": res.get("client_order_id"),
                "order_id": res.get("order_id"),
                "status": (res.get("status") or "NEW").upper(),
                "mode": res.get("mode",""),
                "is_exit": False,
            }
            _write_state(state_entry)
            _log(f"[place] screen=PASS product={prod} side={side} size={t['base_size']} "
                 f"entry={t['entry_price']} tp={t['tp_price']} sl={t['stop_trigger']} "
                 f"oid={state_entry['order_id']} status={state_entry['status']} mode={state_entry['mode']}")
        except Exception as e:
            _log(f"[error] {type(e).__name__}: {e}")

        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
