from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, getcontext
from typing import Any, Dict, List, Optional, Tuple

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


def _get_product_spec(product_id: str) -> ProductSpec:
    mkt = _mkt_mod()
    if mkt is None:
        return ProductSpec(product_id, "USDC", "USD", Decimal("0.0001"), Decimal("0.01"), Decimal("1"))
    product = mkt.get_product(product_id) if hasattr(mkt, "get_product") else None
    if not product:
        for candidate in mkt.get_products():
            if str(candidate.get("product_id")) == product_id:
                product = candidate
                break
    if not product:
        return ProductSpec(product_id, "USDC", "USD", Decimal("0.0001"), Decimal("0.01"), Decimal("1"))
    return ProductSpec(
        product_id=product_id,
        base=str(product.get("base_currency_id") or product.get("base_currency") or ""),
        quote=str(product.get("quote_currency_id") or product.get("quote_currency") or ""),
        price_increment=_to_decimal(product.get("price_increment") or product.get("quote_increment") or "0.0001") or Decimal("0.0001"),
        size_increment=_to_decimal(product.get("base_increment") or "0.01") or Decimal("0.01"),
        min_size=_to_decimal(product.get("min_order_size") or product.get("base_min_size") or "1") or Decimal("1"),
    )


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
    mid = (bid + ask) / Decimal("2") if bid > 0 and ask > 0 else Decimal("0")
    return _bps_from_delta(ask - bid, mid)


def _maker_entry_price(product_id: str, side: str, spec: ProductSpec) -> Decimal:
    bid, ask = _get_best_bid_ask(product_id)
    if side == "BUY":
        return round_price(bid, spec.price_increment, mode="down")
    return round_price(ask, spec.price_increment, mode="up")


def _compute_size_for_bankroll(product_id: str, side: str, entry: Decimal, cfg: StrategyConfig, balances: Dict[str, Decimal]) -> Tuple[Decimal, Decimal, Optional[str]]:
    target_notional = max(cfg.min_notional, min(cfg.max_notional, (cfg.bankroll_usd * cfg.bankroll_pct).quantize(Decimal("0.01"))))
    size = round_size(target_notional / entry, _get_product_spec(product_id).size_increment, mode="down")
    spec = _get_product_spec(product_id)
    bal_base = balances.get(spec.base, Decimal("0"))
    bal_quote = balances.get(spec.quote, Decimal("0"))
    if side == "SELL":
        size = min(size, round_size(bal_base, spec.size_increment, mode="down"))
        if size <= 0:
            return Decimal("0"), target_notional, "insufficient_balance"
    else:
        max_buy = round_size(bal_quote / entry, spec.size_increment, mode="down") if entry > 0 else Decimal("0")
        size = min(size, max_buy)
        if size <= 0:
            return Decimal("0"), target_notional, "insufficient_balance"
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


def _edge_minus_gate(side: str, entry: Decimal, cfg: StrategyConfig, bid: Decimal, ask: Decimal) -> Decimal:
    spread_penalty = _spread_bps(bid, ask)
    if side == "BUY":
        gross_dev = _bps_from_delta(Decimal("1.0") - entry, entry)
    else:
        gross_dev = _bps_from_delta(entry - Decimal("1.0"), entry)
    gate = cfg.exit_bps + cfg.slippage_bps + cfg.cushion_bps + cfg.maker_fee_bps + max(cfg.maker_fee_bps, cfg.taker_fee_bps) + spread_penalty
    return gross_dev - gate


