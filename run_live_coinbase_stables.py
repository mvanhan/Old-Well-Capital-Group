from __future__ import annotations

import csv
import hashlib
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

from broker import coinbase_private as cb_priv  # type: ignore
from broker import coinbase_public as cb_pub  # type: ignore
from owcg_utils.precision import round_price, round_size

OUTDIR = Path("output_stables")
OUTDIR.mkdir(exist_ok=True)
TICKET_PATH = OUTDIR / "trade_tickets_latest.csv"
EXEC_LOG = OUTDIR / "submit_exec_history.csv"
STATE_PATH = OUTDIR / "submitter_state.json"
POLL_SECS = int(os.getenv("SUBMITTER_POLL_SECS", "5"))
PARENT_TTL_SECS = int(os.getenv("PARENT_TTL_SECS", "300"))
EXIT_REPRICE_SECS = int(os.getenv("EXIT_REPRICE_SECS", "10"))
MARKETABLE_EXIT_BUFFER_BPS = Decimal(os.getenv("MARKETABLE_EXIT_BUFFER_BPS", "1.0"))
MAX_TICKET_AGE_SECS = int(os.getenv("MAX_TICKET_AGE_SECS", "30"))


def q(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _now() -> int:
    return int(time.time())


def _load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"stage": "IDLE"}
    try:
        data = json.loads(STATE_PATH.read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"stage": "IDLE"}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def _append_exec(event: str, details: Dict[str, Any]) -> None:
    row = {
        "ts": str(_now()),
        "event": event,
        "details": json.dumps(details, sort_keys=True),
    }
    exists = EXEC_LOG.exists()
    with EXEC_LOG.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def _ticket_hash(row: Dict[str, str]) -> str:
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _load_ticket() -> Optional[Dict[str, str]]:
    if not TICKET_PATH.exists():
        return None
    with TICKET_PATH.open() as handle:
        rows = list(csv.DictReader(handle))
    return rows[0] if rows else None


def _clear_ticket_file() -> None:
    if TICKET_PATH.exists():
        TICKET_PATH.unlink()


def _product_specs(product_id: str) -> Tuple[Decimal, Decimal, Decimal]:
    product = cb_pub.get_product(product_id) if hasattr(cb_pub, "get_product") else None
    if product is None:
        for candidate in cb_pub.get_products():
            if str(candidate.get("product_id")) == product_id:
                product = candidate
                break
    if product is None:
        raise ValueError(f"Unknown product_id {product_id}")
    base_inc = q(product.get("base_increment") or "0.00000001")
    price_inc = q(product.get("price_increment") or product.get("quote_increment") or "0.0001")
    min_size = q(product.get("min_order_size") or product.get("base_min_size") or "0")
    return base_inc, price_inc, min_size


def _normalize_order_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    order = raw.get("order") if isinstance(raw, dict) else None
    return order if isinstance(order, dict) else raw


def _extract_total_size(order: Dict[str, Any]) -> Decimal:
    direct = order.get("base_size") or order.get("size") or order.get("order_size")
    if direct is not None:
        try:
            return q(direct)
        except Exception:
            pass
    config = order.get("order_configuration") or {}
    if isinstance(config, dict):
        for value in config.values():
            if isinstance(value, dict) and value.get("base_size") is not None:
                try:
                    return q(value.get("base_size"))
                except Exception:
                    pass
    return Decimal("0")


def _order_snapshot(order_id: str) -> Dict[str, Any]:
    raw = cb_priv.get_order_status(order_id)
    order = _normalize_order_payload(raw)
    status = str(order.get("status") or "").upper()
    filled_size = q(order.get("filled_size") or "0")
    total_size = _extract_total_size(order)
    completion = q(order.get("completion_percentage") or "0")
    average_filled_price = q(order.get("average_filled_price") or "0")
    return {
        "order_id": str(order.get("order_id") or order_id),
        "client_order_id": str(order.get("client_order_id") or ""),
        "product_id": str(order.get("product_id") or ""),
        "side": str(order.get("side") or "").upper(),
        "status": status,
        "completion_percentage": completion,
        "filled_size": filled_size,
        "total_size": total_size,
        "average_filled_price": average_filled_price,
        "settled": _boolish(order.get("settled")),
        "reject_reason": str(order.get("reject_reason") or ""),
        "reject_message": str(order.get("reject_message") or ""),
        "cancel_message": str(order.get("cancel_message") or ""),
        "raw": raw,
    }


