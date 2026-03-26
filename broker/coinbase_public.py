from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import find_dotenv, load_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

try:
    from coinbase.rest import RESTClient  # type: ignore
except Exception:
    RESTClient = None  # type: ignore


DEFAULT_EXCLUDED_PRODUCTS = {"USDT-USD", "USDT-USDC"}


def _sanitize_secret(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return raw
    s = raw.strip()
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        s = s[1:-1]
    return s.replace("\\n", "\n")


def _client() -> Optional[RESTClient]:
    if RESTClient is None:
        return None
    key = os.getenv("COINBASE_API_KEY")
    secret = _sanitize_secret(os.getenv("COINBASE_API_SECRET"))
    timeout = float(os.getenv("CB_SDK_TIMEOUT", "10"))
    if key and secret:
        return RESTClient(api_key=key, api_secret=secret, timeout=timeout)
    return RESTClient(timeout=timeout)


def _to_dict(value: Any) -> Any:
    try:
        if hasattr(value, "to_dict"):
            return value.to_dict()
        if hasattr(value, "model_dump"):
            return value.model_dump()
    except Exception:
        pass
    return value


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _normalize_product(product: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(product)
    d["product_id"] = str(d.get("product_id") or "")
    d["base_currency_id"] = d.get("base_currency_id") or d.get("base_currency")
    d["quote_currency_id"] = d.get("quote_currency_id") or d.get("quote_currency")
    d["price_increment"] = d.get("price_increment") or d.get("quote_increment") or "0.0001"
    d["base_increment"] = d.get("base_increment") or "0.01"
    d["min_order_size"] = d.get("min_order_size") or d.get("base_min_size") or "0"
    d["fx_stablecoin"] = _boolish(d.get("fx_stablecoin"))
    d["post_only"] = _boolish(d.get("post_only"))
    d["limit_only"] = _boolish(d.get("limit_only") or d.get("is_limit_only") or d.get("order_book_only"))
    d["cancel_only"] = _boolish(d.get("cancel_only") or d.get("is_cancel_only"))
    d["trading_disabled"] = _boolish(d.get("trading_disabled") or d.get("is_disabled"))
    d["status"] = str(d.get("status") or "")
    return d


def get_products() -> List[Dict[str, Any]]:
    client = _client()
    if client is None:
        return [
            {
                "product_id": "USDC-USD",
                "base_currency_id": "USDC",
                "quote_currency_id": "USD",
                "base_increment": "0.01",
                "quote_increment": "0.0001",
                "price_increment": "0.0001",
                "min_order_size": "1",
                "base_min_size": "1",
                "fx_stablecoin": True,
                "post_only": True,
                "limit_only": False,
                "cancel_only": False,
                "trading_disabled": False,
                "status": "online",
            },
            {
                "product_id": "USDT-USD",
                "base_currency_id": "USDT",
                "quote_currency_id": "USD",
                "base_increment": "0.01",
                "quote_increment": "0.0001",
                "price_increment": "0.0001",
                "min_order_size": "1",
                "base_min_size": "1",
                "fx_stablecoin": True,
                "post_only": True,
                "limit_only": False,
                "cancel_only": False,
                "trading_disabled": False,
                "status": "online",
            },
        ]

    try:
        resp = client.get_public_products()
    except Exception:
        resp = client.get("/api/v3/brokerage/products")
    data = _to_dict(resp)
    products = data.get("products") if isinstance(data, dict) else data
    return [_normalize_product(p) for p in (products or [])]


def get_product(product_id: str) -> Optional[Dict[str, Any]]:
    product_id = product_id.upper()
    for product in get_products():
        if str(product.get("product_id", "")).upper() == product_id:
            return product
    return None


def get_fee_eligible_stable_products() -> List[Dict[str, Any]]:
    raw_excluded = os.getenv("STABLES_EXCLUDED_PRODUCTS", "")
    excluded = {
        item.strip().upper()
        for item in raw_excluded.split(",")
        if item.strip()
    }
    if not excluded:
        excluded = set(DEFAULT_EXCLUDED_PRODUCTS)

    out: List[Dict[str, Any]] = []
    for product in get_products():
        product_id = str(product.get("product_id") or "").upper()
        quote = str(product.get("quote_currency_id") or "").upper()
        status = str(product.get("status") or "").lower()
        if not product.get("fx_stablecoin"):
            continue
        if product_id in excluded:
            continue
        if quote != "USD":
            continue
        if product.get("trading_disabled") or product.get("cancel_only"):
            continue
        if status and status not in {"online", "active", "internal"}:
            continue
        out.append(product)
    return out


def resolve_trading_products() -> List[str]:
    explicit = [p.strip().upper() for p in os.getenv("STABLES_PRODUCTS", "").split(",") if p.strip()]
    auto = os.getenv("STABLES_AUTO_DISCOVER", "1").strip().lower() in {"1", "true", "yes"}
    if explicit:
        return explicit
    if auto:
        return [str(p["product_id"]).upper() for p in get_fee_eligible_stable_products()]
    return ["USDC-USD"]


def resolve_reserve_products() -> List[str]:
    explicit = [p.strip().upper() for p in os.getenv("RESERVE_PRODUCTS", "").split(",") if p.strip()]
    if explicit:
        return explicit
    return resolve_trading_products()


def get_best_bid_ask(product_id: str) -> Tuple[Decimal, Decimal]:
    client = _client()
    if client is None:
        return Decimal("0.9999"), Decimal("1.0001")

    try:
        resp = client.get(f"/api/v3/brokerage/products/{product_id}/ticker")
        data = _to_dict(resp)
        if isinstance(data, dict):
            bid = data.get("bid")
            ask = data.get("ask")
            if bid is not None and ask is not None:
                return Decimal(str(bid)), Decimal(str(ask))
    except Exception:
        pass

    try:
        book = client.get_public_product_book(product_id=product_id, limit=1)
        data = _to_dict(book)
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        bid = Decimal(str(bids[0][0])) if bids else Decimal("0")
        ask = Decimal(str(asks[0][0])) if asks else Decimal("0")
        return bid, ask
    except Exception:
        return Decimal("0"), Decimal("0")


def get_l2(product_id: str, depth: int = 5) -> Dict[str, List[List[str]]]:
    client = _client()
    if client is None:
        return {"bids": [["0.9999", "10000"]], "asks": [["1.0001", "10000"]]}
    resp = client.get_public_product_book(product_id=product_id, limit=depth)
    data = _to_dict(resp)
    return data if isinstance(data, dict) else {"bids": [], "asks": []}


def get_accounts() -> List[Dict[str, Any]]:
    client = _client()
    if client is None:
        return [
            {"currency": "USD", "available": "1000"},
            {"currency": "USDC", "available": "1000"},
        ]
    try:
        resp = client.get("/api/v3/brokerage/accounts")
        data = _to_dict(resp)
        if isinstance(data, dict):
            return data.get("accounts", [])
        return data if isinstance(data, list) else []
    except Exception:
        return []