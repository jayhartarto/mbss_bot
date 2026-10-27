"""
research/ipot_explore/gorengan_episodes.py — the 'gorengan' hypothesis: broker footprint matters for episodic run-ups (a stock is marked up within a time window), not as a persistent trait.

Episode (defined from PRICE ONLY, no broker information): day t0 such that max(high[t0+1..t0+15]) / close[t0] - 1 >= RUN (default +30%), the 10-day return before t0 lies in [-10%, +10%] (the run starts from a quiet base),
and no other episode started in the previous 30 trading days of the same ticker. The broker footprint at t0 (published after the close, so knowable before the run) is compared with all other stock-days of the same
date block and ATR tercile (stratum fixed effects, ticker-clustered t, sign agreement across blocks), exactly like the mover scan but with target = episode start. Liquidity is only a continuous feature.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.gorengan_episodes [--run 0.30]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from research.ipot_explore import common, sweep
from research.ipot_explore.lead_1d import prep
from research.ipot_explore.mover_footprint import OUT, scan

SIZE = ("_vis", "_mid", "_avg", "_n5")


def episode_flags(px: pd.DataFrame, run: float, hor: int = 15) -> pd.DataFrame:
    out = []
    for tk, g in px.sort_values(["ticker", "date"]).groupby("ticker"):
        c, h = g["close"].to_numpy(), g["high"].to_numpy(); n = len(g)
        fmax = np.full(n, np.nan); pre = np.full(n, np.nan)
        for i in range(10, n - hor):
            fmax[i] = h[i + 1:i + hor + 1].max() / c[i] - 1
            pre[i] = c[i] / c[i - 10] - 1
        cand = np.where((fmax >= run) & (pre >= -0.10) & (pre <= 0.10))[0]
        keep, last = [], -999
        for i in cand:
            if i - last >= 30:
                keep.append(i); last = i
        flag = np.zeros(n, bool); flag[keep] = True
        out.append(pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy(), "ep": flag, "fmax15": fmax, "pre10": pre}))
    return pd.concat(out, ignore_index=True)


def main(run: float):
    f1 = prep(None, common.OUT_DIR + "/features.pkl"); f2 = prep(OUT, None)
    f = pd.concat([f1, f2[~f2.set_index(["ticker", "date"]).index.isin(f1.set_index(["ticker", "date"]).index)]], ignore_index=True)
    px = common.load_ohlcv(start="2025-01-01")
    ep = episode_flags(px[px.ticker.isin(f.ticker.unique())], run)
    d = f.merge(ep, on=["ticker", "date"], how="inner").dropna(subset=["fmax15"]).reset_index(drop=True)
    d["tp_first"] = d["ep"].astype(float)          # scan() expects this column name as its target
    d["atr"] = d["atr_pct"]; d["vol"] = pd.qcut(d["atr_pct"], 3, labels=False, duplicates="drop")
    top = set(f1.ticker.unique()); d["pop"] = np.where(d.ticker.isin(top), "top150", "outside")
    u = np.sort(d["date"].unique()); k = len(u) // 3
    d["blk"] = np.where(d["date"] <= u[k], 1, np.where(d["date"] <= u[2 * k], 2, 3))
    print(f"run threshold +{100 * run:.0f}% in 15d from a quiet base | labelled stock-days {len(d)} | episodes {int(d.ep.sum())} (base rate {100 * d.ep.mean():.2f}%) | tickers with an episode {d[d.ep].ticker.nunique()}")
    print("episodes by population:", d[d.ep].groupby("pop").size().to_dict(), "| by block:", d[d.ep].groupby("blk").size().to_dict())
    print("episode stocks: median val20 (bn IDR) %.1f vs all %.1f | median ATR%% %.1f vs all %.1f | median price %.0f vs all %.0f" % (
        d[d.ep].val20.median() / 1e9, d.val20.median() / 1e9, 100 * d[d.ep].atr_pct.median(), 100 * d.atr_pct.median(), d[d.ep].close.median(), d.close.median()))
    if d.ep.sum() < 25:
        print("too few episodes for a scan"); return
    feats = [c for c in sweep.feature_columns(f1) if c in d.columns and not c.endswith(SIZE)] + ["rsi14", "dist_sma20", "range60_pos", "pctb", "pret_1d", "pret_5d", "log_val20"]
    feats = sorted(set(feats))
    r = scan(d.assign(SKIP_=0), 3)
    r = r[r.feature.isin(feats)].copy()
    r["abs_t"] = r.t.abs()
    print("\nfeatures at the signal day that separate episode starts from same-block/same-ATR stock-days (beta = change in probability, in pp, per 1 sd):")
    print(r.sort_values("abs_t", ascending=False).head(18)[["feature", "beta", "t", "signs", "same"]].round(2).to_string(index=False))
    # --- own-history version: feature at t0 minus the ticker's mean over [t0-70, t0-11] (what is NEW in the 10 days before the run, not who the stock is)
    PRICEDEF = {"past_ret_10d", "dist_sma20", "pctb", "rsi14", "range60_pos", "pret_1d", "pret_5d", "h_", "l_", "o_", "log_close", "log_val20"}
    base_cols = [c for c in feats if c not in PRICEDEF]
    d = d.sort_values(["ticker", "date"]).reset_index(drop=True)
    for c in base_cols:
        x = d[c].astype(float).replace([np.inf, -np.inf], np.nan)
        mu = x.groupby(d["ticker"]).transform(lambda v: v.shift(10).rolling(60, min_periods=30).mean())
        d[c + "_chg"] = x - mu
    chg = [c + "_chg" for c in base_cols]
    dd = d.dropna(subset=chg, how="all")
    r2 = scan(dd, 3); r2 = r2[r2.feature.isin(chg)].copy(); r2["abs_t"] = r2.t.abs()
    print(f"\nOWN-HISTORY CHANGE (feature minus the ticker's own mean over days -70..-11), rows {len(dd)}, episodes {int(dd.ep.sum())}:")
    print(r2.sort_values("abs_t", ascending=False).head(14)[["feature", "beta", "t", "signs", "same"]].round(2).to_string(index=False))
    # --- does the broker-side change survive controls for price/volume compression? (linear probability, stratum FE block x ATR tercile, ticker-clustered)
    ctrl = ["atr_pct_chg", "vol_ratio_chg", "day_ret_chg", "past_ret_10d", "range60_pos", "log_val20", "log_close"]
    for tgt in ("routine_buyavg_cv_chg", "churn_share_chg", "two_sym_chg", "buy_cls_foreign_inst_chg"):
        cols = [tgt] + ctrl
        q = dd.dropna(subset=cols + ["ep"]).copy()
        cell = (q["blk"].astype(str) + "_" + q["vol"].astype(str))
        M = q[cols].astype(float).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        for c in cols:
            lo, hi = M[c].quantile([.01, .99]); M[c] = ((M[c].clip(lo, hi) - M[c].clip(lo, hi).groupby(cell).transform("mean")) / M[c].clip(lo, hi).std())
        y = q["ep"].astype(float) - q["ep"].astype(float).groupby(cell).transform("mean")
        for lab, use in (("alone", [tgt]), ("with controls", cols)):
            X = M[use].to_numpy(); XtXi = np.linalg.inv(X.T @ X); b = XtXi @ X.T @ y.to_numpy(); e = y.to_numpy() - X @ b
            S = np.zeros((len(use),) * 2)
            for _, idx in pd.Series(np.arange(len(q))).groupby(q["ticker"].to_numpy()):
                s_ = X[idx.to_numpy()].T @ e[idx.to_numpy()]; S += np.outer(s_, s_)
            V = XtXi @ S @ XtXi
            print(f"   {tgt:<26} {lab:<14} beta {100 * b[0]:+.3f}pp per sd (t {b[0] / np.sqrt(V[0, 0]):+.1f})")
    x = dd["routine_buyavg_cv_chg"]
    dec = pd.qcut(x.rank(method="first"), 10, labels=False)
    print("   episode rate by decile of routine_buyavg_cv_chg (0 = most stable buyer price vs own norm):", " ".join(f"{100 * dd.loc[dec == k, 'ep'].mean():.2f}%" for k in range(10)), f"| base {100 * dd.ep.mean():.2f}%")
    print("\nepisode share among stock-days in the top decile of selected features (lift vs base rate):")
    for c in [x for x in r.sort_values("abs_t", ascending=False).feature if x not in PRICEDEF][:6]:
        x = d[c].astype(float).replace([np.inf, -np.inf], np.nan)
        for lab, m in (("top decile", x >= x.quantile(.9)), ("bottom decile", x <= x.quantile(.1))):
            print(f"   {c:<26} {lab:<14} episode rate {100 * d.loc[m, 'ep'].mean():.2f}% vs base {100 * d.ep.mean():.2f}% (n={int(m.sum())})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--run", type=float, default=0.30)
    main(ap.parse_args().run)
