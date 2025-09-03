# run_live_coinbase.py
"""
Live runner for Coinbase Advanced Trade:
  • Places a post-only LIMIT BUY and, in the SAME request, attaches a TP/SL bracket
    via 'attached_order_configuration.trigger_bracket_gtc' (OCO behavior).
  • Rounds all prices/sizes to product increments to avoid rejection.

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

def main():
    # --- API keys (CDP Advanced Trade SDK style) ---
    #   COINBASE_API_KEY    -> "organizations/{org_id}/apiKeys/{key_id}"
    #   COINBASE_API_SECRET -> full EC private key PEM block
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
    entry_price_r = pub.round_price(product_id, entry_price)
    base_size_r = pub.round_size(product_id, base_size)
    tp_price_r = pub.round_price(product_id, tp_price)
    sl_price_r = pub.round_price(product_id, sl_price)

    print(f"[coinbase] product_id={product_id}")
    print(f"[entry]  LIMIT_MAKER BUY base_size={base_size_r} price={entry_price_r} post_only={CFG['post_only']}")
    print(f"[bracket] TP(limit)={tp_price_r}  SL(stop_trigger)={sl_price_r}")

    # --- Place parent with attached TP/SL (one API call) ---
    resp = prv.create_limit_buy_with_bracket(
        product_id=product_id,
        base_size=str(base_size_r),
        limit_price=str(entry_price_r),
        post_only=CFG["post_only"],
        tp_limit_price=str(tp_price_r),
        sl_stop_trigger_price=str(sl_price_r),
    )

    ok = resp.get("success", False)
    if not ok:
        raise SystemExit(f"Create order failed: {resp}")

    sr = resp.get("success_response", {}) or {}
    order_id = sr.get("order_id") or "<unknown>"
    print(f"[ok] order_id={order_id} product_id={product_id}")

if __name__ == "__main__":
    main()
