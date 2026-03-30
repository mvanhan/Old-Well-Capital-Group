#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from decimal import Decimal
from typing import Dict

from broker import coinbase_private as cb_priv  # type: ignore
from broker import coinbase_public as cb_pub  # type: ignore
from owcg_utils.precision import round_price, round_size


def q(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _product_map() -> Dict[str, Dict[str, str]]:
    return {str(p.get("product_id")): p for p in cb_pub.get_products() if p.get("product_id")}


def _product(product_id: str) -> Dict[str, str]:
    product = _product_map().get(product_id)
    if not product:
        raise ValueError(f"Unknown product_id {product_id}")
    return product


def place_manual_order(product_id: str, side: str, usd_notional: Decimal) -> str:
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")

    product = _product(product_id)
    base_inc = q(product.get("base_increment", "0.01"))
    price_inc = q(product.get("price_increment") or product.get("quote_increment") or "0.0001")
    min_size = q(product.get("min_order_size") or product.get("base_min_size") or "0")

    raw_price = cb_pub.get_maker_limit_price(product_id, side)
    entry = round_price(raw_price, price_inc, mode="down" if side == "BUY" else "up")
    size = round_size(usd_notional / entry, base_inc, mode="down")
    if size < min_size:
        raise ValueError(f"Computed size {size} is below min_size {min_size}")

    client_order_id = f"manual-reserve-{int(time.time())}"
    ok, resp = cb_priv.place_limit_order(
        product_id=product_id,
        side=side,
        size=str(size),
        limit_price=str(entry),
        post_only=True,
        client_order_id=client_order_id,
    )
    if not ok:
        raise RuntimeError(resp)

    order_id = resp.get("order_id") or (resp.get("success_response") or {}).get("order_id") or ""
    print(f"[manual-reserve] {side} {product_id} {size}@{entry} order_id={order_id}")
    return str(order_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual reserve adjustment tool.")
    parser.add_argument("--product", default="USDC-USD", help="Coinbase product_id, e.g. USDC-USD")
    parser.add_argument("--side", default="BUY", choices=["BUY", "SELL"], help="Order side")
    parser.add_argument("--usd", default="50", help="Approximate USD notional")
    args = parser.parse_args()

    place_manual_order(product_id=args.product, side=args.side, usd_notional=Decimal(str(args.usd)))


if __name__ == "__main__":
    main()