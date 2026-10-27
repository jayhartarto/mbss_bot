"""
research/ipot_explore/bigwinner_tp.py — EXP-BIGWIN-4 (registered before running): the best take-profit for the right-tail picks.
Picks = the clean, de-duplicated top-1% picks of the price-only walk-forward GBM (min price Rp30), as in bigwinner_risk2.py.  Entry open(t+1), costs 0.4%, same-day TP/SL touch = stop, gap through the stop exits at the open,
a TP is filled at the TP price (conservative).  Split by pick date: DISCOVERY <= 2025-06-30, VALIDATION after; the best rule is chosen on DISCOVERY only and its VALIDATION result is shown untouched.
Rule families: FIX  fixed TP t% (5..40, none) x fixed stop s% (4, 6, 8, 12) x horizon (10, 20)  |  R  TP = R x stop (R = 1, 1.5, 2, 3, 4, 6; stops 4, 6)  |  ATR  stop = m x ATR%, TP = k x ATR% (volatility-adaptive; ATR% of the signal day)
|  PART  sell half at +t1 (t1 = 6, 8, 10, 15), the other half runs with its stop moved to breakeven and exits at day 20 (no TP), initial stop s.
Reported: mean net, validation mean, win rate, average loser, profit factor, mean without the best 5 trades, positive quarters, and the mean R (net / (stop + cost)).
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.bigwinner_tp
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore.bigwinner_risk import get_picks, dedup
from research.ipot_explore.entry_scenarios import simulate, COST

SPLIT = pd.Timestamp("2025-06-30")


def partial_sim(px, rows, t1, s, H=20):
    O, Hh, L, C = (px[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    out = np.full(len(rows), np.nan)
    for j, r in enumerate(rows):
        if r + H >= len(px):
            continue
        E = O[r + 1]
        stop = E * (1 - s)
        tp = E * (1 + t1)
        res = None
        for k in range(1, H + 1):
            i = r + k
            if O[i] <= stop:
                res = O[i] / E - 1
                break
            if L[i] <= stop:
                res = stop / E - 1
                break
            if Hh[i] >= tp:
                # first half locked at the TP; second half continues with the stop at breakeven, exit at day H
                tail = None
                for kk in range(k + 1, H + 1):
                    ii = r + kk
                    if O[ii] <= E:
                        tail = O[ii] / E - 1
                        break
                    if L[ii] <= E:
                        tail = 0.0
                        break
                if tail is None:
                    tail = C[r + H] / E - 1
                res = 0.5 * t1 + 0.5 * tail
                break
        out[j] = res if res is not None else C[r + H] / E - 1
    return out


def stats(net, SD, st):
    d = SD["date"].to_numpy() <= np.datetime64(SPLIT)
    s = np.sort(net)[::-1]
    los, win = net[net < 0], net[net > 0]
    pq = pd.Series(net, index=SD.index).groupby(SD["q"]).mean()
    return dict(mean=net.mean(), disc=net[d].mean(), val=net[~d].mean(), win=(net > 0).mean(), avg_loss=los.mean() if len(los) else np.nan, pf=win.sum() / -los.sum() if len(los) else np.nan,
                ex5=s[5:].mean(), qp=int((pq > 0).sum()), nq=len(pq), R=(net / (st + COST)).mean())


def main():
    px, D, T = get_picks()
    S = T[T["p"] >= T.groupby("q")["p"].transform(lambda s: s.quantile(0.99))]
    SD = dedup(S)
    rows = SD["pxrow"].to_numpy()
    atr = SD["atr"].to_numpy()
    nd = int((SD["date"] <= SPLIT).sum())
    print(f"picks {len(SD)} (discovery {nd}, validation {len(SD) - nd}); ATR% of picks: median {100 * np.median(atr):.1f}% (P25 {100 * np.percentile(atr, 25):.1f}, P75 {100 * np.percentile(atr, 75):.1f})")
    res = []

    def add(label, family, gross, st):
        net = gross - COST
        if np.isnan(net).any():
            m = ~np.isnan(net)
            net = np.where(m, net, 0.0)
        s = stats(net, SD, st)
        s.update(label=label, family=family)
        res.append(s)

    TPS = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.40, 9.99]
    for H in (20, 10):
        for s_ in (0.04, 0.06, 0.08, 0.12):
            for tp in TPS:
                f_, g_ = simulate(px, rows, None, tp, s_, H, hmax=20)
                add(f"FIX s{int(100 * s_)} tp{'none' if tp > 5 else int(100 * tp)} H{H}", "FIX", g_, s_)
    for s_ in (0.04, 0.06):
        for R in (1, 1.5, 2, 3, 4, 6):
            f_, g_ = simulate(px, rows, None, R * s_, s_, 20, hmax=20)
            add(f"R s{int(100 * s_)} R{R} H20", "R", g_, s_)
    for m in (0.75, 1.0, 1.5):
        for k in (1.0, 1.5, 2.0, 3.0, 4.0):
            f_, g_ = simulate(px, rows, None, np.clip(k * atr, 0.03, 1.0), np.clip(m * atr, 0.03, 0.2), 20, hmax=20)
            add(f"ATR stop{m}x tp{k}x H20", "ATR", g_, float(np.median(np.clip(m * atr, 0.03, 0.2))))
    for s_ in (0.06, 0.08):
        for t1 in (0.06, 0.08, 0.10, 0.15):
            add(f"PART s{int(100 * s_)} half@{int(100 * t1)} rest BE", "PART", partial_sim(px, rows, t1, s_), s_)
    R_ = pd.DataFrame(res)
    pd.set_option("display.width", 220)
    show = lambda d: d.assign(**{c: (d[c] * 100).round(2) for c in ("mean", "disc", "val", "win", "avg_loss", "ex5")}).round({"pf": 2, "R": 2})[["label", "mean", "disc", "val", "win", "avg_loss", "pf", "ex5", "qp", "R"]]
    print("\nFIXED TP curve, stop 6%, 20 days (all picks):")
    c = R_[(R_.family == "FIX") & R_.label.str.contains(" s6 ") & R_.label.str.endswith("H20")]
    print(show(c).to_string(index=False))
    print("\nFIXED TP curve, stop 6%, 10 days:")
    print(show(R_[(R_.family == "FIX") & R_.label.str.contains(" s6 ") & R_.label.str.endswith("H10")]).to_string(index=False))
    print("\nbest TP per stop (20 days) by DISCOVERY mean, with the untouched validation mean:")
    for s_ in (4, 6, 8, 12):
        c = R_[(R_.family == "FIX") & R_.label.str.contains(f" s{s_} ") & R_.label.str.endswith("H20")].sort_values("disc", ascending=False).head(3)
        print(show(c).to_string(index=False, header=(s_ == 4)))
    print("\nR-multiple grid (TP = R x stop):")
    print(show(R_[R_.family == "R"]).to_string(index=False))
    print("\nvolatility-adaptive (stop m x ATR, TP k x ATR):")
    print(show(R_[R_.family == "ATR"]).to_string(index=False))
    print("\nscale-out (half at +t1, rest at breakeven stop to day 20):")
    print(show(R_[R_.family == "PART"]).to_string(index=False))
    allr = R_.sort_values("disc", ascending=False)
    print("\nTOP 10 BY DISCOVERY MEAN across all families, with the validation mean:")
    print(show(allr.head(10)).to_string(index=False))
    print(f"\ncorrelation of discovery and validation means across the {len(R_)} rules: {R_.disc.corr(R_.val):.2f}; rules positive in both halves: {int(((R_.disc > 0) & (R_.val > 0)).sum())}")


if __name__ == "__main__":
    main()
