# kraken_public.py — public market data for Kraken
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

def list_asset_pairs():
    return _get("/AssetPairs")

def find_usd_pairs_for_symbol(symbol_hint: str):
    res = list_asset_pairs()
    out = []
    for k, v in res.items():
        alt = v.get("altname") or k
        if symbol_hint.upper() in alt.upper():
            if alt.upper().endswith("USD") or alt.upper().endswith("ZUSD"):
                out.append(alt)
    return sorted(list(set(out)))

def ticker_info(pair: str):
    res = _get("/Ticker", {"pair": pair})
    return list(res.values())[0]

def order_book(pair: str, count: int = 50):
    res = _get("/Depth", {"pair": pair, "count": count})
    return list(res.values())[0]

def pair_decimals(pair: str) -> int:
    res = list_asset_pairs()
    for k, v in res.items():
        alt = v.get("altname") or k
        if alt == pair:
            return int(v.get("pair_decimals", 2))
    return 2
