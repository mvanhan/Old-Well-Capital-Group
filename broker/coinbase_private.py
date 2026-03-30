from __future__ import annotations

import os
import uuid
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlencode

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


def _request_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    cl = _client()
    full_path = path
    if params:
        filtered = {k: v for k, v in params.items() if v is not None and v != ""}
        if filtered:
            full_path = f"{path}?{urlencode(filtered, doseq=True)}"
    return _to_dict(cl.get(full_path))


def _request_post(path: str, payload: Dict[str, Any]) -> Any:
    cl = _client()
    return _to_dict(cl.post(path, payload))


def _success_and_payload(resp: Any) -> Tuple[bool, Dict[str, Any]]:
    data = _to_dict(resp)
    if isinstance(data, dict):
        ok = bool(data.get("success", True)) and not data.get("error_response")
        return ok, data
    return True, {"raw": data}


def get_balances() -> List[Dict[str, Any]]:
    data = _request_get("/api/v3/brokerage/accounts")
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
    payload = {
        "client_order_id": client_order_id or str(uuid.uuid4()),
        "product_id": product_id,
        "side": side.upper(),
        "order_configuration": {
            "limit_limit_gtc": {
                "base_size": str(size),
                "limit_price": str(limit_price),
                "post_only": bool(post_only),
            }
        },
    }
    return _success_and_payload(_request_post("/api/v3/brokerage/orders", payload))


def place_market_ioc_order(
    product_id: str,
    side: str,
    size: str,
    client_order_id: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    payload = {
        "client_order_id": client_order_id or str(uuid.uuid4()),
        "product_id": product_id,
        "side": side.upper(),
        "order_configuration": {
            "market_market_ioc": {
                "base_size": str(size),
            }
        },
    }
    return _success_and_payload(_request_post("/api/v3/brokerage/orders", payload))


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
    resp = _request_post("/api/v3/brokerage/orders/batch_cancel", {"order_ids": [order_id]})
    if isinstance(resp, dict):
        results = resp.get("results") or []
        if results:
            return bool(results[0].get("success"))
        return bool(resp.get("success", True))
    return True


def get_order_status(order_id: str) -> Dict[str, Any]:
    data = _request_get(f"/api/v3/brokerage/orders/historical/{order_id}")
    return data if isinstance(data, dict) else {"raw": data}


def get_open_orders(product_id: Optional[str] = None) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"order_status": ["OPEN", "PENDING"]}
    if product_id:
        params["product_ids"] = [product_id]
    data = _request_get("/api/v3/brokerage/orders/historical/batch", params)
    if isinstance(data, dict):
        return data.get("orders", [])
    return data if isinstance(data, list) else []