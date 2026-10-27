"""
research/ipot_explore/bigwinner_broker.py — EXP-BIGWIN-5 (registered before running): do brokers / broker groups 'follow' the rare winners, and do they improve the precision of the price-only right-tail model?
Rows: the out-of-sample test rows of the price-only walk-forward GBM (bigwinner_risk.get_picks, price >= Rp30, test quarters 2024Q3-2026Q3) that also have IPOT broker coverage for the 20-day window (>= 70% of the days).
Label BIG20 = net >= +20% (stop 12%, hold 20 days, entry open t+1). Price score = logit of the OOS price-model probability.
Broker features at the close of t: net buying share (net_val_mid / traded value over the window) for each of 17 brokers and 5 groups, windows 1, 5 and 20 days.
 A. Linear-probability regressions BIG20 ~ standardised broker share + price score (+ log value, ATR), date fixed effects, ticker-clustered t; 17 brokers x 3 windows + 5 groups x 3 windows = 66 tests, Benjamini-Hochberg.
 B. Inside the candidate set (top 5% of the price score per quarter): BIG20 rate by tercile of each GROUP share (5-day and 20-day), and the regression within the candidate set.
 C. Ablation: logistic regression with the price score only vs price score + the 5 group shares (5d and 20d) on a chronological split of the covered rows: AUC and top-decile precision.
Groups: FOR_INST AK BK KZ RX GA | FOR_RETAIL ZP YU | GOV CC OD NI DX | SCALPER MG CP | RETAIL_APP XL XC YP PD.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.bigwinner_broker
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from research.ipot_explore import common
from research.ipot_explore import broker_accum_windows as bw
from research.ipot_explore.bigwinner_risk import get_picks
from research.ipot_explore.breakout_backing import bh
from research.ipot_explore.mover_footprint import TOP, OUT

GROUPS = {"FOR_INST": ["AK", "BK", "KZ", "RX", "GA"], "FOR_RETAIL": ["ZP", "YU"], "GOV": ["CC", "OD", "NI", "DX"], "SCALPER": ["MG", "CP"], "RETAIL_APP": ["XL", "XC", "YP", "PD"]}


def norm_p(t):
    return math.erfc(abs(t) / math.sqrt(2))


def main():
    px, D, T = get_picks()
    T = T.copy()
    T["ticker"] = T["ticker_s"]
    raw = pd.concat([common.load_ipot(TOP), common.load_ipot(OUT)], ignore_index=True)
    px_all = common.load_ohlcv(start="2025-01-01")
    sub = px_all[px_all.ticker.isin(raw.ticker.unique())]
    bw.WINDOWS = (1, 5, 20)
    S = bw.window_shares(raw, sub)
    W = S.pivot_table(index=["ticker", "date"], columns=["broker", "W"], values="share", aggfunc="first")
    W.columns = [f"{b}_{w}" for b, w in W.columns]
    W = W.reset_index()
    M = T.merge(W, on=["ticker", "date"], how="inner")
    cov = M[[f"{b}_20" for b in bw.BROKERS if f"{b}_20" in M.columns]].notna().mean(axis=1)
    M = M[cov >= 0.5].copy()
    for g, bl in GROUPS.items():
        for w in (1, 5, 20):
            cols = [f"{b}_{w}" for b in bl if f"{b}_{w}" in M.columns]
            M[f"{g}_{w}"] = M[cols].fillna(0.0).sum(axis=1)
    M["pz"] = np.log(M["p"].clip(1e-4, 1 - 1e-4) / (1 - M["p"].clip(1e-4, 1 - 1e-4)))
    M["lval"] = np.log(M["val20"].clip(lower=1))
    print(f"covered OOS rows {len(M)} ({M.ticker.nunique()} tickers, {M.date.min().date()}..{M.date.max().date()}) | BIG20 base {100 * M.BIG20.mean():.1f}% ({int(M.BIG20.sum())} events) | price-score AUC on these rows {roc_auc_score(M.BIG20, M.pz):.3f}")
    feats = [c for c in M.columns if any(c == f"{b}_{w}" for b in bw.BROKERS for w in (1, 5, 20))] + [f"{g}_{w}" for g in GROUPS for w in (1, 5, 20)]

    def lpm(df, xcol, extra=("pz", "lval", "atr")):
        d = df.dropna(subset=[xcol, "BIG20"] + list(extra)).copy()
        x = d[xcol].astype(float)
        lo, hi = x.quantile([0.01, 0.99])
        x = x.clip(lo, hi)
        sd = x.std()
        if sd == 0 or len(d) < 300:
            return None
        cols = pd.DataFrame({"x": (x - x.mean()) / sd, **{e: d[e].astype(float) for e in extra}})
        cols = cols - cols.groupby(d["date"].to_numpy()).transform("mean")
        y = d["BIG20"].astype(float) - d.groupby("date")["BIG20"].transform("mean")
        A = cols.to_numpy()
        XtXi = np.linalg.inv(A.T @ A)
        b = XtXi @ A.T @ y.to_numpy()
        e = y.to_numpy() - A @ b
        S_ = np.zeros((A.shape[1],) * 2)
        for _, idx in pd.Series(np.arange(len(y))).groupby(d["ticker"].to_numpy()):
            sv = A[idx.to_numpy()].T @ e[idx.to_numpy()]
            S_ += np.outer(sv, sv)
        V = XtXi @ S_ @ XtXi
        return 100 * b[0], b[0] / np.sqrt(V[0, 0]), len(d)

    rows = []
    for c in feats:
        r = lpm(M, c)
        if r:
            rows.append(dict(feature=c, coef=r[0], t=r[1], n=r[2]))
    G = pd.DataFrame(rows)
    G["p"] = G["t"].map(norm_p)
    G["bh"] = bh(G["p"].to_numpy())
    pd.set_option("display.width", 200)
    print(f"\nA. FULL covered sample, control price score: {len(G)} tests | nominal |t|>=2: {int((G.t.abs() >= 2).sum())} (chance ~{0.05 * len(G):.0f}) | |t|>=3: {int((G.t.abs() >= 3).sum())} | BH survivors: {int(G.bh.sum())}")
    print("   coef = change in P(BIG20) in percentage points per +1 sd of the broker net-buying share")
    print(G.assign(a=G.t.abs()).sort_values("a", ascending=False).head(14)[["feature", "coef", "t", "bh"]].round(3).to_string(index=False))
    print("\n   group-level results:")
    print(G[G.feature.str.contains("|".join(GROUPS))].sort_values("feature")[["feature", "coef", "t"]].round(3).to_string(index=False))

    thr = M.groupby("q")["p"].transform(lambda s: s.quantile(0.95))
    Cn = M[M["p"] >= thr].copy()
    print(f"\nB. CANDIDATE SET (top 5% price score per quarter, within the covered rows): n {len(Cn)}, BIG20 rate {100 * Cn.BIG20.mean():.1f}% (all covered rows {100 * M.BIG20.mean():.1f}%), mean net {100 * Cn.net.mean():+.2f}%")
    print(f"{'feature':<16}{'coef pp/sd':>11}{'t':>7} | BIG20 rate by tercile of the share (low / mid / high)  | mean net by tercile")
    for g in GROUPS:
        for w in (5, 20):
            c = f"{g}_{w}"
            r = lpm(Cn, c, extra=("pz",))
            tt = pd.qcut(Cn[c].rank(method="first"), 3, labels=False)
            rate = [100 * Cn.loc[tt == k, "BIG20"].mean() for k in range(3)]
            net = [100 * Cn.loc[tt == k, "net"].mean() for k in range(3)]
            print(f"{c:<16}{(r[0] if r else np.nan):>11.2f}{(r[1] if r else np.nan):>7.1f} | {rate[0]:5.1f}% / {rate[1]:5.1f}% / {rate[2]:5.1f}%  | {net[0]:+6.2f}% / {net[1]:+6.2f}% / {net[2]:+6.2f}%")

    print("\nC. ABLATION on a chronological split of the covered rows (train first 50% of dates, test the rest):")
    dts = np.sort(M["date"].unique())
    cut = dts[len(dts) // 2]
    tr, te = M[M.date <= cut], M[M.date > cut]
    gcols = [f"{g}_{w}" for g in GROUPS for w in (5, 20)]
    for label, cols in (("price score only", ["pz"]), ("price score + 5 group shares (5d,20d)", ["pz"] + gcols), ("price score + XL/XC/YP/PD only", ["pz", "RETAIL_APP_5", "RETAIL_APP_20"])):
        sc = StandardScaler().fit(tr[cols].fillna(0.0))
        m = LogisticRegression(C=0.2, max_iter=500).fit(sc.transform(tr[cols].fillna(0.0)), tr["BIG20"])
        p = m.predict_proba(sc.transform(te[cols].fillna(0.0)))[:, 1]
        top = te.assign(pp=p).nlargest(max(int(0.1 * len(te)), 1), "pp")
        print(f"   {label:<42} AUC {roc_auc_score(te.BIG20, p):.3f} | top-10% precision {100 * top.BIG20.mean():4.1f}% (n {len(top)}) vs base {100 * te.BIG20.mean():.1f}% | net {100 * top.net.mean():+.2f}%")


if __name__ == "__main__":
    main()
