"""
research/ipot_explore/bigwinner_risk.py — cut the LEFT tail of the right-tail signal (EXP-BIGWIN-2, registered before running).
Picks = the CLEAN, de-duplicated top-1% picks of the price-only walk-forward GBM of bigwinner_final.py (same data, 468 picks, plus the liquid-executable slice close >= 200 & value >= Rp1bn).
Only the trade management changes; each variant is simulated on the daily path from open(t+1) with the conservative rules (stop checked before the day's high updates trails; gap through the stop exits at the open):
  BASE   stop 12%, time exit day 20 | S4/S6/S8/S10  fixed stop at 4/6/8/10% | TR10/TR15  stop 12% initially, then trailing 10%/15% below the highest high since entry
  BE     stop 12%; once the high reached +10% the stop moves to breakeven, once +20% to +8%
  NP3/NP5/NP8  stop 12%; if at the close of day 3/5/8 the position is not at least +2% above entry, exit at that close
  CONF   skip the trade unless day 1 closes above its open and not below the entry by more than 1%; enter at the day-1 CLOSE, then stop 12%
  COMBO  stop 8% + breakeven rule BE + no-progress exit NP5
Reported per variant: trades, mean net, median, win rate, average loser, average winner, 5th/10th percentile of net, share of trades losing more than 10%, profit factor, mean/average-loss, mean without the best 5 trades,
and the number of test quarters with a positive mean.  Costs 0.4% round trip (+1% extra slippage shown for the finalists).
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.bigwinner_risk
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from research.ipot_explore.bigwinner import build, PRICE_F, PURGE
from research.ipot_explore.entry_scenarios import simulate, COST


def get_picks():
    px = build()
    g = px.groupby("ticker", observed=True)
    px["fmax"] = g["ret1"].transform(lambda s: s[::-1].rolling(20, min_periods=1).max()[::-1].shift(-1))
    px["fmin"] = g["ret1"].transform(lambda s: s[::-1].rolling(20, min_periods=1).min()[::-1].shift(-1))
    px["gap_next"] = g["open"].shift(-1) / px["close"] - 1
    rows_all = np.where((px["elig"] & (px["nobs"] % 3 == 0)).to_numpy())[0]
    filled, ret = simulate(px, rows_all, None, 9.99, 0.12, 20, hmax=20)
    rows = rows_all[filled]
    D = px.iloc[rows].copy()
    D["pxrow"] = rows
    D["net"] = ret[filled] - COST
    D["ticker_s"] = D["ticker"].astype(str)
    D["BIG20"] = (D["net"] >= 0.20).astype(int)
    D["q"] = D["date"].dt.to_period("Q")
    D["susp"] = ((D["fmax"] >= 0.40) | (D["fmin"] <= -0.30) | (D["gap_next"].abs() >= 0.40)).astype(int)
    F = PRICE_F
    D[F] = D[F].replace([np.inf, -np.inf], np.nan)
    D = D.dropna(subset=F + ["gap_next", "fmax"])
    D = D[D.susp == 0].reset_index(drop=True)
    for col in F:
        lo, hi = D[col].quantile([0.005, 0.995])
        D[col] = D[col].clip(lo, hi)
    tdates = np.sort(D["date"].unique())
    parts = []
    for q in sorted(D["q"].unique()):
        if q < pd.Period("2024Q3"):
            continue
        te = D["q"] == q
        cut = tdates[max(np.searchsorted(tdates, np.datetime64(D.loc[te, "date"].min())) - PURGE, 0)]
        tr = D["date"] < cut
        if tr.sum() < 15000 or te.sum() < 500:
            continue
        m = HistGradientBoostingClassifier(max_iter=250, max_depth=4, learning_rate=0.04, l2_regularization=1.0, random_state=7).fit(D.loc[tr, F].to_numpy(float), D.loc[tr, "BIG20"])
        T = D.loc[te].copy()
        T["p"] = m.predict_proba(T[F].to_numpy(float))[:, 1]
        parts.append(T)
    T = pd.concat(parts)
    return px, D, T


def dedup(S: pd.DataFrame) -> pd.DataFrame:
    S = S.sort_values(["ticker_s", "date"])
    keep, last = [], {}
    for idx, tk, dt in zip(S.index, S.ticker_s, S.date):
        if tk not in last or (dt - last[tk]).days >= 28:
            keep.append(idx)
            last[tk] = dt
    return S.loc[keep]


def manage(px: pd.DataFrame, r: int, v: str):
    """Simulate one trade entered after the signal row r. Returns the gross return or nan (skipped)."""
    O, H, L, C = (px[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(px)
    if r + 20 >= n:
        return np.nan
    E = O[r + 1]
    start = 1
    if v == "CONF":
        if not (C[r + 1] > O[r + 1] and C[r + 1] >= E * 0.99):
            return np.nan
        E = C[r + 1]
        start = 2
    stop0 = {"S4": 0.04, "S6": 0.06, "S8": 0.08, "S10": 0.10}.get(v, 0.08 if v == "COMBO" else 0.12)
    stop = E * (1 - stop0)
    hh = E if start == 1 else max(O[r + 1], H[r + 1])
    be1 = be2 = False
    for k in range(start, 21):
        i = r + k
        if k > start - 1 and O[i] <= stop:
            return O[i] / E - 1
        if L[i] <= stop:
            return stop / E - 1
        # state updates after the stop check (end of day information)
        hh = max(hh, H[i])
        if v in ("BE", "COMBO"):
            if hh >= E * 1.20 and not be2:
                stop, be2, be1 = max(stop, E * 1.08), True, True
            elif hh >= E * 1.10 and not be1:
                stop, be1 = max(stop, E), True
        if v in ("TR10", "TR15"):
            t = 0.10 if v == "TR10" else 0.15
            stop = max(stop, hh * (1 - t))
        nk = {"NP3": 3, "NP5": 5, "NP8": 8}.get(v, 5 if v == "COMBO" else None)
        if nk and k == nk and C[i] < E * 1.02:
            return C[i] / E - 1
    return C[r + 20] / E - 1


def summary(net: np.ndarray, q: np.ndarray | None = None) -> dict:
    net = net[~np.isnan(net)]
    los, win = net[net < 0], net[net > 0]
    s = np.sort(net)[::-1]
    out = dict(n=len(net), mean=net.mean(), med=np.median(net), win=(net > 0).mean(), avg_loss=los.mean() if len(los) else np.nan, avg_win=win.mean() if len(win) else np.nan,
               p5=np.percentile(net, 5), p10=np.percentile(net, 10), big_loss=(net <= -0.10).mean(), pf=win.sum() / -los.sum() if len(los) else np.nan,
               payoff=net.mean() / -los.mean() if len(los) else np.nan, ex5=s[5:].mean() if len(s) > 10 else np.nan)
    return out


def main():
    px, D, T = get_picks()
    S = T[T["p"] >= T.groupby("q")["p"].transform(lambda s: s.quantile(0.99))]
    SD = dedup(S)
    print(f"picks (clean, de-duplicated top-1%): {len(SD)} | liquid slice (close>=200 & value>=Rp1bn): {int(((SD.close >= 200) & (SD.val20 >= 1e9)).sum())}")
    variants = ["BASE", "S10", "S8", "S6", "S4", "TR15", "TR10", "BE", "NP8", "NP5", "NP3", "CONF", "COMBO"]
    for label, P in (("ALL PICKS", SD), ("LIQUID SLICE", SD[(SD.close >= 200) & (SD.val20 >= 1e9)])):
        print(f"\n===== {label} (n={len(P)}) net of {100 * COST:.1f}% cost =====")
        print(f"{'variant':<7}{'n':>5}{'mean':>8}{'median':>8}{'win':>6}{'avgLoss':>9}{'avgWin':>8}{'P5':>8}{'P10':>8}{'<=-10%':>8}{'PF':>6}{'payoff':>8}{'ex-best5':>9}{'qtrs+':>7}")
        for v in variants:
            gross = np.array([manage(px, int(r), v) for r in P["pxrow"].to_numpy()])
            net = gross - COST
            s = summary(net)
            pq = pd.Series(net, index=P.index).groupby(P["q"]).mean()
            print(f"{v:<7}{s['n']:>5}{100 * s['mean']:>7.2f}%{100 * s['med']:>7.1f}%{100 * s['win']:>5.0f}%{100 * s['avg_loss']:>8.1f}%{100 * s['avg_win']:>7.1f}%{100 * s['p5']:>7.1f}%{100 * s['p10']:>7.1f}%{100 * s['big_loss']:>7.0f}%{s['pf']:>6.2f}{s['payoff']:>8.2f}{100 * s['ex5']:>8.2f}%{int((pq > 0).sum()):>4}/{len(pq)}")
    print("\nextra 1% slippage on the finalists (ALL PICKS): mean net")
    for v in ("BASE", "S8", "TR15", "BE", "NP5", "CONF", "COMBO"):
        gross = np.array([manage(px, int(r), v) for r in SD["pxrow"].to_numpy()])
        print(f"   {v:<6} {100 * (np.nanmean(gross) - COST - 0.01):+.2f}%")


if __name__ == "__main__":
    main()
