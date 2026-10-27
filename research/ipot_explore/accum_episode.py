"""
research/ipot_explore/accum_episode.py — does a broker's multi-day NET accumulation episode lead price? (lead from ELIT/AK 2026-10-27)

Event = (ticker, broker, day t) where, over the last W trading days of that ticker:
  * cumulative CONSERVATIVE net lots (net_lo, see below) / cumulative market volume >= THETA
  * the broker was net-positive (net_lo > 0) on >= MIN_POS of the W days
  * first qualifying day only; COOLDOWN days skipped afterwards
Daily net is buy_lot - sell_lot. A broker missing from one side of the top-10 is BELOW that side's 10th place, so net_lo uses the worst
case (missing sell = the day's smallest listed sell, missing buy = 0) -> an event survives truncation.
Signal known after close t -> outcome measured from open(t+1) to close(t+H). Benchmark = mean of the same date x ATR-tercile cell
(excess return). Inference: ticker-clustered bootstrap, 3 time blocks with embargo, BH-FDR over per-broker tests.
Placebo for "is it the broker's identity?": same events but labelled by pooled ALL-broker events (any broker with the same accumulation share).
"""
from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from engine.broker_roles import role_of  # noqa: E402

DB = os.path.join(ROOT, "research", "ipot_merged.sqlite")
OHLCV = os.path.join(ROOT, "mbss_ohlcv.db")
OUT = os.path.join(ROOT, "research", "ipot_explore", "out")
W, THETA, MIN_POS, COOLDOWN = 5, float(os.environ.get("THETA", 0.04)), 3, 10
HS = (5, 10)
BLOCKS = [("2023-01-01", "2024-06-30"), ("2024-07-01", "2025-06-30"), ("2025-07-01", "2026-12-31")]
EMBARGO = 12
rng = np.random.default_rng(7)


def load():
    c = sqlite3.connect(DB)
    d = pd.read_sql_query(
        "SELECT ticker, trade_date date, broker_code broker, buy_lot, sell_lot FROM ipot_broker_daily WHERE fd='all' AND board='all'", c)
    c.close()
    c = sqlite3.connect(OHLCV)
    o = pd.read_sql_query("SELECT ticker, date, open, high, low, close, volume FROM ohlcv_daily WHERE date>='2022-12-01'", c)
    c.close()
    return d, o


def prep_price(o):
    o = o.sort_values(["ticker", "date"]).reset_index(drop=True)
    g = o.groupby("ticker", sort=False)
    o["vol_lot"] = o["volume"] / 100.0
    pc = g["close"].shift(1)
    tr = np.maximum.reduce([(o.high - o.low).values, (o.high - pc).abs().values, (o.low - pc).abs().values])
    o["atr"] = pd.Series(tr, index=o.index).groupby(o.ticker).transform(lambda s: s.rolling(14, min_periods=10).mean()) / o.close
    o["open_n"] = g["open"].shift(-1)
    for h in HS:
        o[f"cl_{h}"] = g["close"].shift(-h)
        o[f"ret{h}"] = (o[f"cl_{h}"] / o["open_n"] - 1) * 100
    o["ret_pre5"] = (o.close / g["close"].shift(5) - 1) * 100
    o["cell"] = o.groupby("date")["atr"].transform(lambda s: pd.qcut(s.rank(method="first"), 3, labels=False, duplicates="drop"))
    for h in HS:
        o[f"base{h}"] = o.groupby(["date", "cell"])[f"ret{h}"].transform("mean")
        o[f"ex{h}"] = o[f"ret{h}"] - o[f"base{h}"]
    return o


def events(d, o):
    # daily conservative net per (ticker, date, broker)
    day = d.groupby(["ticker", "date"])
    min_sell = day["sell_lot"].transform("min")  # smallest listed sell of the day (NaN-safe: min ignores NaN)
    min_sell = min_sell.fillna(0)
    d = d.assign(bl=d.buy_lot.fillna(0), sl=d.sell_lot.fillna(0))
    d["net_lo"] = np.where(d.buy_lot.isna(), -d.sl, d.bl - np.where(d.sell_lot.isna(), min_sell, d.sl))
    d["net_mid"] = d.bl - d.sl
    vol = o.set_index(["ticker", "date"])["vol_lot"]
    out = []
    px = o.set_index(["ticker", "date"])
    tickers = d.ticker.unique()
    for t in tickers:
        dt = d[d.ticker == t]
        cal = o.loc[o.ticker == t, "date"].values
        if len(cal) < 60:
            continue
        piv = dt.pivot_table(index="date", columns="broker", values="net_lo", aggfunc="sum").reindex(cal).fillna(0.0)
        pm = dt.pivot_table(index="date", columns="broker", values="net_mid", aggfunc="sum").reindex(cal).fillna(0.0)
        v = vol.loc[t].reindex(cal).values.astype(float)
        vroll = pd.Series(v).rolling(W).sum().values
        cum = piv.rolling(W).sum().values
        pos = (piv.values > 0).astype(float)
        npos = pd.DataFrame(pos).rolling(W).sum().values
        share = cum / np.where(vroll > 0, vroll, np.nan)[:, None]
        ok = (share >= THETA) & (npos >= MIN_POS)
        brokers = piv.columns.values
        for j in range(ok.shape[1]):
            idx = np.where(ok[:, j])[0]
            last = -10**9
            for i in idx:
                if i - last < COOLDOWN:
                    continue
                last = i
                # follow-through: broker net_mid over next 5 days relative to what it accumulated
                fut = pm.values[i + 1:i + 6, j].sum() if i + 6 <= len(cal) else np.nan
                out.append((t, cal[i], brokers[j], share[i, j], cum[i, j], fut / cum[i, j] if cum[i, j] else np.nan))
    ev = pd.DataFrame(out, columns=["ticker", "date", "broker", "share", "cum_lo", "fut5_rel"])
    ev["role"] = ev.broker.map(role_of)
    return ev


