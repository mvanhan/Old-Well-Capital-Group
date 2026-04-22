from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import find_dotenv, load_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

from broker import coinbase_public as cb_pub  # type: ignore
from strategies.stables_mean_reversion import (
    StrategyConfig,
    _compute_size_for_bankroll,
    _gate_bps,
    _get_best_bid_ask,
    _get_product_spec,
    _gross_dev_bps,
    _maker_entry_price,
    _quote_reference_price,
    _spread_bps,
    scan_once,
)

OUTDIR = "output_stables"
CSV_TICKET_LATEST = os.path.join(OUTDIR, "trade_tickets_latest.csv")
SUBMITTER_STATE = os.path.join(OUTDIR, "submitter_state.json")

INTERVAL_SECS = int(os.getenv("STABLES_SCAN_INTERVAL", "15"))
PRODUCT_REFRESH_SECS = int(os.getenv("STABLES_PRODUCT_REFRESH_SECS", "60"))

TICKET_HEADER = [
    "ticket_id",
    "ts",
    "expire_ts",
    "product_id",
    "side",
    "entry_price",
    "size",
    "tp_price",
    "sl_price",
    "post_only",
    "bracket_desired",
    "client_tag",
    "reason",
]


def _env_bool(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "y", "on"}


def _balances() -> Dict[str, Decimal]:
    if os.getenv("OWCG_OFFLINE") == "1":
        return {"USDC": Decimal("1000"), "USD": Decimal("1000"), "USDT": Decimal("1000"), "DAI": Decimal("1000")}

    from broker import coinbase_private as cb_priv  # type: ignore

    out: Dict[str, Decimal] = {}
    entries = cb_priv.get_balances()
    for entry in entries:
        symbol = entry.get("currency") or entry.get("asset") or entry.get("symbol")
        value = entry.get("available") or entry.get("available_balance") or entry.get("available_for_trading")
        if isinstance(value, dict):
            value = value.get("value")
        if symbol and value is not None:
            try:
                out[str(symbol).upper()] = Decimal(str(value))
            except Exception:
                pass

    if not out:
        raise RuntimeError("No balances returned from Coinbase. Check API key/secret, permissions, and account access.")
    return out


def _ts() -> int:
    return int(time.time())


def _ensure_outdir() -> None:
    os.makedirs(OUTDIR, exist_ok=True)


def _write_latest(path: str, row: Dict[str, Any], header: List[str]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in header})


def _ticket_id(row: Dict[str, str]) -> str:
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _load_submitter_state() -> Dict[str, Any]:
    if not os.path.exists(SUBMITTER_STATE):
        return {"stage": "IDLE"}
    try:
        with open(SUBMITTER_STATE) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {"stage": "IDLE"}
    except Exception:
        return {"stage": "IDLE"}


def _submitter_busy() -> bool:
    return str(_load_submitter_state().get("stage") or "IDLE").upper() != "IDLE"


def _resolve_products() -> List[str]:
    products = [str(product).upper() for product in cb_pub.resolve_trading_products()]
    deduped: List[str] = []
    seen = set()
    for product in products:
        if product and product not in seen:
            seen.add(product)
            deduped.append(product)
    return deduped


def _build_cfg(products: List[str]) -> StrategyConfig:
    return StrategyConfig(
        products=products,
        out_dir=OUTDIR,
        maker_fee_bps=Decimal(os.getenv("MAKER_FEE_BPS", "0.0")),
        taker_fee_bps=Decimal(os.getenv("TAKER_FEE_BPS", "0.0")),
        exit_bps=Decimal(os.getenv("EXIT_BPS", "4.0")),
        sl_bps=Decimal(os.getenv("SL_BPS", "6.0")),
        slippage_bps=Decimal(os.getenv("SLIPPAGE_BPS", "1.0")),
        cushion_bps=Decimal(os.getenv("CUSHION_BPS", "0.3")),
        hold_minutes=int(os.getenv("HOLD_MINUTES", "180")),
        depth_ticks=int(os.getenv("DEPTH_TICKS", "2")),
        min_depth_multiplier=Decimal(os.getenv("MIN_DEPTH_MULTIPLIER", "1.10")),
        block_on_missing_l2=_env_bool("BLOCK_ON_MISSING_L2", "1"),
        bankroll_usd=Decimal(os.getenv("BANKROLL_USD", "100")),
        bankroll_pct=Decimal(os.getenv("BANKROLL_PCT", "0.10")),
        min_notional=Decimal(os.getenv("MIN_NOTIONAL", "5")),
        max_notional=Decimal(os.getenv("MAX_NOTIONAL", "500")),
        max_risk_usd=Decimal(os.getenv("MAX_RISK_USD", "3")),
        min_tp_ticks=int(os.getenv("MIN_TP_TICKS", "1")),
        min_sl_ticks=int(os.getenv("MIN_SL_TICKS", "1")),
        max_spread_bps=Decimal(os.getenv("MAX_SPREAD_BPS", "3.0")),
        max_dev_bps=Decimal(os.getenv("MAX_DEV_BPS", "25.0")),
        ticket_ttl_secs=int(os.getenv("MAX_TICKET_AGE_SECS", "30")),
    )


