# strategies/stables_mean_reversion.py
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, getcontext, ROUND_DOWN, ROUND_HALF_UP
from typing import Dict, Any, Optional, Tuple, List
import time
import os

# Try to use your existing precision helpers; otherwise fall back.
try:
    from owcg_utils.precision import round_price, round_size
except Exception:
    def _quant(x: Decimal, increment: Decimal, rounding=ROUND_DOWN) -> Decimal:
        if increment == 0:
            return x
        q = (x / increment).to_integral_value(rounding=rounding)
        # quantize to increment's exponent to avoid long decimals
        exp = Decimal(str(increment))
        return (q * increment).quantize(exp)
    def round_price(x: Decimal, increment: Decimal) -> Decimal:
        return _quant(x, increment, rounding=ROUND_HALF_UP)
    def round_size(x: Decimal, increment: Decimal) -> Decimal:
        return _quant(x, increment, rounding=ROUND_DOWN)

# Public market data wrapper — ADAPT ME to your broker layer if needed.
try:
    from broker import coinbase_public as mkt
except Exception:
    mkt = None  # We'll guard calls.

getcontext().prec = 28

@dataclass
class StrategyConfig:
    products: List[str] = None  # e.g., ["USDT-USDC"]
    exit_bps: Decimal = Decimal("0.6")        # thin TP
    sl_bps: Decimal = Decimal("3.0")          # stop distance
    slippage_bps: Decimal = Decimal("0.2")
    maker_fee_bps: Decimal = Decimal("0.0")
    taker_fee_bps: Decimal = Decimal("0.0")
    cushion_bps: Decimal = Decimal("0.2")     # extra safety
    hold_minutes: int = 180

    # Depth validation near entry
    depth_ticks: int = 2
    min_depth_multiplier: Decimal = Decimal("1.10")  # depth >= 110% of base size

    # Sizing
    bankroll_pct: Decimal = Decimal("0.10")   # 10% of bankroll as target
    min_notional: Decimal = Decimal("50")
    max_notional: Decimal = Decimal("500")

    # Ticketing / output
    out_dir: str = "output_stables"

    def __post_init__(self):
        if self.products is None:
            self.products = ["USDT-USDC"]

@dataclass
class ProductSpec:
    price_increment: Decimal
    size_increment: Decimal
    min_size: Decimal

@dataclass
class Quote:
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    ts: float

@dataclass
class OrderbookSlice:
    bids: List[Tuple[Decimal, Decimal]]  # [(price, size), ...]
    asks: List[Tuple[Decimal, Decimal]]

@dataclass
class Ticket:
    product_id: str
    side: str  # BUY or SELL
    entry_price: Decimal
    tp_price: Decimal
    sl_price: Decimal
    base_size: Decimal
    dev_bps: Decimal
    rr: Decimal
    hold_minutes: int
    reason: str
    risk_dollars: Decimal
    post_only: bool = True  # parent is post-only; child stops may be taker
    bracket_desired: bool = True  # we prefer bracket when exchange allows

    def to_row(self) -> Dict[str, Any]:
        d = asdict(self)
        d["ts"] = int(time.time())
        return d

# ------------- Helpers to coerce various API shapes ------------- #

def _to_decimal(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))

