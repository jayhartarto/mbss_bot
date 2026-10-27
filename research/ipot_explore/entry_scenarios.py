"""
research/ipot_explore/entry_scenarios.py — 'buy cheap, sell dear': a scenario search on 3.8 years of daily OHLCV (684 tickers), independent of the broker data. (experiment family EXP-ENTRY-1, registered before running)

Signals (known at the close of t): A_DROP  ret1 <= -4% and close > MA50 | B_PULL  close <= 0.92 x its 10-day high, RSI14 <= 40, close > MA100 | C_CAPIT  volume >= 3x its previous-20-day mean and ret1 <= -5%
| D_OVERS  RSI14 <= 30 and close below the lower Bollinger band (20, 2) | F_UPDIP  uptrend (close > MA50, MA20 > MA50) on a red day (ret1 <= 0) | Z_ALL  control: every eligible stock-day.
Eligibility (execution realism, not a research gate): close >= 50, 20-day average traded value >= Rp0.1bn, at least 100 days of history.
Entry modes: M0 market buy at open(t+1) | L1/L2/L3  limit buy at close(t) x (1 - 1/2/3%): filled at the open if open <= limit, else at the limit if low(t+1) <= limit, else no trade.
Exits (TP, SL, max days; the entry day counts as day 1): E1 5/5/10 | E2 8/6/10 | E3 10/7/10 | E4 6/4/5 | E5 12/8/10. Same-day touch of TP and SL = stop; a gap through the stop exits at the open; after an intraday limit fill the TP cannot trigger on the fill day.
Costs: 0.4% round trip (also 1.0% shown for the finalists). Periods by SIGNAL date: DISCOVERY 2023-01..2025-06, VALIDATION 2025-07..2026-10.
Scenario selection is made on DISCOVERY only (net mean > 0 and ticker-clustered t >= 2, BH over the whole grid); the validation numbers are reported untouched.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.entry_scenarios
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common
from research.ipot_explore.breakout_backing import norm_p, bh

COST = 0.004
SPLIT = pd.Timestamp("2025-06-30")
MODES = {"M0": None, "L1": 0.01, "L2": 0.02, "L3": 0.03}
EXITS = {"E1": (0.05, 0.05, 10), "E2": (0.08, 0.06, 10), "E3": (0.10, 0.07, 10), "E4": (0.06, 0.04, 5), "E5": (0.12, 0.08, 10)}
HMAX = 10


def load() -> pd.DataFrame:
    px = common.load_ohlcv(start="2022-06-01")
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    g = px.groupby("ticker")
    c, v = px["close"], px["volume"]
    px["ret1"] = g["close"].pct_change()
    px["ma20"] = g["close"].transform(lambda s: s.rolling(20).mean())
    px["ma50"] = g["close"].transform(lambda s: s.rolling(50).mean())
    px["ma100"] = g["close"].transform(lambda s: s.rolling(100).mean())
    sd20 = g["close"].transform(lambda s: s.rolling(20).std())
    px["bbl"] = px["ma20"] - 2 * sd20
    d = g["close"].diff()
    up = d.clip(lower=0).groupby(px["ticker"]).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
    dn = (-d.clip(upper=0)).groupby(px["ticker"]).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
    px["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    px["hi10"] = g["close"].transform(lambda s: s.rolling(10).max())
    px["vr"] = v / g["volume"].transform(lambda s: s.shift(1).rolling(20).mean())
    px["val20"] = (c * v).groupby(px["ticker"]).transform(lambda s: s.rolling(20).mean())
    px["nobs"] = g.cumcount()
    return px[px["date"] >= "2023-01-01"].reset_index(drop=True) if False else px


def regimes() -> pd.DataFrame:
    import yfinance as yf
    from engine.ihsg_regime import _build_regime
    hist = yf.Ticker("^JKSE").history(period="6y")
    hist.index = hist.index.tz_localize(None)
    r = _build_regime(hist)
    dcol = "Date" if "Date" in r.columns else r.columns[0]
    r["date"] = pd.to_datetime(r[dcol]).dt.normalize().astype("datetime64[ns]")
    return r[["date", "regime_phase"]]


def simulate(px: pd.DataFrame, rows: np.ndarray, mode: float | None, tp: float, sl: float, H: int, hmax: int = HMAX):
    """Vectorised trade simulation for the signal rows `rows` (indices into the sorted px). Returns (filled mask, net return array) over the rows that have a full horizon."""
    N = len(px)
    O, Hh, L, C = (px[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    T = px["ticker"].cat.codes.to_numpy() if hasattr(px["ticker"], "cat") else pd.factorize(px["ticker"])[0]
    ks = np.arange(1, max(hmax, H) + 1)
    idx = rows[:, None] + ks[None, :]
    inb = idx < N
    idxc = np.where(inb, idx, N - 1)
    same = inb & (T[idxc] == T[rows][:, None])
    full = same[:, :H].all(axis=1)
    Fo, Fh, Fl, Fc = O[idxc], Hh[idxc], L[idxc], C[idxc]
    C0 = C[rows]
    if mode is None:
        fill_open = np.ones(len(rows), bool)
        fill_intra = np.zeros(len(rows), bool)
        E = Fo[:, 0]
    else:
        lim = C0 * (1 - mode)
        fill_open = Fo[:, 0] <= lim
        fill_intra = (~fill_open) & (Fl[:, 0] <= lim)
        E = np.where(fill_open, Fo[:, 0], np.where(fill_intra, lim, np.nan))
    filled = (fill_open | fill_intra) & full
    TP = E * (1 + tp)
    SL = E * (1 - sl)
    SLh = Fl[:, :H] <= SL[:, None]
    TPh = Fh[:, :H] >= TP[:, None]
    TPh[:, 0] &= ~fill_intra
    sl_i = np.where(SLh.any(1), SLh.argmax(1), 99)
    tp_i = np.where(TPh.any(1), TPh.argmax(1), 99)
    rr = np.arange(len(rows))
    sl_k = np.minimum(sl_i, H - 1)
    gap = (sl_i > 0) & (Fo[rr, sl_k] <= SL)
    price = np.where(sl_i <= tp_i, np.where(gap, Fo[rr, sl_k], SL), np.where(tp_i < 99, TP, Fc[:, H - 1]))
    price = np.where((sl_i == 99) & (tp_i == 99), Fc[:, H - 1], price)
    ret = price / E - 1
    return filled, ret


def clus_t(x: np.ndarray, groups: np.ndarray) -> float:
    if len(x) < 5:
        return np.nan
    m = x.mean()
    cs = pd.Series(x - m).groupby(groups).sum()
    s = np.sqrt((cs ** 2).sum()) / len(x)
    return m / s if s > 0 else np.nan


def main():
    px = load()
    px["ticker"] = px["ticker"].astype("category")
    reg = regimes()
    px = px.merge(reg, on="date", how="left")
    px["regime_phase"] = px["regime_phase"].fillna("NA")
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    elig = (px["close"] >= 50) & (px["val20"] >= 0.1e9) & (px["nobs"] >= 100) & (px["date"] >= "2023-01-01")
    sig = {
        "A_DROP": (px.ret1 <= -0.04) & (px.close > px.ma50),
        "B_PULL": (px.close <= 0.92 * px.hi10) & (px.rsi <= 40) & (px.close > px.ma100),
        "C_CAPIT": (px.vr >= 3) & (px.ret1 <= -0.05),
        "D_OVERS": (px.rsi <= 30) & (px.close < px.bbl),
        "F_UPDIP": (px.close > px.ma50) & (px.ma20 > px.ma50) & (px.ret1 <= 0),
        "Z_ALL": pd.Series(True, index=px.index),
    }
    print(f"price rows {len(px)} ({px.ticker.nunique()} tickers, {px.date.min().date()}..{px.date.max().date()}); eligible stock-days {int(elig.sum())}")
    rows_out = []
    detail = {}
    for sname, s in sig.items():
        rows = np.where((s & elig).to_numpy())[0]
        if sname == "Z_ALL":
            rows = rows[::7]              # control: every 7th eligible stock-day (cost); mechanics are identical
        dts = px["date"].to_numpy()[rows]
        tk = px["ticker"].cat.codes.to_numpy()[rows]
        disc = dts <= np.datetime64(SPLIT)
        print(f"  signal {sname}: {len(rows)} rows (discovery {int(disc.sum())}, validation {int((~disc).sum())})")
        for mname, d in MODES.items():
            for ename, (tp, sl, H) in EXITS.items():
                filled, ret = simulate(px, rows, d, tp, sl, H)
                net = ret - COST
                rec = dict(signal=sname, mode=mname, exit=ename, n_sig=len(rows), fill=float(filled.mean()))
                for per, m in (("disc", disc), ("val", ~disc)):
                    mm = filled & m
                    x = net[mm]
                    rec[f"n_{per}"] = int(mm.sum())
                    rec[f"mean_{per}"] = float(x.mean()) if len(x) else np.nan
                    rec[f"win_{per}"] = float((x > 0).mean()) if len(x) else np.nan
                    rec[f"t_{per}"] = clus_t(x, tk[mm]) if len(x) > 20 else np.nan
                rows_out.append(rec)
                detail[(sname, mname, ename)] = (rows, filled, net)
    G = pd.DataFrame(rows_out)
    G["p_disc"] = G["t_disc"].map(lambda t: norm_p(t) if pd.notna(t) else 1.0)
    G["bh_disc"] = bh(G["p_disc"].to_numpy())
    pd.set_option("display.width", 220)
    print(f"\nGRID: {len(G)} scenarios (6 signals x 4 entry modes x 5 exits); discovery net mean>0 & t>=2: {int(((G.mean_disc > 0) & (G.t_disc >= 2)).sum())}; BH survivors on discovery: {int(G.bh_disc.sum())}")
    ctrl = G[G.signal == "Z_ALL"]
    print("\nCONTROL (every eligible stock-day, market buy at the next open): net mean by exit, discovery | validation")
    for r in ctrl[ctrl["mode"] == "M0"].itertuples():
        print(f"   {r.exit}: disc {100 * r.mean_disc:+.2f}% win {100 * r.win_disc:.0f}% | val {100 * r.mean_val:+.2f}% win {100 * r.win_val:.0f}%")
    cols = ["signal", "mode", "exit", "fill", "n_disc", "mean_disc", "win_disc", "t_disc", "n_val", "mean_val", "win_val", "t_val"]
    fmt = lambda df: df.assign(**{c: (df[c] * 100).round(2) for c in ("mean_disc", "mean_val", "win_disc", "win_val", "fill")}).round({"t_disc": 1, "t_val": 1})
    cand = G[(G.mean_disc > 0) & (G.t_disc >= 2)].sort_values("t_disc", ascending=False)
    print("\nSCENARIOS SELECTED ON DISCOVERY (net mean>0, t>=2) with their untouched VALIDATION result (means/win/fill in %):")
    print(fmt(cand.head(24))[cols].to_string(index=False))
    print(f"\nof the {len(cand)} selected: validation net mean > 0 in {int((cand.mean_val > 0).sum())} ({100 * (cand.mean_val > 0).mean():.0f}%), validation t >= 2 in {int((cand.t_val >= 2).sum())}")
    print("\nDOES WAITING FOR A DISCOUNT HELP? mean net by entry mode, averaged over signals (excl. control) and exits, discovery | validation (filled trades only)")
    h = G[G.signal != "Z_ALL"].groupby("mode").agg(mean_disc=("mean_disc", "mean"), mean_val=("mean_val", "mean"), fill=("fill", "mean"), n_val=("n_val", "sum"))
    print((h * [100, 100, 100, 1]).round(2).to_string())
    G.to_csv(common.OUT_DIR + "/entry_scenarios_grid.csv", index=False)


if __name__ == "__main__":
    main()
