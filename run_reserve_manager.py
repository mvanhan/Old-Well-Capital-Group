# run_reserve_manager.py
from __future__ import annotations

import os
import time
from decimal import Decimal, getcontext, ROUND_DOWN, ROUND_HALF_UP
from typing import Dict, Any, List, Optional, Tuple

# --- Load .env early (non-fatal if missing) ---
try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

getcontext().prec = 28

# ================== CONFIG (env-overridable) ================== #
TOKENS = [t.strip().upper() for t in os.getenv("OWCG_RES_TOKENS", "USDC,USDT,DAI").split(",") if t.strip()]
CHECK_INTERVAL_SECS = int(os.getenv("OWCG_RES_CHECK_INTERVAL", "15"))

FLOOR_USD = Decimal(os.getenv("OWCG_RES_FLOOR_USD", "25"))
TARGET_USD = Decimal(os.getenv("OWCG_RES_TARGET_USD", "100"))

USE_TAKER = os.getenv("OWCG_USE_TAKER", "true").lower() in ("1", "true", "yes", "y")
FEE_PAD_BPS = Decimal(os.getenv("OWCG_FEE_PAD_BPS", "2.0"))
MIN_ACTION_USD = Decimal(os.getenv("OWCG_MIN_ACTION_USD", "1.00"))

# New: verbose heartbeat so you can tell it's running (default ON)
VERBOSE = os.getenv("OWCG_RES_VERBOSE", "1").lower() in ("1", "true", "yes", "y")

class Pub:
    @staticmethod
    def get_product(product_id: str) -> Dict[str, Any]:
        from broker import coinbase_public as pub
        return pub.get_product(product_id)

    @staticmethod
    def get_best_bid_ask(product_id: str):
        from broker import coinbase_public as pub
        return pub.get_best_bid_ask(product_id)

class Priv:
    @staticmethod
    def get_balances() -> List[Dict[str, Any]]:
        from broker import coinbase_private as priv
        return priv.get_balances()

    @staticmethod
    def place_market(product_id: str, side: str, size: Optional[str] = None, funds: Optional[str] = None, client_oid: Optional[str] = None) -> Dict[str, Any]:
        from broker import coinbase_private as priv
        return priv.place_market(product_id, side, size=size, funds=funds, client_oid=client_oid)

    @staticmethod
    def get_order_status(order_id: str) -> Dict[str, Any]:
        from broker import coinbase_private as priv
        return priv.get_order_status(order_id)

    @staticmethod
    def convert_usd_to_usdc(amount_usd: Decimal) -> Dict[str, Any]:
        from broker import coinbase_private as priv
        return priv.convert_usd_to_usdc(str(amount_usd.quantize(Decimal("0.01"))))

# ================== UTILS ================== #
def _to_dec(x: Any) -> Decimal:
    if isinstance(x, Decimal): return x
    return Decimal(str(x))

def _quant(x: Decimal, inc: Decimal, rounding=ROUND_DOWN) -> Decimal:
    if inc == 0: return x
    q = (x / inc).to_integral_value(rounding=rounding)
    return (q * inc).quantize(inc)

def round_price(x: Decimal, inc: Decimal) -> Decimal:
    return _quant(x, inc, ROUND_HALF_UP)

def round_size(x: Decimal, inc: Decimal) -> Decimal:
    return _quant(x, inc, ROUND_DOWN)

def _parse_best_quote(obj: Any) -> Dict[str, Decimal]:
    bid = ask = None
    if isinstance(obj, (list, tuple)) and len(obj) >= 2:
        bid, ask = _to_dec(obj[0]), _to_dec(obj[1])
    elif isinstance(obj, dict):
        if "bid" in obj and "ask" in obj:
            bid, ask = _to_dec(obj["bid"]), _to_dec(obj["ask"])
        elif "best_bid" in obj and "best_ask" in obj:
            bid, ask = _to_dec(obj["best_bid"]), _to_dec(obj["best_ask"])
        elif "bids" in obj and "asks" in obj and obj["bids"] and obj["asks"]:
            bid, ask = _to_dec(obj["bids"][0][0]), _to_dec(obj["asks"][0][0])
    if bid is None or ask is None:
        raise ValueError("Unrecognized best quote shape.")
    return {"bid": bid, "ask": ask}

