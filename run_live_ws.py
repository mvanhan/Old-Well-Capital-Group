# run_live_ws.py
# Kraken Spot via WebSocket v2: maker LIMIT entry with a percentage STOP-LOSS as OTO "conditional".
# After the entry fills, place a TAKE-PROFIT LIMIT separately (since WS v2 supports only one conditional).
# No market fallback. Logs decision->ack time and execution metadata.

import os, time, hmac, base64, hashlib, urllib.parse as up, json
from datetime import datetime, timezone
import requests, pandas as pd, runpy
import ssl, certifi

import config as C

API_BASE = "https://api.kraken.com"
WS_V2_URL = "wss://ws-auth.kraken.com/v2"

EXEC_LOG = "logs/executions.csv"
PNL_LOG  = "logs/pnl_live.csv"
TICKETS  = "output/trade_tickets_latest.csv"

API_KEY = os.getenv("KRAKEN_API_KEY", "")
API_SECRET_RAW = os.getenv("KRAKEN_API_SECRET", "")

# ----- Maker / repricing knobs -----
ENTRY_LIMIT_OFFSET_BPS = 3
REPRICE_ENABLED = True
REPRICE_STEPS = 6
REPRICE_STEP_BPS = 2
REPRICE_INTERVAL_SEC = 10
FILL_TIMEOUT_SEC = 240

MIN_NOTIONAL_USD = 15.0

# ----- Private REST helpers -----
def _clean_key(s: str) -> str:
    return (s or "").strip().strip('"').strip("'").replace(" ", "")

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

