from __future__ import annotations

from broker import coinbase_private as cb_priv  # type: ignore

CANDIDATES = [
    "BTC-USD",
    "ETH-USD",
    "BTC-USDC",
    "USDT-USD",
    "USDT-USDC",
    "USD1-USD",
    "USD1-USDC",
]

def try_preview(product_id: str) -> None:
    try:
        preview = cb_priv._request_post(  # type: ignore
            "/api/v3/brokerage/orders/preview",
            {
                "product_id": product_id,
                "side": "BUY",
                "order_configuration": {
                    "market_market_ioc": {
                        "quote_size": "10"
                    }
                },
            },
        )
        print(f"{product_id}: OK -> {preview}")
    except Exception as exc:
        print(f"{product_id}: FAIL -> {exc}")

def main() -> None:
    balances = cb_priv.get_balances()
    print(f"balances_returned={len(balances)}")

    perms = cb_priv._request_get("/api/v3/brokerage/key_permissions")  # type: ignore
    print("key_permissions=", perms)

    for product_id in CANDIDATES:
        try_preview(product_id)

if __name__ == "__main__":
    main()