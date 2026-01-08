# broker/coinbase_public.py
from __future__ import annotations
import os
from typing import Any, Dict, List, Tuple
from decimal import Decimal

# .env is optional for public calls, but we support it for convenience
try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

# Official Coinbase Advanced Trade SDK
try:
    from coinbase.rest import RESTClient  # type: ignore
except Exception:
    RESTClient = None  # type: ignore


def _sanitize_secret(raw: str | None) -> str | None:
    if not raw:
        return raw
    s = raw.strip()
    # strip surrounding quotes if present
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        s = s[1:-1]
    # turn \n into real newlines for single-line .env secrets
    if "\\n" in s and "-----BEGIN" not in s:
        # sometimes users paste without PEM headers; leave as-is in that case
        pass
    s = s.replace("\\n", "\n")
    return s


def _client():
    """
    Returns a RESTClient. If API key/secret are present, returns an authenticated client.
    Otherwise returns an unauthenticated client (public endpoints only).
    """
    if RESTClient is None:
        return None
    key = os.getenv("COINBASE_API_KEY")
    secret = _sanitize_secret(os.getenv("COINBASE_API_SECRET"))
    if key and secret:
        return RESTClient(key, secret)
    # unauthenticated client for public endpoints
    return RESTClient()


def _to_dict(x):
    try:
        if hasattr(x, "to_dict"):
            return x.to_dict()
        if hasattr(x, "model_dump"):
            return x.model_dump()
    except Exception:
        pass
    return x


def get_products() -> List[Dict[str, Any]]:
    """
    Public products list (no auth required).
    Normalizes fields we rely on.
    """
    cl = _client()
    if cl is None:
        # minimal offline fallback set
        return [
            {
                "product_id": "USDT-USD",
                "base_currency_id": "USDT",
                "quote_currency_id": "USD",
                "base_increment": "0.01",
                "quote_increment": "0.0001",
                "price_increment": "0.0001",
                "min_order_size": "1",
                "base_min_size": "1",
            },
            {
                "product_id": "USDC-USD",
                "base_currency_id": "USDC",
                "quote_currency_id": "USD",
                "base_increment": "0.01",
                "quote_increment": "0.0001",
                "price_increment": "0.0001",
                "min_order_size": "1",
                "base_min_size": "1",
            },
            {
                "product_id": "USDT-USDC",
                "base_currency_id": "USDT",
                "quote_currency_id": "USDC",
                "base_increment": "0.01",
                "quote_increment": "0.0001",
                "price_increment": "0.0001",
                "min_order_size": "1",
                "base_min_size": "1",
            },
        ]
    # Prefer the SDK's public endpoint
    resp = cl.get_public_products()
    data = _to_dict(resp)
    prods = data.get("products") if isinstance(data, dict) else data
    out: List[Dict[str, Any]] = []
    for p in prods or []:
        d = dict(p)
        # normalize
        d["price_increment"] = d.get("price_increment", d.get("quote_increment"))
        d["base_currency_id"] = d.get("base_currency_id", d.get("base_currency"))
        d["quote_currency_id"] = d.get("quote_currency_id", d.get("quote_currency"))
        out.append(d)
    return out


def get_best_bid_ask(product_id: str) -> Tuple[Decimal, Decimal]:
    """
    Uses the private ticker if creds exist; otherwise approximates from public L2 book top.
    """
    cl = _client()
    if cl is None:
        return Decimal("0.9999"), Decimal("1.0001")

    # Try private ticker first (more direct)
    try:
        resp = cl.get(f"/api/v3/brokerage/products/{product_id}/ticker")
        d = _to_dict(resp)
        bid = d.get("bid") if isinstance(d, dict) else None
        ask = d.get("ask") if isinstance(d, dict) else None
        if bid and ask:
            return Decimal(str(bid)), Decimal(str(ask))
    except Exception:
        pass

    # Fallback to public L2 book (no auth required)
    try:
        book = cl.get_public_product_book(product_id=product_id, limit=1)
        dd = _to_dict(book)
        bids = dd.get("bids") or []
        asks = dd.get("asks") or []
        b = Decimal(str(bids[0][0])) if bids else Decimal("0")
        a = Decimal(str(asks[0][0])) if asks else Decimal("0")
        return b, a
    except Exception:
        return Decimal("0"), Decimal("0")


def get_l2(product_id: str, depth: int = 5) -> Dict[str, List[List[str]]]:
    """
    Public L2 book (no auth required).
    """
    cl = _client()
    if cl is None:
        return {"bids": [["0.9999", "10000"]], "asks": [["1.0001", "10000"]]}
    # SDK has a public product book call
    resp = cl.get_public_product_book(product_id=product_id, limit=depth)
    return _to_dict(resp)


def get_accounts() -> List[Dict[str, Any]]:
    """
    Private accounts (requires auth). Provided for convenience/testing.
    """
    cl = _client()
    if cl is None:
        return [{"currency": "USD", "available": "1000"}, {"currency": "USDT", "available": "1000"}, {"currency": "USDC", "available": "1000"}]
    try:
        resp = cl.get("/api/v3/brokerage/accounts")
        d = _to_dict(resp)
        return d.get("accounts", d)
    except Exception:
        return []
