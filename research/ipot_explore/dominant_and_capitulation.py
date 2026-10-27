"""
research/ipot_explore/dominant_and_capitulation.py — two external ideas tested with a persistence design.

(1) "Follow the dominant broker of each stock": in the FIRST half of dates, per ticker pick the broker whose net buy (as share of that day's visible buy value) correlates best with the
    ticker's demeaned forward rel_5d; in the SECOND half measure that same broker's out-of-sample correlation. Null: the same procedure with a random broker per ticker (and the
    in-sample-best of a circularly shifted outcome), so selection bias is priced in.
(4) "Retail capitulation": buy when the retail-app class (XL, XC, YP, PD) is the net SELLER share extreme; compare class-level net share (buy share - sell share of visible value) quintiles
    with demeaned rel_5d/rel_10d, date-clustered, by 3 date blocks. Reported both for the 4-broker class and for XL alone.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features

DB = "research/ipot_broker_daily_vps_2026_10_22.sqlite"
RETAIL = ["XL", "XC", "YP", "PD"]


def main():
    raw = common.load_ipot(DB)
    feat = pd.read_pickle(common.OUT_DIR + "/features.pkl")
    o = feat[["ticker", "date", "rel_5d", "rel_10d"]].copy()
    for c in ("rel_5d", "rel_10d"):
        o[c] = o[c] - o.groupby("ticker")[c].transform("mean")
    a = features.broker_day_table(raw, "all", "all")
    a["net"] = a["net_val_mid"]
    vis = a.groupby(["ticker", "date"])["buy_val"].transform("sum")
    a["net_sh"] = a["net"] / vis.replace(0, np.nan)
    dates = np.sort(a["date"].unique()); half = dates[len(dates) // 2]; emb = dates[len(dates) // 2 + 10]
    rng = np.random.default_rng(7)

    # ---- (1) dominant broker per ticker
    ins, oos, rnd, shf = [], [], [], []
    for t, g in a.groupby("ticker"):
        y = o[o.ticker == t].set_index("date")["rel_5d"].dropna()
        if len(y) < 120:
            continue
        P = g.pivot_table(index="date", columns="broker", values="net_sh", aggfunc="sum").reindex(y.index).fillna(0.0)
        tr, te = P.index <= half, P.index >= emb
        if tr.sum() < 40 or te.sum() < 40:
            continue
        keep = (P[tr] != 0).sum() >= 30
        cols = P.columns[keep.to_numpy()]
        if len(cols) < 3:
            continue
        ctr = P.loc[tr, cols].corrwith(y[tr]); cte = P.loc[te, cols].corrwith(y[te])
        best = ctr.abs().idxmax()
        ins.append(ctr[best] * np.sign(ctr[best])); oos.append(cte[best] * np.sign(ctr[best]))
        rnd.append(cte[rng.choice(cols)] * np.sign(rng.choice([-1, 1])))
        ysh = pd.Series(np.roll(y.to_numpy(), rng.integers(10, len(y) - 10)), index=y.index)
        cs = P.loc[tr, cols].corrwith(ysh[tr]); bs = cs.abs().idxmax()
        shf.append(cte[bs] * np.sign(cs[bs]))
    ins, oos, rnd, shf = map(np.array, (ins, oos, rnd, shf))
    ok = np.isfinite(ins) & np.isfinite(oos) & np.isfinite(rnd) & np.isfinite(shf)
    ins, oos, rnd, shf = ins[ok], oos[ok], rnd[ok], shf[ok]
    se = lambda x: x.std(ddof=1) / np.sqrt(len(x))
    print(f"(1) dominant broker per ticker, {len(oos)} tickers (select on first half, test on second half after 10-day embargo)")
    print(f"    in-sample corr of selected broker: {ins.mean():+.3f}")
    print(f"    OUT-of-sample corr (sign kept from selection): {oos.mean():+.3f} (se {se(oos):.3f}, t {oos.mean() / se(oos):+.1f}); share of tickers positive {np.mean(oos > 0):.2f}")
    print(f"    random broker with random sign: {rnd.mean():+.3f} | selected on SHIFTED outcome: {shf.mean():+.3f}  (what selection-from-noise gives)")

    # ---- (4) retail capitulation (class net share of visible buy value)
    r = a[a["broker"].isin(RETAIL)].groupby(["ticker", "date"])["net_sh"].sum().rename("retail_net").reset_index()
    x = a[a["broker"] == "XL"].groupby(["ticker", "date"])["net_sh"].sum().rename("xl_net").reset_index()
    d = o.merge(r, on=["ticker", "date"], how="left").merge(x, on=["ticker", "date"], how="left").fillna({"retail_net": 0.0, "xl_net": 0.0})
    blk = pd.cut(d["date"].rank(method="dense"), 3, labels=["b1", "b2", "b3"])
    print("\n(4) retail-app class (XL/XC/YP/PD) and XL alone, net share of visible buy value, quintile of the feature -> mean demeaned rel (%), date-clustered t (Q1 = most net SELLING)")
    for col in ("retail_net", "xl_net"):
        d["q"] = pd.qcut(d[col].rank(method="first"), 5, labels=False)
        for h in ("rel_5d", "rel_10d"):
            row = []
            for q in range(5):
                s = d[d.q == q]; mu = s[h].mean(); cs = s.groupby("date")[h].apply(lambda v: float((v - mu).sum()))
                row.append(f"Q{q + 1} {100 * mu:+.2f}% (t {mu / (np.sqrt((cs ** 2).sum()) / len(s)):+.1f})")
            print(f"    {col:<10} {h:<7} " + " | ".join(row))
        for b in ("b1", "b2", "b3"):
            s = d[blk == b]; lo, hi = s[s.q == 0]["rel_10d"].mean(), s[s.q == 4]["rel_10d"].mean()
            print(f"       {b}: 10d Q1 {100 * lo:+.2f}% vs Q5 {100 * hi:+.2f}% (spread Q1-Q5 {100 * (lo - hi):+.2f}pp)")


if __name__ == "__main__":
    main()
