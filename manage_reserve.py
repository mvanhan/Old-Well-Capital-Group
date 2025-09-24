#!/usr/bin/env python3
"""
manage_reserve.py

Keeps enough USDT and USDC "float" for your slim-spread MR bot.
- Unit = % of bankroll (clamped by min/max)
- Target N units per leg (USDT & USDC)
- Places maker LIMITs to top up (USD pairs preferred, else convert via USDT-USDC)
- Prints RAW Coinbase response when an order_id isn't returned, and retries once.

Usage (PowerShell):
  # dry-run
  python -m dotenv run -- python manage_reserve.py --units-per-leg 3 --unit-bps 25

  # live (places maker orders)
  python -m dotenv run -- python manage_reserve.py --units-per-leg 3 --unit-bps 25 --live
"""

from __future__ import annotations
import argparse
from decimal import Decimal
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

# Auto-load .env for Windows convenience
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"), override=True)
except Exception:
    pass

from broker import coinbase_private as cb_priv
from broker import coinbase_public as cb_pub
from owcg_utils.precision import q, round_price, round_size

CURRENCIES = ("USD", "USDC", "USDT")
PAIR_USDC_USD = "USDC-USD"
PAIR_USDT_USD = "USDT-USD"
PAIR_USDT_USDC = "USDT-USDC"

def _get_balances() -> Dict[str, Decimal]:
    out = {}
    for c in CURRENCIES:
        try:
            out[c] = cb_priv.get_available(c)
        except Exception:
            out[c] = Decimal("0")
    return out

def _get_products_by_id() -> Dict[str, Dict[str, Any]]:
    prods = cb_pub.get_products()
    return {p.get("product_id"): p for p in prods}

def _price_bid_ask(product_id: str) -> Tuple[Decimal, Decimal]:
    bid, ask = cb_pub.get_best_bid_ask(product_id)
    return q(bid), q(ask)

def _specs(product_id: str, products: Dict[str, Dict[str, Any]]) -> Tuple[Decimal, Decimal, Decimal]:
    p = products.get(product_id) or {}
    base_inc  = q(p.get("base_increment", "0.00000001"))
    quote_inc = q(p.get("quote_increment", "0.00000001"))
    min_size  = q(p.get("min_order_size", p.get("base_min_size", p.get("min_order", "0")) or "0"))
    return base_inc, quote_inc, min_size

def _extract_oid(resp: Any) -> Tuple[Optional[str], Dict[str, Any]]:
    d = resp if isinstance(resp, dict) else (resp.to_dict() if hasattr(resp, "to_dict") else {})
    oid = d.get("order_id") or (d.get("order") or {}).get("order_id")
    return oid, d

def _limit_maker_once(product_id: str, side: str, base_size: Decimal, limit_price: Decimal,
                      products: Dict[str, Dict[str, Any]], live: bool, tag: str) -> Optional[str]:
    """
    One attempt to place a maker-only LIMIT. Returns order_id or None.
    Prints RAW response on live attempt if no oid.
    """
    base_inc, quote_inc, min_size = _specs(product_id, products)
    size = round_size(base_size, base_inc, mode="down")
    if size < min_size:
        raise ValueError(f"{product_id} size {size} < min_size {min_size}")

    bid, ask = _price_bid_ask(product_id)
    px = round_price(limit_price, quote_inc, mode="nearest")
    if side.upper() == "BUY" and px > bid:
        px = round_price(bid, quote_inc, mode="down")
    if side.upper() == "SELL" and px < ask:
        px = round_price(ask, quote_inc, mode="up")

    if not live:
        print(f"[plan] {product_id} {side.upper()} size={size} price={px} (maker)")
        return None

    resp = cb_priv.add_order_limit_only(
        product_id=product_id,
        side=side.upper(),
        base_size=f"{size:f}",
        limit_price=f"{px:f}",
        post_only=True,
        client_order_id=f"reserve:{tag}",
    )
    oid, raw = _extract_oid(resp)
    if oid:
        print(f"[live]  {product_id} {side.upper()} size={size} price={px} -> oid={oid}")
    else:
        print(f"[live][RAW_RESPONSE] {product_id} {side.upper()} size={size} price={px} -> {raw}")
    return oid

def _limit_maker_with_retry(product_id: str, side: str, base_size: Decimal,
                            products: Dict[str, Dict[str, Any]], live: bool, tag: str) -> Optional[str]:
    """
    Try once at the current best maker price; if no oid, refresh bid/ask and retry.
    """
    bid, ask = _price_bid_ask(product_id)
    first_px = bid if side.upper() == "BUY" else ask
    oid = _limit_maker_once(product_id, side, base_size, first_px, products, live, tag)
    if oid or not live:
        return oid

    # Retry with fresh book (book may have moved; choose maker-safe again)
    bid2, ask2 = _price_bid_ask(product_id)
    retry_px = bid2 if side.upper() == "BUY" else ask2
    print(f"[retry] {product_id} {side.upper()} at refreshed book (bid={bid2}, ask={ask2})")
    return _limit_maker_once(product_id, side, base_size, retry_px, products, live, tag+"|retry")

def compute_bankroll(bal: Dict[str, Decimal]) -> Decimal:
    # Treat USD, USDC, USDT all ≈ $1
    return bal.get("USD", Decimal(0)) + bal.get("USDC", Decimal(0)) + bal.get("USDT", Decimal(0))

def pick_unit(bankroll: Decimal, unit_bps: int, min_unit: Decimal, max_unit: Decimal) -> Decimal:
    # unit = bankroll * (bps/10_000), clamped and rounded to cents
    raw = bankroll * Decimal(unit_bps) / Decimal(10_000)
    unit = max(min_unit, min(max_unit, raw))
    return (unit.quantize(Decimal("0.01")))

