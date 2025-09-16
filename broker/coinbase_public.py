from __future__ import annotations
"""
Lightweight Coinbase Advanced public helpers for OWCG.

Exposes:
- get_products() -> list[dict] with normalized fields your screener expects
- get_best_bid_ask(product_id) -> (Decimal bid, Decimal ask)

Robust fetch strategy:
  1) Prefer Coinbase Advanced REST SDK with explicit timeouts
  2) Fallbacks for discovery and quotes use public HTTP with timeouts:
     - Products: Brokerage public endpoint
     - Quotes:   Exchange public order book (unauthenticated) to avoid 401

Notes:
- ALWAYS convert SDK response objects via .to_dict() before dict-style access.
- Normalize fields so downstream code can rely on:
    product_id, base_currency, quote_currency, base_increment, quote_increment,
    price_increment, min_order (best-effort), fx_stablecoin (heuristic if missing)
- Stable-universe inference is *tight*: base must be a stable, and quote must be USD or a stable.
"""

import os
from decimal import Decimal
from typing import Any, Dict, List, Tuple

# Optional public HTTP fallback
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore

# ---- configuration ----

# Default timeouts (seconds). Override via env if desired.
SDK_TIMEOUT = float(os.getenv("CB_SDK_TIMEOUT", "10"))
HTTP_TIMEOUT = float(os.getenv("CB_HTTP_TIMEOUT", "10"))

# Set of stables we care about
_STABLES = {"USD", "USDC", "USDT", "DAI", "PYUSD", "TUSD", "USDD", "USDP"}


# ---- helpers ----

def _to_dict(obj: Any) -> Any:
    """Convert SDK response objects to dict when possible."""
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


def _dec(x: Any, default: str = "0") -> Decimal:
    if x is None:
        return Decimal(default)
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def _infer_fx_stablecoin(base: str, quote: str) -> bool:
    """
    Tight inference:
    - Base must be a stable, and
    - Quote must be USD or a stable
    This excludes alt/stable crosses like ROSE-USDT.
    """
    return (base in _STABLES) and (quote in _STABLES or quote == "USD")


# ---- public API ----

def get_products() -> List[Dict[str, Any]]:
    """
    Return a normalized list of products with fields our code expects:
      product_id, base_currency, quote_currency,
      base_increment, quote_increment, price_increment,
      min_order (best-effort), fx_stablecoin (bool)

    Tries SDK then public HTTP. All calls use explicit timeouts.
    """
    products: List[Dict[str, Any]] = []

    # 1) Try SDK (no auth required for public methods, but allow creds if present)
    try:
        from coinbase.rest import RESTClient  # coinbase-advanced-py
        client = RESTClient(
            api_key=os.getenv("COINBASE_API_KEY"),
            api_secret=os.getenv("COINBASE_API_SECRET"),
            timeout=SDK_TIMEOUT,  # explicit timeout to avoid hangs
        )
        resp = client.get_products()
        data = _to_dict(resp)

        raw_list = (
            data.get("products")
            if isinstance(data, dict)
            else (data or [])
        )

        for p in raw_list:
            p = _to_dict(p)
            pid = p.get("product_id") or p.get("id")
            if not pid:
                continue
            base = p.get("base_currency_id") or p.get("base_currency") or ""
            quote = p.get("quote_currency_id") or p.get("quote_currency") or ""
            products.append({
                "product_id": pid,
                "base_currency": base,
                "quote_currency": quote,
                "base_increment": _dec(p.get("base_increment"), "0.00000001"),
                "quote_increment": _dec(p.get("quote_increment"), "0.0001"),
                "price_increment": _dec(p.get("price_increment"), "0.0001"),
                "min_order": _dec(
                    p.get("base_min_size") or p.get("min_market_order_size") or p.get("min_order_size"),
                    "0"
                ),
                "fx_stablecoin": bool(p.get("fx_stablecoin", _infer_fx_stablecoin(base, quote))),
            })
        if products:
            return products
    except Exception:
        # Fall through to HTTP
        pass

    # 2) Public HTTP fallback for discovery
    if requests is None:
        return products  # empty fallback

    try:
        url = "https://api.coinbase.com/api/v3/brokerage/products?limit=250"
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        raw_list = data.get("products", [])
        for p in raw_list:
            pid = p.get("product_id")
            if not pid:
                continue
            base = p.get("base_currency_id") or p.get("base_currency") or ""
            quote = p.get("quote_currency_id") or p.get("quote_currency") or ""
            products.append({
                "product_id": pid,
                "base_currency": base,
                "quote_currency": quote,
                "base_increment": _dec(p.get("base_increment"), "0.00000001"),
                "quote_increment": _dec(p.get("quote_increment"), "0.0001"),
                "price_increment": _dec(p.get("price_increment"), "0.0001"),
                "min_order": _dec(
                    p.get("base_min_size") or p.get("min_market_order_size") or p.get("min_order_size"),
                    "0"
                ),
                "fx_stablecoin": bool(p.get("fx_stablecoin", _infer_fx_stablecoin(base, quote))),
            })
    except Exception:
        return products

    return products


