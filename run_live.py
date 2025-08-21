# run_live.py
# Run strategy -> auto-place MARKET entry with attached MARKET stop-loss on Kraken.
# - Primary path: single AddOrder with close[ordertype]=stop-loss, close[price]=...
# - Fallback: place MARKET entry, then place separate stop using actual balance (minus a tiny safety margin)
# - Logs execution timing to logs/executions.csv and basic PnL placeholder to logs/pnl_live.csv

import os
import time
import hmac
import base64
import hashlib
import urllib.parse as up
from datetime import datetime, timezone
import runpy
import requests
import pandas as pd

import config as C

API_BASE = "https://api.kraken.com"
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET_RAW = os.getenv("KRAKEN_API_SECRET", "")

EXEC_LOG = "logs/executions.csv"
PNL_LOG  = "logs/pnl_live.csv"
TICKETS  = "output/trade_tickets_latest.csv"

# Raise this or risk.single_trade_cap_usd if Kraken rejects tiny orders
MIN_NOTIONAL_USD = 15.0

def _clean_secret(s: str) -> bytes:
    if not s:
        raise SystemExit("Missing KRAKEN_API_SECRET in .env")
    s = s.strip().strip('"').strip("'").replace(" ", "")
    pad = (-len(s)) % 4
    s_padded = s + ("=" * pad)
    try:
        return base64.b64decode(s_padded)
    except Exception:
        return base64.urlsafe_b64decode(s_padded)

API_SECRET = _clean_secret(API_SECRET_RAW)

def _nonce() -> str:
    return str(int(time.time() * 1000))

