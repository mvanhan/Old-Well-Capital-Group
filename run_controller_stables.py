from __future__ import annotations

import csv
import json
import os
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from broker import coinbase_private as cb_priv  # type: ignore
from broker import coinbase_public as cb_pub  # type: ignore
from owcg_utils.precision import round_price, round_size

OUTDIR = "output_stables"
STATE_PATH = os.path.join(OUTDIR, "state.jsonl")
RESERVE_LOG = os.path.join(OUTDIR, "reserve_actions.csv")
SUBMITTER_STATE_PATH = os.path.join(OUTDIR, "submitter_state.json")

POLL_SECS = int(os.getenv("CONTROLLER_POLL_SECS", "10"))
PRODUCT_REFRESH_SECS = int(os.getenv("RESERVE_PRODUCT_REFRESH_SECS", "60"))
RESERVE_ORDER_PREFIX = os.getenv("RESERVE_ORDER_PREFIX", "reserve-")

DEFAULT_FUNDING_ASSETS = {"USD", "USDC", "USDT", "DAI", "PYUSD", "FDUSD", "USDP", "GUSD", "TUSD", "RLUSD"}
DEFAULT_TARGET_ASSET_PRIORITY = ["USD", "USDC", "USDT", "DAI", "PYUSD", "FDUSD", "USDP", "GUSD", "TUSD", "RLUSD"]