def _refresh_products_if_due(
    current_products: List[str],
    last_refresh_ts: int,
) -> Tuple[List[str], int, Optional[str]]:
    now = _ts()
    if current_products and (now - last_refresh_ts) < PRODUCT_REFRESH_SECS:
        return current_products, last_refresh_ts, None

    refreshed = _resolve_products()
    if not refreshed:
        if current_products:
            return current_products, last_refresh_ts, "refresh_empty_using_last_good"
        raise RuntimeError("No eligible trading products resolved. Check STABLES_PRODUCTS / STABLES_AUTO_DISCOVER.")

    if refreshed != current_products:
        return refreshed, now, f"universe_updated:{','.join(refreshed)}"
    return refreshed, now, None


def _fmt_decimal(value: Any, places: int = 6) -> str:
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:
        return str(value)

    quant = Decimal("1").scaleb(-places)
    text = format(dec.quantize(quant), "f")
    text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def _side_diag(
    product_id: str,
    side: str,
    cfg: StrategyConfig,
    balances: Dict[str, Decimal],
) -> Dict[str, str]:
    spec = _get_product_spec(product_id)
    reference = _quote_reference_price(spec)

    if reference is None:
        return {
            "side": side,
            "reason": f"unsupported_quote_reference base={spec.base} quote={spec.quote}",
            "entry": "",
            "dev_bps": "",
            "gate_bps": "",
            "edge_bps": "",
            "size": "",
            "notional": "",
            "spread_bps": "",
        }

    bid, ask = _get_best_bid_ask(product_id)
    if bid <= 0 or ask <= 0 or bid >= ask:
        return {
            "side": side,
            "reason": f"bad_quote bid={bid} ask={ask}",
            "entry": "",
            "dev_bps": "",
            "gate_bps": "",
            "edge_bps": "",
            "size": "",
            "notional": "",
            "spread_bps": "",
        }

    spread = _spread_bps(bid, ask)
    entry = _maker_entry_price(side, bid, ask, spec)
    dev_bps = _gross_dev_bps(reference, entry, side)
    gate = _gate_bps(cfg, bid, ask)
    edge = dev_bps - gate

    size_str = ""
    notional_str = ""
    reason = "pass"

    if spread > cfg.max_spread_bps:
        reason = f"spread_too_wide {_fmt_decimal(spread)}"
    elif dev_bps <= 0:
        reason = "no_deviation"
    elif dev_bps > cfg.max_dev_bps:
        reason = f"dev_too_large {_fmt_decimal(dev_bps)}"
    else:
        try:
            size, _, balance_note = _compute_size_for_bankroll(spec, side, entry, cfg, balances)
            notional = entry * size
            size_str = _fmt_decimal(size, 8)
            notional_str = _fmt_decimal(notional, 2)
            if balance_note:
                reason = balance_note
        except Exception as exc:
            reason = f"size_error {exc}"

    return {
        "side": side,
        "reason": reason,
        "entry": _fmt_decimal(entry, 8),
        "dev_bps": _fmt_decimal(dev_bps, 6),
        "gate_bps": _fmt_decimal(gate, 6),
        "edge_bps": _fmt_decimal(edge, 6),
        "size": size_str,
        "notional": notional_str,
        "spread_bps": _fmt_decimal(spread, 6),
    }


