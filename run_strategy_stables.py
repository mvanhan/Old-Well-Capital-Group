# run_strategy_stables.py
from __future__ import annotations

import os, time, csv
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, List, Optional

# .env (non-fatal)
try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

from strategies.stables_mean_reversion import StrategyConfig, scan_once

OUTDIR = "output_stables"
CSV_SCANS   = os.path.join(OUTDIR, "screen_latest.csv")
CSV_HISTORY = os.path.join(OUTDIR, "screen_history.csv")
CSV_TICKET  = os.path.join(OUTDIR, "trade_tickets_latest.csv")
CSV_TIXHIST = os.path.join(OUTDIR, "trade_tickets_history.csv")

INTERVAL_SECS = int(os.getenv("STABLES_SCAN_INTERVAL", "15"))

# ---- Broker balances (Coinbase Advanced) ----
def _balances() -> Dict[str, Decimal]:
    if os.getenv("OWCG_OFFLINE") == "1":
        return {"USDT": Decimal("1000"), "USDC": Decimal("1000"), "USD": Decimal("1000")}
    try:
        from broker import coinbase_private as cb_priv  # type: ignore
        bals = cb_priv.get_balances()
        out: Dict[str, Decimal] = {}
        for b in bals:
            ccy = b.get("currency") or b.get("asset") or b.get("symbol")
            avail = b.get("available") or b.get("available_balance") or b.get("available_for_trading")
            if isinstance(avail, dict):  # SDK returns {"value":"...","currency":"USD"}
                avail = avail.get("value")
            if not ccy or avail is None: continue
            try:
                out[str(ccy)] = Decimal(str(avail))
            except Exception:
                pass
        return out
    except Exception:
        return {}

def _ts() -> int: return int(time.time())

def _ts_human(ts: Optional[int] = None) -> str:
    ts = ts or _ts()
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

def _ensure_outdir(path: str) -> None:
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def _writerow(path: str, row: Dict[str, Any], header: List[str]) -> None:
    exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if not exists: w.writeheader()
        w.writerow(row)

def _scan_header(diag: Dict[str, Any]) -> List[str]:
    base = ["ts","ts_human","product_id","side","reason"]
    for k in ("dev_bps","edge_minus_gate_bps","notional","gate_bps","depth_note","risk_dollars"):
        if k in diag and k not in base: base.append(k)
    return base

def _ticket_header() -> List[str]:
    return ["ts","product_id","side","entry_price","size","tp_price","sl_price","post_only","bracket_desired","client_tag","reason"]

def main():
    _ensure_outdir(OUTDIR)

    # DEFAULT CONFIG — Coinbase Advanced stables only
    cfg = StrategyConfig(
        products=[
            "USDT-USD",
            "USDC-USD",
            "USDT-USDC",
        ],
        out_dir=OUTDIR,
        maker_fee_bps=Decimal(os.getenv("MAKER_FEE_BPS","0.0")),
        taker_fee_bps=Decimal(os.getenv("TAKER_FEE_BPS","0.0")),
        exit_bps=Decimal(os.getenv("EXIT_BPS","4.0")),
        sl_bps=Decimal(os.getenv("SL_BPS","6.0")),
        slippage_bps=Decimal(os.getenv("SLIPPAGE_BPS","1.0")),
        cushion_bps=Decimal(os.getenv("CUSHION_BPS","0.2")),
        block_on_missing_l2=(os.getenv("BLOCK_ON_MISSING_L2","1") in ("1","true","True")),
        bankroll_usd=Decimal(os.getenv("BANKROLL_USD","100")),
        bankroll_pct=Decimal(os.getenv("BANKROLL_PCT","0.10")),
        min_notional=Decimal(os.getenv("MIN_NOTIONAL","5")),
        max_notional=Decimal(os.getenv("MAX_NOTIONAL","500")),
        max_risk_usd=Decimal(os.getenv("MAX_RISK_USD","3")),
        min_tp_ticks=int(os.getenv("MIN_TP_TICKS","1")),
        min_sl_ticks=int(os.getenv("MIN_SL_TICKS","1")),
    )

    print("[stables] Staring scanner loop; Ctrl+C to stop.")
    while True:
        try:
            bals = _balances()
            tkt, diag = scan_once(cfg, balances=bals)

            # write diag rows
            diag_row = {"ts": _ts(), "ts_human": _ts_human(), **diag}
            _writerow(CSV_SCANS, diag_row, _scan_header(diag_row))
            _writerow(CSV_HISTORY, diag_row, _scan_header(diag_row))

            if tkt:
                row = {**tkt.to_row(), "reason":"pass"}
                _writerow(CSV_TICKET,  row, _ticket_header())
                _writerow(CSV_TIXHIST, row, _ticket_header())
                print(f"[stables] signal {tkt.side} {tkt.product_id} sz={tkt.size} @ {tkt.entry_price} tp={tkt.tp_price} sl={tkt.sl_price}")
            else:
                print(f"[stables] no-signal: {diag.get('reason')}")

        except KeyboardInterrupt:
            print("[stables] Stopping.")
            break
        except Exception as e:
            msg = getattr(e, "args", [str(e)])[0] if e else "unknown error"
            print(f"[stables] ERROR: {msg}")
            # keep running
        finally:
            time.sleep(INTERVAL_SECS)

if __name__ == "__main__":
    main()