def scan_once(cfg: StrategyConfig, balances: Dict[str, Decimal]) -> Tuple[Optional[Ticket], Dict[str, Any]]:
    os.makedirs(cfg.out_dir, exist_ok=True)
    best_ticket: Optional[Ticket] = None
    best_diag: Optional[Dict[str, Any]] = None
    best_edge = Decimal("-1000000")

    for product_id in cfg.products:
        spec = _get_product_spec(product_id)
        bid, ask = _get_best_bid_ask(product_id)
        now = int(time.time())

        if bid <= 0 or ask <= 0 or bid >= ask:
            diag = {
                "product_id": product_id,
                "side": "NONE",
                "reason": f"bad_quote bid={bid} ask={ask}",
                "dev_bps": "0",
                "edge_minus_gate_bps": "0",
                "notional": "0",
                "gate_bps": "0",
                "depth_note": "n/a",
                "risk_dollars": "0",
                "spread_bps": "0",
            }
            if best_diag is None:
                best_diag = diag
            continue

        spread = _spread_bps(bid, ask)
        if spread > cfg.max_spread_bps:
            diag = {
                "product_id": product_id,
                "side": "NONE",
                "reason": f"spread_too_wide {spread}",
                "dev_bps": "0",
                "edge_minus_gate_bps": "0",
                "notional": "0",
                "gate_bps": str(spread),
                "depth_note": "n/a",
                "risk_dollars": "0",
                "spread_bps": str(spread),
            }
            if best_diag is None:
                best_diag = diag
            continue

        candidates: List[Tuple[str, Decimal]] = []
        buy_entry = _maker_entry_price(product_id, "BUY", spec)
        sell_entry = _maker_entry_price(product_id, "SELL", spec)
        buy_dev = _bps_from_delta(Decimal("1.0") - buy_entry, buy_entry)
        sell_dev = _bps_from_delta(sell_entry - Decimal("1.0"), sell_entry)

        if buy_dev > 0:
            candidates.append(("BUY", buy_dev))
        if sell_dev > 0:
            candidates.append(("SELL", sell_dev))

        if not candidates:
            diag = {
                "product_id": product_id,
                "side": "NONE",
                "reason": "no_deviation",
                "dev_bps": "0",
                "edge_minus_gate_bps": "0",
                "notional": "0",
                "gate_bps": "0",
                "depth_note": "n/a",
                "risk_dollars": "0",
                "spread_bps": str(spread),
            }
            if best_diag is None:
                best_diag = diag
            continue

        for side, dev_bps in candidates:
            entry = buy_entry if side == "BUY" else sell_entry

            if dev_bps > cfg.max_dev_bps:
                diag = {
                    "product_id": product_id,
                    "side": side,
                    "reason": f"dev_too_large {dev_bps}",
                    "dev_bps": str(dev_bps),
                    "edge_minus_gate_bps": "0",
                    "notional": "0",
                    "gate_bps": "0",
                    "depth_note": "n/a",
                    "risk_dollars": "0",
                    "spread_bps": str(spread),
                }
                if best_diag is None:
                    best_diag = diag
                continue

            edge_minus_gate = _edge_minus_gate(side, entry, cfg, bid, ask)
            if edge_minus_gate <= 0:
                diag = {
                    "product_id": product_id,
                    "side": side,
                    "reason": f"edge_not_met {edge_minus_gate}",
                    "dev_bps": str(dev_bps),
                    "edge_minus_gate_bps": str(edge_minus_gate),
                    "notional": "0",
                    "gate_bps": str(dev_bps - edge_minus_gate),
                    "depth_note": "n/a",
                    "risk_dollars": "0",
                    "spread_bps": str(spread),
                }
                current_best = Decimal(str(best_diag.get("edge_minus_gate_bps", "-1000000"))) if best_diag else Decimal("-1000000")
                if best_diag is None or edge_minus_gate > current_best:
                    best_diag = diag
                continue

            size, notional, balance_note = _compute_size_for_bankroll(product_id, side, entry, cfg, balances)
            if size < spec.min_size:
                diag = {
                    "product_id": product_id,
                    "side": side,
                    "reason": balance_note or "size_below_min",
                    "dev_bps": str(dev_bps),
                    "edge_minus_gate_bps": str(edge_minus_gate),
                    "notional": str(notional),
                    "gate_bps": str(dev_bps - edge_minus_gate),
                    "depth_note": "n/a",
                    "risk_dollars": "0",
                    "spread_bps": str(spread),
                }
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
                    diag = {
                        "product_id": product_id,
                        "side": side,
                        "reason": "risk_cap",
                        "dev_bps": str(dev_bps),
                        "edge_minus_gate_bps": str(edge_minus_gate),
                        "notional": str(notional),
                        "gate_bps": str(dev_bps - edge_minus_gate),
                        "depth_note": "n/a",
                        "risk_dollars": str(risk),
                        "spread_bps": str(spread),
                    }
                    current_best = Decimal(str(best_diag.get("edge_minus_gate_bps", "-1000000"))) if best_diag else Decimal("-1000000")
                    if best_diag is None or edge_minus_gate > current_best:
                        best_diag = diag
                    continue
                risk = _risk_dollars(entry, sl, size)

            l2 = _get_l2(product_id, depth=max(5, cfg.depth_ticks + 2))
            depth_ok, depth_note = _depth_sufficient(side, entry, spec, l2, size, cfg)
            if not depth_ok:
                diag = {
                    "product_id": product_id,
                    "side": side,
                    "reason": "depth_check_failed",
                    "dev_bps": str(dev_bps),
                    "edge_minus_gate_bps": str(edge_minus_gate),
                    "notional": str((entry * size).quantize(Decimal("0.01"))),
                    "gate_bps": str(dev_bps - edge_minus_gate),
                    "depth_note": depth_note,
                    "risk_dollars": str(risk),
                    "spread_bps": str(spread),
                }
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
            diag = {
                "product_id": product_id,
                "side": side,
                "reason": "pass",
                "dev_bps": str(dev_bps),
                "edge_minus_gate_bps": str(edge_minus_gate),
                "notional": str((entry * size).quantize(Decimal("0.01"))),
                "gate_bps": str(dev_bps - edge_minus_gate),
                "depth_note": depth_note,
                "risk_dollars": str(risk),
                "spread_bps": str(spread),
            }
            if edge_minus_gate > best_edge:
                best_edge = edge_minus_gate
                best_ticket = ticket
                best_diag = diag

    if best_diag is None:
        best_diag = {
            "product_id": "",
            "side": "NONE",
            "reason": "no_products",
            "dev_bps": "0",
            "edge_minus_gate_bps": "0",
            "notional": "0",
            "gate_bps": "0",
            "depth_note": "n/a",
            "risk_dollars": "0",
            "spread_bps": "0",
        }

    return best_ticket, best_diag