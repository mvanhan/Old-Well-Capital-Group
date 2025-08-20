# coingecko_client.py — throttled CoinGecko calls (free Demo plan friendly)
import os, time, requests, pandas as pd
import config

API_BASE = "https://api.coingecko.com/api/v3"
HEADERS = {"accept": "application/json"}
if config.COINGECKO_API_KEY and config.COINGECKO_API_KEY != "PUT_YOUR_CG_KEY_HERE":
    HEADERS["x-cg-demo-api-key"] = config.COINGECKO_API_KEY  # Demo/pro header

def _throttle():
    time.sleep(3.2)  # stay under ~30 req/min

def cg_get(path, params=None, throttle=True):
    url = f"{API_BASE}{path}"
    if throttle: _throttle()
    r = requests.get(url, headers=HEADERS, params=params or {}, timeout=30)
    if r.status_code == 429:
        time.sleep(5)
        r = requests.get(url, headers=HEADERS, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()

def fetch_tickers(coin_id: str, exchange_ids: str | None = None) -> pd.DataFrame:
    params = {"page": 1, "order": "volume_desc"}
    if exchange_ids: params["exchange_ids"] = exchange_ids
    j = cg_get(f"/coins/{coin_id}/tickers", params)
    t = j.get("tickers", []) if isinstance(j, dict) else j
    return pd.DataFrame(t)

def fetch_prices_1d_5m(coin_id: str, vs="usd") -> pd.Series:
    params = {"vs_currency": vs, "days": 1}
    j = cg_get(f"/coins/{coin_id}/market_chart", params)
    arr = j.get("prices", [])
    if not arr: return pd.Series(dtype=float)
    return pd.Series([a[1] for a in arr], name="price")
