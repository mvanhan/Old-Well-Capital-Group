from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

try:
    from dotenv import find_dotenv, load_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

from broker import coinbase_public as cb_pub  # type: ignore
from strategies.stables_mean_reversion import StrategyConfig, scan_once

OUTDIR = "output_stables"
CSV_SCANS_LATEST = os.path.join(OUTDIR, "screen_latest.csv")
CSV_SCANS_HISTORY = os.path.join(OUTDIR, "screen_history.csv")
CSV_TICKET_LATEST = os.path.join(OUTDIR, "trade_tickets_latest.csv")
CSV_TICKET_HISTORY = os.path.join(OUTDIR, "trade_tickets_history.csv")
INTERVAL_SECS = int(os.getenv("STABLES_SCAN_INTERVAL", "15"))


def _balances() -> Dict[str, Decimal]:
    if os.getenv("OWCG_OFFLINE") == "1":
        return {"USDC": Decimal("1000"), "USD": Decimal("1000")}
    try:
        from broker import coinbase_private as cb_priv  # type: ignore
        out: Dict[str, Decimal] = {}
        for entry in cb_priv.get_balances():
            symbol = entry.get("currency") or entry.get("asset") or entry.get("symbol")
            value = entry.get("available") or entry.get("available_balance") or entry.get("available_for_trading")
            if isinstance(value, dict):
                value = value.get("value")
            if symbol and value is not None:
                try:
                    out[str(symbol)] = Decimal(str(value))
                except Exception:
                    pass
        return out
    except Exception:
        return {}


def _ts() -> int:
    return int(time.time())


def _ts_human(ts: Optional[int] = None) -> str:
    stamp = ts or _ts()
    return datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _ensure_outdir() -> None:
    os.makedirs(OUTDIR, exist_ok=True)


def _write_latest(path: str, row: Dict[str, Any], header: List[str]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerow(row)


def _append_history(path: str, row: Dict[str, Any], header: List[str]) -> None:
    exists = os.path.exists(path)
    with open(path, "a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def _scan_header(diag: Dict[str, Any]) -> List[str]:
    ordered = ["ts", "ts_human", "product_id", "side", "reason"]
    for key in ("dev_bps", "edge_minus_gate_bps", "notional", "gate_bps", "depth_note", "risk_dollars"):
        if key in diag and key not in ordered:
            ordered.append(key)
    return ordered


def _ticket_header() -> List[str]:
    return [
        "ticket_id",
        "ts",
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


def _ticket_id(row: Dict[str, str]) -> str:
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _products() -> List[str]:
    products = cb_pub.resolve_trading_products()
    if not products:
        raise RuntimeError("No eligible trading products resolved. Check STABLES_PRODUCTS / STABLES_AUTO_DISCOVER.")
    return products


def main() -> None:
    _ensure_outdir()
    products = _products()
    cfg = StrategyConfig(
        products=products,
        out_dir=OUTDIR,
        maker_fee_bps=Decimal(os.getenv("MAKER_FEE_BPS", "0.0")),
        taker_fee_bps=Decimal(os.getenv("TAKER_FEE_BPS", "0.0")),
        exit_bps=Decimal(os.getenv("EXIT_BPS", "4.0")),
        sl_bps=Decimal(os.getenv("SL_BPS", "6.0")),
        slippage_bps=Decimal(os.getenv("SLIPPAGE_BPS", "1.0")),
        cushion_bps=Decimal(os.getenv("CUSHION_BPS", "0.2")),
        block_on_missing_l2=os.getenv("BLOCK_ON_MISSING_L2", "1").lower() in {"1", "true", "yes"},
        bankroll_usd=Decimal(os.getenv("BANKROLL_USD", "100")),
        bankroll_pct=Decimal(os.getenv("BANKROLL_PCT", "0.10")),
        min_notional=Decimal(os.getenv("MIN_NOTIONAL", "5")),
        max_notional=Decimal(os.getenv("MAX_NOTIONAL", "500")),
        max_risk_usd=Decimal(os.getenv("MAX_RISK_USD", "3")),
        min_tp_ticks=int(os.getenv("MIN_TP_TICKS", "1")),
        min_sl_ticks=int(os.getenv("MIN_SL_TICKS", "1")),
    )

    print(f"[stables] starting scanner loop for {', '.join(cfg.products)}; Ctrl+C to stop.")
    while True:
        try:
            balances = _balances()
            ticket, diag = scan_once(cfg, balances=balances)

            diag_row = {"ts": _ts(), "ts_human": _ts_human(), **diag}
            scan_header = _scan_header(diag_row)
            _write_latest(CSV_SCANS_LATEST, diag_row, scan_header)
            _append_history(CSV_SCANS_HISTORY, diag_row, scan_header)

            if ticket:
                row = {**ticket.to_row(), "reason": "pass"}
                row["ticket_id"] = _ticket_id(row)
                header = _ticket_header()
                _write_latest(CSV_TICKET_LATEST, row, header)
                _append_history(CSV_TICKET_HISTORY, row, header)
                print(
                    f"[stables] signal {ticket.side} {ticket.product_id} "
                    f"sz={ticket.size} @ {ticket.entry_price} tp={ticket.tp_price} sl={ticket.sl_price}"
                )
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