# coingecko_client.py — CoinGecko FREE (with optional Pro) + symbol→coin_id resolution
from __future__ import annotations
import time
from typing import Dict, Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config as C


def _session() -> requests.Session:
    s = requests.Session()
    r = Retry(
        total=5,
        backoff_factor=1.2,
        status_forcelist=[408, 425, 429, 500, 502, 503, 504],
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=r))
    return s


def _compute_base() -> str:
    base = getattr(C, "COINGECKO_BASE_URL", None)
    if isinstance(base, str):
        b = base.strip()
        if b and b.lower() not in ("none", "null", "~"):
            if not (b.startswith("http://") or b.startswith("https://")):
                b = "https://" + b.lstrip("/")
            return b.rstrip("/")
    # default
    return "https://pro-api.coingecko.com/api/v3" if getattr(C, "COINGECKO_USE_PRO", False) else "https://api.coingecko.com/api/v3"


def _headers() -> Dict[str, str]:
    h = {"accept": "application/json", "user-agent": "owcg/1.0"}
    if getattr(C, "COINGECKO_USE_PRO", False) and getattr(C, "COINGECKO_API_KEY", ""):
        h["x-cg-pro-api-key"] = C.COINGECKO_API_KEY
    return h


def _get(path: str, params: Dict = None, timeout=20, retries: int = 3) -> requests.Response:
    sess = _session()
    base = _compute_base()
    url = f"{base}{path}"

    for _ in range(retries):
        r = sess.get(url, params=params or {}, headers=_headers(), timeout=timeout)
        if r.status_code == 429:
            delay = 2
            try: delay = int(r.headers.get("Retry-After", "2"))
            except Exception: pass
            time.sleep(max(1, min(10, delay)))
            continue
        if r.status_code >= 400:
            txt = (r.text or "")[:300].replace("\n", " ")
            if getattr(C, "COINGECKO_USE_PRO", False) and r.status_code == 401:
                raise requests.HTTPError("401 Unauthorized: Pro host requires a valid API key.", response=r)
            raise requests.HTTPError(f"{r.status_code} at {url} :: {txt}", response=r)
        return r

    r.raise_for_status()
    return r


# -------- Symbol → Coin ID --------
_KNOWN_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "APT": "aptos",
    "FLOKI": "floki",
    "PEPE": "pepe",
    "BONK": "bonk",
    "WIF": "dogwifcoin",
    "SUI": "sui",
    "SEI": "sei-network",
    "ORDI": "ordinals",
}


def coin_id_for_symbol(symbol: str) -> Optional[str]:
    sym = (symbol or "").upper().strip()
    if not sym:
        return None
    if sym in _KNOWN_MAP:
        return _KNOWN_MAP[sym]
    try:
        r = _get("/search", params={"query": sym})
        j = r.json()
        for c in j.get("coins", []):
            if str(c.get("symbol", "")).upper() == sym:
                return c.get("id")
    except Exception:
        pass
    return None


# -------- Data fetch --------
def fetch_tickers(coin_id: str) -> dict:
    return _get(f"/coins/{coin_id}/tickers").json()


def fetch_prices_1d_5m(coin_id: str) -> pd.DataFrame:
    r = _get(f"/coins/{coin_id}/market_chart", params={"vs_currency": "usd", "days": 1})
    j = r.json()
    prices = j.get("prices") or []
    if not prices:
        return pd.DataFrame(columns=["time", "close"])
    df = pd.DataFrame(prices, columns=["ms", "close"])
    df["time"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
    df = df[["time", "close"]].set_index("time").sort_index()
    df = df["close"].resample("5min").last().ffill().to_frame()
    df.reset_index(inplace=True)
    return df
