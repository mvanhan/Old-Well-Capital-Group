from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import coinbase_http as cb_http


def _request_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    status, data = cb_http.request("GET", path, params=params, auth=True)
    if 200 <= status < 300:
        return data
    raise RuntimeError(f"HTTP {status} for {path}: {data}")


def _request_post(path: str, payload: Dict[str, Any]) -> Any:
    return cb_http.authed_post(path, payload)


def _success_and_payload(resp: Any) -> Tuple[bool, Dict[str, Any]]:
    data = resp if isinstance(resp, dict) else {"raw": resp}
    http_status = int(data.get("_http_status", 200))

    if http_status < 200 or http_status >= 300:
        return False, data

    error_response = data.get("error_response")
    success_response = data.get("success_response")

    if error_response:
        return False, data
    if data.get("success") is False:
        return False, data
    if data.get("error"):
        return False, data

    if success_response and isinstance(success_response, dict):
        order_id = success_response.get("order_id")
        if order_id and not data.get("order_id"):
            data["order_id"] = order_id

    return True, data


def _collect_paginated(path: str, list_key: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None

    while True:
        merged: Dict[str, Any] = dict(params or {})
        if cursor:
            merged["cursor"] = cursor

        data = _request_get(path, merged)
        if not isinstance(data, dict):
            if isinstance(data, list):
                out.extend(item for item in data if isinstance(item, dict))
            break

        batch = data.get(list_key)
        if isinstance(batch, list):
            out.extend(item for item in batch if isinstance(item, dict))

        pagination = data.get("pagination")
        next_cursor = pagination.get("next_cursor") if isinstance(pagination, dict) else None
        if not next_cursor:
            break
        cursor = str(next_cursor)

    return out


def get_balances() -> List[Dict[str, Any]]:
    return _collect_paginated("/api/v3/brokerage/accounts", "accounts", {"limit": 250})


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
        "product_id": str(product_id).upper(),
        "side": str(side).upper(),
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
        "product_id": str(product_id).upper(),
        "side": str(side).upper(),
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
        "product_id": str(product_id).upper(),
        "side": str(side).upper(),
        "size": str(size),
        "limit_price": str(limit_price),
        "tp_price": str(tp_price),
        "sl_price": str(sl_price),
        "post_only": bool(post_only),
        "client_order_id": client_order_id,
    }


def cancel_order(order_id: str) -> bool:
    response = _request_post("/api/v3/brokerage/orders/batch_cancel", {"order_ids": [order_id]})
    data = response if isinstance(response, dict) else {"raw": response}

    http_status = int(data.get("_http_status", 200))
    if http_status < 200 or http_status >= 300:
        return False

    results = data.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            if first.get("success") is not None:
                return bool(first.get("success"))
            failure_reason = str(first.get("failure_reason") or "").upper()
            return failure_reason in {"UNKNOWN_CANCEL_ORDER", "ORDER_ALREADY_FILLED", "ORDER_NOT_FOUND", "ALREADY_CANCELLED"}

    if data.get("success") is not None:
        return bool(data.get("success"))

    return True


def get_order_status(order_id: str) -> Dict[str, Any]:
    data = _request_get(f"/api/v3/brokerage/orders/historical/{order_id}")
    return data if isinstance(data, dict) else {"raw": data}


def get_open_orders(product_id: Optional[str] = None) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "order_status": ["OPEN", "PENDING", "ACTIVE"],
        "limit": 250,
    }
    if product_id:
        params["product_ids"] = [str(product_id).upper()]
    return _collect_paginated("/api/v3/brokerage/orders/historical/batch", "orders", params)