def _parse_best_quote(obj: Any) -> Quote:
    """
    Accepts multiple shapes:
      - tuple/list: (bid, ask) or (bid, ask, bid_size, ask_size)
      - dict with 'bid'/'ask' (sizes optional)
      - dict with 'best_bid'/'best_ask'
      - dict with 'bids'/'asks' top-of-book arrays [[price, size], ...]
    """
    bid = ask = bid_sz = ask_sz = None

    if isinstance(obj, (list, tuple)):
        if len(obj) >= 2:
            bid = _to_decimal(obj[0])
            ask = _to_decimal(obj[1])
        if len(obj) >= 4:
            bid_sz = _to_decimal(obj[2])
            ask_sz = _to_decimal(obj[3])

    elif isinstance(obj, dict):
        if "bid" in obj and "ask" in obj:
            bid = _to_decimal(obj["bid"])
            ask = _to_decimal(obj["ask"])
            if "bid_size" in obj: bid_sz = _to_decimal(obj["bid_size"])
            if "ask_size" in obj: ask_sz = _to_decimal(obj["ask_size"])
        elif "best_bid" in obj and "best_ask" in obj:
            bid = _to_decimal(obj["best_bid"])
            ask = _to_decimal(obj["best_ask"])
            if "best_bid_size" in obj: bid_sz = _to_decimal(obj["best_bid_size"])
            if "best_ask_size" in obj: ask_sz = _to_decimal(obj["best_ask_size"])
        elif "bids" in obj and "asks" in obj and obj["bids"] and obj["asks"]:
            # Top-of-book from orderbook dict
            b0 = obj["bids"][0]
            a0 = obj["asks"][0]
            bid = _to_decimal(b0[0])
            ask = _to_decimal(a0[0])
            if len(b0) > 1: bid_sz = _to_decimal(b0[1])
            if len(a0) > 1: ask_sz = _to_decimal(a0[1])

    if bid is None or ask is None:
        raise ValueError("Unable to parse best quote shape.")

    if bid_sz is None: bid_sz = Decimal("0")
    if ask_sz is None: ask_sz = Decimal("0")
    return Quote(bid=bid, ask=ask, bid_size=bid_sz, ask_size=ask_sz, ts=time.time())

def _parse_orderbook_l2(obj: Any, depth: int = 5) -> Optional[OrderbookSlice]:
    """
    Accepts dict with 'bids'/'asks' arrays; each level may be [price, size] or dicts.
    Returns top N levels as Decimal tuples.
    """
    if not isinstance(obj, dict):
        return None
    bids_raw = obj.get("bids", [])
    asks_raw = obj.get("asks", [])

    def _as_px_sz(row) -> Tuple[Decimal, Decimal]:
        if isinstance(row, dict):
            px = _to_decimal(row.get("price"))
            sz = _to_decimal(row.get("size", row.get("quantity", "0")))
            return px, sz
        # assume [price, size, ...]
        px = _to_decimal(row[0])
        sz = _to_decimal(row[1]) if len(row) > 1 else Decimal("0")
        return px, sz

    bids = []
    asks = []
    for i, row in enumerate(bids_raw):
        if i >= depth: break
        try:
            bids.append(_as_px_sz(row))
        except Exception:
            continue
    for i, row in enumerate(asks_raw):
        if i >= depth: break
        try:
            asks.append(_as_px_sz(row))
        except Exception:
            continue
    return OrderbookSlice(bids=bids, asks=asks)

# ------------- Market helpers (ADAPT ME as needed) -------------- #

def _get_product_spec(product_id: str) -> Optional[ProductSpec]:
    """
    Tries several common Coinbase fields:
      - price_increment, base_increment, quote_increment
      - base_min_size or min_size as the minimum tradable base size
    """
    if mkt is None:
        return ProductSpec(Decimal("0.0001"), Decimal("0.01"), Decimal("0.01"))
    try:
        spec = mkt.get_product(product_id)  # expect a dict
        # Field normalization
        price_inc = spec.get("price_increment") or spec.get("quote_increment") or "0.0001"
        size_inc = spec.get("base_increment") or spec.get("size_increment") or "0.01"
        min_size = spec.get("base_min_size") or spec.get("min_size") or size_inc
        return ProductSpec(
            price_increment=_to_decimal(price_inc),
            size_increment=_to_decimal(size_inc),
            min_size=_to_decimal(min_size),
        )
    except Exception:
        # Sensible defaults for stables
        return ProductSpec(Decimal("0.0001"), Decimal("0.01"), Decimal("0.01"))

def _get_best_quote(product_id: str) -> Optional[Quote]:
    if mkt is None:
        # Fallback dummy (for tests)
        return Quote(Decimal("0.9999"), Decimal("1.0001"), Decimal("10000"), Decimal("10000"), time.time())
    raw = mkt.get_best_bid_ask(product_id)
    return _parse_best_quote(raw)

