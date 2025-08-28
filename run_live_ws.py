# run_live_ws.py — Entry + attached stop-loss-limit + independent TP + WS v2 OCO
from __future__ import annotations
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from typing import Optional, Dict

import websocket  # websocket-client
import ssl, certifi  # TLS CA bundle

import config as C
from broker.kraken_private import (
    KrakenAuth,
    place_entry_with_stop_rest,
    place_limit_order,
    cancel_order,
    get_balance,  # read balances for free base
)
from kraken_public import (
    ticker_info,
    pair_decimals,
    ordermin_for_pair,
    base_asset_for_pair,
)
from signal_engine import screen_and_build_candidates, make_maker_tickets

getcontext().prec = 28
WS_URL = "wss://ws-auth.kraken.com/v2"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")


def _round(x: float, dp: int | None) -> float:
    if dp is None:
        return x
    return float(Decimal(str(x)).quantize(Decimal("1." + ("0" * int(dp)))))


@dataclass
class State:
    pair: str
    side: str
    qty_total: float
    entry_px: float
    tp_px: float
    stop_trig: float
    stop_limit: float
    entry_id: Optional[str] = None
    tp_id: Optional[str] = None
    stop_child_id: Optional[str] = None
    entry_filled: float = 0.0
    tp_open_qty: float = 0.0
    done: bool = False
    userref: int = 0


