# run_live.py
# Spot-safe exits with REST API limitations:
#  - Entry: LIMIT (post-only) + reprice loop; no market fallback; cancel if still unfilled.
#  - After FULL fill: place independent MARKET stop-loss sell.
#  - TP LIMIT only when price reaches TP: cancel stop, then place TP (can't hold both on spot REST).
#  - Logs decision->accept latency and execution metadata.

import os, time, hmac, base64, hashlib, urllib.parse as up
from datetime import datetime, timezone
import runpy, requests, pandas as pd
import config as C

API_BASE = "https://api.kraken.com"
API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET_RAW = os.getenv("KRAKEN_API_SECRET", "")

EXEC_LOG = "logs/executions.csv"
PNL_LOG  = "logs/pnl_live.csv"
TICKETS  = "output/trade_tickets_latest.csv"

# -------- Guardrails --------
MIN_NOTIONAL_USD = 15.0
ENTRY_LIMIT_OFFSET_BPS = 3
FILL_TIMEOUT_SEC = 240

REPRICE_ENABLED = True
REPRICE_STEPS = 6
REPRICE_STEP_BPS = 2
REPRICE_INTERVAL_SEC = 10

# TP monitor (after entry + stop placed)
TP_MONITOR_ENABLED = True
TP_MONITOR_MAX_SEC = 3600          # stop watching after 1h (keeps stop live if TP not hit)
TP_MONITOR_POLL_SEC = 5

# Rate limiting / pacing
MIN_PRIVATE_INTERVAL = 1.1
RL_MAX_RETRIES = 5
RL_INITIAL_SLEEP = 1.5
POLL_BASE_SEC = max(2.0, REPRICE_INTERVAL_SEC)
_last_private_ts = 0.0

# ---------- Signing ----------
def _clean_secret(s: str) -> bytes:
    if not s: raise SystemExit("Missing KRAKEN_API_SECRET in .env")
    s = s.strip().strip('"').strip("'").replace(" ", "")
    pad = (-len(s)) % 4
    s_padded = s + ("=" * pad)
    try:    return base64.b64decode(s_padded)
    except: return base64.urlsafe_b64decode(s_padded)
API_SECRET = _clean_secret(API_SECRET_RAW)

def _nonce() -> str: return str(int(time.time() * 1000))