def _get_l2(product_id: str, depth: int = 5) -> Optional[OrderbookSlice]:
    if mkt is None:
        return None
    try:
        ob = mkt.get_orderbook(product_id, level=2)  # expect {"bids":[...], "asks":[...]}
        return _parse_orderbook_l2(ob, depth=depth)
    except Exception:
        return None

# --------------------- Core logic -------------------------------- #

def _bps(a: Decimal, b: Decimal) -> Decimal:
    # bps of difference between a and b, assuming around $1
    return (a - b) / b * Decimal(10000)

def _required_gate_bps(cfg: StrategyConfig) -> Decimal:
    fees = max(cfg.maker_fee_bps, cfg.taker_fee_bps)
    return cfg.exit_bps + cfg.slippage_bps + fees + cfg.cushion_bps

def _round_for_product(price: Decimal, size: Decimal, spec: ProductSpec) -> Tuple[Decimal, Decimal]:
    return round_price(price, spec.price_increment), round_size(size, spec.size_increment)

def _compute_size_for_bankroll(
    product_id: str,
    side: str,
    price: Decimal,
    cfg: StrategyConfig,
    balances: Dict[str, Decimal],
) -> Tuple[Decimal, Decimal, str]:
    """
    Returns (base_size, notional, reason_if_blocked)
    """
    # Crude bankroll across USD/USDT/USDC
    usd_like = balances.get("USD", Decimal("0")) + balances.get("USDT", Decimal("0")) + balances.get("USDC", Decimal("0"))
    target = (usd_like * cfg.bankroll_pct).quantize(Decimal("0.01"))
    if target < cfg.min_notional:
        target = cfg.min_notional
    if target > cfg.max_notional:
        target = cfg.max_notional

    # Balance caps by side
    if side == "BUY":
        # Spending quote (assume quote is the RHS currency)
        quote = product_id.split("-")[1]
        avail = balances.get(quote, Decimal("0"))
        if avail < target:
            target = avail
    else:
        # Selling base
        base = product_id.split("-")[0]
        avail = balances.get(base, Decimal("0"))
        if avail * price < target:
            target = (avail * price).quantize(Decimal("0.01"))

    if target <= Decimal("0"):
        return Decimal("0"), Decimal("0"), "insufficient_balance"

    base_size = (target / price).quantize(Decimal("0.0000001"))
    return base_size, target, ""

def _depth_sufficient(
    side: str,
    entry: Decimal,
    spec: ProductSpec,
    l2: Optional[OrderbookSlice],
    base_needed: Decimal,
    cfg: StrategyConfig,
) -> Tuple[bool, str]:
    if l2 is None or not l2.bids or not l2.asks:
        return True, "depth_unavailable_skip"

    tick = spec.price_increment
    if tick <= 0:
        return True, "no_tick_skip"

    if side == "BUY":
        band = entry + (tick * cfg.depth_ticks)
        cum = sum(sz for px, sz in l2.asks if px <= band)
    else:
        band = entry - (tick * cfg.depth_ticks)
        cum = sum(sz for px, sz in l2.bids if px >= band)

    needed = (base_needed * cfg.min_depth_multiplier).quantize(Decimal("0.0000001"))
    return (cum >= needed), (f"depth_ok {cum}>= {needed}" if cum >= needed else f"depth_insufficient {cum} < {needed}")

