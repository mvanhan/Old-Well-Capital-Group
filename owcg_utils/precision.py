from __future__ import annotations
from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext

getcontext().prec = 28  # plenty for crypto ticks

def q(x: str | float | int) -> Decimal:
    return Decimal(str(x))

def round_price(x, inc, mode="nearest") -> Decimal:
    x, inc = q(x), q(inc)
    if inc <= 0:
        return x
    n = (x / inc)
    if mode == "down":
        n = n.to_integral_value(rounding=ROUND_DOWN)
    elif mode == "up":
        n = n.to_integral_value(rounding=ROUND_UP)
    else:
        n = (n + q("0.5")).to_integral_value(rounding=ROUND_DOWN)
    return n * inc

def round_size(x, inc, mode="down") -> Decimal:
    return round_price(x, inc, mode=mode)
