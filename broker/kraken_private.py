# broker/kraken_private.py
import os, time, base64, hashlib, hmac, urllib.parse, requests
from typing import Dict, Any, List, Union

# Load .env automatically if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BASE = "https://api.kraken.com"

API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET = os.getenv("KRAKEN_API_SECRET", "")  # <-- put Base64 secret in .env

def _nonce() -> str:
    return str(int(time.time() * 1000))

def _sign(path: str, data: Dict[str, Any], secret_b64: str) -> str:
    postdata = urllib.parse.urlencode(data)
    encoded = (data["nonce"] + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret_b64), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()

def _post(path: str, data: Dict[str, Any]) -> Dict[str, Any]:
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Missing KRAKEN_API_KEY / KRAKEN_API_SECRET in environment (.env)")
    payload = {"nonce": _nonce(), **data}
    headers = {
        "API-Key": API_KEY,
        "API-Sign": _sign(path, payload, API_SECRET),
    }
    url = f"{BASE}{path}"
    r = requests.post(url, data=payload, headers=headers, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"Kraken error: {j['error']}")
    return j.get("result", j)

# ---- Public API wrappers (private endpoints) ----

def get_balance() -> Dict[str, Any]:
    return _post("/0/private/Balance", {})

def add_order(
    pair: str,
    side: str,           # "buy" or "sell"
    price: float,
    volume: float,
    post_only: bool = True,
    time_in_force: str = "GTC",
    ordertype: str = "limit",    # "limit", "stop-loss", "take-profit" etc.
) -> Dict[str, Any]:
    """
    For spot:
      - Limit entry: ordertype="limit", side="buy"/"sell", price
      - Stop-loss:   side="sell", ordertype="stop-loss", price=<trigger>
      - TP (limit):  side="sell", ordertype="limit", price=<target>  (sits on book)
    """
    data: Dict[str, Any] = {
        "pair": pair,
        "type": side.lower(),
        "ordertype": ordertype,
        "price": str(price),
        "volume": str(volume),
        "timeinforce": time_in_force,
    }
    if post_only and ordertype == "limit":
        data["oflags"] = "post"   # post-only only applies to LIMIT orders
    return _post("/0/private/AddOrder", data)

def cancel_order(txid: str) -> Dict[str, Any]:
    return _post("/0/private/CancelOrder", {"txid": txid})

def open_orders() -> Dict[str, Any]:
    return _post("/0/private/OpenOrders", {})

def query_orders(txids: Union[str, List[str]]) -> Dict[str, Any]:
    if isinstance(txids, (list, tuple)):
        txids = ",".join(txids)
    return _post("/0/private/QueryOrders", {"txid": txids})
