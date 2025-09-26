# run_controller_stables.py
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Any, Optional

STATE_PATH = "output_stables/state.jsonl"
TICKET_PATH = "output_stables/trade_tickets_latest.csv"

# -------- Broker interfaces (ADAPT ME to your wrappers) -------- #
# Map these to your broker.coinbase_* modules once and you're done.

class Broker:
    @staticmethod
    def place_bracket_limit_post_only(product_id: str, side: str, price: str, size: str, tp: str, sl: str, client_oid: str) -> Dict[str, Any]:
        """
        Return {"parent_id": "...", "tp_id": "...", "sl_id": "..."}
        Raise NotImplementedError if exchange doesn't support bracket post_only.
        """
        try:
            from broker import coinbase_private as priv
            return priv.place_bracket_limit_post_only(product_id, side, price, size, tp, sl, client_oid)
        except AttributeError:
            raise NotImplementedError("bracket_post_only_not_supported")

    @staticmethod
    def place_limit_post_only(product_id: str, side: str, price: str, size: str, client_oid: str) -> Dict[str, Any]:
        from broker import coinbase_private as priv
        return priv.place_limit_post_only(product_id, side, price, size, client_oid)

    @staticmethod
    def cancel_order(order_id: str) -> None:
        from broker import coinbase_private as priv
        priv.cancel_order(order_id)

    @staticmethod
    def get_order_status(order_id: str) -> Dict[str, Any]:
        from broker import coinbase_private as priv
        return priv.get_order_status(order_id)

    @staticmethod
    def get_open_orders(product_id: Optional[str] = None) -> list[Dict[str, Any]]:
        from broker import coinbase_private as priv
        return priv.get_open_orders(product_id=product_id) if hasattr(priv, "get_open_orders") else []

    @staticmethod
    def place_taker_stop_or_flatten(product_id: str, side: str, stop_price: str, size: str, client_oid: str) -> Dict[str, Any]:
        """
        Submit a taker protective order once parent is filled (or flatten at market if needed).
        """
        from broker import coinbase_private as priv
        if hasattr(priv, "place_stop_market"):
            # side for the stop is opposite the position direction
            stop_side = "SELL" if side == "BUY" else "BUY"
            return priv.place_stop_market(product_id, stop_side, stop_price, size, client_oid)
        elif hasattr(priv, "flatten_market"):
            return priv.flatten_market(product_id, side, size, client_oid)  # ADAPT: side may be ignored
        else:
            # Last resort: market out using opposite side
            opp = "SELL" if side == "BUY" else "BUY"
            if hasattr(priv, "place_market"):
                return priv.place_market(product_id, opp, size, client_oid)
            raise NotImplementedError("No taker stop/flatten method available.")

class MarketData:
    @staticmethod
    def best_bid_ask(product_id: str) -> Dict[str, Decimal]:
        from broker import coinbase_public as pub
        q = pub.get_best_bid_ask(product_id)
        return {"bid": Decimal(q["bid"]), "ask": Decimal(q["ask"])}

# ---------------- Risk / health checks ------------------------- #
def trading_allowed(product_id: str) -> bool:
    try:
        from risk.healthchecks import trading_allowed as ra
        return ra(product_id)
    except Exception:
        return True

# -------------------- Utilities -------------------------------- #
def _append_state(entry: Dict[str, Any]):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")

