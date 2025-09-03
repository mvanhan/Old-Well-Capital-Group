# run_live_binance.py
"""
Live runner for Binance Spot using an OCO bracket:
  1) Places a LIMIT_MAKER BUY for the selected symbol/qty/price
  2) When FILLED, immediately places an OCO SELL:
       - above: LIMIT_MAKER take-profit
       - below: STOP_LOSS_LIMIT stop with limit offset
Reads the top candidate from output/trade_tickets_latest.csv (the same artifact your strategy already writes).
"""

import os
import time
import uuid
import csv
from pathlib import Path
from decimal import Decimal

from broker.binance_public import BinancePublic
from broker.binance_private import BinancePrivate

# ---- Config defaults (can be overridden by config.yaml) ----
DEFAULTS = {
    "api_base": os.environ.get("BINANCE_API_BASE") or "https://api.binance.com",
    "recv_window_ms": int(os.environ.get("BINANCE_RECV_WINDOW_MS", "5000")),
    "quote_asset": os.environ.get("BINANCE_QUOTE_ASSET") or "USDT",  # use "USD" for Binance.US if you prefer
    "tp_offset_bps": 30,             # default +30 bps TP if not provided by ticket
    "min_stop_pct": 0.5,             # min 0.5% stop if not provided by ticket
    "stop_limit_offset_bps": 5,      # SL limit = stop * (1 - 5 bps)
    "entry_fill_timeout_s": 60,      # cancel entry if not filled within 60s
    "sleep_poll_s": 0.5,             # polling cadence
}

def load_yaml_overrides():
    import yaml
    cfg_file = Path("config.yaml")
    if not cfg_file.exists():
        return {}
    with cfg_file.open("r") as f:
        raw = yaml.safe_load(f) or {}
    b = (raw.get("binance_us") or raw.get("binance") or {})
    bracket = raw.get("brackets") or {}
    return {
        "api_base": b.get("api_base") or DEFAULTS["api_base"],
        "recv_window_ms": int(b.get("recv_window_ms") or DEFAULTS["recv_window_ms"]),
        "quote_asset": b.get("quote_asset") or DEFAULTS["quote_asset"],
        "tp_offset_bps": int(bracket.get("tp_offset_bps") or DEFAULTS["tp_offset_bps"]),
        "min_stop_pct": float(bracket.get("min_stop_pct") or DEFAULTS["min_stop_pct"]),
        "stop_limit_offset_bps": int(bracket.get("stop_limit_offset_bps") or DEFAULTS["stop_limit_offset_bps"]),
        "entry_fill_timeout_s": int(bracket.get("entry_fill_timeout_s") or DEFAULTS["entry_fill_timeout_s"]),
        "sleep_poll_s": float(bracket.get("sleep_poll_s") or DEFAULTS["sleep_poll_s"]),
    }

CFG = {**DEFAULTS, **load_yaml_overrides()}

