from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext
from typing import Any, Dict, List, Optional, Set, Tuple

getcontext().prec = 28

try:
    from owcg_utils.precision import round_price, round_size
except Exception:
    def _q(x: Any) -> Decimal:
        return x if isinstance(x, Decimal) else Decimal(str(x))

    def round_price(x: Any, inc: Any, mode: str = "nearest") -> Decimal:
        x, inc = _q(x), _q(inc)
        if inc <= 0:
            return x
        n = x / inc
        if mode == "down":
            n = n.to_integral_value(rounding=ROUND_DOWN)
        elif mode == "up":
            n = n.to_integral_value(rounding=ROUND_UP)
        else:
            n = (n + _q("0.5")).to_integral_value(rounding=ROUND_DOWN)
        return n * inc

    def round_size(x: Any, inc: Any, mode: str = "down") -> Decimal:
        return round_price(x, inc, mode=mode)


def _mkt_mod():
    if os.getenv("OWCG_OFFLINE") == "1":
        return None
    try:
        from broker import coinbase_public as cb_pub  # type: ignore
        return cb_pub
    except Exception:
        return None


def _env_set(name: str, default: Set[str]) -> Set[str]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return set(default)
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


USD_PEG_ASSETS = _env_set(
    "STABLES_USD_PEG_ASSETS",
    {"USD", "USDC", "USDT", "DAI", "PYUSD", "FDUSD", "USDP", "GUSD", "TUSD", "RLUSD"},
)

_SPEC_CACHE: Dict[str, "ProductSpec"] = {}


@dataclass
class ProductSpec:
    product_id: str
    base: str
    quote: str
    price_increment: Decimal
    size_increment: Decimal
    min_size: Decimal


@dataclass
class L2:
    bids: List[Tuple[Decimal, Decimal]]
    asks: List[Tuple[Decimal, Decimal]]


@dataclass
class Ticket:
    ts: int
    expire_ts: int
    product_id: str
    side: str
    entry_price: Decimal
    size: Decimal
    tp_price: Decimal
    sl_price: Decimal
    post_only: bool = True
    bracket_desired: bool = False
    client_tag: str = "stables_mr"

    def to_row(self) -> Dict[str, str]:
        row = asdict(self)
        row["entry_price"] = str(self.entry_price)
        row["size"] = str(self.size)
        row["tp_price"] = str(self.tp_price)
        row["sl_price"] = str(self.sl_price)
        row["ts"] = str(self.ts)
        row["expire_ts"] = str(self.expire_ts)
        return row


@dataclass
class StrategyConfig:
    products: List[str]
    out_dir: str = "output_stables"
    maker_fee_bps: Decimal = Decimal("0.0")
    taker_fee_bps: Decimal = Decimal("0.0")
    exit_bps: Decimal = Decimal("4.0")
    sl_bps: Decimal = Decimal("6.0")
    slippage_bps: Decimal = Decimal("1.0")
    cushion_bps: Decimal = Decimal("0.3")
    hold_minutes: int = 180
    depth_ticks: int = 2
    min_depth_multiplier: Decimal = Decimal("1.10")
    block_on_missing_l2: bool = True
    bankroll_usd: Decimal = Decimal("100.00")
    bankroll_pct: Decimal = Decimal("0.10")
    min_notional: Decimal = Decimal("5.00")
    max_notional: Decimal = Decimal("500.00")
    max_risk_usd: Decimal = Decimal("3.00")
    min_tp_ticks: int = 1
    min_sl_ticks: int = 1
    max_spread_bps: Decimal = Decimal("3.0")
    max_dev_bps: Decimal = Decimal("25.0")
    ticket_ttl_secs: int = 30


def _to_decimal(x: Any) -> Optional[Decimal]:
    try:
        if x is None:
            return None
        return x if isinstance(x, Decimal) else Decimal(str(x))
    except Exception:
        return None