def _get_product_spec(product_id: str) -> Tuple[Decimal, Decimal, Decimal, Decimal]:
    p = Pub.get_product(product_id)
    price_inc = _to_dec(p.get("price_increment") or p.get("quote_increment") or "0.0001")
    size_inc  = _to_dec(p.get("base_increment")  or p.get("size_increment")  or "0.01")
    min_size  = _to_dec(p.get("base_min_size")   or p.get("min_size")        or size_inc)
    min_funds = _to_dec(p.get("min_market_funds") or "0.00")
    return price_inc, size_inc, min_size, min_funds

def _balances_map() -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for b in Priv.get_balances():
        cur = str(b.get("currency", "")).upper()
        if not cur: continue
        amt = b.get("available", b.get("balance", "0"))
        out[cur] = _to_dec(amt)
    return out

def _pair_exists(product_id: str) -> bool:
    try:
        Pub.get_product(product_id)
        return True
    except Exception:
        return False

def _buy_with_usd(product_id: str, usd_funds: Decimal, note: str):
    p_inc, s_inc, min_size, min_funds = _get_product_spec(product_id)
    funds = usd_funds.quantize(Decimal("0.01"))
    if funds < MIN_ACTION_USD:
        print(f"[reserve] {ts()} {note}: ${funds} < MIN_ACTION_USD ${MIN_ACTION_USD}")
        return "SKIPPED_MIN_FUNDS"
    if min_funds > 0 and funds < min_funds:
        print(f"[reserve] {ts()} {note}: funds {funds} < min_market_funds {min_funds} on {product_id}")
        return "SKIPPED_MIN_FUNDS"
    if USE_TAKER:
        resp = Priv.place_market(product_id, "BUY", size=None, funds=str(funds), client_oid=f"reserve_{product_id.replace('-','_')}_{int(time.time())}")
    else:
        q = _parse_best_quote(Pub.get_best_bid_ask(product_id))
        price = round_price(q["ask"], p_inc)
        size  = round_size((funds / price), s_inc)
        if size < min_size:
            print(f"[reserve] {ts()} {note}: computed size {size} < min_size {min_size} on {product_id}")
            return "SKIPPED_MIN_SIZE"
        from broker import coinbase_private as priv
        resp = priv.place_limit(product_id, "BUY", str(price), str(size), post_only=False, client_oid=f"reserve_{product_id.replace('-','_')}_{int(time.time())}")
    oid = resp.get("order_id") or resp.get("id") or resp.get("orderId")
    print(f"[reserve] {ts()} {note}: submitted {product_id} order id={oid}")
    return "SUBMITTED"

def _buy_with_usdc(product_id: str, usdc_funds: Decimal, note: str):
    p_inc, s_inc, min_size, min_funds = _get_product_spec(product_id)
    funds = usdc_funds.quantize(Decimal("0.01"))
    if funds < MIN_ACTION_USD:
        print(f"[reserve] {ts()} {note}: ${funds} < MIN_ACTION_USD ${MIN_ACTION_USD}")
        return "SKIPPED_MIN_FUNDS"
    if min_funds > 0 and funds < min_funds:
        print(f"[reserve] {ts()} {note}: funds {funds} < min_market_funds {min_funds} on {product_id}")
        return "SKIPPED_MIN_FUNDS"
    if USE_TAKER:
        resp = Priv.place_market(product_id, "BUY", size=None, funds=str(funds), client_oid=f"reserve_{product_id.replace('-','_')}_{int(time.time())}")
    else:
        q = _parse_best_quote(Pub.get_best_bid_ask(product_id))
        price = round_price(q["ask"], p_inc)
        size  = round_size((funds / price), s_inc)
        if size < min_size:
            print(f"[reserve] {ts()} {note}: computed size {size} < min_size {min_size} on {product_id}")
            return "SKIPPED_MIN_SIZE"
        from broker import coinbase_private as priv
        resp = priv.place_limit(product_id, "BUY", str(price), str(size), post_only=False, client_oid=f"reserve_{product_id.replace('-','_')}_{int(time.time())}")
    oid = resp.get("order_id") or resp.get("id") or resp.get("orderId")
    print(f"[reserve] {ts()} {note}: submitted {product_id} order id={oid}")
    return "SUBMITTED"

def _usd_to_usdc_fallback(spend: Decimal) -> bool:
    """If convert USD→USDC isn't supported, route USD→USDT, then USDT→USDC via USDC-USDT."""
    if not _pair_exists("USDT-USD"):
        return False
    print(f"[reserve] {ts()} Fallback: buying USDT with ${spend} via USDT-USD")
    _buy_with_usd("USDT-USD", spend, note="fallback USDT buy")
    if not _pair_exists("USDC-USDT"):
        print(f"[reserve] {ts()} [WARN] Fallback: USDC-USDT not available; keeping USDT.")
        return True
    pad = (Decimal("1.0") - (FEE_PAD_BPS / Decimal(10000)))
    usdt_to_use = (spend * pad).quantize(Decimal("0.01"))
    print(f"[reserve] {ts()} Fallback: swapping USDT→USDC via USDC-USDT (≈{usdt_to_use} USDT)")
    _buy_with_usdc("USDC-USDT", usdt_to_use, note="fallback USDC buy via USDT")
    return True

