from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path


CLOSED_LOG = Path("output_stables/closed_trades.csv")


def q(value: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")


def fmt(value: Decimal, places: int = 8) -> str:
    quant = Decimal("1").scaleb(-places)
    text = format(value.quantize(quant), "f")
    text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def main() -> None:
    if not CLOSED_LOG.exists():
        print("all_time trades=0 wins=0 losses=0 flat=0 products=0 total_size=0 avg_size=0 entry_notional=0 avg_notional=0 gross_pnl=0 fees=0 net_pnl=0")
        return

    trades = 0
    wins = 0
    losses = 0
    flat = 0
    total_size = Decimal("0")
    total_entry_notional = Decimal("0")
    gross_pnl = Decimal("0")
    fees = Decimal("0")
    net_pnl = Decimal("0")
    products: set[str] = set()

    with CLOSED_LOG.open() as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            size = q(row.get("filled_size", "0"))
            entry_avg = q(row.get("entry_avg_price", "0"))
            gross = q(row.get("gross_pnl", "0"))
            fee = q(row.get("fees", "0"))
            net = q(row.get("net_pnl", "0"))
            product = (row.get("product_id") or "").strip()

            trades += 1
            total_size += size
            total_entry_notional += size * entry_avg
            gross_pnl += gross
            fees += fee
            net_pnl += net

            if net > 0:
                wins += 1
            elif net < 0:
                losses += 1
            else:
                flat += 1

            if product:
                products.add(product)

    avg_size = total_size / trades if trades else Decimal("0")
    avg_notional = total_entry_notional / trades if trades else Decimal("0")

    print(
        "all_time "
        f"trades={trades} "
        f"wins={wins} "
        f"losses={losses} "
        f"flat={flat} "
        f"products={len(products)} "
        f"total_size={fmt(total_size)} "
        f"avg_size={fmt(avg_size)} "
        f"entry_notional={fmt(total_entry_notional, 2)} "
        f"avg_notional={fmt(avg_notional, 2)} "
        f"gross_pnl={fmt(gross_pnl)} "
        f"fees={fmt(fees)} "
        f"net_pnl={fmt(net_pnl)}"
    )


if __name__ == "__main__":
    main()