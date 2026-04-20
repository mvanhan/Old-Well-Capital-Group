from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode

try:
    from dotenv import find_dotenv, load_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

try:
    from coinbase.rest import RESTClient  # type: ignore
except Exception:
    RESTClient = None  # type: ignore


DEFAULT_EXCLUDED_PRODUCTS: Set[str] = set()
DEFAULT_REFERENCE_ASSETS: Set[str] = {
    "USD",
    "USDC",
    "USDT",
    "DAI",
    "PYUSD",
    "FDUSD",
    "USDP",
    "GUSD",
    "TUSD",
    "RLUSD",
    "EURC",
}
ACTIVE_STATUSES = {"online", "active", "internal"}


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
    timeout = float(os.getenv("CB_SDK_TIMEOUT", "10"))
    key = os.getenv("COINBASE_API_KEY")
    secret = _sanitize_secret(os.getenv("COINBASE_API_SECRET"))
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


def _split_csv_env(name: str) -> List[str]:
    raw = os.getenv(name, "")
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def _request_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    client = _client()
    if client is None:
        raise RuntimeError("coinbase-advanced-py not installed")
    full_path = path
    if params:
        filtered = {k: v for k, v in params.items() if v is not None and v != ""}
        if filtered:
            full_path = f"{path}?{urlencode(filtered, doseq=True)}"
    return _to_dict(client.get(full_path))


