# broker/coinbase_private.py
from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import Optional, Dict, Any, List, Tuple

# --- Load .env early (non-fatal if missing) ---
try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

# We rely on coinbase-advanced-py (pip install coinbase-advanced-py)
try:
    from coinbase.rest import RESTClient
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "Missing dependency 'coinbase-advanced-py'. "
        "Install with: pip install coinbase-advanced-py"
    ) from e

SDK_TIMEOUT = float(os.getenv("COINBASE_SDK_TIMEOUT", "10"))

def _client() -> RESTClient:
    """Build a REST client from env vars.

    Supported env names:
      - COINBASE_API_KEY / COINBASE_API_KEY_ID
      - COINBASE_API_SECRET / COINBASE_PRIVATE_KEY
    """
    api_key = os.getenv("COINBASE_API_KEY") or os.getenv("COINBASE_API_KEY_ID")
    api_secret = os.getenv("COINBASE_API_SECRET") or os.getenv("COINBASE_PRIVATE_KEY")
    if not api_key or not api_secret:
        raise RuntimeError("Missing Coinbase API credentials in environment (.env is supported).")
    try:
        return RESTClient(api_key=api_key, api_secret=api_secret, timeout=SDK_TIMEOUT)
    except TypeError:
        # Older versions of the SDK used different parameter names
        return RESTClient(key=api_key, secret=api_secret, timeout=SDK_TIMEOUT)

def _to_dict(obj: Any) -> Any:
    # Normalize SDK models / pydantic models to dicts
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

# -------------------- HELPERS -------------------- #
def _extract_order_id(resp: Dict[str, Any]) -> Optional[str]:
    """Best-effort extraction of order_id from Create Order response."""
    if not isinstance(resp, dict):
        return None
    sr = resp.get("success_response") or {}
    if isinstance(sr, dict) and sr.get("order_id"):
        return str(sr["order_id"])
    if resp.get("order_id"):
        return str(resp["order_id"])
    for k in ("id", "orderId", "orderID"):
        if k in resp:
            return str(resp[k])
    return None

def _accounts_index(cl: RESTClient) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
    Returns:
      by_currency: {'USD': {..., 'uuid': '...'}, 'USDC': {...}}
      by_uuid:     {'<uuid>': {...}}
    """
    data = _to_dict(cl.get_accounts())
    accts = data.get("accounts") if isinstance(data, dict) else data
    by_currency: Dict[str, Dict[str, Any]] = {}
    by_uuid: Dict[str, Dict[str, Any]] = {}
    if isinstance(accts, list):
        for a in accts:
            a = _to_dict(a)
            cur = str(a.get("currency") or a.get("asset") or a.get("symbol") or "").upper()
            uid = str(a.get("uuid") or a.get("id") or "")
            if cur:
                by_currency[cur] = a
            if uid:
                by_uuid[uid] = a
    return by_currency, by_uuid

# -------------------- BALANCES -------------------- #
def get_balances() -> List[Dict[str, str]]:
    """Return [{'currency': 'USDC', 'available': '10.00'}, ...]."""
    cl = _client()
    by_currency, _ = _accounts_index(cl)
    out: List[Dict[str, str]] = []
    for cur, a in by_currency.items():
        avail = None
        bal_field = a.get("available_balance")
        if isinstance(bal_field, dict):
            avail = bal_field.get("value")
        if avail is None:
            avail = a.get("available", a.get("balance", "0"))
        out.append({"currency": cur, "available": str(avail)})
    return out

def get_available(currency: str) -> Decimal:
    """Convenience accessor for one currency's available balance."""
    cl = _client()
    by_currency, _ = _accounts_index(cl)
    a = by_currency.get(currency.upper())
    if not a:
        return Decimal("0")
    val = None
    bal_field = a.get("available_balance")
    if isinstance(bal_field, dict):
        val = bal_field.get("value")
    if val is None:
        val = a.get("available", a.get("balance", "0"))
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal("0")

# -------------------- ORDERS -------------------- #
def place_market(
    product_id: str,
    side: str,          # "BUY" or "SELL"
    size: Optional[str] = None,   # base size
    funds: Optional[str] = None,  # quote funds
    client_oid: Optional[str] = None,
) -> Dict[str, Any]:
    """Place a market IOC order. Provide either base 'size' or quote 'funds'."""
    if not (size or funds):
        raise ValueError("place_market requires 'size' (base) or 'funds' (quote).")

    cl = _client()
    if not client_oid:
        client_oid = str(uuid.uuid4())

    cfg: Dict[str, Any] = {"market_market_ioc": {}}
    if size:
        cfg["market_market_ioc"]["base_size"] = str(size)
    if funds:
        cfg["market_market_ioc"]["quote_size"] = str(funds)

    payload = {
        "client_order_id": client_oid,
        "product_id": product_id,
        "side": side.upper(),
        "order_configuration": cfg,
    }
    resp = _to_dict(cl.post("/api/v3/brokerage/orders", data=payload))
    oid = _extract_order_id(resp)
    if oid:
        resp["order_id"] = oid  # normalize
    return resp

