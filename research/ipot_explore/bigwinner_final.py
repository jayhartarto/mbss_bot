"""
research/ipot_explore/bigwinner_final.py — last robustness round for the right-tail signal (EXP-BIGWIN-1): CLEAN windows only, price-only GBM (the ablation had the better AUC), top 1% of the predicted probability per test quarter.
 A. de-duplication: one pick per ticker per 20 trading days (a single ARA run no longer counts many times)
 B. execution robustness slices (research slices, not a gate): close >= Rp200, 20d value >= Rp1bn, both; and dropping picks whose next open gaps >= +5%
 C. limit entry 2% / 3% under the close on the de-duplicated picks
 D. feature groups: permutation AUC drop on the test folds (momentum, trend/location, volume, volatility, size/price)
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.bigwinner_final
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from research.ipot_explore.bigwinner import build, PRICE_F, PURGE
from research.ipot_explore.entry_scenarios import simulate, COST

GROUPS = {"momentum": ["ret1", "r5", "r10", "r20", "r60", "rk_r20"], "trend/location": ["d20", "d50", "d100", "s20", "s50", "dd60", "pos60", "rsi", "rk_rsi"],
          "volume": ["vr", "vr5", "rk_vr"], "volatility": ["atr", "bw"], "size/price": ["lval", "lclose"]}


def main():
    px = build()
    g = px.groupby("ticker", observed=True)
    px["fmax"] = g["ret1"].transform(lambda s: s[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1))
    px["fmin"] = g["ret1"].transform(lambda s: s[::-1].rolling(20, min_periods=1).min()[::-1].shift(-1))
    px["gap_next"] = g["open"].shift(-1) / px["close"] - 1
    rows_all = np.where((px["elig"] & (px["nobs"] % 3 == 0)).to_numpy())[0]
    filled, ret = simulate(px, rows_all, None, 9.99, 0.12, 20, hmax=20)
    rows = rows_all[filled]
    D = px.iloc[rows].copy()
    D["pxrow"] = rows
    D["net"] = ret[filled] - COST
    D["ticker_s"] = D["ticker"].astype(str)
    D["BIG20"] = (D["net"] >= 0.20).astype(int)
    D["q"] = D["date"].dt.to_period("Q")
    D["susp"] = ((D["fmax"] >= 0.40) | (D["fmin"] <= -0.30) | (D["gap_next"].abs() >= 0.40)).astype(int)
    F = PRICE_F
    D[F] = D[F].replace([np.inf, -np.inf], np.nan)
    D = D.dropna(subset=F + ["gap_next", "fmax"])
    D = D[D.susp == 0].reset_index(drop=True)
    for col in F:
        lo, hi = D[col].quantile([0.005, 0.995])
        D[col] = D[col].clip(lo, hi)
    tdates = np.sort(D["date"].unique())
    parts, imps = [], []
    rng = np.random.default_rng(7)
    for q in sorted(D["q"].unique()):
        if q < pd.Period("2024Q3"):
            continue
        te = D["q"] == q
        cut = tdates[max(np.searchsorted(tdates, np.datetime64(D.loc[te, "date"].min())) - PURGE, 0)]
        tr = D["date"] < cut
        if tr.sum() < 15000 or te.sum() < 500:
            continue
        m = HistGradientBoostingClassifier(max_iter=250, max_depth=4, learning_rate=0.04, l2_regularization=1.0, random_state=7).fit(D.loc[tr, F].to_numpy(float), D.loc[tr, "BIG20"])
        T = D.loc[te].copy()
        Xt = T[F].to_numpy(float)
        T["p"] = m.predict_proba(Xt)[:, 1]
        parts.append(T)
        try:
            base_auc = roc_auc_score(T.BIG20, T.p)
            for gname, cols in GROUPS.items():
                Xp = Xt.copy()
                idx = [F.index(c) for c in cols if c in F]
                perm = rng.permutation(len(Xp))
                Xp[:, idx] = Xp[perm][:, idx]
                imps.append((gname, base_auc - roc_auc_score(T.BIG20, m.predict_proba(Xp)[:, 1])))
        except ValueError:
            pass
    T = pd.concat(parts)
    S = T[T["p"] >= T.groupby("q")["p"].transform(lambda s: s.quantile(0.99))].copy()
    base_net, base_big = T.net.mean(), T.BIG20.mean()
    print(f"CLEAN price-only GBM | test rows {len(T)} base BIG20 {100 * base_big:.1f}% net {100 * base_net:+.2f}% | AUC {roc_auc_score(T.BIG20, T.p):.3f}")

    def line(tag, X):
        if len(X) < 15:
            print(f"   {tag:<46} n {len(X)} (too few)")
            return
        print(f"   {tag:<46} n {len(X):>4} ({X.ticker_s.nunique():>3} tk) | BIG20 {100 * X.BIG20.mean():4.1f}% | net {100 * X.net.mean():+6.2f}% median {100 * X.net.median():+5.1f}% win {100 * (X.net > 0).mean():.0f}% | winsor95 {100 * X.net.clip(upper=X.net.quantile(.95)).mean():+5.2f}% | ex-best-5 {100 * X.net.sort_values(ascending=False).iloc[5:].mean():+5.2f}%")

    print("\nA. all top-1% picks vs de-duplicated (one per ticker per 20 trading days, earliest first)")
    line("all top-1% picks", S)
    S = S.sort_values(["ticker_s", "date"])
    keep, last = [], {}
    for idx, tk, dt in zip(S.index, S.ticker_s, S.date):
        if tk not in last or (dt - last[tk]).days >= 28:
            keep.append(idx)
            last[tk] = dt
    SD = S.loc[keep]
    line("de-duplicated", SD)
    print("\nB. execution robustness slices of the de-duplicated picks")
    line("close >= Rp200", SD[SD.close >= 200])
    line("20d value >= Rp1bn", SD[SD.val20 >= 1e9])
    line("close >= Rp200 and value >= Rp1bn", SD[(SD.close >= 200) & (SD.val20 >= 1e9)])
    line("drop next-open gap >= +5%", SD[SD.gap_next < 0.05])
    line("close >= Rp200, value >= 1bn, gap < +5%", SD[(SD.close >= 200) & (SD.val20 >= 1e9) & (SD.gap_next < 0.05)])
    print("\nC. 'buy cheap' limit entry on the de-duplicated picks (net, filled trades only)")
    for d in (0.02, 0.03):
        f2, r2 = simulate(px, SD["pxrow"].to_numpy(), d, 9.99, 0.12, 20, hmax=20)
        n2 = r2[f2] - COST
        print(f"   limit {100 * d:.0f}%: fill {100 * f2.mean():.0f}% (n {int(f2.sum())}) | net {100 * n2.mean():+.2f}% median {100 * np.median(n2):+.1f}% win {100 * (n2 > 0).mean():.0f}% | BIG20 {100 * (n2 >= .2).mean():.0f}%")
    print("\nD. de-duplicated picks by year / quarter")
    print("   " + " | ".join(f"{y}: n{len(g_)} {100 * g_.net.mean():+.2f}% (median {100 * g_.net.median():+.1f}%)" for y, g_ in SD.groupby(SD.date.dt.year)))
    print("   " + " ".join(f"{q}:{100 * g_.net.mean():+.1f}%(n{len(g_)})" for q, g_ in SD.groupby('q')))
    print("\nE. permutation importance (AUC drop, mean over test folds):")
    imp = pd.DataFrame(imps, columns=["group", "drop"]).groupby("group")["drop"].mean().sort_values(ascending=False)
    print("   " + " | ".join(f"{k} {v:+.3f}" for k, v in imp.items()))


if __name__ == "__main__":
    main()
