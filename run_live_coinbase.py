# run_live_coinbase.py
"""
Live runner for Coinbase Advanced Trade:
  • Places a post-only LIMIT BUY and, in the SAME request, attaches a TP/SL bracket
    via 'attached_order_configuration.trigger_bracket_gtc' (OCO behavior).
  • Rounds all prices/sizes to product increments to avoid rejection.
  • Ensures TP > entry and SL < entry by at least one tick.
  • Logs inefficiency metrics (spread, tick, reward/risk in bps, RR, inefficiency_score).

Reads the top candidate from output/trade_tickets_latest.csv with flexible columns:
  symbol | kraken_pair | pair, entry_price, qty | quantity, take_profit?, stop?
"""

import os
import csv
from pathlib import Path
from decimal import Decimal

from dotenv import load_dotenv
load_dotenv()  # ensure COINBASE_API_KEY / COINBASE_API_SECRET are loaded

from broker.coinbase_public import CoinbasePublic
from broker.coinbase_private import CoinbasePrivate

# ---- Defaults (can be overridden by config.yaml or env) ----
DEFAULTS = {
    "quote_asset": os.environ.get("COINBASE_QUOTE_ASSET") or "USD",
    "tp_offset_bps": int(os.environ.get("TP_OFFSET_BPS", "30")),    # +30 bps if TP missing
    "min_stop_pct": float(os.environ.get("MIN_STOP_PCT", "0.5")),   # -0.5% if SL missing
    "post_only": True,                                              # maker-only entry
}

def load_yaml_overrides():
    try:
        import yaml
    except ImportError:
        return {}
    cfg_file = Path("config.yaml")
    if not cfg_file.exists():
        return {}
    with cfg_file.open("r") as f:
        raw = yaml.safe_load(f) or {}
    cb = raw.get("coinbase") or {}
    bracket = raw.get("brackets") or {}
    return {
        "quote_asset": cb.get("quote_asset") or DEFAULTS["quote_asset"],
        "tp_offset_bps": int(bracket.get("tp_offset_bps") or DEFAULTS["tp_offset_bps"]),
        "min_stop_pct": float(bracket.get("min_stop_pct") or DEFAULTS["min_stop_pct"]),
        "post_only": bool(bracket.get("post_only") if "post_only" in bracket else DEFAULTS["post_only"]),
    }

CFG = {**DEFAULTS, **load_yaml_overrides()}

