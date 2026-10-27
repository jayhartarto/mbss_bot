"""
research/ipot_explore/breakout_backing.py — real vs fake breakout, and is there 'bandar backing'?  (experiment family EXP-BRK-1; the whole grid below is registered, nothing else was run in this family.)

Triggers (first event per ticker per trigger type, 20-trading-day cooldown; known at the close of day t, entry open t+1, so nothing here is look-ahead):
  MA20      first close >2% above MA20 after 10 days with no close above it, MA20 rising over 5 days
  H20 / H60 close above the highest close of the previous 20 / 60 days
  R5V       one-day return >= +5% with volume >= 2x the previous 20-day mean   | R10: one-day return >= +10%   | R20: one-day return >= +20%
  BB        close above the upper Bollinger band (20,2) right after a squeeze (bandwidth in its own lowest quintile of the last 120 days, day before)
  GC        MA20 crosses above MA50 with close above MA20
Bandar-backing markers (per-date percentile rules among all stocks with data that day, fixed in advance, direction as stated):
  B1 quiet steady accumulation score in the top decile        B2 dominant 10-day accumulator: inventory share >= P80 and net-buyer on >= 70% of days
  B3 top net buyer's share of traded value >= P80 (breakout-day concentration)    B4 domestic-led: buy_cls_foreign_inst <= P33    B5 XL is the #1 net buyer (expected AGAINST)    B6 accumulator ramp > 0 (buying accelerating)
Outcomes from open(t+1): rel_10d (relative to the liquid-universe median, %), and the mechanic TP+8% / SL-6% / 10 days (same-day touch = stop), net of 0.4% cost.
Per cell: marker ON minus OFF within the trigger events, ticker-clustered t, sign agreement across 3 date terciles; Benjamini-Hochberg over the whole grid; then each marker pooled over triggers (trigger x tercile fixed effects).
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.breakout_backing
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from research.ipot_explore import common, features
from research.ipot_explore import gorengan_trade as gt
from research.ipot_explore.mover_footprint import TOP, OUT

COST = 0.004
COOL = 20


def triggers(px: pd.DataFrame) -> pd.DataFrame:
    out = []
    for tk, g in px.sort_values(["ticker", "date"]).groupby("ticker"):
        if len(g) < 130:
            continue
        c, v = g["close"], g["volume"]
        ma20, ma50 = c.rolling(20).mean(), c.rolling(50).mean()
        sd20 = c.rolling(20).std()
        ret1 = c.pct_change()
        above = (c > ma20).astype(float)
        T = pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy()})
        T["MA20"] = ((c > ma20 * 1.02) & (above.shift(1).rolling(10).sum() == 0) & (ma20 > ma20.shift(5))).to_numpy()
        T["H20"] = (c > c.shift(1).rolling(20).max()).to_numpy()
        T["H60"] = (c > c.shift(1).rolling(60).max()).to_numpy()
        T["R5V"] = ((ret1 >= 0.05) & (v >= 2 * v.shift(1).rolling(20).mean())).to_numpy()
        T["R10"] = (ret1 >= 0.10).to_numpy()
        T["R20"] = (ret1 >= 0.20).to_numpy()
        bw = 4 * sd20 / ma20
        sq = bw.shift(1) <= bw.rolling(120, min_periods=60).quantile(0.2).shift(1)
        T["BB"] = ((c > ma20 + 2 * sd20) & sq).to_numpy()
        T["GC"] = ((ma20 > ma50) & (ma20.shift(1) <= ma50.shift(1)) & (c > ma20)).to_numpy()
        for col in ("MA20", "H20", "H60", "R5V", "R10", "R20", "BB", "GC"):
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


def norm_p(t: float) -> float:
    return math.erfc(abs(t) / math.sqrt(2))


def bh(p: np.ndarray, q: float = 0.05) -> np.ndarray:
    n = len(p)
    order = np.argsort(p)
    ok = p[order] <= q * (np.arange(1, n + 1) / n)
    k = (np.max(np.where(ok)[0]) + 1) if ok.any() else 0
    keep = np.zeros(n, bool)
    keep[order[:k]] = True
    return keep


def diff_cluster(y: np.ndarray, x: np.ndarray, tick: np.ndarray):
    xm = x - x.mean()
    if (xm ** 2).sum() == 0:
        return np.nan, np.nan
    b = (xm * (y - y.mean())).sum() / (xm ** 2).sum()
    e = (y - y.mean()) - b * xm
    cs = pd.Series(xm * e).groupby(tick).sum()
    return b, b / (np.sqrt((cs ** 2).sum()) / (xm ** 2).sum())


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
    pct = lambda c: f.groupby("date")[c].rank(pct=True)
    f["B1"] = f["sig"].astype(bool)
    f["B2"] = (pct("dom_inventory_share") >= 0.8) & (f["dom_pos_days"] >= 0.7)
    f["B3"] = pct("top_net_buyer_share") >= 0.8
    f["B4"] = pct("buy_cls_foreign_inst") <= 0.33
    f["B6"] = f["dom_ramp"] > 0
    markers = ["B1", "B2", "B3", "B4", "B5", "B6"]
    gt.MECH.clear()
    gt.MECH.update({"TP8/SL6/10d": (0.08, -0.06, 10)})
    R = gt.sim_all(pxs)
    TR = triggers(pxs[pxs.ticker.isin(f.ticker.unique())])
    D = f.merge(TR, on=["ticker", "date"], how="inner").merge(R, on=["ticker", "date"], how="left")
    D["y_rel"] = D["rel_10d"] * 100
    D["y_tp"] = (D["TP8/SL6/10d"] - COST) * 100
    u = np.sort(f["date"].unique())
    k = len(u) // 3
    D["blk"] = np.where(D["date"] <= u[k], 1, np.where(D["date"] <= u[2 * k], 2, 3))
    trig = ["MA20", "H20", "H60", "R5V", "R10", "R20", "BB", "GC"]
    print("events per trigger (with IPOT features and outcome):", {t: int(D[t].sum()) for t in trig})
    rows = []
    for t in trig:
        E = D[D[t]].dropna(subset=["y_rel"]).copy()
        for m in markers:
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
    G["consistent"] = G["signs"].map(lambda s: len(set(s)) == 1 and s[0] != 0)
    print(f"\nGRID: {len(G)} tests (trigger x marker x outcome, n_on and n_off >= 25); nominal |t|>=2: {int((G.t.abs() >= 2).sum())}, |t|>=3: {int((G.t.abs() >= 3).sum())}; BH q<0.05 survivors: {int(G.bh.sum())}")
    pd.set_option("display.width", 220)
    show = G.assign(abs_t=G.t.abs()).sort_values("abs_t", ascending=False).head(22)
    print(show[["trigger", "marker", "outcome", "n_on", "n_off", "on", "off", "diff", "t", "signs", "bh"]].round(2).to_string(index=False))
    print("\nPOOLED over triggers (trigger x tercile fixed effects), marker ON minus OFF:")
    for m in markers:
        for ycol, lab in (("y_rel", "rel_10d %"), ("y_tp", "TP8/SL6 net %")):
            rowsm = []
            E = pd.concat([D[D[t]].assign(trig=t) for t in trig]).dropna(subset=[ycol])
            cell = E["trig"] + "_" + E["blk"].astype(str)
            y = E[ycol] - E.groupby(cell)[ycol].transform("mean")
            x = E[m].astype(float) - E[m].astype(float).groupby(cell).transform("mean")
            b = (x * y).sum() / (x * x).sum()
            e = y - b * x
            cs = (x * e).groupby(E["ticker"]).sum()
            t = b / (np.sqrt((cs ** 2).sum()) / (x * x).sum())
            print(f"   {m} {lab:<14} ON share {100 * E[m].mean():4.1f}% | diff {b:+.2f} (t {t:+.1f}) | ON {E[E[m]][ycol].mean():+.2f} vs OFF {E[~E[m]][ycol].mean():+.2f}  [events {len(E)}, dependent: the same stock-day can trigger several types]")


if __name__ == "__main__":
    main()