def _fallback_spec(product_id: str) -> ProductSpec:
    return ProductSpec(product_id, "USDC", "USD", Decimal("0.0001"), Decimal("0.01"), Decimal("1"))


def _get_product_spec(product_id: str) -> ProductSpec:
    normalized = str(product_id).upper()
    cached = _SPEC_CACHE.get(normalized)
    if cached is not None:
        return cached

    mkt = _mkt_mod()
    if mkt is None:
        spec = _fallback_spec(normalized)
        _SPEC_CACHE[normalized] = spec
        return spec

    product = mkt.get_product(normalized) if hasattr(mkt, "get_product") else None
    if not product and hasattr(mkt, "get_products"):
        for candidate in mkt.get_products():
            if str(candidate.get("product_id") or "").upper() == normalized:
                product = candidate
                break

    if not product:
        spec = _fallback_spec(normalized)
        _SPEC_CACHE[normalized] = spec
        return spec

    spec = ProductSpec(
        product_id=normalized,
        base=str(product.get("base_currency_id") or product.get("base_currency") or "").upper(),
        quote=str(product.get("quote_currency_id") or product.get("quote_currency") or "").upper(),
        price_increment=_to_decimal(product.get("price_increment") or product.get("quote_increment") or "0.0001") or Decimal("0.0001"),
        size_increment=_to_decimal(product.get("base_increment") or "0.01") or Decimal("0.01"),
        min_size=_to_decimal(product.get("min_order_size") or product.get("base_min_size") or "1") or Decimal("1"),
    )
    _SPEC_CACHE[normalized] = spec
    return spec


def _get_best_bid_ask(product_id: str) -> Tuple[Decimal, Decimal]:
    mkt = _mkt_mod()
    if mkt is None:
        return Decimal("0.9999"), Decimal("1.0001")
    bid, ask = mkt.get_best_bid_ask(product_id)
    return _to_decimal(bid) or Decimal("0"), _to_decimal(ask) or Decimal("0")


def _get_l2(product_id: str, depth: int = 5) -> Optional[L2]:
    mkt = _mkt_mod()
    if mkt is None:
        return L2(bids=[(Decimal("0.9999"), Decimal("10000"))], asks=[(Decimal("1.0001"), Decimal("10000"))])
    try:
        book = mkt.get_l2(product_id, depth=depth)
    except Exception:
        return None

    bids: List[Tuple[Decimal, Decimal]] = []
    asks: List[Tuple[Decimal, Decimal]] = []

    for row in book.get("bids") or []:
        px = _to_decimal(row[0])
        sz = _to_decimal(row[1])
        if px is not None and sz is not None:
            bids.append((px, sz))

    for row in book.get("asks") or []:
        px = _to_decimal(row[0])
        sz = _to_decimal(row[1])
        if px is not None and sz is not None:
            asks.append((px, sz))

    if not bids or not asks:
        return None
    return L2(bids=bids, asks=asks)


def _bps_from_delta(delta: Decimal, ref: Decimal) -> Decimal:
    if ref <= 0:
        return Decimal("0")
    return (delta / ref) * Decimal("10000")


def _spread_bps(bid: Decimal, ask: Decimal) -> Decimal:
    if bid <= 0 or ask <= 0:
        return Decimal("0")
    mid = (bid + ask) / Decimal("2")
    return _bps_from_delta(ask - bid, mid)


def _maker_entry_price(side: str, bid: Decimal, ask: Decimal, spec: ProductSpec) -> Decimal:
    if side == "BUY":
        return round_price(bid, spec.price_increment, mode="down")
    return round_price(ask, spec.price_increment, mode="up")


def _quote_reference_price(spec: ProductSpec) -> Optional[Decimal]:
    if spec.base in USD_PEG_ASSETS and spec.quote in USD_PEG_ASSETS:
        return Decimal("1")
    return None