def _read_ticket(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        r = list(csv.DictReader(f))
        return r[0] if r else None

def _now() -> int:
    return int(time.time())

# -------------------- Controller -------------------------------- #
@dataclass
class ControllerConfig:
    reprice_every_secs: int = 45
    max_reprices: int = 3
    watch_sl_interval_secs: int = 2
    max_hold_seconds: int = 180 * 60  # align with ticket hold_minutes
    position_cap_per_product: int = 1  # conservative: one active parent per product

def _ready_to_place(product_id: str, cfg: ControllerConfig) -> bool:
    # Ensure we don't pile up multiple parents
    opens = Broker.get_open_orders(product_id)
    parents = [o for o in opens if o.get("product_id") == product_id and o.get("status") in ("OPEN","PENDING")]
    return len(parents) < cfg.position_cap_per_product

def _place_with_reprice_and_optional_bracket(ticket: Dict[str, Any], cfg: ControllerConfig) -> Dict[str, Any]:
    """
    Try bracket+post-only first; if unsupported, place post-only parent and manage reprice/stop ourselves.
    Returns a dict with placement details.
    """
    product_id = ticket["product_id"]
    side = ticket["side"]
    price = Decimal(ticket["entry_price"])
    size = Decimal(ticket["base_size"])
    tp = Decimal(ticket["tp_price"])
    sl = Decimal(ticket["sl_price"])
    client_oid_base = f"stables_{product_id}_{_now()}"

    # Try bracket + post-only
    bracket_supported = True
    try:
        resp = Broker.place_bracket_limit_post_only(
            product_id, side, str(price), str(size), str(tp), str(sl), client_oid_base+"b"
        )
        placement = {
            "mode": "BRACKET_POST_ONLY",
            "parent_id": resp.get("parent_id"),
            "tp_id": resp.get("tp_id"),
            "sl_id": resp.get("sl_id"),
            "bracket_supported": True,
        }
        _append_state({"ts": _now(), "event": "placed_bracket_postonly", "details": placement})
        return placement
    except NotImplementedError:
        bracket_supported = False

    # Fallback: post-only parent, reprice if stale
    placement = {"mode": "LIMIT_ONLY", "bracket_supported": False, "parent_id": None}
    attempts = 0

    while attempts <= cfg.max_reprices:
        client_oid = client_oid_base + f"r{attempts}"
        resp = Broker.place_limit_post_only(product_id, side, str(price), str(size), client_oid)
        parent_id = resp.get("order_id") or resp.get("id")
        placement["parent_id"] = parent_id
        _append_state({"ts": _now(), "event": "placed_limit_postonly", "details": {"parent_id": parent_id, "price": str(price), "attempt": attempts}})

        # Wait and check if we are still top-of-book; if not, reprice
        t0 = time.time()
        while time.time() - t0 < cfg.reprice_every_secs:
            status = Broker.get_order_status(parent_id)
            if status.get("status") in ("FILLED","FILLED_PARTIAL","DONE","MATCHED","CLOSED"):
                placement["filled"] = True
                _append_state({"ts": _now(), "event": "parent_filled", "details": {"parent_id": parent_id}})
                return placement
            # still open; small sleep to avoid hammering
            time.sleep(1.0)

        # Reprice if not filled
        attempts += 1
        if attempts > cfg.max_reprices:
            _append_state({"ts": _now(), "event": "max_reprices_reached", "details": {"parent_id": parent_id}})
            break

        # Cancel and move to new best
        try:
            Broker.cancel_order(parent_id)
            q = MarketData.best_bid_ask(product_id)
            price = q["bid"] if side == "BUY" else q["ask"]
            _append_state({"ts": _now(), "event": "repricing", "details": {"new_price": str(price), "attempt": attempts}})
        except Exception as e:
            _append_state({"ts": _now(), "event": "repricing_failed", "details": {"error": str(e)}})
            break

    return placement

def _watch_stop_if_needed(product_id: str, side: str, sl_price: Decimal, size: Decimal, placement: Dict[str, Any], cfg: ControllerConfig):
    """
    Only for LIMIT_ONLY mode where we didn't get an on-exchange stop.
    If parent gets filled, arm a taker stop or flatten when price breaches SL.
    """
    if placement.get("mode") != "LIMIT_ONLY":
        return

    parent_id = placement.get("parent_id")
    if not parent_id:
        return

    start = time.time()
    filled = False
    while time.time() - start < cfg.max_hold_seconds:
        st = Broker.get_order_status(parent_id)
        status = st.get("status", "")
        if status in ("FILLED","FILLED_PARTIAL","DONE","MATCHED","CLOSED") and not filled:
            filled = True
            _append_state({"ts": _now(), "event": "parent_filled_limit_only", "details": {"parent_id": parent_id}})
        # Price-based stop after fill
        if filled:
            q = MarketData.best_bid_ask(product_id)
            px = q["bid"] if side == "BUY" else q["ask"]
            breach = (px <= sl_price) if side == "BUY" else (px >= sl_price)
            if breach:
                try:
                    Broker.place_taker_stop_or_flatten(
                        product_id, side, str(sl_price), str(size), f"stables_stop_{_now()}"
                    )
                    _append_state({"ts": _now(), "event": "stop_executed", "details": {"parent_id": parent_id, "sl_price": str(sl_price)}})
                except Exception as e:
                    _append_state({"ts": _now(), "event": "stop_failed", "details": {"error": str(e)}})
                break
        time.sleep(cfg.watch_sl_interval_secs)

def main():
    cfg = ControllerConfig()

    t = _read_ticket(TICKET_PATH)
    if not t:
        print("[controller] No ticket file.")
        return
    if t.get("side") in (None, "", "NONE"):
        print(f"[controller] No signal: {t.get('reason','')}")
        return

    product_id = t["product_id"]
    if not trading_allowed(product_id):
        print("[controller] Trading paused by risk controls.")
        return

    if not _ready_to_place(product_id, cfg):
        print("[controller] Position cap reached; skipping.")
        return

    placement = _place_with_reprice_and_optional_bracket(t, cfg)

    # If no bracket, we must supervise stop ourselves
    if placement.get("mode") == "LIMIT_ONLY":
        _watch_stop_if_needed(
            product_id=product_id,
            side=t["side"],
            sl_price=Decimal(t["sl_price"]),
            size=Decimal(t["base_size"]),
            placement=placement,
            cfg=cfg,
        )

if __name__ == "__main__":
    main()