def _is_terminal_rejected(snapshot: Dict[str, Any]) -> bool:
    return snapshot["status"] in {"REJECTED", "FAILED", "EXPIRED"}


def _is_terminal_cancelled(snapshot: Dict[str, Any]) -> bool:
    return snapshot["status"] in {"CANCELLED", "CANCELED"}


def _is_filled(snapshot: Dict[str, Any]) -> bool:
    total_size = snapshot["total_size"]
    filled_size = snapshot["filled_size"]
    if snapshot["status"] in {"FILLED", "DONE", "COMPLETED"}:
        return True
    if total_size > 0 and filled_size >= total_size:
        return True
    if snapshot["completion_percentage"] >= Decimal("100"):
        return True
    return False


def _opposite_side(side: str) -> str:
    return "SELL" if str(side).upper() == "BUY" else "BUY"


def _rounded_size(product_id: str, size: Decimal) -> Decimal:
    base_inc, _, min_size = _product_specs(product_id)
    normalized = round_size(size, base_inc, mode="down")
    if normalized < min_size:
        return Decimal("0")
    return normalized


def _marketable_exit_price(product_id: str, side: str) -> Decimal:
    _, price_inc, _ = _product_specs(product_id)
    bid, ask = cb_pub.get_best_bid_ask(product_id)
    if bid <= 0 or ask <= 0:
        raise RuntimeError(f"Bad quote for {product_id}: bid={bid} ask={ask}")
    if side.upper() == "SELL":
        raw = q(bid) * (Decimal("1") - MARKETABLE_EXIT_BUFFER_BPS / Decimal("10000"))
        return round_price(raw, price_inc, mode="down")
    raw = q(ask) * (Decimal("1") + MARKETABLE_EXIT_BUFFER_BPS / Decimal("10000"))
    return round_price(raw, price_inc, mode="up")


def _place_limit(
    product_id: str,
    side: str,
    size: Decimal,
    price: Decimal,
    post_only: bool,
    client_prefix: str,
) -> Tuple[bool, Dict[str, Any]]:
    base_inc, price_inc, min_size = _product_specs(product_id)
    size = round_size(size, base_inc, mode="down")
    price = round_price(price, price_inc, mode="down" if side.upper() == "SELL" else "up")
    if size < min_size:
        raise ValueError(f"size {size} < min_size {min_size}")
    client_order_id = f"{client_prefix}-{uuid.uuid4().hex[:10]}"
    ok, resp = cb_priv.place_limit_order(
        product_id=product_id,
        side=side,
        size=str(size),
        limit_price=str(price),
        post_only=post_only,
        client_order_id=client_order_id,
    )
    payload = dict(resp)
    payload["client_order_id"] = client_order_id
    payload["normalized_size"] = str(size)
    payload["normalized_price"] = str(price)
    return ok, payload


def _new_ticket_state(ticket: Dict[str, str], ticket_id: str, parent_payload: Dict[str, Any]) -> Dict[str, Any]:
    parent_order_id = str(parent_payload.get("order_id") or (parent_payload.get("success_response") or {}).get("order_id") or "")
    return {
        "stage": "PARENT_WORKING",
        "ticket_id": ticket_id,
        "ticket": ticket,
        "product_id": ticket["product_id"],
        "entry_side": ticket["side"].upper(),
        "exit_side": _opposite_side(ticket["side"]),
        "entry_price": ticket["entry_price"],
        "tp_price": ticket["tp_price"],
        "sl_price": ticket["sl_price"],
        "planned_size": ticket["size"],
        "parent_order_id": parent_order_id,
        "parent_submitted_ts": _now(),
    }


