"""
research/ipot_explore/sweep.py — Engine 1 of the IPOT research framework: univariate sweep of EVERY feature x bin x horizon against the relative forward return,
calibrated by a within-date permutation null and Westfall-Young maxT adjustment.

Run (project root):  python -m research.ipot_explore.sweep [--features research/ipot_explore/out/features.pkl] [--B 500] [--min-n 30] [--inject]

Method
  * Outcomes: rel_{1,3,5,10}d = forward return from open(t+1) minus the liquid-universe median of the same date (so regime / market direction cancel).
  * Bins per continuous feature: <=P20, <=P33, >=P67, >=P80 (cutoffs pooled over the sample; they depend on X only, so the permutation null stays valid);
    boolean features: ==1. Statistic: (mean of bin - mean of all) / sqrt(s2/n_bin * (1 - n_bin/N)).
  * Null: shuffle the outcomes among the tickers of the SAME DATE (keeps date effects and the cross-sectional structure), recompute every statistic, keep the
    max |t| over all tests -> adjusted p (maxT) = share of permutations whose max |t| >= observed. Also a global check: is the COUNT of |t|>2 bins larger than chance?
  * Replication: sign and size of the same bin inside each of 3 contiguous date blocks.
  * `--inject` adds +3% to rel_5d for the top-quintile of one feature: a positive control (the sweep must find it, otherwise the machinery has no power).
Caveats written into the output: overlapping horizons are autocorrelated (naive t overstates for h>1; ticker-clustered CIs are computed for the shortlisted bins only in the
next step), and a few dozen tickers x a few weeks (dev data) cannot support conclusions.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from research.ipot_explore import common

OUTCOMES = ["rel_1d", "rel_3d", "rel_5d", "rel_10d"]
EXCLUDE = {"ticker", "date", "close", "value", "volume", "val20", "mfe_5d", "mae_5d", "dom_vwap"}
# raw IDR / price-level columns identify the stock (its price or liquidity), so they "predict" through ticker identity -- use only scale-free ratios
EXCLUDE_SUFFIX = ("_avg", "_vis", "_mid")


def feature_columns(df: pd.DataFrame) -> list[str]:
    out = []
    for c in df.columns:
        if c in EXCLUDE or c.endswith(EXCLUDE_SUFFIX) or c.startswith(("ret_", "rel_")):
            continue
        if not (pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_bool_dtype(df[c])):
            continue                      # broker-name / label columns are not swept here
        out.append(c)
    return out


def design(df: pd.DataFrame, cols: list[str], min_unique: int = 8):
    """Boolean membership matrix (rows x bins) and a metadata frame."""
    masks, meta = [], []
    for c in cols:
        x = df[c]
        if x.dtype == bool or set(pd.unique(x.dropna())) <= {0, 1, True, False}:
            masks.append((x == 1).to_numpy() & x.notna().to_numpy())
            meta.append((c, "==1"))
            continue
        xv = x.replace([np.inf, -np.inf], np.nan)
        if xv.nunique() < min_unique:
            continue
        q = xv.quantile([0.20, 0.33, 0.67, 0.80])
        ok = xv.notna().to_numpy()
        for name, m in (("<=P20", xv <= q[0.20]), ("<=P33", xv <= q[0.33]), (">=P67", xv >= q[0.67]), (">=P80", xv >= q[0.80])):
            masks.append(m.to_numpy() & ok)
            meta.append((c, name))
    return np.column_stack(masks), pd.DataFrame(meta, columns=["feature", "bin"])


def _tstats(M: np.ndarray, Y: np.ndarray):
    """Per bin x outcome: n, diff-from-overall mean, t. M float32 (n x k), Y float (n x h) with NaN."""
    V = (~np.isnan(Y)).astype(np.float64)
    Yz = np.nan_to_num(Y, nan=0.0)
    N = V.sum(axis=0)
    mu = Yz.sum(axis=0) / N
    s2 = (Yz ** 2).sum(axis=0) / N - mu ** 2
    nb = M.T @ V
    sb = M.T @ Yz
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_b = sb / nb
        diff = mean_b - mu
        se = np.sqrt(s2 * (1.0 / nb) * (1.0 - nb / N))
        t = diff / se
    t[nb < 2] = np.nan
    return nb, diff, t


def _within_date_perm(date_codes: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Row index permutation that shuffles rows only among rows of the same date."""
    base = np.argsort(date_codes, kind="stable")
    shuffled = np.lexsort((rng.random(len(date_codes)), date_codes))
    perm = np.empty(len(date_codes), dtype=np.int64)
    perm[base] = shuffled
    return perm


def _ticker_shift_perm(groups: list[np.ndarray], n: int, rng: np.random.Generator) -> np.ndarray:
    """Row permutation that circularly shifts the outcome series of EACH ticker by its own random offset (same offset for all horizons)."""
    perm = np.arange(n)
    for idx in groups:
        if len(idx) > 2:
            perm[idx] = idx[np.roll(np.arange(len(idx)), int(rng.integers(1, len(idx))))]
    return perm


