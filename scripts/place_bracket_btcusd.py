# scripts/place_bracket_btcusd.py — live tiny bracket using config & DRY_RUN
import time, math, sys
import config as C
from kraken_public import ticker_info, pair_decimals
from sizing import compute_order_for_pair
from broker.kraken_private import add_order, cancel_order, query_orders

PAIR = "XBTUSD"  # Kraken's altname for BTCUSD is typically XBTUSD; adjust if yours is BTCUSD
POLL_SEC = 5
POLL_TIMEOUT = 10 * 60

def _extract_txid(res) -> str:
    if isinstance(res, dict) and "txid" in res and isinstance(res["txid"], list) and res["txid"]:
        return res["txid"][0]
    for v in res.values():
        if isinstance(v, str) and v:
            return v
        if isinstance(v, list) and v and isinstance(v[0], str):
            return v[0]
    raise RuntimeError(f"Could not parse Kraken txid from: {res}")

def _round_price(p: float, decimals: int) -> float:
    q = 10 ** decimals
    return math.floor(p * q + 0.5) / q

def add_order_safe(**kwargs):
    if C.DRY_RUN:
        print("[DRY-RUN] AddOrder", kwargs)
        return {"txid": ["DRY-"+kwargs.get("type","") + "-"+kwargs.get("pair","")]}
    return add_order(**kwargs)

def cancel_order_safe(txid: str):
    if C.DRY_RUN:
        print("[DRY-RUN] CancelOrder", txid); return {"count":1}
    return cancel_order(txid)

def query_orders_safe(txids):
    if C.DRY_RUN:
        tid = txids[0] if isinstance(txids, (list,tuple)) else txids
        return {tid: {"status":"open","vol_exec":"0"}}
    return query_orders(txids)

def main():
    # Get bid/ask from Ticker
    t = ticker_info(PAIR)
    bid = float(t['b'][0]); ask = float(t['a'][0])
    mid = (bid + ask) / 2.0
    dec = pair_decimals(PAIR)
    entry = _round_price(bid, dec)

    # For quick live test: ATR_5m proxy (conservative). If you have an intraday series, use that instead.
    atr_5m = mid * 0.0025 / 20.0  # ~0.0125% of price per 5m as a safe floor

    order = compute_order_for_pair(PAIR, entry_price=entry, atr_5m=atr_5m, spread_median_bps=(ask-bid)/mid*10000.0)
    if order is None:
        print("Skip: order too small after caps/min-size. Tweak config.risk or caps.")
        sys.exit(0)

    qty = order["qty"]; stop = order["stop"]; target = order["target"]
    print(f"[SETUP] {PAIR} entry={entry} stop={stop} target={target} qty={qty} "
          f"(risk≈${order['realized_risk_usd']:.2f} intent ${order['intended_risk_usd']:.2f})")
    print(f"[COST]  {order['note']}  dry_run={C.DRY_RUN}")

    # Place entry
    res_entry = add_order_safe(pair=PAIR, type="buy", price=entry, volume=qty,
                               post_only=True, time_in_force="GTC", ordertype="limit")
    entry_txid = _extract_txid(res_entry)
    print(f"[ENTRY] LIMIT BUY {PAIR} txid={entry_txid}")

    if C.DRY_RUN:
        print("[DRY-RUN] Skipping fill/oco watcher.")
        return

    # Wait for fill
    start = time.time()
    while time.time() - start < POLL_TIMEOUT:
        q = query_orders_safe([entry_txid])
        od = q.get(entry_txid, {})
        if od.get("status") == "closed" and float(od.get("vol_exec", 0)) > 0:
            break
        time.sleep(POLL_SEC)

    # Place stop/TP
    res_stop = add_order_safe(pair=PAIR, type="sell", price=stop, volume=qty,
                              post_only=False, time_in_force="GTC", ordertype="stop-loss")
    stop_txid = _extract_txid(res_stop)
    res_tp   = add_order_safe(pair=PAIR, type="sell", price=target, volume=qty,
                              post_only=True, time_in_force="GTC", ordertype="limit")
    tp_txid = _extract_txid(res_tp)
    print(f"[BRACKET] stop={stop_txid}  tp={tp_txid}")

    # Simple OCO watcher
    print("[OCO] Watching...")
    while True:
        q = query_orders_safe([stop_txid, tp_txid])
        s_status = q.get(stop_txid,{}).get("status")
        t_status = q.get(tp_txid,{}).get("status")
        if s_status == "closed" and t_status != "closed":
            cancel_order_safe(tp_txid); print("[OCO] Stop filled → canceled TP."); break
        if t_status == "closed" and s_status != "closed":
            cancel_order_safe(stop_txid); print("[OCO] TP filled → canceled Stop."); break
        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
