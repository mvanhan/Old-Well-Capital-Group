# run_strategy.py — screen with CoinGecko, map to Kraken, size tickets, compute PnL
import os
from pathlib import Path

import pandas as pd

import config as C
from signal_engine import screen_and_build_candidates, make_maker_tickets, ticket_pnl_scenarios

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

def main():
    cands = screen_and_build_candidates()
    df = make_maker_tickets(cands)

    if df.empty:
        print("No tickets generated.")
        return

    # Compute PnL scenarios
    pnl_tp = []
    pnl_st = []
    for _, row in df.iterrows():
        t, s = ticket_pnl_scenarios(row)
        pnl_tp.append(t)
        pnl_st.append(s)
    df["pnl_if_tp_usd"] = pnl_tp
    df["pnl_if_stop_usd"] = pnl_st

    # Pretty print top trades
    print("Top trades with PnL scenarios (first 10):")
    print(df.head(10).to_string(index=False))

    # Save artifacts
    df.to_csv(OUTPUT_DIR / "trade_tickets_latest.csv", index=False)
    summary = {
        "total_pnl_if_tp_usd": float(df["pnl_if_tp_usd"].sum()),
        "total_pnl_if_stop_usd": float(df["pnl_if_stop_usd"].sum()),
        "num_trades": int(len(df)),
    }
    pd.DataFrame([summary]).to_csv(OUTPUT_DIR / "pnl_summary_latest.csv", index=False)
    df.head(50).to_csv(OUTPUT_DIR / "screen_latest.csv", index=False)

    print("\nTotals:", summary)
    print(f"\nFiles saved to {OUTPUT_DIR}/: screen_latest.csv, trade_tickets_latest.csv, pnl_summary_latest.csv")


if __name__ == "__main__":
    main()