def _normalize_product(product: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(product)
    d["product_id"] = str(d.get("product_id") or "").upper()
    d["base_currency_id"] = str(d.get("base_currency_id") or d.get("base_currency") or "").upper()
    d["quote_currency_id"] = str(d.get("quote_currency_id") or d.get("quote_currency") or "").upper()
    d["price_increment"] = str(d.get("price_increment") or d.get("quote_increment") or "0.0001")
    d["base_increment"] = str(d.get("base_increment") or "0.01")
    d["min_order_size"] = str(d.get("min_order_size") or d.get("base_min_size") or "0")
    d["fx_stablecoin"] = _boolish(d.get("fx_stablecoin"))
    d["post_only"] = _boolish(d.get("post_only"))
    d["limit_only"] = _boolish(d.get("limit_only") or d.get("is_limit_only") or d.get("order_book_only"))
    d["cancel_only"] = _boolish(d.get("cancel_only") or d.get("is_cancel_only"))
    d["trading_disabled"] = _boolish(d.get("trading_disabled") or d.get("is_disabled") or d.get("view_only"))
    d["auction_mode"] = _boolish(d.get("auction_mode") or d.get("auction") or d.get("is_auction_mode"))
    d["status"] = str(d.get("status") or "").lower()
    return d


def _offline_products() -> List[Dict[str, Any]]:
    return [
        _normalize_product(
            {
                "product_id": "USDC-USD",
                "base_currency_id": "USDC",
                "quote_currency_id": "USD",
                "base_increment": "0.01",
                "price_increment": "0.0001",
                "min_order_size": "1",
                "fx_stablecoin": True,
                "post_only": True,
                "limit_only": False,
                "cancel_only": False,
                "trading_disabled": False,
                "status": "online",
            }
        ),
        _normalize_product(
            {
                "product_id": "USDT-USDC",
                "base_currency_id": "USDT",
                "quote_currency_id": "USDC",
                "base_increment": "0.01",
                "price_increment": "0.0001",
                "min_order_size": "1",
                "fx_stablecoin": True,
                "post_only": True,
                "limit_only": False,
                "cancel_only": False,
                "trading_disabled": False,
                "status": "online",
            }
        ),
    ]


def _collect_products(path: str) -> List[Dict[str, Any]]:
    seen: Set[str] = set()
    cursor: Optional[str] = None
    out: List[Dict[str, Any]] = []

    while True:
        params: Dict[str, Any] = {"limit": 250}
        if cursor:
            params["cursor"] = cursor
        data = _request_get(path, params)
        products = data.get("products") if isinstance(data, dict) else None
        batch = products if isinstance(products, list) else []
        for raw in batch:
            normalized = _normalize_product(raw)
            product_id = normalized.get("product_id")
            if product_id and product_id not in seen:
                seen.add(product_id)
                out.append(normalized)

        pagination = data.get("pagination") if isinstance(data, dict) else None
        next_cursor = pagination.get("next_cursor") if isinstance(pagination, dict) else None
        if not next_cursor:
            break
        cursor = str(next_cursor)

    return out


def get_market_products() -> List[Dict[str, Any]]:
    client = _client()
    if client is None:
        return _offline_products()
    for path in ("/api/v3/brokerage/market/products", "/api/v3/brokerage/products"):
        try:
            products = _collect_products(path)
            if products:
                return products
        except Exception:
            continue
    return []


def get_tradable_products() -> List[Dict[str, Any]]:
    client = _client()
    if client is None:
        return _offline_products()
    try:
        return _collect_products("/api/v3/brokerage/products")
    except Exception:
        return []


def get_products() -> List[Dict[str, Any]]:
    return get_market_products()


def get_product(product_id: str) -> Optional[Dict[str, Any]]:
    normalized = str(product_id).upper()

    for product in get_tradable_products():
        if product.get("product_id") == normalized:
            return product

    for path in (
        f"/api/v3/brokerage/products/{normalized}",
        f"/api/v3/brokerage/market/products/{normalized}",
    ):
        try:
            data = _request_get(path)
            if isinstance(data, dict) and data.get("product_id"):
                return _normalize_product(data)
        except Exception:
            continue

    for product in get_market_products():
        if product.get("product_id") == normalized:
            return product

    return None


def _status_allows_trading(product: Dict[str, Any]) -> bool:
    status = str(product.get("status") or "").lower()
    if product.get("trading_disabled") or product.get("cancel_only") or product.get("auction_mode"):
        return False
    if status and status not in ACTIVE_STATUSES:
        return False
    return True


def _reference_assets(products: List[Dict[str, Any]]) -> Set[str]:
    assets = set(DEFAULT_REFERENCE_ASSETS)
    assets.update(_split_csv_env("STABLES_REFERENCE_ASSETS"))
    for product in products:
        base = str(product.get("base_currency_id") or "").upper()
        quote = str(product.get("quote_currency_id") or "").upper()
        if product.get("fx_stablecoin"):
            if base:
                assets.add(base)
            if quote:
                assets.add(quote)
    return {asset for asset in assets if asset}


def _allowed_quote_assets(reference_assets: Set[str]) -> Set[str]:
    configured = set(_split_csv_env("STABLES_ALLOWED_QUOTES"))
    if configured:
        return configured
    return set(reference_assets)


def _product_sort_key(product: Dict[str, Any], allowed_quotes: Set[str]) -> Tuple[int, int, int, str, str]:
    quote = str(product.get("quote_currency_id") or "").upper()
    product_id = str(product.get("product_id") or "").upper()
    quote_priority = {
        "USD": 0,
        "USDC": 1,
        "USDT": 2,
        "DAI": 3,
        "PYUSD": 4,
    }.get(quote, 50 if quote in allowed_quotes else 100)
    post_only_priority = 0 if product.get("post_only") else 1
    limit_only_priority = 1 if product.get("limit_only") else 0
    return (quote_priority, post_only_priority, limit_only_priority, quote, product_id)


def get_fee_eligible_stable_products() -> List[Dict[str, Any]]:
    products = get_tradable_products()
    if not products:
        return []

    excluded = set(_split_csv_env("STABLES_EXCLUDED_PRODUCTS"))
    if not excluded:
        excluded = set(DEFAULT_EXCLUDED_PRODUCTS)

    reference_assets = _reference_assets(products)
    allowed_quotes = _allowed_quote_assets(reference_assets)
    require_post_only = _boolish(os.getenv("STABLES_REQUIRE_POST_ONLY", "0"))
    include_limit_only = _boolish(os.getenv("STABLES_INCLUDE_LIMIT_ONLY", "0"))
    require_flag = _boolish(os.getenv("STABLES_REQUIRE_FX_STABLECOIN_FLAG", "0"))

    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    for product in products:
        product_id = str(product.get("product_id") or "").upper()
        base = str(product.get("base_currency_id") or "").upper()
        quote = str(product.get("quote_currency_id") or "").upper()

        if not product_id or product_id in seen or product_id in excluded:
            continue
        if not base or not quote or base == quote:
            continue
        if not _status_allows_trading(product):
            continue
        if require_post_only and not product.get("post_only"):
            continue
        if not include_limit_only and product.get("limit_only"):
            continue
        if quote not in allowed_quotes:
            continue
        if base not in reference_assets or quote not in reference_assets:
            continue
        if require_flag and not product.get("fx_stablecoin"):
            continue

        seen.add(product_id)
        out.append(product)

    out.sort(key=lambda product: _product_sort_key(product, allowed_quotes))
    return out


def _validate_products(products: List[str], label: str, tradable_only: bool = True) -> List[str]:
    source = get_tradable_products() if tradable_only else get_market_products()
    live_products = {str(p.get("product_id") or "").upper() for p in source}
    missing = [p for p in products if p not in live_products]
    if missing:
        universe = "tradable brokerage product list" if tradable_only else "market product list"
        raise RuntimeError(f"Configured {label} not found in Coinbase {universe}: {', '.join(missing)}")
    return products


def resolve_trading_products() -> List[str]:
    explicit = [p.strip().upper() for p in os.getenv("STABLES_PRODUCTS", "").split(",") if p.strip()]
    auto = _boolish(os.getenv("STABLES_AUTO_DISCOVER", "0"))

    if explicit:
        return _validate_products(explicit, "STABLES_PRODUCTS", tradable_only=True)

    if auto:
        products = [str(p["product_id"]).upper() for p in get_fee_eligible_stable_products()]
        if not products:
            raise RuntimeError(
                "No eligible stable products found. Check Coinbase product availability, STABLES_EXCLUDED_PRODUCTS, "
                "STABLES_ALLOWED_QUOTES, STABLES_REFERENCE_ASSETS, and STABLES_INCLUDE_LIMIT_ONLY."
            )
        return products

    return []


def resolve_reserve_products() -> List[str]:
    explicit = [p.strip().upper() for p in os.getenv("RESERVE_PRODUCTS", "").split(",") if p.strip()]
    if explicit:
        return _validate_products(explicit, "RESERVE_PRODUCTS", tradable_only=True)
    return resolve_trading_products()


def _extract_price_size_rows(rows: Any) -> List[List[str]]:
    out: List[List[str]] = []
    for row in rows or []:
        if isinstance(row, dict):
            price = row.get("price")
            size = row.get("size") or row.get("qty") or row.get("quantity")
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price, size = row[0], row[1]
        else:
            continue
        if price is None or size is None:
            continue
        out.append([str(price), str(size)])
    return out


def get_best_bid_ask(product_id: str) -> Tuple[Decimal, Decimal]:
    client = _client()
    if client is None:
        return Decimal("0.9999"), Decimal("1.0001")

    normalized = str(product_id).upper()

    for path, params in (
        (f"/api/v3/brokerage/market/products/{normalized}/ticker", {"limit": 1}),
        ("/api/v3/brokerage/best_bid_ask", {"product_ids": normalized}),
        (f"/api/v3/brokerage/products/{normalized}/ticker", None),
    ):
        try:
            data = _request_get(path, params)
            if path.endswith("/ticker"):
                bid = data.get("best_bid") or data.get("bid")
                ask = data.get("best_ask") or data.get("ask")
                if bid is not None and ask is not None:
                    return Decimal(str(bid)), Decimal(str(ask))
            else:
                books = data.get("pricebooks") if isinstance(data, dict) else []
                if books:
                    pricebook = books[0]
                    bids = _extract_price_size_rows(pricebook.get("bids"))
                    asks = _extract_price_size_rows(pricebook.get("asks"))
                    if bids and asks:
                        return Decimal(bids[0][0]), Decimal(asks[0][0])
        except Exception:
            continue

    try:
        book = get_l2(normalized, depth=1)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if bids and asks:
            return Decimal(bids[0][0]), Decimal(asks[0][0])
    except Exception:
        pass

    return Decimal("0"), Decimal("0")


def get_l2(product_id: str, depth: int = 5) -> Dict[str, List[List[str]]]:
    client = _client()
    if client is None:
        return {"bids": [["0.9999", "10000"]], "asks": [["1.0001", "10000"]]}

    normalized = str(product_id).upper()
    attempts = [
        ("/api/v3/brokerage/market/product_book", {"product_id": normalized, "limit": depth}),
        ("/api/v3/brokerage/product_book", {"product_id": normalized, "limit": depth}),
    ]
    for path, params in attempts:
        try:
            data = _request_get(path, params)
            pricebook = data.get("pricebook") if isinstance(data, dict) else None
            source = pricebook if isinstance(pricebook, dict) else data
            if isinstance(source, dict):
                bids = _extract_price_size_rows(source.get("bids"))
                asks = _extract_price_size_rows(source.get("asks"))
                if bids or asks:
                    return {"bids": bids, "asks": asks}
        except Exception:
            continue
    return {"bids": [], "asks": []}


def get_accounts() -> List[Dict[str, Any]]:
    client = _client()
    if client is None:
        return [{"currency": "USD", "available": "1000"}, {"currency": "USDC", "available": "1000"}]
    try:
        data = _request_get("/api/v3/brokerage/accounts")
        if isinstance(data, dict):
            return data.get("accounts", [])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_maker_limit_price(product_id: str, side: str) -> Decimal:
    bid, ask = get_best_bid_ask(product_id)
    if bid <= 0 or ask <= 0:
        raise RuntimeError(f"Bad quote for {product_id}: bid={bid} ask={ask}")
    side = str(side).upper()
    if side == "BUY":
        return bid
    if side == "SELL":
        return ask
    raise ValueError("side must be BUY or SELL")


def get_marketable_limit_price(product_id: str, side: str, buffer_bps: Decimal | str | float = Decimal("1.0")) -> Decimal:
    bid, ask = get_best_bid_ask(product_id)
    if bid <= 0 or ask <= 0:
        raise RuntimeError(f"Bad quote for {product_id}: bid={bid} ask={ask}")
    buf = Decimal(str(buffer_bps)) / Decimal("10000")
    side = str(side).upper()
    if side == "BUY":
        return ask * (Decimal("1") + buf)
    if side == "SELL":
        return bid * (Decimal("1") - buf)
    raise ValueError("side must be BUY or SELL")