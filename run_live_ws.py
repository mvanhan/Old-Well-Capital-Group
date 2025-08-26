from __future__ import annotations
from decimal import Decimal, getcontext
from typing import List, Dict
from time import sleep
from datetime import datetime, timezone
import yaml, os

from env_utils import get_kraken_credentials
from signal_engine import screen_and_build_candidates
from broker.kraken_private import (
    KrakenAuth, WsV2Trader, place_limit_exit, place_market_exit, place_entry_with_stop_rest
)
from kraken_public_adapter import _to_rest_pair as _pair_to_rest  # reuse converter

getcontext().prec = 28

def _expand_env_vars(d):
    if isinstance(d, dict):
        return {k: _expand_env_vars(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_expand_env_vars(x) for x in d]
    if isinstance(d, str) and d.startswith("${") and d.endswith("}"):
        return os.getenv(d[2:-1], "")
    return d

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)
    return _expand_env_vars(cfg)

CFG = load_config()

def kraken_symbol(symbol: str) -> str:
    return f"{symbol.upper()}/USD"

def get_last_price(symbol_pair: str) -> Decimal:
    from kraken_public_adapter import get_ticker_last
    px = get_ticker_last(symbol_pair)
    return Decimal(str(px))

class TpManager:
    def __init__(self):
        self.targets: Dict[int, Dict] = {}
    def register(self, userref: int, symbol: str, side: str, tp_price: Decimal, filled_base: Decimal):
        self.targets[userref] = {"symbol": symbol, "side": side, "tp_price": tp_price, "filled_base": filled_base}
    def update_fill(self, userref: int, add_base: Decimal):
        if userref in self.targets:
            self.targets[userref]["filled_base"] += add_base
    def should_tp(self, userref: int, last: Decimal) -> bool:
        t = self.targets.get(userref)
        if not t: return False
        return (last >= t["tp_price"]) if t["side"] == "buy" else (last <= t["tp_price"])

TPM = TpManager()

def place_entry_with_bracket(
    auth: KrakenAuth,
    symbol_pair: str,
    side: str,
    qty: Decimal,
    limit_px: Decimal,
    tp_px: Decimal,
    sl_trigger: Decimal,
    userref: int,
):
    print(f"[info] TP target registered for userref={userref}: {symbol_pair} TP={tp_px} side={'sell' if side=='buy' else 'buy'} qty<=filled")

    # Try WS v2 first (requires WS token). If it fails (network), fall back to REST OTO.
    try:
        trader = WsV2Trader(auth)
        trader.connect()  # robust TLS (certifi) inside; tries two sslopt modes
        try:
            resp = trader.add_order_limit_with_stop(
                symbol=symbol_pair,
                side=side,
                qty=qty,
                limit_price=limit_px,
                stop_trigger=sl_trigger,
                stop_limit=None,
                post_only=True,
                userref=userref,
                cl_ord_id=f"owcg-{userref}",  # OK on WS
            )
            print(f"[entry:ws] posted symbol={symbol_pair} side={side} qty={qty} px={limit_px} userref={userref}")
        finally:
            trader.close()
    except Exception as e:
        print(f"[warn] WS path unavailable ({e}); falling back to REST OTO.")
        rest_pair = _pair_to_rest(symbol_pair)  # e.g., "FLOKI/USD" -> "FLOKIUSD"
        resp = place_entry_with_stop_rest(
            auth=auth,
            symbol=rest_pair,
            side=side,
            qty=qty,
            limit_price=limit_px,
            stop_trigger=sl_trigger,
            userref=userref,
            post_only=True,
            # no cl_ord_id on REST when userref is set
        )
        print(f"[entry:rest] posted symbol={rest_pair} side={side} qty={qty} px={limit_px} userref={userref}")

    TPM.register(userref, symbol_pair, side, tp_px, Decimal("0"))

def maybe_fire_tp_and_cancel_stops(auth: KrakenAuth, userref: int) -> bool:
    t = TPM.targets.get(userref)
    if not t:
        return False
    last = get_last_price(t["symbol"])
    print(f"[status] userref={userref} last={last} tp_target={t['tp_price']} filled={t['filled_base']}")
    if not TPM.should_tp(userref, last):
        return False
    qty = t["filled_base"].quantize(Decimal("0.00000001"))
    if qty <= 0:
        print(f"[warn] TP condition met but no filled qty yet for userref={userref}")
        return False
    side_exit = "sell" if t["side"] == "buy" else "buy"

    if str(CFG["brackets"]["tp_exit"]).lower() == "market":
        txid = place_market_exit(auth, t["symbol"], side_exit, qty)
        print(f"[oco] TP MARKET placed: txid={txid} symbol={t['symbol']} side={side_exit} qty={qty}")
    else:
        txid = place_limit_exit(auth, t["symbol"], side_exit, qty, t["tp_price"])
        print(f"[oco] TP LIMIT placed: txid={txid} symbol={t['symbol']} side={side_exit} qty={qty} price={t['tp_price']}")

    # Cancel OTO stop children by userref (works for both WS and REST placements)
    trader = WsV2Trader(auth)
    try:
        trader.connect()
    except Exception:
        # If WS still down, REST loop inside broker will still work
        pass
    try:
        canceled = trader.cancel_oto_children_for_userref(userref)
        print(f"[oco] canceled {len(canceled)} STOP children for userref={userref}")
        TPM.targets.pop(userref, None)
    finally:
        try:
            trader.close()
        except Exception:
            pass
    return True

def main():
    api_key, api_secret_b64 = get_kraken_credentials()
    print(f"[env] KRAKEN_API_KEY len={len(api_key)}  KRAKEN_API_SECRET(_B64) len={len(api_secret_b64)}")

    universe: List[str] = CFG["screen"]["universe"]
    tickets = screen_and_build_candidates(universe, CFG)
    if not tickets:
        print("[run] no candidates")
        return

    t0 = tickets[0]
    sym = t0["symbol"]
    pair = kraken_symbol(sym)
    print(f"[run] candidate {sym} score={t0['score']:.3f} intent={t0['intent']}")

    last = get_last_price(pair)
    qty = (Decimal("50") / last).quantize(Decimal("0.00000001"))
    side = "buy"
    tp_off_bps = Decimal(str(CFG["brackets"]["tp_offset_bps"])) / Decimal(10000)
    tp_px = (last * (Decimal("1.0") + tp_off_bps)).quantize(Decimal("0.00000001"))
    sl_px = (last * (Decimal("1.0") - (tp_off_bps * Decimal("0.7")))).quantize(Decimal("0.00000001"))
    userref = int(datetime.now(timezone.utc).timestamp() * 1000) % 2_147_483_647

    if CFG["live"]["dry_run"]:
        print(f"[dry-run] would place entry {side} {qty} @ {last} on {pair}")
        print(f"[dry-run] TP target will be {tp_px} (exit side {'sell' if side=='buy' else 'buy'})")
        print(f"[dry-run] SL trigger will be {sl_px}")
        return

    auth = KrakenAuth(api_key, api_secret_b64)

    place_entry_with_bracket(
        auth=auth,
        symbol_pair=pair,
        side=side,
        qty=qty,
        limit_px=last,
        tp_px=tp_px,
        sl_trigger=sl_px,
        userref=userref,
    )

    # Simulate full fill so TP logic can trigger when price crosses
    TPM.update_fill(userref, qty)

    for _ in range(120):
        if maybe_fire_tp_and_cancel_stops(auth, userref):
            print("[done] TP placed and STOPs canceled (synthetic OCO complete).")
            break
        sleep(1.0)

if __name__ == "__main__":
    main()
