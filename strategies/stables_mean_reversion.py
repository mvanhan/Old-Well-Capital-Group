# strategies/stables_mean_reversion.py
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, getcontext, ROUND_DOWN
from typing import Dict, Any, Optional, Tuple, List
import os, time

# High precision for crypto ticks
getcontext().prec = 28

# ---- Precision helpers ----
try:
    from owcg_utils.precision import round_price, round_size
except Exception:
    def _q(x) -> Decimal:
        return x if isinstance(x, Decimal) else Decimal(str(x))
    def round_price(x, inc, mode="nearest") -> Decimal:
        x, inc = _q(x), _q(inc)
        if inc <= 0: return x
        n = (x / inc)
        if mode == "down":
            n = n.to_integral_value(rounding=ROUND_DOWN)
        else:
            n = (n + _q("0.5")).to_integral_value(rounding=ROUND_DOWN)
        return n * inc
    def round_size(x, inc, mode="down") -> Decimal:
        return round_price(x, inc, mode=mode)

# ---- Public/Private market wrappers (import lazily so tests can stub) ----
def _mkt_mod():
    if os.getenv("OWCG_OFFLINE") == "1":
        return None  # force offline/dummy
    try:
        from broker import coinbase_public as cb_pub  # type: ignore
        return cb_pub
    except Exception:
        return None

# ---- Data containers ----
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
    product_id: str
    side: str         # "BUY" or "SELL"
    entry_price: Decimal
    size: Decimal
    tp_price: Decimal
    sl_price: Decimal
    post_only: bool = True
    bracket_desired: bool = True
    client_tag: str = "stables_mr"

    def to_row(self) -> Dict[str, str]:
        d = asdict(self)
        d["entry_price"] = str(self.entry_price)
        d["size"] = str(self.size)
        d["tp_price"] = str(self.tp_price)
        d["sl_price"] = str(self.sl_price)
        d["ts"] = str(self.ts)
        return d

@dataclass
class StrategyConfig:
    products: List[str]                               # e.g. ["USDT-USD","USDC-USD","USDT-USDC"]
    out_dir: str = "output_stables"
    # fees/slippage/cushion in bps
    maker_fee_bps: Decimal = Decimal("0.0")
    taker_fee_bps: Decimal = Decimal("0.0")
    exit_bps: Decimal = Decimal("4.0")               # TP distance
    sl_bps: Decimal = Decimal("6.0")                 # SL distance
    slippage_bps: Decimal = Decimal("1.0")
    cushion_bps: Decimal = Decimal("0.2")
    hold_minutes: int = 180

    # Depth validation
    depth_ticks: int = 2
    min_depth_multiplier: Decimal = Decimal("1.10")   # book size >= 110% of order size
    block_on_missing_l2: bool = True

    # Sizing
    bankroll_usd: Decimal = Decimal("100.00")
    bankroll_pct: Decimal = Decimal("0.10")           # 10% of bankroll as target notional
    min_notional: Decimal = Decimal("5.00")
    max_notional: Decimal = Decimal("500.00")
    max_risk_usd: Decimal = Decimal("3.00")           # hard cap on |entry - SL| * size

    # Tick separation for TP/SL
    min_tp_ticks: int = 1
    min_sl_ticks: int = 1

# ---- Helpers ----
def _to_decimal(x: Any) -> Optional[Decimal]:
    try:
        if x is None: return None
        return x if isinstance(x, Decimal) else Decimal(str(x))
    except Exception:
        return None

def _get_product_spec(product_id: str) -> ProductSpec:
    mkt = _mkt_mod()
    if mkt is None:
        # Offline: default 1e-4 tick, 1e-2 size, min 1
        return ProductSpec(product_id, "USDT", "USD", Decimal("0.0001"), Decimal("0.01"), Decimal("1"))
    for p in mkt.get_products():
        if str(p.get("product_id")) == product_id:
            base = p.get("base_currency_id") or p.get("base_currency") or ""
            quote = p.get("quote_currency_id") or p.get("quote_currency") or ""
            price_inc = _to_decimal(p.get("price_increment") or p.get("quote_increment") or "0.0001") or Decimal("0.0001")
            size_inc  = _to_decimal(p.get("base_increment") or "0.01") or Decimal("0.01")
            min_size  = _to_decimal(p.get("min_order_size") or p.get("base_min_size") or p.get("min_order") or "1") or Decimal("1")
            return ProductSpec(product_id, base, quote, price_inc, size_inc, min_size)
    # Fallback
    return ProductSpec(product_id, "USDT", "USD", Decimal("0.0001"), Decimal("0.01"), Decimal("1"))

def _get_best_bid_ask(product_id: str) -> Tuple[Decimal, Decimal]:
    mkt = _mkt_mod()
    if mkt is None:
        # Offline dummy around parity
        return Decimal("0.9999"), Decimal("1.0001")
    bid, ask = mkt.get_best_bid_ask(product_id)
    b = _to_decimal(bid) or Decimal("0")
    a = _to_decimal(ask) or Decimal("0")
    return b, a

