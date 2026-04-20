from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext
from typing import Any

getcontext().prec = 28


def q(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def round_price(x: Any, inc: Any, mode: str = "nearest") -> Decimal:
    x = q(x)
    inc = q(inc)

    if inc <= 0:
        return x

    n = x / inc
    mode = str(mode).lower()

    if mode == "down":
        n = n.to_integral_value(rounding=ROUND_DOWN)
    elif mode == "up":
        n = n.to_integral_value(rounding=ROUND_UP)
    else:
        n = (n + q("0.5")).to_integral_value(rounding=ROUND_DOWN)

    return n * inc


def round_size(x: Any, inc: Any, mode: str = "down") -> Decimal:
    return round_price(x, inc, mode=mode)