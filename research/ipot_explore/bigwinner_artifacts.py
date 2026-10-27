"""
research/ipot_explore/bigwinner_artifacts.py — are the 'big winners' of bigwinner.py real, or corporate-action / data artefacts?
Step 1: list the biggest winners among the top-1% picks with their daily path (largest one-day close-to-close jumps in the 20-day window, volume, price).
Step 2: flag a trade as suspicious when ANY one-day close-to-close move inside the 20-day window (or on the entry gap) is >= +40% or <= -30% (the IDX auto-rejection limits are 20-35% for normal days; a bigger move means a split / reverse split / relisting / bad tick).
Step 3: rerun the walk-forward with suspicious windows removed from BOTH train and test, report precision / net / by year / by quarter, and the dependence on low prices.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.bigwinner_artifacts
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from research.ipot_explore.bigwinner import build, PRICE_F, MARKET_F, PURGE
from research.ipot_explore.entry_scenarios import simulate, COST


def main():
    px = build()
    g = px.groupby("ticker", observed=True)
    r1 = px["ret1"]
    # forward window t+1..t+20 of the one-day returns (max and min)
    fmax = g["ret1"].transform(lambda s: s[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1))
    fmin = g["ret1"].transform(lambda s: s[::-1].rolling(20, min_periods=1).min()[::-1].shift(-1))
    px["fmax"], px["fmin"] = fmax, fmin
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
    F = PRICE_F + MARKET_F
    D[F] = D[F].replace([np.inf, -np.inf], np.nan)
    D = D.dropna(subset=F + ["gap_next", "fmax"]).reset_index(drop=True)
    print(f"population {len(D)}; suspicious windows (one-day move >= +40% or <= -30% in t+1..t+20, or a >= 40% entry gap): {int(D.susp.sum())} ({100 * D.susp.mean():.2f}%)")
    big = D[D.BIG20 == 1]
    print(f"   of BIG20 winners {len(big)}: {int(big.susp.sum())} are suspicious ({100 * big.susp.mean():.1f}%); mean net of the suspicious ones {100 * big[big.susp == 1].net.mean():+.0f}% vs clean {100 * big[big.susp == 0].net.mean():+.0f}%")
    top = D.sort_values("net", ascending=False).head(15)
    print("\nthe 15 biggest outcomes in the whole population (net %, close, max one-day jump in the window, gap, suspicious?):")
    for r in top.itertuples():
        print(f"   {r.ticker_s:<6} {r.date.date()} net {100 * r.net:+7.0f}% close {r.close:>7.0f} | max 1-day {100 * r.fmax:+5.0f}% min 1-day {100 * r.fmin:+5.0f}% | gap {100 * r.gap_next:+5.0f}% | susp {r.susp}")
    for col in F:
        lo, hi = D[col].quantile([0.005, 0.995])
        D[col] = D[col].clip(lo, hi)
    tdates = np.sort(D["date"].unique())

    def walk(Dsub, tag):
        parts = []
        for q in sorted(Dsub["q"].unique()):
            if q < pd.Period("2024Q3"):
                continue
            te = Dsub["q"] == q
            cut = tdates[max(np.searchsorted(tdates, np.datetime64(Dsub.loc[te, "date"].min())) - PURGE, 0)]
            tr = Dsub["date"] < cut
            if tr.sum() < 15000 or te.sum() < 500:
                continue
            m = HistGradientBoostingClassifier(max_iter=250, max_depth=4, learning_rate=0.04, l2_regularization=1.0, random_state=7).fit(Dsub.loc[tr, F].to_numpy(float), Dsub.loc[tr, "BIG20"])
            T = Dsub.loc[te].copy()
            T["p"] = m.predict_proba(T[F].to_numpy(float))[:, 1]
            parts.append(T)
        T = pd.concat(parts)
        S = T[T["p"] >= T.groupby("q")["p"].transform(lambda s: s.quantile(0.99))]
        print(f"\n=== {tag}: test rows {len(T)}, base BIG20 {100 * T.BIG20.mean():.1f}%, base net {100 * T.net.mean():+.2f}% | AUC {roc_auc_score(T.BIG20, T.p):.3f}")
        print(f"   top 1% picks {len(S)} ({S.ticker_s.nunique()} tickers): BIG20 {100 * S.BIG20.mean():.1f}% (lift {S.BIG20.mean() / T.BIG20.mean():.1f}x) | net mean {100 * S.net.mean():+.2f}% median {100 * S.net.median():+.1f}% win {100 * (S.net > 0).mean():.0f}% | winsorised P95 {100 * S.net.clip(upper=S.net.quantile(.95)).mean():+.2f}% | without best 10: {100 * S.net.sort_values(ascending=False).iloc[10:].mean():+.2f}%")
        print(f"   picks' median close {S.close.median():.0f} vs all {T.close.median():.0f} | share of picks with close < 100: {100 * (S.close < 100).mean():.0f}% (all rows {100 * (T.close < 100).mean():.0f}%) | suspicious picks {int(S.susp.sum())}")
        print("   by year: " + " | ".join(f"{y}: n{len(g_)} {100 * g_.net.mean():+.2f}% (median {100 * g_.net.median():+.1f}%, base {100 * T[T.date.dt.year == y].net.mean():+.2f}%)" for y, g_ in S.groupby(S.date.dt.year)))
        print("   by quarter: " + " ".join(f"{q}:{100 * g_.net.mean():+.1f}%" for q, g_ in S.groupby('q')))
        return T, S

    walk(D, "ALL windows (as before)")
    clean = D[D.susp == 0].reset_index(drop=True)
    T, S = walk(clean, "CLEAN windows (suspicious removed from train and test)")
    # limit-entry version on the clean picks
    for d in (0.02, 0.03):
        f2, r2 = simulate(px, S["pxrow"].to_numpy(), d, 9.99, 0.12, 20, hmax=20)
        n2 = r2[f2] - COST
        print(f"   CLEAN picks, limit {100 * d:.0f}% under the close: fill {100 * f2.mean():.0f}% | net {100 * n2.mean():+.2f}% median {100 * np.median(n2):+.1f}% win {100 * (n2 > 0).mean():.0f}%")


if __name__ == "__main__":
    main()