def _get_l2(product_id: str, depth: int = 5) -> Optional[L2]:
    mkt = _mkt_mod()
    if mkt is None:
        return L2(bids=[(Decimal("0.9999"), Decimal("10000"))],
                  asks=[(Decimal("1.0001"), Decimal("10000"))])
    try:
        book = mkt.get_l2(product_id, depth=depth)  # expected: {"bids":[[p,s],...], "asks":[...]}
        bids = []
        asks = []
        for row in (book.get("bids") or []):
            px = _to_decimal(row[0]); sz = _to_decimal(row[1])
            if px and sz: bids.append((px, sz))
        for row in (book.get("asks") or []):
            px = _to_decimal(row[0]); sz = _to_decimal(row[1])
            if px and sz: asks.append((px, sz))
        return L2(bids=bids, asks=asks)
    except Exception:
        return None

def _bps(a: Decimal, b: Decimal) -> Decimal:
    if b == 0: return Decimal("0")
    return (a / b - Decimal("1.0")) * Decimal("10000")

def _required_gate_bps(cfg: StrategyConfig) -> Decimal:
    # entry (maker) + worst exit (tp maker vs sl taker) + slippage + cushion
    entry_fee = cfg.maker_fee_bps
    worst_exit = max(cfg.maker_fee_bps, cfg.taker_fee_bps)
    return cfg.exit_bps + cfg.slippage_bps + entry_fee + worst_exit + cfg.cushion_bps

def _compute_size_for_bankroll(product_id: str, side: str, entry: Decimal, cfg: StrategyConfig, balances: Dict[str, Decimal]) -> Tuple[Decimal, Decimal, Optional[str]]:
    # target notional
    target = (cfg.bankroll_usd * cfg.bankroll_pct).quantize(Decimal("0.01"))
    target = max(cfg.min_notional, min(cfg.max_notional, target))
    size = (target / entry).quantize(Decimal("0.00000001"))
    # balance cap
    spec = _get_product_spec(product_id)
    base, quote = spec.base, spec.quote
    bal_base = balances.get(base, Decimal("0"))
    bal_quote = balances.get(quote, Decimal("0"))
    if side == "SELL":
        size = min(size, bal_base)  # cannot sell more than base balance
        if size <= 0:
            return Decimal("0"), target, "insufficient_balance"
    else:  # BUY
        max_buy = (bal_quote / entry).quantize(Decimal("0.00000001"))
        size = min(size, max_buy)
        if size <= 0:
            return Decimal("0"), target, "insufficient_balance"
    return size, target, None

def _depth_sufficient(side: str, entry: Decimal, spec: ProductSpec, l2: Optional[L2], size: Decimal, cfg: StrategyConfig) -> Tuple[bool, str]:
    if l2 is None or not l2.bids or not l2.asks:
        return ((not cfg.block_on_missing_l2), ("no_l2" if cfg.block_on_missing_l2 else "depth_unavailable_skip"))
    if side == "SELL":
        # ensure bids N ticks below entry have enough size
        step = spec.price_increment * cfg.depth_ticks
        thresh = entry - step
        agg = sum(sz for (px, sz) in l2.bids if px >= thresh)
    else:
        step = spec.price_increment * cfg.depth_ticks
        thresh = entry + step
        agg = sum(sz for (px, sz) in l2.asks if px <= thresh)
    need = size * cfg.min_depth_multiplier
    return (agg >= need, f"depth_{'ok' if agg >= need else 'thin'} agg={agg} need={need}")

def _enforce_min_tick_separation(side: str, entry: Decimal, tp: Decimal, sl: Decimal, spec: ProductSpec, cfg: StrategyConfig) -> Tuple[Decimal, Decimal]:
    tick = spec.price_increment
    if side == "SELL":
        # tp below entry; sl above entry
        min_tp = entry - tick * cfg.min_tp_ticks
        min_sl = entry + tick * cfg.min_sl_ticks
        if tp >= entry: tp = round_price(min_tp, tick)
        if sl <= entry: sl = round_price(min_sl, tick)
    else:  # BUY
        min_tp = entry + tick * cfg.min_tp_ticks
        min_sl = entry - tick * cfg.min_sl_ticks
        if tp <= entry: tp = round_price(min_tp, tick)
        if sl >= entry: sl = round_price(min_sl, tick)
    return tp, sl

