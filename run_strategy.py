# run_strategy.py
"""
Coinbase-native screener -> ticket writer with depth validation.

Flow:
  1) Pull Coinbase public products (auth required on Advanced).
  2) Filter to products with the configured quote asset (coinbase.quote_asset, default USD).
  3) Sort candidates by 24h quote volume (desc) and pick the first that returns a live best bid/ask.
     If none, fall back to a short majors list and again require live depth.
  4) Build a BUY ticket that:
       • joins best bid as a post-only limit,
       • sizes to ~target_notional_usd (rounded to base_increment),
       • derives TP (+tp_offset_bps) & SL (-min_stop_pct) from config if not provided.
  5) Write output/trade_tickets_latest.csv compatible with run_live_coinbase.py.
"""

import os
import csv
from pathlib import Path
from decimal import Decimal

from dotenv import load_dotenv
load_dotenv()  # ensure COINBASE_API_KEY / COINBASE_API_SECRET are loaded

import yaml
from broker.coinbase_public import CoinbasePublic

OUTDIR = Path("output")
OUTDIR.mkdir(exist_ok=True)
TICKET_PATH = OUTDIR / "trade_tickets_latest.csv"

# ---------- Load config ----------
DEFAULTS = {
    "quote_asset": os.environ.get("COINBASE_QUOTE_ASSET") or "USD",
    "tp_offset_bps": 30,
    "min_stop_pct": 0.5,
    "target_notional_usd": 25,
}

def load_cfg():
    cfg = DEFAULTS.copy()
    if Path("config.yaml").exists():
        with open("config.yaml", "r") as f:
            raw = yaml.safe_load(f) or {}
        cb = raw.get("coinbase") or {}
        br = raw.get("brackets") or {}
        cfg["quote_asset"] = cb.get("quote_asset", cfg["quote_asset"])
        cfg["tp_offset_bps"] = int(br.get("tp_offset_bps", cfg["tp_offset_bps"]))
        cfg["min_stop_pct"] = float(br.get("min_stop_pct", cfg["min_stop_pct"]))
        cfg["target_notional_usd"] = float(br.get("target_notional_usd", cfg["target_notional_usd"]))
    return cfg

CFG = load_cfg()

def env_or_fail(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Missing required env var: {name}")
    return v

FALLBACK_UNIVERSE = [
    "BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD", "APT-USD", "AVAX-USD", "DOGE-USD"
]

def first_with_depth(pub: CoinbasePublic, product_ids):
    for pid in product_ids:
        try:
            bid, ask = pub.best_bid_ask(pid)
            if bid > 0 and ask > 0:
                return pid, bid, ask
        except Exception:
            continue
    return None, None, None

def pick_product_with_depth(pub: CoinbasePublic, quote_asset: str):
    products = pub.list_products()
    cands = []
    for p in products:
        if p.get("status") != "online":
            continue
        if p.get("trading_disabled"):
            continue
        pid = p.get("product_id") or p.get("id")
        if not pid or not pid.endswith(f"-{quote_asset.upper()}"):
            continue
        vol = Decimal(str(p.get("quote_volume_24h") or p.get("volume_24h") or "0"))
        cands.append((pid, vol))

    if cands:
        cands.sort(key=lambda x: x[1], reverse=True)
        ordered = [pid for pid, _ in cands]
        pid, bid, ask = first_with_depth(pub, ordered)
        if pid:
            return pid, bid, ask

    # Fallback to majors
    majors = [pid for pid in FALLBACK_UNIVERSE if pid.endswith(f"-{quote_asset.upper()}")]
    pid, bid, ask = first_with_depth(pub, majors)
    if pid:
        return pid, bid, ask

    # Last resort
    fallback = f"BTC-{quote_asset.upper()}"
    bid, ask = pub.best_bid_ask(fallback)  # let it raise if truly no depth
    return fallback, bid, ask

def write_ticket_row(row: dict):
    fields = ["symbol", "entry_price", "qty", "take_profit", "stop"]
    with open(TICKET_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerow(row)

def main():
    # Require keys because Advanced Trade market data needs Authorization
    api_key = env_or_fail("COINBASE_API_KEY")
    api_secret = env_or_fail("COINBASE_API_SECRET")

    pub = CoinbasePublic(api_key=api_key, api_secret=api_secret)

    product_id, best_bid, best_ask = pick_product_with_depth(pub, CFG["quote_asset"])

    # Maker join on buy: place at best_bid (resting), post_only will guard us
    entry_price = pub.round_price(product_id, best_bid)

    # Size from target notional
    notional = Decimal(str(CFG["target_notional_usd"]))
    if entry_price <= 0:
        raise SystemExit(f"Invalid entry price for {product_id}: {entry_price}")
    base_size = notional / entry_price
    base_size_r = pub.round_size(product_id, base_size)
    if base_size_r <= 0:
        # try bump to one increment
        base_size_r = pub.round_size(product_id, pub.base_increment(product_id))

    # TP/SL
    tp = entry_price * (Decimal(1) + Decimal(CFG["tp_offset_bps"]) / Decimal(10000))
    sl = entry_price * (Decimal(1) - Decimal(CFG["min_stop_pct"]) / Decimal(100))
    tp_r = pub.round_price(product_id, tp)
    sl_r = pub.round_price(product_id, sl)

    row = {
        "symbol": product_id,           # already Coinbase format; runner will accept it directly
        "entry_price": str(entry_price),
        "qty": str(base_size_r),
        "take_profit": str(tp_r),
        "stop": str(sl_r),
    }
    write_ticket_row(row)

    print("[strategy] Wrote ticket:")
    for k, v in row.items():
        print(f"  {k}: {v}")

if __name__ == "__main__":
    main()