def _ensure_minimum(token: str, bal: Dict[str, Decimal]) -> Decimal:
    have = bal.get(token, Decimal("0"))
    if have >= FLOOR_USD:
        return Decimal("0")

    needed_to_target = max(Decimal("0"), TARGET_USD - have)
    usd_avail = bal.get("USD", Decimal("0"))
    spend = min(needed_to_target, usd_avail)
    if spend <= 0:
        print(f"[reserve] {ts()} [WARN] {token} below floor ({have} < {FLOOR_USD}) but no USD available.")
        return Decimal("0")
    if spend < MIN_ACTION_USD:
        print(f"[reserve] {ts()} [SKIP] {token} top-up ${spend} < MIN_ACTION_USD ${MIN_ACTION_USD}")
        return Decimal("0")

    if token == "USDC":
        try:
            print(f"[reserve] {ts()} Converting USD→USDC for ~${spend}")
            Priv.convert_usd_to_usdc(spend)
            return spend
        except Exception as e:
            print(f"[reserve] {ts()} [WARN] Convert USD→USDC failed ({type(e).__name__}: {e}); trying USDT route.")
            ok = _usd_to_usdc_fallback(spend)
            return spend if ok else Decimal("0")

    p_usd = f"{token}-USD"
    if _pair_exists(p_usd):
        print(f"[reserve] {ts()} Top-up {token} with ~${spend} via {p_usd}")
        _buy_with_usd(p_usd, spend, note=f"{token} floor refill")
        return spend

    print(f"[reserve] {ts()} {token} has no USD pair; converting USD→USDC and buying via USDC")
    try:
        Priv.convert_usd_to_usdc(spend)
    except Exception as e:
        print(f"[reserve] {ts()} [WARN] Convert USD→USDC failed ({type(e).__name__}: {e}); trying USDT route.")
        ok = _usd_to_usdc_fallback(spend)
        return spend if ok else Decimal("0")

    p_usdc = f"{token}-USDC"
    if not _pair_exists(p_usdc):
        print(f"[reserve] {ts()} [WARN] No {p_usdc} pair; leaving funds in USDC.")
        return spend
    pad = (Decimal("1.0") - (FEE_PAD_BPS / Decimal(10000)))
    usdc_to_use = (spend * pad).quantize(Decimal("0.01"))
    _buy_with_usdc(p_usdc, usdc_to_use, note=f"{token} via USDC hub")
    return spend

# ---------- pretty printing ---------- #
def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _fmt2(x: Decimal) -> str:
    try:
        return f"{x.quantize(Decimal('0.01'))}"
    except Exception:
        return str(x)

def _print_snapshot(bal: Dict[str, Decimal]):
    if not VERBOSE:
        return
    parts = [f"USD={_fmt2(bal.get('USD', Decimal('0')))}"]
    for t in TOKENS:
        parts.append(f"{t}={_fmt2(bal.get(t, Decimal('0')))}")
    print(f"[reserve] {ts()} tick | " + " | ".join(parts))
    print(f"[reserve] {ts()} sleeping {CHECK_INTERVAL_SECS}s...")

# =================== main loop =================== #
def run_forever():
    print(f"[reserve] {ts()} Manager starting. Tokens={TOKENS}, floor=${FLOOR_USD}, target=${TARGET_USD}, interval={CHECK_INTERVAL_SECS}s, verbose={VERBOSE}")
    while True:
        try:
            bal = _balances_map()
            _print_snapshot(bal)
            spent_total = Decimal("0")
            for t in TOKENS:
                spent_total += _ensure_minimum(t, bal)
                if spent_total > 0:
                    bal = _balances_map()  # refresh after actions
            if spent_total > 0:
                print(f"[reserve] {ts()} Cycle spent ≈ ${_fmt2(spent_total)}")
        except KeyboardInterrupt:
            print(f"[reserve] {ts()} Stopping manager.")
            break
        except Exception as e:
            print(f"[reserve] {ts()} [ERROR] {type(e).__name__}: {e}")
        time.sleep(CHECK_INTERVAL_SECS)

if __name__ == "__main__":
    run_forever()
