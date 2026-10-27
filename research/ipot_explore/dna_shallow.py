"""
research/ipot_explore/dna_shallow.py — Engine 2 of the IPOT research framework: shallow-tree "DNA" of the stocks that go UP, from technical + broker features.

Run (project root):  python -m research.ipot_explore.dna_shallow [--h 10] [--top 0.2] [--depth 3] [--leaf 300] [--embargo 10]

Method
  * Winner = ticker-day whose rel_{h}d (forward return from open(t+1) minus the same-date liquid-universe median) is in the within-date top `top` share.
    Base precision is therefore ~top by construction; a rule is useful only if its OUT-OF-SAMPLE precision and mean rel_h beat the base.
  * Features: scale-free broker features (same exclusions as sweep.py) and past-only technicals computed here from OHLCV (RSI14, dist_sma20, ATR%, 60d range
    position, %B, past 1/5/10d return). Three feature sets are compared: tech only, broker only, both -> broker adds value only if "both" beats "tech only".
  * Rules: a depth-<=`depth` tree is fit on the TRAIN dates; every leaf is a conjunction of <=depth conditions. Leaves with train n>=`leaf` and train mean rel_h > base
    are kept, ranked by train mean rel_h; the top 5 are then evaluated UNCHANGED on the TEST dates.
  * Walk-forward with embargo: dates are split in 3 contiguous blocks; folds A->B, A+B->C, and the reverse C->A (embargo of `embargo` trading days around the
    boundary, since rel_10d overlaps 10 days). A rule counts as replicated only if its test mean rel_h > 0 in EVERY fold it is re-found in.
  * Test-row overlap: rows are NOT de-duplicated per ticker-episode here, so n and t are optimistic; this is the exploration pass, not the Path-2 standard.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeClassifier

from research.ipot_explore import common
from research.ipot_explore.sweep import feature_columns

TECH = ["rsi14", "dist_sma20", "atr_pct", "range60_pos", "pctb", "pret_1d", "pret_5d"]


def add_technicals(df: pd.DataFrame) -> pd.DataFrame:
    px = common.load_ohlcv(start="2025-01-01").sort_values(["ticker", "date"])
    g = px.groupby("ticker")
    c = px["close"]
    d = c.diff()
    up = d.clip(lower=0).groupby(px["ticker"]).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
    dn = (-d.clip(upper=0)).groupby(px["ticker"]).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
    px["rsi14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    sma20 = g["close"].transform(lambda s: s.rolling(20).mean())
    sd20 = g["close"].transform(lambda s: s.rolling(20).std())
    px["dist_sma20"] = c / sma20 - 1
    px["pctb"] = (c - (sma20 - 2 * sd20)) / (4 * sd20)
    pc = g["close"].shift(1)
    tr = pd.concat([px["high"] - px["low"], (px["high"] - pc).abs(), (px["low"] - pc).abs()], axis=1).max(axis=1)
    px["atr_pct"] = tr.groupby(px["ticker"]).transform(lambda s: s.rolling(14).mean()) / c
    lo = g["low"].transform(lambda s: s.rolling(60, min_periods=30).min())
    hi = g["high"].transform(lambda s: s.rolling(60, min_periods=30).max())
    px["range60_pos"] = (c - lo) / (hi - lo)
    px["pret_1d"] = g["close"].pct_change(1)
    px["pret_5d"] = g["close"].pct_change(5)
    return df.merge(px[["ticker", "date"] + TECH], on=["ticker", "date"], how="left")


def broker_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in feature_columns(df) if c not in TECH and c not in ("past_ret_10d", "win")]


def rules_from_tree(tree: DecisionTreeClassifier, names: list[str]):
    """All leaves as lists of (feature, '<=' | '>', threshold)."""
    t = tree.tree_
    out = []

    def walk(node, path):
        if t.children_left[node] == -1:
            out.append(path)
            return
        f, th = names[t.feature[node]], t.threshold[node]
        walk(t.children_left[node], path + [(f, "<=", th)])
        walk(t.children_right[node], path + [(f, ">", th)])

    walk(0, [])
    return out


def mask(df: pd.DataFrame, rule) -> np.ndarray:
    m = np.ones(len(df), bool)
    for f, op, th in rule:
        x = df[f].to_numpy()
        m &= (x <= th) if op == "<=" else (x > th)
    return m


def fmt(rule) -> str:
    return " & ".join(f"{f}{op}{th:.3g}" for f, op, th in rule)


def folds(dates: np.ndarray, embargo: int):
    u = np.sort(np.unique(dates))
    k = len(u) // 3
    A, B, C = u[:k], u[k:2 * k], u[2 * k:]
    def cut(x, n, side):          # drop the `n` dates of x nearest the boundary
        return x[:-n] if side == "end" else x[n:]
    return [("A->B", cut(A, embargo, "end"), B), ("A+B->C", cut(np.concatenate([A, B]), embargo, "end"), C), ("C->A", cut(C, embargo, "start"), A)]


def run(df: pd.DataFrame, h: int = 10, top: float = 0.2, depth: int = 3, leaf: int = 300, embargo: int = 10, seed: int = 7):
    y = f"rel_{h}d"
    df = df.dropna(subset=[y]).copy()
    df["win"] = (df.groupby("date")[y].rank(pct=True, method="average") > 1 - top).astype(int)
    sets = {"tech": TECH + ["past_ret_10d"], "broker": broker_columns(df), "both": TECH + ["past_ret_10d"] + broker_columns(df)}
    rows = []
    for name, cols in sets.items():
        cols = [c for c in cols if c in df.columns]
        d = df.dropna(subset=cols).copy()
        d[cols] = d[cols].replace([np.inf, -np.inf], np.nan)
        d = d.dropna(subset=cols)
        for fname, tr_dates, te_dates in folds(d["date"].to_numpy(), embargo):
            tr, te = d[d["date"].isin(tr_dates)], d[d["date"].isin(te_dates)]
            base_te = te[y].mean()
            tree = DecisionTreeClassifier(max_depth=depth, min_samples_leaf=leaf, class_weight="balanced", random_state=seed).fit(tr[cols], tr["win"])
            cand = []
            for r in rules_from_tree(tree, cols):
                m = mask(tr, r)
                if m.sum() >= leaf and tr.loc[m, y].mean() > tr[y].mean():
                    cand.append((tr.loc[m, y].mean(), r))
            for tr_mean, r in sorted(cand, key=lambda x: -x[0])[:5]:
                m = mask(te, r)
                if m.sum() < 30:
                    continue
                rows.append(dict(set=name, fold=fname, rule=fmt(r), n_test=int(m.sum()), tr_mean=100 * tr_mean, te_mean=100 * te.loc[m, y].mean(),
                                 te_lift=100 * (te.loc[m, y].mean() - base_te), te_prec=te.loc[m, "win"].mean(), base_prec=te["win"].mean()))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default=common.OUT_DIR + "/features.pkl")
    ap.add_argument("--h", type=int, default=10)
    ap.add_argument("--top", type=float, default=0.2)
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--leaf", type=int, default=300)
    ap.add_argument("--embargo", type=int, default=10)
    a = ap.parse_args()
    df = add_technicals(pd.read_pickle(a.features))
    res = run(df, a.h, a.top, a.depth, a.leaf, a.embargo)
    res.to_csv(common.OUT_DIR + f"/dna_shallow_h{a.h}.csv", index=False)
    pd.set_option("display.width", 250, "display.max_colwidth", 90)
    print("OUT-OF-SAMPLE lift of train-selected leaves (te_lift = test mean rel_h of the rule minus test mean of all rows, in pp):")
    print(res.groupby(["set", "fold"]).agg(rules=("rule", "count"), te_lift_mean=("te_lift", "mean"), pos_share=("te_lift", lambda s: (s > 0).mean()),
                                           prec=("te_prec", "mean"), base=("base_prec", "mean")).round(3))
    print()
    print(res.sort_values("te_lift", ascending=False).head(12)[["set", "fold", "rule", "n_test", "tr_mean", "te_mean", "te_lift", "te_prec"]].round(2).to_string(index=False))
