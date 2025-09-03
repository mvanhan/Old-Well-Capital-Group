# broker/binance_private.py
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode
from decimal import Decimal
from typing import Optional, Dict, Any

class BinancePrivate:
    """
    Minimal Spot private client with signed requests and helpers for:
      - POST /api/v3/order (LIMIT_MAKER entry)
      - GET/DELETE /api/v3/order (status/cancel)
      - POST /api/v3/orderList/oco (place OCO TP/SL)
    Works for binance.com and binance.us by switching api_base.
    """
    def __init__(self, api_key: str, api_secret: str, api_base: str = "https://api.binance.com",
                 recv_window_ms: int = 5000, timeout: int = 10):
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.recv_window_ms = recv_window_ms

    # ---------- HTTP ----------
    def _headers(self):
        return {"X-MBX-APIKEY": self.api_key}

    def _ts_params(self, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
        p = {
            "timestamp": int(time.time() * 1000),
            "recvWindow": int(self.recv_window_ms),
        }
        if extra:
            p.update(extra)
        return p

    def _sign(self, params: Dict[str, Any]) -> str:
        query = urlencode(params, True)
        return hmac.new(self.api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, params: Dict[str, Any] | None = None, signed: bool = False):
        url = f"{self.api_base}{path}"
        params = params or {}
        if signed:
            params = self._ts_params(params)
            params["signature"] = self._sign(params)
        if method == "GET":
            r = requests.get(url, params=params, headers=self._headers() if signed else None, timeout=self.timeout)
        elif method == "POST":
            r = requests.post(url, params=params, headers=self._headers(), timeout=self.timeout)
        elif method == "DELETE":
            r = requests.delete(url, params=params, headers=self._headers(), timeout=self.timeout)
        else:
            raise ValueError(f"Unsupported method {method}")
        if r.status_code >= 400:
            try:
                raise RuntimeError(f"HTTP {r.status_code}: {r.json()}")
            except Exception:
                r.raise_for_status()
        return r.json() if r.text else {}

    # ---------- Orders ----------
    def new_order(self, symbol: str, side: str, type_: str, **kwargs):
        """
        Generic order. Example:
        new_order("APTUSDT", "BUY", "LIMIT_MAKER", price="4.5000", quantity="2.34")
        """
        params = {"symbol": symbol.upper(), "side": side.upper(), "type": type_.upper()}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return self._request("POST", "/api/v3/order", params, signed=True)

    def query_order(self, symbol: str, orderId: Optional[int] = None, origClientOrderId: Optional[str] = None):
        if not orderId and not origClientOrderId:
            raise ValueError("Either orderId or origClientOrderId is required")
        params = {"symbol": symbol.upper()}
        if orderId:
            params["orderId"] = orderId
        if origClientOrderId:
            params["origClientOrderId"] = origClientOrderId
        return self._request("GET", "/api/v3/order", params, signed=True)

    def cancel_order(self, symbol: str, orderId: Optional[int] = None, origClientOrderId: Optional[str] = None):
        if not orderId and not origClientOrderId:
            raise ValueError("Either orderId or origClientOrderId is required")
        params = {"symbol": symbol.upper()}
        if orderId:
            params["orderId"] = orderId
        if origClientOrderId:
            params["origClientOrderId"] = origClientOrderId
        return self._request("DELETE", "/api/v3/order", params, signed=True)

    # ---------- OCO ----------
    def create_oco_sell_tpsl(self,
                             symbol: str,
                             quantity: Decimal | float | str,
                             tp_price: Decimal | float | str,
                             sl_stop_price: Decimal | float | str,
                             sl_limit_price: Decimal | float | str,
                             tif: str = "GTC",
                             new_order_resp_type: str = "RESULT"):
        """
        Place an OCO on the SELL side with:
          - above leg: LIMIT_MAKER at tp_price
          - below leg: STOP_LOSS_LIMIT with stop=sl_stop_price, limit=sl_limit_price

        Endpoint: POST /api/v3/orderList/oco
        Required params per docs:
          symbol, side, quantity,
          aboveType/abovePrice/aboveTimeInForce,
          belowType/belowStopPrice/belowPrice/belowTimeInForce
        """
        params = {
            "symbol": symbol.upper(),
            "side": "SELL",
            "quantity": str(quantity),
            # above = take-profit limit maker
            "aboveType": "LIMIT_MAKER",
            "abovePrice": str(tp_price),
            "aboveTimeInForce": tif,
            # below = stop-loss-limit
            "belowType": "STOP_LOSS_LIMIT",
            "belowStopPrice": str(sl_stop_price),
            "belowPrice": str(sl_limit_price),
            "belowTimeInForce": tif,
            "newOrderRespType": new_order_resp_type
        }
        return self._request("POST", "/api/v3/orderList/oco", params, signed=True)
