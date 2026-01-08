# broker/coinbase_private.py
from __future__ import annotations

import os, uuid
from typing import Optional, Dict, Any, List, Tuple
from decimal import Decimal

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
    # remove wrapping quotes if present
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        s = s[1:-1]
    # convert literal \n into newline characters (one-line .env style)
    s = s.replace("\\n", "\n")
    return s


def _client():
    if RESTClient is None:
        raise RuntimeError("coinbase-advanced-py not installed")
    key = os.getenv("COINBASE_API_KEY")
    secret = _sanitize_secret(os.getenv("COINBASE_API_SECRET"))
    if not key or not secret:
        raise RuntimeError("COINBASE_API_KEY/SECRET not set")
    return RESTClient(key, secret)


def _to_dict(x):
    try:
        if hasattr(x, "to_dict"):
            return x.to_dict()
        if hasattr(x, "model_dump"):
            return x.model_dump()
    except Exception:
        pass
    return x


# ---- Balances ----
def get_balances() -> List[Dict[str, Any]]:
    cl = _client()
    resp = cl.get("/api/v3/brokerage/accounts")
    d = _to_dict(resp)
    return d.get("accounts", d)


# ---- Orders ----
def place_limit_order(product_id: str, side: str, size: str, limit_price: str, post_only: bool, client_order_id: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
    """
    Places a maker LIMIT (POST_ONLY) order.
    """
    cl = _client()
    body = {
        "client_order_id": client_order_id or str(uuid.uuid4()),
        "product_id": product_id,
        "side": side.lower(),  # coinbase expects "buy"/"sell"
        "order_configuration": {
            "limit_limit_gtc": {
                "post_only": bool(post_only),
                "limit_price": str(limit_price),
                "base_size": str(size),
            }
        },
    }
    resp = _to_dict(cl.post("/api/v3/brokerage/orders", body))
    ok = bool(resp.get("success", True))
    return ok, resp


def place_bracket_order(product_id: str, side: str, size: str, limit_price: str, tp_price: str, sl_price: str, post_only: bool, client_order_id: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
    """
    Attempts parent + bracket (best-effort). Many accounts/products don't support true server-side OCO.
    We place the parent and return its response; caller should handle TP/SL on fill if needed.
    """
    cl = _client()
    parent = {
        "client_order_id": client_order_id or str(uuid.uuid4()),
        "product_id": product_id,
        "side": side.lower(),
        "order_configuration": {
            "limit_limit_gtc": {
                "post_only": bool(post_only),
                "limit_price": str(limit_price),
                "base_size": str(size),
            }
        },
    }
    parent_resp = _to_dict(cl.post("/api/v3/brokerage/orders", parent))
    ok = bool(parent_resp.get("success", True))
    return ok and bool(parent_resp.get("order_id")), parent_resp


def cancel_order(order_id: str) -> bool:
    cl = _client()
    resp = _to_dict(cl.post(f"/api/v3/brokerage/orders/{order_id}/cancel"))
    return bool(resp.get("success", True))


def get_order_status(order_id: str) -> Dict[str, Any]:
    cl = _client()
    resp = _to_dict(cl.get(f"/api/v3/brokerage/orders/historical/{order_id}"))
    return resp


def get_open_orders(product_id: Optional[str] = None) -> List[Dict[str, Any]]:
    cl = _client()
    params = f"?product_id={product_id}" if product_id else ""
    resp = _to_dict(cl.get(f"/api/v3/brokerage/orders{params}"))
    if isinstance(resp, dict) and "orders" in resp:
        return resp["orders"]
    return resp if isinstance(resp, list) else []
