# broker/coinbase_public.py
from decimal import Decimal, ROUND_DOWN
from typing import Optional

try:
    # Official SDK (handles auth if keys provided; works unauthenticated for public endpoints)
    from coinbase.rest import RESTClient
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "Missing dependency 'coinbase-advanced-py'. "
        "Add it to requirements and pip install before using Coinbase runners."
    ) from e


class CoinbasePublic:
    """
    Lightweight wrapper around Coinbase Advanced Trade 'public products' to:
      • map pair strings to product_id (e.g., 'APT/USD' -> 'APT-USD')
      • fetch product increments (base_increment, quote_increment)
      • round price/size to valid increments

    Uses RESTClient(). Public endpoints can be called without API keys.
    """

    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None, timeout: int = 10):
        kwargs = {}
        if api_key and api_secret:
            kwargs.update(dict(api_key=api_key, api_secret=api_secret))
        self.client = RESTClient(**kwargs)
        self.timeout = timeout
        self._cache = {}

    # ---------- Pair mapping ----------
    @staticmethod
    def normalize_base(asset: str) -> str:
        s = asset.upper().replace("XBT", "BTC")
        return s

    @staticmethod
    def map_pair_to_product_id(pair_like: str, default_quote: str = "USD") -> str:
        """
        Accepts forms like: 'APT/USD', 'APT-USD', 'APTUSDC', 'APTUSDT', 'APTUSD'.
        Returns a Coinbase-style product_id 'BASE-QUOTE'.
        Heuristics:
          • Prefer configured default_quote (USD by default)
          • If incoming endswith 'USDC'/'USD'/'USDT', respect it
        """
        s = pair_like.strip().upper().replace("/", "-")
        s = s.replace(" ", "")
        if "-" in s:
            base, quote = s.split("-", 1)
        else:
            # No separator: infer from trailing stablecoins/fiat tokens
            for q in ("USDC", "USDT", "USD", "EUR", "GBP"):
                if s.endswith(q):
                    base, quote = s[: -len(q)], q
                    break
            else:
                base, quote = s, default_quote
        base = CoinbasePublic.normalize_base(base)
        quote = quote.upper()
        return f"{base}-{quote}"

    # ---------- Product info ----------
    def _get_product_public(self, product_id: str) -> dict:
        # Cache by product_id for short runs
        if product_id in self._cache:
            return self._cache[product_id]
        # Public endpoint (SDK: get_public_product)
        prod = self.client.get_public_product(product_id=product_id)
        # SDK returns an object; allow both obj and dict access
        if hasattr(prod, "to_dict"):
            prod = prod.to_dict()
        self._cache[product_id] = prod
        return prod

    # ---------- Increments & rounding ----------
    @staticmethod
    def _to_dec(x) -> Decimal:
        return Decimal(str(x))

    @staticmethod
    def _round_down_to_increment(value: Decimal, increment: Decimal) -> Decimal:
        if increment == 0:
            return value
        # floor to nearest multiple of increment
        q = (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        return q.quantize(increment)

    def quote_increment(self, product_id: str) -> Decimal:
        p = self._get_product_public(product_id)
        inc = p.get("quote_increment") or p.get("price_increment")
        return self._to_dec(inc)

    def base_increment(self, product_id: str) -> Decimal:
        p = self._get_product_public(product_id)
        inc = p.get("base_increment") or "0.00000001"
        return self._to_dec(inc)

    def round_price(self, product_id: str, price) -> Decimal:
        return self._round_down_to_increment(self._to_dec(price), self.quote_increment(product_id))

    def round_size(self, product_id: str, size) -> Decimal:
        return self._round_down_to_increment(self._to_dec(size), self.base_increment(product_id))