def _target_notional(cfg: StrategyConfig) -> Decimal:
    raw = (cfg.bankroll_usd * cfg.bankroll_pct).quantize(Decimal("0.01"))
    floor = min(cfg.min_notional, cfg.max_notional)
    ceiling = max(cfg.min_notional, cfg.max_notional)
    return max(floor, min(ceiling, raw))


def _compute_size_for_bankroll(
    spec: ProductSpec,
    side: str,
    entry: Decimal,
    cfg: StrategyConfig,
    balances: Dict[str, Decimal],
) -> Tuple[Decimal, Decimal, Optional[str]]:
    target_notional = _target_notional(cfg)
    if entry <= 0:
        return Decimal("0"), target_notional, "bad_entry"

    desired_size = round_size(target_notional / entry, spec.size_increment, mode="down")
    bal_base = balances.get(spec.base, Decimal("0"))
    bal_quote = balances.get(spec.quote, Decimal("0"))

    if side == "SELL":
        max_sell = round_size(bal_base, spec.size_increment, mode="down")
        size = min(desired_size, max_sell)
        if size <= 0:
            return Decimal("0"), target_notional, f"insufficient_base_balance asset={spec.base} available={bal_base}"
        if size < desired_size:
            return size, target_notional, f"base_balance_limited asset={spec.base} available={bal_base}"
        return size, target_notional, None

    max_buy = round_size(bal_quote / entry, spec.size_increment, mode="down") if entry > 0 else Decimal("0")
    size = min(desired_size, max_buy)
    if size <= 0:
        return Decimal("0"), target_notional, f"insufficient_quote_balance asset={spec.quote} available={bal_quote}"
    if size < desired_size:
        return size, target_notional, f"quote_balance_limited asset={spec.quote} available={bal_quote}"
    return size, target_notional, None


def _depth_sufficient(side: str, entry: Decimal, spec: ProductSpec, l2: Optional[L2], size: Decimal, cfg: StrategyConfig) -> Tuple[bool, str]:
    if l2 is None:
        return ((not cfg.block_on_missing_l2), ("no_l2" if cfg.block_on_missing_l2 else "depth_unavailable_skip"))

    ticks = spec.price_increment * cfg.depth_ticks
    need = size * cfg.min_depth_multiplier

    if side == "SELL":
        threshold = entry - ticks
        agg = sum(sz for px, sz in l2.bids if px >= threshold)
    else:
        threshold = entry + ticks
        agg = sum(sz for px, sz in l2.asks if px <= threshold)

    return agg >= need, f"depth_{'ok' if agg >= need else 'thin'} agg={agg} need={need}"


def _risk_dollars(entry: Decimal, sl: Decimal, size: Decimal) -> Decimal:
    return abs(entry - sl) * size


def _enforce_min_tick_separation(side: str, entry: Decimal, tp: Decimal, sl: Decimal, spec: ProductSpec, cfg: StrategyConfig) -> Tuple[Decimal, Decimal]:
    tp_ticks = spec.price_increment * cfg.min_tp_ticks
    sl_ticks = spec.price_increment * cfg.min_sl_ticks

    if side == "BUY":
        min_tp = entry + tp_ticks
        max_sl = entry - sl_ticks
        if tp <= entry:
            tp = min_tp
        if sl >= entry:
            sl = max_sl
        tp = round_price(tp, spec.price_increment, mode="up")
        sl = round_price(sl, spec.price_increment, mode="down")
    else:
        max_tp = entry - tp_ticks
        min_sl = entry + sl_ticks
        if tp >= entry:
            tp = max_tp
        if sl <= entry:
            sl = min_sl
        tp = round_price(tp, spec.price_increment, mode="down")
        sl = round_price(sl, spec.price_increment, mode="up")

    return tp, sl


def _gate_bps(cfg: StrategyConfig, bid: Decimal, ask: Decimal) -> Decimal:
    spread_penalty = _spread_bps(bid, ask)
    return cfg.exit_bps + cfg.slippage_bps + cfg.cushion_bps + cfg.maker_fee_bps + max(cfg.maker_fee_bps, cfg.taker_fee_bps) + spread_penalty