def q(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _env_set(name: str, default: Set[str]) -> Set[str]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return set(default)
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


def _env_list(name: str, default: Sequence[str]) -> List[str]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return [str(item).upper() for item in default]
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def _ensure_outdir() -> None:
    os.makedirs(OUTDIR, exist_ok=True)


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


def _now() -> int:
    return int(time.time())


def _balances() -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for entry in cb_priv.get_balances():
        symbol = entry.get("currency") or entry.get("asset") or entry.get("symbol")
        value = entry.get("available") or entry.get("available_balance") or entry.get("available_for_trading")
        if isinstance(value, dict):
            value = value.get("value")
        if symbol and value is not None:
            try:
                out[str(symbol).upper()] = Decimal(str(value))
            except Exception:
                pass
    return out


def _product_map() -> Dict[str, Dict[str, Any]]:
    return {str(p.get("product_id") or "").upper(): p for p in cb_pub.get_tradable_products() if p.get("product_id")}


def _base_quote(product_id: str, products: Dict[str, Dict[str, Any]]) -> Tuple[str, str]:
    product = products.get(product_id.upper())
    if not product:
        raise ValueError(f"Unknown product_id {product_id}")
    base = str(product.get("base_currency_id") or product.get("base_currency") or "").upper()
    quote = str(product.get("quote_currency_id") or product.get("quote_currency") or "").upper()
    if not base or not quote:
        raise ValueError(f"Could not determine base/quote for {product_id}")
    return base, quote


def _min_balance(symbol: str) -> Decimal:
    specific = os.getenv(f"RESERVE_MIN_{symbol.upper()}", "")
    if specific.strip():
        return Decimal(specific)
    return Decimal(os.getenv("RESERVE_MIN_DEFAULT", "50"))


def _max_topup_unit_usd() -> Decimal:
    return Decimal(os.getenv("RESERVE_TOPUP_USD", "50"))


def _funding_assets() -> Set[str]:
    return _env_set("RESERVE_FUNDING_ASSETS", DEFAULT_FUNDING_ASSETS)


def _target_asset_priority() -> List[str]:
    return _env_list("RESERVE_TARGET_PRIORITY", DEFAULT_TARGET_ASSET_PRIORITY)


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
    if str(state.get("stage") or "IDLE").upper() == "IDLE":
        return []
    product = str(state.get("product_id") or "").upper()
    return [product] if product else []


def _submit_limit(
    product_id: str,
    side: str,
    quote_notional: Decimal,
    client_suffix: str,
    products: Dict[str, Dict[str, Any]],
) -> Optional[Tuple[str, Decimal, Decimal]]:
    product = products.get(product_id.upper())
    if not product:
        return None

    price_inc = q(product.get("price_increment") or product.get("quote_increment") or "0.0001")
    base_inc = q(product.get("base_increment") or "0.01")
    min_size = q(product.get("min_order_size") or product.get("base_min_size") or "0")

    raw = cb_pub.get_maker_limit_price(product_id, side)
    price = round_price(raw, price_inc, mode="down" if side.upper() == "BUY" else "up")
    if price <= 0:
        return None

    size = round_size(quote_notional / price, base_inc, mode="down")
    if size < min_size:
        return None

    client_order_id = f"{RESERVE_ORDER_PREFIX}{client_suffix}-{_now()}"
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

    order_id = str(resp.get("order_id") or (resp.get("success_response") or {}).get("order_id") or "")
    if not order_id:
        return None
    return order_id, size, price


def _resolve_products() -> List[str]:
    products = [str(product).upper() for product in cb_pub.resolve_reserve_products()]
    deduped: List[str] = []
    seen: Set[str] = set()
    for product in products:
        if product and product not in seen:
            seen.add(product)
            deduped.append(product)
    if not deduped:
        raise RuntimeError("No reserve products resolved. Check RESERVE_PRODUCTS / STABLES_PRODUCTS / STABLES_AUTO_DISCOVER.")
    return deduped


def _refresh_products_if_due(current_products: List[str], last_refresh_ts: int) -> Tuple[List[str], int, Optional[str]]:
    now = _now()
    if current_products and (now - last_refresh_ts) < PRODUCT_REFRESH_SECS:
        return current_products, last_refresh_ts, None

    refreshed = _resolve_products()
    if refreshed != current_products:
        return refreshed, now, f"reserve_universe_updated:{','.join(refreshed)}"
    return refreshed, now, None


def _asset_set(products: List[str], product_map: Dict[str, Dict[str, Any]]) -> Set[str]:
    assets: Set[str] = set()
    for product_id in products:
        product = product_map.get(product_id.upper())
        if not product:
            continue
        base = str(product.get("base_currency_id") or product.get("base_currency") or "").upper()
        quote = str(product.get("quote_currency_id") or product.get("quote_currency") or "").upper()
        if base:
            assets.add(base)
        if quote:
            assets.add(quote)
    return assets


def _asset_shortfalls(assets: Set[str], balances: Dict[str, Decimal]) -> List[Tuple[str, Decimal]]:
    priority = {asset: idx for idx, asset in enumerate(_target_asset_priority())}
    rows: List[Tuple[str, Decimal]] = []
    for asset in assets:
        shortfall = _min_balance(asset) - balances.get(asset, Decimal("0"))
        if shortfall > 0:
            rows.append((asset, shortfall))

    rows.sort(key=lambda item: (priority.get(item[0], 999), -item[1], item[0]))
    return rows


def _source_excess(asset: str, balances: Dict[str, Decimal]) -> Decimal:
    return balances.get(asset, Decimal("0")) - _min_balance(asset)


def _buy_candidate(
    product_id: str,
    target_asset: str,
    target_shortfall: Decimal,
    balances: Dict[str, Decimal],
    products: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    base, quote = _base_quote(product_id, products)
    if base != target_asset:
        return None

    available_quote = _source_excess(quote, balances)
    if available_quote <= 0:
        return None

    max_quote = min(_max_topup_unit_usd(), available_quote)
    if max_quote <= 0:
        return None

    price = q(cb_pub.get_maker_limit_price(product_id, "BUY"))
    if price <= 0:
        return None

    quote_needed = target_shortfall * price
    quote_notional = min(max_quote, quote_needed)
    if quote_notional <= 0:
        return None

    return {
        "product_id": product_id,
        "side": "BUY",
        "target_asset": target_asset,
        "source_asset": quote,
        "quote_notional": quote_notional,
        "reason": f"topup_{target_asset.lower()}_using_{quote.lower()}",
    }


def _sell_candidate(
    product_id: str,
    target_asset: str,
    target_shortfall: Decimal,
    balances: Dict[str, Decimal],
    products: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    base, quote = _base_quote(product_id, products)
    if quote != target_asset:
        return None

    available_base = _source_excess(base, balances)
    if available_base <= 0:
        return None

    price = q(cb_pub.get_maker_limit_price(product_id, "SELL"))
    if price <= 0:
        return None

    base_capacity_quote = available_base * price
    quote_notional = min(_max_topup_unit_usd(), target_shortfall, base_capacity_quote)
    if quote_notional <= 0:
        return None

    return {
        "product_id": product_id,
        "side": "SELL",
        "target_asset": target_asset,
        "source_asset": base,
        "quote_notional": quote_notional,
        "reason": f"raise_{target_asset.lower()}_from_{base.lower()}",
    }


def _candidate_rank(candidate: Dict[str, Any], funding_assets: Set[str]) -> Tuple[int, int, str]:
    source_asset = str(candidate.get("source_asset") or "").upper()
    side = str(candidate.get("side") or "").upper()
    funding_priority = 0 if source_asset in funding_assets else 1
    side_priority = 0 if side == "BUY" else 1
    return funding_priority, side_priority, str(candidate.get("product_id") or "")


def _reserve_actions(
    balances: Dict[str, Decimal],
    busy_products: List[str],
    reserve_products: List[str],
) -> List[str]:
    actions: List[str] = []
    busy = {product.upper() for product in busy_products}
    products = _product_map()
    funding_assets = _funding_assets()
    tracked_assets = _asset_set(reserve_products, products)

    if not tracked_assets:
        return actions

    shortfalls = _asset_shortfalls(tracked_assets, balances)
    if not shortfalls:
        return actions

    for target_asset, target_shortfall in shortfalls:
        candidates: List[Dict[str, Any]] = []
        for product_id in reserve_products:
            normalized = product_id.upper()
            if normalized in busy or _reserve_order_open(normalized):
                continue

            buy_candidate = _buy_candidate(normalized, target_asset, target_shortfall, balances, products)
            if buy_candidate:
                candidates.append(buy_candidate)

            sell_candidate = _sell_candidate(normalized, target_asset, target_shortfall, balances, products)
            if sell_candidate:
                candidates.append(sell_candidate)

        candidates.sort(key=lambda candidate: _candidate_rank(candidate, funding_assets))
        for candidate in candidates:
            submitted = _submit_limit(
                product_id=str(candidate["product_id"]),
                side=str(candidate["side"]),
                quote_notional=q(candidate["quote_notional"]),
                client_suffix=str(candidate["reason"]),
                products=products,
            )
            if not submitted:
                continue

            order_id, size, price = submitted
            actions.append(
                f"{candidate['side']} {candidate['product_id']} "
                f"size={size} price={price} target={candidate['target_asset']} "
                f"source={candidate['source_asset']} order_id={order_id}"
            )
            return actions

    return actions


def main() -> None:
    _ensure_outdir()

    reserve_products: List[str] = []
    last_refresh_ts = 0

    try:
        reserve_products, last_refresh_ts, _ = _refresh_products_if_due([], 0)
        print(f"[controller] reserve universe ({len(reserve_products)}): {', '.join(reserve_products)}")
    except Exception as exc:
        print(f"[controller] startup product resolution failed: {exc}")

    while True:
        try:
            reserve_products, last_refresh_ts, refresh_note = _refresh_products_if_due(reserve_products, last_refresh_ts)
            if refresh_note:
                print(f"[controller] {refresh_note}")

            balances = _balances()
            busy_products = _busy_products()
            actions = _reserve_actions(balances, busy_products, reserve_products)

            state_row = {
                "ts": _now(),
                "products": reserve_products,
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
            _write_jsonl(STATE_PATH, {"ts": _now(), "error": str(exc)})
            print(f"[controller] error: {exc}")
        finally:
            time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()