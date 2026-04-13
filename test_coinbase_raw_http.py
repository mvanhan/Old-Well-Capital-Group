from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

import certifi
import requests

try:
    from dotenv import find_dotenv, load_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

from coinbase import jwt_generator  # type: ignore


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


def _request(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    token = _build_jwt(method, path)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.request(
            method=method.upper(),
            url=f"{BASE_URL}{path}",
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


def main() -> None:
    tests = [
        ("GET", "/api/v3/brokerage/key_permissions", None, "key_permissions"),
        ("GET", "/api/v3/brokerage/accounts", None, "accounts"),
        ("GET", "/api/v3/brokerage/products", None, "brokerage_products"),
        ("GET", "/api/v3/brokerage/market/products", None, "market_products"),
        (
            "POST",
            "/api/v3/brokerage/orders/preview",
            {
                "product_id": "BTC-USD",
                "side": "BUY",
                "order_configuration": {
                    "market_market_ioc": {
                        "quote_size": "10"
                    }
                },
            },
            "preview_btc_usd",
        ),
        (
            "POST",
            "/api/v3/brokerage/orders/preview",
            {
                "product_id": "USDT-USD",
                "side": "BUY",
                "order_configuration": {
                    "market_market_ioc": {
                        "quote_size": "10"
                    }
                },
            },
            "preview_usdt_usd",
        ),
    ]

    for method, path, payload, label in tests:
        status, data = _request(method, path, payload)
        _pretty(label, status, data)


if __name__ == "__main__":
    main()