from __future__ import annotations

import csv
import json
import os
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from broker import coinbase_private as cb_priv  # type: ignore
from broker import coinbase_public as cb_pub  # type: ignore
from owcg_utils.precision import round_price, round_size

OUTDIR = "output_stables"
STATE_PATH = os.path.join(OUTDIR, "state.jsonl")
RESERVE_LOG = os.path.join(OUTDIR, "reserve_actions.csv")
SUBMITTER_STATE_PATH = os.path.join(OUTDIR, "submitter_state.json")
POLL_SECS = int(os.getenv("CONTROLLER_POLL_SECS", "10"))
RESERVE_ORDER_PREFIX = os.getenv("RESERVE_ORDER_PREFIX", "reserve-")


def q(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _ensure_outdir() -> None:
    os.makedirs(OUTDIR, exist_ok=True)


def _products() -> List[str]:
    products = cb_pub.resolve_reserve_products()
    if not products:
        raise RuntimeError("No reserve products resolved. Check RESERVE_PRODUCTS / STABLES_PRODUCTS.")
    return products


def _write_jsonl(path: str, obj: Dict[str, Any]) -> None:
    _ensure_outdir()
    with open(path, "a") as handle:
        handle.write(json.dumps(obj) + "\n")


def _append_csv(path: str, row: Dict[str, str]) -> None:
    _ensure_outdir()
    exists = os.path.exists(path)
    with open(path, "a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def _balances() -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for entry in cb_priv.get_balances():
        symbol = entry.get("currency") or entry.get("asset") or entry.get("symbol")
        value = entry.get("available") or entry.get("available_balance") or entry.get("available_for_trading")
        if isinstance(value, dict):
            value = value.get("value")
        if symbol and value is not None:
            try:
                out[str(symbol)] = Decimal(str(value))
            except Exception:
                pass
    return out


def _product_map() -> Dict[str, Dict[str, Any]]:
    return {str(p.get("product_id")): p for p in cb_pub.get_products() if p.get("product_id")}


def _base_quote(product_id: str, products: Dict[str, Dict[str, Any]]) -> Tuple[str, str]:
    product = products.get(product_id)
    if not product:
        raise ValueError(f"Unknown product_id {product_id}")
    base = str(product.get("base_currency_id") or product.get("base_currency") or "")
    quote = str(product.get("quote_currency_id") or product.get("quote_currency") or "")
    if not base or not quote:
        raise ValueError(f"Could not determine base/quote for {product_id}")
    return base, quote


def _min_balance(symbol: str) -> Decimal:
    return Decimal(os.getenv(f"RESERVE_MIN_{symbol}", "50"))


def _max_topup_unit_usd() -> Decimal:
    return Decimal(os.getenv("RESERVE_TOPUP_USD", "50"))


def _reserve_order_open(product_id: str) -> bool:
    try:
        orders = cb_priv.get_open_orders(product_id=product_id)
    except Exception:
        return False
    for order in orders:
        client_id = str(order.get("client_order_id") or order.get("client_oid") or "")
        if client_id.startswith(RESERVE_ORDER_PREFIX):
            return True
    return False


def _load_submitter_state() -> Dict[str, Any]:
    if not os.path.exists(SUBMITTER_STATE_PATH):
        return {"stage": "IDLE"}
    try:
        with open(SUBMITTER_STATE_PATH) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {"stage": "IDLE"}
    except Exception:
        return {"stage": "IDLE"}


def _busy_products() -> List[str]:
    state = _load_submitter_state()
    if str(state.get("stage") or "IDLE") == "IDLE":
        return []
    product = str(state.get("product_id") or "")
    return [product] if product else []


def _submit_limit(product_id: str, side: str, usd_notional: Decimal, client_suffix: str) -> Optional[str]:
    products = _product_map()
    product = products.get(product_id)
    if not product:
        return None
    price_inc = q(product.get("price_increment") or product.get("quote_increment") or "0.0001")
    base_inc = q(product.get("base_increment") or "0.01")
    min_size = q(product.get("min_order_size") or product.get("base_min_size") or "0")
    raw = cb_pub.get_maker_limit_price(product_id, side)
    price = round_price(raw, price_inc, mode="down" if side.upper() == "BUY" else "up")
    size = round_size(usd_notional / price, base_inc, mode="down") if price > 0 else Decimal("0")
    if size < min_size:
        return None
    client_order_id = f"{RESERVE_ORDER_PREFIX}{client_suffix}-{int(time.time())}"
    ok, resp = cb_priv.place_limit_order(
        product_id=product_id,
        side=side,
        size=str(size),
        limit_price=str(price),
        post_only=True,
        client_order_id=client_order_id,
    )
    if not ok:
        return None
    return str(resp.get("order_id") or (resp.get("success_response") or {}).get("order_id") or "")


def _reserve_actions(balances: Dict[str, Decimal], busy_products: List[str]) -> List[str]:
    actions: List[str] = []
    products = _product_map()
    usd_floor = _min_balance("USD")
    max_unit = _max_topup_unit_usd()
    busy = set(busy_products)

    if balances.get("USD", Decimal("0")) < usd_floor:
        usd_shortfall = usd_floor - balances.get("USD", Decimal("0"))
        for product_id in _products():
            if product_id in busy or _reserve_order_open(product_id):
                continue
            base, quote = _base_quote(product_id, products)
            if quote != "USD":
                continue
            base_floor = _min_balance(base)
            available_to_sell = balances.get(base, Decimal("0")) - base_floor
            if available_to_sell <= 0:
                continue
            price = cb_pub.get_maker_limit_price(product_id, "SELL")
            notional_capacity = available_to_sell * price
            usd_notional = min(max_unit, usd_shortfall, notional_capacity)
            if usd_notional <= 0:
                continue
            order_id = _submit_limit(product_id, "SELL", usd_notional, f"raise-usd-{base.lower()}")
            if order_id:
                actions.append(f"SELL {product_id} raise USD order_id={order_id}")
                return actions

    for product_id in _products():
        if product_id in busy or _reserve_order_open(product_id):
            continue
        base, quote = _base_quote(product_id, products)
        if quote != "USD":
            continue
        base_floor = _min_balance(base)
        current_base = balances.get(base, Decimal("0"))
        if current_base >= base_floor:
            continue
        price = cb_pub.get_maker_limit_price(product_id, "BUY")
        usd_shortfall = (base_floor - current_base) * price
        usd_available = balances.get("USD", Decimal("0")) - usd_floor
        usd_notional = min(max_unit, usd_shortfall, usd_available)
        if usd_notional <= 0:
            continue
        order_id = _submit_limit(product_id, "BUY", usd_notional, f"topup-{base.lower()}")
        if order_id:
            actions.append(f"BUY {product_id} top-up order_id={order_id}")

    return actions


def main() -> None:
    chosen = _products()
    print(f"[controller] reserve controller running for {', '.join(chosen)}; Ctrl+C to stop.")
    while True:
        try:
            balances = _balances()
            busy_products = _busy_products()
            actions = _reserve_actions(balances, busy_products)
            state_row = {
                "ts": int(time.time()),
                "products": chosen,
                "busy_products": busy_products,
                "balances": {k: str(v) for k, v in balances.items()},
                "actions": actions,
            }
            _write_jsonl(STATE_PATH, state_row)
            if actions:
                _append_csv(RESERVE_LOG, {"ts": str(state_row["ts"]), "actions": " | ".join(actions)})
                print(f"[controller] {' | '.join(actions)}")
        except KeyboardInterrupt:
            print("[controller] stopping")
            break
        except Exception as exc:
            _write_jsonl(STATE_PATH, {"ts": int(time.time()), "error": str(exc)})
            print(f"[controller] error: {exc}")
        finally:
            time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()