def boot_ci(x, groups, n=400):
    if len(x) < 20:
        return np.nan, np.nan, np.nan
    df = pd.DataFrame({"x": x, "g": groups}).dropna()
    m = df.x.mean()
    gs = df.groupby("g").x.agg(["sum", "count"])
    s, c = gs["sum"].values, gs["count"].values
    bs = []
    for _ in range(n):
        k = rng.integers(0, len(gs), len(gs))
        bs.append(s[k].sum() / c[k].sum())
    lo, hi = np.percentile(bs, [2.5, 97.5])
    t = m / (np.std(bs) + 1e-9)
    return m, (lo, hi), t


def summarize(ev, label_col, tag):
    rows = []
    for lab, g in ev.groupby(label_col):
        for h in HS:
            x = g[f"ex{h}"].dropna()
            if len(x) < 30:
                continue
            m, ci, t = boot_ci(g.loc[x.index, f"ex{h}"].values, g.loc[x.index, "ticker"].values)
            rows.append(dict(set=tag, label=lab, h=h, n=len(x), tickers=g.loc[x.index, "ticker"].nunique(), mean_ex=m,
                             lo=ci[0], hi=ci[1], t=t, win_ex=(x > 0).mean(), raw=g.loc[x.index, f"ret{h}"].mean()))
    return pd.DataFrame(rows)


def main():
    os.makedirs(OUT, exist_ok=True)
    d, o = load()
    o = prep_price(o)
    ev = events(d, o)
    ev = ev.merge(o[["ticker", "date", "ret_pre5", "atr"] + [f"ex{h}" for h in HS] + [f"ret{h}" for h in HS]], on=["ticker", "date"], how="left")
    ev = ev.dropna(subset=["ex5"])
    ev["block"] = -1
    for b, (a, z) in enumerate(BLOCKS):
        ev.loc[(ev.date >= a) & (ev.date <= z), "block"] = b
    ev.to_csv(os.path.join(OUT, "accum_episode_events.csv"), index=False)
    print(f"THETA={THETA}  events={len(ev)}  tickers={ev.ticker.nunique()}  brokers={ev.broker.nunique()}")
    print("\n== ALL brokers pooled (placebo for identity) ==")
    allr = summarize(ev.assign(all="ALL"), "all", "pooled")
    print(allr.round(3).to_string(index=False))
    print("\n== by role ==")
    byrole = summarize(ev, "role", "role")
    print(byrole.round(3).to_string(index=False))
    # block replication by role (embargo: drop events within EMBARGO days of block end)
    print("\n== role x block (h=5 excess) ==")
    rb = []
    cal = sorted(o.date.unique())
    for b, (a, z) in enumerate(BLOCKS):
        end = cal[max(0, np.searchsorted(cal, z, side="right") - 1 - EMBARGO)] if z < cal[-1] else z
        sub = ev[(ev.block == b) & (ev.date <= end)]
        s = summarize(sub, "role", f"blk{b}")
        s = s[s.h == 5]
        rb.append(s)
    rb = pd.concat(rb)
    print(rb.pivot(index="label", columns="set", values=["n", "mean_ex", "t"]).round(2).to_string())
    # per broker, FDR
    bb = summarize(ev, "broker", "broker")
    bb = bb[(bb.n >= 200) & (bb.tickers >= 30)]
    from math import erf, sqrt
    bb["p"] = bb.t.apply(lambda z: 2 * (1 - 0.5 * (1 + erf(abs(z) / sqrt(2)))))
    bb = bb.sort_values("p")
    bb["bh"] = bb.p * len(bb) / (np.arange(len(bb)) + 1)
    print("\n== per broker (n>=200, >=30 tickers), by p ==")
    print(bb.round(3).head(25).to_string(index=False))
    bb.to_csv(os.path.join(OUT, "accum_episode_by_broker.csv"), index=False)
    # stratum: accumulation while price falling / flat (ELIT-like) vs after run-up
    ev["pre"] = pd.cut(ev.ret_pre5, [-100, -5, 0, 5, 1000], labels=["<-5", "-5..0", "0..5", ">5"])
    print("\n== pooled by prior-5d return (h=5,10) ==")
    print(summarize(ev, "pre", "pre").round(3).to_string(index=False))
    # follow-through (distribution)
    ev["unload"] = ev.fut5_rel <= -0.5
    print("\n== follow-through: broker unloads >=50% within next 5d ==")
    print(ev.groupby("role").unload.agg(["mean", "count"]).round(3).to_string())
    print("ex5 when unload vs not:", ev.groupby("unload").ex5.mean().round(3).to_dict())


if __name__ == "__main__":
    main()