# ---- Main scan ----
def scan_once(cfg: StrategyConfig, balances: Dict[str, Decimal]) -> Tuple[Optional[Ticket], Dict[str, Any]]:
    """
    Returns (ticket_or_None, diag_row_dict). Always emits a diag row.
    """
    os.makedirs(cfg.out_dir, exist_ok=True)
    gate = _required_gate_bps(cfg)

    best: Optional[Tuple[Decimal, Ticket, Dict[str, Any]]] = None

    for product_id in cfg.products:
        spec = _get_product_spec(product_id)
        bid, ask = _get_best_bid_ask(product_id)
        if bid <= 0 or ask <= 0 or bid >= ask:
            # no spread or invalid quote
            diag = {"product_id": product_id, "side": "NONE", "reason": f"bad_quote bid={bid} ask={ask}", "ts": int(time.time())}
            if best is None: best = (Decimal("-1"), None, diag)  # keep first diag if nothing passes
            continue

        # deviations vs parity 1.0000
        dev_buy_bps  = _bps(bid, Decimal("1.0000"))  # negative → BUY edge magnitude = abs
        dev_sell_bps = _bps(ask, Decimal("1.0000"))  # positive → SELL

        side = "NONE"; dev = Decimal("0")
        if dev_sell_bps >= gate and dev_sell_bps >= abs(dev_buy_bps):
            side = "SELL"; dev = dev_sell_bps
            entry = round_price(ask, spec.price_increment)
            tp = round_price(entry * (Decimal("1.0") - cfg.exit_bps / Decimal(10000)), spec.price_increment)
            sl = round_price(entry * (Decimal("1.0") + cfg.sl_bps   / Decimal(10000)), spec.price_increment)
        elif abs(dev_buy_bps) >= gate and abs(dev_buy_bps) >= dev_sell_bps:
            side = "BUY"; dev = abs(dev_buy_bps)
            entry = round_price(bid, spec.price_increment)
            tp = round_price(entry * (Decimal("1.0") + cfg.exit_bps / Decimal(10000)), spec.price_increment)
            sl = round_price(entry * (Decimal("1.0") - cfg.sl_bps   / Decimal(10000)), spec.price_increment)
        else:
            # no gate pass; keep a diag
            diag = {"product_id": product_id, "side": "NONE", "reason": f"gate_not_met gate={gate}bps dev_buy={dev_buy_bps} dev_sell={dev_sell_bps}", "ts": int(time.time())}
            if best is None: best = (Decimal("-1"), None, diag)
            continue

        # Enforce min tick separation
        tp, sl = _enforce_min_tick_separation(side, entry, tp, sl, spec, cfg)

        # Size by bankroll and balances
        base_size, target_notional, reason_bal = _compute_size_for_bankroll(product_id, side, entry, cfg, balances)
        if base_size <= 0:
            diag = {"product_id": product_id, "side": "NONE", "reason": reason_bal or "size_zero", "dev_bps": str(dev), "ts": int(time.time())}
            if best is None: best = (Decimal("-1"), None, diag)
            continue

        # Round/validate min size
        base_size = round_size(base_size, spec.size_increment)
        if base_size < spec.min_size:
            diag = {"product_id": product_id, "side": "NONE", "reason": f"min_size_violation {base_size} < {spec.min_size}", "dev_bps": str(dev), "ts": int(time.time())}
            if best is None: best = (Decimal("-1"), None, diag)
            continue

        # Depth check
        l2 = _get_l2(product_id, depth=5)
        ok_depth, depth_reason = _depth_sufficient(side, entry, spec, l2, base_size, cfg)
        if not ok_depth:
            diag = {"product_id": product_id, "side": "NONE", "reason": depth_reason, "dev_bps": str(dev), "ts": int(time.time())}
            if best is None: best = (Decimal("-1"), None, diag)
            continue

        # Risk dollars cap
        stop_dist = abs(entry - sl)
        risk_dollars = (stop_dist * base_size).quantize(Decimal("0.01"))
        if stop_dist <= 0:
            diag = {"product_id": product_id, "side": "NONE", "reason": "zero_stop_dist", "dev_bps": str(dev), "ts": int(time.time())}
            if best is None: best = (Decimal("-1"), None, diag)
            continue
        if risk_dollars > cfg.max_risk_usd:
            # shrink size to fit cap
            adj_size = (cfg.max_risk_usd / stop_dist).quantize(spec.size_increment, rounding=ROUND_DOWN)
            if adj_size < spec.min_size:
                diag = {"product_id": product_id, "side": "NONE", "reason": "risk_cap_min_size_block", "dev_bps": str(dev), "ts": int(time.time())}
                if best is None: best = (Decimal("-1"), None, diag)
                continue
            base_size = adj_size

        # Candidate ticket
        ticket = Ticket(
            ts=int(time.time()),
            product_id=product_id,
            side=side,
            entry_price=entry,
            size=base_size,
            tp_price=tp,
            sl_price=sl,
            post_only=True,
            bracket_desired=True,
            client_tag="stables_mr",
        )

        # Score by (edge - gate); pick best
        edge = dev - gate
        diag = {
            "product_id": product_id,
            "side": side,
            "reason": "pass",
            "dev_bps": str(dev),
            "edge_minus_gate_bps": str(edge),
            "notional": str((entry * base_size).quantize(Decimal("0.01"))),
            "gate_bps": str(gate),
            "depth_note": depth_reason,
            "risk_dollars": str(risk_dollars),
            "ts": ticket.ts,
        }
        if best is None or edge > best[0]:
            best = (edge, ticket, diag)

    # Return best passing candidate if any; else the last diag we saved
    if best is not None and best[1] is not None:
        return best[1], best[2]
    # fallback diag
    if best is not None:
        return None, best[2]
    return None, {"product_id": "NA", "side": "NONE", "reason": "no_products", "ts": int(time.time())}
