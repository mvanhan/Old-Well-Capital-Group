from __future__ import annotations

import os
import uuid
from typing import Optional, Dict, Any, List, Tuple

try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

try:
    from coinbase.rest import RESTClient  # type: ignore
except Exception:
    RESTClient = None  # type: ignore


def _sanitize_secret(raw: str | None) -> str | None:
    if not raw:
        return raw
    s = raw.strip()
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        s = s[1:-1]
    return s.replace("\\n", "\n")


def _client() -> RESTClient:
    if RESTClient is None:
        raise RuntimeError("coinbase-advanced-py not installed")
    key = os.getenv("COINBASE_API_KEY")
    secret = _sanitize_secret(os.getenv("COINBASE_API_SECRET"))
    if not key or not secret:
        raise RuntimeError("COINBASE_API_KEY/SECRET not set")
    timeout = float(os.getenv("CB_SDK_TIMEOUT", "10"))
    return RESTClient(api_key=key, api_secret=secret, timeout=timeout)


def _to_dict(value: Any) -> Any:
    try:
        if hasattr(value, "to_dict"):
            return value.to_dict()
        if hasattr(value, "model_dump"):
            return value.model_dump()
    except Exception:
        pass
    return value


def _success_and_payload(resp: Any) -> Tuple[bool, Dict[str, Any]]:
    data = _to_dict(resp)
    if isinstance(data, dict):
        ok = bool(data.get("success", True)) and not data.get("error_response")
        return ok, data
    return True, {"raw": data}


def get_balances() -> List[Dict[str, Any]]:
    cl = _client()
    resp = cl.get("/api/v3/brokerage/accounts")
    data = _to_dict(resp)
    if isinstance(data, dict):
        return data.get("accounts", [])
    return data if isinstance(data, list) else []


def place_limit_order(
    product_id: str,
    side: str,
    size: str,
    limit_price: str,
    post_only: bool,
    client_order_id: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    cl = _client()
    payload = {
        "client_order_id": client_order_id or str(uuid.uuid4()),
        "product_id": product_id,
        "side": side.upper(),
        "order_configuration": {
            "limit_limit_gtc": {
                "post_only": bool(post_only),
                "limit_price": str(limit_price),
                "base_size": str(size),
            }
        },
    }
    return _success_and_payload(cl.post("/api/v3/brokerage/orders", payload))


def place_bracket_order(
    product_id: str,
    side: str,
    size: str,
    limit_price: str,
    tp_price: str,
    sl_price: str,
    post_only: bool,
    client_order_id: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    return False, {
        "success": False,
        "error": "not_implemented",
        "message": "Server-side bracket orders are not implemented in this repo.",
        "product_id": product_id,
        "side": side,
        "size": str(size),
        "limit_price": str(limit_price),
        "tp_price": str(tp_price),
        "sl_price": str(sl_price),
        "post_only": bool(post_only),
        "client_order_id": client_order_id,
    }


def cancel_order(order_id: str) -> bool:
    cl = _client()
    resp = _to_dict(cl.post("/api/v3/brokerage/orders/batch_cancel", {"order_ids": [order_id]}))
    if isinstance(resp, dict):
        results = resp.get("results") or []
        if results:
            return bool(results[0].get("success"))
        return bool(resp.get("success", True))
    return True


def get_order_status(order_id: str) -> Dict[str, Any]:
    cl = _client()
    return _to_dict(cl.get(f"/api/v3/brokerage/orders/historical/{order_id}"))


def get_open_orders(product_id: Optional[str] = None) -> List[Dict[str, Any]]:
    cl = _client()
    path = "/api/v3/brokerage/orders/historical/batch?order_status=OPEN"
    if product_id:
        path += f"&product_id={product_id}"
    resp = _to_dict(cl.get(path))
    if isinstance(resp, dict):
        return resp.get("orders", [])
    return resp if isinstance(resp, list) else []