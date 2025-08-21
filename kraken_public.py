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
    """Return list of altname pairs like ['WIFUSD', 'BONKUSD'] where base ~ symbol_hint and quote USD."""
    out = []
    res = list_asset_pairs()
    hint = symbol_hint.upper()
    for _, v in res.items():
        alt = v.get("altname") or ""
        wsname = v.get("wsname") or ""
        if alt.endswith("USD") and (alt.startswith(hint) or wsname.upper().startswith(hint)):
            out.append(alt)
    return sorted(set(out))

def ticker_info(pair: str):
    res = _get("/Ticker", {"pair": pair})
    return list(res.values())[0]

def order_book(pair: str, count: int = 50):
    res = _get("/Depth", {"pair": pair, "count": count})
    return list(res.values())[0]

def pair_decimals(pair: str) -> int:
    res = list_asset_pairs()
    for _, v in res.items():
        alt = v.get("altname") or ""
        if alt == pair:
            return int(v.get("pair_decimals", 2))
    return 2