def _gross_dev_bps(reference: Decimal, entry: Decimal, side: str) -> Decimal:
    if side == "BUY":
        return _bps_from_delta(reference - entry, entry)
    return _bps_from_delta(entry - reference, entry)


def _diag(
    product_id: str,
    side: str,
    reason: str,
    dev_bps: Decimal | str = "0",
    edge_minus_gate_bps: Decimal | str = "0",
    notional: Decimal | str = "0",
    gate_bps: Decimal | str = "0",
    depth_note: str = "n/a",
    risk_dollars: Decimal | str = "0",
    spread_bps: Decimal | str = "0",
) -> Dict[str, Any]:
    return {
        "product_id": product_id,
        "side": side,
        "reason": reason,
        "dev_bps": str(dev_bps),
        "edge_minus_gate_bps": str(edge_minus_gate_bps),
        "notional": str(notional),
        "gate_bps": str(gate_bps),
        "depth_note": depth_note,
        "risk_dollars": str(risk_dollars),
        "spread_bps": str(spread_bps),
    }


def scan_once(cfg: StrategyConfig, balances: Dict[str, Decimal]) -> Tuple[Optional[Ticket], Dict[str, Any]]:
    os.makedirs(cfg.out_dir, exist_ok=True)

    best_ticket: Optional[Ticket] = None
    best_diag: Optional[Dict[str, Any]] = None
    best_edge = Decimal("-1000000")
    now = int(time.time())

    for product_id in cfg.products:
        spec = _get_product_spec(product_id)
        reference = _quote_reference_price(spec)

        if reference is None:
            diag = _diag(product_id, "NONE", f"unsupported_quote_reference base={spec.base} quote={spec.quote}")
            if best_diag is None:
                best_diag = diag
            continue

        bid, ask = _get_best_bid_ask(product_id)
        if bid <= 0 or ask <= 0 or bid >= ask:
            diag = _diag(product_id, "NONE", f"bad_quote bid={bid} ask={ask}")
            if best_diag is None:
                best_diag = diag
            continue

        spread = _spread_bps(bid, ask)
        if spread > cfg.max_spread_bps:
            diag = _diag(product_id, "NONE", f"spread_too_wide {spread}", gate_bps=spread, spread_bps=spread)
            if best_diag is None:
                best_diag = diag
            continue

        gate = _gate_bps(cfg, bid, ask)
        candidates: List[Tuple[str, Decimal, Decimal]] = []

        buy_entry = _maker_entry_price("BUY", bid, ask, spec)
        sell_entry = _maker_entry_price("SELL", bid, ask, spec)

        buy_dev = _gross_dev_bps(reference, buy_entry, "BUY")
        sell_dev = _gross_dev_bps(reference, sell_entry, "SELL")

        if buy_dev > 0:
            candidates.append(("BUY", buy_entry, buy_dev))
        if sell_dev > 0:
            candidates.append(("SELL", sell_entry, sell_dev))

        if not candidates:
            diag = _diag(product_id, "NONE", "no_deviation", spread_bps=spread)
            if best_diag is None:
                best_diag = diag
            continue

        for side, entry, dev_bps in candidates:
            if dev_bps > cfg.max_dev_bps:
                diag = _diag(product_id, side, f"dev_too_large {dev_bps}", dev_bps=dev_bps, gate_bps=gate, spread_bps=spread)
                if best_diag is None:
                    best_diag = diag
                continue

            edge_minus_gate = dev_bps - gate
            if edge_minus_gate <= 0:
                diag = _diag(
                    product_id,
                    side,
                    f"edge_not_met {edge_minus_gate}",
                    dev_bps=dev_bps,
                    edge_minus_gate_bps=edge_minus_gate,
                    gate_bps=gate,
                    spread_bps=spread,
                )
                current_best = Decimal(str(best_diag.get("edge_minus_gate_bps", "-1000000"))) if best_diag else Decimal("-1000000")
                if best_diag is None or edge_minus_gate > current_best:
                    best_diag = diag
                continue

            size, target_notional, balance_note = _compute_size_for_bankroll(spec, side, entry, cfg, balances)
            if size < spec.min_size:
                diag = _diag(
                    product_id,
                    side,
                    balance_note or "size_below_min",
                    dev_bps=dev_bps,
                    edge_minus_gate_bps=edge_minus_gate,
                    notional=target_notional,
                    gate_bps=gate,
                    spread_bps=spread,
                )
                current_best = Decimal(str(best_diag.get("edge_minus_gate_bps", "-1000000"))) if best_diag else Decimal("-1000000")
                if best_diag is None or edge_minus_gate > current_best:
                    best_diag = diag
                continue

            if side == "BUY":
                tp = entry * (Decimal("1") + cfg.exit_bps / Decimal("10000"))
                sl = entry * (Decimal("1") - cfg.sl_bps / Decimal("10000"))
            else:
                tp = entry * (Decimal("1") - cfg.exit_bps / Decimal("10000"))
                sl = entry * (Decimal("1") + cfg.sl_bps / Decimal("10000"))

            tp, sl = _enforce_min_tick_separation(side, entry, tp, sl, spec, cfg)
            risk = _risk_dollars(entry, sl, size)

            if risk > cfg.max_risk_usd:
                max_size = round_size(cfg.max_risk_usd / abs(entry - sl), spec.size_increment, mode="down") if entry != sl else Decimal("0")
                size = min(size, max_size)
                if size < spec.min_size:
                    diag = _diag(
                        product_id,
                        side,
                        "risk_cap",
                        dev_bps=dev_bps,
                        edge_minus_gate_bps=edge_minus_gate,
                        notional=target_notional,
                        gate_bps=gate,
                        risk_dollars=risk,
                        spread_bps=spread,
                    )
                    current_best = Decimal(str(best_diag.get("edge_minus_gate_bps", "-1000000"))) if best_diag else Decimal("-1000000")
                    if best_diag is None or edge_minus_gate > current_best:
                        best_diag = diag
                    continue
                risk = _risk_dollars(entry, sl, size)

            l2 = _get_l2(product_id, depth=max(5, cfg.depth_ticks + 2))
            depth_ok, depth_note = _depth_sufficient(side, entry, spec, l2, size, cfg)
            notional = (entry * size).quantize(Decimal("0.01"))

            if not depth_ok:
                diag = _diag(
                    product_id,
                    side,
                    "depth_check_failed",
                    dev_bps=dev_bps,
                    edge_minus_gate_bps=edge_minus_gate,
                    notional=notional,
                    gate_bps=gate,
                    depth_note=depth_note,
                    risk_dollars=risk,
                    spread_bps=spread,
                )
                current_best = Decimal(str(best_diag.get("edge_minus_gate_bps", "-1000000"))) if best_diag else Decimal("-1000000")
                if best_diag is None or edge_minus_gate > current_best:
                    best_diag = diag
                continue

            ticket = Ticket(
                ts=now,
                expire_ts=now + int(cfg.ticket_ttl_secs),
                product_id=product_id,
                side=side,
                entry_price=entry,
                size=size,
                tp_price=tp,
                sl_price=sl,
                post_only=True,
                bracket_desired=False,
                client_tag="stables_mr",
            )
            diag = _diag(
                product_id,
                side,
                balance_note or "pass",
                dev_bps=dev_bps,
                edge_minus_gate_bps=edge_minus_gate,
                notional=notional,
                gate_bps=gate,
                depth_note=depth_note,
                risk_dollars=risk,
                spread_bps=spread,
            )

            if edge_minus_gate > best_edge:
                best_edge = edge_minus_gate
                best_ticket = ticket
                best_diag = diag

    if best_diag is None:
        best_diag = _diag("", "NONE", "no_products")

    return best_ticket, best_diag