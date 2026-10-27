"""
research/ipot_explore/m012.py — incremental-value test (brief section 7 stage C, hypothesis H8): does adding broker features to a price/volume model improve out-of-sample ranking of next returns?

Models (all ridge regression, alpha fixed a priori = 300, on cross-sectionally rank-normalised features; the target is the per-date rank of rel_{h}d):
  M0  ticker historical mean of the target in the TRAIN period (no features)       M1  price/volume: rsi14, dist_sma20, atr_pct, range60_pos, pctb, pret_1d, pret_5d, past_ret_10d, vol_ratio, log val20, log close, IHSG regime dummies
  M2  M1 + the scale-free broker features of the sweep                              M3  broker features only
Evaluation: 5 chronological date blocks; folds B1->B2, B1+B2->B3, B1..B3->B4, B1..B4->B5; train rows within `embargo` trading days before the test block are purged. Metrics on the TEST dates:
  daily Spearman rank IC (mean, Newey-West t with lag = horizon), top-quintile minus bottom-quintile realised rel return per date, top-quintile mean raw ret_h net of 0.4% cost vs all-rows mean,
  and the paired difference M2-M1 in daily IC / top-quintile return (NW t). Liquidity is a continuous input, never a gate.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.m012
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, sweep
from research.ipot_explore.lead_1d import prep
from research.ipot_explore.mover_footprint import TOP, OUT
from research.ipot_explore.xl_study import ihsg_phase

ALPHA = 300.0
COST = 0.004
PRICE = ["rsi14", "dist_sma20", "atr_pct", "range60_pos", "pctb", "pret_1d", "pret_5d", "past_ret_10d", "vol_ratio", "log_val20", "log_close"]
REG = ["BEARISH_GRIND", "BOUNCE_OFF_LOW", "CRASH_FAST", "NEUTRAL_BULLISH", "PULLBACK_IN_BOUNCE"]
SIZE = ("_vis", "_mid", "_avg", "_n5")


def rank_norm(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    g = df.groupby("date")
    out = pd.DataFrame(index=df.index)
    for c in cols:
        r = g[c].rank(pct=True, method="average")
        out[c] = ((r - 0.5) * np.sqrt(12)).fillna(0.0)
    return out


def nw_t(x: np.ndarray, lag: int) -> float:
    x = np.asarray(x, float); x = x[~np.isnan(x)]; n = len(x)
    if n < 10:
        return np.nan
    m = x.mean(); e = x - m; v = (e @ e) / n
    for k in range(1, min(lag, n - 1) + 1):
        v += 2 * (1 - k / (lag + 1)) * (e[k:] @ e[:-k]) / n
    return m / np.sqrt(v / n) if v > 0 else np.nan


def ridge(X: np.ndarray, y: np.ndarray, a: float) -> np.ndarray:
    return np.linalg.solve(X.T @ X + a * np.eye(X.shape[1]), X.T @ y)


def daily_metrics(te: pd.DataFrame, pred: np.ndarray, ycol: str, rcol: str) -> pd.DataFrame:
    d = te[["date", ycol, rcol]].copy(); d["pred"] = pred
    rows = []
    for dt, g in d.groupby("date"):
        if len(g) < 40:
            continue
        ic = g["pred"].rank().corr(g[ycol].rank())
        q = g["pred"].rank(pct=True)
        top, bot = g[q > 0.8], g[q <= 0.2]
        rows.append((dt, ic, top[ycol].mean() - bot[ycol].mean(), top[rcol].mean() - COST, g[rcol].mean() - COST, (top[rcol] - COST > 0).mean()))
    return pd.DataFrame(rows, columns=["date", "ic", "spread", "top_net", "all_net", "top_win"]).set_index("date")


def main():
    f1 = prep(None, common.OUT_DIR + "/features.pkl"); f2 = prep(OUT, None)
    f = pd.concat([f1, f2[~f2.set_index(["ticker", "date"]).index.isin(f1.set_index(["ticker", "date"]).index)]], ignore_index=True)
    reg = ihsg_phase(); f = f.merge(reg, on="date", how="left"); f["regime_phase"] = f["regime_phase"].fillna("NA")
    for r in REG:
        f[f"reg_{r}"] = (f["regime_phase"] == r).astype(float)
    broker = [c for c in sweep.feature_columns(f1) if c in f.columns and c not in PRICE and not c.endswith(SIZE) and c not in ("past_ret_10d", "val20", "log_val20", "log_close", "y", "day_ret", "close_pos", "close_vs_typ")
              and c not in ("rsi14", "dist_sma20", "atr_pct", "range60_pos", "pctb", "pret_1d", "pret_5d") and not c.startswith(("reg_", "o_", "h_", "l_"))]
    print(f"pooled rows {len(f)} ({f.ticker.nunique()} tickers, {f.date.min().date()}..{f.date.max().date()}) | price feats {len(PRICE)}+{len(REG)} regime | broker feats {len(broker)}")
    sets = {"M1 price/vol+IHSG": PRICE + [f"reg_{r}" for r in REG], "M2 M1+broker": PRICE + [f"reg_{r}" for r in REG] + broker, "M3 broker only": broker}
    for h in (1, 5, 10):
        ycol, rcol = f"rel_{h}d", f"ret_{h}d"
        d = f.dropna(subset=[ycol, rcol]).reset_index(drop=True)
        d["yrank"] = d.groupby("date")[ycol].rank(pct=True) - 0.5
        X = {k: rank_norm(d, [c for c in v if not c.startswith("reg_")]) for k, v in sets.items()}
        for k, v in sets.items():
            for c in [c for c in v if c.startswith("reg_")]:
                X[k][c] = d[c].to_numpy()
        u = np.sort(d["date"].unique()); nb = 5; edges = np.linspace(0, len(u), nb + 1).astype(int)
        res = {k: [] for k in list(sets) + ["M0 ticker hist mean", "M2w M1+broker (own history)", "M3w broker (own history)"]}
        for fold in range(1, nb):
            te_d = u[edges[fold]:edges[fold + 1]]; tr_d = u[:max(edges[fold] - h - 2, 0)]
            tr = d["date"].isin(tr_d).to_numpy(); te = d["date"].isin(te_d).to_numpy()
            if tr.sum() < 3000 or te.sum() < 500:
                continue
            mu = d.loc[tr].groupby("ticker")["yrank"].mean(); p0 = d.loc[te, "ticker"].map(mu).fillna(0.0).to_numpy()
            res["M0 ticker hist mean"].append(daily_metrics(d.loc[te], p0, ycol, rcol))
            for k in sets:
                cols = list(X[k].columns); A = X[k][cols].to_numpy(); w = ridge(A[tr], d.loc[tr, "yrank"].to_numpy(), ALPHA)
                res[k].append(daily_metrics(d.loc[te], A[te] @ w, ycol, rcol))
            # own-history version: broker features minus the ticker's TRAIN mean (flow relative to the stock's own norm), so a static ticker trait (foreign-owned large cap) cannot carry the signal
            mu_b = d.loc[tr].groupby("ticker")[broker].mean()
            dm = d[broker].astype(float).replace([np.inf, -np.inf], np.nan) - mu_b.reindex(d["ticker"]).to_numpy()
            dm["date"] = d["date"].to_numpy(); Xw = rank_norm(dm, broker)
            Xp = X["M1 price/vol+IHSG"]
            for name, parts in (("M2w M1+broker (own history)", [Xp, Xw]), ("M3w broker (own history)", [Xw])):
                A = pd.concat(parts, axis=1).to_numpy(); w = ridge(A[tr], d.loc[tr, "yrank"].to_numpy(), ALPHA)
                res[name].append(daily_metrics(d.loc[te], A[te] @ w, ycol, rcol))
        R = {k: pd.concat(v) for k, v in res.items() if v}
        print(f"\n######## horizon {h}d | OOS dates {len(R['M1 price/vol+IHSG'])} (folds on blocks 2-5 of 5) | all-rows mean ret net of cost {100 * R['M1 price/vol+IHSG'].all_net.mean():+.2f}% ########")
        print(f"{'model':<22}{'mean IC':>9}{'NW t':>7}{'IC>0 share':>12}{'top-bot spread':>16}{'top20% net ret':>16}{'top20% win':>11}")
        for k, m in R.items():
            print(f"{k:<22}{m.ic.mean():>9.4f}{nw_t(m.ic.to_numpy(), h):>7.1f}{(m.ic > 0).mean():>12.2f}{100 * m.spread.mean():>15.2f}%{100 * m.top_net.mean():>15.2f}%{100 * m.top_win.mean():>10.0f}%")
        for lab, key in (("M2 - M1", "M2 M1+broker"), ("M2w - M1 (own history)", "M2w M1+broker (own history)")):
            a2, b2 = R[key], R["M1 price/vol+IHSG"]; ix = a2.index.intersection(b2.index)
            dic2 = (a2.loc[ix, "ic"] - b2.loc[ix, "ic"]).to_numpy(); dtop2 = (a2.loc[ix, "top_net"] - b2.loc[ix, "top_net"]).to_numpy()
            print(f"PAIRED {lab:<22}: IC {np.nanmean(dic2):+.4f} (NW t {nw_t(dic2, h):+.1f}) | top20% net ret {100 * dtop2.mean():+.2f}pp (t {nw_t(dtop2, h):+.1f})")
        a, b = R["M2 M1+broker"], R["M1 price/vol+IHSG"]
        idx = a.index.intersection(b.index)
        dic = (a.loc[idx, "ic"] - b.loc[idx, "ic"]).to_numpy(); dtop = (a.loc[idx, "top_net"] - b.loc[idx, "top_net"]).to_numpy(); dsp = (a.loc[idx, "spread"] - b.loc[idx, "spread"]).to_numpy()
        print(f"PAIRED M2 - M1: IC {dic.mean():+.4f} (NW t {nw_t(dic, h):+.1f}) | spread {100 * dsp.mean():+.2f}pp (t {nw_t(dsp, h):+.1f}) | top20% net ret {100 * dtop.mean():+.2f}pp (t {nw_t(dtop, h):+.1f})")
        # per fold stability of the paired IC difference
        per = []
        for q in range(4):
            seg = idx[q * len(idx) // 4:(q + 1) * len(idx) // 4]
            per.append((a.loc[seg, "ic"] - b.loc[seg, "ic"]).mean())
        print("   paired IC diff by OOS quarter:", " ".join(f"{x:+.4f}" for x in per))


if __name__ == "__main__":
    main()
