"""
research/ipot_explore/dominant_window_study.py — the "dominant net buyer over N days" idea: who is the biggest net buyer over 10 / 20 / 40 trading days, how big is its inventory,
where is its average buy price relative to the market, is it foreign or domestic, and does any of that tell the forward return?

Run (project root):  python -m research.ipot_explore.dominant_window_study
Dominant broker = argmax of the rolling sum of net_val_mid (features.rolling_pattern_features). Foreign/domestic = the broker's own foreign share of buy value (F variant / all variant)
over the whole sample: foreign if > 0.5. Outcome = ticker-demeaned rel_5d / rel_10d (%), date-clustered t. one-way = share of window days the dominant broker was a net buyer
(MM-like two-sided liquidity providers sit near 0.5, accumulators near 1).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features

DB = "research/ipot_broker_daily_vps_2026_10_22.sqlite"


def t_of(s: pd.DataFrame, col: str):
    n = len(s)
    if n < 100:
        return n, np.nan, np.nan
    mu = s[col].mean(); cs = s.groupby("date")[col].apply(lambda v: float((v - mu).sum()))
    return n, mu, mu / (np.sqrt((cs ** 2).sum()) / n)


def main():
    raw = common.load_ipot(DB)
    feat = pd.read_pickle(common.OUT_DIR + "/features.pkl")
    o = feat[["ticker", "date", "rel_5d", "rel_10d"]].copy()
    for c in ("rel_5d", "rel_10d"):
        o[c] = 100 * (o[c] - o.groupby("ticker")[c].transform("mean"))
    allv = raw[(raw.fd == "all") & (raw.board == "all")].groupby("broker")["buy_val"].sum()
    fv = raw[(raw.fd == "F") & (raw.board == "all")].groupby("broker")["buy_val"].sum()
    fshare = (fv / allv).fillna(0.0).clip(0, 1)
    print(f"brokers: {len(fshare)} | foreign (>0.5 of buy value is foreign flow): {int((fshare > .5).sum())} -> {', '.join(sorted(fshare[fshare > .5].index))}")
    px_all = common.load_ohlcv(start="2025-01-01")
    px = px_all[px_all.ticker.isin(raw.ticker.unique())]
    for W in (10, 20, 40):
        pat = features.rolling_pattern_features(raw, px, window=W).dropna(subset=["dom_broker", "dom_vwap_dist"])
        pat["type"] = np.where(pat["dom_broker"].map(fshare) > 0.5, "foreign", "domestic")
        d = pat.merge(o, on=["ticker", "date"], how="inner").dropna(subset=["rel_10d"])
        print(f"\n===== window {W} trading days | rows {len(d)} | foreign-dominant {np.mean(d.type == 'foreign'):.2f} =====")
        print(f"  dominant inventory share of window traded value: median {d.dom_inventory_share.median():.3f}, P90 {d.dom_inventory_share.quantile(.9):.3f} | "
              f"net-buyer days share median {d.dom_pos_days.median():.2f} | price vs its avg buy (close/vwap-1): P10 {d.dom_vwap_dist.quantile(.1):+.1%} median {d.dom_vwap_dist.median():+.1%} P90 {d.dom_vwap_dist.quantile(.9):+.1%}")
        print("  A) dominant TYPE x inventory strength (top third of dom_inventory_share) -> rel_5d / rel_10d %  [n, mean, t]")
        thr = d.dom_inventory_share.quantile(2 / 3)
        for ty in ("foreign", "domestic"):
            for lab, m in (("all", d.type == ty), ("strong inventory", (d.type == ty) & (d.dom_inventory_share >= thr)), ("strong & one-way(>=.7)", (d.type == ty) & (d.dom_inventory_share >= thr) & (d.dom_pos_days >= .7))):
                s = d[m]; a, b = t_of(s, "rel_5d"), t_of(s, "rel_10d")
                print(f"     {ty:<8} {lab:<24} n={a[0]:>6} | 5d {a[1]:+.2f} (t {a[2]:+.1f}) | 10d {b[1]:+.2f} (t {b[2]:+.1f})")
        print("  B) price vs dominant's average buy price (close/vwap-1), within strong-inventory rows")
        s = d[d.dom_inventory_share >= thr]
        bins = [-1, -.05, 0, .05, .15, 9]
        s = s.assign(bin=pd.cut(s.dom_vwap_dist, bins, labels=["<-5%", "-5..0", "0..5", "5..15", ">15%"]))
        for ty in ("foreign", "domestic"):
            row = []
            for bn, g in s[s.type == ty].groupby("bin", observed=True):
                b = t_of(g, "rel_10d"); row.append(f"{bn}: n{b[0]} {b[1]:+.2f}(t{b[2]:+.1f})" if b[0] >= 100 else f"{bn}: n{b[0]} -")
            print(f"     {ty:<8} 10d | " + " | ".join(row))


if __name__ == "__main__":
    main()
