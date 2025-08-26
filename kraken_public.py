# kraken_public.py — small public-data wrapper for Kraken
from __future__ import annotations
from typing import Dict, List, Optional

import requests

API_BASE = "https://api.kraken.com/0/public"


def _get(path: str, params=None, timeout=20):
    url = f"{API_BASE}{path}"
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(j["error"])
    return j["result"]


def list_asset_pairs() -> Dict:
    return _get("/AssetPairs")


def _find_pair_info_by_altname(altname: str) -> Optional[Dict]:
    res = list_asset_pairs()
    for _, v in res.items():
        if (v.get("altname") or "") == altname:
            return v
    return None


def _canonical_usd_pairs_for(symbol: str) -> List[str]:
    base = symbol.upper()
    if base == "BTC":
        base = "XBT"
    res = list_asset_pairs()
    out: List[str] = []
    for _, v in res.items():
        alt = v.get("altname") or ""
        if alt.endswith("USD") and alt.startswith(base):
            out.append(alt)
    return sorted(set(out))


def find_usd_pairs_for_symbol(symbol: str) -> List[str]:
    return _canonical_usd_pairs_for(symbol)


def ticker_info(pair: str) -> Dict:
    res = _get("/Ticker", {"pair": pair})
    return list(res.values())[0]


def order_book(pair: str, count: int = 50) -> Dict:
    res = _get("/Depth", {"pair": pair, "count": count})
    return list(res.values())[0]


def pair_decimals(pair: str) -> int:
    v = _find_pair_info_by_altname(pair)
    if v:
        return int(v.get("pair_decimals", 2))
    return 2


def ordermin_for_pair(pair: str) -> float:
    """Minimum base-asset order size (volume) for this pair (ordermin)."""
    v = _find_pair_info_by_altname(pair)
    if not v:
        return 0.0
    try:
        return float(v.get("ordermin", 0.0))
    except Exception:
        return 0.0


def base_asset_for_pair(pair: str) -> str:
    """
    Returns Kraken's base asset code for a given altname pair (e.g., 'ETHFIUSD' -> 'ETHFI' or 'XETH' style).
    Use this key to look up balances in /private/Balance.
    """
    v = _find_pair_info_by_altname(pair)
    if not v:
        return ""
    # Kraken returns something like 'base': 'XETH' (or just 'ETHFI' for some tokens)
    return str(v.get("base", "")).strip()
