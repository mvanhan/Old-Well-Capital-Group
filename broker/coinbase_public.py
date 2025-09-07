from __future__ import annotations
"""
Lightweight Coinbase Advanced public helpers for OWCG.

Exposes:
- get_products() -> list[dict]
- get_best_bid_ask(product_id) -> (Decimal bid, Decimal ask)

Robust fetch strategy:
  A) Try PUBLIC endpoints first via SDK (if available)
  B) Fallback to PUBLIC HTTPS endpoints with `requests`
  C) If still needed and API keys exist, fall back to PRIVATE SDK calls

Requires:
  - coinbase-advanced-py
  - requests
  - python-dotenv (optional; for loading API creds)
"""

from decimal import Decimal
from typing import Dict, List, Tuple, Any, Optional
import os

# Optional .env loader
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Coinbase Advanced SDK (we’ll use it when possible)
try:
    from coinbase.rest import RESTClient  # provided by coinbase-advanced-py
except Exception as e:
    raise RuntimeError(
        "Missing dependency 'coinbase-advanced-py'. Install it via:\n"
        "  pip install coinbase-advanced-py"
    ) from e

# Public HTTPS fallback
try:
    import requests
except Exception as e:
    raise RuntimeError(
        "Missing dependency 'requests'. Install it via:\n"
        "  pip install requests"
    ) from e

_HTTP_BASE = os.getenv("COINBASE_API_URL", "https://api.coinbase.com")
_CLIENT: Optional[RESTClient] = None
_STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "PYUSD", "USDP"}


def _get_client() -> RESTClient:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    api_key = os.getenv("COINBASE_API_KEY")
    api_secret = os.getenv("COINBASE_API_SECRET")
    api_passphrase = os.getenv("COINBASE_API_PASSPHRASE")

    # RESTClient can be used without creds for PUBLIC calls (on most versions),
    # but some combos treat /brokerage/products as private → we rely on public HTTP for that anyway.
    try:
        if api_key and api_secret and api_passphrase:
            _CLIENT = RESTClient(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        else:
            _CLIENT = RESTClient()
    except TypeError:
        # Older SDK signatures
        if api_key and api_secret and api_passphrase:
            _CLIENT = RESTClient(api_key, api_secret, api_passphrase)  # type: ignore
        else:
            _CLIENT = RESTClient()  # type: ignore
    return _CLIENT


def _http_get(path: str, params: dict | None = None) -> dict:
    url = f"{_HTTP_BASE}{path}"
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def _as_dec(x: Any, default: str = "0") -> Decimal:
    if x is None:
        return Decimal(default)
    return Decimal(str(x))


def _pick(d: Dict[str, Any], *keys: str, default: Any = None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def get_products() -> List[Dict[str, Any]]:
    """
    Return a normalized list of products with fields our code expects:
      product_id, base_increment, quote_increment, min_order_size,
      fx_stablecoin (bool), base_name, quote_name, status, price_increment
    """
    client = _get_client()

    products_raw: List[Dict[str, Any]] | None = None
    errors: List[str] = []

    # A) Try PUBLIC SDK call if available in this SDK build
    try:
        if hasattr(client, "get_market_products"):
            resp_public = client.get_market_products()  # PUBLIC
            products_raw = resp_public.get("products", resp_public)
            if isinstance(products_raw, dict):
                products_raw = products_raw.get("products", [])
    except Exception as e:
        errors.append(f"SDK.get_market_products failed: {e!r}")
        products_raw = None

    # B) PUBLIC HTTPS fallback
    if not products_raw:
        try:
            resp = _http_get("/api/v3/brokerage/market/products", params={"limit": 500})
            products_raw = resp.get("products") or resp  # tolerate either shape
            if isinstance(products_raw, dict):
                products_raw = products_raw.get("products", [])
        except Exception as e:
            errors.append(f"HTTPS /market/products failed: {e!r}")
            products_raw = None

    # C) PRIVATE fallback only if creds exist
    if (not products_raw) and os.getenv("COINBASE_API_KEY"):
        try:
            resp_private = client.get_products()  # PRIVATE
            products_raw = resp_private.get("products", resp_private)
            if isinstance(products_raw, dict):
                products_raw = products_raw.get("products", [])
        except Exception as e:
            errors.append(f"SDK.get_products (private) failed: {e!r}")
            products_raw = None

    if not products_raw:
        raise RuntimeError(
            "Unable to fetch products from Coinbase (public and private endpoints failed). "
            + " | ".join(errors)
        )

    out: List[Dict[str, Any]] = []
    for p in products_raw:
        product_id = _pick(p, "product_id", "id")
        if not product_id:
            continue

        base = _pick(p, "base_name", "base_currency", "base_display_symbol")
        quote = _pick(p, "quote_name", "quote_currency", "quote_display_symbol")

        base_inc  = _as_dec(_pick(p, "base_increment", "base_increment_decimal", "base_min_size", default="0.00000001"))
        quote_inc = _as_dec(_pick(p, "quote_increment", "price_increment", "quote_increment_decimal", default="0.00000001"))
        min_size  = _as_dec(_pick(p, "min_order_size", "base_min_size", default="0"))

        fx_flag = _pick(p, "fx_stablecoin", "is_stablecoin", default=None)
        if fx_flag is None:
            base_sym = (base or "").upper()
            quote_sym = (quote or "").upper()
            fx_flag = (quote_sym == "USD" and base_sym in _STABLE_SYMBOLS) or \
                      (base_sym in _STABLE_SYMBOLS and quote_sym in _STABLE_SYMBOLS)

        out.append({
            "product_id": product_id,
            "base_name": base or (product_id.split("-")[0] if "-" in product_id else None),
            "quote_name": quote or (product_id.split("-")[1] if "-" in product_id else None),
            "base_increment": str(base_inc),
            "quote_increment": str(quote_inc),
            "min_order_size": str(min_size),
            "fx_stablecoin": bool(fx_flag),
            "status": _pick(p, "status", default=None),
            "price_increment": str(_as_dec(_pick(p, "price_increment", default=quote_inc))),
        })

    return out


def get_best_bid_ask(product_id: str) -> Tuple[Decimal, Decimal]:
    """
    Return (best_bid, best_ask) as Decimals using PUBLIC book.
    Tries SDK first, then HTTPS fallback.
    """
    client = _get_client()

    # SDK attempt
    book: Dict[str, Any] | None = None
    try:
        try:
            book = client.get_product_book(product_id=product_id, limit=1)
        except TypeError:
            book = client.get_product_book(product_id=product_id, level=1)
    except Exception:
        book = None

    # HTTPS fallback
    if book is None:
        resp = _http_get("/api/v3/brokerage/market/product_book", params={"product_id": product_id, "limit": 1})
        book = resp

    bids = book.get("bids") or (book.get("pricebooks") or {}).get("bids")
    asks = book.get("asks") or (book.get("pricebooks") or {}).get("asks")
    if not bids or not asks:
        raise ValueError(f"No L1 quotes available for {product_id}")

    def _first_price(side):
        first = side[0]
        if isinstance(first, dict):
            return Decimal(str(first.get("price")))
        if isinstance(first, (list, tuple)) and first:
            return Decimal(str(first[0]))
        return Decimal(str(first))

    bid_price = _first_price(bids)
    ask_price = _first_price(asks)
    if bid_price <= 0 or ask_price <= 0:
        raise ValueError(f"Invalid quotes for {product_id}: bid={bid_price}, ask={ask_price}")

    return bid_price, ask_price
