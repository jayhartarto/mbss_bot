"""
research/ipot_explore/mover_rule_by_regime.py — the walk-forward mover score of mover_rule_walkforward.py, split by IHSG regime phase of the signal date.
Every labelled row appears exactly once as a TEST row across the three top150 folds (A->B, A+B->C, C->A), so their selected rows are pooled; the outside-top150 rows are tested with the
rule trained on top150 (cross-population). Per regime: n entries, hit-rate and mean realised return of entries vs ALL rows of the same regime (and same block x ATR cell for the hit lift).
Run (project root, PYTHONUTF8=1):  python -W ignore -m research.ipot_explore.mover_rule_by_regime
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common
from research.ipot_explore.mover_footprint import panel, TOP, OUT, SKIP
from research.ipot_explore.mover_rule_walkforward import realised, select, score, SIZE, HOR
from research.ipot_explore.xl_study import ihsg_phase


def split(d, test_blks):
    te = d[d.blk.isin(test_blks)]
    u = pd.DatetimeIndex(d["date"].unique()).sort_values(); lo = u.searchsorted(te.date.min()) - HOR; hi = u.searchsorted(te.date.max()) + HOR
    keep = (d["date"] >= u[max(lo, 0)]) & (d["date"] <= u[min(hi, len(u) - 1)])
    return d[(~d.blk.isin(test_blks)) & (~keep)], te


def selected(train, test, feats):
    kept = select(train, feats)
    if not kept:
        return test.iloc[0:0].assign(sel=False), None
    thr = score(train, kept).quantile(0.8)
    t = test.copy(); t["score"] = score(t, kept); t["sel"] = t["score"] >= thr
    return t, kept


def report(name, t, reg):
    t = t.merge(reg, on="date", how="left"); t["regime_phase"] = t["regime_phase"].fillna("NA")
    t["base"] = t.groupby(["blk", "vol"])["tp_first"].transform("mean")
    print(f"\n{name}: entries {int(t.sel.sum())} of {len(t)} rows")
    print("  regime               rows(all) | entries | hit% (all)  lift-pp | ret% entries vs all rows (diff)")
    for rg, g in t.groupby("regime_phase"):
        s = g[g.sel]
        if len(s) < 40:
            print(f"  {rg:<20} {len(g):>6} | {len(s):>5} | too few"); continue
        lift = (s.tp_first - s.base).mean()
        cs = (s.tp_first - s.base).groupby(s["ticker"]).apply(lambda v: float((v - (s.tp_first - s.base).mean()).sum()))
        tt = lift / (np.sqrt((cs ** 2).sum()) / len(s))
        print(f"  {rg:<20} {len(g):>6} | {len(s):>5} | {100 * s.tp_first.mean():4.1f} ({100 * g.tp_first.mean():4.1f}) {100 * lift:+5.1f} (t {tt:+.1f}) | {100 * s.ret.mean():+.2f} vs {100 * g.ret.mean():+.2f} ({100 * (s.ret.mean() - g.ret.mean()):+.2f})")


def main():
    rl = realised(common.load_ohlcv(start="2025-06-01"))
    d1 = panel(TOP, 3, common.OUT_DIR + "/features.pkl").merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"]).reset_index(drop=True)
    d2 = panel(OUT, 2, None).merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"]).reset_index(drop=True)
    feats = [c for c in d1.columns if c in d2.columns and c not in SKIP and c != "ret" and not c.endswith(SIZE) and c not in ("value", "volume")
             and (pd.api.types.is_numeric_dtype(d1[c]) or pd.api.types.is_bool_dtype(d1[c]))]
    reg = ihsg_phase()
    parts = []
    for tb, trb in (([2], [1]), ([3], [1, 2]), ([1], [3])):
        tr, te = split(d1, tb); tr = tr[tr.blk.isin(trb)]
        t, kept = selected(tr, te, feats); parts.append(t)
    report("top150 walk-forward (3 folds pooled)", pd.concat(parts), reg)
    t, kept = selected(d1, d2, feats)
    report("train top150 -> test outside top150", t, reg)
    t2, kept2 = selected(d2, d1, feats)
    report("train outside -> test top150", t2, reg)


if __name__ == "__main__":
    main()
