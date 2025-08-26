"""
env_utils.py
Loads .env deterministically and exposes helpers for Kraken & Coingecko creds.

Key behaviors:
- Loads the nearest .env (or DOTENV_PATH if provided) with override=True so it
  replaces any previously exported env vars in the shell.
- Accepts either KRAKEN_API_SECRET_B64 (preferred) or KRAKEN_API_SECRET (raw or base64).
- Provides diagnostics and the loaded .env path for debugging.
"""
from __future__ import annotations
import os
import base64
import re
from typing import Tuple, Dict

LOADED_DOTENV_PATH: str = ""

def _clean(s: str | None) -> str:
    if s is None:
        return ""
    # strip whitespace and accidental quotes
    return s.strip().strip('"').strip("'")

def _to_base64_if_needed(secret: str) -> str:
    s = _clean(secret)
    if not s:
        return ""
    try:
        base64.b64decode(s, validate=True)  # already base64
        return s
    except Exception:
        return base64.b64encode(s.encode()).decode()

# simple matcher to guess if a string looks base64-ish
_B64ISH = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

def _load_dotenv_override() -> str:
    """
    Load .env with override=True so it replaces any pre-set env vars.
    Preference:
    - If DOTENV_PATH is set and points to a file, use that.
    - Else, use the nearest .env from the current working directory upward.
    Returns the path that was loaded (or empty string if none).
    """
    global LOADED_DOTENV_PATH
    try:
        from dotenv import load_dotenv, find_dotenv
    except Exception:
        # dotenv not installed; rely on OS env
        LOADED_DOTENV_PATH = ""
        return LOADED_DOTENV_PATH

    dotenv_path = os.getenv("DOTENV_PATH") or ""
    if dotenv_path and os.path.isfile(dotenv_path):
        load_dotenv(dotenv_path, override=True)
        LOADED_DOTENV_PATH = dotenv_path
        return LOADED_DOTENV_PATH

    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found, override=True)
        LOADED_DOTENV_PATH = found
    else:
        LOADED_DOTENV_PATH = ""
    return LOADED_DOTENV_PATH

# Load immediately on import
_load_dotenv_override()

def get_loaded_dotenv_path() -> str:
    """Return the path of the .env file that was loaded (may be empty)."""
    return LOADED_DOTENV_PATH

def get_kraken_credentials() -> Tuple[str, str]:
    """
    Returns: (api_key, api_secret_b64)

    Accepts:
      - KRAKEN_API_SECRET_B64 (already base64), or
      - KRAKEN_API_SECRET (raw or base64; auto-detect/convert)
    """
    key = _clean(os.getenv("KRAKEN_API_KEY"))
    secret_b64 = _clean(os.getenv("KRAKEN_API_SECRET_B64"))
    if not secret_b64:
        secret_raw_or_b64 = _clean(os.getenv("KRAKEN_API_SECRET"))
        secret_b64 = _to_base64_if_needed(secret_raw_or_b64)
    return key, secret_b64

def get_coingecko_key() -> str:
    return _clean(os.getenv("COINGECKO_API_KEY"))

def diagnose_credentials() -> Dict[str, object]:
    """
    Non-sensitive diagnostics to help debug env issues.
    """
    key, b64 = get_kraken_credentials()
    info = {
        "loaded_dotenv_path": get_loaded_dotenv_path(),
        "api_key_len": len(key),
        "api_key_contains_plus_slash": ("+" in key) or ("/" in key),
        "api_key_looks_base64": bool(_B64ISH.match(key)) if key else False,
        "secret_b64_len": len(b64),
        "secret_b64_decodable": False,
        "secret_raw_len": 0,
        "warnings": [],
    }
    try:
        raw = base64.b64decode(b64, validate=True) if b64 else b""
        info["secret_b64_decodable"] = True if b64 else False
        info["secret_raw_len"] = len(raw)
    except Exception:
        info["warnings"].append(
            "Secret not base64-decodable. If you set KRAKEN_API_SECRET (raw), "
            "env_utils will encode it automatically."
        )

    if info["api_key_looks_base64"] or info["api_key_contains_plus_slash"]:
        info["warnings"].append(
            "API key looks base64-like. Kraken Spot API KEY should NOT be base64; "
            "verify you didn't paste the SECRET into KRAKEN_API_KEY."
        )
    if info["api_key_len"] == 0:
        info["warnings"].append("KRAKEN_API_KEY is empty or missing.")
    if info["secret_b64_len"] == 0:
        info["warnings"].append("KRAKEN_API_SECRET / KRAKEN_API_SECRET_B64 is empty or missing.")
    return info