def env_or_fail(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Missing required env var: {name}")
    return v

def read_first_ticket(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Ticket file not found: {path}")
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        try:
            row = next(reader)
        except StopIteration:
            raise SystemExit("trade_tickets_latest.csv is empty")
    return row

def bps(x: Decimal) -> str:
    return f"{x:.2f} bps"

def fmt(x) -> str:
    if isinstance(x, Decimal):
        return f"{x.normalize()}"
    return str(x)

def main():
    # --- API keys (CDP Advanced Trade SDK style) ---
    api_key = env_or_fail("COINBASE_API_KEY")
    api_secret = env_or_fail("COINBASE_API_SECRET")

    # --- Clients ---
    pub = CoinbasePublic(api_key=api_key, api_secret=api_secret)  # auth also for market data calls
    prv = CoinbasePrivate(api_key=api_key, api_secret=api_secret)

    # --- Load ticket ---
    ticket = read_first_ticket(Path("output/trade_tickets_latest.csv"))
    pair_like = ticket.get("symbol") or ticket.get("kraken_pair") or ticket.get("pair")
    if not pair_like:
        raise SystemExit("Ticket missing symbol/kraken_pair/pair column")

    raw_entry = ticket.get("entry_price")
    raw_qty = ticket.get("qty") or ticket.get("quantity")
    raw_tp = ticket.get("take_profit")
    raw_sl = ticket.get("stop") or ticket.get("stop_loss")

    if not raw_entry or not raw_qty:
        raise SystemExit(f"Incomplete ticket row: {ticket}")

    product_id = pair_like  # already Coinbase format from strategy
    entry_price = Decimal(str(raw_entry))
    base_size = Decimal(str(raw_qty))
    tp_price = Decimal(str(raw_tp)) if raw_tp else None
    sl_price = Decimal(str(raw_sl)) if raw_sl else None

    # Derive TP/SL if missing
    if tp_price is None:
        tp_price = entry_price * (Decimal(1) + Decimal(CFG["tp_offset_bps"]) / Decimal(10000))
    if sl_price is None:
        sl_price = entry_price * (Decimal(1) - Decimal(CFG["min_stop_pct"]) / Decimal(100))

    # Rounding to product increments
    entry_r = pub.round_price(product_id, entry_price)
    size_r  = pub.round_size(product_id, base_size)
    tp_r    = pub.round_price(product_id, tp_price)
    sl_r    = pub.round_price(product_id, sl_price)

    # --- Enforce Coinbase bracket bounds: TP > entry, SL < entry by at least one tick ---
    tick = pub.quote_increment(product_id)
    if tp_r <= entry_r:
        tp_r = entry_r + tick
    if sl_r >= entry_r:
        sl_r = entry_r - tick
    if sl_r <= 0:
        sl_r = tick
    tp_r = pub.round_price(product_id, tp_r)
    sl_r = pub.round_price(product_id, sl_r)

    # Market snapshot (for logging)
    try:
        bid, ask = pub.best_bid_ask(product_id)
        mid = (bid + ask) / 2
        spread = (ask - bid)
        spread_bps = (spread / mid) * 10000 if mid > 0 else Decimal(0)
        tick_bps = (tick / mid) * 10000 if mid > 0 else Decimal(0)
    except Exception:
        bid = ask = mid = spread = spread_bps = tick_bps = Decimal(0)

    reward_bps = ((tp_r - entry_r) / entry_r) * 10000 if entry_r > 0 else Decimal(0)
    risk_bps   = ((entry_r - sl_r) / entry_r) * 10000 if entry_r > 0 else Decimal(0)
    rr_ratio   = (reward_bps / risk_bps) if risk_bps > 0 else Decimal(0)
    denom      = spread_bps + (tick_bps / 2) if (spread_bps + tick_bps) > 0 else Decimal(1)
    inefficiency_score = reward_bps / denom

    # Logs
    print(f"[coinbase] product_id={product_id}")
    if bid and ask:
        print(f"[market]  bid={fmt(bid)}  ask={fmt(ask)}  spread={fmt(spread)} ({bps(spread_bps)})  tick={fmt(tick)} ({bps(tick_bps)})")
    else:
        print(f"[market]  (depth unavailable right now)")
    print(f"[entry]   LIMIT_MAKER BUY base_size={fmt(size_r)} price={fmt(entry_r)} post_only={CFG['post_only']}")
    print(f"[bracket] TP(limit)={fmt(tp_r)}  SL(stop_trigger)={fmt(sl_r)}")
    print(f"[risk]    reward={bps(reward_bps)}  risk={bps(risk_bps)}  RR={rr_ratio:.2f}x")
    print(f"[score]   inefficiency_score={inefficiency_score:.2f}  (reward_bps / (spread_bps + 0.5*tick_bps))")

    # --- Place parent with attached TP/SL (one API call) ---
    resp = prv.create_limit_buy_with_bracket(
        product_id=product_id,
        base_size=str(size_r),
        limit_price=str(entry_r),
        post_only=CFG["post_only"],
        tp_limit_price=str(tp_r),
        sl_stop_trigger_price=str(sl_r),
    )

    ok = resp.get("success", False)
    if not ok:
        raise SystemExit(f"Create order failed: {resp}")

    sr = resp.get("success_response", {}) or {}
    order_id = sr.get("order_id") or "<unknown>"
    print(f"[ok] order_id={order_id} product_id={product_id}")

if __name__ == "__main__":
    main()
