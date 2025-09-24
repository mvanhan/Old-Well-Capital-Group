# broker/coinbase_private.py
from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import Optional, Dict, Any

try:
    from coinbase.rest import RESTClient
    from requests import HTTPError
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "Missing dependency 'coinbase-advanced-py'. "
        "Install with: pip install coinbase-advanced-py"
    ) from e

SDK_TIMEOUT = float(os.getenv("COINBASE_SDK_TIMEOUT", "10"))

def _to_dict(resp):
    return resp.to_dict() if hasattr(resp, "to_dict") else resp

def _client() -> RESTClient:
    api_key = os.getenv("COINBASE_API_KEY") or os.getenv("COINBASE_API_KEY_ID")
    api_secret = os.getenv("COINBASE_API_SECRET") or os.getenv("COINBASE_PRIVATE_KEY")
    if not api_key or not api_secret:
        raise RuntimeError("Missing Coinbase API credentials in environment.")
    try:
        return RESTClient(api_key=api_key, api_secret=api_secret, timeout=SDK_TIMEOUT)
    except TypeError:
        return RESTClient(key=api_key, secret=api_secret, timeout=SDK_TIMEOUT)

# ---------- ORDER PLACEMENT HELPERS ----------

def add_order_limit_only(
    *,
    product_id: str,
    side: str,            # "BUY" or "SELL"
    base_size: str,       # stringified decimal
    limit_price: str,     # parent limit price
    post_only: bool = True,
    client_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Place a post-only LIMIT GTC without any attached orders.
    """
    cl = _client()
    if client_order_id is None:
        client_order_id = str(uuid.uuid4())
    payload = {
        "client_order_id": client_order_id,
        "product_id": product_id,
        "side": side.upper(),
        "order_configuration": {
            "limit_limit_gtc": {
                "post_only": post_only,
                "base_size": base_size,
                "limit_price": limit_price,
            }
        },
    }
    resp = cl.post("/api/v3/brokerage/orders", data=payload)
    return _to_dict(resp)

def add_order_limit_with_bracket(
    *,
    product_id: str,
    side: str,                      # "BUY" or "SELL"
    base_size: str,                 # stringified decimal
    limit_price: str,               # parent limit price
    tp_limit_price: str,            # attached TP limit price
    stop_trigger_price: str,        # attached SL trigger price
    post_only: bool = True,
    client_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Place a post-only LIMIT parent with an attached Trigger Bracket (GTC).

    Correct schema:
      - order_configuration: parent oneof (limit_limit_gtc)
      - attached_order_configuration: { "trigger_bracket_gtc": { ... } }
    """
    cl = _client()
    if client_order_id is None:
        client_order_id = str(uuid.uuid4())

    payload = {
        "client_order_id": client_order_id,
        "product_id": product_id,
        "side": side.upper(),
        "order_configuration": {
            "limit_limit_gtc": {
                "post_only": post_only,
                "base_size": base_size,
                "limit_price": limit_price,
            }
        },
        "attached_order_configuration": {
            "trigger_bracket_gtc": {
                # inherits size from the parent
                "limit_price": tp_limit_price,
                "stop_trigger_price": stop_trigger_price,
            }
        },
    }
    resp = cl.post("/api/v3/brokerage/orders", data=payload)
    return _to_dict(resp)

# ---------- ACCOUNT / ORDER MGMT ----------

def get_order(order_id: str) -> Dict[str, Any]:
    cl = _client()
    resp = cl.get(f"/api/v3/brokerage/orders/historical/{order_id}")
    return _to_dict(resp)

def cancel_order(order_id: str) -> Dict[str, Any]:
    cl = _client()
    resp = cl.post("/api/v3/brokerage/orders/batch_cancel", data={"order_ids": [order_id]})
    return _to_dict(resp)

def get_available(currency: str) -> Decimal:
    cl = _client()
    acc = cl.get_accounts()
    d = _to_dict(acc)
    for a in d.get("accounts", []):
        if a.get("currency") == currency:
            bal = a.get("available_balance") or {}
            try:
                return Decimal(str(bal.get("value", "0")))
            except Exception:
                return Decimal("0")
    return Decimal("0")