def place_limit(
    product_id: str,
    side: str,
    price: str,
    size: str,
    post_only: bool = False,
    client_oid: Optional[str] = None,
) -> Dict[str, Any]:
    """Place a limit GTC order. Set post_only=True for maker-only."""
    cl = _client()
    if not client_oid:
        client_oid = str(uuid.uuid4())
    payload = {
        "client_order_id": client_oid,
        "product_id": product_id,
        "side": side.upper(),
        "order_configuration": {
            "limit_limit_gtc": {
                "post_only": bool(post_only),
                "base_size": str(size),
                "limit_price": str(price),
            }
        },
    }
    resp = _to_dict(cl.post("/api/v3/brokerage/orders", data=payload))
    oid = _extract_order_id(resp)
    if oid:
        resp["order_id"] = oid  # normalize
    return resp

def place_limit_post_only(product_id: str, side: str, price: str, size: str, client_oid: Optional[str] = None) -> Dict[str, Any]:
    return place_limit(product_id, side, price, size, post_only=True, client_oid=client_oid)

def cancel_order(order_id: str) -> Dict[str, Any]:
    cl = _client()
    resp = cl.post("/api/v3/brokerage/orders/batch_cancel", data={"order_ids": [order_id]})
    return _to_dict(resp)

def get_order_status(order_id: str) -> Dict[str, Any]:
    cl = _client()
    resp = cl.get(f"/api/v3/brokerage/orders/historical/{order_id}")
    return _to_dict(resp)

def get_open_orders(product_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Best-effort open orders fetch. If endpoint is unavailable, return empty list."""
    cl = _client()
    try:
        params: Dict[str, Any] = {"order_status": ["OPEN"]}
        if product_id:
            params["product_id"] = product_id
        resp = cl.get("/api/v3/brokerage/orders/historical/batch", params=params)
        data = _to_dict(resp)
        orders = data.get("orders") or data.get("results") or []
        out: List[Dict[str, Any]] = []
        for o in orders:
            o = _to_dict(o)
            if product_id and o.get("product_id") != product_id:
                continue
            if (o.get("status") or o.get("order_status") or "").upper() in ("OPEN","PENDING","ACTIVE"):
                out.append(o)
        return out
    except Exception:
        return []

# -------------------- CONVERSIONS (USD ⇄ USDC) -------------------- #
# Some API versions accept currency *codes* for quote, but the *commit* call
# is stricter and expects account UUIDs. We resolve UUIDs automatically.
def _resolve_accounts(from_currency: str, to_currency: str) -> Tuple[str, str]:
    cl = _client()
    by_currency, _ = _accounts_index(cl)
    fa = by_currency.get(from_currency.upper())
    ta = by_currency.get(to_currency.upper())
    if not fa or not ta:
        raise RuntimeError(f"Accounts not found for convert {from_currency}->{to_currency}")
    from_uuid = str(fa.get("uuid") or fa.get("id") or "").strip()
    to_uuid = str(ta.get("uuid") or ta.get("id") or "").strip()
    if not from_uuid or not to_uuid:
        raise RuntimeError("Missing account UUIDs for convert commit.")
    return from_uuid, to_uuid

def convert(from_account: str, to_account: str, amount: str) -> Dict[str, Any]:
    """Convert 'amount' from one internal account (e.g., USD) to another (e.g., USDC)."""
    cl = _client()
    # 1) Create quote (prefer currency codes)
    quote_payload = {
        "from_account": str(from_account).upper(),
        "to_account": str(to_account).upper(),
        "amount": str(amount),
    }
    q = _to_dict(cl.post("/api/v3/brokerage/convert/quote", data=quote_payload))
    trade = _to_dict(q.get("trade", {}))
    trade_id = trade.get("id")
    if not trade_id:
        # Try again using UUIDs in case this API version requires them at quote time
        f_uuid, t_uuid = _resolve_accounts(from_account, to_account)
        quote_payload_uuid = {"from_account": f_uuid, "to_account": t_uuid, "amount": str(amount)}
        q = _to_dict(cl.post("/api/v3/brokerage/convert/quote", data=quote_payload_uuid))
        trade = _to_dict(q.get("trade", {}))
        trade_id = trade.get("id")
        if not trade_id:
            raise RuntimeError(f"Failed to create convert quote: {q}")

    # 2) Commit using UUIDs (most consistent)
    f_uuid, t_uuid = _resolve_accounts(from_account, to_account)
    commit_payload = {"from_account": f_uuid, "to_account": t_uuid}
    c = _to_dict(cl.post(f"/api/v3/brokerage/convert/trade/{trade_id}", data=commit_payload))
    if isinstance(c, dict) and "trade" in c and isinstance(c["trade"], dict):
        c["trade_id"] = c["trade"].get("id")
        c["status"] = c["trade"].get("status")
    return c

def convert_usd_to_usdc(amount: str) -> Dict[str, Any]:
    return convert("USD", "USDC", amount)

def convert_usdc_to_usd(amount: str) -> Dict[str, Any]:
    return convert("USDC", "USD", amount)

# Optional helpers for stop/flatten; callers should be resilient if unsupported.
def place_stop_market(product_id: str, side: str, stop_price: str, size: str, client_oid: Optional[str] = None) -> Dict[str, Any]:
    """Place a stop-market by submitting a stop-entry in the opposite direction as market IOC when triggered.
    Advanced Trade API doesn't expose a single 'stop_market' primitive, so this is a placeholder.
    Callers should not rely on this and instead handle risk off-venue when necessary.
    """
    raise NotImplementedError("stop_market_not_supported")
