from __future__ import annotations

from broker import coinbase_private as cb_priv  # type: ignore
from broker import coinbase_public as cb_pub  # type: ignore


def main() -> None:
    products = cb_pub.get_products()
    if not products:
        raise RuntimeError("No products returned from Coinbase")

    eligible = cb_pub.get_fee_eligible_stable_products()
    balances = cb_priv.get_balances()
    if not balances:
        raise RuntimeError("No balances returned from Coinbase")

    print(f"products={len(products)}")
    print("eligible_stable_products=")
    for product in eligible[:20]:
        print(f"  {product['product_id']}")

    first = eligible[0]["product_id"] if eligible else products[0]["product_id"]
    bid, ask = cb_pub.get_best_bid_ask(first)
    print(f"sample_product={first}")
    print(f"best_bid={bid} best_ask={ask}")
    print(f"balances_returned={len(balances)}")
    print("OK")


if __name__ == "__main__":
    main()