#!/usr/bin/env python3
"""
Manual reserve manager (you can still run this on demand).
The controller already calls equivalent routines automatically when thresholds are breached.
"""
from __future__ import annotations
import os, argparse, time
from decimal import Decimal
from typing import Dict, Any, Tuple

from broker import coinbase_public as cb_pub  # type: ignore
from broker import coinbase_private as cb_priv  # type: ignore
from owcg_utils.precision import round_price, round_size

def q(x) -> Decimal: return x if isinstance(x, Decimal) else Decimal(str(x))

def _balances() -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for b in cb_priv.get_balances():
        sym = b.get("currency") or b.get("asset") or b.get("symbol")
        val = b.get("available") or b.get("available_balance") or b.get("available_for_trading")
        if isinstance(val, dict): val = val.get("value")
        if sym and val is not None:
            out[str(sym)] = q(val)
    return out

def buy_stable(product_id: str, usd_notional: Decimal) -> None:
    # Place maker BUY near bid
    prods = cb_pub.get_products()
    p = next((pp for pp in prods if pp.get("product_id")==product_id), None)
    if not p: raise RuntimeError(f"unknown {product_id}")
    base_inc  = q(p.get("base_increment","0.01"))
    price_inc = q(p.get("price_increment", p.get("quote_increment","0.0001")))
    bid, ask = cb_pub.get_best_bid_ask(product_id)
    price = round_price(bid, price_inc)
    size  = round_size(usd_notional / price, base_inc)
    ok, resp = cb_priv.place_limit_order(product_id, side="BUY", size=str(size), limit_price=str(price), post_only=True, client_order_id=f"manual-reserve-{int(time.time())}")
    if not ok:
        raise RuntimeError(resp)
    print(f"[reserve] BUY {product_id} {size}@{price} (notional ~${(size*price):.2f}) id={resp.get('order_id')}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", default="USDT-USD", help="USDT-USD or USDC-USD")
    ap.add_argument("--usd", default="50", help="USD notional to buy as float/str")
    args = ap.parse_args()
    buy_stable(args.product, Decimal(str(args.usd)))

if __name__ == "__main__":
    main()