def _submit_parent_from_ticket(ticket: Dict[str, str]) -> Dict[str, Any]:
    product_id = ticket["product_id"]
    side = ticket["side"].upper()
    entry = q(ticket["entry_price"])
    size = q(ticket["size"])
    tp = q(ticket["tp_price"])
    sl = q(ticket["sl_price"])
    post_only = _boolish(ticket.get("post_only", "true"))
    client_tag = ticket.get("client_tag", "stables_mr")

    base_inc, price_inc, min_size = _product_specs(product_id)
    entry = round_price(entry, price_inc, mode="down" if side == "SELL" else "up")
    tp = round_price(tp, price_inc, mode="down" if side == "SELL" else "up")
    sl = round_price(sl, price_inc, mode="down" if side == "SELL" else "up")
    size = round_size(size, base_inc, mode="down")

    if size < min_size:
        raise ValueError(f"size {size} < min_size {min_size}")
    if side == "SELL" and not (tp < entry and sl > entry):
        raise ValueError("SELL ticket has invalid TP/SL relationship")
    if side == "BUY" and not (tp > entry and sl < entry):
        raise ValueError("BUY ticket has invalid TP/SL relationship")

    ok, resp = _place_limit(
        product_id=product_id,
        side=side,
        size=size,
        price=entry,
        post_only=post_only,
        client_prefix=client_tag,
    )
    if not ok:
        raise RuntimeError(resp)

    normalized_ticket = dict(ticket)
    normalized_ticket["entry_price"] = resp["normalized_price"]
    normalized_ticket["size"] = resp["normalized_size"]
    normalized_ticket["tp_price"] = str(tp)
    normalized_ticket["sl_price"] = str(sl)
    normalized_ticket["post_only"] = str(post_only)
    return normalized_ticket, resp


def _submit_tp_exit(state: Dict[str, Any], size: Decimal) -> Dict[str, Any]:
    ok, resp = _place_limit(
        product_id=state["product_id"],
        side=state["exit_side"],
        size=size,
        price=q(state["tp_price"]),
        post_only=True,
        client_prefix="tp",
    )
    if not ok:
        raise RuntimeError(resp)
    state["stage"] = "TP_WORKING"
    state["tp_order_id"] = str(resp.get("order_id") or (resp.get("success_response") or {}).get("order_id") or "")
    state["entry_filled_size"] = str(size)
    state["tp_submitted_ts"] = _now()
    return state


def _submit_sl_exit(state: Dict[str, Any], size: Decimal) -> Dict[str, Any]:
    price = _marketable_exit_price(state["product_id"], state["exit_side"])
    ok, resp = _place_limit(
        product_id=state["product_id"],
        side=state["exit_side"],
        size=size,
        price=price,
        post_only=False,
        client_prefix="sl",
    )
    if not ok:
        raise RuntimeError(resp)
    state["stage"] = "SL_WORKING"
    state["sl_order_id"] = str(resp.get("order_id") or (resp.get("success_response") or {}).get("order_id") or "")
    state["sl_submitted_ts"] = _now()
    return state


def _stop_is_hit(state: Dict[str, Any]) -> bool:
    bid, ask = cb_pub.get_best_bid_ask(state["product_id"])
    sl = q(state["sl_price"])
    if state["entry_side"] == "BUY":
        return q(bid) <= sl
    return q(ask) >= sl


def _remaining_exit_size(state: Dict[str, Any], snapshot: Optional[Dict[str, Any]] = None) -> Decimal:
    entry_size = q(state.get("entry_filled_size") or "0")
    if snapshot is None:
        return entry_size
    remaining = entry_size - snapshot["filled_size"]
    return remaining if remaining > 0 else Decimal("0")


def _cancel_if_possible(order_id: str) -> bool:
    if not order_id:
        return False
    try:
        return cb_priv.cancel_order(order_id)
    except Exception:
        return False


def _handle_idle(state: Dict[str, Any]) -> Dict[str, Any]:
    ticket = _load_ticket()
    if not ticket:
        return state
    ticket_id = ticket.get("ticket_id") or _ticket_hash(ticket)
    if state.get("last_completed_ticket_id") == ticket_id:
        return state
    ticket_ts = int(ticket.get("ts") or 0)
    if ticket_ts and _now() - ticket_ts > MAX_TICKET_AGE_SECS:
        _append_exec("stale_ticket_discarded", {"ticket_id": ticket_id, "ticket": ticket})
        _clear_ticket_file()
        return state
    normalized_ticket, parent_resp = _submit_parent_from_ticket(ticket)
    new_state = _new_ticket_state(normalized_ticket, ticket_id, parent_resp)
    _save_state(new_state)
    _clear_ticket_file()
    _append_exec("parent_submitted", new_state)
    return new_state


