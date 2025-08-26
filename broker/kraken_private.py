from __future__ import annotations
import json, time, hmac, hashlib, base64, threading, ssl
from decimal import Decimal, getcontext
from typing import Optional, Dict, Any, List

import requests
import websocket  # pip install websocket-client
import certifi    # pip install certifi

getcontext().prec = 28

KRAKEN_REST = "https://api.kraken.com"
WS_V2_URL = "wss://ws-auth.kraken.com/v2"


class KrakenAuth:
    def __init__(self, api_key: str, api_secret_b64: str):
        self.api_key = (api_key or "").strip()
        self.api_secret = base64.b64decode(api_secret_b64) if api_secret_b64 else b""

    def _sign(self, path: str, data: Dict[str, str]) -> Dict[str, str]:
        nonce = str(int(time.time() * 1000))
        data = {"nonce": nonce, **data}
        postdata = "&".join([f"{k}={v}" for k, v in data.items()])

        sha256 = hashlib.sha256((nonce + postdata).encode()).digest()
        mac = hmac.new(self.api_secret, path.encode() + sha256, hashlib.sha512)

        return {
            "API-Key": self.api_key,
            "API-Sign": base64.b64encode(mac.digest()).decode(),
        }, data

    def get_ws_token(self) -> str:
        headers, data = self._sign("/0/private/GetWebSocketsToken", {})
        r = requests.post(
            KRAKEN_REST + "/0/private/GetWebSocketsToken",
            data=data,
            headers=headers,
            timeout=10,
            verify=certifi.where(),  # ensure CA bundle
        )
        r.raise_for_status()
        j = r.json()
        if j.get("error"):
            raise RuntimeError(j["error"])
        return j["result"]["token"]

    def rest(self, endpoint: str, data: Dict[str, str]) -> dict:
        headers, signed = self._sign(f"/0/private/{endpoint}", data)
        r = requests.post(
            f"{KRAKEN_REST}/0/private/{endpoint}",
            data=signed,
            headers=headers,
            timeout=15,
            verify=certifi.where(),  # ensure CA bundle
        )
        r.raise_for_status()
        j = r.json()
        if j.get("error"):
            raise RuntimeError(j["error"])
        return j["result"]


