# broker/binance_public.py
import time
import requests
from decimal import Decimal, ROUND_DOWN

class BinancePublic:
    """
    Thin REST client for Binance Spot public endpoints we need:
      - GET /api/v3/exchangeInfo
    Handles symbol filters (PRICE_FILTER, LOT_SIZE, MIN_NOTIONAL/NOTIONAL) and rounding helpers.
    Works for both binance.com and binance.us by switching api_base.
    """
    def __init__(self, api_base: str = "https://api.binance.com", timeout=10):
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._exchange_info_cache = {}
        self._all_info_last_ts = 0

    # ---------- HTTP ----------
    def _get(self, path: str, params=None):
        url = f"{self.api_base}{path}"
        r = requests.get(url, params=params or {}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ---------- Exchange Info ----------
    def get_symbol_info(self, symbol: str) -> dict:
        symbol = symbol.upper()
        now = time.time()
        # cache by symbol for 30s
        if symbol in self._exchange_info_cache and now - self._exchange_info_cache[symbol]["_ts"] < 30:
            return self._exchange_info_cache[symbol]["data"]
        data = self._get("/api/v3/exchangeInfo", params={"symbol": symbol})
        if "symbols" in data and data["symbols"]:
            info = data["symbols"][0]
        else:
            raise ValueError(f"Symbol not found on Binance: {symbol}")
        self._exchange_info_cache[symbol] = {"_ts": now, "data": info}
        return info

    def _find_filter(self, info: dict, ftype: str) -> dict | None:
        for f in info.get("filters", []):
            if f.get("filterType") == ftype:
                return f
        return None

    # ---------- Rounding & Validation ----------
    @staticmethod
    def _to_dec(x) -> Decimal:
        return Decimal(str(x))

    @staticmethod
    def _round_to_step(value: Decimal, step: Decimal) -> Decimal:
        """
        Floors value to a multiple of 'step' using Decimal math.
        """
        if step == 0:
            return value
        quant = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
        # normalize to string precision of step
        return quant.quantize(step)

    def round_price(self, symbol: str, price) -> Decimal:
        info = self.get_symbol_info(symbol)
        pf = self._find_filter(info, "PRICE_FILTER")
        if not pf:
            return self._to_dec(price)
        tick = self._to_dec(pf["tickSize"])
        return self._round_to_step(self._to_dec(price), tick)

    def round_qty(self, symbol: str, qty) -> Decimal:
        info = self.get_symbol_info(symbol)
        lf = self._find_filter(info, "LOT_SIZE")
        if not lf:
            return self._to_dec(qty)
        step = self._to_dec(lf["stepSize"])
        return self._round_to_step(self._to_dec(qty), step)

    def min_notional(self, symbol: str) -> Decimal | None:
        info = self.get_symbol_info(symbol)
        nf = self._find_filter(info, "MIN_NOTIONAL") or self._find_filter(info, "NOTIONAL")
        if not nf:
            return None
        return self._to_dec(nf.get("minNotional", "0"))

    def ensure_notional_ok(self, symbol: str, price: Decimal, qty: Decimal) -> None:
        mn = self.min_notional(symbol)
        if mn is None:
            return
        notional = price * qty
        if notional < mn:
            raise ValueError(f"Order notional {notional} < minNotional {mn} for {symbol}")

    # ---------- Symbols ----------
    @staticmethod
    def make_symbol(base: str, quote: str) -> str:
        return f"{base.upper()}{quote.upper()}"

    @staticmethod
    def map_pair_to_binance_symbol(pair_like: str, default_quote: str = "USDT") -> str:
        """
        Accepts strings like 'APT/USD', 'APTUSD', 'APT-USDT', 'APT/USDT'.
        Heuristic: strip separators; if endswith 'USD' and default_quote is 'USDT', replace.
        Kraken's 'XBT' -> 'BTC' normalization is handled.
        """
        s = pair_like.replace("/", "").replace("-", "").upper()
        # Common normalizations
        if s.startswith("XBT"):
            s = "BTC" + s[3:]
        if s.endswith("USD") and default_quote == "USDT":
            s = s[:-3] + "USDT"
        if s.endswith("USDT") and default_quote == "USD":
            s = s[:-4] + "USD"
        return s
