# broker/coinbase_public.py
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Tuple, Dict, Any, List

try:
    # Official SDK (handles auth if keys provided; works unauthenticated for public endpoints,
    # but Advanced endpoints often still require Authorization)
    from coinbase.rest import RESTClient
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "Missing dependency 'coinbase-advanced-py'. "
        "Add it to requirements and pip install before using Coinbase runners."
    ) from e


def _to_dict(obj):
    return obj.to_dict() if hasattr(obj, "to_dict") else obj


class CoinbasePublic:
    """
    Lightweight wrapper around Coinbase Advanced Trade public endpoints to:
      • map pair strings to product_id (e.g., 'APT/USD' -> 'APT-USD')
      • list/filter products
      • fetch product increments (base_increment, quote_increment)
      • round price/size to valid increments
      • fetch best bid/ask (robust, with fallback)
    """

    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None, timeout: int = 10):
        kwargs = {}
        if api_key and api_secret:
            kwargs.update(dict(api_key=api_key, api_secret=api_secret, timeout=timeout))
            self.client = RESTClient(**kwargs)
        else:
            self.client = RESTClient(timeout=timeout)
        self._cache: Dict[str, Any] = {}

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
        """
        s = pair_like.strip().upper().replace("/", "-").replace(" ", "")
        if "-" in s:
            base, quote = s.split("-", 1)
        else:
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
    def list_products(self) -> List[Dict[str, Any]]:
        data = _to_dict(self.client.get_public_products())
        prods = data.get("products") or data.get("data") or []
        return [_to_dict(p) for p in prods]

    def get_product(self, product_id: str) -> Dict[str, Any]:
        if product_id in self._cache:
            return self._cache[product_id]
        prod = _to_dict(self.client.get_public_product(product_id=product_id))
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
        q = (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        return q.quantize(increment)

    def quote_increment(self, product_id: str) -> Decimal:
        p = self.get_product(product_id)
        inc = p.get("quote_increment") or p.get("price_increment") or "0.01"
        return self._to_dec(inc)

    def base_increment(self, product_id: str) -> Decimal:
        p = self.get_product(product_id)
        inc = p.get("base_increment") or "0.00000001"
        return self._to_dec(inc)

    def round_price(self, product_id: str, price) -> Decimal:
        return self._round_down_to_increment(self._to_dec(price), self.quote_increment(product_id))

    def round_size(self, product_id: str, size) -> Decimal:
        return self._round_down_to_increment(self._to_dec(size), self.base_increment(product_id))

    # ---------- Robust best bid/ask ----------
    def best_bid_ask(self, product_id: str) -> Tuple[Decimal, Decimal]:
        """
        Returns (best_bid, best_ask) as Decimals.
        Tries Best Bid/Ask endpoint first, then falls back to product_book.
        Handles multiple possible response shapes from SDK/models.
        """
        # 1) Try best-bid/ask endpoint
        try:
            resp = _to_dict(self.client.get_best_bid_ask(product_ids=[product_id]))
            # Common shapes:
            #   {'pricebooks': [{'product_id': 'BTC-USD', 'bids': [{'price': '...', 'size': '...'}], 'asks': [...]}]}
            # or {'data': {'pricebooks': [...]}}
            books = resp.get("pricebooks") or (resp.get("data") or {}).get("pricebooks") or []
            item = None
            if isinstance(books, list):
                for b in books:
                    d = _to_dict(b)
                    if d.get("product_id") == product_id:
                        item = d
                        break
            if item:
                bids = item.get("bids") or []
                asks = item.get("asks") or []
                bid_price = None
                ask_price = None
                if isinstance(bids, list) and bids:
                    b0 = _to_dict(bids[0])
                    bid_price = self._to_dec(b0.get("price") if isinstance(b0, dict) else b0)
                if isinstance(asks, list) and asks:
                    a0 = _to_dict(asks[0])
                    ask_price = self._to_dec(a0.get("price") if isinstance(a0, dict) else a0)
                if bid_price is not None and ask_price is not None:
                    return bid_price, ask_price
        except Exception:
            # swallow and try fallback
            pass

        # 2) Fallback: L1 book
        book = _to_dict(self.client.get_product_book(product_id=product_id, limit=1))
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            raise RuntimeError(f"No depth for {product_id}")
        best_bid = self._to_dec(bids[0]["price"])
        best_ask = self._to_dec(asks[0]["price"])
        return best_bid, best_ask
