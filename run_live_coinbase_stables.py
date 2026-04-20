from __future__ import annotations

import csv
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from dotenv import find_dotenv, load_dotenv  # type: ignore
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
CLOSED_LOG = OUTDIR / "closed_trades.csv"

POLL_SECS = int(os.getenv("SUBMITTER_POLL_SECS", "5"))
PARENT_TTL_SECS = int(os.getenv("PARENT_TTL_SECS", "300"))
PARENT_PARTIAL_PROMOTE_SECS = int(os.getenv("PARENT_PARTIAL_PROMOTE_SECS", "20"))
MAX_TICKET_AGE_SECS = int(os.getenv("MAX_TICKET_AGE_SECS", "30"))
POSITION_MAX_AGE_SECS = int(os.getenv("POSITION_MAX_AGE_SECS", str(int(os.getenv("HOLD_MINUTES", "180")) * 60)))


def q(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _now() -> int:
    return int(time.time())


def _now_human() -> str:
    return datetime.fromtimestamp(_now(), tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


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
        "ts_human": _now_human(),
        "event": event,
        "details": json.dumps(details, sort_keys=True),
    }
    exists = EXEC_LOG.exists()
    with EXEC_LOG.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def _append_closed(row: Dict[str, str]) -> None:
    exists = CLOSED_LOG.exists()
    header = list(row.keys())
    with CLOSED_LOG.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
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
    product = cb_pub.get_product(product_id)
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
    total_fees = order.get("total_fees") or order.get("fee") or "0"
    return {
        "order_id": str(order.get("order_id") or order_id),
        "client_order_id": str(order.get("client_order_id") or ""),
        "product_id": str(order.get("product_id") or ""),
        "side": str(order.get("side") or "").upper(),
        "status": str(order.get("status") or "").upper(),
        "completion_percentage": q(order.get("completion_percentage") or "0"),
        "filled_size": q(order.get("filled_size") or "0"),
        "total_size": _extract_total_size(order),
        "average_filled_price": q(order.get("average_filled_price") or "0"),
        "total_fees": q(total_fees),
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
    return snapshot["completion_percentage"] >= Decimal("100")


def _opposite_side(side: str) -> str:
    return "SELL" if str(side).upper() == "BUY" else "BUY"


def _rounded_size(product_id: str, size: Decimal) -> Decimal:
    base_inc, _, min_size = _product_specs(product_id)
    normalized = round_size(size, base_inc, mode="down")
    return normalized if normalized >= min_size else Decimal("0")


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
    if side.upper() == "BUY":
        price = round_price(price, price_inc, mode="down" if post_only else "up")
    else:
        price = round_price(price, price_inc, mode="up" if post_only else "down")
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


def _place_market_ioc(product_id: str, side: str, size: Decimal, client_prefix: str) -> Tuple[bool, Dict[str, Any]]:
    base_inc, _, min_size = _product_specs(product_id)
    size = round_size(size, base_inc, mode="down")
    if size < min_size:
        raise ValueError(f"size {size} < min_size {min_size}")
    client_order_id = f"{client_prefix}-{uuid.uuid4().hex[:10]}"
    ok, resp = cb_priv.place_market_ioc_order(
        product_id=product_id,
        side=side,
        size=str(size),
        client_order_id=client_order_id,
    )
    payload = dict(resp)
    payload["client_order_id"] = client_order_id
    payload["normalized_size"] = str(size)
    return ok, payload


def _cancel_if_possible(order_id: str) -> bool:
    if not order_id:
        return False
    try:
        return cb_priv.cancel_order(order_id)
    except Exception:
        return False


def _submit_parent_from_ticket(ticket: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, Any]]:
    product_id = ticket["product_id"]
    side = ticket["side"].upper()
    entry = q(ticket["entry_price"])
    size = q(ticket["size"])
    tp = q(ticket["tp_price"])
    sl = q(ticket["sl_price"])
    post_only = _boolish(ticket.get("post_only", "true"))
    client_tag = ticket.get("client_tag", "stables_mr")

    base_inc, price_inc, min_size = _product_specs(product_id)
    entry = round_price(entry, price_inc, mode="down" if side == "BUY" else "up")
    tp = round_price(tp, price_inc, mode="up" if side == "BUY" else "down")
    sl = round_price(sl, price_inc, mode="down" if side == "BUY" else "up")
    size = round_size(size, base_inc, mode="down")

    if size < min_size:
        raise ValueError(f"size {size} < min_size {min_size}")
    if side == "BUY" and not (tp > entry and sl < entry):
        raise ValueError("BUY ticket has invalid TP/SL relationship")
    if side == "SELL" and not (tp < entry and sl > entry):
        raise ValueError("SELL ticket has invalid TP/SL relationship")

    ok, resp = _place_limit(product_id, side, size, entry, post_only=post_only, client_prefix=client_tag)
    if not ok:
        raise RuntimeError(resp)

    normalized_ticket = dict(ticket)
    normalized_ticket["entry_price"] = resp["normalized_price"]
    normalized_ticket["size"] = resp["normalized_size"]
    normalized_ticket["tp_price"] = str(tp)
    normalized_ticket["sl_price"] = str(sl)
    normalized_ticket["post_only"] = str(post_only)
    return normalized_ticket, resp


def _extract_order_id(payload: Dict[str, Any]) -> str:
    return str(payload.get("order_id") or (payload.get("success_response") or {}).get("order_id") or "")


def _new_parent_state(ticket: Dict[str, str], ticket_id: str, parent_payload: Dict[str, Any]) -> Dict[str, Any]:
    current = _load_state()
    return {
        "stage": "PARENT_WORKING",
        "ticket_id": ticket_id,
        "product_id": ticket["product_id"],
        "entry_side": ticket["side"].upper(),
        "exit_side": _opposite_side(ticket["side"]),
        "entry_price": ticket["entry_price"],
        "tp_price": ticket["tp_price"],
        "sl_price": ticket["sl_price"],
        "planned_size": ticket["size"],
        "parent_order_id": _extract_order_id(parent_payload),
        "parent_submitted_ts": _now(),
        "last_completed_ticket_id": current.get("last_completed_ticket_id", ""),
    }


def _open_position_state(state: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    filled_size = _rounded_size(state["product_id"], snapshot["filled_size"])
    if filled_size <= 0:
        return {"stage": "IDLE", "last_completed_ticket_id": state["ticket_id"]}

    entry_avg = snapshot["average_filled_price"] if snapshot["average_filled_price"] > 0 else q(state["entry_price"])
    return {
        "stage": "POSITION_OPEN",
        "ticket_id": state["ticket_id"],
        "product_id": state["product_id"],
        "entry_side": state["entry_side"],
        "exit_side": state["exit_side"],
        "entry_order_id": state["parent_order_id"],
        "entry_avg_price": str(entry_avg),
        "position_size": str(filled_size),
        "tp_price": state["tp_price"],
        "sl_price": state["sl_price"],
        "tp_order_id": "",
        "tp_working": False,
        "position_open_ts": _now(),
        "entry_fees": str(snapshot.get("total_fees", Decimal("0"))),
        "realized_exit_size": "0",
        "realized_exit_value": "0",
        "realized_exit_fees": "0",
        "last_completed_ticket_id": state.get("last_completed_ticket_id", ""),
    }


def _submit_tp_order(state: Dict[str, Any]) -> Dict[str, Any]:
    size = _remaining_position(state)
    if size <= 0:
        return state

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

    next_state = dict(state)
    next_state["tp_order_id"] = _extract_order_id(resp)
    next_state["tp_working"] = True
    next_state["tp_submitted_ts"] = _now()
    next_state["tp_accounted_filled_size"] = "0"
    next_state["tp_accounted_fees"] = "0"
    return next_state


def _submit_stop_exit(state: Dict[str, Any], remaining: Decimal, reason: str) -> Dict[str, Any]:
    ok, resp = _place_market_ioc(
        product_id=state["product_id"],
        side=state["exit_side"],
        size=remaining,
        client_prefix=reason,
    )
    if not ok:
        raise RuntimeError(resp)

    next_state = dict(state)
    next_state["stage"] = "EXIT_WORKING"
    next_state["exit_reason"] = reason
    next_state["exit_order_id"] = _extract_order_id(resp)
    next_state["exit_submitted_ts"] = _now()
    next_state["exit_accounted_filled_size"] = "0"
    next_state["exit_accounted_fees"] = "0"
    return next_state


def _remaining_position(state: Dict[str, Any]) -> Decimal:
    remaining = q(state.get("position_size") or "0") - q(state.get("realized_exit_size") or "0")
    return remaining if remaining > 0 else Decimal("0")


def _update_realized_from_order(state: Dict[str, Any], snapshot: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    next_state = dict(state)

    accounted_size_key = f"{prefix}_accounted_filled_size"
    accounted_fees_key = f"{prefix}_accounted_fees"

    prev_size = q(next_state.get(accounted_size_key) or "0")
    prev_fees = q(next_state.get(accounted_fees_key) or "0")
    current_size = snapshot["filled_size"]
    current_fees = snapshot["total_fees"]

    delta_size = current_size - prev_size
    delta_fees = current_fees - prev_fees

    if delta_size > 0:
        avg_price = snapshot["average_filled_price"]
        next_state["realized_exit_size"] = str(q(next_state.get("realized_exit_size") or "0") + delta_size)
        next_state["realized_exit_value"] = str(q(next_state.get("realized_exit_value") or "0") + (delta_size * avg_price))

    if delta_fees > 0:
        next_state["realized_exit_fees"] = str(q(next_state.get("realized_exit_fees") or "0") + delta_fees)

    next_state[accounted_size_key] = str(current_size)
    next_state[accounted_fees_key] = str(current_fees)
    return next_state


def _stop_hit(state: Dict[str, Any]) -> bool:
    try:
        bid, ask = cb_pub.get_best_bid_ask(state["product_id"])
    except Exception:
        return False

    sl = q(state["sl_price"])
    if state["entry_side"] == "BUY":
        return q(bid) <= sl
    return q(ask) >= sl


def _position_expired(state: Dict[str, Any]) -> bool:
    opened = int(state.get("position_open_ts") or 0)
    if opened <= 0 or POSITION_MAX_AGE_SECS <= 0:
        return False
    return (_now() - opened) >= POSITION_MAX_AGE_SECS


def _write_closed_trade(state: Dict[str, Any], exit_snapshot: Dict[str, Any], reason: str) -> None:
    entry_size = q(state.get("position_size") or "0")
    entry_avg = q(state.get("entry_avg_price") or "0")
    realized_size = q(state.get("realized_exit_size") or "0")
    realized_value = q(state.get("realized_exit_value") or "0")

    if realized_size > 0:
        exit_avg = realized_value / realized_size
        exit_size = min(entry_size, realized_size)
    else:
        exit_avg = exit_snapshot["average_filled_price"]
        exit_size = entry_size

    gross = (exit_avg - entry_avg) * exit_size if state["entry_side"] == "BUY" else (entry_avg - exit_avg) * exit_size
    fees = q(state.get("entry_fees") or "0") + q(state.get("realized_exit_fees") or "0")
    net = gross - fees

    row = {
        "ts": str(_now()),
        "ts_human": _now_human(),
        "ticket_id": str(state.get("ticket_id") or ""),
        "product_id": str(state.get("product_id") or ""),
        "entry_side": str(state.get("entry_side") or ""),
        "exit_reason": reason,
        "entry_order_id": str(state.get("entry_order_id") or state.get("parent_order_id") or ""),
        "exit_order_id": str(exit_snapshot.get("order_id") or state.get("exit_order_id") or state.get("tp_order_id") or ""),
        "filled_size": str(exit_size),
        "entry_avg_price": str(entry_avg),
        "exit_avg_price": str(exit_avg),
        "gross_pnl": str(gross),
        "fees": str(fees),
        "net_pnl": str(net),
    }
    _append_closed(row)


def _idle_state(last_completed_ticket_id: str = "") -> Dict[str, Any]:
    return {"stage": "IDLE", "last_completed_ticket_id": last_completed_ticket_id}


def _handle_idle(state: Dict[str, Any]) -> Dict[str, Any]:
    ticket = _load_ticket()
    if not ticket:
        return state

    ticket_id = ticket.get("ticket_id") or _ticket_hash(ticket)
    if state.get("last_completed_ticket_id") == ticket_id:
        return state

    expire_ts = int(ticket.get("expire_ts") or 0)
    ticket_ts = int(ticket.get("ts") or 0)
    now = _now()

    if (expire_ts and now > expire_ts) or (ticket_ts and now - ticket_ts > MAX_TICKET_AGE_SECS):
        _append_exec("stale_ticket_discarded", {"ticket_id": ticket_id, "ticket": ticket})
        _clear_ticket_file()
        return _idle_state(state.get("last_completed_ticket_id", ""))

    normalized_ticket, parent_resp = _submit_parent_from_ticket(ticket)
    new_state = _new_parent_state(normalized_ticket, ticket_id, parent_resp)
    _save_state(new_state)
    _clear_ticket_file()
    _append_exec("parent_submitted", new_state)
    return new_state


def _handle_parent(state: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _order_snapshot(state["parent_order_id"])
    age = _now() - int(state.get("parent_submitted_ts", _now()))

    if _is_terminal_rejected(snapshot):
        _append_exec("parent_rejected", snapshot)
        return _idle_state(state["ticket_id"])

    if _is_filled(snapshot):
        next_state = _open_position_state(state, snapshot)
        _append_exec("position_opened", next_state)
        return next_state

    if snapshot["filled_size"] > 0 and age >= PARENT_PARTIAL_PROMOTE_SECS:
        cancelled = _cancel_if_possible(state["parent_order_id"])
        next_state = _open_position_state(state, snapshot)
        _append_exec("position_opened_from_partial", {"cancelled": cancelled, **next_state})
        return next_state

    if age >= PARENT_TTL_SECS:
        cancelled = _cancel_if_possible(state["parent_order_id"])
        _append_exec("parent_cancel_requested", {"cancelled": cancelled, **snapshot})
        if snapshot["filled_size"] > 0:
            next_state = _open_position_state(state, snapshot)
            _append_exec("position_opened_after_ttl", next_state)
            return next_state
        return _idle_state(state["ticket_id"])

    return state


def _handle_position_open(state: Dict[str, Any]) -> Dict[str, Any]:
    if _position_expired(state):
        if state.get("tp_working") and state.get("tp_order_id"):
            _cancel_if_possible(state["tp_order_id"])
        remaining = _remaining_position(state)
        if remaining > 0:
            next_state = _submit_stop_exit(state, remaining, "max_hold")
            _append_exec("max_hold_exit_submitted", next_state)
            return next_state
        return _idle_state(state["ticket_id"])

    if not state.get("tp_working"):
        try:
            next_state = _submit_tp_order(state)
            _append_exec("tp_submitted", next_state)
            return next_state
        except Exception as exc:
            _append_exec("tp_submit_failed", {"error": str(exc), **state})
            if _stop_hit(state):
                remaining = _remaining_position(state)
                if remaining > 0:
                    return _submit_stop_exit(state, remaining, "sl")
            return state

    tp_snapshot = _order_snapshot(state["tp_order_id"])
    state = _update_realized_from_order(state, tp_snapshot, "tp")

    if _remaining_position(state) <= 0:
        _append_exec("tp_filled", tp_snapshot)
        _write_closed_trade(state, tp_snapshot, "tp")
        return _idle_state(state["ticket_id"])

    if _is_terminal_rejected(tp_snapshot) or _is_terminal_cancelled(tp_snapshot):
        next_state = dict(state)
        next_state["tp_working"] = False
        next_state["tp_order_id"] = ""
        _append_exec("tp_unavailable", tp_snapshot)
        if _stop_hit(next_state):
            remaining = _remaining_position(next_state)
            if remaining > 0:
                return _submit_stop_exit(next_state, remaining, "sl")
        return next_state

    if _stop_hit(state):
        _cancel_if_possible(state["tp_order_id"])
        remaining = _remaining_position(state)
        if remaining <= 0:
            _write_closed_trade(state, tp_snapshot, "tp")
            return _idle_state(state["ticket_id"])
        next_state = _submit_stop_exit(state, remaining, "sl")
        _append_exec("stop_submitted", next_state)
        return next_state

    return state


def _handle_exit_working(state: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _order_snapshot(state["exit_order_id"])
    state = _update_realized_from_order(state, snapshot, "exit")
    remaining = _remaining_position(state)

    if remaining <= 0:
        _append_exec("exit_filled", snapshot)
        _write_closed_trade(state, snapshot, str(state.get("exit_reason") or "exit"))
        return _idle_state(state["ticket_id"])

    if _is_terminal_rejected(snapshot) or _is_terminal_cancelled(snapshot) or _is_filled(snapshot):
        next_state = _submit_stop_exit(state, remaining, str(state.get("exit_reason") or "exit"))
        _append_exec("exit_retried", next_state)
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
            elif stage == "POSITION_OPEN":
                state = _handle_position_open(state)
            elif stage == "EXIT_WORKING":
                state = _handle_exit_working(state)
            else:
                state = _idle_state(state.get("last_completed_ticket_id", ""))

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