"""
research/ipot_explore/shakeout.py — EXP-SHAKE-1 (registered before running): the 'market maker pushes the price down to shake retail out, then marks it up' pattern.
Footprint at the close of t (IPOT-covered stock-days, price >= Rp30):  DROP = the close is >= 8% under its 10-day high while still above its 100-day average (a pullback inside a longer uptrend);
RET_SELL = the retail apps (XL XC YP PD) net SOLD heavily over the last 5 days (net share of the 5-day traded value in the lowest quintile of the DROP rows);
ABSORB = some non-retail broker net BOUGHT heavily over the same 5 days (the largest non-retail 5-day net share in the top 30% of the DROP rows).  SHAKEOUT = DROP & RET_SELL & ABSORB.
Outcomes (entry open t+1, net of 0.4%): BIG20 (net >= +20% with stop 12%, 20 days), the right-tail structure S6 (stop 6%, no TP, 20 days), and plain 10-day hold.
Reported: base rates for all covered rows and for DROP, then the 2x2 retail-selling x absorption inside DROP, the SHAKEOUT cell with ticker-clustered differences vs the DROP rows, the same without the uptrend condition,
and the largest-absorber identity (which non-retail brokers are the absorbers).
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.shakeout
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features
from research.ipot_explore.entry_scenarios import load, simulate, COST
from research.ipot_explore.mover_footprint import TOP, OUT

RETAIL = ["XL", "XC", "YP", "PD"]


def main():
    px = load()
    px["ticker"] = px["ticker"].astype("category")
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    g = px.groupby("ticker", observed=True)
    px["hi10"] = g["close"].transform(lambda s: s.rolling(10).max())
    px["dd10"] = px["close"] / px["hi10"] - 1
    raw = pd.concat([common.load_ipot(TOP), common.load_ipot(OUT)], ignore_index=True)
    a = features.broker_day_table(raw, "all", "all")[["ticker", "date", "broker", "net_val_mid"]].dropna()
    a = a.groupby(["ticker", "date", "broker"], as_index=False)["net_val_mid"].sum()
    val = (px["close"] * px["volume"])
    px["value"] = val
    have = a[["ticker", "date"]].drop_duplicates()
    out = []
    ticks = set(a["ticker"].unique())
    for tk, gg in px[px["ticker"].isin(ticks)].groupby("ticker", observed=True):
        dts = pd.DatetimeIndex(gg["date"])
        cov = pd.Series(dts.isin(have.loc[have.ticker == tk, "date"]), index=dts).astype(float)
        v = pd.Series(gg["value"].to_numpy(), index=dts) * cov
        nt = a[a.ticker == tk].pivot_table(index="date", columns="broker", values="net_val_mid", aggfunc="sum").reindex(dts).fillna(0.0)
        n5 = nt.rolling(5).sum().fillna(0.0)
        den = v.rolling(5).sum()
        ok = (cov.rolling(5).sum() >= 4) & (den > 0)
        ret_cols = [c for c in n5.columns if c in RETAIL]
        non_cols = [c for c in n5.columns if c not in RETAIL]
        retail5 = n5[ret_cols].sum(axis=1) / den.where(den > 0) if ret_cols else pd.Series(0.0, index=dts)
        nonmax = n5[non_cols].max(axis=1) / den.where(den > 0) if non_cols else pd.Series(np.nan, index=dts)
        who = n5[non_cols].idxmax(axis=1) if non_cols else pd.Series("", index=dts)
        df = pd.DataFrame({"ticker": tk, "date": dts, "retail5": retail5.where(ok).to_numpy(), "nonmax5": nonmax.where(ok).to_numpy(), "absorber": who.to_numpy()})
        out.append(df)
    B = pd.concat(out, ignore_index=True)
    P = px.merge(B, on=["ticker", "date"], how="inner")
    elig = (P["close"] >= 30) & (P["val20"] >= 0.1e9) & (P["nobs"] >= 130) & P["retail5"].notna() & P["dd10"].notna() & P["ma100"].notna()
    P = P[elig].copy()
    # position of each row in the sorted px for the simulator
    idx = pd.Series(np.arange(len(px)), index=pd.MultiIndex.from_frame(px[["ticker", "date"]]))
    P["pxrow"] = idx.reindex(pd.MultiIndex.from_frame(P[["ticker", "date"]])).to_numpy()
    rows = P["pxrow"].to_numpy().astype(int)
    f12, r12 = simulate(px, rows, None, 9.99, 0.12, 20, hmax=20)
    f6, r6 = simulate(px, rows, None, 9.99, 0.06, 20, hmax=20)
    f10, r10 = simulate(px, rows, None, 9.99, 0.99, 10, hmax=20)
    P["BIG20"] = ((r12 - COST) >= 0.20).astype(float)
    P["s6"] = r6 - COST
    P["h10"] = r10 - COST
    P = P[f12 & f6 & f10].reset_index(drop=True)
    print(f"covered eligible stock-days with outcomes: {len(P)} ({P.ticker.nunique()} tickers, {P.date.min().date()}..{P.date.max().date()}) | BIG20 {100 * P.BIG20.mean():.1f}% | S6 net {100 * P.s6.mean():+.2f}% | 10-day hold {100 * P.h10.mean():+.2f}%")

    def tstat(x, tk):
        m = x.mean()
        cs = (x - m).groupby(tk.to_numpy()).sum()
        return m / (np.sqrt((cs ** 2).sum()) / len(x)) if len(x) > 3 else np.nan

    def block(D, tag):
        drop = D[(D.dd10 <= -0.08) & (D.close > D.ma100)] if tag == "uptrend" else D[D.dd10 <= -0.08]
        q_ret = drop.retail5.quantile(0.20)
        q_abs = drop.nonmax5.quantile(0.70)
        drop = drop.assign(RET_SELL=drop.retail5 <= q_ret, ABSORB=drop.nonmax5 >= q_abs)
        print(f"\n===== DROP condition: {'close >= 8% under its 10-day high and above MA100' if tag == 'uptrend' else 'close >= 8% under its 10-day high (any trend)'} =====")
        print(f"all covered rows: BIG20 {100 * D.BIG20.mean():.1f}%, S6 {100 * D.s6.mean():+.2f}% | DROP rows n {len(drop)} ({drop.ticker.nunique()} tickers): BIG20 {100 * drop.BIG20.mean():.1f}%, S6 {100 * drop.s6.mean():+.2f}%, 10d {100 * drop.h10.mean():+.2f}%")
        print(f"thresholds inside DROP: retail 5-day net share <= {100 * q_ret:.2f}% (heavy retail selling); largest non-retail 5-day net share >= {100 * q_abs:.2f}% (absorption)")
        print(f"{'cell':<34}{'n':>5}{'tickers':>8}{'BIG20':>8}{'S6 net':>9}{'10d net':>9}{'S6 diff vs DROP':>17}{'t':>6}")
        for lab, m in (("DROP (all)", pd.Series(True, index=drop.index)), ("RET_SELL only", drop.RET_SELL & ~drop.ABSORB), ("ABSORB only", ~drop.RET_SELL & drop.ABSORB), ("neither", ~drop.RET_SELL & ~drop.ABSORB),
                       ("SHAKEOUT = RET_SELL & ABSORB", drop.RET_SELL & drop.ABSORB), ("RET_SELL any (with/without absorb)", drop.RET_SELL), ("ABSORB any", drop.ABSORB)):
            x = drop[m]
            if len(x) < 15:
                print(f"{lab:<34}{len(x):>5}  too few")
                continue
            d = x.s6 - drop.s6.mean()
            print(f"{lab:<34}{len(x):>5}{x.ticker.nunique():>8}{100 * x.BIG20.mean():>7.1f}%{100 * x.s6.mean():>8.2f}%{100 * x.h10.mean():>8.2f}%{100 * d.mean():>16.2f}pp{tstat(d, x.ticker):>6.1f}")
        sk = drop[drop.RET_SELL & drop.ABSORB]
        if len(sk) >= 15:
            print(f"   SHAKEOUT by year: " + " | ".join(f"{y}: n{len(gx)} S6 {100 * gx.s6.mean():+.2f}% BIG20 {100 * gx.BIG20.mean():.0f}%" for y, gx in sk.groupby(sk.date.dt.year)))
            print("   most frequent absorbers in SHAKEOUT: " + ", ".join(f"{k} {v}" for k, v in sk.absorber.value_counts().head(8).items()))
            print("   absorber identity vs outcome (S6 net, n):  " + " | ".join(f"{k} {100 * gx.s6.mean():+.1f}% (n{len(gx)})" for k, gx in sk.groupby("absorber") if len(gx) >= 8))
    block(P, "uptrend")
    block(P, "any")


if __name__ == "__main__":
    main()