def _handle_parent(state: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _order_snapshot(state["parent_order_id"])
    age = _now() - int(state.get("parent_submitted_ts", _now()))
    filled_size = snapshot["filled_size"]

    if _is_terminal_rejected(snapshot):
        _append_exec("parent_rejected", snapshot)
        return {"stage": "IDLE", "last_completed_ticket_id": state["ticket_id"]}

    if _is_filled(snapshot):
        size = _rounded_size(state["product_id"], filled_size)
        if size <= 0:
            _append_exec("parent_filled_zero", snapshot)
            return {"stage": "IDLE", "last_completed_ticket_id": state["ticket_id"]}
        state["entry_avg_price"] = str(snapshot["average_filled_price"] or q(state["entry_price"]))
        next_state = _submit_tp_exit(state, size)
        _append_exec("tp_submitted", next_state)
        return next_state

    if age >= PARENT_TTL_SECS:
        cancelled = _cancel_if_possible(state["parent_order_id"])
        _append_exec("parent_cancel_requested", {"cancelled": cancelled, **snapshot})
        if filled_size > 0:
            size = _rounded_size(state["product_id"], filled_size)
            if size > 0:
                state["entry_avg_price"] = str(snapshot["average_filled_price"] or q(state["entry_price"]))
                next_state = _submit_tp_exit(state, size)
                _append_exec("tp_submitted_after_partial_parent", next_state)
                return next_state
        return {"stage": "IDLE", "last_completed_ticket_id": state["ticket_id"]}

    return state


def _handle_tp(state: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _order_snapshot(state["tp_order_id"])
    if _is_terminal_rejected(snapshot):
        remaining = _remaining_exit_size(state, snapshot)
        if remaining <= 0:
            return {"stage": "IDLE", "last_completed_ticket_id": state["ticket_id"]}
        next_state = _submit_sl_exit(state, remaining)
        _append_exec("tp_rejected_sl_submitted", next_state)
        return next_state

    if _is_filled(snapshot):
        _append_exec("tp_filled", snapshot)
        return {"stage": "IDLE", "last_completed_ticket_id": state["ticket_id"]}

    if _stop_is_hit(state):
        _cancel_if_possible(state["tp_order_id"])
        remaining = _remaining_exit_size(state, snapshot)
        if remaining <= 0:
            return {"stage": "IDLE", "last_completed_ticket_id": state["ticket_id"]}
        next_state = _submit_sl_exit(state, remaining)
        _append_exec("sl_submitted", next_state)
        return next_state

    return state


def _handle_sl(state: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _order_snapshot(state["sl_order_id"])
    if _is_filled(snapshot):
        _append_exec("sl_filled", snapshot)
        return {"stage": "IDLE", "last_completed_ticket_id": state["ticket_id"]}

    remaining = _remaining_exit_size(state, snapshot)
    if remaining <= 0:
        return {"stage": "IDLE", "last_completed_ticket_id": state["ticket_id"]}

    age = _now() - int(state.get("sl_submitted_ts", _now()))
    if _is_terminal_cancelled(snapshot) or _is_terminal_rejected(snapshot) or age >= EXIT_REPRICE_SECS:
        _cancel_if_possible(state["sl_order_id"])
        next_state = _submit_sl_exit(state, remaining)
        _append_exec("sl_repriced", next_state)
        return next_state

    return state


def main() -> None:
    print("[submitter] running; Ctrl+C to stop.")
    while True:
        try:
            state = _load_state()
            stage = state.get("stage", "IDLE")
            if stage == "IDLE":
                state = _handle_idle(state)
            elif stage == "PARENT_WORKING":
                state = _handle_parent(state)
            elif stage == "TP_WORKING":
                state = _handle_tp(state)
            elif stage == "SL_WORKING":
                state = _handle_sl(state)
            else:
                state = {"stage": "IDLE", "last_completed_ticket_id": state.get("last_completed_ticket_id")}

            _save_state(state)
        except KeyboardInterrupt:
            print("[submitter] stopping")
            break
        except Exception as exc:
            _append_exec("submitter_error", {"error": str(exc)})
            print(f"[submitter] error: {exc}")
        finally:
            time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()