API_KEY = _clean_key(API_KEY)
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
    nonce = _nonce()
    payload = {**data, "nonce": nonce}
    headers = {
        "API-Key": API_KEY,
        "API-Sign": _sign(path, payload),
        "User-Agent": "owcg-live-ws/1.2",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    r = requests.post(API_BASE + path, headers=headers, data=payload, timeout=20)
    r.raise_for_status()
    j = r.json()
    errs = j.get("error", []) or []
    if errs:
        raise RuntimeError(f"Kraken error: {errs}")
    return j

def get_ws_token() -> str:
    return _private_post("GetWebSocketsToken", {})["result"]["token"]

def fetch_pair_meta(pair: str) -> dict:
    r = requests.get(API_BASE + "/0/public/AssetPairs", params={"pair": pair}, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"AssetPairs error: {j['error']}")
    info = list(j.get("result", {}).values())[0]
    return {
        "pair_decimals": int(info.get("pair_decimals", 8)),
        "lot_decimals": int(info.get("lot_decimals", 8)),
        "ordermin": float(info.get("ordermin", "0") or 0.0),
        "costmin": float(info.get("costmin", "0") or 0.0),
        "tick_size": float(info.get("tick_size", "0") or 0.0),
        "wsname": info.get("wsname", ""),  # e.g., "FLOKI/USD"
    }

def fetch_best_bid_ask(pair: str) -> tuple[float, float]:
    r = requests.get(API_BASE + "/0/public/Ticker", params={"pair": pair}, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"Ticker error: {j['error']}")
    info = list(j.get("result", {}).values())[0]
    return float(info["b"][0]), float(info["a"][0])

def fmt_price(p: float, decimals: int) -> str:
    return f"{p:.{decimals}f}"

def round_qty_down(q: float, decimals: int) -> float:
    return float(f"{q:.{decimals}f}")

def apply_mins_and_precisions(entry: float, qty: float, cap_usd: float, meta: dict) -> tuple[float, float]:
    target_usd = max(min(entry * qty, cap_usd), MIN_NOTIONAL_USD)
    costmin = float(meta.get("costmin", 0.0) or 0.0)
    if costmin > 0:
        target_usd = max(target_usd, costmin)
    lot_decimals = int(meta.get("lot_decimals", 8))
    ordermin = float(meta.get("ordermin", 0.0) or 0.0)
    qty_target = max(target_usd / entry, ordermin if ordermin > 0 else 0.0)
    qty_rounded = round_qty_down(qty_target, lot_decimals)
    if qty_rounded == 0 and qty_target > 0:
        qty_rounded = float(fmt_price(qty_target, lot_decimals))
    return qty_rounded, target_usd

def compute_initial_limit(side: str, entry: float, offset_bps: float,
                          best_bid: float, best_ask: float, tick: float) -> float:
    if side == "buy":
        return max(min(entry * (1 - offset_bps / 1e4), best_ask - tick), tick)
    else:
        return min(max(entry * (1 + offset_bps / 1e4), best_bid + tick), 1e18)

def compute_reprice(side: str, prev_limit: float, step_bps: float,
                    best_bid: float, best_ask: float, tick: float) -> float:
    if side == "buy":
        return min(prev_limit * (1 + step_bps / 1e4), best_ask - tick)
    else:
        return max(prev_limit * (1 - step_bps / 1e4), best_bid + tick)

# ----- WS v2 add_order with ONE conditional (stop-loss %). -----
def ws_add_order_with_stop(symbol: str, side: str, limit_price: float, order_qty: float,
                           post_only: bool, sl_pct: float, token: str) -> dict:
    """
    Places a LIMIT (post-only) entry with ONE conditional secondary:
      - stop-loss (market) at sl_pct% (negative for buys, positive for sells).
    """
    from websocket import create_connection, WebSocketTimeoutException

    # sign for buys: negative % means trigger when price drops
    sl_pct_effective = -abs(sl_pct) if side == "buy" else abs(sl_pct)

    payload = {
        "method": "add_order",
        "params": {
            "token": token,
            "symbol": symbol,          # e.g., "FLOKI/USD"
            "side": side,              # "buy" or "sell"
            "order_type": "limit",
            "limit_price": float(limit_price),
            "order_qty": float(order_qty),
            "post_only": bool(post_only),
            "time_in_force": "gtc",
            "conditional": {
                "order_type": "stop-loss",
                "trigger_price": float(sl_pct_effective),
                "trigger_price_type": "pct"
            }
        }
    }

    ws = create_connection(
        WS_V2_URL,
        timeout=30,
        sslopt={"cert_reqs": ssl.CERT_REQUIRED, "ca_certs": certifi.where()}
    )
    try:
        ws.send(json.dumps(payload))
        t0 = time.monotonic()
        while True:
            try:
                msg = ws.recv()
            except WebSocketTimeoutException:
                raise RuntimeError("WS add_order timed out waiting for ack. (Network hiccup or payload rejected)")
            t1 = time.monotonic()
            try:
                j = json.loads(msg)
            except Exception:
                continue
            # Kraken v2 returns dicts with success/error; accept either "result" or "error".
            if isinstance(j, dict) and ("result" in j or "error" in j or "success" in j):
                j["latency_secs"] = t1 - t0
                return j
    finally:
        ws.close()

def append_csv(path: str, row: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df = pd.DataFrame([row])
    if not os.path.exists(path): df.to_csv(path, index=False)
    else: df.to_csv(path, mode="a", header=False, index=False)

def place_tp_limit_rest(pair: str, price_abs: float, qty: float, pdp: int, ldp: int, side_entry: str):
    # After a BUY entry fills, we place a SELL limit TP. (For sell entries, invert.)
    side_tp = "sell" if side_entry == "buy" else "buy"
    data = {
        "pair": pair,
        "type": side_tp,
        "ordertype": "limit",
        "price": fmt_price(price_abs, pdp),
        "volume": fmt_price(qty, ldp),
        # reduce_only not supported for Spot; omit.
        "timeinforce": "GTC",
        "trading_agreement": "agree"
    }
    j = _private_post("AddOrder", data)
    return j["result"]

def main():
    if C.DRY_RUN:
        raise SystemExit("DRY_RUN is True in config. Set live.dry_run: false to place live orders.")
    if not API_KEY or not API_SECRET_RAW:
        raise SystemExit("Missing Kraken API credentials in .env")

    cap_usd = float(getattr(C, "SINGLE_TRADE_CAP_USD", 25.0))

    # 1) Generate ticket
    print("[run_live_ws] Running strategy to generate ticket...")
    runpy.run_path("run_strategy.py", run_name="__main__")
    if not os.path.exists(TICKETS): raise SystemExit("No trade_tickets_latest.csv found.")
    df = pd.read_csv(TICKETS)
    if df.empty: raise SystemExit("trade_tickets_latest.csv is empty.")

    row = df.iloc[0]
    pair = str(row["kraken_pair"])
    side = str(row["side"]).strip().lower()
    entry = float(row["entry_price"])
    stop_abs = float(row["stop"])
    tp_abs   = float(row["take_profit"])
    qty_f    = float(row["qty"])
    coin     = str(row["coin_id"])

    # 2) Meta + book
    meta = fetch_pair_meta(pair)
    ws_symbol = meta["wsname"] or pair.replace("USD", "/USD")
    pdp, ldp = int(meta["pair_decimals"]), int(meta["lot_decimals"])
    tick = meta["tick_size"] if meta["tick_size"] > 0 else 10 ** (-pdp)
    best_bid, best_ask = fetch_best_bid_ask(pair)
    print(f"[run_live_ws] Pair: {ws_symbol} (pair_decimals={pdp}, lot_decimals={ldp}, tick={tick})")
    print(f"[run_live_ws] Top-of-book: bid={best_bid:.{pdp}f} ask={best_ask:.{pdp}f}")

    qty_use, _ = apply_mins_and_precisions(entry, qty_f, cap_usd, meta)
    volume_disp = fmt_price(qty_use, ldp)

    # 3) % offsets (derived from absolute levels in the ticket)
    tp_pct  = (tp_abs / entry - 1.0) * 100.0
    sl_pct  = (stop_abs / entry - 1.0) * 100.0

    # 4) Initial maker limit
    initial_limit_f = compute_initial_limit(side, entry, ENTRY_LIMIT_OFFSET_BPS, best_bid, best_ask, tick)
    placed_limit = initial_limit_f
    limit_str = fmt_price(placed_limit, pdp)

    # timing anchor
    t_decide = time.monotonic()

    print(f"[run_live_ws] LIMIT (POST-ONLY) {side.upper()} {ws_symbol} @ {limit_str} qty={volume_disp} "
          f"with SL {sl_pct:+.3f}% (attached) and TP {tp_pct:+.3f}% (will place after fill)")

    # 5) WS token + submit
    token = get_ws_token()
    ws_ack = ws_add_order_with_stop(
        symbol=ws_symbol,
        side=side,
        limit_price=placed_limit,
        order_qty=qty_use,
        post_only=True,
        sl_pct=sl_pct,
        token=token
    )
    decision_to_accept_secs = float(ws_ack.get("latency_secs", 0.0) or 0.0)
    print(f"[run_live_ws] WS add_order ack in {decision_to_accept_secs:.3f}s "
          f"-> success={ws_ack.get('success')} err={ws_ack.get('error')}")

    # Extract order id
    result = ws_ack.get("result", {}) if isinstance(ws_ack.get("result"), dict) else ws_ack
    entry_id = (
        result.get("order_id")
        or result.get("txid")
        or (result.get("orders", [{}])[0].get("order_id") if isinstance(result.get("orders"), list) else None)
    )
    if not entry_id:
        raise SystemExit(f"Could not find order id in ws response: {ws_ack}")

    # 6) Reprice loop (maker only)
    reprice_count = 0
    start_ts = time.time()
    while time.time() - start_ts < FILL_TIMEOUT_SEC:
        q = _private_post("QueryOrders", {"txid": entry_id})["result"].get(entry_id, {})
        status_q = q.get("status", "")
        vol_exec = float(q.get("vol_exec", "0") or 0.0)
        vol      = float(q.get("vol", "0") or 0.0)
        if status_q == "closed" or (vol > 0 and abs(vol_exec - vol) < 1e-9):
            print(f"[run_live_ws] Entry filled. qty={vol_exec}")
            break

        zero_fill = vol_exec < 1e-12
        if REPRICE_ENABLED and zero_fill and reprice_count < REPRICE_STEPS:
            best_bid, best_ask = fetch_best_bid_ask(pair)
            new_limit_f = compute_reprice(side, placed_limit, REPRICE_STEP_BPS, best_bid, best_ask, tick)
            if abs(new_limit_f - placed_limit) >= tick / 2:
                new_limit_str = fmt_price(new_limit_f, pdp)
                print(f"[run_live_ws] Reprice {reprice_count+1}/{REPRICE_STEPS}: cancel {entry_id}, new limit={new_limit_str} "
                      f"(bid={best_bid:.{pdp}f} ask={best_ask:.{pdp}f})")
                try:
                    _private_post("CancelOrder", {"txid": entry_id})
                except Exception as e:
                    print(f"[run_live_ws] Cancel error (continuing): {e}")
                token = get_ws_token()
                ws_ack = ws_add_order_with_stop(
                    symbol=ws_symbol,
                    side=side,
                    limit_price=new_limit_f,
                    order_qty=qty_use,
                    post_only=True,
                    sl_pct=sl_pct,
                    token=token
                )
                result = ws_ack.get("result", {}) if isinstance(ws_ack.get("result"), dict) else ws_ack
                new_entry_id = (
                    result.get("order_id")
                    or result.get("txid")
                    or (result.get("orders", [{}])[0].get("order_id") if isinstance(result.get("orders"), list) else None)
                )
                if new_entry_id:
                    entry_id = new_entry_id
                placed_limit = new_limit_f
                reprice_count += 1
                time.sleep(REPRICE_INTERVAL_SEC)
                continue

        time.sleep(REPRICE_INTERVAL_SEC)

    # 7) If unfilled -> cancel. If filled -> post TP limit immediately.
    q = _private_post("QueryOrders", {"txid": entry_id})["result"].get(entry_id, {})
    status_q = q.get("status", "")
    vol_exec = float(q.get("vol_exec", "0") or 0.0)
    final_state = "filled" if status_q == "closed" or vol_exec > 0 else "canceled_unfilled"
    if final_state != "filled":
        try:
            _private_post("CancelOrder", {"txid": entry_id})
            print(f"[run_live_ws] Unfilled after timeout — canceled order {entry_id}.")
        except Exception as e:
            print(f"[run_live_ws] Cancel at timeout error (continuing): {e}")
    else:
        # Place TP LIMIT for executed quantity
        tp_res = place_tp_limit_rest(pair, tp_abs, vol_exec, pdp, ldp, side_entry=side)
        print(f"[run_live_ws] TP LIMIT placed: {tp_res}")

    # 8) Logging
    now = datetime.now(timezone.utc).isoformat()
    exec_row = {
        "ts_utc": now,
        "coin_id": coin,
        "pair": pair,
        "side": side,
        "qty": fmt_price(round_qty_down(qty_use, ldp), ldp),
        "entry_price_requested": fmt_price(entry, pdp),
        "limit_price_sent": fmt_price(placed_limit, pdp),
        "tp_pct": f"{tp_pct:+.4f}",
        "sl_pct": f"{sl_pct:+.4f}",
        "entry_order_id": entry_id,
        "decision_to_accept_secs": f"{(time.monotonic()-t_decide):.3f}",
        "reprices": reprice_count,
        "final_state": final_state,
    }
    append_csv(EXEC_LOG, exec_row)

    pnl_row = {
        "ts_utc": now,
        "coin_id": coin,
        "pair": pair,
        "side": side,
        "qty": fmt_price(round_qty_down(qty_use, ldp), ldp),
        "entry_price": fmt_price(entry, pdp),
        "tp_price_abs": fmt_price(tp_abs, pdp),
        "stop_price_abs": fmt_price(stop_abs, pdp),
        "entry_notional_usd": fmt_price(qty_use * entry, 2),
        "realized_pnl_usd": "",
        "unrealized_pnl_usd": "",
        "entry_order_id": entry_id,
    }
    append_csv(PNL_LOG, pnl_row)

    print(f"[run_live_ws] Logged execution to {EXEC_LOG} and PnL placeholder to {PNL_LOG}")
    print("[run_live_ws] Done.")

if __name__ == "__main__":
    main()
