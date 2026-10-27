"""
research/ipot_explore/xl_study.py — focused test of the broker-skill lead "XL (retail app) as the #1 net buyer of a ticker-day is followed by under-performance".

Run (project root):  python -m research.ipot_explore.xl_study [--db research/ipot_broker_daily_vps_2026_10_22.sqlite] [--B 500]

Event: broker XL is the top net buyer (net_val_mid>0, rank 1) of a ticker-day. Outcome: rel_{h}d minus that ticker's own mean (so XL gets no credit/blame for the stocks it
happens to trade). Reported per horizon: mean, ticker-clustered t and date-clustered t, then splits by 3 date blocks, IHSG regime phase, liquidity tercile and
first-event-of-streak only. Controls: the other retail-app brokers (XC, YP, PD) and a random same-size set of events. Null: per-ticker circular shift of the outcome.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from research.ipot_explore import common, features, broker_skill
from engine.ihsg_regime import _build_regime

H = (3, 5, 10)


def ihsg_phase() -> pd.DataFrame:
    import yfinance as yf
    hist = yf.Ticker("^JKSE").history(period="3y")
    hist.index = hist.index.tz_localize(None)
    r = _build_regime(hist)
    dcol = "Date" if "Date" in r.columns else r.columns[0]
    r["date"] = pd.to_datetime(r[dcol]).dt.normalize().astype("datetime64[ns]")
    return r[["date", "regime_phase"]]


def events(raw: pd.DataFrame, out: pd.DataFrame, brokers: list[str], side: str = "buy") -> pd.DataFrame:
    a = features.broker_day_table(raw, "all", "all")
    sign = 1.0 if side == "buy" else -1.0
    a = a[(sign * a["net_val_mid"]) > 0].copy()
    a["rk"] = a.groupby(["ticker", "date"])["net_val_mid"].rank(ascending=(side == "sell"), method="first")
    ev = a[(a["rk"] == 1) & a["broker"].isin(brokers)][["ticker", "date", "broker"]].copy()
    o = out.copy()
    for h in H:
        o[f"y{h}"] = o[f"rel_{h}d"] - o.groupby("ticker")[f"rel_{h}d"].transform("mean")
    ev = ev.merge(o[["ticker", "date", "val20"] + [f"y{h}" for h in H]], on=["ticker", "date"], how="left")
    ev = ev.sort_values(["ticker", "date"])
    gap = ev.groupby("ticker")["date"].diff().dt.days
    ev["first_of_streak"] = gap.isna() | (gap > 4)
    return ev


def stat(ev: pd.DataFrame, ycol: str):
    g = ev.dropna(subset=[ycol])
    if len(g) < 30:
        return len(g), np.nan, np.nan, np.nan
    mu = g[ycol].mean()
    ct = g.groupby("ticker")[ycol].apply(lambda s: float((s - mu).sum()))
    cd = g.groupby("date")[ycol].apply(lambda s: float((s - mu).sum()))
    return len(g), mu, mu / (np.sqrt((ct ** 2).sum()) / len(g)), mu / (np.sqrt((cd ** 2).sum()) / len(g))


def shift_null(ev_keys: pd.DataFrame, out: pd.DataFrame, h: int, B: int, seed: int = 7) -> np.ndarray:
    """Mean of the event outcome when each ticker's demeaned outcome series is circularly shifted by a random offset."""
    rng = np.random.default_rng(seed)
    o = out.sort_values(["ticker", "date"]).copy()
    o["y"] = o[f"rel_{h}d"] - o.groupby("ticker")[f"rel_{h}d"].transform("mean")
    o = o.reset_index(drop=True)
    idx = o.groupby("ticker").indices
    key = ev_keys.merge(o.reset_index()[["ticker", "date", "index"]], on=["ticker", "date"], how="inner")
    pos = {t: i for t, i in idx.items()}
    # position of each event inside its ticker's block
    inblock = {}
    for t, ix in pos.items():
        inblock.update({(t, int(j)): k for k, j in enumerate(ix)})
    ev_t = key["ticker"].to_numpy()
    ev_ix = key["index"].to_numpy()
    yv = o["y"].to_numpy()
    res = np.empty(B)
    for b in range(B):
        shifts = {t: rng.integers(1, len(ix)) for t, ix in pos.items() if len(ix) > 1}
        vals = []
        for t, j in zip(ev_t, ev_ix):
            ix = pos[t]
            k = inblock[(t, int(j))]
            vals.append(yv[ix[(k + shifts.get(t, 0)) % len(ix)]])
        res[b] = np.nanmean(vals)
    return res


