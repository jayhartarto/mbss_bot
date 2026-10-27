"""
research/ipot_explore/lead_1d.py — investigate the 1-day sweep lead (23 bins with maxT p_adj < 0.05 on rel_1d = close(t+1)/open(t+1) - 1 minus the liquid-universe median of the date).
Liquidity is a CONTINUOUS control (log 20d value traded), never a gate. For each flagged feature (standardised, clipped at P1/P99):
  rel_1d (bp) ~ z(feature) with two-way fixed effects (ticker, date) and, in three nested specs, controls:
  (a) none | (b) + log val20, ATR% | (c) + past_ret_10d, pret_1d, pret_5d, vol_ratio, range60_pos, log close (tick size)
Two-way clustered (ticker, date) t. Then: per-tercile sign stability, the outside-top150 population, descriptive split by liquidity tercile (not a gate), and the size of the effect vs a 0.4% round-trip cost.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.lead_1d
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features, sweep, dna_shallow
from research.ipot_explore.mover_footprint import TOP, OUT


def demean2(M: np.ndarray, tk: np.ndarray, dt: np.ndarray, it: int = 6) -> np.ndarray:
    M = M.astype(float).copy()
    for _ in range(it):
        for key in (tk, dt):
            g = pd.DataFrame(M).groupby(key).transform("mean").to_numpy()
            M -= g
    return M


def twoway_t(X: np.ndarray, y: np.ndarray, tk: np.ndarray, dt: np.ndarray, j: int = 0):
    XtXi = np.linalg.inv(X.T @ X); b = XtXi @ X.T @ y; e = y - X @ b
    def meat(key):
        S = np.zeros((X.shape[1],) * 2)
        for _, idx in pd.Series(np.arange(len(y))).groupby(key):
            s = X[idx.to_numpy()].T @ e[idx.to_numpy()]; S += np.outer(s, s)
        return S
    inter = pd.factorize(pd.Series(tk).astype(str) + "|" + pd.Series(dt).astype(str))[0]
    V = XtXi @ (meat(tk) + meat(dt) - meat(inter)) @ XtXi
    v = V[j, j]
    return b[j], (b[j] / np.sqrt(v) if v > 0 else np.nan)


def prep(raw_db: str | None, feat_pkl: str | None) -> pd.DataFrame:
    if feat_pkl:
        f = pd.read_pickle(feat_pkl)
    else:
        raw = common.load_ipot(raw_db); px_all = common.load_ohlcv(start="2025-01-01"); px = px_all[px_all.ticker.isin(raw.ticker.unique())]
        f = features.ticker_day_features(raw, px).merge(features.rolling_pattern_features(raw, px), on=["ticker", "date"], how="left").merge(features.outcomes(px_all), on=["ticker", "date"], how="left")
    f = dna_shallow.add_technicals(f)
    ohlc = common.load_ohlcv(start="2025-01-01")[["ticker", "date", "open", "high", "low"]].rename(columns={"open": "o_", "high": "h_", "low": "l_"})
    f = f.merge(ohlc, on=["ticker", "date"], how="left")
    f["day_ret"] = f["close"] / f["o_"] - 1
    f["close_pos"] = (f["close"] - f["l_"]) / (f["h_"] - f["l_"]).replace(0, np.nan)
    f["close_vs_typ"] = f["close"] / ((f["h_"] + f["l_"] + f["close"]) / 3) - 1
    f["log_val20"] = np.log(f["val20"].clip(lower=1)); f["log_close"] = np.log(f["close"].clip(lower=1))
    f["y"] = f["rel_1d"] * 1e4
    return f.dropna(subset=["y"]).reset_index(drop=True)


def analyse(name: str, f: pd.DataFrame, feats: list[str]):
    specs = {"a none": [], "b +liq,ATR": ["log_val20", "atr_pct"], "c +price ctrl": ["log_val20", "atr_pct", "past_ret_10d", "pret_1d", "pret_5d", "vol_ratio", "range60_pos", "log_close"],
             "d +intraday ctrl": ["log_val20", "atr_pct", "past_ret_10d", "pret_1d", "pret_5d", "vol_ratio", "range60_pos", "log_close", "day_ret", "close_pos", "close_vs_typ"]}
    u = np.sort(f["date"].unique()); k = len(u) // 3
    blk = np.where(f["date"] <= u[k], 1, np.where(f["date"] <= u[2 * k], 2, 3))
    rows = []
    for c in feats:
        d = f[["ticker", "date", "y", c] + specs["d +intraday ctrl"]].copy()
        d[c] = d[c].astype(float).replace([np.inf, -np.inf], np.nan)
        d = d.dropna(); lo, hi = d[c].quantile([.01, .99]); d[c] = d[c].clip(lo, hi); d["z"] = (d[c] - d[c].mean()) / d[c].std()
        tk, dt = d["ticker"].to_numpy(), d["date"].to_numpy(); out = {"feature": c, "n": len(d)}
        for sn, cc in specs.items():
            cols = ["z"] + [x for x in cc]
            M = demean2(d[cols + ["y"]].to_numpy(), tk, dt)
            Z = (M[:, :-1] - 0.0); b, t = twoway_t(Z, M[:, -1], tk, dt)
            out[sn] = (b, t)
        zc = np.corrcoef(d["z"], d["log_val20"])[0, 1]; out["corr_liq"] = zc
        # sign stability per tercile under spec c
        sg = []
        bb = pd.Series(blk, index=f.index).reindex(d.index).to_numpy()
        for b_ in (1, 2, 3):
            m = bb == b_
            if m.sum() > 500:
                M = demean2(d.loc[m, ["z"] + specs["c +price ctrl"] + ["y"]].to_numpy(), tk[m], dt[m]); bcoef, _ = twoway_t(M[:, :-1], M[:, -1], tk[m], dt[m]); sg.append(bcoef)
        out["terciles"] = sg
        rows.append(out)
    print(f"\n===== {name}: effect of +1 sd of the feature on next-day open->close relative return (bp), two-way clustered t =====")
    print(f"{'feature':<26}{'n':>7} | {'a none':>16} | {'b +liq,ATR':>16} | {'c +price ctrl':>16} | {'d +intraday ctrl':>17} | corr(feat,logliq) | c by tercile (bp)")
    for o in sorted(rows, key=lambda o: -abs(o["c +price ctrl"][1])):
        print(f"{o['feature']:<26}{o['n']:>7} | {o['a none'][0]:+7.1f} (t{o['a none'][1]:+5.1f}) | {o['b +liq,ATR'][0]:+7.1f} (t{o['b +liq,ATR'][1]:+5.1f}) | {o['c +price ctrl'][0]:+7.1f} (t{o['c +price ctrl'][1]:+5.1f}) | {o['d +intraday ctrl'][0]:+7.1f} (t{o['d +intraday ctrl'][1]:+5.1f}) | {o['corr_liq']:+.2f} | "
              + " ".join(f"{x:+.1f}" for x in o["terciles"]))
    return rows


def main():
    f1 = prep(None, common.OUT_DIR + "/features.pkl")
    res = sweep.run(pd.read_pickle(common.OUT_DIR + "/features.pkl"), B=300, verbose=False)
    r1 = res[(res.outcome == "rel_1d") & (res.p_adj_maxT < 0.05)].copy(); r1["diff_bp"] = r1["diff"] * 1e4
    print("sweep bins flagged on rel_1d (diff in bp vs overall, maxT p_adj):")
    print(r1.sort_values("p_adj_maxT")[["feature", "bin", "n", "diff_bp", "t", "p_adj_maxT"]].round(2).to_string(index=False))
    feats = sorted(set(r1.feature))
    rows = analyse("top150 (1 year)", f1, feats)
    f2 = prep(OUT, None)
    analyse("outside top150 (3 months)", f2, [c for c in feats if c in f2.columns])
    print("\ndescriptive (NOT a gate): effect under spec (c) by liquidity tercile of the stock (log val20), top150 population")
    f1["liq_t"] = pd.qcut(f1["log_val20"], 3, labels=["low", "mid", "high"])
    for c in feats[:6]:
        out = []
        for lt, g in f1.groupby("liq_t", observed=True):
            d = g[["ticker", "date", "y", c, "log_val20", "atr_pct", "past_ret_10d", "pret_1d", "pret_5d", "vol_ratio", "range60_pos", "log_close"]].replace([np.inf, -np.inf], np.nan).dropna()
            lo, hi = d[c].quantile([.01, .99]); d[c] = d[c].clip(lo, hi); d["z"] = (d[c] - d[c].mean()) / d[c].std()
            M = demean2(d[["z", "log_val20", "atr_pct", "past_ret_10d", "pret_1d", "pret_5d", "vol_ratio", "range60_pos", "log_close", "y"]].to_numpy(), d["ticker"].to_numpy(), d["date"].to_numpy())
            b, t = twoway_t(M[:, :-1], M[:, -1], d["ticker"].to_numpy(), d["date"].to_numpy()); out.append(f"{lt} {b:+.1f}bp (t{t:+.1f})")
        print(f"   {c:<26} " + " | ".join(out))
    print("\nscale: a 0.4% round trip = 40 bp; mean |effect| of the flagged features above, spec c, is the number to compare.")


if __name__ == "__main__":
    main()