def _sign(path: str, data: dict) -> str:
    postdata = up.urlencode(data)
    encoded = (data["nonce"] + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(API_SECRET, message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()

def _private_post(endpoint: str, data: dict) -> dict:
    if not API_KEY:
        raise SystemExit("Missing KRAKEN_API_KEY in .env")
    path = f"/0/private/{endpoint}"
    data = {**data, "nonce": _nonce()}
    headers = {
        "API-Key": API_KEY,
        "API-Sign": _sign(path, data),
        "User-Agent": "owcg-live-runner/1.2",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    r = requests.post(API_BASE + path, headers=headers, data=data, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"Kraken error: {j['error']}")
    return j

def _append_csv(path: str, row: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df = pd.DataFrame([row])
    if not os.path.exists(path):
        df.to_csv(path, index=False)
    else:
        df.to_csv(path, mode="a", header=False, index=False)

def _base_from_pair(pair: str) -> str:
    for q in ("USD","USDT","USDC","EUR","GBP","AUD","CAD","CHF","JPY"):
        if pair.endswith(q):
            return pair[:-len(q)]
    return pair  # fallback

def get_balance() -> dict:
    """Return balances as {asset: float} for available funds."""
    j = _private_post("Balance", {})
    res = j.get("result", {}) or {}
    out = {}
    for k, v in res.items():
        try:
            out[k] = float(v)
        except Exception:
            pass
    return out

def place_market_with_attached_stop(pair: str, side: str, volume: float, stop_price: float):
    """
    Place MARKET entry with an attached stop-loss (market) using close[...] fields.
    """
    data = {
        "pair": pair,
        "type": side,                   # "buy" or "sell"
        "ordertype": "market",
        "volume": f"{volume:.10f}",
        # Attached closing stop-loss (market)
        "close[ordertype]": "stop-loss",
        "close[price]": f"{stop_price:.10f}",
        # "validate": "true",  # uncomment to have Kraken validate without placing
    }
    t0 = time.monotonic()
    resp = _private_post("AddOrder", data)
    t1 = time.monotonic()
    return resp, t1 - t0

def place_market(pair: str, side: str, volume: float):
    data = {
        "pair": pair,
        "type": side,
        "ordertype": "market",
        "volume": f"{volume:.10f}",
    }
    t0 = time.monotonic()
    resp = _private_post("AddOrder", data)
    t1 = time.monotonic()
    return resp, t1 - t0

def place_stop_market(pair: str, side: str, stop_price: float, volume: float):
    data = {
        "pair": pair,
        "type": side,                 # "sell" for long stops; "buy" for short
        "ordertype": "stop-loss",     # Kraken: stop-loss == market stop
        "price": f"{stop_price:.10f}",
        "volume": f"{volume:.10f}",
    }
    t0 = time.monotonic()
    resp = _private_post("AddOrder", data)
    t1 = time.monotonic()
    return resp, t1 - t0

def main():
    # 1) Safety checks
    if C.DRY_RUN:
        raise SystemExit("DRY_RUN is True in config. Set live.dry_run: false to place live orders.")
    if not API_KEY or not API_SECRET:
        raise SystemExit("Missing Kraken API credentials in .env")

    cap_usd = float(getattr(C, "SINGLE_TRADE_CAP_USD", 25.0))

    # 2) Run the strategy to create a fresh ticket
    print("[run_live] Running strategy to generate ticket...")
    runpy.run_path("run_strategy.py", run_name="__main__")

    # 3) Read the latest ticket
    if not os.path.exists(TICKETS):
        raise SystemExit("No trade_tickets_latest.csv found. Strategy did not emit a ticket.")
    df = pd.read_csv(TICKETS)
    if df.empty:
        raise SystemExit("trade_tickets_latest.csv is empty. Screener produced no trades.")
    row = df.iloc[0]  # auto-exec top ticket only

    pair = str(row["kraken_pair"])
    side = str(row["side"]).strip().lower()
    entry = float(row["entry_price"])
    stop  = float(row["stop"])
    qty   = float(row["qty"])
    coin  = str(row["coin_id"])
    status = str(row.get("status", "SUGGESTED"))

    if side not in ("buy", "sell"):
        raise SystemExit(f"Unexpected side in ticket: {side}")

    notional = entry * qty
    # Auto-bump to pass min order size if needed, but do not exceed cap
    target_usd = max(notional, MIN_NOTIONAL_USD)
    target_usd = min(target_usd, cap_usd)
    if target_usd > notional:
        qty = target_usd / entry
        print(f"[run_live] Increasing qty to meet notional constraints: ${notional:.2f} -> ${target_usd:.2f}")

    # 4) Try MARKET entry with attached STOP-LOSS (single call)
    print(f"[run_live] Placing MARKET {side.upper()} {pair} with attached STOP-LOSS @ {stop:.10f} qty={qty:.8f} ~${qty*entry:.2f}")
    try:
        resp_entry, exec_secs_entry = place_market_with_attached_stop(pair, side, qty, stop)
        entry_txids = resp_entry.get("result", {}).get("txid", [])
        print(f"[run_live] Entry+Stop OK (attached). txids={entry_txids} time={exec_secs_entry:.3f}s")
        stop_txids = []  # attached, Kraken won’t return separate txid here
    except Exception as e:
        print(f"[run_live] Attached stop failed: {e} — falling back to separate stop placement.")

        # 4b) Fallback: place MARKET entry first
        resp_entry, exec_secs_entry = place_market(pair, side, qty)
        entry_txids = resp_entry.get("result", {}).get("txid", [])
        print(f"[run_live] Entry OK. txids={entry_txids} time={exec_secs_entry:.3f}s")

        # Give Kraken a moment to credit the base asset; then measure actual available balance
        base = _base_from_pair(pair)
        # Try a few times to see the base credited to available balance
        balance_base = 0.0
        for _ in range(6):
            time.sleep(0.7)
            bal = get_balance()
            # Kraken spot assets are often named plainly for alts (e.g., "FLOKI")
            # If not found, try alt code variants (not needed for FLOKI typically)
            if base in bal:
                balance_base = bal[base]
                break

        # Safety: sell a touch less than balance to avoid rounding/fee rejections
        stop_qty = min(balance_base, qty) * 0.9975 if balance_base > 0 else qty * 0.9975
        stop_side = "sell" if side == "buy" else "buy"
        print(f"[run_live] Placing separate STOP-LOSS MARKET {stop_side.upper()} {pair} @ {stop:.10f} qty={stop_qty:.8f} (balance_base={balance_base:.8f})")
        resp_stop, exec_secs_stop = place_stop_market(pair, stop_side, stop, stop_qty)
        stop_txids = resp_stop.get("result", {}).get("txid", [])
        print(f"[run_live] Stop OK. txids={stop_txids} time={exec_secs_stop:.3f}s")

    # 5) Log execution timing
    now = datetime.now(timezone.utc).isoformat()
    exec_row = {
        "ts_utc": now,
        "coin_id": coin,
        "pair": pair,
        "side": side,
        "qty": f"{qty:.10f}",
        "entry_price": f"{entry:.10f}",
        "stop_price": f"{stop:.10f}",
        "entry_txids": "|".join(entry_txids),
        "stop_txids": "|".join(stop_txids),
        "entry_exec_secs": f"{exec_secs_entry:.3f}",
        "stop_exec_secs": "",  # left blank if attached stop
        "ticket_status": status,
        "attached_stop": "yes" if not stop_txids else "no",
    }
    _append_csv(EXEC_LOG, exec_row)

    # 6) PnL placeholder (realized requires a close)
    pnl_row = {
        "ts_utc": now,
        "coin_id": coin,
        "pair": pair,
        "side": side,
        "qty": f"{qty:.10f}",
        "entry_price": f"{entry:.10f}",
        "stop_price": f"{stop:.10f}",
        "entry_notional_usd": f"{qty*entry:.2f}",
        "realized_pnl_usd": "",
        "unrealized_pnl_usd": "",
        "entry_txids": "|".join(entry_txids),
    }
    _append_csv(PNL_LOG, pnl_row)

    print(f"[run_live] Logged execution to {EXEC_LOG} and PnL placeholder to {PNL_LOG}")
    print("[run_live] Done.")

if __name__ == "__main__":
    main()
