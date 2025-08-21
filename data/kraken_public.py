# data/kraken_public.py
import requests
import pandas as pd
from typing import Optional, Dict, Any

BASE = "https://api.kraken.com"

# Common symbol → Kraken pair mapping
PAIR_MAP = {
    "BTCUSD": "XBTUSD",
    "ETHUSD": "ETHUSD",
    "SOLUSD": "SOLUSD",
    "WIFUSD": "WIFUSD",
    "BONKUSD": "BONKUSD",
    "PEPEUSD": "PEPEUSD",
    "FLOKIUSD": "FLOKIUSD",
    "SUIUSD": "SUIUSD",
    "SEIUSD": "SEIUSD",
    "APTUSD": "APTUSD",
}

def _pair_to_kraken(symbol: str) -> str:
    return PAIR_MAP.get(symbol.upper(), symbol.upper())

def interval_to_kraken(minutes: int) -> int:
    # Kraken supports: 1,5,15,30,60,240,1440,10080,21600 (minutes)
    return minutes if minutes in [1,5,15,30,60,240,1440,10080,21600] else 1440

def get_ohlc(symbol: str, interval_minutes: int = 1440, since: Optional[int] = None) -> pd.DataFrame:
    pair = _pair_to_kraken(symbol)
    url = f"{BASE}/0/public/OHLC"
    params = {"pair": pair, "interval": interval_to_kraken(interval_minutes)}
    if since is not None:
        params["since"] = since
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    # result key is the pair code (e.g., 'XXBTZUSD' or 'XBTUSD'); pick the first non-'last'
    key = [k for k in data["result"].keys() if k != "last"][0]
    rows = data["result"][key]
    cols = ["time", "open", "high", "low", "close", "vwap", "volume", "count"]
    df = pd.DataFrame(rows, columns=cols)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    for c in ["open", "high", "low", "close", "vwap", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.set_index("time").sort_index()

def get_recent_spreads(symbol: str, since: Optional[int] = None) -> pd.DataFrame:
    pair = _pair_to_kraken(symbol)
    url = f"{BASE}/0/public/Spread"
    params = {"pair": pair}
    if since is not None:
        params["since"] = since
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    key = [k for k in data["result"].keys() if k != "last"][0]
    rows = data["result"][key]
    df = pd.DataFrame(rows, columns=["time", "bid", "ask"])
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    for c in ["bid", "ask"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["spread_bps"] = (df["ask"] - df["bid"]) / ((df["ask"] + df["bid"]) / 2) * 10000.0
    return df.set_index("time").sort_index()

def get_ticker(symbol: str) -> Dict[str, float]:
    """Best bid/ask/last."""
    pair = _pair_to_kraken(symbol)
    url = f"{BASE}/0/public/Ticker"
    r = requests.get(url, params={"pair": pair}, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(j["error"])
    key = next(k for k in j["result"].keys())
    t = j["result"][key]
    return {
        "bid": float(t["b"][0]),
        "ask": float(t["a"][0]),
        "last": float(t["c"][0]),
    }

def get_pair_info(symbol: str) -> Dict[str, Any]:
    """
    Returns price decimals, lot decimals, and minimum order size for the spot pair.
    """
    pair = _pair_to_kraken(symbol)
    url = f"{BASE}/0/public/AssetPairs"
    r = requests.get(url, params={"pair": pair}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(j["error"])
    # Take the first entry in 'result'
    info = next(iter(j["result"].values()))
    return {
        "altname": info.get("altname", pair),
        "pair_decimals": int(info.get("pair_decimals", 2)),
        "lot_decimals": int(info.get("lot_decimals", 8)),
        "ordermin": float(info.get("ordermin", "0.0001")),
    }
