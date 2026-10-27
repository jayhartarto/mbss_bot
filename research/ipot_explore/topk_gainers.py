"""
research/ipot_explore/topk_gainers.py — can a filter pick 1-5 stocks per day that reach a big gain? (experiment family EXP-TOPK-1, registered before running)

Target ("big gainer"): from open(t+1), the highest high of the next 5 trading days is >= +10%  (MFE5 >= 10%).  Tradable outcome of each pick: TP +10% / SL -7% / 10 days (same-day touch = stop), entry open t+1, net of 0.4% round trip.
Models (all fitted ONLY on earlier dates; 5 chronological date blocks, folds B1->B2, B1+B2->B3, ..., purge 12 trading days before each test block; every test date is scored exactly once):
  P   price/volume/trigger model: rsi14, dist_sma20, atr_pct, range60_pos, pctb, pret_1d/5d, past_ret_10d, vol_ratio, log val20, log close, trigger flags (H20, H60, R5V, R10, R20, V5, V10, BB, GC, S20, X5, X10, X20), IHSG regime dummies
  PB  P + scale-free broker features (+ XL top buyer / top seller flags)
  Two learners: ridge (linear probability, alpha 300) and a small gradient-boosted tree (HistGradientBoosting, 150 iterations, depth 3, lr 0.05) as a challenger.
Selection: on every test date take the k highest scores (k = 1, 3, 5), skipping a ticker picked during the previous 5 trading days. Reported: picks, precision (share with MFE5 >= 10%) vs the base rate of ALL stock-days
on the same dates, mean net return of the TP10/SL7/10d mechanic, win rate, and a date-block bootstrap 90% CI for the precision lift (block = 5 days). Liquidity is not a gate; log val20 is just a feature.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.topk_gainers
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from research.ipot_explore import common, sweep
from research.ipot_explore import gorengan_trade as gt
from research.ipot_explore.breakout_backing import triggers, COST
from research.ipot_explore.breakout_backing2 import triggers2
from research.ipot_explore.breakout_exit import triggers3
from research.ipot_explore.m012 import PRICE, REG, SIZE, rank_norm, ridge
from research.ipot_explore.mover_footprint import TOP, OUT
from research.ipot_explore.xl_seller import xl_flags
from research.ipot_explore.xl_study import ihsg_phase

FLAGS = ["H20", "H60", "R5V", "R10", "R20", "V5", "V10", "BB", "GC", "S20", "X5", "X10", "X20"]
PURGE = 12


def mfe5(px: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for tk, g in px.sort_values(["ticker", "date"]).groupby("ticker"):
        o, h = g["open"].to_numpy(), g["high"].to_numpy()
        n = len(g)
        v = np.full(n, np.nan)
        for i in range(n - 6):
            if o[i + 1] > 0:
                v[i] = h[i + 1:i + 6].max() / o[i + 1] - 1
        rows.append(pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy(), "mfe5": v}))
    return pd.concat(rows, ignore_index=True)


def pick(day: pd.DataFrame, score: np.ndarray, k: int, recent: dict, tdi_today: int) -> list[int]:
    order = np.argsort(-score)
    chosen = []
    for j in order:
        tk = day["ticker"].iloc[j]
        if tdi_today - recent.get(tk, -99) < 5:
            continue
        chosen.append(j)
        if len(chosen) == k:
            break
    return chosen


def main():
    f, pxs, px_all, tdi = gt.build()
    raw = pd.concat([common.load_ipot(TOP), common.load_ipot(OUT)], ignore_index=True)
    f = f.merge(xl_flags(raw), on=["ticker", "date"], how="left")
    for c in ("XLB", "XLS"):
        f[c] = f[c].fillna(False).astype(float)
    sub = pxs[pxs.ticker.isin(f.ticker.unique())]
    f = f.merge(triggers(sub).merge(triggers2(sub), on=["ticker", "date"], how="outer").merge(triggers3(sub), on=["ticker", "date"], how="outer"), on=["ticker", "date"], how="left")
    for c in FLAGS:
        f[c] = f[c].fillna(False).astype(float)
    reg = ihsg_phase()
    f = f.merge(reg, on="date", how="left")
    for r in REG:
        f[f"reg_{r}"] = (f["regime_phase"] == r).astype(float)
    gt.MECH.clear()
    gt.MECH.update({"TP10S7": (0.10, -0.07, 10)})
    D = f.merge(mfe5(sub), on=["ticker", "date"], how="left").merge(gt.sim_all(sub), on=["ticker", "date"], how="left")
    D = D.dropna(subset=["mfe5", "TP10S7"]).reset_index(drop=True)
    D["gain"] = (D["mfe5"] >= 0.10).astype(float)
    D["t"] = D["date"].map(tdi)
    broker = [c for c in sweep.feature_columns(D) if c not in PRICE and not c.endswith(SIZE) and c not in ("mfe5", "gain", "t", "y", "day_ret", "close_pos", "close_vs_typ", "TP10S7", "score", "blk", "vol", "val20")
              and not c.startswith(("reg_", "o_", "h_", "l_", "ret_", "rel_", "mfe_")) and c not in FLAGS and c not in ("XLB", "XLS") and not c.endswith(("_chg", "_r", "_pct"))]
    cont_p = [c for c in PRICE if c in D.columns]
    P_cols = cont_p
    PB_cols = cont_p + broker
    flag_cols = FLAGS + [f"reg_{r}" for r in REG]
    print(f"rows {len(D)} ({D.ticker.nunique()} tickers, {D.date.min().date()}..{D.date.max().date()}) | base rate MFE5>=10%: {100 * D.gain.mean():.1f}% | TP10/SL7/10d net mean {100 * (D.TP10S7.mean() - COST):+.2f}% | broker feats {len(broker)}")
    XP = pd.concat([rank_norm(D, P_cols), D[flag_cols]], axis=1)
    XPB = pd.concat([rank_norm(D, PB_cols), D[flag_cols + ["XLB", "XLS"]]], axis=1)
    u = np.sort(D["date"].unique())
    edges = np.linspace(0, len(u), 6).astype(int)
    models = {"P ridge": (XP, "ridge"), "PB ridge": (XPB, "ridge"), "P tree": (XP, "tree"), "PB tree": (XPB, "tree")}
    out = {m: [] for m in models}
    for fold in range(1, 5):
        te_d = u[edges[fold]:edges[fold + 1]]
        tr_d = u[:max(edges[fold] - PURGE, 0)]
        tr = D["date"].isin(tr_d).to_numpy()
        te = D["date"].isin(te_d).to_numpy()
        for name, (X, kind) in models.items():
            A = X.to_numpy(float)
            if kind == "ridge":
                w = ridge(A[tr], D.loc[tr, "gain"].to_numpy() - D.loc[tr, "gain"].mean(), 300.0)
                s = A[te] @ w
            else:
                clf = HistGradientBoostingClassifier(max_iter=150, max_depth=3, learning_rate=0.05, random_state=7).fit(A[tr], D.loc[tr, "gain"].to_numpy())
                s = clf.predict_proba(A[te])[:, 1]
            T = D.loc[te].copy()
            T["s"] = s
            out[name].append(T)
    print(f"test dates: {sum(len(set(x.date)) for x in out['P ridge'])} (folds on blocks 2-5)")
    rng = np.random.default_rng(7)

    def report(name, T, score):
        T = T.copy()
        T["s"] = score
        T = T.sort_values("date").reset_index(drop=True)
        base_by_date = T.groupby("date")["gain"].mean()
        print(f"\n=== {name} ===")
        for k in (1, 3, 5):
            recent, rows = {}, []
            for dt, g in T.groupby("date"):
                g = g.reset_index(drop=True)
                idx = pick(g, g["s"].to_numpy(), k, recent, int(g["t"].iloc[0]))
                for j in idx:
                    recent[g["ticker"].iloc[j]] = int(g["t"].iloc[0])
                    rows.append((dt, g["gain"].iloc[j], g["mfe5"].iloc[j], g["TP10S7"].iloc[j], base_by_date[dt], float(g["ret_5d"].iloc[j] >= 0.10), g["atr_pct"].iloc[j]))
            R = pd.DataFrame(rows, columns=["date", "gain", "mfe5", "ret", "base", "close10", "atr"])
            days = np.array(sorted(R.date.unique()))
            by = R.assign(lift=R.gain - R.base).groupby("date")["lift"].agg(["sum", "count"])
            boots = []
            nb = int(np.ceil(len(days) / 5))
            for _ in range(300):
                starts = rng.integers(0, len(days) - 4, nb)
                x = by.loc[np.concatenate([days[s:s + 5] for s in starts])]
                boots.append(x["sum"].sum() / x["count"].sum())
            lo, hi = np.percentile(boots, [5, 95])
            print(f"  k={k}: picks {len(R):>4} | MFE5>=10% {100 * R.gain.mean():4.1f}% (base {100 * R.base.mean():4.1f}%, lift {100 * (R.gain - R.base).mean():+.1f}pp CI [{100 * lo:+.1f},{100 * hi:+.1f}]) "
                  f"| CLOSE-based ret5>=10% {100 * R.close10.mean():4.1f}% | mean ATR {100 * R.atr.mean():.1f}% | TP10/SL7/10d net {100 * (R.ret.mean() - COST):+.2f}% win {100 * ((R.ret - COST) > 0).mean():.0f}%")

    T0 = pd.concat(out["P ridge"]).reset_index(drop=True)
    print(f"test dates {T0.date.nunique()} | all test stock-days: MFE5>=10% {100 * T0.gain.mean():.1f}%, CLOSE-based ret5>=10% {100 * (T0.ret_5d >= 0.10).mean():.1f}%, mean ATR {100 * T0.atr_pct.mean():.1f}%, TP10/SL7/10d net {100 * (T0.TP10S7.mean() - COST):+.2f}%")
    report("BASELINE random pick", T0, rng.random(len(T0)))
    report("BASELINE highest ATR% (volatility only)", T0, T0["atr_pct"].to_numpy())
    report("BASELINE lowest price (log close)", T0, -T0["log_close"].to_numpy())
    report("BASELINE highest volume ratio", T0, T0["vol_ratio"].fillna(0).to_numpy())
    for name in models:
        T = pd.concat(out[name]).reset_index(drop=True)
        report(name, T, T["s"].to_numpy())

if __name__ == "__main__":
    main()
