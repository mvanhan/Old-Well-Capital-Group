# utils.py
import math
import numpy as np
import pandas as pd

def annualized_rv_from_series(prices: pd.Series, bars_per_day: int = 288) -> float:
    px = prices.astype(float).values
    if px.size < 3:
        return float("nan")
    rets = np.diff(np.log(px))
    return float(np.std(rets) * math.sqrt(bars_per_day) * 100.0)

def atr_from_close(prices: pd.Series, lookback: int = 14) -> float:
    s = prices.astype(float).values
    if len(s) < lookback + 2:
        return float("nan")
    tr = np.abs(np.diff(s))
    return float(np.mean(tr[-lookback:]))

def bps_to_abs(bps: float, price: float) -> float:
    return price * (bps / 10000.0)

def round_to_decimals(x: float, decimals: int) -> float:
    step = 10 ** (-decimals)
    return round(x / step) * step