def scan_once(
    cfg: StrategyConfig,
    balances: Dict[str, Decimal],
) -> Tuple[Optional[Ticket], Dict[str, Any]]:
    """
    Returns (ticket_or_None, diag_row_dict)
    Always produces a diag row with a 'reason'.
    """
    os.makedirs(cfg.out_dir, exist_ok=True)
    gate = _required_gate_bps(cfg)

    for product_id in cfg.products:
        spec = _get_product_spec(product_id)
        try:
            q = _get_best_quote(product_id)
        except Exception as e:
            return None, {
                "product_id": product_id,
                "side": "NONE",
                "reason": f"quote_parse_error {e}",
                "ts": int(time.time()),
            }
        if q is None or spec is None:
            return None, {
                "product_id": product_id,
                "side": "NONE",
                "reason": "no_quote_or_spec",
                "ts": int(time.time()),
            }

        # Deviations vs 1.0000
        dev_buy_bps = _bps(q.bid, Decimal("1.0000"))  # negative bps triggers BUY
        dev_sell_bps = _bps(q.ask, Decimal("1.0000"))  # positive bps triggers SELL

        # Decide side by max absolute edge
        side = "NONE"
        dev = Decimal("0")
        if dev_sell_bps >= gate and dev_sell_bps >= abs(dev_buy_bps):
            side = "SELL"
            dev = dev_sell_bps
            entry = round_price(q.ask, spec.price_increment)
            tp = round_price(entry * (Decimal("1.0") - cfg.exit_bps / Decimal(10000)), spec.price_increment)
            sl = round_price(entry * (Decimal("1.0") + cfg.sl_bps / Decimal(10000)), spec.price_increment)
        elif abs(dev_buy_bps) >= gate and abs(dev_buy_bps) >= dev_sell_bps:
            side = "BUY"
            dev = abs(dev_buy_bps)
            entry = round_price(q.bid, spec.price_increment)
            tp = round_price(entry * (Decimal("1.0") + cfg.exit_bps / Decimal(10000)), spec.price_increment)
            sl = round_price(entry * (Decimal("1.0") - cfg.sl_bps / Decimal(10000)), spec.price_increment)
        else:
            # No side passes the dynamic gate
            return None, {
                "product_id": product_id,
                "side": "NONE",
                "reason": f"gate_not_met gate={gate}bps dev_buy={dev_buy_bps} dev_sell={dev_sell_bps}",
                "ts": int(time.time()),
            }

        base_size, notional, reason_bal = _compute_size_for_bankroll(product_id, side, entry, cfg, balances)
        if base_size <= 0:
            return None, {
                "product_id": product_id,
                "side": "NONE",
                "reason": reason_bal or "size_zero",
                "dev_bps": str(dev),
                "ts": int(time.time()),
            }

        # Round size & ensure min size
        base_size = round_size(base_size, spec.size_increment)
        if base_size < spec.min_size:
            return None, {
                "product_id": product_id,
                "side": "NONE",
                "reason": f"min_size_violation {base_size} < {spec.min_size}",
                "dev_bps": str(dev),
                "ts": int(time.time()),
            }

        # Depth validation
        l2 = _get_l2(product_id, depth=5)
        ok, depth_reason = _depth_sufficient(side, entry, spec, l2, base_size, cfg)
        if not ok:
            return None, {
                "product_id": product_id,
                "side": "NONE",
                "reason": depth_reason,
                "dev_bps": str(dev),
                "ts": int(time.time()),
            }

        # Risk metrics
        stop_dist = abs(entry - sl)
        risk_dollars = (stop_dist * base_size).quantize(Decimal("0.01"))
        reward = abs(entry - tp)
        rr = Decimal("0.0")
        if stop_dist > 0:
            rr = (reward / stop_dist).quantize(Decimal("0.01"))

        ticket = Ticket(
            product_id=product_id,
            side=side,
            entry_price=entry,
            tp_price=tp,
            sl_price=sl,
            base_size=base_size,
            dev_bps=dev.quantize(Decimal("0.01")),
            rr=rr,
            hold_minutes=cfg.hold_minutes,
            reason="ok",
            risk_dollars=risk_dollars,
        )
        diag = {
            **ticket.to_row(),
            "notional": str((entry * base_size).quantize(Decimal("0.01"))),
            "gate_bps": str(gate),
            "depth_note": depth_reason,
        }
        return ticket, diag

    # Should not reach; fallback
    return None, {
        "product_id": cfg.products[0] if cfg.products else "NA",
        "side": "NONE",
        "reason": "no_products",
        "ts": int(time.time()),
    }
