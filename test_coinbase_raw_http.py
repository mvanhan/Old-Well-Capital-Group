from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from broker import coinbase_http as cb_http  # type: ignore
from broker import coinbase_public as cb_pub  # type: ignore


def _request(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    auth: bool = False,
) -> Tuple[int, Any]:
    return cb_http.request(method, path, params=params, payload=payload, auth=auth)


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
    except Exception as exc:
        print(f"\nresolved_trading_products_error={exc}")

    fallback = [
        "BTC-USD",
        "ETH-USD",
        "BTC-USDC",
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
        auth=True,
    )


def main() -> None:
    fixed_tests = [
        ("GET", "/api/v3/brokerage/key_permissions", None, None, "key_permissions", True),
        ("GET", "/api/v3/brokerage/accounts", None, {"limit": 25}, "accounts", True),
        ("GET", "/api/v3/brokerage/products", None, {"limit": 25}, "brokerage_products", True),
        ("GET", "/api/v3/brokerage/market/products", None, {"limit": 25}, "market_products", False),
        ("GET", "/api/v3/brokerage/best_bid_ask", None, {"product_ids": ["BTC-USD"]}, "best_bid_ask_btc_usd", True),
    ]

    for method, path, payload, params, label, auth in fixed_tests:
        status, data = _request(method, path, payload, params=params, auth=auth)
        _pretty(label, status, data)

    for product_id in _candidate_products():
        status, data = _preview_quote_buy(product_id)
        _pretty(f"preview_buy_{product_id.lower().replace('-', '_')}", status, data)


if __name__ == "__main__":
    main()