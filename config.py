# config.py — single source of truth for OWCG (supports new YAML schema)
import os
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).resolve().parent

# Load .env if present (no-op if missing)
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
except Exception:
    pass


def _load_yaml(path: Path = ROOT / "config.yaml") -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_cfg = _load_yaml()


def _get(*keys, default=None):
    cur: Any = _cfg
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _flag(val, default=False) -> bool:
    if val is None:
        return bool(default)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "y")


# -------- API / EXCHANGE --------
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY") or _get("api", "coingecko_key", default="")
COINGECKO_USE_PRO = _flag(os.getenv("COINGECKO_USE_PRO", None) if os.getenv("COINGECKO_USE_PRO", None) is not None else _get("api", "use_pro", default=False))
COINGECKO_BASE_URL = _get("exchange", "coingecko_base_url", default=None)

# Kraken (read by env_utils; expose here for visibility)
KRAKEN_KEY = os.getenv("KRAKEN_API_KEY") or _get("api", "kraken_key", default="")
KRAKEN_SECRET_B64 = os.getenv("KRAKEN_API_SECRET_B64") or _get("api", "kraken_secret_b64", default="")

# -------- FEES --------
MAKER_BPS = float(_get("fees_bps", "maker", default=_get("alpha", "fees_bps", "maker", default=25.0)))
TAKER_BPS = float(_get("fees_bps", "taker", default=_get("alpha", "fees_bps", "taker", default=40.0)))

# -------- LIVE / SAFETY --------
DRY_RUN = (
    str(_get("live", "dry_run", default=True)).lower() in ("true", "1", "yes")
    or os.getenv("DRY_RUN") in ("1", "true", "True")
)
TIMEZONE = _get("live", "timezone", default="UTC")

# -------- SCREEN / UNIVERSE --------
SCREEN_UNIVERSE_SYMBOLS: List[str] = list(_get("screen", "universe", default=[])) or []
MIN_VOL_USD_24H = float(_get("screen", "min_vol_usd_24h", default=_get("screen", "min_vol_usd", default=0.0)))
UNIVERSE_TUPLES = _get("universe", default=[])

# -------- ALPHA / TAKE --------
TAKE = int(_get("alpha", "take", default=_get("screen", "take", default=1)))
ALPHA_MIN_SCORE = float(_get("alpha", "min_score", default=0.0))
ALPHA_NORMALIZE = _get("alpha", "normalize", default="none")
ALPHA_WEIGHTS = _get("alpha", "weights", default={}) or {}

# -------- RISK --------
NAV_USD = float(_get("risk", "nav_usd", default=1500))
RISK_PER_TRADE_USD = _get("risk", "risk_per_trade_usd", default=None)
RISK_PER_TRADE_USD = float(RISK_PER_TRADE_USD) if RISK_PER_TRADE_USD is not None else None
RISK_PER_TRADE_PCT = float(_get("risk", "per_trade_pct", default=0.50))
SINGLE_TRADE_CAP_USD = float(_get("risk", "single_trade_cap_usd", default=_get("risk", "single_trade_cap", default=75)))
MIN_REALIZED_RISK_FRAC = float(_get("risk", "min_realized_risk_fraction", default=_get("risk", "min_realized_risk_frac", default=0.40)))
MAX_PORTFOLIO_RISK_PCT = float(_get("risk", "max_portfolio_risk_pct", default=1.0))
MIN_STOP_PCT = float(_get("risk", "min_stop_pct", default=0.0125))

# -------- STRATEGY WINDOWS --------
ATR_LOOKBACK_BARS = int(_get("strategy", "atr_5m_window", default=14))
STOP_ATR_MULT = float(_get("strategy", "stop_atr_mult_5m", default=8))
TP_ATR_MULT = float(_get("strategy", "tp_atr_mult_5m", default=20))
MAKER_SPREAD_FRACTION = float(_get("strategy", "maker_spread_fraction", default=0.5))

# -------- BRACKETS / EXECUTION --------
USE_WS_V2 = _flag(_get("brackets", "use_ws_v2", default=True))
TP_EXIT_MODE = str(_get("brackets", "tp_exit", default="limit")).lower()  # "limit" | "market"
TP_OFFSET_BPS = float(_get("brackets", "tp_offset_bps", default=12.0))
CANCEL_TIMEOUT_MS = int(_get("brackets", "cancel_timeout_ms", default=500))
STOP_LIMIT_OFFSET_BPS = float(_get("strategy", "stop_limit_offset_bps", default=15.0))
TP_GRACE_MS = int(_get("brackets", "tp_grace_ms", default=0))  # 0 = disabled

OFLAGS_ENTRY = _get("execution", "oflags_entry", default="post") or ""
PRICE_ROUND_DP = _get("execution", "price_round_dp", default=None)
QTY_ROUND_DP = _get("execution", "qty_round_dp", default=None)
PRICE_ROUND_DP = int(PRICE_ROUND_DP) if PRICE_ROUND_DP is not None else None
QTY_ROUND_DP = int(QTY_ROUND_DP) if QTY_ROUND_DP is not None else None
