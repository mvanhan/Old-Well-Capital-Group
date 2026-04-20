from __future__ import annotations

from typing import Any, Dict, List

from broker import coinbase_private as cb_priv  # type: ignore
from broker import coinbase_public as cb_pub  # type: ignore


def _preview_quote_buy(product_id: str, quote_size: str = "10") -> Dict[str, Any]:
    return cb_priv._request_post(  # type: ignore
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
    ]
    for product_id in fallback:
        normalized = product_id.upper()
        if normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)

    return ordered[:8]


def main() -> None:
    balances = cb_priv.get_balances()
    print(f"balances_returned={len(balances)}")
    if balances:
        sample = balances[:5]
        print("balance_sample=", sample)

    key_permissions = cb_priv._request_get("/api/v3/brokerage/key_permissions")  # type: ignore
    print("key_permissions=", key_permissions)

    market_products = cb_pub.get_market_products()
    tradable_products = cb_pub.get_tradable_products()
    print(f"market_products_returned={len(market_products)}")
    print(f"tradable_products_returned={len(tradable_products)}")

    try:
        resolved = cb_pub.resolve_trading_products()
        print(f"resolved_trading_products={resolved}")
    except Exception as exc:
        print(f"resolved_trading_products_error={exc}")

    for product_id in _candidate_products():
        try:
            preview = _preview_quote_buy(product_id)
            print(f"{product_id}: OK -> {preview}")
        except Exception as exc:
            print(f"{product_id}: FAIL -> {exc}")


if __name__ == "__main__":
    main()