def env_or_fail(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Missing required env var: {name}")
    return v

def find_ticket(path: Path) -> dict:
    """
    Reads output/trade_tickets_latest.csv and returns the first row as a dict.
    Expected flexible columns: kraken_pair|symbol, entry_price, qty, take_profit, stop
    """
    if not path.exists():
        raise SystemExit(f"Ticket file not found: {path}")
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        try:
            row = next(reader)
        except StopIteration:
            raise SystemExit("No rows in trade_tickets_latest.csv")
    return row

def main():
    # --- Setup clients ---
    api_key = env_or_fail("BINANCE_API_KEY")
    api_secret = env_or_fail("BINANCE_API_SECRET")
    api_base = CFG["api_base"]
    print(f"[binance] api_base={api_base}")

    pub = BinancePublic(api_base=api_base)
    prv = BinancePrivate(api_key, api_secret, api_base=api_base, recv_window_ms=CFG["recv_window_ms"])

    # --- Load ticket ---
    ticket = find_ticket(Path("output/trade_tickets_latest.csv"))
    pair_like = ticket.get("symbol") or ticket.get("kraken_pair") or ticket.get("pair")
    if not pair_like:
        raise SystemExit("Ticket missing symbol/kraken_pair/pair column")
    raw_entry = ticket.get("entry_price")
    raw_qty = ticket.get("qty") or ticket.get("quantity")
    raw_tp = ticket.get("take_profit")
    raw_stop = ticket.get("stop") or ticket.get("stop_loss")

    if not all([raw_entry, raw_qty]) or (not raw_tp and not raw_stop):
        raise SystemExit(f"Incomplete ticket row: {ticket}")

    symbol = pub.map_pair_to_binance_symbol(pair_like, CFG["quote_asset"])
    print(f"[ticket] pair={pair_like} -> binance_symbol={symbol}")

    entry_price = Decimal(str(raw_entry))
    qty = Decimal(str(raw_qty))
    tp_price = Decimal(str(raw_tp)) if raw_tp else None
    stop_price = Decimal(str(raw_stop)) if raw_stop else None

    # Derive TP/SL if missing
    if tp_price is None:
        tp_price = entry_price * (Decimal(1) + Decimal(CFG["tp_offset_bps"]) / Decimal(10000))
    if stop_price is None:
        stop_price = entry_price * (Decimal(1) - Decimal(CFG["min_stop_pct"]) / Decimal(100))

    # Round entry and qty to filters
    entry_price_r = pub.round_price(symbol, entry_price)
    qty_r = pub.round_qty(symbol, qty)
    pub.ensure_notional_ok(symbol, entry_price_r, qty_r)

    # Round TP/SL to filters. For stop-limit, compute a slightly worse limit.
    tp_price_r = pub.round_price(symbol, tp_price)
    stop_price_r = pub.round_price(symbol, stop_price)
    sl_limit = stop_price_r * (Decimal(1) - Decimal(CFG["stop_limit_offset_bps"]) / Decimal(10000))
    sl_limit_r = pub.round_price(symbol, sl_limit)

    print(f"[entry] LIMIT_MAKER BUY {symbol} qty={qty_r} price={entry_price_r}")
    print(f"[bracket] TP(limit_maker)={tp_price_r}  SL(stop)={stop_price_r}  SL(limit)={sl_limit_r}")

    # --- Place entry (LIMIT_MAKER BUY) ---
    entry_client_id = f"owcg-entry-{uuid.uuid4().hex[:12]}"
    entry_resp = prv.new_order(
        symbol=symbol,
        side="BUY",
        type_="LIMIT_MAKER",
        price=str(entry_price_r),
        quantity=str(qty_r),
        newClientOrderId=entry_client_id,
        newOrderRespType="RESULT",
    )
    entry_order_id = entry_resp.get("orderId")
    print(f"[entry] placed orderId={entry_order_id} clOrdId={entry_client_id}")

    # --- Poll status until FILLED or timeout, cancel if needed ---
    t0 = time.time()
    status = None
    while time.time() - t0 < CFG["entry_fill_timeout_s"]:
        st = prv.query_order(symbol, orderId=entry_order_id)
        status = st.get("status")
        if status in ("FILLED", "PARTIALLY_FILLED"):
            break
        if status in ("REJECTED", "EXPIRED", "CANCELED"):
            raise SystemExit(f"[entry] not live: status={status} details={st}")
        time.sleep(CFG["sleep_poll_s"])

    if status != "FILLED":
        # Try to cancel and exit
        try:
            prv.cancel_order(symbol, orderId=entry_order_id)
        except Exception as e:
            print(f"[warn] cancel failed: {e}")
        raise SystemExit(f"[entry] fill timeout after {CFG['entry_fill_timeout_s']}s; canceled and exiting.")

    print(f"[entry] FILLED. Placing OCO bracket...")

    # --- Place OCO SELL (TP + SL) for filled qty ---
    # To be conservative, re-query executedQty in case of partials then top-up/adjust.
    filled = prv.query_order(symbol, orderId=entry_order_id).get("executedQty") or str(qty_r)
    filled_qty = pub.round_qty(symbol, Decimal(str(filled)))
    if filled_qty <= 0:
        raise SystemExit("[bracket] executedQty=0 ? Aborting OCO placement.")
    # Ensure notional for both legs
    pub.ensure_notional_ok(symbol, tp_price_r, filled_qty)
    pub.ensure_notional_ok(symbol, sl_limit_r, filled_qty)

    oco_resp = prv.create_oco_sell_tpsl(
        symbol=symbol,
        quantity=str(filled_qty),
        tp_price=str(tp_price_r),
        sl_stop_price=str(stop_price_r),
        sl_limit_price=str(sl_limit_r),
        tif="GTC",
        new_order_resp_type="RESULT",
    )
    olist_id = oco_resp.get("orderListId") or oco_resp.get("listClientOrderId")
    print(f"[bracket] OCO placed for {symbol} qty={filled_qty} listId={olist_id}")

if __name__ == "__main__":
    main()
