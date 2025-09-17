#!/usr/bin/env python3
# fund_stable_inventory.py
from __future__ import annotations

import os
import sys
import uuid
import argparse
from decimal import Decimal, ROUND_DOWN

try:
    from coinbase.rest import RESTClient
except Exception as e:
    print("ERROR: coinbase-advanced-py not installed. Run: pip install coinbase-advanced-py", file=sys.stderr)
    raise

# Optional: reuse your public helper (timeouts, product list)
try:
    from broker import coinbase_public as cb_pub
except Exception:
    cb_pub = None

SDK_TIMEOUT = float(os.getenv("CB_SDK_TIMEOUT", "10"))

def to_dec(x) -> Decimal:
    return Decimal(str(x))

def client() -> RESTClient:
    api_key = os.getenv("COINBASE_API_KEY")
    api_secret = os.getenv("COINBASE_API_SECRET")
    if not api_key or not api_secret:
        print("ERROR: COINBASE_API_KEY / COINBASE_API_SECRET missing from environment.", file=sys.stderr)
        sys.exit(1)
    return RESTClient(api_key=api_key, api_secret=api_secret, timeout=SDK_TIMEOUT)

def usd_available(cl: RESTClient) -> Decimal:
    acc = cl.get_accounts()
    d = acc.to_dict() if hasattr(acc, "to_dict") else acc
    for a in d.get("accounts", []):
        if a.get("currency") == "USD":
            bal = a.get("available_balance") or {}
            return to_dec(bal.get("value", "0"))
    return Decimal("0")

def discover_usd_pairs():
    # Prefer your helper (handles timeouts); otherwise, minimal fallback via SDK
    if cb_pub is not None:
        prods = cb_pub.get_products()
    else:
        c = client()
        prods = c.get_products()
        prods = prods.to_dict()["products"] if hasattr(prods, "to_dict") else prods
    wanted = {}
    for p in prods:
        pid = p.get("product_id") or p.get("id")
        base = p.get("base_currency_id") or p.get("base_currency")
        quote = p.get("quote_currency_id") or p.get("quote_currency")
        if not pid or not base or not quote:
            continue
        if quote == "USD" and base in {"USDT", "DAI"}:
            wanted[base] = pid  # e.g., {'USDT': 'USDT-USD', 'DAI': 'DAI-USD'}
    return wanted

def place_market_buy_quote(cl: RESTClient, product_id: str, quote_usd: Decimal):
    """
    Advanced Trade market IOC with quote_size in USD.
    """
    payload = {
        "client_order_id": f"owcg-fund-{uuid.uuid4().hex[:10]}",
        "product_id": product_id,
        "side": "BUY",
        "order_configuration": {
            "market_market_ioc": {
                "quote_size": str(quote_usd.quantize(Decimal("0.01"), rounding=ROUND_DOWN))  # cents
            }
        },
    }
    resp = cl.post("/api/v3/brokerage/orders", data=payload)
    return resp.to_dict() if hasattr(resp, "to_dict") else resp

def main():
    ap = argparse.ArgumentParser(description="Buy small USD amounts of base stables for inventory.")
    ap.add_argument("--per-asset-usd", type=Decimal, default=Decimal("25"),
                    help="USD to buy per asset (default: 25)")
    ap.add_argument("--assets", nargs="*", default=["USDT", "DAI"],
                    help="Which base stables to buy (default: USDT DAI)")
    ap.add_argument("--dry-run", action="store_true", help="Print what would be bought, don’t place orders")
    args = ap.parse_args()

    cl = client()
    usd_avail = usd_available(cl)
    if usd_avail <= 0:
        print("ERROR: No USD available.", file=sys.stderr)
        sys.exit(2)

    pairs = discover_usd_pairs()
    to_buy = [a for a in args.assets if a in pairs]
    if not to_buy:
        print("ERROR: No USD-quoted pairs found for requested assets. Found pairs:", pairs)
        sys.exit(3)

    # Cap per-asset spend if not enough cash (keep a small $5 buffer)
    n = len(to_buy)
    max_each = (usd_avail - Decimal("5")) / n
    per_each = min(args.per_asset_usd, max_each)
    if per_each <= 0:
        print(f"ERROR: Not enough USD ({usd_avail}) for {n} assets with buffer.", file=sys.stderr)
        sys.exit(4)

    print(f"USD available: {usd_avail}")
    print(f"Buying {per_each} USD of each: {', '.join(to_buy)}")
    results = []

    for base in to_buy:
        pid = pairs[base]
        if args.dry_run:
            print(f"[DRY-RUN] Would BUY {per_each} USD of {base} on {pid}")
            continue
        try:
            r = place_market_buy_quote(cl, pid, per_each)
            ok = r.get("success") is True
            oid = (r.get("success_response") or {}).get("order_id") if ok else None
            err = (r.get("error_response") or {}).get("error") if not ok else None
            results.append((pid, ok, oid, err, r))
            if ok:
                print(f"[OK] {pid} market buy (quote {per_each} USD) order_id={oid}")
            else:
                print(f"[ERR] {pid} market buy failed: {err} — full: {r}")
        except Exception as e:
            print(f"[EXC] {pid} market buy exception: {e}")

    if args.dry_run:
        print("Dry-run complete.")
    else:
        print("Done.")

if __name__ == "__main__":
    main()
