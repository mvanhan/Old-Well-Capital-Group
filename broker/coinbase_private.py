# broker/coinbase_private.py
from __future__ import annotations

import os
import uuid
from typing import Optional, Dict, Any

try:
    # coinbase-advanced-py (Advanced Trade)
    from coinbase.rest import RESTClient
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "Missing dependency 'coinbase-advanced-py'. "
        "Install with: pip install coinbase-advanced-py"
    ) from e

# Configurable timeouts (seconds)
SDK_TIMEOUT = float(os.getenv("CB_SDK_TIMEOUT", "10"))

def _to_dict(obj):
    """Convert SDK response objects to plain dict when possible."""
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    return obj

def _client() -> RESTClient:
    """Construct a REST client using env creds with an explicit timeout."""
    api_key = os.getenv("COINBASE_API_KEY")
    api_secret = os.getenv("COINBASE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("COINBASE_API_KEY / COINBASE_API_SECRET are required in .env")
    return RESTClient(api_key=api_key, api_secret=api_secret, timeout=SDK_TIMEOUT)

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

    Matches the signature expected by run_live_coinbase_stables.py.
    Returns the Coinbase API response as a plain dict.
    """
    cl = _client()
    if client_order_id is None:
        client_order_id = f"owcg-{uuid.uuid4().hex[:16]}"

    # Coinbase Advanced expects this shape for a limit parent order:
    order_cfg = {
        "limit_limit_gtc": {
            "base_size": str(base_size),
            "limit_price": str(limit_price),
            "post_only": bool(post_only),
        }
    }

    payload = {
        "client_order_id": client_order_id,
        "product_id": product_id,
        "side": side.upper(),  # "BUY" or "SELL"
        "order_configuration": order_cfg,
        "attached_order_configuration": {
            "trigger_bracket_gtc": {
                "limit_price": str(tp_limit_price),
                "stop_trigger_price": str(stop_trigger_price),
            }
        },
    }

    resp = cl.post("/api/v3/brokerage/orders", data=payload)
    return _to_dict(resp)

# Backwards-/alternate-name compatibility expected by the live runner:
def trigger_bracket_limit_maker(**kwargs) -> Dict[str, Any]:
    """Alias to add_order_limit_with_bracket for compatibility."""
    return add_order_limit_with_bracket(**kwargs)

# Optional convenience (not required by the live runner)
def get_order(order_id: str) -> Dict[str, Any]:
    cl = _client()
    o = cl.get_order(order_id=order_id)
    return _to_dict(o)
