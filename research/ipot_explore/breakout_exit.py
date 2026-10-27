"""
research/ipot_explore/breakout_exit.py — experiment family EXP-BRK-3 (registered before running).
Part 1  looser MA triggers: X5/X10/X20 = close crosses above MA5/MA10/MA20 (previous close at or below it) with that MA rising over 3 days; S20 = two consecutive closes >2% above MA20 (first such day after a day that was not).
        20-trading-day cooldown per ticker and type (outcomes overlap otherwise). Together with the older triggers (MA20, H20, H60, R5V, R10, R20, BB, GC, MA5, MA10, V5, V10).
Part 2  exit mechanics (entry open t+1; stops are checked BEFORE the day's high updates the trail and a gap through the stop exits at the open):
        hold5, hold10, hold20 (hold N closes, no stop) | S10H15 (stop -10%, 15 days) | S12H20 (stop -12%, 20 days) | TR8 and TR12 (trailing stop 8% / 12% below the highest high since entry, initial stop the same distance, 20 days)
        | TP20S10 (TP +20% / SL -10% / 20 days). Net of 0.4% round trip; excess = mean minus the same date-block x ATR-tercile cell mean of ALL stock-days under the same mechanic.
Part 3  pooled marker effects (B1L top-30% quiet accumulation, B4 domestic-led, B5 XL #1 net buyer) under every mechanic, trigger x tercile fixed effects, ticker-clustered.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.breakout_exit
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features
from research.ipot_explore import gorengan_trade as gt
from research.ipot_explore.breakout_backing import triggers, norm_p, bh, COST, COOL
from research.ipot_explore.breakout_backing2 import triggers2
from research.ipot_explore.mover_footprint import TOP, OUT

MECH = {
    "hold5": dict(H=5), "hold10": dict(H=10), "hold20": dict(H=20),
    "S10H15": dict(H=15, sl=0.10), "S12H20": dict(H=20, sl=0.12),
    "TR8": dict(H=20, trail=0.08), "TR12": dict(H=20, trail=0.12),
    "TP20S10": dict(H=20, sl=0.10, tp=0.20),
}


def triggers3(px: pd.DataFrame) -> pd.DataFrame:
    out = []
    for tk, g in px.sort_values(["ticker", "date"]).groupby("ticker"):
        if len(g) < 60:
            continue
        c = g["close"]
        T = pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy()})
        for n, name in ((5, "X5"), (10, "X10"), (20, "X20")):
            ma = c.rolling(n).mean()
            T[name] = ((c > ma) & (c.shift(1) <= ma.shift(1)) & (ma > ma.shift(3))).to_numpy()
        ma20 = c.rolling(20).mean()
        two = (c > ma20 * 1.02) & (c.shift(1) > ma20.shift(1) * 1.02)
        T["S20"] = (two & ~(two.shift(1).fillna(False))).to_numpy()
        for col in ("X5", "X10", "X20", "S20"):
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


def sim_mech(px: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for tk, g in px.sort_values(["ticker", "date"]).groupby("ticker"):
        o, h, l, c = (g[k].to_numpy() for k in ("open", "high", "low", "close"))
        n = len(g)
        res = {m: np.full(n, np.nan) for m in MECH}
        for i in range(n - 1):
            e = o[i + 1]
            if not e > 0:
                continue
            for m, p in MECH.items():
                H = p["H"]
                if i + H >= n:
                    continue
                sl, tp, trail = p.get("sl"), p.get("tp"), p.get("trail")
                stop = e * (1 - sl) if sl else (e * (1 - trail) if trail else None)
                mx = e
                r = c[i + H] / e - 1
                for k in range(i + 1, i + H + 1):
                    if stop is not None:
                        if o[k] <= stop:
                            r = o[k] / e - 1
                            break
                        if l[k] <= stop:
                            r = stop / e - 1
                            break
                    if tp and h[k] / e - 1 >= tp:
                        r = tp
                        break
                    if trail:
                        mx = max(mx, h[k])
                        stop = mx * (1 - trail)
                res[m][i] = r
        rows.append(pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy(), **res}))
    return pd.concat(rows, ignore_index=True)


def clus_t(x: pd.Series, tick: pd.Series) -> float:
    return gt.clus_t(x, tick)


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
    f["B1L"] = f.groupby("date")["score"].rank(pct=True) >= 0.7
    f["B4"] = f.groupby("date")["buy_cls_foreign_inst"].rank(pct=True) <= 0.33
    sub = pxs[pxs.ticker.isin(f.ticker.unique())]
    R = sim_mech(sub)
    TR = triggers(sub).merge(triggers2(sub), on=["ticker", "date"], how="outer").merge(triggers3(sub), on=["ticker", "date"], how="outer")
    D = f.merge(TR, on=["ticker", "date"], how="inner").merge(R, on=["ticker", "date"], how="left")
    base_cells = f.merge(R, on=["ticker", "date"], how="left")
    cellmean = {m: base_cells.dropna(subset=[m]).groupby(["blk", "vol"])[m].mean() for m in MECH}
    u = np.sort(f["date"].unique())
    k = len(u) // 3
    D["tcl"] = np.where(D["date"] <= u[k], 1, np.where(D["date"] <= u[2 * k], 2, 3))
    trig = ["MA20", "MA10", "MA5", "X20", "X10", "X5", "S20", "H20", "H60", "R5V", "R10", "R20", "V5", "V10", "BB", "GC"]
    print("events per trigger:", {t: int(D[t].fillna(False).sum()) for t in trig})
    print("\nnet mean %  (cost 0.4%) | rows = trigger, columns = mechanic ; second line = excess over same-cell base in pp (t)")
    print(f"{'trigger':<7}{'n':>5} " + "".join(f"{m:>16}" for m in MECH))
    recs = []
    for t in trig:
        E = D[D[t].fillna(False)]
        if len(E) < 40:
            print(f"{t:<7}{len(E):>5}  too few")
            continue
        l1, l2 = [], []
        for m in MECH:
            ok = E.dropna(subset=[m])
            ref = pd.Series([cellmean[m].get((b, v), np.nan) for b, v in zip(ok.blk, ok.vol)], index=ok.index)
            net = ok[m] - COST
            ex = ok[m] - ref
            tt = clus_t(ex, ok.ticker)
            recs.append(dict(trigger=t, mech=m, n=len(ok), net=100 * net.mean(), ex=100 * ex.mean(), t=tt))
            l1.append(f"{100 * net.mean():>+16.2f}")
            l2.append(f"{f'{100 * ex.mean():+.2f}({tt:+.1f})':>16}")
        print(f"{t:<7}{len(E):>5} " + "".join(l1))
        print(f"{'':<12}" + "".join(l2))
    G = pd.DataFrame(recs)
    G["p"] = G["t"].map(norm_p)
    G["bh"] = bh(G["p"].to_numpy())
    print(f"\nPART 2 grid: {len(G)} trigger x mechanic excess tests; nominal |t|>=2: {int((G.t.abs() >= 2).sum())}, |t|>=3: {int((G.t.abs() >= 3).sum())}; BH survivors: {int(G.bh.sum())}")
    print("average over triggers by mechanic: net mean % / excess pp:")
    print(G.groupby("mech", sort=False).agg(net=("net", "mean"), ex=("ex", "mean"), pos_net=("net", lambda s: (s > 0).mean())).round(2).to_string())
    print("\nPART 3 pooled marker effects (ON minus OFF, net %, ticker-clustered t): [plain] trigger x tercile FE only | [controlled] FE trigger x tercile x ATR-tercile plus continuous log val20, ATR%, log close")
    E0 = pd.concat([D[D[t].fillna(False)].assign(trig=t) for t in trig])
    for mk in ("B1L", "B4", "B5"):
        for lab in ("plain", "controlled"):
            line = []
            for m in MECH:
                E = E0.dropna(subset=[m, "log_val20", "atr_pct", "log_close"])
                y = (E[m] - COST) * 100
                cell = E["trig"] + "_" + E["tcl"].astype(str) + ("_" + E["vol"].astype(str) if lab == "controlled" else "")
                cols = [mk] + (["log_val20", "atr_pct", "log_close"] if lab == "controlled" else [])
                X = E[cols].astype(float)
                X = X - X.groupby(cell).transform("mean")
                sdm = float(X[mk].std())
                if lab == "controlled":
                    X = X / X.std()
                yy = (y - y.groupby(cell).transform("mean")).to_numpy()
                A = X.to_numpy()
                XtXi = np.linalg.inv(A.T @ A)
                bb = XtXi @ A.T @ yy
                e = yy - A @ bb
                S = np.zeros((A.shape[1],) * 2)
                for _, idx in pd.Series(np.arange(len(yy))).groupby(E["ticker"].to_numpy()):
                    sv = A[idx.to_numpy()].T @ e[idx.to_numpy()]
                    S += np.outer(sv, sv)
                V = XtXi @ S @ XtXi
                coef = bb[0] / (sdm if lab == "controlled" else 1.0)
                line.append(f"{m} {coef:+.2f}({bb[0] / np.sqrt(V[0, 0]):+.1f})")
            print(f"   {mk:<4}{lab:<11} " + " | ".join(line))


if __name__ == "__main__":
    main()
