# run_strategy.py
"""
Coinbase-native screener -> ticket writer with depth validation and detailed logs.

Flow:
  1) Pull Coinbase public products (auth required on Advanced).
  2) Filter to products with the configured quote asset (coinbase.quote_asset, default USD).
  3) Sort candidates by 24h quote volume (desc) and pick the first that returns a live best bid/ask.
     If none, fall back to a majors list and require live depth.
  4) Build a BUY ticket that:
       • joins best bid as a post-only limit (rounded to quote_increment),
       • sizes to ~target_notional_usd (rounded to base_increment),
       • derives TP (+tp_offset_bps) & SL (-min_stop_pct) and nudges them by one tick if needed
         so that TP > entry and SL < entry.
  5) Write output/trade_tickets_latest.csv compatible with run_live_coinbase.py.
  6) Print and save a rich log with an "inefficiency score" and all related data.
"""

import os
import csv
import time
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
load_dotenv()  # ensure COINBASE_API_KEY / COINBASE_API_SECRET are loaded

import yaml
from broker.coinbase_public import CoinbasePublic

OUTDIR = Path("output")
OUTDIR.mkdir(exist_ok=True)
LOGDIR = Path("logs")
LOGDIR.mkdir(exist_ok=True)

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
        # Risk block (optional, if present keep target in sync)
        risk = raw.get("risk") or {}
        if "bankroll_usd" in risk and "max_trade_pct" in risk:
            try:
                bankroll = Decimal(str(risk["bankroll_usd"]))
                max_pct = Decimal(str(risk["max_trade_pct"])) / Decimal(100)
                cfg["target_notional_usd"] = float((bankroll * max_pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            except Exception:
                pass

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
        from decimal import Decimal as D
        vol = D(str(p.get("quote_volume_24h") or p.get("volume_24h") or "0"))
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

def bps(x: Decimal) -> str:
    return f"{x:.2f} bps"

def fmt(x) -> str:
    # Compact decimal -> string
    if isinstance(x, Decimal):
        return f"{x.normalize()}"
    return str(x)

def log_block(text: str):
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = LOGDIR / f"strategy_{ts}.log"
    with path.open("w") as f:
        f.write(text)
    print(text)

def main():
    # Require keys because Advanced Trade market data needs Authorization
    api_key = env_or_fail("COINBASE_API_KEY")
    api_secret = env_or_fail("COINBASE_API_SECRET")

    pub = CoinbasePublic(api_key=api_key, api_secret=api_secret)

    product_id, best_bid, best_ask = pick_product_with_depth(pub, CFG["quote_asset"])

    # Maker join on buy: place at best_bid (resting), post_only will guard us
    entry = pub.round_price(product_id, best_bid)
    tick = pub.quote_increment(product_id)

    # Size from target notional
    notional = Decimal(str(CFG["target_notional_usd"]))
    if entry <= 0:
        raise SystemExit(f"Invalid entry price for {product_id}: {entry}")
    base_size = notional / entry
    base_size_r = pub.round_size(product_id, base_size)
    if base_size_r <= 0:
        base_size_r = pub.round_size(product_id, pub.base_increment(product_id))

    # TP/SL (derive + round + nudge by 1 tick if rounding collapsed them onto entry)
    tp0 = entry * (Decimal(1) + Decimal(CFG["tp_offset_bps"]) / Decimal(10000))
    sl0 = entry * (Decimal(1) - Decimal(CFG["min_stop_pct"]) / Decimal(100))
    tp = pub.round_price(product_id, tp0)
    sl = pub.round_price(product_id, sl0)
    if tp <= entry:
        tp = entry + tick
    if sl >= entry:
        sl = entry - tick
    if sl <= 0:
        sl = tick
    tp = pub.round_price(product_id, tp)
    sl = pub.round_price(product_id, sl)

    # Metrics
    mid = (best_bid + best_ask) / 2
    spread = (best_ask - best_bid)
    spread_bps = (spread / mid) * 10000 if mid > 0 else Decimal(0)
    tick_bps = (tick / mid) * 10000 if mid > 0 else Decimal(0)

    reward_bps = ((tp - entry) / entry) * 10000 if entry > 0 else Decimal(0)
    risk_bps   = ((entry - sl) / entry) * 10000 if entry > 0 else Decimal(0)
    rr_ratio   = (reward_bps / risk_bps) if risk_bps > 0 else Decimal(0)

    # Inefficiency score: upside (reward_bps) compared to friction (spread + half-tick)
    denom = spread_bps + (tick_bps / 2) if (spread_bps + tick_bps) > 0 else Decimal(1)
    inefficiency_score = reward_bps / denom

    # Write ticket
    row = {
        "symbol": product_id,
        "entry_price": str(entry),
        "qty": str(base_size_r),
        "take_profit": str(tp),
        "stop": str(sl),
    }
    write_ticket_row(row)

    # Rich log
    lines = []
    lines.append("=== STRATEGY TICKET ===")
    lines.append(f"product_id: {product_id}")
    lines.append(f"quote_asset: {CFG['quote_asset']}")
    lines.append("")
    lines.append("--- Market ---")
    lines.append(f"best_bid: {fmt(best_bid)}")
    lines.append(f"best_ask: {fmt(best_ask)}")
    lines.append(f"mid:      {fmt(mid)}")
    lines.append(f"spread:   {fmt(spread)}  ({bps(spread_bps)})")
    lines.append(f"tick:     {fmt(tick)}     ({bps(tick_bps)})")
    lines.append("")
    lines.append("--- Order Plan ---")
    lines.append(f"entry: {fmt(entry)}  size: {fmt(base_size_r)} (~${CFG['target_notional_usd']})  post_only: True")
    lines.append(f"TP:    {fmt(tp)}")
    lines.append(f"SL:    {fmt(sl)}")
    lines.append("")
    lines.append("--- Risk/Reward ---")
    lines.append(f"reward_bps: {bps(reward_bps)}")
    lines.append(f"risk_bps:   {bps(risk_bps)}")
    lines.append(f"RR_ratio:   {rr_ratio:.2f}x")
    lines.append("")
    lines.append("--- Inefficiency ---")
    lines.append(f"inefficiency_score: {inefficiency_score:.2f}  (reward_bps / (spread_bps + 0.5*tick_bps))")
    lines.append("")
    lines.append("--- CSV ---")
    for k, v in row.items():
        lines.append(f"{k}: {v}")
    lines.append("======================")

    log_block("\n".join(lines))

if __name__ == "__main__":
    main()
