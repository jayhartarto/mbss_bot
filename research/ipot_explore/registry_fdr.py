"""
research/ipot_explore/registry_fdr.py — multiple-testing audit of the families of tests run so far (see hypotheses.md).
Family A (engine 1 sweep, 188 bins x 4 horizons): Westfall-Young maxT adjusted p (already FWER-controlling) -> count of bins with p_adj < 0.05 per horizon.
Family B (mover footprint scan, ~63 features per population, ticker-clustered t -> two-sided normal p): Benjamini-Hochberg q = 0.05 per population, and the intersection with same sign in both.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.registry_fdr
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from research.ipot_explore import common, sweep
from research.ipot_explore.mover_footprint import panel, scan, TOP, OUT


def norm_p(t: float) -> float:
    return math.erfc(abs(t) / math.sqrt(2))


def bh(p: np.ndarray, q: float = 0.05) -> np.ndarray:
    n = len(p); order = np.argsort(p); thr = q * (np.arange(1, n + 1) / n)
    ok = p[order] <= thr
    k = np.max(np.where(ok)[0]) + 1 if ok.any() else 0
    keep = np.zeros(n, bool); keep[order[:k]] = True
    return keep


def main():
    feat = pd.read_pickle(common.OUT_DIR + "/features.pkl")
    res = sweep.run(feat, B=500, verbose=False)
    print("FAMILY A  sweep (maxT-adjusted, FWER):  tests:", len(res))
    for h, g in res.groupby("outcome"):
        print(f"   {h:7s} bins with p_adj<0.05: {int((g.p_adj_maxT < 0.05).sum()):>3} | <0.10: {int((g.p_adj_maxT < 0.10).sum()):>3} | features: {sorted(set(g[g.p_adj_maxT < 0.05].feature))}")
    d1 = panel(TOP, 3, common.OUT_DIR + "/features.pkl"); d2 = panel(OUT, 2, None)
    r1, r2 = scan(d1, 3), scan(d2, 2)
    for name, r in (("top150", r1), ("outside", r2)):
        r = r.copy(); r["p"] = r["t"].map(norm_p); r["bh"] = bh(r["p"].to_numpy())
        print(f"\nFAMILY B  mover scan {name}: {len(r)} features, nominal |t|>=2: {int((r.t.abs() >= 2).sum())}, |t|>=3: {int((r.t.abs() >= 3).sum())}, BH q<0.05 survivors: {int(r.bh.sum())}")
        print("   survivors:", ", ".join(f"{a}({t:+.1f})" for a, t in zip(r[r.bh].feature, r[r.bh].t)))
        if name == "top150":
            r1 = r
        else:
            r2 = r
    m = r1.merge(r2, on="feature", suffixes=("_top", "_out"))
    both = m[m.bh_top & m.bh_out & (np.sign(m.beta_top) == np.sign(m.beta_out))]
    print(f"\nsurvive BH in BOTH populations with the same sign ({len(both)}): " + ", ".join(f"{a}({bt:+.1f}/{bo:+.1f})" for a, bt, bo in zip(both.feature, both.t_top, both.t_out)))
    print("\nExperiment count so far (approx.): sweep 752 bin-outcome tests; broker skill 59 brokers x 6 views; mover scan 63-67 features x 2 populations; dominant window 3 windows x 6 cells x 2 horizons;")
    print("VCP ladder 6 steps x 2 mechanics x 2 designs; heuristics 7 rules x 2 populations; engine-2 trees 3 sets x 3 folds.")


if __name__ == "__main__":
    main()