class WsV2Trader:
    def __init__(self, auth: KrakenAuth):
        self.auth = auth
        self.ws = None
        self.token = None
        self.lock = threading.Lock()

    # ---- connection helpers ----
    def _connect_with_ssl_context(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(certifi.where())
        return websocket.create_connection(
            WS_V2_URL,
            timeout=15,
            sslopt={"ssl_context": ctx},
        )

    def _connect_with_sslopt_legacy(self):
        return websocket.create_connection(
            WS_V2_URL,
            timeout=15,
            sslopt={
                "cert_reqs": ssl.CERT_REQUIRED,
                "ca_certs": certifi.where(),
                "check_hostname": True,
            },
        )

    def connect(self):
        # prove REST creds + WS permission, get short-lived token
        self.token = self.auth.get_ws_token()
        try:
            self.ws = self._connect_with_ssl_context()
        except ssl.SSLError:
            self.ws = self._connect_with_sslopt_legacy()
        # make recv non-blocking for long periods; we’ll manage our own timeouts
        self.ws.settimeout(5.0)

    def close(self):
        if self.ws:
            try:
                self.ws.close()
            finally:
                self.ws = None

    # ---- generic RPC waiter ----
    def _recv_until(self, *, want_req_id: int, want_method: str, timeout_sec: float = 12.0) -> dict:
        """
        Read frames until we see the response matching our req_id (or method),
        skipping heartbeats and system status updates that may arrive first.
        """
        assert self.ws is not None
        deadline = time.time() + timeout_sec
        last_raw = None
        while time.time() < deadline:
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:
                raise RuntimeError(f"WS recv failed: {e}") from e

            last_raw = raw
            try:
                msg = json.loads(raw)
            except Exception:
                # ignore non-JSON frames
                continue

            # Possible shapes:
            # - {"channel":"status","type":"update",...} -> ignore
            # - {"type":"heartbeat"} -> ignore
            # - {"method":"add_order","req_id":123,"success":true,...} -> desired
            # - {"method":"add_order","req_id":123,"success":false,"error":...}
            if isinstance(msg, dict):
                if msg.get("type") == "heartbeat":
                    continue
                if msg.get("channel") == "status":
                    continue
                if msg.get("method") == want_method:
                    # if req_id present, must match; if not present, accept method match
                    if "req_id" in msg and msg["req_id"] != want_req_id:
                        continue
                    return msg
                # Some servers include only success+req_id without method; handle that too
                if "success" in msg and ("req_id" in msg and msg["req_id"] == want_req_id):
                    return msg

        raise RuntimeError(f"Timed out waiting for {want_method} reply (req_id={want_req_id}). Last frame={last_raw!r}")

    # ---- trading RPCs ----
    def add_order_limit_with_stop(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        limit_price: Decimal,
        stop_trigger: Decimal,
        stop_limit: Optional[Decimal],
        post_only: bool,
        userref: int,
        cl_ord_id: Optional[str] = None,
    ) -> dict:
        """
        Preferred WS v2 path (requires WS token). Attaches ONE conditional child (STOP).
        We now wait for the proper reply to our req_id and ignore status/heartbeat frames.
        """
        assert self.ws is not None, "WS not connected"

        req_id = int(time.time() * 1000) % 2_147_483_647
        payload = {
            "method": "add_order",
            "params": {
                "order_type": "limit",
                "side": side,
                "order_qty": float(qty),
                "symbol": symbol,                   # e.g., "FLOKI/USD"
                "limit_price": float(limit_price),
                "post_only": bool(post_only),
                "order_userref": int(userref),
                "token": self.token,
                "conditional": {
                    "order_type": "stop-loss-limit" if stop_limit is not None else "stop-loss",
                    "trigger_price": float(stop_trigger),
                },
            },
            "req_id": req_id,
        }
        if stop_limit is not None:
            payload["params"]["conditional"]["limit_price"] = float(stop_limit)
        if cl_ord_id:
            payload["params"]["cl_ord_id"] = cl_ord_id

        with self.lock:
            self.ws.send(json.dumps(payload))
            resp = self._recv_until(want_req_id=req_id, want_method="add_order", timeout_sec=15.0)

        # Kraken WS v2 returns {"success": true/false, "error": "..."} on method reply
        if not bool(resp.get("success", False)):
            raise RuntimeError(f"Order rejected: {resp}")
        return resp

    # ---------- REST helpers ----------
    def cancel_order(self, txid: str) -> None:
        self.auth.rest("CancelOrder", {"txid": txid})

    def list_open_orders(self) -> dict:
        return self.auth.rest("OpenOrders", {})

    def cancel_oto_children_for_userref(self, userref: int) -> List[str]:
        """
        Cancel all stop/trigger children associated with a userref (synthetic OCO).
        """
        canceled: List[str] = []
        oo = self.list_open_orders().get("open", {})
        for txid, od in oo.items():
            try:
                if int(od.get("userref", -1)) != int(userref):
                    continue
            except Exception:
                continue
            otype = (od.get("descr", {}) or {}).get("ordertype", "")
            if otype.startswith("stop-loss"):
                try:
                    self.cancel_order(txid)
                    canceled.append(txid)
                except Exception:
                    pass
        return canceled


# ---------- Exit helpers (return txid) ----------
def _extract_txid(result: dict) -> str:
    txids = result.get("txid") or []
    if isinstance(txids, list) and txids:
        return txids[0]
    return ""


def place_market_exit(auth: KrakenAuth, symbol: str, side: str, qty: Decimal) -> str:
    data = {
        "pair": symbol.replace("/", ""),
        "type": side,
        "ordertype": "market",
        "volume": str(qty),
    }
    res = auth.rest("AddOrder", data)
    return _extract_txid(res)


def place_limit_exit(auth: KrakenAuth, symbol: str, side: str, qty: Decimal, px: Decimal) -> str:
    data = {
        "pair": symbol.replace("/", ""),
        "type": side,
        "ordertype": "limit",
        "price": str(px),
        "volume": str(qty),
        "oflags": "post",
    }
    res = auth.rest("AddOrder", data)
    return _extract_txid(res)


# ---------- REST entry with attached STOP (no WS token needed) ----------
def place_entry_with_stop_rest(
    auth: KrakenAuth,
    symbol: str,         # pass REST pair like "FLOKIUSD" (caller converts)
    side: str,           # "buy" | "sell"
    qty: Decimal,
    limit_price: Decimal,
    stop_trigger: Decimal,
    userref: int,
    post_only: bool = True,
    cl_ord_id: Optional[str] = None,  # ignored (REST cannot mix with userref)
) -> dict:
    """
    Kraken REST AddOrder supports ONE 'close' child (OTO). REST **forbids** sending both
    userref and cl_ord_id. We keep userref and omit cl_ord_id on REST.
    """
    pair = symbol.replace("/", "")
    data = {
        "pair": pair,
        "type": side,
        "ordertype": "limit",
        "price": str(limit_price),
        "volume": str(qty),
        "userref": str(userref),  # keep this for reconciliation/cancel
        # Attach a stop child:
        "close[ordertype]": "stop-loss",
        "close[price]": str(stop_trigger),
    }
    if post_only:
        data["oflags"] = "post"
    # DO NOT set cl_ord_id when userref is present (REST will reject)
    res = auth.rest("AddOrder", data)
    return res
