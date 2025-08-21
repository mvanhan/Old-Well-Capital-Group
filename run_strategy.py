# run_strategy.py — screen with CoinGecko, map to Kraken, size tickets, compute PnL
import os, math, time
import pandas as pd
import numpy as np
import config as C
from coingecko_client import fetch_tickers, fetch_prices_1d_5m
from kraken_public import find_usd_pairs_for_symbol, ticker_info
from utils import annualized_rv_from_series, atr_from_close
from signal_engine import make_maker_tickets, ticket_pnl_scenarios

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def screen_and_rank() -> pd.DataFrame:
    rows = []
    for cid, sym in C.UNIVERSE:
        # Find Kraken USD pairs and compute spreads
        spreads = []
        pair_candidates = find_usd_pairs_for_symbol(sym)  # e.g., ['WIFUSD']
        for p in pair_candidates:
            try:
                t = ticker_info(p)
                bid = float(t['b'][0]); ask = float(t['a'][0])
                mid = (bid + ask) / 2.0
                if mid <= 0:
                    continue
                spread_bps = (ask - bid) / mid * 10000.0
                spreads.append((p, mid, spread_bps))
            except Exception:
                continue

        if not spreads:
            continue

        pair, mid, spread_median_bps = sorted(spreads, key=lambda x: x[2])[0]

        # 1-day 5-min for RV/ATR proxy (CoinGecko)
        try:
            px = fetch_prices_1d_5m(cid)  # index=time, price
            rv_1d = annualized_rv_from_series(px['price'], bars_per_day=288)  # %
            atr_5m = atr_from_close(px['price'], lookback=C.ATR_LOOKBACK_BARS)
        except Exception as e:
            print(f"[WARN] fetch_prices_1d_5m({cid}) failed: {e}")
            rv_1d = float('nan'); atr_5m = float('nan')

        ineff = -spread_median_bps + (rv_1d if not math.isnan(rv_1d) else 0.0)

        rows.append({
            "coin_id": cid,
            "symbol": sym,
            "kraken_pair": pair,
            "mid": mid,
            "spread_median_bps": spread_median_bps,
            "rv_1d_pct": rv_1d,
            "atr_5m": atr_5m,
            "inefficiency_score": ineff,
        })

        time.sleep(0.6)  # gentle pacing to avoid 429 on free tier

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("inefficiency_score", ascending=True).head(C.TAKE).reset_index(drop=True)
    df.to_csv(os.path.join(OUTPUT_DIR, "screen_latest.csv"), index=False)
    return df

def build_tickets(screen_df: pd.DataFrame) -> pd.DataFrame:
    tickets = []
    for _, r in screen_df.iterrows():
        mk = make_maker_tickets(r.to_dict())
        for t in mk:
            fees = ticket_pnl_scenarios(t,
                maker_bps=C.MAKER_BPS,
                taker_bps=C.TAKER_BPS,
                slippage_out_bps=C.SLIPPAGE_OUT_BPS
            )
            tickets.append({
                "coin_id": t.coin_id,
                "kraken_pair": t.kraken_pair,
                "side": t.side,
                "entry_price": t.price,
                "qty": t.qty,
                "stop": t.stop,
                "take_profit": t.take_profit,
                "mid": t.mid,
                "spread_median_bps": t.spread_median_bps,
                "rv_1d_pct": t.rv_1d_pct,
                "inefficiency_score": t.inefficiency_score,
                "entry_fee_usd": fees["entry_fee_usd"],
                "exit_tp_fee_usd": fees["exit_tp_fee_usd"],
                "exit_stop_fee_usd": fees["exit_stop_fee_usd"],
                "pnl_if_tp_usd": fees["pnl_if_tp_usd"],
                "pnl_if_stop_usd": fees["pnl_if_stop_usd"],
                "status": t.status,
            })
    tdf = pd.DataFrame(tickets)
    tdf.to_csv(os.path.join(OUTPUT_DIR, "trade_tickets_latest.csv"), index=False)
    return tdf

def summarize_pnl(tdf: pd.DataFrame):
    if tdf.empty:
        return tdf, {"total_pnl_if_tp_usd": 0.0, "total_pnl_if_stop_usd": 0.0, "num_trades": 0}
    summary = {
        "total_pnl_if_tp_usd": float(tdf["pnl_if_tp_usd"].sum(skipna=True)),
        "total_pnl_if_stop_usd": float(tdf["pnl_if_stop_usd"].sum(skipna=True)),
        "num_trades": int(len(tdf)),
    }
    pd.DataFrame([summary]).to_csv(os.path.join(OUTPUT_DIR, "pnl_summary_latest.csv"), index=False)
    return tdf[["kraken_pair","side","entry_price","qty","take_profit","stop","pnl_if_tp_usd","pnl_if_stop_usd"]], summary

if __name__ == "__main__":
    screen_df = screen_and_rank()
    if screen_df.empty:
        print("No screen results; check universe, API key, or rate limits.")
    else:
        tickets_df = build_tickets(screen_df)
        trade_list, totals = summarize_pnl(tickets_df)
        print("Top trades with PnL scenarios (first 10):")
        print(trade_list.head(10).to_string(index=False))
        print("\nTotals:", totals)
        print(f"\nFiles saved to {OUTPUT_DIR}/: screen_latest.csv, trade_tickets_latest.csv, pnl_summary_latest.csv")
