# config.py — edit these values to fit your fund
COINGECKO_API_KEY = "CG-pxTiHP9YAcYPc1JK8tJFw53c"  # get a free Demo key from CoinGecko

UNIVERSE = [
    # (CoinGecko ID, Kraken symbol hint)
    ("dogwifcoin", "WIF"),
    ("bonk", "BONK"),
    ("pepe", "PEPE"),
    ("floki", "FLOKI"),
    ("baby-doge-coin", "BABYDOGE"),
    ("book-of-meme", "BOME"),
    ("ordinals", "ORDI"),
    ("sui", "SUI"),
    ("sei-network", "SEI"),
    ("aptos", "APT"),
]

# Filters and sizing
TAKE = 8                  # how many names from the screen to trade
MIN_VOL_USD = 20_000_000  # soft liquidity filter on screen step (applied heuristically)
NAV_USD = 1500          # notional NAV
RISK_PER_TRADE_PCT = 0.5  # % of NAV risked per ticket
SINGLE_TRADE_CAP_USD = 150  # cap per single quote notional

# Fees and execution assumptions (bps = 0.01%)
MAKER_BPS = 2        # assumed maker entry fee
TAKER_BPS = 6        # assumed taker exit fee
SLIPPAGE_OUT_BPS = 4 # assumed exit slippage (stop/TP)

# Strategy knobs
ATR_LOOKBACK_BARS = 14     # on 5-min CG data
STOP_ATR_MULT = 1.0
TP_ATR_MULT   = 2.0
MAKER_SPREAD_FRACTION = 0.6  # fraction of median spread to target as edge