class OCOController:
    def __init__(self, auth: KrakenAuth, st: State):
        self.auth = auth
        self.ws_token = auth.get_ws_token()
        self.state = st
        self.ws: Optional[websocket.WebSocketApp] = None
        self._lock = threading.Lock()
        self._base_asset = base_asset_for_pair(st.pair)
        self._ordmin = ordermin_for_pair(st.pair)
        self._first_fill_ts: Optional[float] = None  # for TP grace enforcement

    # ---------- helpers ----------
    def _free_base_qty(self) -> float:
        """How much base asset is free (not locked by other orders)."""
        try:
            bals = get_balance(self.auth)  # {'ZUSD': '123.45', 'XETH': '0.12', 'ETHFI': '9.5', ...}
            raw = bals.get(self._base_asset, "0")
            return float(raw)
        except Exception:
            return 0.0

    def _enforce_tp_grace_if_needed(self):
        """If we had a first fill but still have no TP after the grace window, cancel the entry."""
        if not C.TP_GRACE_MS:
            return
        if self._first_fill_ts is None:
            return
        s = self.state
        if s.tp_id:
            return
        if (time.time() - self._first_fill_ts) * 1000 >= C.TP_GRACE_MS:
            try:
                # Cancel entry; canceling parent drops the conditional close child as well.
                cancel_order(self.auth, s.entry_id)
                print(f"[{_now()}] TP grace exceeded ({C.TP_GRACE_MS} ms) → canceled ENTRY {s.entry_id}")
            except Exception as e:
                print(f"[{_now()}] Failed to cancel ENTRY after TP grace: {e}")
            s.done = True

    # ---------- placement ----------
    def place_entry_with_attached_stop(self):
        s = self.state
        post_only = "post" in (C.OFLAGS_ENTRY or "").lower()
        s.entry_id = place_entry_with_stop_rest(
            self.auth, s.pair, s.side, s.entry_px, s.qty_total, s.stop_trig, s.stop_limit, userref=s.userref, post_only=post_only
        )
        print(f"[{_now()}] Entry placed: {s.entry_id}")

    def try_place_tp_now(self) -> bool:
        s = self.state
        try:
            if C.TP_EXIT_MODE == "limit":
                s.tp_id = place_limit_order(
                    self.auth,
                    s.pair,
                    "sell" if s.side == "buy" else "buy",
                    s.tp_px,
                    s.qty_total,
                    post_only=True,
                    reduce_only=True,  # rejected on Spot; harmless
                )
                self.state.tp_open_qty = s.qty_total
                print(f"[{_now()}] TP placed: {s.tp_id} (reduce_only=True)")
                return True
            return False
        except Exception as e:
            print(f"[{_now()}] TP immediate placement deferred: {e}")
            return False

    def place_or_resize_tp_to(self, desired_qty: float) -> bool:
        """Place/amend TP using ONLY free base and honoring ordermin. Returns True if a TP is live."""
        with self._lock:
            s = self.state
            if C.TP_EXIT_MODE != "limit":
                return False

            free_base = self._free_base_qty()
            qty = float(max(0.0, min(desired_qty, free_base)))

            if self._first_fill_ts is None and qty > 0:
                self._first_fill_ts = time.time()

            if qty <= 0:
                print(f"[{_now()}] TP deferred: free base={free_base:.8f}; waiting for fills to credit.")
                return False

            if self._ordmin and qty < self._ordmin:
                print(f"[{_now()}] TP deferred: free base {qty:.8f} < ordermin {self._ordmin}.")
                return False

            if not s.tp_id:
                try:
                    s.tp_id = place_limit_order(
                        self.auth,
                        s.pair,
                        "sell" if s.side == "buy" else "buy",
                        s.tp_px,
                        qty,
                        post_only=True,
                        reduce_only=False,
                    )
                    s.tp_open_qty = qty
                    print(f"[{_now()}] TP placed after fill: {s.tp_id} size={qty}")
                    return True
                except Exception as e:
                    print(f"[{_now()}] Could not place TP yet (will retry): {e}")
                    return False

            try:
                msg = {"method": "amend_order", "params": {"order_id": s.tp_id, "order_qty": qty, "token": self.ws_token}}
                self.ws.send(json.dumps(msg))
                s.tp_open_qty = qty
                print(f"[{_now()}] TP amend requested -> qty={qty}")
                return True
            except Exception as e:
                print(f"[{_now()}] TP amend failed (will retry): {e}")
                return False

    # ---------- WS lifecycle ----------
    def run(self):
        # Place entry + stop; try to place TP immediately
        self.place_entry_with_attached_stop()
        self.try_place_tp_now()

        # Start WS v2 executions
        def on_open(ws):
            sub = {"method": "subscribe", "params": {"channel": "executions", "token": self.ws_token, "snap_orders": True}}
            ws.send(json.dumps(sub))
            print(f"[{_now()}] WS connected; subscribed to executions.")

        def on_message(ws, message: str):
            try:
                payload = json.loads(message)
            except Exception:
                return
            if isinstance(payload, dict) and payload.get("channel") == "executions":
                for er in payload.get("data", []):
                    self._handle_exec(er)

        def on_error(ws, err):
            print(f"[{_now()}] WS error: {err}")

        def on_close(ws, *args):
            print(f"[{_now()}] WS closed.")

        self.ws = websocket.WebSocketApp(
            WS_URL, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close
        )
        # portable TLS verification using certifi bundle
        t = threading.Thread(
            target=self.ws.run_forever,
            kwargs={
                "ping_interval": 20,
                "ping_timeout": 10,
                "sslopt": {"cert_reqs": ssl.CERT_REQUIRED, "ca_certs": certifi.where()},
            },
            daemon=True,
        )
        t.start()

        # Cooperative loop: also enforces TP grace even if no further execs arrive
        while not self.state.done:
            self._enforce_tp_grace_if_needed()
            time.sleep(0.25)

        try:
            self.ws.close()
        except Exception:
            pass

    # ---------- Exec handler ----------
    def _handle_exec(self, er: Dict):
        s = self.state

        # Learn stop child id when Kraken creates it
        if er.get("ord_ref_id") == s.entry_id and er.get("order_type") in ("stop-loss", "stop-loss-limit"):
            if not s.stop_child_id and er.get("order_id"):
                s.stop_child_id = er["order_id"]
                print(f"[{_now()}] Stop child created: {s.stop_child_id}")

        # Entry updates
        if er.get("order_id") == s.entry_id:
            st = er.get("order_status")

            # Show live progress
            if "cum_qty" in er:
                s.entry_filled = float(er["cum_qty"])
                fb = self._free_base_qty()
                print(f"[{_now()}] Exec update: cum_qty={s.entry_filled:.8f}, free_base={fb:.8f}, ordmin={self._ordmin}")

                tp_ok = self.place_or_resize_tp_to(s.entry_filled)
                if not tp_ok:
                    self._enforce_tp_grace_if_needed()

            if st in ("filled", "canceled", "expired"):
                if s.tp_id:
                    try:
                        cancel_order(self.auth, s.tp_id)
                        print(f"[{_now()}] Entry closed -> canceled TP {s.tp_id}")
                    except Exception as e:
                        print(f"[{_now()}] Cancel TP failed after entry close: {e}")
                s.done = True
                return

        # TP filled
        if s.tp_id and er.get("order_id") == s.tp_id and er.get("order_status") == "filled":
            try:
                if s.stop_child_id:
                    cancel_order(self.auth, s.stop_child_id)
                    print(f"[{_now()}] TP filled -> canceled STOP child {s.stop_child_id}")
                else:
                    cancel_order(self.auth, s.entry_id)
                    print(f"[{_now()}] TP filled -> canceled ENTRY to drop STOP")
            except Exception as e:
                print(f"[{_now()}] Cancel STOP after TP failed: {e}")
            s.done = True
            return

        # STOP filled
        if s.stop_child_id and er.get("order_id") == s.stop_child_id and er.get("order_status") == "filled":
            if s.tp_id:
                try:
                    cancel_order(self.auth, s.tp_id)
                    print(f"[{_now()}] STOP filled -> canceled TP {s.tp_id}")
                except Exception as e:
                    print(f"[{_now()}] Cancel TP after STOP failed: {e}")
            s.done = True
            return


