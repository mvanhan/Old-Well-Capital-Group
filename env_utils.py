"""env_utils.py — consistent creds loader for Kraken & CoinGecko."""
from __future__ import annotations
import base64
import os
from pathlib import Path
from typing import Dict, Tuple, Optional

ROOT = Path(__file__).resolve().parent

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
except Exception:
    pass


def _b64(s: str) -> str:
    try:
        base64.b64decode(s, validate=True)
        return s  # already base64
    except Exception:
        return base64.b64encode(s.encode()).decode()


def get_kraken_credentials() -> Tuple[str, str]:
    """
    Returns (api_key, secret_b64)
    Accepts either:
      - KRAKEN_API_SECRET_B64  (preferred)
      - KRAKEN_API_SECRET      (raw or base64; will be base64-encoded if needed)
    """
    key = os.getenv("KRAKEN_API_KEY", "") or os.getenv("api_kraken_key", "")
    if not key:
        raise RuntimeError("Missing KRAKEN_API_KEY in environment/.env")

    sec_b64 = os.getenv("KRAKEN_API_SECRET_B64", "")
    if not sec_b64:
        raw = os.getenv("KRAKEN_API_SECRET", "")
        if not raw:
            raise RuntimeError("Missing KRAKEN_API_SECRET or KRAKEN_API_SECRET_B64 in environment/.env")
        sec_b64 = _b64(raw)
    else:
        # validate & normalize
        try:
            base64.b64decode(sec_b64, validate=True)
        except Exception:
            sec_b64 = _b64(sec_b64)

    return key, sec_b64


def creds_diagnostics() -> Dict[str, Optional[str]]:
    key = os.getenv("KRAKEN_API_KEY", "")
    s_raw = os.getenv("KRAKEN_API_SECRET", "")
    s_b64 = os.getenv("KRAKEN_API_SECRET_B64", "")
    return {
        "api_key_len": str(len(key)),
        "has_secret_raw": str(bool(s_raw)),
        "secret_b64_len": str(len(s_b64)),
    }
