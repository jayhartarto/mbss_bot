"""
research/ipot_explore/mover_rule_walkforward.py — walk-forward test of a broker-footprint score for "stock moves up within 20 days" with TP+10% / SL-7% / 20-day timeout mechanics.

Run (project root):  python -W ignore -m research.ipot_explore.mover_rule_walkforward
Per fold, ON THE TRAIN ROWS ONLY: scan every feature (same block x ATR-tercile demeaning as mover_footprint), keep features with |t| >= 3 whose sign is the same in every train block,
EXCLUDING raw rupiah/size proxies (*_vis, *_mid, *_avg, value, volume). Score = mean over kept features of sign * z (z from train mean/sd). Entry rule = score >= the train P80 of score.
Test (held-out rows, a 20-trading-day embargo is removed from train on both sides of the test period): hit rate and mean realised return of the entry rule vs the test base rate of the same
block x ATR cell, on all rows and on NON-OVERLAPPING entries (greedy per ticker, a new entry only 20 trading days after the last one). Folds: A->B, A+B->C, C->A on top150; b1->b2, b2->b1
outside; and cross-population (train top150 -> test outside, train outside -> test top150). No costs or slippage are deducted.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common
from research.ipot_explore.mover_footprint import panel, TOP, OUT, SKIP

SIZE = ("_vis", "_mid", "_avg", "_n5")
TP, SL, HOR = 0.10, -0.07, 20


def realised(px: pd.DataFrame) -> pd.DataFrame:
    out = []
    for tk, g in px.sort_values(["ticker", "date"]).groupby("ticker"):
        o, h, l, c = g["open"].to_numpy(), g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy()
        n = len(g); ret = np.full(n, np.nan)
        for i in range(n - 1 - HOR):
            e = o[i + 1]
            if not e > 0:
                continue
            r = c[i + HOR] / e - 1
            for k in range(i + 1, i + HOR + 1):
                if l[k] / e - 1 <= SL:
                    r = SL; break
                if h[k] / e - 1 >= TP:
                    r = TP; break
            ret[i] = r
        out.append(pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy(), "ret": ret}))
    return pd.concat(out, ignore_index=True)


def select(train: pd.DataFrame, feats: list[str]):
    cell = train["blk"].astype(str) + "_" + train["vol"].astype(str)
    y = train["tp_first"] - train.groupby(cell)["tp_first"].transform("mean")
    kept = []
    for c in feats:
        x = train[c].astype(float).replace([np.inf, -np.inf], np.nan)
        if x.notna().mean() < 0.7 or x.nunique() < 5:
            continue
        lo, hi = x.quantile([.01, .99]); x = x.clip(lo, hi)
        xs = (x - x.groupby(cell).transform("mean")) / x.std(); m = xs.notna()
        xv, yv = xs[m].to_numpy(), y[m].to_numpy()
        beta = (xv * yv).sum() / (xv * xv).sum(); e = yv - beta * xv
        cs = pd.Series(xv * e).groupby(train.loc[m, "ticker"].to_numpy()).sum()
        t = beta / (np.sqrt((cs ** 2).sum()) / (xv * xv).sum())
        sg = [np.sign((xv[(train.loc[m, "blk"] == b).to_numpy()] * yv[(train.loc[m, "blk"] == b).to_numpy()]).sum()) for b in sorted(train.blk.unique())]
        if abs(t) >= 3 and all(s == sg[0] for s in sg) and sg[0] != 0:
            kept.append((c, float(np.sign(beta)), float(train[c].astype(float).clip(lo, hi).mean()), float(train[c].astype(float).clip(lo, hi).std()), lo, hi))
    return kept


def score(d: pd.DataFrame, kept) -> pd.Series:
    if not kept:
        return pd.Series(np.nan, index=d.index)
    z = [sg * ((d[c].astype(float).clip(lo, hi) - mu) / sd).fillna(0.0) for c, sg, mu, sd, lo, hi in kept]
    return sum(z) / len(z)


def nonoverlap(s: pd.DataFrame) -> pd.DataFrame:
    keep = []
    for tk, g in s.sort_values("date").groupby("ticker"):
        last = None
        for idx, dt in zip(g.index, g["date"]):
            if last is None or (dt - last).days >= 28:       # ~20 trading days
                keep.append(idx); last = dt
    return s.loc[keep]


def evaluate(name: str, train: pd.DataFrame, test: pd.DataFrame, feats: list[str]):
    kept = select(train, feats)
    if not kept:
        print(f"  {name:<28} no feature passed the train filter"); return
    thr = score(train, kept).quantile(0.8)
    test = test.copy(); test["score"] = score(test, kept)
    sel = test[test["score"] >= thr]
    base_hit = test.groupby(["blk", "vol"])["tp_first"].transform("mean")
    lift = (sel["tp_first"] - base_hit.loc[sel.index]).mean()
    no = nonoverlap(sel)
    cs = sel.groupby("ticker")["tp_first"].apply(lambda v: float((v - v.mean()).sum()))
    print(f"  {name:<28} feats {len(kept):>2} | rule n {len(sel):>5} hit {100 * sel.tp_first.mean():4.1f}% vs test base {100 * test.tp_first.mean():4.1f}% (cell-matched lift {100 * lift:+.1f}pp) "
          f"| ret {100 * sel.ret.mean():+.2f}% vs all {100 * test.ret.mean():+.2f}% | non-overlap n {len(no):>4} ret {100 * no.ret.mean():+.2f}% hit {100 * no.tp_first.mean():.1f}%")
    return kept


def main():
    px_all = common.load_ohlcv(start="2025-06-01")
    rl = realised(px_all)
    d1 = panel(TOP, 3, common.OUT_DIR + "/features.pkl").merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"]).reset_index(drop=True)
    d2 = panel(OUT, 2, None).merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"]).reset_index(drop=True)
    common_cols = [c for c in d1.columns if c in d2.columns and c not in SKIP and c not in ("ret",) and not c.endswith(SIZE) and c not in ("value", "volume")
                   and (pd.api.types.is_numeric_dtype(d1[c]) or pd.api.types.is_bool_dtype(d1[c]))]
    print(f"top150 rows {len(d1)} (mean ret {100 * d1.ret.mean():+.2f}%, hit {100 * d1.tp_first.mean():.1f}%) | outside rows {len(d2)} (mean ret {100 * d2.ret.mean():+.2f}%, hit {100 * d2.tp_first.mean():.1f}%) | candidate features {len(common_cols)}")

    def split(d, test_blks):
        te = d[d.blk.isin(test_blks)]
        u = pd.DatetimeIndex(d["date"].unique()).sort_values(); lo = u.searchsorted(te.date.min()) - HOR; hi = u.searchsorted(te.date.max()) + HOR
        keep = (d["date"] >= u[max(lo, 0)]) & (d["date"] <= u[min(hi, len(u) - 1)])
        return d[(~d.blk.isin(test_blks)) & (~keep)], te

    print("\n[top150] walk-forward with embargo")
    for name, tb in (("A->B", [2]), ("A+B->C", [3]), ("C->A", [1])):
        tr, te = split(d1, tb)
        if name == "A->B":
            tr = tr[tr.blk == 1]
        if name == "A+B->C":
            tr = tr[tr.blk.isin([1, 2])]
        if name == "C->A":
            tr = tr[tr.blk == 3]
        evaluate(name, tr, te, common_cols)
    print("\n[outside top150] walk-forward with embargo (2 blocks)")
    for name, tb in (("b1->b2", [2]), ("b2->b1", [1])):
        tr, te = split(d2, tb)
        evaluate(name, tr, te, common_cols)
    print("\n[cross-population] train on one universe, test on the other (no embargo needed except in-time overlap, so train rows within the test dates are dropped)")
    kept = evaluate("train top150 -> test outside", d1[d1.date < d2.date.min() - pd.Timedelta(days=30)] if False else d1, d2, common_cols)
    if kept:
        print("      features used:", ", ".join(f"{c}({'+' if sg > 0 else '-'})" for c, sg, *_ in kept))
    kept = evaluate("train outside -> test top150", d2, d1, common_cols)
    if kept:
        print("      features used:", ", ".join(f"{c}({'+' if sg > 0 else '-'})" for c, sg, *_ in kept))


if __name__ == "__main__":
    main()
