# run_seed_reserves_multi.py
from __future__ import annotations

import os, time
from decimal import Decimal, getcontext, ROUND_DOWN, ROUND_HALF_UP
from typing import Dict, Any, List, Optional, Tuple, Iterable

# --- Load .env early (non-fatal if missing) ---
try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

getcontext().prec = 28

# ================= USER CONFIG (override via env vars) =================
TOKENS = [t.strip().upper() for t in os.getenv("OWCG_TOKENS", "USDC,USDT,DAI").split(",") if t.strip()]
RAW_WEIGHTS = os.getenv("OWCG_WEIGHTS", "1,1,1")  # or "USDC:2,USDT:1,DAI:1"

BUDGET_USD_ENV = os.getenv("OWCG_BUDGET_USD")  # e.g., "140" (RECOMMENDED)
USE_TAKER = os.getenv("OWCG_USE_TAKER", "true").lower() in ("1","true","yes","y")

USD_PRODUCT = {t: f"{t}-USD" for t in TOKENS}
USDC_PRODUCT = {t: f"{t}-USDC" for t in TOKENS if t != "USDC"}

FEE_PAD_BPS = Decimal(os.getenv("OWCG_FEE_PAD_BPS", "2.0"))
TIMEOUT_SECS = int(os.getenv("OWCG_ORDER_TIMEOUT_SECS", "25"))

FAKE_BAL_ENV = os.getenv("OWCG_FAKE_BALANCES", "").strip()

ADV_KEY = os.getenv("COINBASE_API_KEY") or os.getenv("COINBASE_API_KEY_ID") or os.getenv("CB_ADV_KEY")
ADV_SECRET = os.getenv("COINBASE_API_SECRET") or os.getenv("COINBASE_PRIVATE_KEY") or os.getenv("CB_ADV_SECRET")
ADV_PASSPHRASE = os.getenv("COINBASE_API_PASSPHRASE") or os.getenv("CB_ADV_PASSPHRASE")

# New: minimum size guard to avoid tiny ops that often fail upstream
MIN_ACTION_USD = Decimal(os.getenv("OWCG_MIN_ACTION_USD", "1.00"))

# ================== BROKER ADAPTERS (robust) ===================
class Pub:
    @staticmethod
    def get_product(product_id: str) -> Dict[str, Any]:
        from broker import coinbase_public as pub
        return pub.get_product(product_id)

    @staticmethod
    def get_best_bid_ask(product_id: str) -> Any:
        from broker import coinbase_public as pub
        return pub.get_best_bid_ask(product_id)

