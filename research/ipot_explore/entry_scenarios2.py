"""
research/ipot_explore/entry_scenarios2.py — EXP-ENTRY-2 (registered before running): strength-based and trend-based entries with longer holds, plus 'buy the retest' limit entries, on the same 3.8 years of OHLCV, with the same
discovery (signal date <= 2025-06-30) / validation (after) protocol, costs, and conservative fills as entry_scenarios.py.
Signals (cooldown 20 trading days per ticker and type, known at the close of t): BRK_H60 close above the previous 60-day high | BRK_BB close above the upper Bollinger band right after a squeeze (bandwidth in its own lowest quintile of the last 120 days)
| BRK_V5 volume >= 5x the previous 20-day mean with ret1 >= +3% | TREND_ST first day of close > MA20 > MA50, MA20 higher than 5 days ago, RSI in [50, 75], close <= 1.10 x MA20 | REV_MA close crosses above MA10 (MA10 rising 3 days), close <= 85% of its 50-day high
| UPDIP reference (uptrend, red day, as before) -- UPDIP uses every 3rd eligible day.
Entry modes: M0 market at open(t+1) | L2 limit buy 2% under close(t) (the 'retest' / buy-cheap version).
Exits (TP, SL, days): X1 none/10/10 | X2 none/10/20 | X3 none/12/20 | X4 20/10/20 | X5 10/7/10 (the quick one used before). 'none' = hold to the time exit unless the stop is hit.
Reported: discovery vs validation net means and t, the control (every 7th eligible stock-day, M0) per exit, the selection on discovery (net mean > 0 and t >= 2, one-sided BH) with the untouched validation result, and a by-IHSG-regime table for the finalists.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.entry_scenarios2
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common
from research.ipot_explore.breakout_backing import norm_p
from research.ipot_explore.entry_scenarios import load, regimes, simulate, clus_t, COST, SPLIT

MODES = {"M0": None, "L2": 0.02}
EXITS = {"X1": (9.99, 0.10, 10), "X2": (9.99, 0.10, 20), "X3": (9.99, 0.12, 20), "X4": (0.20, 0.10, 20), "X5": (0.10, 0.07, 10)}


def cooldown(mask: pd.Series, px: pd.DataFrame, gap: int = 20) -> np.ndarray:
    rows = np.where(mask.to_numpy())[0]
    tk = px["ticker"].cat.codes.to_numpy()[rows]
    keep, last_t, last_i = [], -1, -999
    for r, t in zip(rows, tk):
        if t != last_t or r - last_i >= gap:
            keep.append(r)
            last_t, last_i = t, r
    return np.array(keep)


def one_sided_bh(p: np.ndarray, q: float = 0.05) -> np.ndarray:
    n = len(p)
    order = np.argsort(p)
    ok = p[order] <= q * (np.arange(1, n + 1) / n)
    k = (np.max(np.where(ok)[0]) + 1) if ok.any() else 0
    keep = np.zeros(n, bool)
    keep[order[:k]] = True
    return keep


def main():
    px = load()
    px["ticker"] = px["ticker"].astype("category")
    px = px.merge(regimes(), on="date", how="left")
    px["regime_phase"] = px["regime_phase"].fillna("NA")
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    g = px.groupby("ticker", observed=True)
    c = px["close"]
    px["hh60"] = g["close"].transform(lambda s: s.shift(1).rolling(60).max())
    px["hi50"] = g["close"].transform(lambda s: s.rolling(50).max())
    sd20 = g["close"].transform(lambda s: s.rolling(20).std())
    px["bw"] = 4 * sd20 / px["ma20"]
    px["bwq"] = g["bw"].transform(lambda s: s.rolling(120, min_periods=60).quantile(0.2)) if "bw" in px else np.nan
    px["bwq"] = px.groupby("ticker", observed=True)["bw"].transform(lambda s: s.rolling(120, min_periods=60).quantile(0.2))
    px["ma10"] = g["close"].transform(lambda s: s.rolling(10).mean())
    elig = (px["close"] >= 50) & (px["val20"] >= 0.1e9) & (px["nobs"] >= 130) & (px["date"] >= "2023-01-01")
    ma20_up = px["ma20"] > g["ma20"].shift(5)
    trend_state = (c > px["ma20"]) & (px["ma20"] > px["ma50"]) & ma20_up & (px["rsi"] >= 50) & (px["rsi"] <= 75) & (c <= 1.10 * px["ma20"])
    sq_prev = px.groupby("ticker", observed=True)["bw"].shift(1) <= px.groupby("ticker", observed=True)["bwq"].shift(1)
    raw_sig = {
        "BRK_H60": (c > px["hh60"]) & elig,
        "BRK_BB": (c > px["ma20"] + 2 * sd20) & sq_prev & elig,
        "BRK_V5": (px["vr"] >= 5) & (px["ret1"] >= 0.03) & elig,
        "TREND_ST": trend_state & ~trend_state.groupby(px["ticker"], observed=True).shift(1, fill_value=False) & elig,
        "REV_MA": (c > px["ma10"]) & (g["close"].shift(1) <= px.groupby("ticker", observed=True)["ma10"].shift(1)) & (px["ma10"] > px.groupby("ticker", observed=True)["ma10"].shift(3)) & (c <= 0.85 * px["hi50"]) & elig,
    }
    sig_rows = {k: cooldown(v, px) for k, v in raw_sig.items()}
    upd = np.where(((c > px["ma50"]) & (px["ma20"] > px["ma50"]) & (px["ret1"] <= 0) & elig).to_numpy())[0]
    sig_rows["UPDIP"] = upd[::3]
    sig_rows["Z_ALL"] = np.where(elig.to_numpy())[0][::7]
    tkc = px["ticker"].cat.codes.to_numpy()
    dts = px["date"].to_numpy()
    reg = px["regime_phase"].to_numpy()
    print(f"price rows {len(px)}; signal counts: " + ", ".join(f"{k} {len(v)}" for k, v in sig_rows.items()))
    recs, store = [], {}
    for sname, rows in sig_rows.items():
        disc = dts[rows] <= np.datetime64(SPLIT)
        for mname, d in MODES.items():
            for ename, (tp, sl, H) in EXITS.items():
                filled, ret = simulate(px, rows, d, tp, sl, H, hmax=20)
                net = ret - COST
                rec = dict(signal=sname, mode=mname, exit=ename, fill=float(filled.mean()))
                for per, m in (("disc", disc), ("val", ~disc)):
                    mm = filled & m
                    x = net[mm]
                    rec[f"n_{per}"] = int(mm.sum())
                    rec[f"mean_{per}"] = float(x.mean()) if len(x) else np.nan
                    rec[f"win_{per}"] = float((x > 0).mean()) if len(x) else np.nan
                    rec[f"t_{per}"] = clus_t(x, tkc[rows][mm]) if len(x) > 20 else np.nan
                recs.append(rec)
                store[(sname, mname, ename)] = (rows, filled, net)
    G = pd.DataFrame(recs)
    G["p_pos"] = G["t_disc"].map(lambda t: 0.5 * np.exp(-0.717 * t - 0.416 * t * t) if pd.notna(t) and t > 0 else 1.0)
    G["bh_pos"] = one_sided_bh(G["p_pos"].to_numpy())
    pd.set_option("display.width", 220)
    cols = ["signal", "mode", "exit", "fill", "n_disc", "mean_disc", "win_disc", "t_disc", "n_val", "mean_val", "win_val", "t_val"]
    fmt = lambda d: d.assign(**{k: (d[k] * 100).round(2) for k in ("mean_disc", "mean_val", "win_disc", "win_val", "fill")}).round({"t_disc": 1, "t_val": 1})
    print(f"\nGRID: {len(G)} scenarios | discovery net mean > 0 and t >= 2: {int(((G.mean_disc > 0) & (G.t_disc >= 2)).sum())} | one-sided BH survivors (positive side): {int(G.bh_pos.sum())}")
    print("\nCONTROL (every 7th eligible stock-day, M0) by exit, discovery | validation net mean:")
    for r in G[(G.signal == "Z_ALL") & (G["mode"] == "M0")].itertuples():
        print(f"   {r.exit}: {100 * r.mean_disc:+.2f}% (t {r.t_disc:+.1f}) | {100 * r.mean_val:+.2f}% (t {r.t_val:+.1f})")
    print("\nby signal x mode (average over exits): discovery net %, validation net %")
    print((G.groupby(["signal", "mode"])[["mean_disc", "mean_val"]].mean() * 100).round(2).to_string())
    cand = G[(G.mean_disc > 0) & (G.t_disc >= 2)].sort_values("t_disc", ascending=False)
    print(f"\nSELECTED ON DISCOVERY (net mean > 0, t >= 2): {len(cand)} scenarios; validation net mean > 0 in {int((cand.mean_val > 0).sum())}, validation t >= 2 in {int((cand.t_val >= 2).sum())}")
    print(fmt(cand.head(20))[cols].to_string(index=False))
    fin = cand.head(6)
    print("\nBY IHSG REGIME for the 6 leading discovery scenarios (net mean %, n) -- discovery+validation pooled, descriptive:")
    for r in fin.itertuples():
        rows, filled, net = store[(r.signal, r.mode, r.exit)]
        line = []
        for rg in ("BEARISH_GRIND", "BOUNCE_OFF_LOW", "CRASH_FAST", "NEUTRAL_BULLISH", "PULLBACK_IN_BOUNCE"):
            m = filled & (reg[rows] == rg)
            line.append(f"{rg[:9]} {100 * net[m].mean():+.2f}% (n{int(m.sum())})" if m.sum() >= 30 else f"{rg[:9]} n{int(m.sum())}")
        print(f"   {r.signal} {r.mode} {r.exit}: " + " | ".join(line))
    G.to_csv(common.OUT_DIR + "/entry_scenarios2_grid.csv", index=False)


if __name__ == "__main__":
    main()
