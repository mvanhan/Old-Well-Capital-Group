# broker/kraken_private.py — REST helpers for entries/exits + WS token + balance
from __future__ import annotations
import base64
import hashlib
import hmac
import time
import urllib.parse as up
from typing import Dict, Any, Optional

import requests

from env_utils import get_kraken_credentials

KRAKEN_REST = "https://api.kraken.com"


class KrakenAuth:
    def __init__(self):
        self.api_key, self.secret_b64 = get_kraken_credentials()

    def _sign(self, path: str, data: Dict[str, Any]) -> str:
        postdata = up.urlencode(data)
        hashed = hashlib.sha256((str(data["nonce"]) + postdata).encode()).digest()
        mac = hmac.new(base64.b64decode(self.secret_b64), path.encode() + hashed, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def rest(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        path = f"/0/private/{endpoint}"
        url = f"{KRAKEN_REST}{path}"
        payload = dict(data)
        payload.setdefault("nonce", int(time.time() * 1000))
        headers = {
            "API-Key": self.api_key,
            "API-Sign": self._sign(path, payload),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "owcg/1.0",
        }
        r = requests.post(url, data=payload, headers=headers, timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get("error"):
            raise RuntimeError(j["error"])
        return j["result"]

    def get_ws_token(self) -> str:
        res = self.rest("GetWebSocketsToken", {})
        token = res.get("token") or res.get("wsToken") or ""
        if not token:
            for v in res.values():
                if isinstance(v, str) and v:
                    token = v
                    break
        if not token:
            raise RuntimeError(f"Could not obtain WS token from response: {res}")
        return token


def _extract_txid(res: Dict[str, Any]) -> Optional[str]:
    if isinstance(res, dict):
        if "txid" in res and isinstance(res["txid"], list) and res["txid"]:
            return res["txid"][0]
        for v in res.values():
            if isinstance(v, str) and v:
                return v
            if isinstance(v, list) and v and isinstance(v[0], str):
                return v[0]
    return None


# --------- High-level helpers ---------
def place_entry_with_stop_rest(
    auth: KrakenAuth,
    pair: str,
    side: str,
    limit_price: float,
    qty: float,
    stop_trigger: float,
    stop_limit: float,
    userref: Optional[int] = None,
    post_only: bool = True,
) -> str:
    data: Dict[str, Any] = {
        "pair": pair,
        "type": side,
        "ordertype": "limit",
        "price": str(limit_price),
        "volume": str(qty),
    }
    if userref is not None:
        data["userref"] = str(userref)
    if post_only:
        data["oflags"] = "post"
    data["close[ordertype]"] = "stop-loss-limit"
    data["close[price]"] = str(stop_trigger)
    data["close[price2]"] = str(stop_limit)

    res = auth.rest("AddOrder", data)
    txid = _extract_txid(res)
    if not txid:
        raise RuntimeError(f"Could not obtain txid from response: {res}")
    return txid


def place_limit_order(
    auth: KrakenAuth,
    pair: str,
    side: str,
    limit_price: float,
    qty: float,
    *,
    post_only: bool = False,
    reduce_only: bool = False,
) -> str:
    data: Dict[str, Any] = {
        "pair": pair,
        "type": side,
        "ordertype": "limit",
        "price": str(limit_price),
        "volume": str(qty),
    }
    if post_only:
        data["oflags"] = "post"
    if reduce_only:
        data["reduce_only"] = True
    res = auth.rest("AddOrder", data)
    txid = _extract_txid(res)
    if not txid:
        raise RuntimeError(f"Could not obtain txid from response: {res}")
    return txid


def cancel_order(auth: KrakenAuth, txid: str) -> Dict[str, Any]:
    return auth.rest("CancelOrder", {"txid": txid})


def get_balance(auth: KrakenAuth) -> Dict[str, str]:
    """Returns mapping like {'ZUSD': '123.45', 'XETH': '0.5', ...}"""
    return auth.rest("Balance", {})
