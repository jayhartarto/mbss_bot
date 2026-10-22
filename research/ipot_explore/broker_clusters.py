"""
research/ipot_explore/broker_clusters.py — Engine 3 of the IPOT research framework: behaviour-based broker clusters (replaces the provisional static classes in common.BROKER_CLASS).

Run (project root):  python -m research.ipot_explore.broker_clusters [--db research/ipot_dev.sqlite] [--min-rows 150] [--boot 30]

Per broker, pooled over tickers (rows = ticker-days where the broker is in the top-10 of the all/all variant):
  gross_share   mean (buy + sell) / (2 * traded value)                  size in the stocks it trades
  n_tickers     breadth
  two_frac      share of rows on both sides
  net_gross     median |net| / (buy + sell)                             ~0 churner, ~1 one-way
  spread        median sell_avg / buy_avg - 1 on two-sided rows
  corr_same     corr(net share, return of the same day)                 negative = buys falls / sells rallies (contrarian), positive = chases
  corr_next     corr(net share, return of the next day)
  buy_pos / sell_pos   average position of its buy / sell price inside the day's range (0 = low, 1 = high)
  foreign_share share of its buy value that is foreign-investor flow (F variant / all variant)
  autocorr      mean lag-1 autocorrelation of its daily net share inside a ticker (persistence of accumulation)
Clusters: KMeans on robust-standardised profiles, k chosen by silhouette among 2..5, then a ticker-bootstrap stability check (co-assignment frequency of every pair).
With only a few dozen brokers the clusters are descriptive; the stability number says how much to trust the grouping. Names are NOT auto-assigned.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from research.ipot_explore import common, features

PROFILE_COLS = ["gross_share", "n_tickers", "two_frac", "net_gross", "spread", "corr_same", "corr_next", "buy_pos", "sell_pos", "foreign_share", "autocorr"]


def _prepare(raw: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    p = px.sort_values(["ticker", "date"]).copy()
    p["ret0"] = p.groupby("ticker")["close"].pct_change()
    p["ret1"] = p.groupby("ticker")["ret0"].shift(-1)
    a = features.broker_day_table(raw, "all", "all").merge(p[["ticker", "date", "value", "high", "low", "ret0", "ret1"]], on=["ticker", "date"], how="left")
    a = a[a["value"] > 0].copy()
    a["net_share"] = a["net_val_mid"] / a["value"]
    a["gross_share"] = (a["buy_val"].fillna(0) + a["sell_val"].fillna(0)) / (2 * a["value"])
    a["gross_val"] = a["buy_val"].fillna(0) + a["sell_val"].fillna(0)
    a["net_gross"] = a["net_val_mid"].abs() / a["gross_val"].replace(0, np.nan)
    a["spread"] = np.where(a["is_two"], a["sell_avg"] / a["buy_avg"] - 1, np.nan)
    rng = (a["high"] - a["low"]).replace(0, np.nan)
    a["buy_pos"] = (a["buy_avg"] - a["low"]) / rng
    a["sell_pos"] = (a["sell_avg"] - a["low"]) / rng
    return a


def broker_profiles(raw: pd.DataFrame, px: pd.DataFrame, min_rows: int = 150, tickers: list | None = None) -> pd.DataFrame:
    a = _prepare(raw, px)
    if tickers is not None:
        a = a[a["ticker"].isin(set(tickers))]
    f = features.broker_day_table(raw, "F", "all")
    if tickers is not None and not f.empty:
        f = f[f["ticker"].isin(set(tickers))]
    fsum = f.groupby("broker")["buy_val"].sum() if not f.empty else pd.Series(dtype=float)
    asum = a.groupby("broker")["buy_val"].sum()
    # lag-1 autocorrelation of the net share inside each ticker, averaged over tickers with >= 10 observations
    ac = {}
    for (b, tk), g in a.sort_values("date").groupby(["broker", "ticker"]):
        if len(g) >= 10:
            ac.setdefault(b, []).append(g["net_share"].autocorr(1))
    rows = {}
    for b, g in a.groupby("broker"):
        if len(g) < min_rows:
            continue
        gg, g1 = g.dropna(subset=["ret0", "net_share"]), g.dropna(subset=["ret1", "net_share"])
        rows[b] = {
            "n_rows": len(g), "gross_share": g["gross_share"].mean(), "n_tickers": g["ticker"].nunique(), "two_frac": g["is_two"].mean(),
            "net_gross": g["net_gross"].median(), "spread": g["spread"].median(), "corr_same": gg["net_share"].corr(gg["ret0"]),
            "corr_next": g1["net_share"].corr(g1["ret1"]), "buy_pos": g["buy_pos"].mean(), "sell_pos": g["sell_pos"].mean(),
            "foreign_share": (fsum.get(b, 0.0) / asum.get(b, np.nan)) if asum.get(b, 0) else np.nan,
            "autocorr": float(np.nanmean(ac.get(b, [np.nan]))),
        }
    return pd.DataFrame(rows).T.astype(float)


def _standardise(P: pd.DataFrame) -> pd.DataFrame:
    X = P[PROFILE_COLS].copy()
    X = X.fillna(X.median())
    med, mad = X.median(), (X - X.median()).abs().median().replace(0, np.nan)
    return ((X - med) / (1.4826 * mad)).clip(-3, 3).fillna(0.0)


def choose_k(X: pd.DataFrame, ks=(2, 3, 4, 5), seed: int = 0):
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    best = None
    for k in ks:
        if k >= len(X):
            continue
        lab = KMeans(n_clusters=k, n_init=20, random_state=seed).fit_predict(X)
        if len(set(lab)) < 2:
            continue
        sc = silhouette_score(X, lab)
        if best is None or sc > best[1]:
            best = (k, sc, lab)
    return best


def cluster(P: pd.DataFrame, seed: int = 0):
    X = _standardise(P)
    k, sil, lab = choose_k(X, seed=seed)
    return pd.Series(lab, index=P.index, name="cluster"), k, sil


def bootstrap_stability(raw: pd.DataFrame, px: pd.DataFrame, k: int, base_labels: pd.Series, min_rows: int, B: int = 30, seed: int = 5) -> float:
    """Average co-assignment agreement: for every pair of brokers, how often the ticker-bootstrap clustering puts them together/apart as the base clustering does."""
    from sklearn.cluster import KMeans
    rng = np.random.default_rng(seed)
    tickers = np.array(sorted(raw["ticker"].unique()))
    names = list(base_labels.index)
    base = (base_labels.to_numpy()[:, None] == base_labels.to_numpy()[None, :])
    agree = np.zeros((len(names), len(names)))
    cnt = np.zeros_like(agree)
    for _ in range(B):
        samp = rng.choice(tickers, size=len(tickers), replace=True)
        P = broker_profiles(raw, px, min_rows=max(30, int(min_rows * 0.7)), tickers=list(samp))
        common_b = [b for b in names if b in P.index]
        if len(common_b) < k + 2:
            continue
        X = _standardise(P.loc[common_b])
        lab = KMeans(n_clusters=k, n_init=10, random_state=int(rng.integers(1e6))).fit_predict(X)
        same = lab[:, None] == lab[None, :]
        ii = [names.index(b) for b in common_b]
        for a_i, i in enumerate(ii):
            for b_i, j in enumerate(ii):
                agree[i, j] += float(same[a_i, b_i] == base[i, j])
                cnt[i, j] += 1
    mask = (cnt > 0) & ~np.eye(len(names), dtype=bool)
    return float(np.sum(agree[mask]) / np.sum(cnt[mask])) if mask.any() else float("nan")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=common.IPOT_DB)
    ap.add_argument("--min-rows", type=int, default=150)
    ap.add_argument("--boot", type=int, default=30)
    a = ap.parse_args()
    raw = common.load_ipot(a.db)
    px = common.load_ohlcv(tickers=sorted(raw["ticker"].unique()), start="2025-01-01")
    P = broker_profiles(raw, px, a.min_rows)
    lab, k, sil = cluster(P)
    stab = bootstrap_stability(raw, px, k, lab, a.min_rows, B=a.boot)
    P["cluster"] = lab
    P["static_class"] = [common.BROKER_CLASS.get(b, "other") for b in P.index]
    pd.set_option("display.width", 220)
    print(f"{len(P)} brokers with >= {a.min_rows} rows | k = {k} (silhouette {sil:.2f}) | bootstrap pair-agreement {stab:.2f} (1.0 = perfectly stable; ~0.5-0.6 = barely better than chance for k={k})")
    print("\nCluster medians (profile) and size:")
    print(P.groupby("cluster")[PROFILE_COLS].median().round(3).assign(n=P.groupby("cluster").size()).to_string())
    print("\nMembers (with the provisional static class for comparison):")
    for c, g in P.groupby("cluster"):
        print(f"  cluster {int(c)}: " + ", ".join(f"{b}({g.loc[b, 'static_class'][:6]})" for b in g.sort_values('gross_share', ascending=False).index))
    print("\n(Descriptive: the clusters describe HOW brokers trade; naming and any edge claim need the full data and the engine-4 skill test.)")
