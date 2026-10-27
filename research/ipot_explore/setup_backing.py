"""
research/ipot_explore/setup_backing.py — experiment family EXP-SETUP-1 (registered before running): inside price-only bullish setups and inside the SAME ATR stratum, does bandar backing add to the outcome?

Setups (state entries, cooldown 15 trading days per ticker and setup; known at the close of t, entry open t+1):
  TREND   close > MA20 > MA50, MA20 higher than 5 days ago, RSI14 in [50, 75], close <= 1.10 x MA20 (an orderly uptrend, entered the first day the state holds)
  REV_MACD  MACD histogram (SMA12-SMA26, signal EMA9) turns from <= 0 to > 0 while close > MA10 and close < MA50 (reversal while still below the long average)
  REV_RSI   RSI14 crosses above 50 after having been <= 35 within the previous 15 days
  REV_MA    close crosses above MA10 (previous close at or below) with MA10 rising over 3 days and close at least 15% under its 50-day high (a reversal after a drawdown)
Strata: ATR% terciles of the pooled sample. Markers: XLB (XL #1 net buyer, expected against), XLS (XL #1 seller), B1L (quiet-accumulation score top 30%), B2 (dominant 10d accumulator: inventory >= P80 and net buyer on >= 70% of days),
B3 (top net buyer's share >= P80), B4 (buy_cls_foreign_inst <= P33, a size proxy: controls below), B6 (accumulator ramp > 0).
Outcomes (net 0.4%, entry open t+1): hold10, hold20, S10H15 (stop -10%, 15 days). Pooled effect = marker ON minus OFF with cell fixed effects setup x ATR-tercile x time-tercile, controls log val20 and log close, ticker-clustered t.
Whole family: 4 setups x 7 markers x 3 mechanics = 84 pooled-by-setup tests (BH reported), 21 pooled-over-setups tests.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.setup_backing
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features
from research.ipot_explore import gorengan_trade as gt
from research.ipot_explore.breakout_backing import norm_p, bh, COST
from research.ipot_explore.breakout_exit import sim_mech
from research.ipot_explore.mover_footprint import TOP, OUT
from research.ipot_explore.xl_seller import xl_flags

MECHS = ("hold10", "hold20", "S10H15")
MARK = ["XLB", "XLS", "B1L", "B2", "B3", "B4", "B6"]
COOL = 15


def setups(px: pd.DataFrame) -> pd.DataFrame:
    out = []
    for tk, g in px.sort_values(["ticker", "date"]).groupby("ticker"):
        if len(g) < 80:
            continue
        c = g["close"]
        ma10, ma20, ma50 = c.rolling(10).mean(), c.rolling(20).mean(), c.rolling(50).mean()
        d = c.diff()
        up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
        macd = c.rolling(12).mean() - c.rolling(26).mean()
        hist = macd - macd.ewm(span=9, adjust=False).mean()
        T = pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy()})
        state = (c > ma20) & (ma20 > ma50) & (ma20 > ma20.shift(5)) & (rsi >= 50) & (rsi <= 75) & (c <= 1.10 * ma20)
        T["TREND"] = (state & ~state.shift(1, fill_value=False)).to_numpy()
        T["REV_MACD"] = ((hist > 0) & (hist.shift(1) <= 0) & (c > ma10) & (c < ma50)).to_numpy()
        T["REV_RSI"] = ((rsi > 50) & (rsi.shift(1) <= 50) & (rsi.shift(1).rolling(15).min() <= 35)).to_numpy()
        T["REV_MA"] = ((c > ma10) & (c.shift(1) <= ma10.shift(1)) & (ma10 > ma10.shift(3)) & (c <= 0.85 * c.rolling(50).max())).to_numpy()
        for col in ("TREND", "REV_MACD", "REV_RSI", "REV_MA"):
            idx = np.where(T[col].fillna(False).to_numpy())[0]
            keep, last = [], -999
            for i in idx:
                if i - last >= COOL:
                    keep.append(i)
                    last = i
            fl = np.zeros(len(T), bool)
            fl[keep] = True
            T[col] = fl
        out.append(T)
    return pd.concat(out, ignore_index=True)


def effect(E: pd.DataFrame, mk: str, m: str, cellcols: list[str], controls: list[str]):
    y = (E[m] - COST) * 100
    cell = E[cellcols[0]].astype(str)
    for c in cellcols[1:]:
        cell = cell + "_" + E[c].astype(str)
    X = E[[mk] + controls].astype(float)
    X = X - X.groupby(cell).transform("mean")
    sd = X.std().replace(0, 1)
    A = (X / sd).to_numpy()
    yy = (y - y.groupby(cell).transform("mean")).to_numpy()
    try:
        XtXi = np.linalg.inv(A.T @ A)
    except np.linalg.LinAlgError:
        return np.nan, np.nan
    bb = XtXi @ A.T @ yy
    e = yy - A @ bb
    S = np.zeros((A.shape[1],) * 2)
    for _, idx in pd.Series(np.arange(len(yy))).groupby(E["ticker"].to_numpy()):
        sv = A[idx.to_numpy()].T @ e[idx.to_numpy()]
        S += np.outer(sv, sv)
    V = XtXi @ S @ XtXi
    return bb[0] / sd.iloc[0], bb[0] / np.sqrt(V[0, 0])


def main():
    f, pxs, px_all, tdi = gt.build()
    raw = pd.concat([common.load_ipot(TOP), common.load_ipot(OUT)], ignore_index=True)
    f = f.merge(xl_flags(raw), on=["ticker", "date"], how="left")
    for c in ("XLB", "XLS"):
        f[c] = f[c].fillna(False)
    pct = lambda c: f.groupby("date")[c].rank(pct=True)
    f["B1L"] = pct("score") >= 0.7
    f["B2"] = (pct("dom_inventory_share") >= 0.8) & (f["dom_pos_days"] >= 0.7)
    f["B3"] = pct("top_net_buyer_share") >= 0.8
    f["B4"] = pct("buy_cls_foreign_inst") <= 0.33
    f["B6"] = f["dom_ramp"] > 0
    sub = pxs[pxs.ticker.isin(f.ticker.unique())]
    R = sim_mech(sub)[["ticker", "date"] + list(MECHS)]
    D = f.merge(setups(sub), on=["ticker", "date"], how="inner").merge(R, on=["ticker", "date"], how="left")
    u = np.sort(f["date"].unique())
    k = len(u) // 3
    D["tcl"] = np.where(D["date"] <= u[k], 1, np.where(D["date"] <= u[2 * k], 2, 3))
    D["atrq"] = pd.qcut(f["atr_pct"], 3, labels=False, duplicates="drop").reindex(D.index) if False else pd.qcut(D["atr_pct"], 3, labels=False, duplicates="drop")
    SET = ["TREND", "REV_MACD", "REV_RSI", "REV_MA"]
    print("events per setup x ATR tercile (0 = calmest):")
    for s in SET:
        E = D[D[s].fillna(False)]
        print(f"   {s:<9} total {len(E):>5} | " + " ".join(f"q{q}: {int((E.atrq == q).sum()):>4}" for q in (0, 1, 2)) + f" | tickers {E.ticker.nunique()}")
    gt.MECH.clear()
    gt.MECH.update({"hold10": (None, None, 10)})
    allcell = f.merge(R, on=["ticker", "date"], how="left")
    allcell["atrq"] = pd.qcut(allcell["atr_pct"], 3, labels=False, duplicates="drop")
    allcell["tcl"] = np.where(allcell["date"] <= u[k], 1, np.where(allcell["date"] <= u[2 * k], 2, 3))
    print("\nthe setups themselves (net mean % | excess over ALL stock-days of the same time-tercile x ATR-tercile cell):")
    for s in SET:
        E = D[D[s].fillna(False)]
        line = []
        for m in MECHS:
            ok = E.dropna(subset=[m])
            base = allcell.dropna(subset=[m]).groupby(["tcl", "atrq"])[m].mean()
            ref = pd.Series([base.get((t, a), np.nan) for t, a in zip(ok.tcl, ok.atrq)], index=ok.index)
            ex = ok[m] - ref
            line.append(f"{m} {100 * (ok[m].mean() - COST):+.2f}% | ex {100 * ex.mean():+.2f}pp (t {gt.clus_t(ex, ok.ticker):+.1f})")
        print(f"   {s:<9} " + "  ||  ".join(line))
    ctrl = ["log_val20", "log_close"]
    rows = []
    for s in SET:
        E = D[D[s].fillna(False)].dropna(subset=ctrl)
        for mk in MARK:
            for m in MECHS:
                e = E.dropna(subset=[m])
                if e[mk].sum() < 25 or (~e[mk].astype(bool)).sum() < 25:
                    continue
                b, t = effect(e.assign(**{mk: e[mk].astype(float)}), mk, m, ["atrq", "tcl"], ctrl)
                rows.append(dict(setup=s, marker=mk, mech=m, n_on=int(e[mk].sum()), n_off=int((~e[mk].astype(bool)).sum()), diff=b, t=t))
    G = pd.DataFrame(rows).dropna(subset=["t"])
    G["p"] = G["t"].map(norm_p)
    G["bh"] = bh(G["p"].to_numpy())
    print(f"\nFAMILY (setup x marker x mechanic, n_on,n_off >= 25): {len(G)} tests | nominal |t|>=2: {int((G.t.abs() >= 2).sum())} | |t|>=3: {int((G.t.abs() >= 3).sum())} | BH q<0.05 survivors: {int(G.bh.sum())}")
    pd.set_option("display.width", 200)
    print(G.assign(a=G.t.abs()).sort_values("a", ascending=False).head(16)[["setup", "marker", "mech", "n_on", "n_off", "diff", "t", "bh"]].round(2).to_string(index=False))

    print("\nPOOLED over the four setups (cell FE setup x ATR-tercile x time-tercile, controls log val20, log close): marker ON minus OFF, net pp per trade (t)")
    E0 = pd.concat([D[D[s].fillna(False)].assign(setup=s) for s in SET]).dropna(subset=ctrl)
    for mk in MARK:
        line = []
        for m in MECHS:
            e = E0.dropna(subset=[m])
            b, t = effect(e.assign(**{mk: e[mk].astype(float)}), mk, m, ["setup", "atrq", "tcl"], ctrl)
            line.append(f"{m} {b:+.2f}({t:+.1f})")
        print(f"   {mk:<4} ON {100 * E0[mk].astype(float).mean():4.1f}% | " + " | ".join(line))
    print("\nBY ATR TERCILE (pooled over setups, cell FE setup x time-tercile), hold20:")
    for mk in MARK:
        line = []
        for q in (0, 1, 2):
            e = E0[(E0.atrq == q)].dropna(subset=["hold20"])
            if e[mk].sum() < 25 or (~e[mk].astype(bool)).sum() < 25:
                line.append(f"q{q} n/a")
                continue
            b, t = effect(e.assign(**{mk: e[mk].astype(float)}), mk, "hold20", ["setup", "tcl"], ctrl)
            line.append(f"q{q} {b:+.2f}({t:+.1f}) n_on {int(e[mk].sum())}")
        print(f"   {mk:<4} " + " | ".join(line))


if __name__ == "__main__":
    main()
