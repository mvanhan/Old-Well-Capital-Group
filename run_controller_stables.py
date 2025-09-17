#!/usr/bin/env python3
"""
run_controller_stables.py
Indefinite controller for the stable-pair mean-reversion strategy.

Cycle (every POLL_SEC):
  - Risk gate (ok_to_trade_now).
  - Run screener -> ticket CSV (if any).
  - One-line log with timestamp + PASS/NONE/etc.
  - If PASS and guards OK (no duplicate, no open on product, balances OK):
      submit via run_live_coinbase_stables.place_from_ticket()
      log detailed trade line
  - Monitor open orders; cancel after hold_minutes and log expiry.

ENV (optional):
  STABLES_POLL_SEC   default: 15
  STABLES_DEDUPE_MIN default: 60
  OWCG_TRADING_PAUSED 0/1    (risk pause; optional—Ctrl-C works fine locally)
"""

from __future__ import annotations
import csv
import os
import json
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from owcg_utils.precision import q
from risk.healthchecks import ok_to_trade_now
from broker import coinbase_private as cb_priv

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
    with LOG_PATH.open("a") as f:
        f.write(msg + "\n")

def _write_state(entry: Dict[str, Any]) -> None:
    with STATE_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")

def _iter_state() -> List[Dict[str, Any]]:
    if not STATE_PATH.exists():
        return []
    rows = []
    with STATE_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows

def _latest_open_by_product() -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for row in _iter_state():
        if row.get("status") in {"NEW","OPEN"}:
            latest[row["product_id"]] = row
    return latest

def _load_ticket() -> Optional[Dict[str, str]]:
    if not TICKET_PATH.exists():
        return None
    with TICKET_PATH.open() as f:
        r = csv.DictReader(f)
        rows = list(r)
    return rows[0] if rows else None

def _ticket_age_sec(ticket: Dict[str, str]) -> int:
    # Prefer a ticket['ts'] if present; otherwise fall back to file mtime
    ts_val = ticket.get("ts")
    if ts_val is not None:
        try:
            return max(0, _now_ts() - int(ts_val))
        except Exception:
            pass
    try:
        return max(0, _now_ts() - int(TICKET_PATH.stat().st_mtime))
    except Exception:
        return 999999

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
    base, _quote = product_id.split("-")
    size = q(ticket["base_size"])
    entry = q(ticket["entry_price"])
    if side == "BUY":
        need = size * entry
        usd = cb_priv.get_available("USD")
        return usd >= need
    else:
        bal = cb_priv.get_available(base)
        return bal >= size

def _monitor_and_housekeep() -> None:
    rows = _iter_state()
    updated = []
    modified = False
    for row in rows:
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
        updated.append(row)
    if modified:
        tmp = STATE_PATH.with_suffix(".tmp")
        with tmp.open("w") as f:
            for r in updated:
                f.write(json.dumps(r)+"\n")
        tmp.replace(STATE_PATH)

def main():
    _log(f"[start] controller poll={POLL_SEC}s dedupe={DEDUPE_MIN}m")
    while True:
        try:
            _monitor_and_housekeep()

            if not ok_to_trade_now():
                _log("[cycle] screen=SKIP reason=RISK_OFF")
                time.sleep(POLL_SEC)
                continue

            # 1) Run screener (writes ticket CSV if a candidate exists)
            screener.main()

            # 2) Load ticket and classify outcome
            t = _load_ticket()
            if not t:
                _log("[cycle] screen=NONE")
                time.sleep(POLL_SEC)
                continue

            if not _is_ticket_fresh(t, max_age_sec=max(60, POLL_SEC*2)):
                _log(f"[cycle] screen=STALE age_s={_ticket_age_sec(t)}")
                time.sleep(POLL_SEC)
                continue

            prod = t["product_id"]
            side = t["side"].upper()
            entry = t["entry_price"]
            tp = t["tp_price"]
            sl = t["stop_trigger"]
            size = t["base_size"]

            open_by_prod = _latest_open_by_product()
            if prod in open_by_prod:
                _log(f"[cycle] screen=BLOCKED reason=OPEN_ORDER product={prod}")
                time.sleep(POLL_SEC)
                continue

            if _has_recent_duplicate(t, DEDUPE_MIN):
                _log(f"[cycle] screen=DUP minutes<{DEDUPE_MIN} key={_ticket_key(t)}")
                time.sleep(POLL_SEC)
                continue

            if not _balances_ok(t):
                _log(f"[cycle] screen=INSUFFICIENT product={prod} side={side}")
                time.sleep(POLL_SEC)
                continue

            # 3) Submit
            res = submitter.place_from_ticket()
            state_entry = {
                "ts": _now_ts(),
                "key": _ticket_key(t),
                "product_id": prod,
                "side": side,
                "entry_price": entry,
                "tp_price": tp,
                "stop_trigger": sl,
                "base_size": size,
                "hold_minutes": int(t.get("hold_minutes","180")),
                "client_order_id": res.get("client_order_id"),
                "order_id": res.get("order_id"),
                "status": (res.get("status") or "NEW").upper(),
            }
            _write_state(state_entry)
            _log(
                f"[place] screen=PASS product={prod} side={side} size={size} "
                f"entry={entry} tp={tp} sl={sl} oid={state_entry['order_id']} status={state_entry['status']}"
            )

        except Exception as e:
            _log(f"[error] {type(e).__name__}: {e}")

        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