class Priv:
    @staticmethod
    def _first_available(mod, fn_names: Iterable[str]):
        for name in fn_names:
            if hasattr(mod, name):
                fn = getattr(mod, name)
                if callable(fn):
                    return fn, name
        return None, None

    @staticmethod
    def _balances_via_broker() -> Optional[List[Dict[str, Any]]]:
        try:
            from broker import coinbase_private as priv
        except Exception:
            return None
        candidates = ["get_balances","list_balances","balances","get_accounts","list_accounts","accounts"]
        fn, fname = Priv._first_available(priv, candidates)
        if fn is None:
            for attr in ("client","api","rest","cb","adv"):
                obj = getattr(priv, attr, None)
                if obj and hasattr(obj, "list_accounts") and callable(getattr(obj, "list_accounts")):
                    try:
                        res = obj.list_accounts()
                        return _normalize_balances_result(res, used=f"{attr}.list_accounts")
                    except Exception:
                        pass
            return None
        try:
            res = fn()
            return _normalize_balances_result(res, used=fname)
        except Exception:
            return None

    @staticmethod
    def _balances_via_advanced_trade() -> Optional[List[Dict[str, Any]]]:
        if not (ADV_KEY and ADV_SECRET):
            return None
        try:
            try:
                from coinbase.rest import RESTClient as _Client
                client = _Client(api_key=ADV_KEY, api_secret=ADV_SECRET, api_passphrase=ADV_PASSPHRASE)
            except Exception:
                try:
                    from coinbase.rest import Client as _Client
                    client = _Client(api_key=ADV_KEY, api_secret=ADV_SECRET, api_passphrase=ADV_PASSPHRASE)
                except Exception:
                    return None
            res = None
            for name in ("get_accounts", "list_accounts", "accounts"):
                if hasattr(client, name) and callable(getattr(client, name)):
                    try:
                        res = getattr(client, name)()
                        break
                    except Exception:
                        continue
            if res is None:
                return None
            return _normalize_balances_result(res, used="advanced_trade_client")
        except Exception:
            return None

    @staticmethod
    def get_balances() -> List[Dict[str, Any]]:
        rows = Priv._balances_via_broker()
        if rows:
            return rows
        rows = Priv._balances_via_advanced_trade()
        if rows:
            return rows
        if FAKE_BAL_ENV:
            print("[seed][INFO] Using OWCG_FAKE_BALANCES as fallback for balances.")
            return _parse_fake_balances(FAKE_BAL_ENV)
        raise AttributeError(
            "No balance/accounts function found and Advanced Trade fallback not configured. "
            "Use a wrapper method, set COINBASE_API_KEY/COINBASE_API_SECRET in your .env, "
            "or use OWCG_FAKE_BALANCES='USD:140,DAI:25' for a quick run."
        )

    @staticmethod
    def place_market(product_id: str, side: str, size: Optional[str] = None, funds: Optional[str] = None, client_oid: Optional[str] = None) -> Dict[str, Any]:
        from broker import coinbase_private as priv
        if hasattr(priv, "place_market"):
            return priv.place_market(product_id, side, size=size, funds=funds, client_oid=client_oid)
        q = _parse_best_quote(Pub.get_best_bid_ask(product_id))
        price = q["ask"] if side.upper() == "BUY" else q["bid"]
        if size is None and funds is not None:
            base_size = (Decimal(funds) / price).quantize(Decimal("0.0000001"))
            size = str(base_size)
        if hasattr(priv, "place_limit"):
            return priv.place_limit(product_id, side, str(price), size, post_only=False, client_oid=client_oid)
        raise NotImplementedError("No place_market/place_limit available in coinbase_private.")

    @staticmethod
    def place_limit(product_id: str, side: str, price: str, size: str, post_only: bool, client_oid: Optional[str] = None) -> Dict[str, Any]:
        from broker import coinbase_private as priv
        if hasattr(priv, "place_limit"):
            return priv.place_limit(product_id, side, price, size, post_only=post_only, client_oid=client_oid)
        raise NotImplementedError("place_limit not available; please map to your wrapper.")

    @staticmethod
    def get_order_status(order_id: str) -> Dict[str, Any]:
        from broker import coinbase_private as priv
        if hasattr(priv, "get_order_status"):
            return priv.get_order_status(order_id)
        if hasattr(priv, "order_status"):
            return priv.order_status(order_id)
        return {"status": "SUBMITTED", "id": order_id}

    @staticmethod
    def convert_usd_to_usdc(amount_usd: Decimal) -> Dict[str, Any]:
        """Use Advanced Trade Convert endpoint for USD→USDC."""
        from broker import coinbase_private as priv
        amt = str(amount_usd.quantize(Decimal("0.01")))
        return priv.convert_usd_to_usdc(amt)