def _print_universe(products: List[str]) -> None:
    print(f"[stables] trading universe ({len(products)}): {', '.join(products)}")


def _print_universe_snapshot(products: List[str], balances: Dict[str, Decimal]) -> None:
    if not products:
        print("[stables] universe snapshot: no products")
        return

    cfg = _build_cfg(products)
    print(f"[stables] universe snapshot ({len(products)} products)")

    ranked_rows: List[Tuple[Decimal, str]] = []

    for product_id in products:
        try:
            spec = _get_product_spec(product_id)
            buy = _side_diag(product_id, "BUY", cfg, balances)
            sell = _side_diag(product_id, "SELL", cfg, balances)

            buy_edge = Decimal(buy["edge_bps"]) if buy["edge_bps"] else Decimal("-1000000")
            sell_edge = Decimal(sell["edge_bps"]) if sell["edge_bps"] else Decimal("-1000000")

            if buy_edge >= sell_edge:
                best_side = "BUY"
                best = buy
                best_edge = buy_edge
            else:
                best_side = "SELL"
                best = sell
                best_edge = sell_edge

            line = (
                f"[stables] universe-detail {product_id} "
                f"base={spec.base} quote={spec.quote} "
                f"best_side={best_side} "
                f"best_edge_bps={best.get('edge_bps', '')} "
                f"best_reason={best.get('reason', '')} "
                f"spread_bps={best.get('spread_bps', '')} "
                f"buy_edge_bps={buy.get('edge_bps', '')} "
                f"buy_reason={buy.get('reason', '')} "
                f"sell_edge_bps={sell.get('edge_bps', '')} "
                f"sell_reason={sell.get('reason', '')} "
                f"best_entry={best.get('entry', '')} "
                f"best_size={best.get('size', '')} "
                f"best_notional={best.get('notional', '')}"
            )
            ranked_rows.append((best_edge, line))
        except Exception as exc:
            ranked_rows.append((Decimal("-1000000"), f"[stables] universe-detail {product_id} error={exc}"))

    ranked_rows.sort(key=lambda item: item[0], reverse=True)
    for _, line in ranked_rows:
        print(line)


def main() -> None:
    _ensure_outdir()

    products: List[str] = []
    last_refresh_ts = 0
    printed_initial_snapshot = False

    try:
        products, last_refresh_ts, _ = _refresh_products_if_due([], 0)
        _print_universe(products)
    except Exception as exc:
        print(f"[stables] startup product resolution failed: {exc}")

    while True:
        try:
            products, last_refresh_ts, refresh_note = _refresh_products_if_due(products, last_refresh_ts)
            snapshot_requested = False

            if refresh_note:
                if refresh_note.startswith("universe_updated:"):
                    print(f"[stables] {refresh_note}")
                    _print_universe(products)
                    snapshot_requested = True
                else:
                    print(f"[stables] {refresh_note}")

            if not products:
                print("[stables] no products resolved")
            else:
                balances = _balances()

                if not printed_initial_snapshot or snapshot_requested:
                    _print_universe_snapshot(products, balances)
                    printed_initial_snapshot = True

                cfg = _build_cfg(products)
                ticket, diag = scan_once(cfg, balances=balances)

                if ticket and not _submitter_busy():
                    row = {**ticket.to_row(), "reason": "pass"}
                    row["ticket_id"] = _ticket_id(row)
                    _write_latest(CSV_TICKET_LATEST, row, TICKET_HEADER)
                    print(
                        f"[stables] signal {ticket.side} {ticket.product_id} "
                        f"sz={ticket.size} @ {ticket.entry_price} tp={ticket.tp_price} sl={ticket.sl_price} exp={ticket.expire_ts}"
                    )
                elif ticket:
                    print(f"[stables] signal skipped while submitter busy: {ticket.side} {ticket.product_id}")
                else:
                    print(f"[stables] no-signal: {diag.get('reason')}")

        except KeyboardInterrupt:
            print("[stables] stopping")
            break
        except Exception as exc:
            print(f"[stables] error: {exc}")
        finally:
            time.sleep(INTERVAL_SECS)


if __name__ == "__main__":
    main()