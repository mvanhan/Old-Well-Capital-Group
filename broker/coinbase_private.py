# broker/coinbase_private.py
import uuid
from typing import Optional, Dict, Any

try:
    from coinbase.rest import RESTClient
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "Missing dependency 'coinbase-advanced-py'. "
        "Add it to requirements and pip install before using Coinbase runners."
    ) from e


class CoinbasePrivate:
    """
    Thin wrapper over the official SDK RESTClient for create/get orders.
    We use the generic 'post' to send the full payload including
    attached TP/SL (trigger_bracket_gtc).
    """

    def __init__(self, api_key: str, api_secret: str, timeout: int = 10):
        self.client = RESTClient(api_key=api_key, api_secret=api_secret, timeout=timeout)

    # ---------- Orders ----------
    def create_limit_buy_with_bracket(
        self,
        product_id: str,
        base_size: str,
        limit_price: str,
        post_only: bool,
        tp_limit_price: str,
        sl_stop_trigger_price: str,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Places a post-only LIMIT BUY with attached Take-Profit (limit) and Stop-Loss trigger,
        using 'trigger_bracket_gtc' so that one cancels the other automatically.

        Endpoint: POST /api/v3/brokerage/orders
        Docs: Attached TP/SL via 'attached_order_configuration.trigger_bracket_gtc'
        """
        if not client_order_id:
            client_order_id = f"owcg-{uuid.uuid4().hex[:16]}"

        payload = {
            "client_order_id": client_order_id,
            "product_id": product_id,
            "side": "BUY",
            "order_configuration": {
                "limit_limit_gtc": {
                    "base_size": str(base_size),
                    "limit_price": str(limit_price),
                    "post_only": bool(post_only),
                }
            },
            "attached_order_configuration": {
                "trigger_bracket_gtc": {
                    "limit_price": str(tp_limit_price),
                    "stop_trigger_price": str(sl_stop_trigger_price),
                }
            },
        }
        # Use SDK generic POST so we control the body precisely
        resp = self.client.post("/api/v3/brokerage/orders", data=payload)
        # SDK returns a model; expose dict for consistency with rest of codebase
        return resp.to_dict() if hasattr(resp, "to_dict") else resp

    def get_order(self, order_id: str) -> Dict[str, Any]:
        o = self.client.get_order(order_id=order_id)
        return o.to_dict() if hasattr(o, "to_dict") else o