def run(df: pd.DataFrame, B: int = 500, min_n: int = 30, seed: int = 7, inject: str | None = None, verbose: bool = True,
        null: str = "shift", inject_size: float = 0.03) -> pd.DataFrame:
    df = df.dropna(subset=["rel_1d", "rel_3d", "rel_5d", "rel_10d"], how="all").sort_values(["ticker", "date"]).reset_index(drop=True)
    groups = [np.where(df["ticker"].to_numpy() == t)[0] for t in df["ticker"].unique()]
    cols = feature_columns(df)
    if inject:
        thr = df[inject].quantile(0.8)
        df.loc[df[inject] >= thr, "rel_5d"] += inject_size
    M, meta = design(df, cols)
    Mf = M.astype(np.float32)
    Y = df[OUTCOMES].to_numpy(dtype=np.float64)
    date_codes = pd.factorize(df["date"])[0]
    nb, diff, t = _tstats(Mf, Y)
    tests_ok = nb >= min_n
    t_eff = np.where(tests_ok, t, np.nan)
    obs_max = np.nanmax(np.abs(t_eff), axis=0)

    rng = np.random.default_rng(seed)
    max_null = np.zeros((B, len(OUTCOMES)))
    cnt_null = np.zeros((B, len(OUTCOMES)))
    for b in range(B):
        perm = _within_date_perm(date_codes, rng) if null == "date" else _ticker_shift_perm(groups, len(df), rng)
        _, _, tp = _tstats(Mf, Y[perm])
        tp = np.where(tests_ok, tp, np.nan)
        max_null[b] = np.nanmax(np.abs(tp), axis=0)
        cnt_null[b] = np.nansum(np.abs(tp) > 2.0, axis=0)

    blocks = pd.qcut(pd.factorize(df["date"], sort=True)[0], 3, labels=False, duplicates="drop")
    rows = []
    for j, h in enumerate(OUTCOMES):
        padj = (1 + (max_null[:, j][:, None] >= np.abs(t_eff[:, j])[None, :]).sum(axis=0)) / (B + 1.0)
        for i in np.where(tests_ok[:, j])[0]:
            rows.append((meta.at[i, "feature"], meta.at[i, "bin"], h, int(nb[i, j]), diff[i, j], t[i, j], padj[i]))
    res = pd.DataFrame(rows, columns=["feature", "bin", "outcome", "n", "diff", "t", "p_adj_maxT"])

    # replication across 3 contiguous date blocks (same bin, block-specific baseline)
    def block_diff(i, j, blk):
        rows_b = blocks == blk
        y = Y[rows_b, j]
        m = M[rows_b, i]
        yv = ~np.isnan(y)
        if (m & yv).sum() < 5:
            return np.nan
        return y[m & yv].mean() - y[yv].mean()
    key = {(meta.at[i, "feature"], meta.at[i, "bin"]): i for i in range(len(meta))}
    top = res.sort_values("t", key=np.abs, ascending=False).head(40).copy()
    for blk in range(3):
        top[f"blk{blk + 1}"] = [block_diff(key[(r.feature, r.bin)], OUTCOMES.index(r.outcome), blk) for r in top.itertuples()]
    top["same_sign_blocks"] = [int(np.sum(np.sign(r[["blk1", "blk2", "blk3"]].astype(float)) == np.sign(r["diff"]))) for _, r in top.iterrows()]

    if verbose:
        n_tests = int(tests_ok.sum())
        print(f"rows={len(df)} | features={len(cols)} | bins={len(meta)} | tests (bin x outcome, n>={min_n})={n_tests} | permutations={B} | null={null}")
        for j, h in enumerate(OUTCOMES):
            obs_cnt = int(np.nansum(np.abs(t_eff[:, j]) > 2.0))
            p_glob = (1 + (cnt_null[:, j] >= obs_cnt).sum()) / (B + 1.0)
            print(f"  {h:7s}: bins with |t|>2 = {obs_cnt:3d} (null mean {cnt_null[:, j].mean():5.1f}, P95 {np.percentile(cnt_null[:, j], 95):5.1f}) "
                  f"-> global p = {p_glob:.3f} | max|t| observed {obs_max[j]:.2f} vs null P95 {np.percentile(max_null[:, j], 95):.2f}")
        print("\nTop 15 bins by |t| (diff = mean relative return of the bin minus overall, in return units; p_adj = maxT-adjusted):")
        show = top.head(15).copy()
        for c in ("diff", "blk1", "blk2", "blk3"):
            show[c] = (show[c] * 100).round(2)
        show["t"] = show["t"].round(2)
        show["p_adj_maxT"] = show["p_adj_maxT"].round(3)
        print(show[["feature", "bin", "outcome", "n", "diff", "t", "p_adj_maxT", "blk1", "blk2", "blk3", "same_sign_blocks"]].to_string(index=False))
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default=common.OUT_DIR + "/features.pkl")
    ap.add_argument("--B", type=int, default=500)
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--null", choices=["shift", "date"], default="shift", help="shift = per-ticker circular shift (default, honest); date = shuffle within date")
    ap.add_argument("--inject-size", type=float, default=0.03)
    ap.add_argument("--inject", default=None, help="feature name for the positive control (adds +3%% to rel_5d on its top quintile)")
    a = ap.parse_args()
    feats = pd.read_pickle(a.features)
    run(feats, B=a.B, min_n=a.min_n, inject=a.inject, null=a.null, inject_size=a.inject_size)
