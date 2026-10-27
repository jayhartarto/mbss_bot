"""
research/ipot_explore/breakout_backing2.py — experiment family EXP-BRK-2 (registered before running): faster confirmations, extreme volume, and a LOOSER quiet-accumulation marker.
New triggers (first event per ticker per type, 20-trading-day cooldown; known at the close of t, entry open t+1):
  MA5   first close >1% above MA5 after 5 days with no close above it, MA5 rising over 3 days        MA10  first close >1.5% above MA10 after 7 days with no close above it, MA10 rising over 3 days
  V5    volume >= 5x the previous-20-day mean and a day return >= +3%                                V10   volume >= 10x the previous-20-day mean and a day return >= +3%
Markers: B1 (top decile of the quiet-accumulation score, as before) | B1L (top 30% of the date) | B1M (at least 2 of the 3 components ranked in the top 40%) | B4 (domestic-led: buy_cls_foreign_inst <= P33) | B5 (XL #1 net buyer).
Grid: old triggers (MA20, H20, H60, R5V, R10, R20, BB, GC) x {B1L, B1M}; new triggers x {B1, B1L, B1M, B4, B5}; two outcomes (rel_10d %, TP+8%/SL-6%/10d net of 0.4%). BH over the whole family; pooled marker effects with trigger x tercile fixed effects.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.breakout_backing2
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features
from research.ipot_explore import gorengan_trade as gt
from research.ipot_explore.breakout_backing import triggers, norm_p, bh, diff_cluster, COST, COOL
from research.ipot_explore.mover_footprint import TOP, OUT


def triggers2(px: pd.DataFrame) -> pd.DataFrame:
    out = []
    for tk, g in px.sort_values(["ticker", "date"]).groupby("ticker"):
        if len(g) < 60:
            continue
        c, v = g["close"], g["volume"]
        ma5, ma10 = c.rolling(5).mean(), c.rolling(10).mean()
        vm = v.shift(1).rolling(20).mean()
        ret1 = c.pct_change()
        T = pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy()})
        T["MA5"] = ((c > ma5 * 1.01) & ((c > ma5).astype(float).shift(1).rolling(5).sum() == 0) & (ma5 > ma5.shift(3))).to_numpy()
        T["MA10"] = ((c > ma10 * 1.015) & ((c > ma10).astype(float).shift(1).rolling(7).sum() == 0) & (ma10 > ma10.shift(3))).to_numpy()
        T["V5"] = ((v >= 5 * vm) & (vm > 0) & (ret1 >= 0.03)).to_numpy()
        T["V10"] = ((v >= 10 * vm) & (vm > 0) & (ret1 >= 0.03)).to_numpy()
        for col in ("MA5", "MA10", "V5", "V10"):
            idx = np.where(T[col].fillna(False).to_numpy())[0]
            keep, last = [], -999
            for i in idx:
                if i - last >= COOL:
                    keep.append(i)
                    last = i
            flag = np.zeros(len(T), bool)
            flag[keep] = True
            T[col] = flag
        out.append(T)
    return pd.concat(out, ignore_index=True)


def main():
    f, pxs, px_all, tdi = gt.build()
    f = f.copy()
    raw = pd.concat([common.load_ipot(TOP), common.load_ipot(OUT)], ignore_index=True)
    a = features.broker_day_table(raw, "all", "all")
    a = a[a["net_val_mid"] > 0].copy()
    a["rk"] = a.groupby(["ticker", "date"])["net_val_mid"].rank(ascending=False, method="first")
    xl = a[(a.rk == 1) & (a.broker == "XL")][["ticker", "date"]].drop_duplicates().assign(B5=True)
    f = f.merge(xl, on=["ticker", "date"], how="left")
    f["B5"] = f["B5"].fillna(False).astype(bool)
    rc = [c + "_r" for c in ("routine_buyavg_cv", "churn_share", "two_sym")]
    f["B1"] = f["sig"].astype(bool)
    f["B1L"] = f.groupby("date")["score"].rank(pct=True) >= 0.7
    f["B1M"] = (sum((f[c] >= 0.6).astype(int) for c in rc) >= 2)
    f["B4"] = f.groupby("date")["buy_cls_foreign_inst"].rank(pct=True) <= 0.33
    gt.MECH.clear()
    gt.MECH.update({"TP8/SL6/10d": (0.08, -0.06, 10)})
    R = gt.sim_all(pxs)
    sub = pxs[pxs.ticker.isin(f.ticker.unique())]
    TR = triggers(sub).merge(triggers2(sub), on=["ticker", "date"], how="outer")
    D = f.merge(TR, on=["ticker", "date"], how="inner").merge(R, on=["ticker", "date"], how="left")
    D["y_rel"] = D["rel_10d"] * 100
    D["y_tp"] = (D["TP8/SL6/10d"] - COST) * 100
    u = np.sort(f["date"].unique())
    k = len(u) // 3
    D["blk"] = np.where(D["date"] <= u[k], 1, np.where(D["date"] <= u[2 * k], 2, 3))
    old = ["MA20", "H20", "H60", "R5V", "R10", "R20", "BB", "GC"]
    new = ["MA5", "MA10", "V5", "V10"]
    print("marker prevalence among all feature-days:", {m: round(float(f[m].mean()), 3) for m in ("B1", "B1L", "B1M", "B4", "B5")})
    print("events per new trigger:", {t: int(D[t].fillna(False).sum()) for t in new})
    plan = [(t, m) for t in old for m in ("B1L", "B1M")] + [(t, m) for t in new for m in ("B1", "B1L", "B1M", "B4", "B5")]
    rows = []
    for t, m in plan:
        E = D[D[t].fillna(False)].dropna(subset=["y_rel"]).copy()
        for ycol, lab in (("y_rel", "rel_10d"), ("y_tp", "TP8/SL6")):
            e = E.dropna(subset=[ycol])
            on, off = e[e[m]], e[~e[m]]
            if len(on) < 25 or len(off) < 25:
                continue
            b, tt = diff_cluster(e[ycol].to_numpy(float), e[m].to_numpy(float), e["ticker"].to_numpy())
            sg = []
            for bk in (1, 2, 3):
                s = e[e.blk == bk]
                sg.append(np.sign(s[s[m]][ycol].mean() - s[~s[m]][ycol].mean()) if s[m].sum() >= 5 and (~s[m]).sum() >= 5 else 0)
            rows.append(dict(trigger=t, marker=m, outcome=lab, n_on=len(on), n_off=len(off), on=on[ycol].mean(), off=off[ycol].mean(), diff=b, t=tt, signs=sg))
    G = pd.DataFrame(rows)
    G["p"] = G["t"].map(norm_p)
    G["bh"] = bh(G["p"].to_numpy())
    print(f"\nGRID EXP-BRK-2: planned {len(plan) * 2} tests, run {len(G)} (n_on,n_off >= 25); nominal |t|>=2: {int((G.t.abs() >= 2).sum())}, |t|>=3: {int((G.t.abs() >= 3).sum())}; BH q<0.05 survivors: {int(G.bh.sum())}")
    pd.set_option("display.width", 220)
    print(G.assign(abs_t=G.t.abs()).sort_values("abs_t", ascending=False).head(20)[["trigger", "marker", "outcome", "n_on", "n_off", "on", "off", "diff", "t", "signs", "bh"]].round(2).to_string(index=False))
    print("\nby new trigger: baseline of the breakout itself (all events, no marker), mean rel_10d % / TP8/SL6 net % / events")
    for t in new + old:
        E = D[D[t].fillna(False)].dropna(subset=["y_rel"])
        print(f"   {t:<5} n {len(E):>4} | rel_10d {E.y_rel.mean():+5.2f}% | TP8/SL6 net {E.dropna(subset=['y_tp']).y_tp.mean():+5.2f}%")
    print("\nPOOLED over all triggers in this family (trigger x tercile fixed effects), marker ON minus OFF:")
    allt = old + new
    E0 = pd.concat([D[D[t].fillna(False)].assign(trig=t) for t in allt])
    for m in ("B1", "B1L", "B1M", "B4", "B5"):
        for ycol, lab in (("y_rel", "rel_10d %"), ("y_tp", "TP8/SL6 net %")):
            E = E0.dropna(subset=[ycol])
            cell = E["trig"] + "_" + E["blk"].astype(str)
            y = E[ycol] - E.groupby(cell)[ycol].transform("mean")
            x = E[m].astype(float) - E[m].astype(float).groupby(cell).transform("mean")
            b = (x * y).sum() / (x * x).sum()
            e = y - b * x
            cs = (x * e).groupby(E["ticker"]).sum()
            t = b / (np.sqrt((cs ** 2).sum()) / (x * x).sum())
            print(f"   {m:<4}{lab:<14} ON {100 * E[m].mean():4.1f}% of events | diff {b:+.2f} (t {t:+.1f}) | ON {E[E[m]][ycol].mean():+.2f} vs OFF {E[~E[m]][ycol].mean():+.2f}")


if __name__ == "__main__":
    main()