def _sign(path: str, data: dict) -> str:
    postdata = up.urlencode(data)
    encoded = (data["nonce"] + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(API_SECRET, message, hashlib.sha512)
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
        _pace_private()
        payload = {**data, "nonce": _nonce()}
        headers = {
            "API-Key": API_KEY,
            "API-Sign": _sign(path, payload),
            "User-Agent": "owcg-live-runner/2.7",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            r = requests.post(API_BASE + path, headers=headers, data=payload, timeout=20)
            r.raise_for_status()
            j = r.json()
        except requests.RequestException:
            if attempt < RL_MAX_RETRIES: time.sleep(backoff); backoff *= 1.5; continue
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
    if not os.path.exists(path): df.to_csv(path, index=False)
    else:                         df.to_csv(path, mode="a", header=False, index=False)

def _base_from_pair(pair: str) -> str:
    for q in ("USD","USDT","USDC","EUR","GBP","AUD","CAD","CHF","JPY"):
        if pair.endswith(q): return pair[:-len(q)]
    return pair

# ---------- Public ----------
def fetch_pair_meta(pair: str) -> dict:
    r = requests.get("https://api.kraken.com/0/public/AssetPairs", params={"pair": pair}, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("error"): raise RuntimeError(f"AssetPairs error: {j['error']}")
    info = list(j.get("result", {}).values())[0]
    return {
        "pair_decimals": int(info.get("pair_decimals", 8)),
        "lot_decimals": int(info.get("lot_decimals", 8)),
        "ordermin": float(info.get("ordermin", "0") or 0.0),
        "costmin": float(info.get("costmin", "0") or 0.0),
        "tick_size": float(info.get("tick_size", "0") or 0.0),
        "altname": info.get("altname", ""),
        "wsname": info.get("wsname", ""),
    }

def fetch_best_bid_ask(pair: str) -> tuple[float, float]:
    r = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": pair}, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("error"): raise RuntimeError(f"Ticker error: {j['error']}")
    info = list(j.get("result", {}).values())[0]
    return float(info["b"][0]), float(info["a"][0])

def fmt_price(p: float, decimals: int) -> str: return f"{p:.{decimals}f}"
def round_qty_down(q: float, decimals: int) -> float: return float(f"{q:.{decimals}f}")

def apply_mins_and_precisions(entry: float, qty: float, cap_usd: float, meta: dict) -> tuple[float, float]:
    notional = entry * qty
    target_usd = max(min(notional, cap_usd), MIN_NOTIONAL_USD)
    costmin = float(meta.get("costmin", 0.0) or 0.0)
    if costmin > 0: target_usd = max(target_usd, costmin)
    lot_decimals = int(meta.get("lot_decimals", 8))
    ordermin = float(meta.get("ordermin", 0.0) or 0.0)
    qty_target = max(target_usd / entry, ordermin if ordermin > 0 else 0.0)
    qty_rounded = round_qty_down(qty_target, lot_decimals)
    if qty_rounded == 0 and qty_target > 0: qty_rounded = float(fmt_price(qty_target, lot_decimals))
    return qty_rounded, target_usd

# ---------- Private helpers ----------
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

def place_limit_post_only(pair: str, side: str, limit_price: str, volume: str):
    data = {"pair": pair, "type": side, "ordertype": "limit", "price": limit_price, "volume": volume, "oflags": "post"}
    t0 = time.monotonic(); resp = _private_post("AddOrder", data); t1 = time.monotonic()
    return resp, t1 - t0

def place_stop_loss_market(pair: str, volume: str, stop_price: str):
    # Independent sell stop-loss (market) for long exposure
    data = {"pair": pair, "type": "sell", "ordertype": "stop-loss", "price": stop_price, "volume": volume}
    t0 = time.monotonic(); resp = _private_post("AddOrder", data); t1 = time.monotonic()
    return resp, t1 - t0

def place_tp_limit(pair: str, tp_price: str, volume: str):
    data = {"pair": pair, "type": "sell", "ordertype": "limit", "price": tp_price, "volume": volume}
    t0 = time.monotonic(); resp = _private_post("AddOrder", data); t1 = time.monotonic()
    return resp, t1 - t0

# ---------- Post-only price helpers ----------
def compute_initial_limit(side: str, entry: float, offset_bps: float, best_bid: float, best_ask: float, tick: float) -> float:
    return max(min(entry * (1 - offset_bps / 1e4), best_ask - tick), tick) if side == "buy" else max(best_bid + tick, entry * (1 + offset_bps / 1e4))

def compute_reprice(side: str, prev_limit: float, step_bps: float, best_bid: float, best_ask: float, tick: float) -> float:
    return min(prev_limit * (1 + step_bps / 1e4), best_ask - tick) if side == "buy" else max(prev_limit * (1 - step_bps / 1e4), best_bid + tick)

# ---------- Main ----------
def main():
    if C.DRY_RUN: raise SystemExit("DRY_RUN is True in config. Set live.dry_run: false to place live orders.")
    if not API_KEY or not API_SECRET: raise SystemExit("Missing Kraken API credentials in .env")
    cap_usd = float(getattr(C, "SINGLE_TRADE_CAP_USD", 25.0))

    print("[run_live] Running strategy to generate ticket...")
    runpy.run_path("run_strategy.py", run_name="__main__")

    if not os.path.exists(TICKETS): raise SystemExit("No trade_tickets_latest.csv found.")
    df = pd.read_csv(TICKETS)
    if df.empty: raise SystemExit("trade_tickets_latest.csv is empty.")
    row = df.iloc[0]

    # Decision timestamp
    algo_decision_wall_utc = datetime.now(timezone.utc).isoformat()
    t_decide = time.monotonic()

    pair = str(row["kraken_pair"])
    side = str(row["side"]).strip().lower()
    entry = float(row["entry_price"]); stop_f = float(row["stop"]); tp_f = float(row["take_profit"])
    qty_f = float(row["qty"]); coin = str(row["coin_id"]); status = str(row.get("status", "SUGGESTED"))
    if side not in ("buy","sell"): raise SystemExit(f"Unexpected side: {side}")

    meta = fetch_pair_meta(pair)
    pdp, ldp = int(meta["pair_decimals"]), int(meta["lot_decimals"])
    tick = meta["tick_size"] if meta["tick_size"] > 0 else 10 ** (-pdp)
    best_bid, best_ask = fetch_best_bid_ask(pair)
    print(f"[run_live] Pair meta: pair_decimals={pdp}, lot_decimals={ldp}, ordermin={meta.get('ordermin')}, costmin={meta.get('costmin')}, tick={tick}")
    print(f"[run_live] Top-of-book: bid={best_bid:.{pdp}f} ask={best_ask:.{pdp}f}")

    qty_use, _ = apply_mins_and_precisions(entry, qty_f, cap_usd, meta)
    volume_str = fmt_price(qty_use, ldp)

    initial_limit_f = compute_initial_limit(side, entry, ENTRY_LIMIT_OFFSET_BPS, best_bid, best_ask, tick)
    limit_price = fmt_price(initial_limit_f, pdp)
    stop_price  = fmt_price(stop_f, pdp)
    tp_price    = fmt_price(tp_f, pdp)

    # Place entry (NO attached close)
    print(f"[run_live] LIMIT (POST-ONLY) {side.upper()} {pair} @ {limit_price} qty={volume_str}")
    resp_entry, exec_secs_entry = place_limit_post_only(pair, side, limit_price, volume_str)
    entry_txids = resp_entry.get("result", {}).get("txid", [])
    if not entry_txids: raise SystemExit(f"Entry placement returned no txid: {resp_entry}")
    entry_txid = entry_txids[0]
    t_accept = time.monotonic()
    entry_accepted_wall_utc = datetime.now(timezone.utc).isoformat()
    decision_to_accept_secs = t_accept - t_decide
    print(f"[run_live] Entry accepted. txid={entry_txid} submit_time={exec_secs_entry:.3f}s (decision->accept={decision_to_accept_secs:.3f}s)")

    # Reprice loop (maker only)
    start_ts = time.time()
    placed_limit = initial_limit_f
    reprice_count = 0
    filled_qty = 0.0
    intended_qty = qty_use

    time.sleep(POLL_BASE_SEC)
    while time.time() - start_ts < FILL_TIMEOUT_SEC:
        q = query_orders([entry_txid]).get(entry_txid, {})
        status_q = q.get("status", "")
        vol_exec = float(q.get("vol_exec", "0") or 0.0)
        vol      = float(q.get("vol", "0") or 0.0)

        if status_q == "closed" or abs(vol_exec - vol) < 1e-9:
            filled_qty = vol_exec
            print(f"[run_live] Entry filled. qty={filled_qty:.8f}")
            break

        zero_fill = vol_exec < 1e-12
        if REPRICE_ENABLED and zero_fill and reprice_count < REPRICE_STEPS:
            best_bid, best_ask = fetch_best_bid_ask(pair)
            new_limit_f = compute_reprice(side, placed_limit, REPRICE_STEP_BPS, best_bid, best_ask, tick)
            if abs(new_limit_f - placed_limit) >= tick / 2:
                new_limit = fmt_price(new_limit_f, pdp)
                print(f"[run_live] Reprice {reprice_count+1}/{REPRICE_STEPS}: cancel {entry_txid}, new limit={new_limit} (bid={best_bid:.{pdp}f} ask={best_ask:.{pdp}f})")
                try: cancel_order(entry_txid)
                except Exception as e: print(f"[run_live] Cancel error (continuing): {e}")
                resp_entry, _ = place_limit_post_only(pair, side, new_limit, volume_str)
                entry_txids = resp_entry.get("result", {}).get("txid", [])
                if entry_txids:
                    entry_txid = entry_txids[0]
                    placed_limit = new_limit_f
                    reprice_count += 1
                    time.sleep(REPRICE_INTERVAL_SEC)
                    continue
        time.sleep(REPRICE_INTERVAL_SEC)

    tp_txids, stop_txid = [], ""
    final_state = "filled" if filled_qty > 0 else "canceled_unfilled"

    if filled_qty > 0.0:
        # Immediately protect with MARKET stop-loss (independent order)
        print(f"[run_live] Placing STOP-LOSS MARKET sell {pair} @ {stop_price} qty={volume_str}")
        resp_stop, _ = place_stop_loss_market(pair, volume_str, stop_price)
        stop_txids = resp_stop.get("result", {}).get("txid", [])
        if not stop_txids:
            print(f"[run_live] WARNING: stop placement returned no txid: {resp_stop}")
        else:
            stop_txid = stop_txids[0]
            print(f"[run_live] Stop accepted. txid={stop_txid}")

        # Optional TP monitor (cancel stop, then place TP when bid >= TP)
        if TP_MONITOR_ENABLED:
            print(f"[run_live] TP monitor active for up to {TP_MONITOR_MAX_SEC}s; will cancel stop and place TP @ {tp_price} if bid >= TP.")
            t0 = time.time()
            while time.time() - t0 < TP_MONITOR_MAX_SEC:
                # If stop already triggered, we’re done
                if stop_txid:
                    q = query_orders([stop_txid]).get(stop_txid, {})
                    if q.get("status") == "closed":
                        print("[run_live] Stop filled. Exiting TP monitor.")
                        break
                # Check bid vs TP
                bid, ask = fetch_best_bid_ask(pair)
                if bid >= float(tp_price):
                    print(f"[run_live] Bid {bid:.{pdp}f} >= TP {tp_price}. Canceling stop and placing TP LIMIT.")
                    if stop_txid:
                        try: cancel_order(stop_txid); print(f"[run_live] Canceled stop {stop_txid}.")
                        except Exception as e: print(f"[run_live] Cancel stop error (continuing): {e}")
                    # Size TP to current balance (avoid oversell)
                    base = _base_from_pair(pair)
                    avail = float(get_balance().get(base, 0.0))
                    tp_qty = max(0.0, min(avail, filled_qty) * 0.9975)
                    tp_qty_str = fmt_price(round_qty_down(tp_qty, int(meta["lot_decimals"])), int(meta["lot_decimals"]))
                    resp_tp, exec_secs_tp = place_tp_limit(pair, tp_price, tp_qty_str)
                    tp_txids = resp_tp.get("result", {}).get("txid", [])
                    print(f"[run_live] TP accepted. txids={tp_txids} submit_time={exec_secs_tp:.3f}")
                    break
                time.sleep(TP_MONITOR_POLL_SEC)
            else:
                print("[run_live] TP monitor timeout elapsed; stop remains active.")

    else:
        # Not filled within window — cancel last resting order and exit
        try:
            cancel_order(entry_txid)
            print(f"[run_live] Unfilled after timeout — canceled order {entry_txid}.")
        except Exception as e:
            print(f"[run_live] Cancel at timeout error (continuing): {e}")

    # Logs
    now = datetime.now(timezone.utc).isoformat()
    exec_row = {
        "ts_utc": now, "coin_id": coin, "pair": pair, "side": side,
        "qty": fmt_price(qty_use, int(meta["lot_decimals"])),
        "entry_price_requested": fmt_price(entry, pdp),
        "limit_price_sent": fmt_price(placed_limit, pdp),
        "stop_price": fmt_price(stop_f, pdp), "tp_price": fmt_price(tp_f, pdp),
        "entry_txid": entry_txid, "stop_txid": stop_txid,
        "tp_txids": "|".join(tp_txids),
        "entry_submit_secs": f"{exec_secs_entry:.3f}",
        "algo_decision_ts_utc": algo_decision_wall_utc,
        "entry_accepted_ts_utc": entry_accepted_wall_utc,
        "decision_to_accept_secs": f"{decision_to_accept_secs:.3f}",
        "fill_wait_seconds": f"{min(FILL_TIMEOUT_SEC, max(0,int(time.time()-start_ts)))}",
        "ticket_status": status, "reprices": reprice_count, "final_state": final_state,
        "tp_monitor": "on" if TP_MONITOR_ENABLED else "off"
    }
    _append_csv(EXEC_LOG, exec_row)

    pnl_row = {
        "ts_utc": now, "coin_id": coin, "pair": pair, "side": side,
        "qty": fmt_price(qty_use, int(meta["lot_decimals"])),
        "entry_price": fmt_price(entry, pdp), "stop_price": fmt_price(stop_f, pdp),
        "tp_price": fmt_price(tp_f, pdp), "entry_notional_usd": fmt_price(qty_use * entry, 2),
        "realized_pnl_usd": "", "unrealized_pnl_usd": "", "entry_txid": entry_txid,
    }
    _append_csv(PNL_LOG, pnl_row)

    print(f"[run_live] Logged execution to {EXEC_LOG} and PnL placeholder to {PNL_LOG}")
    print("[run_live] Done.")

if __name__ == "__main__":
    main()
