# coingecko_client.py — header-only auth; Pro/Public compatibility; clearer errors
import time
import requests
import pandas as pd
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import config as C

BASE = getattr(C, "COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3")

def _session():
    s = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.2,
        status_forcelist=[408, 425, 429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

SESSION = _session()

def _is_pro() -> bool:
    return "pro-api.coingecko.com" in BASE

def _headers():
    h = {"accept": "application/json"}
    key = (getattr(C, "COINGECKO_API_KEY", "") or "").strip()
    if key:
        # Primary header works for public and pro keys
        h["x-cg-api-key"] = key
    return h

def _pace():
    # Conservative pacing; adjust per your tier
    time.sleep(1.2)

def _get(path: str, params: dict | None = None) -> requests.Response:
    url = f"{BASE}{path}"
    _pace()
    r = SESSION.get(url, params=(params or {}), headers=_headers(), timeout=30)
    if r.status_code == 401:
        raise requests.HTTPError(
            f"401 Unauthorized from CoinGecko (BASE={BASE}). "
            f"Headers sent={list(_headers().keys())}; key_present={bool(getattr(C,'COINGECKO_API_KEY',''))}",
            response=r
        )
    if r.status_code >= 400:
        snippet = r.text[:300].replace("\n", " ")
        raise requests.HTTPError(f"{r.status_code} error at {url} :: {snippet}", response=r)
    return r

def fetch_tickers(coin_id: str) -> dict:
    r = _get(f"/coins/{coin_id}/tickers")
    return r.json()

# def fetch_prices_1d_5m(coin_id: str) -> pd.DataFrame:
#     """
#     Public base: omit interval to avoid 400.
#     Pro base: request 5m data with interval=5m.
#     """
#     params = {"vs_currency": "usd", "days": 1}
#     if _is_pro():
#         params["interval"] = "5m"
#     r = _get(f"/coins/{coin_id}/market_chart", params=params)
#     j = r.json()
#     rows = j.get("prices", [])
#     df = pd.DataFrame(rows, columns=["ms", "price"])
#     if df.empty:
#         return pd.DataFrame(columns=["time", "price"])
#     df["time"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
#     return df[["time", "price"]].set_index("time").sort_index()


# For Testing
def fetch_prices_1d_5m(coin_id: str) -> pd.DataFrame:
    """
    Free/public base: omit interval (not supported).
    Normalize to true 5-minute bars for ATR(5m) and strategy math.
    """
    r = _get(f"/coins/{coin_id}/market_chart",
             params={"vs_currency": "usd", "days": 1})
    j = r.json()
    rows = j.get("prices", [])
    df = pd.DataFrame(rows, columns=["ms", "price"])
    if df.empty:
        return pd.DataFrame(columns=["time", "price"])
    df["time"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
    df = df[["time", "price"]].set_index("time").sort_index()

    # Ensure consistent 5-minute bars
    df = df.resample("5min").last().interpolate(limit_direction="both")

    return df
