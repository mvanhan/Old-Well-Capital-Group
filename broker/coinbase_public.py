# broker/coinbase_public.py
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Tuple
import os

# --- Load .env early (non-fatal if missing) ---
try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

# Optional requests fallback for public HTTP
try:
    import requests  # type: ignore
except Exception:
    requests = None  # pragma: no cover

try:
    from coinbase.rest import RESTClient
except Exception:
    RESTClient = None  # public HTTP fallback will be used

HTTP_TIMEOUT = float(os.getenv("COINBASE_HTTP_TIMEOUT", "8"))

def _dec(x: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)

def _to_dict(obj: Any) -> Any:
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

def _client() -> RESTClient | None:
    if RESTClient is None:
        return None
    try:
        return RESTClient()
    except Exception:
        return None

def get_products() -> List[Dict[str, Any]]:
    """Return a list of products with normalized fields used by our strategies."""
    # 1) Try SDK
    cl = _client()
    if cl is not None:
        try:
            resp = cl.get("/api/v3/brokerage/products")
            data = _to_dict(resp)
            raw = data.get("products") if isinstance(data, dict) else data
            products: List[Dict[str, Any]] = []
            if isinstance(raw, list):
                for p in raw:
                    p = _to_dict(p)
                    pid = p.get("product_id") or p.get("id")
                    base = p.get("base_currency_id") or p.get("base_currency") or ""
                    quote = p.get("quote_currency_id") or p.get("quote_currency") or ""
                    products.append({
                        "product_id": pid,
                        "base_currency": base,
                        "quote_currency": quote,
                        "base_increment": _dec(p.get("base_increment"), "0.00000001"),
                        "quote_increment": _dec(p.get("quote_increment"), "0.0001"),
                        "price_increment": _dec(p.get("price_increment"), "0.0001"),
                        "min_market_funds": _dec(p.get("min_market_funds"), "0"),
                        "base_min_size": _dec(p.get("base_min_size"), "0"),
                    })
                return products
        except Exception:
            pass

    # 2) Public HTTP fallback
    if requests is None:
        raise RuntimeError("Neither SDK nor requests available for get_products().")
    r = requests.get("https://api.exchange.coinbase.com/products", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    raw = r.json()
    products: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for p in raw:
            pid = p.get("id")
            base = p.get("base_currency") or ""
            quote = p.get("quote_currency") or ""
            products.append({
                "product_id": pid,
                "base_currency": base,
                "quote_currency": quote,
                "base_increment": _dec(p.get("base_increment"), "0.00000001"),
                "quote_increment": _dec(p.get("quote_increment"), "0.0001"),
                "price_increment": _dec(p.get("quote_increment"), "0.0001"),
                "min_market_funds": _dec(p.get("min_market_funds"), "0"),
                "base_min_size": _dec(p.get("base_min_size"), "0"),
            })
    return products

def get_product(product_id: str) -> Dict[str, Any]:
    """Return a single product dict with normalized fields."""
    cl = _client()
    if cl is not None:
        try:
            resp = cl.get(f"/api/v3/brokerage/products/{product_id}")
            p = _to_dict(resp)
            if isinstance(p, dict) and "product" in p:
                p = p["product"]
            p = _to_dict(p)
            base = p.get("base_currency_id") or p.get("base_currency") or ""
            quote = p.get("quote_currency_id") or p.get("quote_currency") or ""
            return {
                "product_id": p.get("product_id") or p.get("id") or product_id,
                "base_currency": base,
                "quote_currency": quote,
                "base_increment": _dec(p.get("base_increment"), "0.00000001"),
                "quote_increment": _dec(p.get("quote_increment"), "0.0001"),
                "price_increment": _dec(p.get("price_increment"), "0.0001"),
                "min_market_funds": _dec(p.get("min_market_funds"), "0"),
                "base_min_size": _dec(p.get("base_min_size"), "0"),
                "min_size": _dec(p.get("min_order_size"), "0"),
            }
        except Exception:
            pass

    for p in get_products():
        if p.get("product_id") == product_id:
            return p
    raise RuntimeError(f"Unknown product_id {product_id}")

def get_best_bid_ask(product_id: str) -> Tuple[Decimal, Decimal]:
    """Return (bid, ask) as Decimals."""
    cl = _client()
    if cl is not None:
        try:
            resp = cl.get(f"/api/v3/brokerage/products/{product_id}/book?level=1")
            d = _to_dict(resp)
            bids = d.get("bids", [])
            asks = d.get("asks", [])
            def _first(x):
                if not x: return Decimal("0")
                f = x[0]
                if isinstance(f, (list, tuple)):
                    return _dec(f[0], "0")
                if isinstance(f, dict):
                    return _dec(f.get("price"), "0")
                return _dec(f, "0")
            bid = _first(bids); ask = _first(asks)
            if bid > 0 and ask > 0:
                return bid, ask
        except Exception:
            pass

    if requests is None:
        raise RuntimeError("No HTTP client available to fetch orderbook.")
    r = requests.get(f"https://api.exchange.coinbase.com/products/{product_id}/book?level=1", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    bids = d.get("bids", []); asks = d.get("asks", [])
    bid = _dec(bids[0][0], "0") if bids and isinstance(bids[0], (list, tuple)) else _dec(0)
    ask = _dec(asks[0][0], "0") if asks and isinstance(asks[0], (list, tuple)) else _dec(0)
    if bid <= 0 or ask <= 0:
        raise RuntimeError(f"Invalid L1 for {product_id}: {bid}/{ask}")
    return bid, ask