# ============================ UTILS ===================================
def _parse_fake_balances(s: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for part in s.split(","):
        if ":" in part:
            k, v = part.split(":")
            out.append({"currency": k.strip().upper(), "available": str(Decimal(v.strip()))})
    return out

def _normalize_balances_result(result: Any, used: str) -> List[Dict[str, Any]]:
    if hasattr(result, "json") and callable(getattr(result, "json")):
        try:
            result = result.json()
        except Exception:
            pass
    rows: List[Any] = []
    if isinstance(result, dict):
        if "accounts" in result and isinstance(result["accounts"], list):
            rows = result["accounts"]
        elif "data" in result and isinstance(result["data"], list):
            rows = result["data"]
        elif "results" in result and isinstance(result["results"], list):
            rows = result["results"]
        else:
            if all(isinstance(v, (int, float, str, dict)) for v in result.values()):
                rows = [{"currency": k, "available": (v if not isinstance(v, dict) else v.get("available", v.get("balance", "0")))} for k, v in result.items()]
    elif isinstance(result, list):
        rows = result
    else:
        raise TypeError(f"Unrecognized balances/accounts shape from '{used}': {type(result).__name__}")
    norm: List[Dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            cur = r.get("currency") or r.get("asset") or r.get("symbol") or r.get("name")
            avail = None
            if "available" in r: avail = r["available"]
            elif "available_balance" in r and isinstance(r["available_balance"], dict): avail = r["available_balance"].get("value")
            elif "balance" in r: avail = r["balance"]
            elif "free" in r: avail = r["free"]
            elif "qty" in r: avail = r["qty"]
            elif "quantity" in r: avail = r["quantity"]
            if cur:
                norm.append({"currency": str(cur).upper(), "available": str(avail if avail is not None else "0")})
        elif isinstance(r, (list, tuple)) and len(r) >= 2:
            norm.append({"currency": str(r[0]).upper(), "available": str(r[1])})
    if not norm:
        raise TypeError(f"No recognizable balance rows in result from '{used}'.")
    return norm

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
    rows = Priv.get_balances()
    out: Dict[str, Decimal] = {}
    for b in rows:
        cur = str(b.get("currency", "")).upper()
        if not cur: continue
        amt = b.get("available", b.get("balance", "0"))
        out[cur] = _to_dec(amt)
    for k, v in list(out.items()):
        if v < Decimal("0") and abs(v) < Decimal("0.0000001"):
            out[k] = Decimal("0")
    return out

def _budget_usd(available_usd: Decimal) -> Decimal:
    if not BUDGET_USD_ENV:
        raise ValueError("OWCG_BUDGET_USD not set. Export e.g. OWCG_BUDGET_USD=140 to cap spend.")
    b = _to_dec(BUDGET_USD_ENV)
    return b if b < available_usd else available_usd

def _parse_weights(tokens: List[str]) -> Dict[str, Decimal]:
    parts = [p.strip() for p in RAW_WEIGHTS.split(",")]
    weights: Dict[str, Decimal] = {}
    if all(":" in p for p in parts):
        for p in parts:
            k, v = p.split(":")
            weights[k.strip().upper()] = _to_dec(v)
    else:
        vals = [ _to_dec(v) for v in parts ]
        if len(vals) != len(tokens):
            raise ValueError("OWCG_WEIGHTS length must match OWCG_TOKENS.")
        for i, t in enumerate(tokens):
            weights[tokens[i]] = vals[i]
    for k, v in list(weights.items()):
        if v <= 0: weights[k] = Decimal("0")
    return weights

def _wait_done(order_id: Optional[str], timeout: int = TIMEOUT_SECS) -> str:
    if not order_id: return "SUBMITTED"
    t0 = time.time()
    last = "PENDING"
    while time.time() - t0 < timeout:
        st = Priv.get_order_status(order_id)
        s = (st.get("status") or st.get("order_status") or "").upper()
        last = s or last
        if s in ("FILLED","DONE","MATCHED","CLOSED","FILLED_PARTIAL"):
            return s
        time.sleep(0.5)
    return last

def _pair_exists(product_id: str) -> bool:
    try:
        Pub.get_product(product_id)
        return True
    except Exception:
        return False

# ======================== CORE BUY/CONVERT LOGIC ===========================
def _buy_with_usd(product_id: str, usd_funds: Decimal, note: str):
    p_inc, s_inc, min_size, min_funds = _get_product_spec(product_id)
    funds = usd_funds.quantize(Decimal("0.01"))
    if funds < MIN_ACTION_USD:
        print(f"[seed][SKIP] {note}: ${funds} < MIN_ACTION_USD ${MIN_ACTION_USD}")
        return "SKIPPED_MIN_FUNDS"
    if min_funds > 0 and funds < min_funds:
        print(f"[seed][SKIP] {note}: funds {funds} < min_market_funds {min_funds} on {product_id}")
        return "SKIPPED_MIN_FUNDS"
    if USE_TAKER:
        resp = Priv.place_market(product_id, "BUY", size=None, funds=str(funds), client_oid=f"seed_{product_id.replace('-','_')}_{int(time.time())}")
    else:
        q = _parse_best_quote(Pub.get_best_bid_ask(product_id))
        price = round_price(q["ask"], p_inc)
        size  = round_size((funds / price), s_inc)
        if size < min_size:
            print(f"[seed][SKIP] {note}: computed size {size} < min_size {min_size} on {product_id}")
            return "SKIPPED_MIN_SIZE"
        resp = Priv.place_limit(product_id, "BUY", str(price), str(size), post_only=False, client_oid=f"seed_{product_id.replace('-','_')}_{int(time.time())}")
    oid = resp.get("order_id") or resp.get("id") or resp.get("orderId")
    st = _wait_done(oid)
    print(f"[seed] {note}: {product_id} BUY status: {st}")
    return st

def _buy_with_usdc(product_id: str, usdc_funds: Decimal, note: str):
    p_inc, s_inc, min_size, min_funds = _get_product_spec(product_id)
    funds = usdc_funds.quantize(Decimal("0.01"))
    if funds < MIN_ACTION_USD:
        print(f"[seed][SKIP] {note}: ${funds} < MIN_ACTION_USD ${MIN_ACTION_USD}")
        return "SKIPPED_MIN_FUNDS"
    if min_funds > 0 and funds < min_funds:
        print(f"[seed][SKIP] {note}: funds {funds} < min_market_funds {min_funds} on {product_id}")
        return "SKIPPED_MIN_FUNDS"
    if USE_TAKER:
        resp = Priv.place_market(product_id, "BUY", size=None, funds=str(funds), client_oid=f"seed_{product_id.replace('-','_')}_{int(time.time())}")
    else:
        q = _parse_best_quote(Pub.get_best_bid_ask(product_id))
        price = round_price(q["ask"], p_inc)
        size  = round_size((funds / price), s_inc)
        if size < min_size:
            print(f"[seed][SKIP] {note}: computed size {size} < min_size {min_size} on {product_id}")
            return "SKIPPED_MIN_SIZE"
        resp = Priv.place_limit(product_id, "BUY", str(price), str(size), post_only=False, client_oid=f"seed_{product_id.replace('-','_')}_{int(time.time())}")
    oid = resp.get("order_id") or resp.get("id") or resp.get("orderId")
    st = _wait_done(oid)
    print(f"[seed] {note}: {product_id} BUY status: {st}")
    return st

def _usd_to_usdc_via_usdt(spend: Decimal) -> bool:
    """Fallback route if convert USD→USDC is unsupported: buy USDT, then swap to USDC."""
    # Step 1: buy USDT with USD
    if not _pair_exists("USDT-USD"):
        return False
    print(f"[seed] Fallback: buying USDT with ${spend} via USDT-USD")
    r1 = _buy_with_usd("USDT-USD", spend, note="fallback USDT buy")
    # Step 2: buy USDC with USDT (quote = USDT)
    if not _pair_exists("USDC-USDT"):
        print("[seed][WARN] Fallback: USDC-USDT not available; keeping USDT.")
        return r1 == "FILLED" or r1 == "DONE" or r1 == "SUBMITTED"
    pad = (Decimal("1.0") - (FEE_PAD_BPS / Decimal(10000)))
    usdt_to_use = (spend * pad).quantize(Decimal("0.01"))
    print(f"[seed] Fallback: swapping USDT→USDC via USDC-USDT (≈{usdt_to_use} USDT)")
    _buy_with_usdc("USDC-USDT", usdt_to_use, note="fallback USDC buy via USDT")
    return True

def _ensure_token_with_budget(token: str, need_usd: Decimal, budget_left: Decimal) -> Decimal:
    spend = min(need_usd, budget_left)
    if spend <= 0:
        return Decimal("0")
    if spend < MIN_ACTION_USD:
        print(f"[seed][SKIP] Requested ${spend} for {token}, below MIN_ACTION_USD ${MIN_ACTION_USD}")
        return Decimal("0")

    if token == "USDC":
        try:
            print(f"[seed] Converting USD→USDC for ~${spend}")
            Priv.convert_usd_to_usdc(spend)
            return spend
        except Exception as e:
            print(f"[seed][WARN] Convert USD→USDC failed ({type(e).__name__}: {e}); trying USDT route.")
            ok = _usd_to_usdc_via_usdt(spend)
            return spend if ok else Decimal("0")

    p_usd = USD_PRODUCT.get(token, f"{token}-USD")
    if _pair_exists(p_usd):
        print(f"[seed] Buying {token} ~${spend} via {p_usd}")
        _buy_with_usd(p_usd, spend, note=f"{token} direct USD")
        return spend

    print(f"[seed] {token} has no USD pair; converting to USDC then buying {token}")
    try:
        Priv.convert_usd_to_usdc(spend)
    except Exception as e:
        print(f"[seed][WARN] Convert USD→USDC failed ({type(e).__name__}: {e}); trying USDT route.")
        ok = _usd_to_usdc_via_usdt(spend)
        return spend if ok else Decimal("0")

    p_usdc = USDC_PRODUCT.get(token, f"{token}-USDC")
    if not _pair_exists(p_usdc):
        print(f"[seed][WARN] No {p_usdc} pair to hub-convert into {token}. Leaving funds in USDC.")
        return spend
    pad = (Decimal("1.0") - (FEE_PAD_BPS / Decimal(10000)))
    usdc_to_use = (spend * pad).quantize(Decimal("0.01"))
    _buy_with_usdc(p_usdc, usdc_to_use, note=f"{token} via USDC hub")
    return spend

# =========================== PLANNER ===============================
def _current_token_usd_values(bal: Dict[str, Decimal], tokens: List[str]) -> Dict[str, Decimal]:
    return {t: max(Decimal("0"), bal.get(t, Decimal("0"))) for t in tokens}

def seed_reserves_proportional():
    tokens = TOKENS
    weights = _parse_weights(tokens)

    bal = _balances_map()
    usd_avail = bal.get("USD", Decimal("0"))
    if usd_avail <= 0 and not FAKE_BAL_ENV:
        raise RuntimeError("No USD available to seed reserves. (Set OWCG_FAKE_BALANCES for dev testing.)")
    budget = _budget_usd(usd_avail if usd_avail > 0 else Decimal("0"))

    current = _current_token_usd_values(bal, tokens)
    pool = budget + sum(current.values())
    wsum = sum(weights.get(t, Decimal("0")) for t in tokens)
    if wsum <= 0:
        raise ValueError("All weights are zero; set OWCG_WEIGHTS properly.")

    targets: Dict[str, Decimal] = {}
    needs: Dict[str, Decimal] = {}
    for t in tokens:
        target_t = (pool * weights[t] / wsum).quantize(Decimal("0.01"))
        have_t = current.get(t, Decimal("0"))
        need_t = max(Decimal("0"), target_t - have_t)
        targets[t], needs[t] = target_t, need_t

    total_need = sum(needs.values())
    budget_left = budget

    print(f"[seed] Starting proportional seeding")
    print(f"[seed] Tokens={tokens} | Weights={weights} | Budget=${budget} | USD avail=${usd_avail}")
    print(f"[seed] Current: {current}")
    print(f"[seed] Targets: {targets}")
    print(f"[seed] Needs (pre-scale): {needs}")

    if total_need > budget and total_need > 0:
        scale = (budget / total_need)
        for t in tokens:
            needs[t] = (needs[t] * scale).quantize(Decimal("0.01"))
        total_need = sum(needs.values())
        print(f"[seed] Needs scaled to budget: {needs} (sum=${total_need})")

    usd_spent_total = Decimal("0")
    for t in tokens:
        need = needs[t]
        if need <= 0:
            continue
        spent = _ensure_token_with_budget(t, need, budget_left)
        budget_left -= spent
        usd_spent_total += spent
        print(f"[seed] {t}: requested ~${need}, spent ~${spent}, budget_left=${budget_left}")
        if budget_left <= 0:
            break

    final_bal = _balances_map()
    final_tokens = {t: final_bal.get(t, Decimal("0")) for t in tokens}
    print(f"[seed] Done. USD spent≈${usd_spent_total}. Final balances: USD={final_bal.get('USD',0)}, {final_tokens}")

# ============================ ENTRYPOINT ============================
if __name__ == "__main__":
    seed_reserves_proportional()
