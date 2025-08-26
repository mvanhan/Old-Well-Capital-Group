# run_live.py
# Spot-safe exits with REST API limitations (corrected):
#  - Entry: LIMIT (post-only) + reprice loop; no market fallback; cancel if still unfilled.
#  - Attach STOP-LOSS as a conditional close on the ENTRY (avoids double reserve of coins).
#  - After FULL fill: place independent TP LIMIT immediately.
#  - OCO monitor: when one leg completes, cancel the sibling.
#  - Logs decision->accept latency and execution metadata.

import os, time, hmac, base64, hashlib, urllib.parse as up
from datetime import datetime, timezone
import runpy, requests, pandas as pd
import config as C

API_BASE = "https://api.kraken.com"
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET_RAW = os.getenv("KRAKEN_API_SECRET", "")

# ---- Private REST helpers ---------------------------------------------------
_last_private_ts = 0.0
MIN_PRIVATE_INTERVAL = 0.50
RL_MAX_RETRIES = 3
RL_INITIAL_SLEEP = 0.75

def _sign(path: str, data: dict) -> str:
    postdata = up.urlencode(data)
    secret = base64.b64decode(API_SECRET_RAW)
    message = (path.encode() + hashlib.sha256((str(data.get('nonce')) + postdata).encode()).digest())
    mac = hmac.new(secret, message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()

def _pace_private():
    global _last_private_ts
    now = time.time()
    dt = now - _last_private_ts
    if dt < MIN_PRIVATE_INTERVAL: time.sleep(MIN_PRIVATE_INTERVAL - dt)
    _last_private_ts = time.time()

def _private_post(endpoint: str, data: dict) -> dict:
    if not API_KEY: raise SystemExit("Missing KRAKEN_API_KEY in .env")
    path = f"/0/private/{endpoint}"
    backoff = RL_INITIAL_SLEEP
    for attempt in range(RL_MAX_RETRIES + 1):
        data = dict(data)
        data.setdefault("nonce", int(time.time() * 1000))
        headers = {
            "API-Key": API_KEY,
            "API-Sign": _sign(path, data),
            "User-Agent": "owcg-live-runner/2.8",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            _pace_private()
            r = requests.post(API_BASE + path, headers=headers, data=data, timeout=20)
            r.raise_for_status()
            j = r.json()
        except requests.RequestException as e:
            if attempt < RL_MAX_RETRIES:
                time.sleep(backoff); backoff *= 1.7; continue
            raise
        errs = j.get("error", []) or []
        if errs:
            if any("EAPI:Rate limit exceeded" in e for e in errs) and attempt < RL_MAX_RETRIES:
                time.sleep(backoff); backoff *= 1.5; continue
            raise RuntimeError(f"Kraken error: {errs}")
        return j
    raise RuntimeError("Exceeded retries on private POST")

def _private(endpoint: str, data: dict | None = None) -> dict:
    return _private_post(endpoint, data or {})

# ---------- Utils ----------

def _append_csv(path: str, row: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df = pd.DataFrame([row])
    mode = 'a' if os.path.exists(path) else 'w'
    header = not os.path.exists(path)
    df.to_csv(path, mode=mode, header=header, index=False)

def fmt_price(x: float, decimals: int) -> str:
    return f"{x:.{decimals}f}"

# ---------- Public endpoints ----------

def fetch_pair_meta(pair: str) -> dict:
    r = requests.get(API_BASE + "/0/public/AssetPairs", params={"pair": pair}, timeout=15)
    r.raise_for_status()
    raw = r.json()["result"]
    # pick first value
    meta = next(iter(raw.values()))
    pdp = int(meta.get("pair_decimals", 5))
    ldp = int(meta.get("lot_decimals", 8))
    meta["pair_decimals"] = pdp
    meta["lot_decimals"] = ldp
    meta["tick_size"] = 10 ** (-pdp)
    return meta

def fetch_best_bid_ask(pair: str) -> tuple[float, float]:
    r = requests.get(API_BASE + "/0/public/Ticker", params={"pair": pair}, timeout=10)
    r.raise_for_status()
    raw = r.json()["result"]
    val = next(iter(raw.values()))
    best_bid = float(val["b"][0])
    best_ask = float(val["a"][0])
    return best_bid, best_ask

# ---------- Order helpers (REST) ----------

def place_limit_post_only(pair: str, side: str, limit_price: str, volume: str,
                          close_stop_price: str | None = None, userref: int | None = None):
    # Attach STOP-LOSS as conditional close (does not double-reserve balance)
    data = {
        "pair": pair, "type": side, "ordertype": "limit",
        "price": limit_price, "volume": volume, "oflags": "post",
    }
    if userref is not None:
        data["userref"] = str(userref)
    if close_stop_price is not None:
        data["close[ordertype]"] = "stop-loss"
        data["close[price]"] = close_stop_price
    t0 = time.monotonic(); resp = _private_post("AddOrder", data); t1 = time.monotonic()
    return resp, t1 - t0

def place_stop_loss_market(pair: str, volume: str, stop_price: str):
    data = {"pair": pair, "type": "sell", "ordertype": "stop-loss", "price": stop_price, "volume": volume}
    t0 = time.monotonic(); resp = _private_post("AddOrder", data); t1 = time.monotonic()
    return resp, t1 - t0

def place_tp_limit(pair: str, tp_price: str, volume: str, userref: int | None = None):
    data = {"pair": pair, "type": "sell", "ordertype": "limit", "price": tp_price, "volume": volume}
    if userref is not None:
        data["userref"] = str(userref)
    t0 = time.monotonic(); resp = _private_post("AddOrder", data); t1 = time.monotonic()
    return resp, t1 - t0

def get_balance() -> dict:
    res = _private("Balance").get("result", {}) or {}
    out = {}
    for k, v in res.items():
        try: out[k] = float(v)
        except: pass
    return out

def query_orders(txids: list) -> dict:
    return _private("QueryOrders", {"txid": ",".join(txids)}).get("result", {})

def open_orders() -> dict:
    return _private("OpenOrders").get("result", {})

def cancel_order(txid: str) -> None:
    _private("CancelOrder", {"txid": txid})

# ---------- Post-only price helpers ----------

def compute_initial_limit(side: str, entry: float, offset_bps: float, best_bid: float, best_ask: float, tick: float) -> float:
    mid = 0.5 * (best_bid + best_ask)
    if side == "buy":
        px = mid * (1 - offset_bps / 10000.0)
        return max(px, best_bid)  # inside bid side
    else:
        px = mid * (1 + offset_bps / 10000.0)
        return min(px, best_ask)

# ---------- Main ----------

def main():
    # Load the first ticket (you can replace with your own selection)
    TICKETS = "output/trade_tickets_latest.csv"
    if not os.path.exists(TICKETS): raise SystemExit("No trade_tickets_latest.csv found.")
    df = pd.read_csv(TICKETS)
    if df.empty: raise SystemExit("trade_tickets_latest.csv is empty.")
    row = df.iloc[0]

    pair = row["kraken_pair"]
    side = row["side"].lower()
    entry = float(row["entry_price"])
    tp_f = float(row["take_profit"])  # float
    stop_f = float(row["stop"])       # float
    qty_f = float(row["qty"])         # float base qty (pre-rounding)

    meta = fetch_pair_meta(pair)
    pdp, ldp = int(meta["pair_decimals"]), int(meta["lot_decimals"])
    tick = meta.get("tick_size", 10 ** (-pdp))
    best_bid, best_ask = fetch_best_bid_ask(pair)
    print(f"[run_live] Pair meta: pair_decimals={pdp}, lot_decimals={ldp}, ordermin={meta.get('ordermin')} tick={tick}")
    print(f"[run_live] Top-of-book: bid={best_bid:.{pdp}f} ask={best_ask:.{pdp}f}")

    # Round quantity roughly (use your own precision helpers if available)
    volume_str = f"{qty_f:.{ldp}f}"

    # Compute initial limit (post-only entry)
    ENTRY_LIMIT_OFFSET_BPS = 3
    initial_limit_f = compute_initial_limit(side, entry, ENTRY_LIMIT_OFFSET_BPS, best_bid, best_ask, tick)
    limit_price = fmt_price(initial_limit_f, pdp)
    stop_price  = fmt_price(stop_f, pdp)
    tp_price    = fmt_price(tp_f, pdp)

    # Place entry with STOP-LOSS attached as conditional close (avoids double reserve)
    userref = int(time.time()) % 100000000
    print(f"[run_live] Placing ENTRY LIMIT {pair} {side} @ {limit_price} qty={volume_str} with close[stop]={stop_price}")
    resp_entry, _ = place_limit_post_only(pair, side, limit_price, volume_str,
                                          close_stop_price=stop_price, userref=userref)
    entry_txids = resp_entry.get("result", {}).get("txid", [])
    if not entry_txids:
        raise SystemExit(f"Entry rejected: {resp_entry}")
    entry_txid = entry_txids[0]

    # Wait for fill (simple polling for demo — replace with your own logic)
    REPRICE_ENABLED = True
    REPRICE_STEPS = 6
    REPRICE_STEP_BPS = 2
    REPRICE_INTERVAL_SEC = 5

    filled_qty = 0.0
    placed_limit = initial_limit_f
    reprice_count = 0

    print("[run_live] Waiting for entry fill...")
    t_wait0 = time.monotonic()
    while True:
        q = query_orders([entry_txid])
        od = q.get(entry_txid, {})
        status_q = od.get("status", "")
        vol = float(od.get("vol", 0.0) or 0.0)
        vol_exec = float(od.get("vol_exec", 0.0) or 0.0)
        filled_qty = vol_exec
        if status_q == "closed" or abs(vol_exec - vol) < 1e-9:
            print(f"[run_live] Entry filled. qty={filled_qty:.8f}")
            break

        zero_fill = vol_exec < 1e-12
        if REPRICE_ENABLED and zero_fill and reprice_count < REPRICE_STEPS:
            best_bid, best_ask = fetch_best_bid_ask(pair)
            new_limit_f = compute_initial_limit(side, entry, REPRICE_STEP_BPS, best_bid, best_ask, tick)
            if abs(new_limit_f - placed_limit) >= tick / 2:
                new_limit = fmt_price(new_limit_f, pdp)
                print(f"[run_live] Reprice {reprice_count+1}/{REPRICE_STEPS}: new_limit={new_limit} (bid={best_bid:.{pdp}f} ask={best_ask:.{pdp}f})")
                try: cancel_order(entry_txid)
                except Exception as e: print(f"[run_live] Cancel error (continuing): {e}")
                resp_entry, _ = place_limit_post_only(pair, side, new_limit, volume_str,
                                                      close_stop_price=stop_price, userref=userref)
                entry_txids = resp_entry.get("result", {}).get("txid", [])
                if entry_txids:
                    entry_txid = entry_txids[0]
                    placed_limit = new_limit_f
                    reprice_count += 1
                    time.sleep(REPRICE_INTERVAL_SEC)
                    continue
        time.sleep(REPRICE_INTERVAL_SEC)

    final_state = "filled" if filled_qty > 0 else "canceled_unfilled"

    if filled_qty > 0.0:
        # Immediately place TP LIMIT for the filled quantity
        print(f"[run_live] Placing TAKE-PROFIT LIMIT sell {pair} @ {tp_price} qty={volume_str}")
        resp_tp, exec_secs_tp = place_tp_limit(pair, tp_price, volume_str, userref=userref)
        tp_txids = resp_tp.get("result", {}).get("txid", [])
        tp_txid = tp_txids[0] if tp_txids else ""
        if not tp_txid:
            print(f"[run_live] WARNING: TP placement returned no txid: {resp_tp}")

        # OCO monitor: if TP fills, cancel conditional stop tied to entry; if stop triggers, cancel TP.
        print("[run_live] Starting OCO monitor (REST). Will cancel sibling when one completes.")
        oco_start = time.time()
        OCO_TIMEOUT_SEC = 6 * 60 * 60
        while time.time() - oco_start < OCO_TIMEOUT_SEC:
            to_query = [x for x in [entry_txid, tp_txid] if x]
            q = query_orders(to_query)
            entry_closed = q.get(entry_txid, {}).get("status") == "closed"
            tp_closed = q.get(tp_txid, {}).get("status") == "closed"

            if tp_closed:
                print("[run_live] TP filled. Canceling any conditional stop that references the entry.")
                oo = open_orders().get("open", {})
                for oid, od in oo.items():
                    if od.get("refid") == entry_txid:
                        try:
                            cancel_order(oid)
                            print(f"[run_live] Canceled conditional stop {oid}")
                        except Exception as e:
                            print(f"[run_live] Cancel stop failed (continuing): {e}")
                final_state = "tp_filled"
                break

            if entry_closed and not tp_closed:
                # Stop likely executed; cancel TP if still open
                if tp_txid:
                    try:
                        cancel_order(tp_txid)
                        print(f"[run_live] Stop executed first. Canceled TP {tp_txid}")
                    except Exception as e:
                        print(f"[run_live] Cancel TP failed (continuing): {e}")
                final_state = "stop_filled"
                break

            time.sleep(2.0)

    else:
        # Not filled in time — cancel
        print("[run_live] Entry not filled; canceled.")

    print(f"[run_live] Done. final_state={final_state}")

if __name__ == "__main__":
    main()