def main(db: str, B: int):
    raw = common.load_ipot(db)
    feat = pd.read_pickle(common.OUT_DIR + "/features.pkl")
    out = feat[["ticker", "date", "val20"] + [f"rel_{h}d" for h in H]]
    xl = events(raw, out, ["XL"])
    print(f"XL top-net-buyer events: {len(xl)} over {xl['ticker'].nunique()} tickers, {xl['date'].nunique()} dates; first-of-streak {int(xl['first_of_streak'].sum())}")
    print("\n[1] pooled (demeaned rel return, %)  h | n | mean | t(ticker-clust) | t(date-clust)")
    for h in H:
        n, mu, tt, td = stat(xl, f"y{h}")
        print(f"   {h:>2}d | {n} | {100 * mu:+.2f} | {tt:+.2f} | {td:+.2f}")
    print("\n[2] shift-null (is the pooled mean beyond what per-ticker drift + autocorrelation produce?)")
    for h in H:
        nul = shift_null(xl, out, h, B)
        mu = xl[f"y{h}"].mean()
        print(f"   {h:>2}d | observed {100 * mu:+.2f}% | null mean {100 * nul.mean():+.2f}% P5 {100 * np.percentile(nul, 5):+.2f}% P95 {100 * np.percentile(nul, 95):+.2f}% | one-sided p(low) {(1 + np.sum(nul <= mu)) / (B + 1):.3f}")

    print("\n[3] splits at h=5d and 10d: group | n | mean% | t(ticker-clust)")
    dates = np.sort(xl["date"].unique())
    k = len(dates) // 3
    blk = pd.Series(np.select([xl["date"] <= dates[k], xl["date"] <= dates[2 * k]], ["block1", "block2"], "block3"), index=xl.index)
    rg = xl.merge(ihsg_phase(), on="date", how="left")["regime_phase"].fillna("NA")
    rg.index = xl.index
    q = pd.qcut(xl["val20"], 3, labels=["liq_low", "liq_mid", "liq_high"], duplicates="drop")
    for name, grp in (("time block", blk), ("IHSG regime", rg), ("liquidity", q)):
        print(f"  by {name}:")
        for lab, sub in xl.groupby(grp, observed=True):
            s5, s10 = stat(sub, "y5"), stat(sub, "y10")
            print(f"     {str(lab):<20} n={s5[0]:>5} | 5d {100 * s5[1]:+.2f}% (t {s5[2]:+.1f}) | 10d {100 * s10[1]:+.2f}% (t {s10[2]:+.1f})")
    fs = xl[xl["first_of_streak"]]
    s5, s10 = stat(fs, "y5"), stat(fs, "y10")
    print(f"  first event of a streak only: n={s5[0]} | 5d {100 * s5[1]:+.2f}% (t {s5[2]:+.1f}) | 10d {100 * s10[1]:+.2f}% (t {s10[2]:+.1f})")

    print("\n[4] controls (same event definition, other brokers), 5d / 10d demeaned mean%, ticker-clustered t")
    for lab, br in (("other retail apps XC/YP/PD", ["XC", "YP", "PD"]), ("foreign retail ZP/YU", ["ZP", "YU"]), ("foreign inst AK/BK/KZ/RX/GA", ["AK", "BK", "KZ", "RX", "GA"]), ("gov CC/OD/NI/DX", ["CC", "OD", "NI", "DX"])):
        e = events(raw, out, br)
        s5, s10 = stat(e, "y5"), stat(e, "y10")
        print(f"   {lab:<30} n={s5[0]:>5} | 5d {100 * s5[1]:+.2f}% (t {s5[2]:+.1f}) | 10d {100 * s10[1]:+.2f}% (t {s10[2]:+.1f})")
    xs = events(raw, out, ["XL"], side="sell")
    s5, s10 = stat(xs, "y5"), stat(xs, "y10")
    print(f"   XL as top net SELLER           n={s5[0]:>5} | 5d {100 * s5[1]:+.2f}% (t {s5[2]:+.1f}) | 10d {100 * s10[1]:+.2f}% (t {s10[2]:+.1f})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=common.IPOT_DB)
    ap.add_argument("--B", type=int, default=300)
    a = ap.parse_args()
    main(a.db, a.B)
