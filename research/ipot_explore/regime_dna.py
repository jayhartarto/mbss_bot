"""
research/ipot_explore/regime_dna.py — experiment family EXP-REGDNA-1 (registered before running): conviction picks for a quick +5% inside <10 days, models trained per IHSG regime, bandarmology as ONE feature group among several.

Target / outcome: from open(t+1) the stock reaches +5% before -5% within 10 trading days (same-day touch = stop; timeout exits at the close of day 10); net of 0.4% round trip.
Feature groups: PRICE (rsi14, dist_sma20, atr_pct, range60_pos, pctb, pret_1d/5d, past_ret_10d, vol_ratio, log val20, log close, day_ret, close_pos, breakout/trigger flags H20 H60 R5V R10 R20 V5 V10 BB GC S20 X5 X10 X20)
and BROKER (scale-free broker features + XL top buyer / top seller flags). Regime = IHSG phase of the signal date (BEARISH_GRIND, BOUNCE_OFF_LOW, CRASH_FAST, NEUTRAL_BULLISH, PULLBACK_IN_BOUNCE), known at the close of t.
Models (ridge, alpha 300, cross-sectionally rank-normalised features, target = the 0/1 outcome centred): G_P / G_PB = one global model (+ regime dummies); R_P / R_PB = one model per regime trained only on earlier rows of that regime.
Walk-forward over 5 date blocks (test blocks 2-5), 12-day purge. Picks: the top 3 scores per test date (a ticker picked in the previous 5 trading days is skipped). Reported per regime: picks, hit rate vs the hit rate of ALL
stock-days with the same regime and ATR tercile (excess, ticker-clustered t), mean net return, and the same for the baselines random pick / highest ATR. Liquidity is only a feature.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.regime_dna
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, sweep
from research.ipot_explore import gorengan_trade as gt
from research.ipot_explore.breakout_backing import triggers, COST
from research.ipot_explore.breakout_backing2 import triggers2
from research.ipot_explore.breakout_exit import triggers3
from research.ipot_explore.m012 import PRICE, SIZE, rank_norm, ridge
from research.ipot_explore.mover_footprint import TOP, OUT
from research.ipot_explore.xl_seller import xl_flags
from research.ipot_explore.xl_study import ihsg_phase

FLAGS = ["H20", "H60", "R5V", "R10", "R20", "V5", "V10", "BB", "GC", "S20", "X5", "X10", "X20"]
REGS = ["BEARISH_GRIND", "BOUNCE_OFF_LOW", "CRASH_FAST", "NEUTRAL_BULLISH", "PULLBACK_IN_BOUNCE"]
PURGE = 12
K = 3


def pick_idx(day: pd.DataFrame, score: np.ndarray, k: int, recent: dict, t_today: int) -> list[int]:
    chosen = []
    for j in np.argsort(-score):
        if t_today - recent.get(day["ticker"].iloc[j], -99) < 5:
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
    TR = triggers(sub).merge(triggers2(sub), on=["ticker", "date"], how="outer").merge(triggers3(sub), on=["ticker", "date"], how="outer")
    f = f.merge(TR[["ticker", "date"] + FLAGS], on=["ticker", "date"], how="left")
    for c in FLAGS:
        f[c] = f[c].fillna(False).astype(float)
    f = f.merge(ihsg_phase(), on="date", how="left")
    f["regime_phase"] = f["regime_phase"].fillna("NA")
    gt.MECH.clear()
    gt.MECH.update({"TP5S5": (0.05, -0.05, 10)})
    D = f.merge(gt.sim_all(sub), on=["ticker", "date"], how="left").dropna(subset=["TP5S5"]).reset_index(drop=True)
    D["win"] = (D["TP5S5"] >= 0.0499).astype(float)
    D["t"] = D["date"].map(tdi)
    D["atrq"] = pd.qcut(D["atr_pct"], 3, labels=False, duplicates="drop")
    broker = [c for c in sweep.feature_columns(D) if c not in PRICE and not c.endswith(SIZE) and c not in ("win", "t", "atrq", "y", "val20", "score", "blk", "vol", "TP5S5") and c not in ("day_ret", "close_pos", "close_vs_typ")
              and not c.startswith(("o_", "h_", "l_", "ret_", "rel_", "mfe_")) and c not in FLAGS + ["XLB", "XLS"] and not c.endswith(("_chg", "_r"))]
    price_cols = [c for c in PRICE if c in D.columns] + ["day_ret", "close_pos"]
    reg_dum = pd.DataFrame({f"reg_{r}": (D["regime_phase"] == r).astype(float) for r in REGS})
    XP = pd.concat([rank_norm(D, price_cols), D[FLAGS], reg_dum], axis=1)
    XPB = pd.concat([rank_norm(D, price_cols + broker), D[FLAGS + ["XLB", "XLS"]], reg_dum], axis=1)
    u = np.sort(D["date"].unique())
    edges = np.linspace(0, len(u), 6).astype(int)
    print(f"rows {len(D)} ({D.ticker.nunique()} tickers, {D.date.min().date()}..{D.date.max().date()}) | base TP5-before-SL5 hit {100 * D.win.mean():.1f}%, mean net {100 * (D.TP5S5.mean() - COST):+.2f}% | broker feats {len(broker)}")
    print("rows / dates per regime:", {r: (int((D.regime_phase == r).sum()), int(D[D.regime_phase == r].date.nunique())) for r in REGS})
    models = {"G_P": (XP, False), "G_PB": (XPB, False), "R_P": (XP, True), "R_PB": (XPB, True)}
    out = {m: [] for m in models}
    for fold in range(1, 5):
        te_d = u[edges[fold]:edges[fold + 1]]
        tr_d = u[:max(edges[fold] - PURGE, 0)]
        trm = D["date"].isin(tr_d).to_numpy()
        tem = D["date"].isin(te_d).to_numpy()
        for name, (X, per_reg) in models.items():
            A = X.to_numpy(float)
            T = D.loc[tem].copy()
            s = np.full(tem.sum(), np.nan)
            if not per_reg:
                w = ridge(A[trm], D.loc[trm, "win"].to_numpy() - D.loc[trm, "win"].mean(), 300.0)
                s = A[tem] @ w
            else:
                reg_te = D.loc[tem, "regime_phase"].to_numpy()
                for r in REGS:
                    mtr = trm & (D["regime_phase"] == r).to_numpy()
                    mte = reg_te == r
                    if mtr.sum() < 600 or mte.sum() == 0:
                        continue
                    w = ridge(A[mtr], D.loc[mtr, "win"].to_numpy() - D.loc[mtr, "win"].mean(), 300.0)
                    s[mte] = A[tem][mte] @ w
            T["s"] = s
            out[name].append(T)
    T0 = pd.concat(out["G_P"]).reset_index(drop=True)
    cellhit = T0.groupby(["regime_phase", "atrq"])["win"].mean()
    rng = np.random.default_rng(7)

    def evaluate(name, T, score):
        T = T.copy()
        T["s"] = score
        rows = []
        recent = {}
        for dt, g in T.sort_values("date").groupby("date", sort=True):
            g = g.reset_index(drop=True)
            if g["s"].isna().all():
                continue
            sc = g["s"].fillna(-9e9).to_numpy()
            for j in pick_idx(g, sc, K, recent, int(g["t"].iloc[0])):
                recent[g["ticker"].iloc[j]] = int(g["t"].iloc[0])
                r = g.iloc[j]
                rows.append((r.regime_phase, r.ticker, r.win, r.TP5S5 - COST, cellhit.get((r.regime_phase, r.atrq), np.nan), r.atr_pct))
        R = pd.DataFrame(rows, columns=["reg", "ticker", "win", "net", "base", "atr"])
        return R

    def line(R, label):
        ex = R.win - R.base
        mu = ex.mean()
        cs = (ex - mu).groupby(R.ticker.to_numpy()).sum()
        t = mu / (np.sqrt((cs ** 2).sum()) / len(R)) if len(R) > 3 else np.nan
        return f"{label:<20} picks {len(R):>4} | hit {100 * R.win.mean():4.1f}% (cell base {100 * R.base.mean():4.1f}%, excess {100 * mu:+.1f}pp t {t:+.1f}) | net {100 * R.net.mean():+.2f}% | ATR {100 * R.atr.mean():.1f}%"

    ev = {
        "random": evaluate("random", T0, rng.random(len(T0))),
        "ATR top": evaluate("atr", T0, T0["atr_pct"].to_numpy()),
    }
    for name in models:
        Tm = pd.concat(out[name]).reset_index(drop=True)
        ev[name] = evaluate(name, Tm, Tm["s"].to_numpy())
    print(f"\n=== POOLED over all regimes (top {K} per day) ===")
    for k_, R in ev.items():
        print("  " + line(R, k_))
    for r in REGS:
        print(f"\n=== regime {r} ===")
        for k_, R in ev.items():
            x = R[R.reg == r]
            if len(x) >= 15:
                print("  " + line(x, k_))
            else:
                print(f"  {k_:<20} picks {len(x)} (too few)")
    print("\nbroker ablation (PB minus P) in mean net return per pick, global and per-regime models:")
    for a, b in (("G_PB", "G_P"), ("R_PB", "R_P")):
        for r in ["ALL"] + REGS:
            xa = ev[a] if r == "ALL" else ev[a][ev[a].reg == r]
            xb = ev[b] if r == "ALL" else ev[b][ev[b].reg == r]
            if len(xa) >= 15 and len(xb) >= 15:
                print(f"   {a} - {b} {r:<20} {100 * (xa.net.mean() - xb.net.mean()):+.2f}pp (picks {len(xa)} / {len(xb)})")


if __name__ == "__main__":
    main()
