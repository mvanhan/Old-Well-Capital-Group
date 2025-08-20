# run_strategy.py — end-to-end: screen with CoinGecko, map to Kraken, make tickets, compute PnL
import os, json
import pandas as pd
import numpy as np
import config
from coingecko_client import fetch_tickers, fetch_prices_1d_5m
from kraken_public import find_usd_pairs_for_symbol, ticker_info, order_book, pair_decimals
from utils import annualized_rv_from_series, atr_from_close
from signal_engine import make_maker_tickets, ticket_pnl_scenarios

os.makedirs("/mnt/data/output", exist_ok=True)

def screen_and_rank():
    rows = []
    for cid, sym in config.UNIVERSE:
        tix = fetch_tickers(cid)
        if tix.empty:
            rows.append({"coin_id": cid, "symbol_hint": sym, "spread_median_bps": np.nan,
                         "venue_price_dispersion_bps": np.nan, "rv_1d_pct": np.nan, "markets_used": 0})
            continue
        tix = tix.dropna(subset=["last"]).sort_values("converted_volume", ascending=False).head(20)
        prices = tix["last"].astype(float).values
        spreads = tix["bid_ask_spread_percentage"].astype(float).replace([np.inf, -np.inf], np.nan).dropna().values

        spread_bps = float(np.nanmedian(spreads) * 100.0) if len(spreads) else np.nan
        pmin, pmax = float(prices.min()), float(prices.max())
        pmid = 0.5*(pmax + pmin)
        disp_bps = float(((pmax - pmin)/pmid) * 10000.0) if pmid > 0 else np.nan

        px = fetch_prices_1d_5m(cid)
        rv = annualized_rv_from_series(px) if not px.empty else np.nan

        rows.append({"coin_id": cid, "symbol_hint": sym,
                     "spread_median_bps": spread_bps,
                     "venue_price_dispersion_bps": disp_bps,
                     "rv_1d_pct": rv,
                     "markets_used": len(tix)})
    df = pd.DataFrame(rows)
    for col in ["spread_median_bps","venue_price_dispersion_bps","rv_1d_pct"]:
        df[f"{col}_pctile"] = df[col].rank(pct=True)
    df["inefficiency_score"] = (0.5*df["spread_median_bps_pctile"] +
                                0.3*df["venue_price_dispersion_bps_pctile"] +
                                0.2*df["rv_1d_pct_pctile"])
    df = df.sort_values("inefficiency_score", ascending=False).head(config.TAKE)
    df.to_csv("/mnt/data/output/screen_latest.csv", index=False)
    return df

def build_tickets(screen_df: pd.DataFrame):
    tickets = []
    for _, row in screen_df.iterrows():
        cid = row["coin_id"]; sym = row["symbol_hint"]
        pairs = find_usd_pairs_for_symbol(sym)
        if not pairs:
            tickets.append({"coin_id": cid, "kraken_pair": None, "status": "SKIPPED", "reason": "No Kraken USD pair"})
            continue
        pair = pairs[0]
        ti = ticker_info(pair)
        bid = float(ti["b"][0]); ask = float(ti["a"][0]); mid = 0.5*(bid+ask)
        try:
            pdec = pair_decimals(pair)
        except Exception:
            pdec = 2

        px = fetch_prices_1d_5m(cid)
        atr_abs = atr_from_close(px, config.ATR_LOOKBACK_BARS) if not px.empty else None

        # size cap by risk
        risk_dollars = config.NAV_USD * (config.RISK_PER_TRADE_PCT/100.0)
        qty_usd_cap = min(risk_dollars, config.SINGLE_TRADE_CAP_USD)

        mk = make_maker_tickets(
            coin_id=cid, pair=pair, mid=mid, spread_median_bps=row["spread_median_bps"],
            price_decimals=pdec, qty_usd_cap=qty_usd_cap, atr_abs=atr_abs or 0.0,
            stop_mult=config.STOP_ATR_MULT, tp_mult=config.TP_ATR_MULT,
            maker_fraction=config.MAKER_SPREAD_FRACTION
        )
        for t in mk:
            res = ticket_pnl_scenarios(t,
                maker_bps=config.MAKER_BPS,
                taker_bps=config.TAKER_BPS,
                slippage_out_bps=config.SLIPPAGE_OUT_BPS
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
                "rv_1d_pct": row["rv_1d_pct"],
                "inefficiency_score": row["inefficiency_score"],
                "entry_fee_usd": res["entry_fee_usd"],
                "exit_tp_fee_usd": res["exit_tp_fee_usd"],
                "exit_stop_fee_usd": res["exit_stop_fee_usd"],
                "pnl_if_tp_usd": res["pnl_if_tp_usd"],
                "pnl_if_stop_usd": res["pnl_if_stop_usd"],
                "status": "SUGGESTED"
            })
    tdf = pd.DataFrame(tickets)
    tdf.to_csv("/mnt/data/output/trade_tickets_latest.csv", index=False)
    return tdf

def summarize_pnl(tdf: pd.DataFrame):
    # Provide both total TP and total Stop PnL sums, and list each trade’s PnL pair
    if tdf.empty:
        return pd.DataFrame(), {"total_pnl_if_tp_usd": 0.0, "total_pnl_if_stop_usd": 0.0}
    summary = {
        "total_pnl_if_tp_usd": float(tdf["pnl_if_tp_usd"].sum(skipna=True)),
        "total_pnl_if_stop_usd": float(tdf["pnl_if_stop_usd"].sum(skipna=True)),
        "num_trades": int(len(tdf))
    }
    pd.DataFrame([summary]).to_csv("/mnt/data/output/pnl_summary_latest.csv", index=False)
    return tdf[["kraken_pair","side","entry_price","qty","take_profit","stop","pnl_if_tp_usd","pnl_if_stop_usd"]], summary

if __name__ == "__main__":
    screen_df = screen_and_rank()
    tickets_df = build_tickets(screen_df)
    trade_list, totals = summarize_pnl(tickets_df)
    print("Top trades with PnL scenarios (first 10):")
    print(trade_list.head(10).to_string(index=False))
    print("\nTotals:", totals)
    print("\nFiles saved to /mnt/data/output/: screen_latest.csv, trade_tickets_latest.csv, pnl_summary_latest.csv")
