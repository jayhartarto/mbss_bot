"""
research/ipot_explore/xl_seller.py — the mirror image of the XL finding: XL as the #1 net SELLER of a ticker-day (and XL as #1 net buyer for reference), same controls as before.
Part A  all stock-days (top150 1y + outside 3m): rel_{1,5,10}d (%) ~ XL_top_buy + XL_top_sell + controls, two-way fixed effects (ticker, date), two-way clustered t; per-time-tercile coefficients for h=5.
Part B  inside breakout events (all 16 trigger types, pooled, trigger x tercile x ATR-tercile cells + continuous controls, ticker-clustered): net return ON minus OFF for XL top buyer / XL top seller under 4 mechanics.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.xl_seller
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features
from research.ipot_explore import gorengan_trade as gt
from research.ipot_explore.breakout_backing import triggers, COST
from research.ipot_explore.breakout_backing2 import triggers2
from research.ipot_explore.breakout_exit import triggers3, sim_mech, MECH
from research.ipot_explore.lead_1d import demean2, twoway_t
from research.ipot_explore.mover_footprint import TOP, OUT

CTRL = ["log_val20", "atr_pct", "past_ret_10d", "pret_1d", "pret_5d", "vol_ratio", "range60_pos", "log_close"]


def xl_flags(raw: pd.DataFrame) -> pd.DataFrame:
    a = features.broker_day_table(raw, "all", "all")[["ticker", "date", "broker", "net_val_mid"]].dropna()
    out = None
    for name, sign in (("XLB", 1.0), ("XLS", -1.0)):
        s = a[sign * a["net_val_mid"] > 0].copy()
        s["rk"] = s.groupby(["ticker", "date"])["net_val_mid"].rank(ascending=(sign < 0), method="first")
        fl = s[(s.rk == 1) & (s.broker == "XL")][["ticker", "date"]].drop_duplicates().assign(**{name: True})
        out = fl if out is None else out.merge(fl, on=["ticker", "date"], how="outer")
    return out


def main():
    f, pxs, px_all, tdi = gt.build()
    raw = pd.concat([common.load_ipot(TOP), common.load_ipot(OUT)], ignore_index=True)
    f = f.merge(xl_flags(raw), on=["ticker", "date"], how="left")
    for c in ("XLB", "XLS"):
        f[c] = f[c].fillna(False).astype(bool)
    print(f"stock-days {len(f)}; XL top buyer {100 * f.XLB.mean():.1f}%, XL top seller {100 * f.XLS.mean():.1f}%")

    print("\nPART A  all stock-days, two-way FE + controls: effect on relative return (pp), two-way clustered t")
    u = np.sort(f["date"].unique())
    k = len(u) // 3
    tcl = np.where(f["date"] <= u[k], 1, np.where(f["date"] <= u[2 * k], 2, 3))
    for h in (1, 5, 10):
        d = f.dropna(subset=[f"rel_{h}d"] + CTRL).copy()
        d["y"] = d[f"rel_{h}d"] * 100
        d["XLB"] = d["XLB"].astype(float)
        d["XLS"] = d["XLS"].astype(float)
        M = demean2(d[["XLB", "XLS"] + CTRL + ["y"]].to_numpy(), d["ticker"].to_numpy(), d["date"].to_numpy())
        X = M[:, :-1]
        sd = X.std(axis=0)
        bB, tB = twoway_t(X, M[:, -1], d["ticker"].to_numpy(), d["date"].to_numpy(), 0)
        bS, tS = twoway_t(np.column_stack([X[:, 1], X[:, 0], X[:, 2:]]), M[:, -1], d["ticker"].to_numpy(), d["date"].to_numpy(), 0)
        line = f"   h={h:>2}d n {len(d):>6} | XL top BUYER {bB:+.3f}pp (t {tB:+.1f}) | XL top SELLER {bS:+.3f}pp (t {tS:+.1f})"
        if h == 5:
            seg = []
            tc = pd.Series(tcl, index=f.index).reindex(d.index).to_numpy()
            for q in (1, 2, 3):
                m = tc == q
                Mq = demean2(d.loc[m, ["XLB", "XLS"] + CTRL + ["y"]].to_numpy(), d.loc[m, "ticker"].to_numpy(), d.loc[m, "date"].to_numpy())
                b1, _ = twoway_t(Mq[:, :-1], Mq[:, -1], d.loc[m, "ticker"].to_numpy(), d.loc[m, "date"].to_numpy(), 0)
                b2, _ = twoway_t(np.column_stack([Mq[:, 1], Mq[:, 0], Mq[:, 2:-1]]), Mq[:, -1], d.loc[m, "ticker"].to_numpy(), d.loc[m, "date"].to_numpy(), 0)
                seg.append(f"T{q}: buyer {b1:+.2f} seller {b2:+.2f}")
            line += " | " + " ; ".join(seg)
        print(line)

    print("\nPART B  inside breakout events (16 trigger types pooled), controlled, net % per trade, ON minus OFF (t)")
    sub = pxs[pxs.ticker.isin(f.ticker.unique())]
    R = sim_mech(sub)
    TR = triggers(sub).merge(triggers2(sub), on=["ticker", "date"], how="outer").merge(triggers3(sub), on=["ticker", "date"], how="outer")
    D = f.merge(TR, on=["ticker", "date"], how="inner").merge(R, on=["ticker", "date"], how="left")
    D["tcl"] = np.where(D["date"] <= u[k], 1, np.where(D["date"] <= u[2 * k], 2, 3))
    trig = ["MA20", "MA10", "MA5", "X20", "X10", "X5", "S20", "H20", "H60", "R5V", "R10", "R20", "V5", "V10", "BB", "GC"]
    E0 = pd.concat([D[D[t].fillna(False)].assign(trig=t) for t in trig])
    E0["XLB"] = E0["XLB"].astype(float)
    E0["XLS"] = E0["XLS"].astype(float)
    print(f"   events {len(E0)}, XL buyer {100 * E0.XLB.mean():.1f}%, XL seller {100 * E0.XLS.mean():.1f}%")
    for m in ("hold5", "hold10", "hold20", "S12H20"):
        E = E0.dropna(subset=[m] + CTRL)
        y = (E[m] - COST) * 100
        cell = E["trig"] + "_" + E["tcl"].astype(str) + "_" + E["vol"].astype(str)
        cols = ["XLB", "XLS"] + ["log_val20", "atr_pct", "log_close"]
        X = E[cols].astype(float)
        X = X - X.groupby(cell).transform("mean")
        sd = X.std()
        A = (X / sd).to_numpy()
        yy = (y - y.groupby(cell).transform("mean")).to_numpy()
        XtXi = np.linalg.inv(A.T @ A)
        bb = XtXi @ A.T @ yy
        e = yy - A @ bb
        S = np.zeros((A.shape[1],) * 2)
        for _, idx in pd.Series(np.arange(len(yy))).groupby(E["ticker"].to_numpy()):
            sv = A[idx.to_numpy()].T @ e[idx.to_numpy()]
            S += np.outer(sv, sv)
        V = XtXi @ S @ XtXi
        print(f"   {m:<7} XL buyer {bb[0] / sd.iloc[0]:+.2f} (t {bb[0] / np.sqrt(V[0, 0]):+.1f}) | XL seller {bb[1] / sd.iloc[1]:+.2f} (t {bb[1] / np.sqrt(V[1, 1]):+.1f}) | ON mean net: buyer {100 * (E[E.XLB == 1][m].mean() - COST):+.2f}% seller {100 * (E[E.XLS == 1][m].mean() - COST):+.2f}% rest {100 * (E[(E.XLB == 0) & (E.XLS == 0)][m].mean() - COST):+.2f}%")


if __name__ == "__main__":
    main()
