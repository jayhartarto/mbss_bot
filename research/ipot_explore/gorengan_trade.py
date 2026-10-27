"""
research/ipot_explore/gorengan_trade.py — follow-up of gorengan_episodes.py.
(a) thresholds: episode = +RUN% within 15 days from a quiet base, RUN in 15/20/25/30/40: episode counts and the lift of the 'quiet steady accumulation' signal.
(b) signal = per-date cross-sectional rank of three OWN-HISTORY changes (fixed a priori, equal weights, no tuning): lower routine_buyavg_cv_chg, lower churn_share_chg, lower two_sym_chg; flagged when the mean rank is in the
    top decile of the date. One entry per ticker per 12 trading days. Mechanics (entry open t+1, same-day touch of TP and SL counts as the stop): A TP+15%/SL-8%/15d | B TP+25%/SL-10%/15d | C hold 10 days (close).
    Net of 0.4% (and 1.0%) round-trip cost; compared with ALL stock-days of the same date block x ATR tercile under the same mechanics (ticker-clustered t).
(c) distribution profile: for +30% episodes, the cumulative net buying (net_val_mid, as a multiple of the stock's mean daily traded value) of the broker that was the dominant accumulator over days -9..0, by event day,
    and the share of episodes where that broker is a net seller around the peak high.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.gorengan_trade
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features
from research.ipot_explore.lead_1d import prep
from research.ipot_explore.mover_footprint import TOP, OUT
from research.ipot_explore.gorengan_episodes import episode_flags

RUNS = (0.15, 0.20, 0.25, 0.30, 0.40)
MECH = {"A TP15/SL8/15d": (0.15, -0.08, 15), "B TP25/SL10/15d": (0.25, -0.10, 15), "C hold10d": (None, None, 10)}


def sim_all(px: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for tk, g in px.sort_values(["ticker", "date"]).groupby("ticker"):
        o, h, l, c = (g[k].to_numpy() for k in ("open", "high", "low", "close"))
        n = len(g)
        res = {m: np.full(n, np.nan) for m in MECH}
        for i in range(n - 1):
            e = o[i + 1]
            if not e > 0:
                continue
            for m, (tp, sl, H) in MECH.items():
                if i + H >= n:
                    continue
                r = c[i + H] / e - 1
                if tp is not None:
                    for k in range(i + 1, i + H + 1):
                        if l[k] / e - 1 <= sl:
                            r = sl
                            break
                        if h[k] / e - 1 >= tp:
                            r = tp
                            break
                res[m][i] = r
        rows.append(pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy(), **res}))
    return pd.concat(rows, ignore_index=True)


def clus_t(x: pd.Series, tick: pd.Series) -> float:
    m = x.mean()
    cs = (x - m).groupby(tick.to_numpy()).sum()
    return m / (np.sqrt((cs ** 2).sum()) / len(x)) if len(x) > 1 else np.nan


def build():
    f1 = prep(None, common.OUT_DIR + "/features.pkl")
    f2 = prep(OUT, None)
    f = pd.concat([f1, f2[~f2.set_index(["ticker", "date"]).index.isin(f1.set_index(["ticker", "date"]).index)]], ignore_index=True)
    f = f.sort_values(["ticker", "date"]).reset_index(drop=True)
    px_all = common.load_ohlcv(start="2025-01-01")
    pxs = px_all[px_all.ticker.isin(f.ticker.unique())]
    dates = pd.DatetimeIndex(sorted(px_all["date"].unique()))
    tdi = {d: i for i, d in enumerate(dates)}
    parts = ("routine_buyavg_cv", "churn_share", "two_sym")
    for c in parts:
        x = f[c].astype(float).replace([np.inf, -np.inf], np.nan)
        f[c + "_chg"] = x - x.groupby(f["ticker"]).transform(lambda v: v.shift(10).rolling(60, min_periods=30).mean())
        f[c + "_r"] = 1 - f.groupby("date")[c + "_chg"].rank(pct=True)      # low change -> high rank
    f["score"] = f[[c + "_r" for c in parts]].mean(axis=1, skipna=False)
    f["sig"] = f.groupby("date")["score"].rank(pct=True) >= 0.9
    f = f.dropna(subset=["score"]).reset_index(drop=True)
    u = np.sort(f["date"].unique())
    k = len(u) // 3
    f["blk"] = np.where(f["date"] <= u[k], 1, np.where(f["date"] <= u[2 * k], 2, 3))
    f["vol"] = pd.qcut(f["atr_pct"], 3, labels=False, duplicates="drop")

    return f, pxs, px_all, tdi


def main():
    f, pxs, px_all, tdi = build()

    print("(a) THRESHOLDS: episode = +RUN within 15d from a quiet base")
    print(f"{'RUN':>5}{'episodes':>10}{'tickers':>9}{'base rate':>11}{'signal rate':>13}{'lift':>7}{'t (ticker-clust)':>18}")
    for run in RUNS:
        ep = episode_flags(pxs, run)
        d = f.merge(ep, on=["ticker", "date"], how="inner").dropna(subset=["fmax15"])
        y = d["ep"].astype(float)
        s = d[d.sig]
        diff = s.ep.astype(float) - y.mean()
        print(f"{100 * run:>4.0f}%{int(y.sum()):>10}{d[d.ep].ticker.nunique():>9}{100 * y.mean():>10.2f}%{100 * s.ep.mean():>12.2f}%{s.ep.mean() / y.mean():>7.2f}{clus_t(diff, s.ticker):>18.1f}")

    print("\n(b) TRADING the signal (top decile of the quiet-accumulation score per date), one entry per ticker per 12 trading days, net of cost")
    R = sim_all(pxs)
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
    print(f"signal rows {int(D.sig.sum())}, deduplicated entries {len(S)} ({S.ticker.nunique()} tickers); by block: {S.groupby('blk').size().to_dict()}")
    for cost in (0.004, 0.010):
        print(f"\n  round-trip cost {100 * cost:.1f}%:")
        for m in MECH:
            ok = S.dropna(subset=[m])
            bcell = D.dropna(subset=[m]).groupby(["blk", "vol"])[m].mean()
            ref = pd.Series([bcell.get((b, v), np.nan) for b, v in zip(ok.blk, ok.vol)], index=ok.index)
            net = ok[m] - cost
            diff = ok[m] - ref
            byb = " ".join(f"b{b}:{100 * (ok[ok.blk == b][m].mean() - cost):+.2f}%(n{int((ok.blk == b).sum())})" for b in (1, 2, 3))
            print(f"   {m:<16} entries {len(ok):>4} | net mean {100 * net.mean():+.2f}% (t {clus_t(net, ok.ticker):+.1f}) win {100 * (net > 0).mean():.0f}% | same-cell base net {100 * (ref.mean() - cost):+.2f}% "
                  f"| excess over base {100 * diff.mean():+.2f}pp (t {clus_t(diff, ok.ticker):+.1f}) | {byb}")

    print("\n(c) DISTRIBUTION PROFILE around +30% episodes (dominant accumulator over days -9..0)")
    raw = pd.concat([common.load_ipot(TOP), common.load_ipot(OUT)], ignore_index=True)
    a = features.broker_day_table(raw, "all", "all")[["ticker", "date", "broker", "net_val_mid"]].dropna().drop_duplicates(["ticker", "date", "broker"])
    by_tk = {tk: g for tk, g in a.groupby("ticker")}
    px_by = {tk: g.sort_values("date").reset_index(drop=True) for tk, g in px_all.groupby("ticker")}
    ep = episode_flags(pxs, 0.30)
    E = ep[ep.ep][["ticker", "date"]]
    prof, peak_sell, n_ok = [], [], 0
    for _, r in E.iterrows():
        g = px_by.get(r.ticker)
        ra = by_tk.get(r.ticker)
        if g is None or ra is None:
            continue
        i = int(np.searchsorted(g["date"].to_numpy(), np.datetime64(r.date)))
        if i < 12 or i + 21 > len(g):
            continue
        win = g["date"].iloc[i - 9:i + 1].to_numpy()
        pre = ra[ra.date.isin(win)].groupby("broker")["net_val_mid"].sum()
        if pre.empty or pre.max() <= 0:
            continue
        b = pre.idxmax()
        dv = float((g["close"] * g["volume"]).iloc[max(i - 29, 0):i + 1].mean())
        if not dv > 0:
            continue
        seq = g["date"].iloc[i - 9:i + 21].to_numpy()
        rb = ra[ra.broker == b].set_index("date")["net_val_mid"].reindex(seq).fillna(0.0).to_numpy() / dv
        cum = np.cumsum(rb)
        prof.append(cum - cum[9])
        hi = g["high"].iloc[i + 1:i + 16].to_numpy()
        pk = i + 1 + int(np.argmax(hi))
        near = ra[(ra.broker == b) & ra.date.isin(g["date"].iloc[max(pk - 2, 0):pk + 4].to_numpy())]["net_val_mid"]
        if len(near):
            peak_sell.append(bool(near.sum() < 0))
        n_ok += 1
    P = np.array(prof)
    if len(P):
        ev = list(np.arange(-9, 21))
        days = (-9, -5, 0, 3, 5, 8, 10, 15, 20)
        print(f"episodes profiled {n_ok} | dominant accumulator's cumulative net (x mean daily value) relative to day 0:")
        print("   event day: " + " ".join(f"{d:>6}" for d in days))
        print("   mean cum : " + " ".join(f"{P[:, ev.index(d)].mean():>6.2f}" for d in days))
        print("   median   : " + " ".join(f"{np.median(P[:, ev.index(d)]):>6.2f}" for d in days))
        print(f"   dominant accumulator is a net SELLER in the window peak-2..peak+3 in {100 * np.mean(peak_sell):.0f}% of {len(peak_sell)} episodes")


if __name__ == "__main__":
    main()
