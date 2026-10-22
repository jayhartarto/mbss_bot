"""
research/ipot_explore/audit.py — Phase 0 data audit for the IPOT broker-summary research (no hypotheses; just "what exactly do we have").

Run (project root):  python -m research.ipot_explore.audit [--db research/ipot_dev.sqlite]

Checks
  1 call log           status per variant, empty-day share
  2 coverage           visible top-10 value vs the day's traded value (OHLCV close*volume): how much of the market the top-10 actually shows
  3 internal checks    val / (lot*100) vs avg price; two-sidedness; sides listing < 10 brokers
  4 variant consistency F + D ~ all for brokers visible in all three variants
  5 broker universe    codes, presence, share of ticker-days, new/vanishing codes over time
  6 reconciliation     IPOT net vs the RapidAPI single-day broker_flow rows (when present in research/broker_flow_history.sqlite)
"""
from __future__ import annotations

import argparse
import sqlite3

import numpy as np
import pandas as pd

from research.ipot_explore import common, features


def main(db_path: str) -> None:
    raw = common.load_ipot(db_path)
    log = common.load_fetch_log(db_path)
    tickers = sorted(raw["ticker"].unique())
    px = common.load_ohlcv(tickers=tickers, start=str(raw["date"].min().date()))
    print(f"DB {db_path}: {len(raw):,} broker rows | {raw['ticker'].nunique()} tickers | {raw['date'].min().date()} .. {raw['date'].max().date()} "
          f"| {raw['date'].nunique()} trading days")

    print("\n== 1. fetch log (status per variant)")
    print(log.groupby(["fd", "board", "status"]).size().unstack(fill_value=0).to_string())
    allv = log[(log.fd == "all") & (log.board == "all")]
    print(f"all/all: {len(allv)} calls, empty share {(allv.status == 'empty').mean() * 100:.1f}% "
          f"(empty = holiday / no trades; the {allv[allv.status == 'empty'].date.nunique()} distinct empty dates)")

    a = features.broker_day_table(raw, "all", "all")
    g = a.groupby(["ticker", "date"])
    td = pd.DataFrame({"buy_vis": g["buy_val"].sum(), "sell_vis": g["sell_val"].sum(), "n_buy": g["buy_val"].count(), "n_sell": g["sell_val"].count(),
                       "cut_buy": g["cut_buy_val"].first(), "cut_sell": g["cut_sell_val"].first()}).reset_index()
    td = td.merge(px[["ticker", "date", "value"]], on=["ticker", "date"], how="left")
    td = td[td["value"] > 0].copy()                                   # days with no OHLCV volume have no denominator
    td["buy_cov"], td["sell_cov"] = td["buy_vis"] / td["value"], td["sell_vis"] / td["value"]

    print("\n== 2. coverage: visible top-10 value / traded value (OHLCV close*volume)")
    q = td[["buy_cov", "sell_cov"]].describe(percentiles=[.1, .25, .5, .75, .9]).T[["count", "mean", "10%", "25%", "50%", "75%", "90%"]]
    print(q.round(3).to_string())
    td["liq_bucket"] = pd.qcut(td["value"], 4, labels=["Q1 low", "Q2", "Q3", "Q4 high"], duplicates="drop")
    print("median buy coverage by traded-value quartile:", td.groupby("liq_bucket", observed=True)["buy_cov"].median().round(3).to_dict())
    print(f"ticker-days where the top-10 shows >=90% of the day's value (truncation negligible): {(td.buy_cov >= 0.9).mean() * 100:.1f}% buy / "
          f"{(td.sell_cov >= 0.9).mean() * 100:.1f}% sell | <40%: {(td.buy_cov < 0.4).mean() * 100:.1f}% buy")
    print(f"sides listing fewer than 10 brokers (nothing hidden): buy {(td.n_buy < 10).mean() * 100:.1f}%, sell {(td.n_sell < 10).mean() * 100:.1f}%")

    print("\n== 3. internal consistency")
    b = a.dropna(subset=["buy_val", "buy_lot", "buy_avg"])
    r = (b["buy_val"] / (b["buy_lot"] * 100.0)) / b["buy_avg"] - 1
    print(f"buy: |val/(lot*100)/avg - 1| median {r.abs().median() * 100:.3f}%  p99 {r.abs().quantile(.99) * 100:.2f}%  "
          f"(values are rounded to 0.1 M/B/T, so a few tenths of a percent is expected)")
    s = a.dropna(subset=["sell_val", "sell_lot", "sell_avg"])
    r2 = (s["sell_val"] / (s["sell_lot"] * 100.0)) / s["sell_avg"] - 1
    print(f"sell: median {r2.abs().median() * 100:.3f}%  p99 {r2.abs().quantile(.99) * 100:.2f}%")
    print(f"two-sided broker rows: {a['is_two'].mean() * 100:.1f}% | day close vs buy avg of #1 buyer: see features.buy_top1_avg_vs_close")
    ob = a.merge(px[["ticker", "date", "high", "low"]], on=["ticker", "date"], how="left")
    out_rng = ob[((ob["buy_avg"] > ob["high"] * 1.01) | (ob["buy_avg"] < ob["low"] * 0.99)) & ob["buy_avg"].notna() & ob["high"].notna()]
    print(f"buy avg outside the OHLCV day range (+-1%): {len(out_rng) / max(1, ob['buy_avg'].notna().sum()) * 100:.2f}% of rows "
          "(negotiated-board prints can legitimately sit outside the regular range)")

    print("\n== 4. variants: F + D vs all for a broker's buy value (brokers visible in all three)")
    p = raw[raw["board"] == "all"].pivot_table(index=["ticker", "date", "broker"], columns="fd", values="buy_val", aggfunc="sum")
    if {"F", "D", "all"} <= set(p.columns):
        both = p.dropna(subset=["F", "D", "all"])
        ratio = (both["F"] + both["D"]) / both["all"]
        ratio = ratio.replace([np.inf, -np.inf], np.nan).dropna()
        print(f"n={len(both):,} | (F+D)/all median {ratio.median():.3f}  p10 {ratio.quantile(.1):.3f}  p90 {ratio.quantile(.9):.3f}  "
              "(~1.0 expected; rounding and board differences explain small gaps)")
        only_f = p["F"].notna() & p["D"].isna() & p["all"].notna()
        print(f"brokers visible in all but only one of F/D: F-only {only_f.mean() * 100:.1f}% of all-visible rows -> the other investor type is below its own 10th place")
    else:
        print("F/D variants not both present")

    print("\n== 5. broker universe")
    pres = a.groupby("broker").agg(rows=("ticker", "size"), tickers=("ticker", "nunique"), days=("date", "nunique"), two=("is_two", "mean"))
    pres["cls"] = pres.index.map(common.BROKER_CLASS).fillna("other")
    print(f"{len(pres)} broker codes; top 12 by rows:")
    print(pres.sort_values("rows", ascending=False).head(12).round(2).to_string())
    first = a.groupby("broker")["date"].min()
    last = a.groupby("broker")["date"].max()
    d0, d1 = a["date"].min(), a["date"].max()
    print(f"codes first seen after {d0.date()}: {(first > d0).sum()} | last seen before {d1.date()}: {(last < d1).sum()} (churn of the code list)")

    print("\n== 6. reconciliation with RapidAPI single-day rows (broker_flow)")
    try:
        conn = sqlite3.connect(common.ROOT + "/research/broker_flow_history.sqlite")
        bf = pd.read_sql_query("SELECT broker_code AS broker, ticker, window_start AS d, net_value FROM broker_flow WHERE window_start = window_end", conn)
        conn.close()
        if bf.empty:
            print("no single-day RapidAPI rows in this copy of broker_flow_history.sqlite (they live on the VPS) -> run this on the VPS copy or copy the file")
        else:
            bf["date"] = pd.to_datetime(bf["d"]).astype("datetime64[ns]")
            m = a[a["is_two"]].merge(bf, on=["ticker", "date", "broker"], how="inner")
            m["ipot_net"] = m["buy_val"] - m["sell_val"]
            same_sign = (np.sign(m["ipot_net"]) == np.sign(m["net_value"])).mean()
            print(f"overlap n={len(m)} | same sign {same_sign * 100:.0f}% | median |ipot-rapid|/|rapid| = "
                  f"{((m['ipot_net'] - m['net_value']).abs() / m['net_value'].abs().replace(0, np.nan)).median():.2f}")
    except Exception as e:  # noqa: BLE001
        print("reconciliation skipped:", str(e)[:100])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=common.IPOT_DB)
    main(ap.parse_args().db)
