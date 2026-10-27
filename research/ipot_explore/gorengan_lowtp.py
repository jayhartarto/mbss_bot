"""
research/ipot_explore/gorengan_lowtp.py — is TP +15% too high for the quiet-accumulation signal? Same signal and dedup as gorengan_trade.py (top decile of the date, one entry per ticker per 12 trading days),
tested with smaller targets. Mechanics (entry open t+1; same-day TP/SL touch counts as the stop): TP/SL/horizon in LOW below. Compared with ALL stock-days of the same date block x ATR tercile under the same mechanic.
Also prints the signal's lift in 'MFE >= x% within H days' for x = 5, 8, 10, 15 (does the edge shrink with the target?).
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.gorengan_lowtp
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import gorengan_trade as gt

LOW = {"TP5/SL4/10d": (0.05, -0.04, 10), "TP8/SL6/10d": (0.08, -0.06, 10), "TP10/SL7/10d": (0.10, -0.07, 10), "TP5/SL5/5d": (0.05, -0.05, 5), "TP8/SL8/5d": (0.08, -0.08, 5)}


def main():
    f, pxs, px_all, tdi = gt.build()
    gt.MECH.clear()
    gt.MECH.update(LOW)
    R = gt.sim_all(pxs)
    D = f.merge(R, on=["ticker", "date"], how="left")
    sig = D[D.sig].copy()
    sig["t"] = sig["date"].map(tdi)
    keep = []
    for tk, g in sig.sort_values(["ticker", "date"]).groupby("ticker"):
        last = None
        for idx, t in zip(g.index, g["t"]):
            if last is None or t - last >= 12:
                keep.append(idx)
                last = t
    S = D.loc[keep]
    print(f"entries {len(S)} ({S.ticker.nunique()} tickers); signal rows {int(D.sig.sum())}")
    for cost in (0.004, 0.010):
        print(f"\nround-trip cost {100 * cost:.1f}%:")
        for m in LOW:
            ok = S.dropna(subset=[m])
            bcell = D.dropna(subset=[m]).groupby(["blk", "vol"])[m].mean()
            ref = pd.Series([bcell.get((b, v), np.nan) for b, v in zip(ok.blk, ok.vol)], index=ok.index)
            net = ok[m] - cost
            diff = ok[m] - ref
            tp = LOW[m][0]
            hit = (ok[m] >= tp - 1e-9).mean()
            hit_base = (D.dropna(subset=[m])[m] >= tp - 1e-9).mean()
            byb = " ".join(f"b{b}:{100 * (ok[ok.blk == b][m].mean() - cost):+.2f}%" for b in (1, 2, 3))
            print(f"  {m:<14} n {len(ok):>4} | TP hit {100 * hit:4.1f}% (all rows {100 * hit_base:4.1f}%) | net mean {100 * net.mean():+.2f}% (t {gt.clus_t(net, ok.ticker):+.1f}) | same-cell base {100 * (ref.mean() - cost):+.2f}% "
                  f"| excess {100 * diff.mean():+.2f}pp (t {gt.clus_t(diff, ok.ticker):+.1f}) | {byb}")
    # lift of reaching +x% (max high within 10 days) for signal days vs all days (cell-matched)
    print("\nlift in P(max high within 10d from open(t+1) >= x%), signal days vs same-cell base:")
    H = 10
    mf = []
    for tk, g in pxs.sort_values(["ticker", "date"]).groupby("ticker"):
        o, h = g["open"].to_numpy(), g["high"].to_numpy()
        n = len(g)
        v = np.full(n, np.nan)
        for i in range(n - 1 - H):
            if o[i + 1] > 0:
                v[i] = h[i + 1:i + H + 1].max() / o[i + 1] - 1
        mf.append(pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy(), "mfe10": v}))
    X = D[["ticker", "date", "sig", "blk", "vol"]].merge(pd.concat(mf), on=["ticker", "date"], how="left").dropna(subset=["mfe10"])
    for x in (0.05, 0.08, 0.10, 0.15):
        y = (X["mfe10"] >= x).astype(float)
        cell = y.groupby([X.blk, X.vol]).transform("mean")
        s = X[X.sig]
        d = y[X.sig] - cell[X.sig]
        print(f"  x={100 * x:>3.0f}%: signal {100 * y[X.sig].mean():5.1f}% vs cell base {100 * cell[X.sig].mean():5.1f}% | lift {y[X.sig].mean() / cell[X.sig].mean():.2f}x | diff {100 * d.mean():+.1f}pp (t {gt.clus_t(d, s.ticker):+.1f})")


if __name__ == "__main__":
    main()