def main():
    print(f"[{_now()}] DRY_RUN={C.DRY_RUN}")
    if C.DRY_RUN:
        print("Dry-run enabled. Set live.dry_run: false (or DRY_RUN=0) to trade.")
        return

    # screen → top ticket
    cands = screen_and_build_candidates()
    tickets = make_maker_tickets(cands)
    if tickets.empty:
        print("No candidates; exiting.")
        return

    t = tickets.iloc[0]
    pair = str(t["kraken_pair"])
    side = str(t["side"])
    entry = float(t["entry_price"])
    qty = float(t["qty"])
    tp = float(t["take_profit"])
    stop_trig = float(t["stop"])

    # rounding: min(pair_decimals, configured dp) if provided
    pair_dp = pair_decimals(pair)
    price_dp = pair_dp if C.PRICE_ROUND_DP is None else min(pair_dp, int(C.PRICE_ROUND_DP))
    qty_dp = C.QTY_ROUND_DP if C.QTY_ROUND_DP is not None else 8

    # compute stop limit beyond trigger
    if side == "buy":
        stop_limit = stop_trig * (1.0 - (C.STOP_LIMIT_OFFSET_BPS / 1e4))
    else:
        stop_limit = stop_trig * (1.0 + (C.STOP_LIMIT_OFFSET_BPS / 1e4))

    entry = _round(entry, price_dp)
    tp = _round(tp, price_dp)
    stop_trig = _round(stop_trig, price_dp)
    stop_limit = _round(stop_limit, price_dp)
    qty = _round(qty, qty_dp)

    print(f"[{_now()}] Candidate: {pair} {side} qty={qty} entry={entry} tp={tp} stop_trig={stop_trig} stop_lim={stop_limit}")

    st = State(
        pair=pair,
        side=side,
        qty_total=qty,
        entry_px=entry,
        tp_px=tp,
        stop_trig=stop_trig,
        stop_limit=stop_limit,
        userref=int(time.time()),
    )
    OCOController(KrakenAuth(), st).run()
    print(f"[{_now()}] Done.")


if __name__ == "__main__":
    main()
