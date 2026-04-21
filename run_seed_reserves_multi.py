from __future__ import annotations

import argparse
import json
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, Sequence, Set, Tuple

from broker import coinbase_private as cb_priv  # type: ignore
from broker import coinbase_public as cb_pub  # type: ignore
from owcg_utils.precision import round_price, round_size

OUTDIR = Path("output_stables")
TARGETS_PATH = OUTDIR / "reserve_targets.json"
DEFAULT_ASSET_PRIORITY = ["USDC", "USDT", "DAI", "USD"]


def q(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


@dataclass
class Edge:
    source_asset: str
    target_asset: str
    product_id: str
    side: str


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
    products: Dict[str, Dict[str, Any]] = {}
    for product in cb_pub.get_tradable_products():
        product_id = str(product.get("product_id") or "").upper()
        if product_id:
            products[product_id] = product
    return products


def _status_allows_trading(product: Dict[str, Any]) -> bool:
    status = str(product.get("status") or "").lower()
    if product.get("trading_disabled") or product.get("cancel_only") or product.get("auction_mode"):
        return False
    if status and status not in {"online", "active", "internal"}:
        return False
    return True


def _tracked_products() -> List[str]:
    explicit = cb_pub.resolve_reserve_products()
    seen: Set[str] = set()
    out: List[str] = []
    for product in explicit:
        normalized = str(product).upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _required_assets(products: Sequence[str], product_map: Dict[str, Dict[str, Any]]) -> Set[str]:
    out: Set[str] = set()
    for product_id in products:
        product = product_map.get(product_id.upper())
        if not product:
            continue
        base = str(product.get("base_currency_id") or product.get("base_currency") or "").upper()
        quote = str(product.get("quote_currency_id") or product.get("quote_currency") or "").upper()
        if base:
            out.add(base)
        if quote:
            out.add(quote)
    return out


def _default_targets(total_usd: Decimal, assets: Set[str]) -> Dict[str, Decimal]:
    ordered_assets = [asset for asset in DEFAULT_ASSET_PRIORITY if asset in assets]
    for asset in sorted(assets):
        if asset not in ordered_assets:
            ordered_assets.append(asset)

    if not ordered_assets:
        return {}

    per_asset = (total_usd / Decimal(len(ordered_assets))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    targets = {asset: per_asset for asset in ordered_assets}
    used = per_asset * Decimal(len(ordered_assets))
    remainder = total_usd - used

    if "USD" in targets:
        targets["USD"] = (targets["USD"] + remainder).quantize(Decimal("0.01"))
    else:
        first = ordered_assets[0]
        targets[first] = (targets[first] + remainder).quantize(Decimal("0.01"))

    return targets


def _write_targets(targets: Dict[str, Decimal]) -> None:
    OUTDIR.mkdir(exist_ok=True)
    payload = {asset: str(value.quantize(Decimal("0.01"))) for asset, value in sorted(targets.items())}
    TARGETS_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _build_graph(products: Dict[str, Dict[str, Any]], allowed_assets: Set[str]) -> DefaultDict[str, List[Edge]]:
    graph: DefaultDict[str, List[Edge]] = defaultdict(list)
    for product_id, product in products.items():
        if not _status_allows_trading(product):
            continue

        base = str(product.get("base_currency_id") or product.get("base_currency") or "").upper()
        quote = str(product.get("quote_currency_id") or product.get("quote_currency") or "").upper()
        if not base or not quote:
            continue
        if base not in allowed_assets or quote not in allowed_assets:
            continue

        graph[quote].append(Edge(source_asset=quote, target_asset=base, product_id=product_id, side="BUY"))
        graph[base].append(Edge(source_asset=base, target_asset=quote, product_id=product_id, side="SELL"))

    return graph


def _find_path(source_asset: str, target_asset: str, graph: DefaultDict[str, List[Edge]], max_hops: int = 2) -> Optional[List[Edge]]:
    source_asset = source_asset.upper()
    target_asset = target_asset.upper()

    if source_asset == target_asset:
        return []

    queue = deque([(source_asset, [])])
    seen = {source_asset}

    while queue:
        asset, path = queue.popleft()
        if len(path) >= max_hops:
            continue

        for edge in graph.get(asset, []):
            next_asset = edge.target_asset
            if next_asset == target_asset:
                return path + [edge]
            if next_asset not in seen:
                seen.add(next_asset)
                queue.append((next_asset, path + [edge]))

    return None


def _wait_for_asset_increase(asset: str, before: Decimal, timeout_secs: float = 8.0) -> Tuple[Decimal, Dict[str, Decimal]]:
    deadline = time.time() + timeout_secs
    last = _balances()
    while time.time() < deadline:
        time.sleep(0.6)
        last = _balances()
        after = last.get(asset.upper(), Decimal("0"))
        if after > before:
            return after - before, last
    after = last.get(asset.upper(), Decimal("0"))
    return max(after - before, Decimal("0")), last


def _product_specs(product_id: str) -> Tuple[Decimal, Decimal, Decimal]:
    product = cb_pub.get_product(product_id.upper())
    if not product:
        raise RuntimeError(f"Unknown product: {product_id}")
    base_inc = q(product.get("base_increment") or "0.00000001")
    price_inc = q(product.get("price_increment") or product.get("quote_increment") or "0.0001")
    min_size = q(product.get("min_order_size") or product.get("base_min_size") or "0")
    return base_inc, price_inc, min_size


def _limit_only_error(resp: Dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(resp.get("error", "")),
            str(resp.get("error_details", "")),
            str(resp.get("message", "")),
        ]
    ).lower()
    return "limit only mode" in text or "use limit order type" in text


def _place_marketable_limit_from_source(edge: Edge, source_amount: Decimal) -> Dict[str, Any]:
    base_inc, price_inc, min_size = _product_specs(edge.product_id)
    buffer_bps = Decimal("5")

    if edge.side == "BUY":
        raw_price = cb_pub.get_marketable_limit_price(edge.product_id, "BUY", buffer_bps=buffer_bps)
        price = round_price(raw_price, price_inc, mode="up")
        size = round_size(source_amount / price, base_inc, mode="down")
    else:
        raw_price = cb_pub.get_marketable_limit_price(edge.product_id, "SELL", buffer_bps=buffer_bps)
        price = round_price(raw_price, price_inc, mode="down")
        size = round_size(source_amount, base_inc, mode="down")

    if size < min_size:
        raise RuntimeError(
            f"Bootstrap limit order too small for {edge.product_id}: size={size} min_size={min_size}"
        )

    ok, resp = cb_priv.place_limit_order(
        product_id=edge.product_id,
        side=edge.side,
        size=str(size),
        limit_price=str(price),
        post_only=False,
        client_order_id=f"seed-limit-{uuid.uuid4().hex[:10]}",
    )
    if not ok:
        raise RuntimeError(f"{edge.side} {edge.product_id} fallback limit failed: {resp}")
    return resp


def _submit_edge_order(edge: Edge, source_amount: Decimal) -> Dict[str, Any]:
    if edge.side == "BUY":
        ok, resp = cb_priv.place_market_ioc_quote_buy(
            product_id=edge.product_id,
            quote_size=str(source_amount),
            client_order_id=f"seed-{uuid.uuid4().hex[:10]}",
        )
    else:
        ok, resp = cb_priv.place_market_ioc_order(
            product_id=edge.product_id,
            side="SELL",
            size=str(source_amount),
            client_order_id=f"seed-{uuid.uuid4().hex[:10]}",
        )

    if ok:
        return resp

    if isinstance(resp, dict) and _limit_only_error(resp):
        return _place_marketable_limit_from_source(edge, source_amount)

    raise RuntimeError(f"{edge.side} {edge.product_id} failed: {resp}")


def _execute_edge(edge: Edge, source_amount: Decimal, dry_run: bool) -> Tuple[Decimal, str]:
    source_amount = source_amount.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    if source_amount <= 0:
        return Decimal("0"), f"skip {edge.side} {edge.product_id} source_amount={source_amount}"

    if dry_run:
        return source_amount, f"dry_run {edge.side} {edge.product_id} source={edge.source_asset} amount={source_amount}"

    before_balances = _balances()
    before_target = before_balances.get(edge.target_asset, Decimal("0"))

    resp = _submit_edge_order(edge, source_amount)

    acquired, _ = _wait_for_asset_increase(edge.target_asset, before_target)
    if acquired <= 0:
        acquired = (source_amount * Decimal("0.995")).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

    order_id = str(resp.get("order_id") or (resp.get("success_response") or {}).get("order_id") or "")
    return acquired, (
        f"{edge.side} {edge.product_id} source={edge.source_asset} amount={source_amount} "
        f"target={edge.target_asset} acquired≈{acquired} order_id={order_id}"
    )


def _execute_path(path: List[Edge], initial_source_amount: Decimal, dry_run: bool) -> Tuple[Decimal, List[str]]:
    current_amount = initial_source_amount
    steps: List[str] = []

    for edge in path:
        current_amount, step = _execute_edge(edge, current_amount, dry_run=dry_run)
        steps.append(step)

    return current_amount, steps


def _seed_asset_from_usd(
    target_asset: str,
    deficit: Decimal,
    graph: DefaultDict[str, List[Edge]],
    dry_run: bool,
) -> List[str]:
    path = _find_path("USD", target_asset, graph, max_hops=2)
    if path is None:
        raise RuntimeError(f"No conversion path found from USD to {target_asset}")

    _, steps = _execute_path(path, deficit, dry_run=dry_run)
    return steps


def _priority_assets(assets: Set[str]) -> List[str]:
    out = [asset for asset in DEFAULT_ASSET_PRIORITY if asset in assets]
    for asset in sorted(assets):
        if asset not in out:
            out.append(asset)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed reserve balances for the stable-pair bot.")
    parser.add_argument("--usd-total", default="150", help="Total bankroll to target across reserve assets.")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned reserve actions without sending orders.")
    args = parser.parse_args()

    total_usd = Decimal(str(args.usd_total)).quantize(Decimal("0.01"))
    reserve_products = _tracked_products()
    products = _product_map()
    assets = _required_assets(reserve_products, products)

    if "USD" not in assets:
        assets.add("USD")

    targets = _default_targets(total_usd, assets)
    _write_targets(targets)

    balances_before = _balances()
    graph = _build_graph(products, assets)

    print(f"[seed] reserve products: {', '.join(reserve_products)}")
    print(f"[seed] target balances: { {k: str(v) for k, v in targets.items()} }")
    print(f"[seed] balances_before: { {k: str(v) for k, v in balances_before.items() if k in assets} }")

    ordered_assets = [asset for asset in _priority_assets(assets) if asset != "USD"]
    all_steps: List[str] = []

    for asset in ordered_assets:
        current = _balances().get(asset, Decimal("0")) if not args.dry_run else balances_before.get(asset, Decimal("0"))
        deficit = (targets.get(asset, Decimal("0")) - current).quantize(Decimal("0.01"))
        if deficit <= Decimal("0.50"):
            continue

        steps = _seed_asset_from_usd(asset, deficit, graph, dry_run=args.dry_run)
        all_steps.extend(steps)

    balances_after = _balances() if not args.dry_run else balances_before

    print("[seed] actions:")
    if all_steps:
        for step in all_steps:
            print(step)
    else:
        print("none")

    print(f"[seed] balances_after: { {k: str(v) for k, v in balances_after.items() if k in assets} }")
    print(f"[seed] wrote targets file: {TARGETS_PATH}")


if __name__ == "__main__":
    main()