def get_best_bid_ask(product_id: str) -> Tuple[Decimal, Decimal]:
    """
    Return (best_bid, best_ask) as Decimals.

    Try SDK first (convert to dict), then public HTTP fallback to Exchange
    (unauthenticated) to avoid 401 on Brokerage endpoints.
    Handles bids/asks like:
      - list of dicts {"price": "...", "size": "..."}
      - list of lists ["123.45","0.10", ...]
    """
    # 1) SDK
    try:
        from coinbase.rest import RESTClient  # coinbase-advanced-py
        client = RESTClient(
            api_key=os.getenv("COINBASE_API_KEY"),
            api_secret=os.getenv("COINBASE_API_SECRET"),
            timeout=SDK_TIMEOUT,  # explicit timeout
        )
        resp = client.get_product_book(product_id=product_id, limit=1)
        data = _to_dict(resp)

        # Some SDKs put bids/asks under "pricebook"
        book = data.get("pricebook") if isinstance(data, dict) else None
        if not book and isinstance(data, dict):
            book = data  # bids/asks might be top-level

        bids = (book or {}).get("bids", []) if isinstance(book, dict) else []
        asks = (book or {}).get("asks", []) if isinstance(book, dict) else []

        def _first_price(side: List[Any]) -> Decimal:
            if not side:
                return Decimal("0")
            first = side[0]
            if isinstance(first, dict):
                return _dec(first.get("price"), "0")
            if isinstance(first, (list, tuple)) and first:
                return _dec(first[0], "0")
            return _dec(first, "0")

        bid = _first_price(bids)
        ask = _first_price(asks)
        if bid <= 0 or ask <= 0:
            raise ValueError(f"Invalid quotes for {product_id}: bid={bid}, ask={ask}")
        return bid, ask
    except Exception:
        pass

    # 2) Public HTTP fallback to Exchange (unauthenticated, reliable)
    if requests is None:
        raise RuntimeError("No SDK and no requests available for get_best_bid_ask().")

    try:
        url = f"https://api.exchange.coinbase.com/products/{product_id}/book?level=1"
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        bids = data.get("bids", []) if isinstance(data, dict) else []
        asks = data.get("asks", []) if isinstance(data, dict) else []

        def _first_price(side: List[Any]) -> Decimal:
            if not side:
                return Decimal("0")
            first = side[0]
            # Exchange book returns list-of-lists: [price, size, num-orders]
            if isinstance(first, (list, tuple)) and first:
                return _dec(first[0], "0")
            if isinstance(first, dict):
                return _dec(first.get("price"), "0")
            return _dec(first, "0")

        bid = _first_price(bids)
        ask = _first_price(asks)
        if bid <= 0 or ask <= 0:
            raise ValueError(f"Invalid quotes for {product_id}: bid={bid}, ask={ask}")
        return bid, ask
    except Exception as e:
        raise RuntimeError(f"Failed to fetch best bid/ask for {product_id}: {e}")
