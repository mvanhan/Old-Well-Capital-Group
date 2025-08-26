# utils.py — math helpers
from __future__ import annotations
import numpy as np
import pandas as pd


def annualized_rv_from_series(prices: pd.Series, periods_per_year: int = 365 * 288) -> float:
    """
    Realized vol from close series (5m bars by default), annualized (sqrt of periods/year).
    """
    s = pd.Series(prices).astype(float).replace([np.inf, -np.inf], np.nan).dropna()
    if len(s) < 3:
        return 0.0
    rets = np.log(s).diff().dropna()
    vol = np.std(rets)
    return float(vol * np.sqrt(periods_per_year))


def atr_from_close(df: pd.DataFrame, lookback: int = 14) -> float:
    """
    Simple ATR proxy from closes: mean(abs(diff(close))) over lookback, as fraction of price.
    """
    if df.empty or "close" not in df.columns:
        return 0.0
    c = df["close"].astype(float)
    if len(c) < lookback + 1:
        lookback = max(2, min(lookback, len(c) - 1))
    tr = c.diff().abs()
    atr = tr.rolling(lookback).mean().iloc[-1]
    return float(atr / max(1e-12, c.iloc[-1]))
