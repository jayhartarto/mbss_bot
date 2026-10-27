"""
research/ipot_explore/xl_lane_filter.py — Path 1 test: do lane trades whose signal day had XL as the #1 net buyer do worse than the other trades of the same lane?

Run (project root):  python -m research.ipot_explore.xl_lane_filter [--db research/ipot_broker_daily_vps_2026_10_22.sqlite]
Trades: research/.hsc_out/wf/pooled_flagged.pkl (ret = realised % after the lane's TP/SL mechanics). Only trades whose signal date is inside the IPOT coverage and whose ticker
has IPOT rows are used (the control group is the SAME lane, same coverage). XL flag at the signal date t (known after the close) and at t-1 (what a close-entry lane like BSJP could
have seen). Differences are flagged minus rest, ticker-clustered t; pooled estimate has lane fixed effects.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from research.ipot_explore import common, features


def xl_flags(raw: pd.DataFrame) -> pd.DataFrame:
    a = features.broker_day_table(raw, "all", "all")
    out = []
    for side, sign in (("buy", 1.0), ("sell", -1.0)):
        s = a[(sign * a["net_val_mid"]) > 0].copy()
        s["rk"] = s.groupby(["ticker", "date"])["net_val_mid"].rank(ascending=(side == "sell"), method="first")
        out.append(s[(s["rk"] == 1) & (s["broker"] == "XL")][["ticker", "date"]].assign(**{f"xl_top_{side}": 1}))
    f = out[0].merge(out[1], on=["ticker", "date"], how="outer").fillna(0)
    return f


def diff(g: pd.DataFrame, flag: str):
    a, b = g[g[flag] == 1], g[g[flag] == 0]
    if len(a) < 15 or len(b) < 15:
        return len(a), len(b), np.nan, np.nan, np.nan, np.nan
    d = a["ret"].mean() - b["ret"].mean()
    # ticker-clustered SE of the difference via the regression ret ~ flag
    x = (g[flag] - g[flag].mean()).to_numpy(); y = (g["ret"] - g["ret"].mean()).to_numpy()
    beta = (x * y).sum() / (x * x).sum(); e = y - beta * x
    cs = pd.Series(x * e).groupby(g["ticker"].to_numpy()).sum()
    se = np.sqrt((cs ** 2).sum()) / (x * x).sum()
    return len(a), len(b), d, d / se, 100 * ((a["ret"] > 0).mean() - (b["ret"] > 0).mean()), a["ret"].mean()


def main(db: str):
    raw = common.load_ipot(db)
    tr = pd.read_pickle("research/.hsc_out/wf/pooled_flagged.pkl")[["ticker", "date", "ret", "lane"]]
    f = xl_flags(raw)
    cov = raw.groupby("ticker")["date"].agg(["min", "max"]).reset_index()
    tr = tr.merge(cov, on="ticker", how="inner")
    tr = tr[(tr["date"] >= tr["min"]) & (tr["date"] <= tr["max"])].drop(columns=["min", "max"])
    # previous available IPOT day for t-1 (trading-day aware)
    days = np.sort(raw["date"].unique())
    prev = dict(zip(days[1:], days[:-1]))
    tr["date_prev"] = tr["date"].map(prev)
    tr = tr.merge(f.rename(columns={"xl_top_buy": "xl_t", "xl_top_sell": "xl_sell_t"}), on=["ticker", "date"], how="left")
    tr = tr.merge(f.rename(columns={"date": "date_prev", "xl_top_buy": "xl_t1", "xl_top_sell": "xl_sell_t1"}), on=["ticker", "date_prev"], how="left")
    for c in ("xl_t", "xl_t1", "xl_sell_t", "xl_sell_t1"):
        tr[c] = tr[c].fillna(0)
    print(f"trades inside IPOT coverage: {len(tr)} over {tr['ticker'].nunique()} tickers; share with XL top buyer at t: {tr['xl_t'].mean():.3f}, at t-1: {tr['xl_t1'].mean():.3f}")
    print("\nlane | flag | n_flag | n_rest | mean_ret(flag - rest, pp) | t(ticker-clust) | win%(flag-rest, pp) | flagged mean ret")
    for flag in ("xl_t", "xl_t1", "xl_sell_t"):
        for lane, g in list(tr.groupby("lane")) + [("ALL (lane FE)", tr.assign(ret=tr["ret"] - tr.groupby("lane")["ret"].transform("mean")))]:
            na, nb, d, t, dw, ma = diff(g, flag)
            if np.isnan(d):
                print(f"{lane:<14} | {flag:<9} | {na:>5} | {nb:>5} | too few")
            else:
                print(f"{lane:<14} | {flag:<9} | {na:>5} | {nb:>5} | {d:+6.2f} | {t:+5.1f} | {dw:+5.1f} | {ma:+.2f}")
    print("\nby block (3 date terciles), pooled with lane FE, flag xl_t:")
    g = tr.assign(ret=tr["ret"] - tr.groupby("lane")["ret"].transform("mean"))
    dts = np.sort(g["date"].unique()); k = len(dts) // 3
    for lab, m in (("block1", g["date"] <= dts[k]), ("block2", (g["date"] > dts[k]) & (g["date"] <= dts[2 * k])), ("block3", g["date"] > dts[2 * k])):
        na, nb, d, t, dw, ma = diff(g[m], "xl_t")
        print(f"  {lab}: n_flag {na}, n_rest {nb}, diff {d:+.2f}pp, t {t:+.1f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=common.IPOT_DB)
    main(ap.parse_args().db)
