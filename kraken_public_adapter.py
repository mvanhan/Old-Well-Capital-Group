"""
Minimal public-market data adapter for Kraken REST.

Provides:
- get_orderbook_levels(pair: "BTC/USD", levels: int) -> {"bids":[(px,sz),...], "asks":[(px,sz),...]}
- get_ticker_last(pair: "BTC/USD") -> float last_price

We format pairs as Kraken expects for REST (e.g., XBTUSD, ETHUSD, FLOKIUSD).
"""

from __future__ import annotations
import requests
from typing import Dict, List, Tuple

KRAKEN_REST = "https://api.kraken.com"

# Map common symbols to Kraken's canonical base codes where they differ
_BASE_MAP = {
    "BTC": "XBT",
    # Most others (ETH, SOL, APT, FLOKI, etc.) are already Kraken-style
}

def _to_rest_pair(pair: str) -> str:
    """
    Convert "BTC/USD" -> "XBTUSD", "ETH/USD" -> "ETHUSD"
    """
    pair = pair.upper().strip().replace(" ", "")
    if "/" in pair:
        base, quote = pair.split("/", 1)
    else:
        # already collapsed? assume last 3-4 chars are quote; fall back
        if pair.endswith("USD"):
            base, quote = pair[:-3], "USD"
        else:
            # default assume USD if missing
            base, quote = pair, "USD"
    base = _BASE_MAP.get(base, base)
    return f"{base}{quote}"

def get_orderbook_levels(pair: str, levels: int = 5) -> Dict[str, List[Tuple[float, float]]]:
    """
    Depth endpoint:
      GET /0/public/Depth?pair=<PAIR>&count=<levels>
    Returns standardized dict with bids/asks as [(price, size), ...] best->worse.
    """
    rest_pair = _to_rest_pair(pair)
    url = f"{KRAKEN_REST}/0/public/Depth"
    params = {"pair": rest_pair, "count": int(levels)}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"Kraken Depth error: {j['error']}")
    result = j.get("result") or {}
    # Kraken nests the actual pair under a dynamic key; pick the first value
    book = next(iter(result.values()))
    bids = [(float(px), float(sz)) for px, sz, *_ in book.get("bids", [])]
    asks = [(float(px), float(sz)) for px, sz, *_ in book.get("asks", [])]
    if not bids or not asks:
        raise RuntimeError(f"No depth returned for {pair} (resolved {rest_pair})")
    return {"bids": bids, "asks": asks}

def get_ticker_last(pair: str) -> float:
    """
    Ticker endpoint:
      GET /0/public/Ticker?pair=<PAIR>
    Returns the last trade price.
    """
    rest_pair = _to_rest_pair(pair)
    url = f"{KRAKEN_REST}/0/public/Ticker"
    params = {"pair": rest_pair}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"Kraken Ticker error: {j['error']}")
    result = j.get("result") or {}
    info = next(iter(result.values()))
    last = info.get("c", [None])[0]
    if last is None:
        raise RuntimeError(f"No last price in Ticker for {pair} (resolved {rest_pair})")
    return float(last)