def plan_reserves(
    *,
    units_per_leg: int,
    unit_bps: int,
    min_unit: Decimal,
    max_unit: Decimal,
) -> Dict[str, Any]:
    bal = _get_balances()
    bankroll = compute_bankroll(bal)
    unit = pick_unit(bankroll, unit_bps, min_unit, max_unit)

    # Target per leg (USDT and USDC). Reserve target is N * unit for each.
    target_usdt = unit * units_per_leg
    target_usdc = unit * units_per_leg

    have_usdt = bal["USDT"]
    have_usdc = bal["USDC"]

    need_usdt = max(Decimal("0"), target_usdt - have_usdt)
    need_usdc = max(Decimal("0"), target_usdc - have_usdc)

    return {
        "balances": bal,
        "bankroll": bankroll,
        "unit": unit,
        "targets": {"USDT": target_usdt, "USDC": target_usdc},
        "deficits": {"USDT": need_usdt, "USDC": need_usdc},
    }

def execute_plan(plan: Dict[str, Any], *, max_spread_bps: int, live: bool) -> None:
    products = _get_products_by_id()

    def spread_ok(product_id: str) -> bool:
        bid, ask = _price_bid_ask(product_id)
        mid = (bid + ask) / 2 if (bid and ask) else bid or ask
        if not (bid and ask and mid):
            return True
        spr_bps = (ask - bid) / mid * Decimal(10_000)
        return spr_bps <= Decimal(max_spread_bps)

    bal = plan["balances"]
    need_usdt = plan["deficits"]["USDT"]
    need_usdc = plan["deficits"]["USDC"]

    # Acquire USDC first
    if need_usdc > 0:
        if PAIR_USDC_USD in products and bal["USD"] > 0:
            if spread_ok(PAIR_USDC_USD):
                size = min(need_usdc, bal["USD"])  # $1-ish parity
                _limit_maker_with_retry(PAIR_USDC_USD, "BUY", size, products, live, tag="USDC-USD:buy")
            else:
                print(f"[skip] {PAIR_USDC_USD} spread too wide for reserve buy")
        elif bal["USDT"] > 0 and PAIR_USDT_USDC in products:
            # Convert USDT -> USDC by SELLING USDT-USDC
            if spread_ok(PAIR_USDT_USDC):
                size = min(need_usdc, bal["USDT"])
                _limit_maker_with_retry(PAIR_USDT_USDC, "SELL", size, products, live, tag="USDT-USDC:sell")
            else:
                print(f"[skip] {PAIR_USDT_USDC} spread too wide for USDT->USDC")
        else:
            print("[warn] Cannot source USDC (no USD or USDT available).")

    # Acquire USDT
    if need_usdt > 0:
        if PAIR_USDT_USD in products and bal["USD"] > 0:
            if spread_ok(PAIR_USDT_USD):
                size = min(need_usdt, bal["USD"])
                _limit_maker_with_retry(PAIR_USDT_USD, "BUY", size, products, live, tag="USDT-USD:buy")
            else:
                print(f"[skip] {PAIR_USDT_USD} spread too wide for reserve buy")
        elif bal["USDC"] > 0 and PAIR_USDT_USDC in products:
            # Convert USDC -> USDT by BUYING USDT-USDC (spend USDC)
            if spread_ok(PAIR_USDT_USDC):
                size = min(need_usdt, bal["USDC"])
                _limit_maker_with_retry(PAIR_USDT_USDC, "BUY", size, products, live, tag="USDT-USDC:buy")
            else:
                print(f"[skip] {PAIR_USDT_USDC} spread too wide for USDC->USDT")
        else:
            print("[warn] Cannot source USDT (no USD or USDC available).")

def main():
    ap = argparse.ArgumentParser(description="Top up stablecoin reserves for slim-spread MR bot.")
    ap.add_argument("--units-per-leg", type=int, default=3, help="Target number of units for each leg (USDT & USDC).")
    ap.add_argument("--unit-bps", type=int, default=25, help="Unit size in bps of bankroll (25 = 0.25%).")
    ap.add_argument("--min-unit", type=Decimal, default=Decimal("5"), help="Minimum $ per unit.")
    ap.add_argument("--max-unit", type=Decimal, default=Decimal("50"), help="Maximum $ per unit.")
    ap.add_argument("--max-spread-bps", type=int, default=4, help="Don’t place reserve orders if spread > this.")
    ap.add_argument("--live", action="store_true", help="Actually place orders (maker limit).")
    args = ap.parse_args()

    plan = plan_reserves(
        units_per_leg=args.units_per_leg,
        unit_bps=args.unit_bps,
        min_unit=args.min_unit,
        max_unit=args.max_unit,
    )

    print("=== RESERVE PLAN ===")
    print(f"bankroll: ${plan['bankroll']:.2f}")
    print(f"unit({args.unit_bps} bps): ${plan['unit']:.2f}")
    print(f"targets:  USDT={plan['targets']['USDT']:.2f}, USDC={plan['targets']['USDC']:.2f}")
    print(f"balances: USD={plan['balances']['USD']}, USDT={plan['balances']['USDT']}, USDC={plan['balances']['USDC']}")
    print(f"deficits: USDT={plan['deficits']['USDT']:.2f}, USDC={plan['deficits']['USDC']:.2f}")
    print(f"mode: {'LIVE' if args.live else 'DRY-RUN'} (maker only)")

    execute_plan(plan, max_spread_bps=args.max_spread_bps, live=args.live)

if __name__ == "__main__":
    main()
