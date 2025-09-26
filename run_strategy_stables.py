# run_strategy_stables.py
from __future__ import annotations

import os
import time
import csv
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, List, Optional

# Load .env early so API keys are present (non-fatal if missing)
try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

# --- Strategy import (uses your existing algo) ---
from strategies.stables_mean_reversion import scan_once  # type: ignore

# -------- Config (env-overridable) --------
INTERVAL_SECS = int(os.getenv("OWCG_STRAT_INTERVAL", "15"))  # scan every N seconds

# Default output directory for strategy artifacts/logs expected by the strategy
DEFAULT_OUTDIR = os.getenv("OWCG_STRAT_OUTDIR", os.path.join("out", "stables"))
# Put CSV inside that folder by default (overridable)
CSV_PATH = os.getenv("OWCG_STRAT_CSV", os.path.join(DEFAULT_OUTDIR, "stables_scans.csv"))

# Verbose console heartbeat
VERBOSE = os.getenv("OWCG_STRAT_VERBOSE", "1").lower() in ("1", "true", "yes", "y")

# Some commonly-used knobs the strategy may read
USE_TAKER = os.getenv("OWCG_USE_TAKER", "true").lower() in ("1", "true", "yes", "y")
MIN_ACTION_USD = Decimal(os.getenv("OWCG_MIN_ACTION_USD", "1.00"))

# -------- Config wrapper that supports attribute access --------
class Cfg(dict):
    """Dict with attribute access; missing attrs return None."""
    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            return None
    def __setattr__(self, name: str, value: Any):
        self[name] = value
    def get(self, name: str, default=None):  # for safety if strategy calls cfg.get(...)
        return super().get(name, default)

def _ensure_outdir(path: str) -> str:
    if path and not os.path.exists(path):
        os.makedirs(path, exist_ok=True)
    return path

def _build_cfg() -> Cfg:
    """
    Build a config object with the attributes the strategy commonly expects.
    If the strategy references more attributes later, add them here (or they’ll
    resolve to None, which most code treats as “use defaults”).
    """
    out_dir = _ensure_outdir(DEFAULT_OUTDIR)
    cfg = Cfg(
        out_dir=out_dir,            # <- strategy expects this to exist
        use_taker=USE_TAKER,        # common flag algos read
        min_action_usd=MIN_ACTION_USD,
        interval_secs=INTERVAL_SECS # handy for logs
    )
    return cfg

# --- Public/Private adapters (use your existing broker wrappers) ---
class Priv:
    @staticmethod
    def get_balances() -> List[Dict[str, Any]]:
        from broker import coinbase_private as priv
        return priv.get_balances()

# -------- Helpers --------
def _balances_map() -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for b in Priv.get_balances():
        cur = str(b.get("currency", "")).upper()
        if not cur:
            continue
        amt = b.get("available", b.get("balance", "0"))
        out[cur] = Decimal(str(amt))
    return out

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _clean_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert non-serializable types (Decimal, lists, dicts) to strings for CSV."""
    cleaned: Dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, (Decimal, list, dict, tuple)):
            cleaned[k] = str(v)
        else:
            cleaned[k] = v
    return cleaned

def _safe_writerow(csv_path: str, row: Dict[str, Any]) -> None:
    """
    Write a dict row to CSV without crashing if new keys appear.
    Uses extrasaction='ignore' so unexpected keys are silently skipped.
    """
    # Ensure directory exists if user set a nested path
    d = os.path.dirname(csv_path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

    file_exists = os.path.exists(csv_path)
    baseline_fields = [
        "ts", "signal", "product_id", "side", "price", "size",
        "score", "zscore", "spread", "vol", "note", "status",
    ]
    mode = "a" if file_exists else "w"
    with open(csv_path, mode, newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=baseline_fields, extrasaction="ignore")
        if not file_exists:
            w.writeheader()
        w.writerow(_clean_row(row))

def _print_heartbeat(diag: Optional[Dict[str, Any]], err: Optional[str] = None):
    if not VERBOSE:
        return
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if err:
        print(f"[stables] {now} ERROR: {err}")
        return
    if not diag:
        print(f"[stables] {now} tick (no diag)")
        return
    sig = diag.get("signal") or diag.get("note") or "ok"
    pid = diag.get("product_id") or ""
    st  = diag.get("status") or ""
    print(f"[stables] {now} tick | {sig} {pid} {st}".strip())

# -------- Main loop --------
def main():
    cfg = _build_cfg()
    print(f"[stables] Writing artifacts to: {cfg.out_dir}")
    print(f"[stables] CSV log: {CSV_PATH}")
    print(f"[stables] Starting scanner loop every {INTERVAL_SECS}s. Ctrl+C to stop.")
    while True:
        try:
            balances = _balances_map()
            # Execute one scan step with a config that has attribute access
            ticket, diag = scan_once(cfg, balances)

            # Ensure we always have something loggable
            if not isinstance(diag, dict):
                diag = {"note": str(diag)}

            # Add timestamp and persist
            row = {"ts": _ts(), **diag}
            _safe_writerow(CSV_PATH, row)

            _print_heartbeat(diag)

        except KeyboardInterrupt:
            print("[stables] Stopping.")
            break
        except Exception as e:
            msg = getattr(e, "args", [str(e)])[0] if e else "unknown error"
            _print_heartbeat(None, err=msg)
            try:
                _safe_writerow(CSV_PATH, {"ts": _ts(), "signal": "error", "note": msg})
            except Exception:
                pass
        finally:
            time.sleep(INTERVAL_SECS)

if __name__ == "__main__":
    main()
