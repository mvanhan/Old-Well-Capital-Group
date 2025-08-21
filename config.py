# config.py — single source of truth loader for Old Well project
import os, yaml
from typing import Any, Dict

# --- Resolve project root and load .env explicitly (works in heredoc & scripts) ---
ROOT = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(ROOT, ".env"), override=True)
except Exception:
    # If python-dotenv isn't installed, we just rely on OS env vars.
    pass

# ----- defaults (used if config.yaml missing fields) -----
_DEFAULTS: Dict[str, Any] = {
    "UNIVERSE": [
        ("dogwifcoin", "WIF"),
        ("bonk", "BONK"),
        ("pepe", "PEPE"),
        ("floki", "FLOKI"),
        ("baby-doge-coin", "BABYDOGE"),
        ("book-of-meme", "BOME"),
        ("ordinals", "ORDI"),
        ("sui", "SUI"),
        ("sei-network", "SEI"),
        ("aptos", "APT"),
    ],
    "screen": {"take": 2, "min_vol_usd": 20_000_000},
    "risk": {
        "nav_usd": 1500,
        "per_trade_pct": 0.50,          # percent of NAV
        "single_trade_cap_usd": 75,
        "min_stop_pct": 0.0125,         # stop floor (1.25%)
        "slippage_bps": 8,
        "min_realized_risk_frac": 0.40,
        "max_portfolio_risk_pct": 1.0,
    },
    "exchange": {"fees": {"maker_bps": 25, "taker_bps": 40}},
    "strategy": {
        "atr_5m_window": 14,
        "stop_atr_mult_5m": 8,
        "tp_atr_mult_5m": 20,
        "maker_spread_fraction": 0.5,
    },
    "live": {"dry_run": True},
}

def _load_yaml(path: str = os.path.join(ROOT, "config.yaml")) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

_cfg = _load_yaml()

def _get(*path, default=None):
    cur = _cfg
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

# ---- API keys ----
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY") or _get("exchange","coingecko_api_key", default="")

# ---- Base URL for CoinGecko (Demo by default; Pro if you override in config.yaml) ----
COINGECKO_BASE_URL = _get("exchange","coingecko_base_url", default="https://api.coingecko.com/api/v3")

# ---- Universe / screening ----
UNIVERSE = _DEFAULTS["UNIVERSE"]
TAKE = int(_get("screen","take", default=_DEFAULTS["screen"]["take"]))
MIN_VOL_USD = float(_get("screen","min_vol_usd", default=_DEFAULTS["screen"]["min_vol_usd"]))

# ---- Risk & fees ----
NAV_USD = float(_get("risk","nav_usd", default=_DEFAULTS["risk"]["nav_usd"]))
RISK_PER_TRADE_PCT = float(_get("risk","per_trade_pct", default=_DEFAULTS["risk"]["per_trade_pct"]))
SINGLE_TRADE_CAP_USD = float(_get("risk","single_trade_cap_usd", default=_DEFAULTS["risk"]["single_trade_cap_usd"]))
MIN_STOP_PCT = float(_get("risk","min_stop_pct", default=_DEFAULTS["risk"]["min_stop_pct"]))
SLIPPAGE_OUT_BPS = float(_get("risk","slippage_bps", default=_DEFAULTS["risk"]["slippage_bps"]))
MIN_REALIZED_RISK_FRAC = float(_get("risk","min_realized_risk_frac", default=_DEFAULTS["risk"]["min_realized_risk_frac"]))
MAX_PORTFOLIO_RISK_PCT = float(_get("risk","max_portfolio_risk_pct", default=_DEFAULTS["risk"]["max_portfolio_risk_pct"]))

MAKER_BPS = float(_get("exchange","fees", default={}).get("maker_bps", _DEFAULTS["exchange"]["fees"]["maker_bps"]))
TAKER_BPS = float(_get("exchange","fees", default={}).get("taker_bps", _DEFAULTS["exchange"]["fees"]["taker_bps"]))

# ---- Strategy ----
ATR_LOOKBACK_BARS = int(_get("strategy","atr_5m_window", default=_DEFAULTS["strategy"]["atr_5m_window"]))
STOP_ATR_MULT = float(_get("strategy","stop_atr_mult_5m", default=_DEFAULTS["strategy"]["stop_atr_mult_5m"]))
TP_ATR_MULT = float(_get("strategy","tp_atr_mult_5m", default=_DEFAULTS["strategy"]["tp_atr_mult_5m"]))
MAKER_SPREAD_FRACTION = float(_get("strategy","maker_spread_fraction", default=_DEFAULTS["strategy"]["maker_spread_fraction"]))

# ---- Live / dry-run ----
DRY_RUN = bool(_get("live","dry_run", default=_DEFAULTS["live"]["dry_run"]) or os.getenv("DRY_RUN") in ("1","true","True"))
