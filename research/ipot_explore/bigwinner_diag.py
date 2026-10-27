"""
research/ipot_explore/bigwinner_diag.py — is the right-tail signal of bigwinner.py real and tradable?  Same population / walk-forward GBM (PRICE+MARKET), top 1% of predicted probability per test quarter, then:
 (1) what the picks look like (gap at the next open, price, liquidity, ATR, tickers) and how concentrated the P&L is,
 (2) outlier sensitivity (winsorised / trimmed means, mean without the best 10 picks),
 (3) execution realism: drop picks that open more than +5% / +8% above the close (cannot be bought at that open), add 1% slippage, and the 'buy cheap' version: limit order 2% / 3% under the close (filled only if the next day trades there),
 (4) by year, and with extra costs.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.bigwinner_diag
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from research.ipot_explore.bigwinner import build, PRICE_F, MARKET_F, PURGE
from research.ipot_explore.entry_scenarios import simulate, COST


def main():
    px = build()
    px["gap_next"] = px.groupby("ticker", observed=True)["open"].shift(-1) / px["close"] - 1
    rows_all = np.where((px["elig"] & (px["nobs"] % 3 == 0)).to_numpy())[0]
    filled, ret = simulate(px, rows_all, None, 9.99, 0.12, 20, hmax=20)
    rows = rows_all[filled]
    D = px.iloc[rows].copy()
    D["pxrow"] = rows
    D["net"] = ret[filled] - COST
    D["ticker_s"] = D["ticker"].astype(str)
    D["BIG20"] = (D["net"] >= 0.20).astype(int)
    D["q"] = D["date"].dt.to_period("Q")
    F = PRICE_F + MARKET_F
    D[F] = D[F].replace([np.inf, -np.inf], np.nan)
    D = D.dropna(subset=F + ["gap_next"]).reset_index(drop=True)
    for col in F:
        lo, hi = D[col].quantile([0.005, 0.995])
        D[col] = D[col].clip(lo, hi)
    tdates = np.sort(D["date"].unique())
    parts = []
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
        T["p"] = m.predict_proba(T[F].to_numpy(float))[:, 1]
        parts.append(T)
    T = pd.concat(parts)
    S = T[T["p"] >= T.groupby("q")["p"].transform(lambda s: s.quantile(0.99))].copy()
    B = T
    print(f"test rows {len(T)} | top-1% picks {len(S)} ({S.ticker_s.nunique()} tickers) | base net {100 * B.net.mean():+.2f}% | picks net {100 * S.net.mean():+.2f}% median {100 * S.net.median():+.1f}%")
    print("\n(1) WHAT THE PICKS LOOK LIKE (picks vs all test rows):")
    for lab, col, scale, unit in (("gap at next open", "gap_next", 100, "%"), ("close price", "close", 1, ""), ("20d value traded (bn IDR)", "val20", 1e-9, ""), ("ATR%", "atr", 100, "%")):
        print(f"   {lab:<26} picks median {S[col].median() * scale:8.2f}{unit} (P25 {S[col].quantile(.25) * scale:.2f}, P75 {S[col].quantile(.75) * scale:.2f}) | all rows median {B[col].median() * scale:8.2f}{unit}")
    print(f"   share of picks that open >= +5% above the close: {100 * (S.gap_next >= 0.05).mean():.0f}%  (all rows {100 * (B.gap_next >= 0.05).mean():.0f}%) | >= +8%: {100 * (S.gap_next >= 0.08).mean():.0f}% ({100 * (B.gap_next >= 0.08).mean():.0f}%)")
    byt = S.groupby("ticker_s")["net"].agg(["sum", "count"]).sort_values("sum", ascending=False)
    print(f"   top 5 tickers carry {100 * byt['sum'].head(5).sum() / byt['sum'].sum():.0f}% of the P&L (most picks from one ticker: {int(byt['count'].max())} of {len(S)}); picks per ticker median {int(byt['count'].median())}")
    print("\n(2) OUTLIER SENSITIVITY (net %):")
    w = S.net.clip(upper=S.net.quantile(0.95))
    srt = S.net.sort_values(ascending=False)
    print(f"   mean {100 * S.net.mean():+.2f} | winsorised at P95 {100 * w.mean():+.2f} | trimmed mean (5%/5%) {100 * S.net.clip(S.net.quantile(.05), S.net.quantile(.95)).mean():+.2f} | without the best 10 picks {100 * srt.iloc[10:].mean():+.2f} | best pick {100 * srt.iloc[0]:+.0f}% | share of picks >= +20%: {100 * (S.net >= .20).mean():.0f}%")
    print("\n(3) EXECUTION REALISM:")
    for thr in (0.05, 0.08):
        x = S[S.gap_next < thr]
        print(f"   drop picks opening >= +{100 * thr:.0f}% above the close: n {len(x)} | net {100 * x.net.mean():+.2f}% median {100 * x.net.median():+.1f}% win {100 * (x.net > 0).mean():.0f}% | with an extra 1% slippage {100 * (x.net.mean() - 0.01):+.2f}%")
    for d in (0.02, 0.03):
        f2, r2 = simulate(px, S["pxrow"].to_numpy(), d, 9.99, 0.12, 20, hmax=20)
        n2 = r2[f2] - COST
        print(f"   limit {100 * d:.0f}% under the close: fill {100 * f2.mean():.0f}% | net {100 * n2.mean():+.2f}% median {100 * np.median(n2):+.1f}% win {100 * (n2 > 0).mean():.0f}% (n {int(f2.sum())})")
    print("\n(4) BY YEAR (top-1% picks vs all test rows, net mean %) and cost sensitivity:")
    for y, g in S.groupby(S.date.dt.year):
        print(f"   {y}: picks {len(g):>3} net {100 * g.net.mean():+.2f}% median {100 * g.net.median():+.1f}% BIG20 {100 * g.BIG20.mean():.0f}% | all rows {100 * B[B.date.dt.year == y].net.mean():+.2f}%")
    for c_extra in (0.0, 0.01, 0.02):
        print(f"   extra cost {100 * c_extra:.0f}% -> picks net {100 * (S.net.mean() - c_extra):+.2f}%")


if __name__ == "__main__":
    main()
