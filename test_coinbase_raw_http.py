from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import certifi
import requests

try:
    from dotenv import find_dotenv, load_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

from coinbase import jwt_generator  # type: ignore
from broker import coinbase_public as cb_pub  # type: ignore

API_HOST = "api.coinbase.com"
BASE_URL = f"https://{API_HOST}"


def _sanitize_secret(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return raw
    s = raw.strip()
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        s = s[1:-1]
    return s.replace("\\n", "\n")


def _credentials() -> Tuple[str, str]:
    api_key = os.getenv("COINBASE_API_KEY", "").strip()
    api_secret = _sanitize_secret(os.getenv("COINBASE_API_SECRET"))
    if not api_key or not api_secret:
        raise RuntimeError("COINBASE_API_KEY and COINBASE_API_SECRET must be set in .env")
    return api_key, api_secret


def _build_jwt(method: str, path: str) -> str:
    api_key, api_secret = _credentials()
    jwt_uri = jwt_generator.format_jwt_uri(method.upper(), path)
    return jwt_generator.build_rest_jwt(jwt_uri, api_key, api_secret)


def _request(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    full_path = path
    if params:
        filtered = {k: v for k, v in params.items() if v is not None and v != ""}
        if filtered:
            full_path = f"{path}?{urlencode(filtered, doseq=True)}"

    token = _build_jwt(method, full_path)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.request(
            method=method.upper(),
            url=f"{BASE_URL}{full_path}",
            headers=headers,
            json=payload,
            timeout=20,
            verify=certifi.where(),
        )
        try:
            data = response.json()
        except Exception:
            data = response.text
        return response.status_code, data
    except requests.exceptions.SSLError as exc:
        return 0, {"ssl_error": str(exc)}
    except requests.exceptions.RequestException as exc:
        return 0, {"request_error": str(exc)}


def _pretty(label: str, status: int, data: Any) -> None:
    print(f"\n=== {label} ===")
    print(f"status={status}")
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(data)


def _candidate_products() -> List[str]:
    seen = set()
    ordered: List[str] = []

    try:
        for product_id in cb_pub.resolve_trading_products():
            normalized = str(product_id).upper()
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
    except Exception:
        pass

    fallback = [
        "BTC-USD",
        "ETH-USD",
        "BTC-USDC",
        "USDC-USD",
        "USDT-USD",
        "USDT-USDC",
        "DAI-USD",
        "DAI-USDC",
    ]
    for product_id in fallback:
        normalized = product_id.upper()
        if normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)

    return ordered[:10]


def _preview_quote_buy(product_id: str, quote_size: str = "10") -> Tuple[int, Any]:
    return _request(
        "POST",
        "/api/v3/brokerage/orders/preview",
        {
            "product_id": str(product_id).upper(),
            "side": "BUY",
            "order_configuration": {
                "market_market_ioc": {
                    "quote_size": str(quote_size),
                }
            },
        },
    )


def main() -> None:
    fixed_tests = [
        ("GET", "/api/v3/brokerage/key_permissions", None, None, "key_permissions"),
        ("GET", "/api/v3/brokerage/accounts", None, {"limit": 25}, "accounts"),
        ("GET", "/api/v3/brokerage/products", None, {"limit": 25}, "brokerage_products"),
        ("GET", "/api/v3/brokerage/market/products", None, {"limit": 25}, "market_products"),
        ("GET", "/api/v3/brokerage/best_bid_ask", None, {"product_ids": ["BTC-USD"]}, "best_bid_ask_btc_usd"),
    ]

    for method, path, payload, params, label in fixed_tests:
        status, data = _request(method, path, payload, params=params)
        _pretty(label, status, data)

    try:
        resolved = cb_pub.resolve_trading_products()
        print(f"\nresolved_trading_products={resolved}")
    except Exception as exc:
        print(f"\nresolved_trading_products_error={exc}")

    for product_id in _candidate_products():
        status, data = _preview_quote_buy(product_id)
        _pretty(f"preview_buy_{product_id.lower().replace('-', '_')}", status, data)


if __name__ == "__main__":
    main()