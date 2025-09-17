# broker/coinbase_private.py
from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import Optional, Dict, Any

try:
    # coinbase-advanced-py (Advanced Trade)
    from coinbase.rest import RESTClient
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
    return RESTClient(key=api_key, secret=api_secret, timeout=SDK_TIMEOUT)

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
    Place a post-only LIMIT parent with an attached GTC trigger bracket (OCO):
      - Parent: maker-only LIMIT (BUY or SELL)
      - Attached: trigger_bracket_gtc with {limit_price, stop_trigger_price}

    Returns the Coinbase response as a dict.
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
            },
            "trigger_bracket_gtc": {
                "take_profit": {"limit_price": tp_limit_price},
                "stop_loss":   {"stop_trigger_price": stop_trigger_price},
            },
        },
    }
    resp = cl.post("/api/v3/brokerage/orders", data=payload)
    return _to_dict(resp)

def trigger_bracket_limit_maker(*, product_id, side, base_size, limit_price, tp_limit_price, stop_trigger_price, client_order_id=None):
    return add_order_limit_with_bracket(
        product_id=product_id,
        side=side,
        base_size=base_size,
        limit_price=limit_price,
        tp_limit_price=tp_limit_price,
        stop_trigger_price=stop_trigger_price,
        post_only=True,
        client_order_id=client_order_id,
    )

def get_order(order_id: str) -> Dict[str, Any]:
    cl = _client()
    resp = cl.get(f"/api/v3/brokerage/orders/historical/{order_id}")
    return _to_dict(resp)

def cancel_order(order_id: str) -> Dict[str, Any]:
    """
    Cancel a single order by id.
    """
    cl = _client()
    resp = cl.post("/api/v3/brokerage/orders/batch_cancel", data={"order_ids": [order_id]})
    return _to_dict(resp)

def get_available(currency: str) -> Decimal:
    """
    Return available balance for a given currency (e.g., 'USD', 'USDC').
    """
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
