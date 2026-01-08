# run_controller_stables.py
from __future__ import annotations

import os, time, csv, json
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Dict, Any, Optional, List

STATE_PATH   = "output_stables/state.jsonl"
TICKET_PATH  = "output_stables/trade_tickets_latest.csv"
RESERVE_LOG  = "output_stables/reserve_actions.csv"
POLL_SECS    = int(os.getenv("CONTROLLER_POLL_SECS", "10"))

# Reserve policy
MIN_USD     = Decimal(os.getenv("RESERVE_MIN_USD",  "50"))
MIN_USDT    = Decimal(os.getenv("RESERVE_MIN_USDT","50"))
MIN_USDC    = Decimal(os.getenv("RESERVE_MIN_USDC","50"))
TOPUP_UNIT  = Decimal(os.getenv("RESERVE_TOPUP_USD","50"))  # per action, in USD notional

# ---- Broker adaptors ----
from broker import coinbase_private as cb_priv  # type: ignore
from broker import coinbase_public  as cb_pub   # type: ignore

def q(x) -> Decimal: return x if isinstance(x, Decimal) else Decimal(str(x))

def _write_jsonl(path: str, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")

def _balances() -> Dict[str, Decimal]:
    bals = cb_priv.get_balances()
    out: Dict[str, Decimal] = {}
    for b in bals:
        sym = b.get("currency") or b.get("asset") or b.get("symbol")
        val = b.get("available") or b.get("available_balance") or b.get("available_for_trading")
        if isinstance(val, dict): val = val.get("value")
        if not sym or val is None: continue
        try: out[str(sym)] = Decimal(str(val))
        except Exception: pass
    return out

def _reserve_topup_log(row: Dict[str,str]) -> None:
    exists = os.path.exists(RESERVE_LOG)
    with open(RESERVE_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists: w.writeheader()
        w.writerow(row)

def _post_maker_buy(product_id: str, usd_notional: Decimal) -> Optional[str]:
    # product like USDT-USD / USDC-USD
    bid, ask = cb_pub.get_best_bid_ask(product_id)
    price_inc = q(next((p for p in cb_pub.get_products() if p.get("product_id")==product_id), {}).get("price_increment", "0.0001"))
    base_inc  = q(next((p for p in cb_pub.get_products() if p.get("product_id")==product_id), {}).get("base_increment", "0.01"))
    # try paying near bid
    price = Decimal(str(bid))
    size  = (usd_notional / price).quantize(base_inc)
    ok, resp = cb_priv.place_limit_order(product_id, side="BUY", size=str(size), limit_price=str(price), post_only=True, client_order_id=f"reserve-{int(time.time())}")
    if ok:
        return resp.get("order_id")
    return None

def _ensure_reserves(bals: Dict[str, Decimal]) -> None:
    actions: List[str] = []
    # maintain USD for BUY legs & top-ups
    if bals.get("USD", Decimal("0")) < MIN_USD:
        # attempt to SELL a bit of USDT->USD or USDC->USD to raise USD
        for base in ("USDT-USD","USDC-USD"):
            # if we have base > threshold, sell TOPUP_UNIT worth
            asset = base.split("-")[0]
            if bals.get(asset, Decimal("0")) > Decimal("1"):
                bid, ask = cb_pub.get_best_bid_ask(base)
                price = Decimal(str(bid))
                size  = (TOPUP_UNIT / price)
                cb_priv.place_limit_order(base, side="SELL", size=str(size), limit_price=str(price), post_only=True, client_order_id=f"usd-raise-{int(time.time())}")
                actions.append(f"SELL {asset}->{base.split('-')[1]} {size}@{price}")
                break

    # keep USDT supply
    if bals.get("USDT", Decimal("0")) < MIN_USDT and bals.get("USD", Decimal("0")) >= TOPUP_UNIT:
        oid = _post_maker_buy("USDT-USD", TOPUP_UNIT)
        if oid: actions.append(f"BUY USDT-USD ${TOPUP_UNIT} oid={oid}")

    # keep USDC supply
    if bals.get("USDC", Decimal("0")) < MIN_USDC and bals.get("USD", Decimal("0")) >= TOPUP_UNIT:
        oid = _post_maker_buy("USDC-USD", TOPUP_UNIT)
        if oid: actions.append(f"BUY USDC-USD ${TOPUP_UNIT} oid={oid}")

    if actions:
        _reserve_topup_log({"ts": str(int(time.time())), "actions": " | ".join(actions)})

def _poison_old_tickets() -> None:
    # optional: truncate the ticket file to avoid resubmitting stale ones in other processes
    try:
        with open(TICKET_PATH) as f:
            rows = list(csv.DictReader(f))
        if rows:
            rows[0]["reason"] = "consumed_by_controller"
            with open(TICKET_PATH, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader(); w.writerow(rows[0])
    except Exception:
        pass

def main():
    print("[controller] running; Ctrl+C to stop.")
    while True:
        try:
            bals = _balances()
            _ensure_reserves(bals)

            # Example loop could also check open orders, stale parents, etc.
            # Keep lightweight and let submitter handle placements.

            _write_jsonl(STATE_PATH, {"ts": int(time.time()), "balances": {k: str(v) for k,v in bals.items()}})

            _poison_old_tickets()  # avoid duplicate consumption if you run multiple processes

        except KeyboardInterrupt:
            print("[controller] stopping")
            break
        except Exception as e:
            _write_jsonl(STATE_PATH, {"ts": int(time.time()), "error": str(e)})
        finally:
            time.sleep(POLL_SECS)

if __name__ == "__main__":
    main()
