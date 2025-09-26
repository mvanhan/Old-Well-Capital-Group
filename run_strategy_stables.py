# run_strategy_stables.py
from __future__ import annotations

import csv
import os
import time
from decimal import Decimal
from typing import Dict, Any, List, Optional

from strategies.stables_mean_reversion import StrategyConfig, scan_once

# ---------------- Config ---------------- #
SCAN_INTERVAL_SECS = int(os.getenv("OWCG_SCAN_INTERVAL", "15"))  # run every 15s by default

# ---------------- Balance source (ADAPT ME to your broker) ---------------- #
def _get_balances() -> Dict[str, Decimal]:
    """
    Returns a dict like {"USD": Decimal(...), "USDT": Decimal(...), "USDC": Decimal(...)}.
    Maps to your coinbase_private.get_balances() if available; otherwise uses a safe fallback.
    """
    try:
        from broker import coinbase_private as priv
        bals = priv.get_balances()  # expect [{"currency":"USD","available":"123.45"}, ...]
        out: Dict[str, Decimal] = {}
        for b in bals:
            # pick "available" if present; else "balance"
            amt = b.get("available", b.get("balance", "0"))
            out[b["currency"]] = Decimal(str(amt))
        return out
    except Exception:
        # Fallback dummy balances (so the loop doesn't crash in dev)
        return {"USD": Decimal("1000"), "USDT": Decimal("0"), "USDC": Decimal("0")}

# ---------------- CSV helpers ---------------- #
def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in row.items()}

def _write_csv(path: str, rows: List[Dict[str, Any]], header: List[str]):
    _ensure_dir(path)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(_normalize_row(r))

def _append_csv(path: str, row: Dict[str, Any], header: List[str]):
    _ensure_dir(path)
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if new_file:
            w.writeheader()
        w.writerow(_normalize_row(row))

# ---------------- Main loop ---------------- #
def _build_cfg() -> StrategyConfig:
    # Thin-margin defaults; adjust as you learn live performance.
    return StrategyConfig(
        products=["USDT-USDC"],         # focus on the tightest stable pair
        exit_bps=Decimal("0.6"),
        sl_bps=Decimal("3.0"),
        slippage_bps=Decimal("0.2"),
        maker_fee_bps=Decimal("0.0"),
        taker_fee_bps=Decimal("0.0"),
        cushion_bps=Decimal("0.2"),
        hold_minutes=180,
        depth_ticks=2,
        min_depth_multiplier=Decimal("1.10"),
        bankroll_pct=Decimal("0.10"),   # 10% bankroll target
        min_notional=Decimal("50"),
        max_notional=Decimal("500"),
        out_dir="output_stables",
    )

def _paths(out_dir: str):
    return {
        "screen_latest": os.path.join(out_dir, "screen_latest.csv"),
        "screen_history": os.path.join(out_dir, "screen_history.csv"),
        "ticket_latest": os.path.join(out_dir, "trade_tickets_latest.csv"),
        "ticket_history": os.path.join(out_dir, "trade_tickets_history.csv"),
    }

def main_forever():
    cfg = _build_cfg()
    paths = _paths(cfg.out_dir)

    # Headers
    screen_header = [
        "ts","product_id","side","entry_price","tp_price","sl_price","base_size",
        "dev_bps","rr","hold_minutes","reason","risk_dollars","notional","gate_bps","depth_note"
    ]
    ticket_header = [
        "ts","product_id","side","entry_price","tp_price","sl_price","base_size",
        "post_only","bracket_desired","hold_minutes","reason","risk_dollars"
    ]
    ticket_none_header = ["ts","product_id","side","reason"]

    print(f"[stables] Starting scanner loop every {SCAN_INTERVAL_SECS}s. Ctrl+C to stop.")
    while True:
        cycle_start = time.time()
        try:
            balances = _get_balances()
            ticket, diag = scan_once(cfg, balances)

            # Always write latest diagnostics
            _write_csv(paths["screen_latest"], [diag], screen_header)
            _append_csv(paths["screen_history"], diag, screen_header)

            if ticket:
                trow = {
                    "ts": int(time.time()),
                    "product_id": ticket.product_id,
                    "side": ticket.side,
                    "entry_price": ticket.entry_price,
                    "tp_price": ticket.tp_price,
                    "sl_price": ticket.sl_price,
                    "base_size": ticket.base_size,
                    "post_only": ticket.post_only,
                    "bracket_desired": ticket.bracket_desired,
                    "hold_minutes": ticket.hold_minutes,
                    "reason": ticket.reason,
                    "risk_dollars": ticket.risk_dollars,
                }
                _write_csv(paths["ticket_latest"], [trow], ticket_header)
                _append_csv(paths["ticket_history"], trow, ticket_header)
                print(f"[stables] Ticket created: {ticket.product_id} {ticket.side} size={ticket.base_size} "
                      f"entry={ticket.entry_price} tp={ticket.tp_price} sl={ticket.sl_price} "
                      f"dev={ticket.dev_bps}bps rr={ticket.rr}")
            else:
                none_row = {
                    "ts": int(time.time()),
                    "product_id": diag.get("product_id", "USDT-USDC"),
                    "side": "NONE",
                    "reason": diag.get("reason", "no_signal"),
                }
                _write_csv(paths["ticket_latest"], [none_row], ticket_none_header)
                _append_csv(paths["ticket_history"], none_row, ticket_none_header)
                print(f"[stables] No candidate: {none_row['reason']}")

        except KeyboardInterrupt:
            print("\n[stables] Stopping scanner loop (KeyboardInterrupt).")
            break
        except Exception as e:
            # Log the error and keep going next cycle
            err_row = {
                "ts": int(time.time()),
                "product_id": "NA",
                "side": "NONE",
                "reason": f"scanner_exception {type(e).__name__}: {e}",
            }
            _write_csv(paths["ticket_latest"], [err_row], ["ts","product_id","side","reason"])
            _append_csv(paths["ticket_history"], err_row, ["ts","product_id","side","reason"])
            print(f"[stables] ERROR in scan loop: {e}")

        # Sleep the remainder of the interval
        elapsed = time.time() - cycle_start
        sleep_for = max(0.0, SCAN_INTERVAL_SECS - elapsed)
        time.sleep(sleep_for)

if __name__ == "__main__":
    main_forever()
