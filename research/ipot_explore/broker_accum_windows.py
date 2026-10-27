"""
research/ipot_explore/broker_accum_windows.py — experiment family EXP-BRKACC-1 (registered before running): per-broker accumulation over a long window.
For broker b, stock s, day t and window W (20 or 40 trading days): share = sum over the window of b's net_val_mid / sum of the stock's traded value over the SAME days (only days with IPOT data count).
Event_b = share >= P95 of that broker's positive shares (and >= 2%).  Outcome: relative return rel_h (h = 5, 10, 20 trading days from open(t+1), minus the liquid-universe median of the date), in pp.
Regression per broker x W x h: rel_h ~ Event_b + controls (log val20, ATR%, past_ret_10d, pret_1d, pret_5d, vol_ratio, range60_pos, log close) with ticker AND date fixed effects, two-way clustered t.
Family: 17 brokers x 2 windows x 3 horizons = 102 tests; Benjamini-Hochberg over the family. Then the sign in the two halves of the sample for the leading cells, and a descriptive list of who is accumulating right now.
Brokers: AK BK ZP YU KZ RX GA (foreign) | CC OD NI DX (gov/state) | MG CP (scalper) | XL XC YP PD (retail apps).
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.broker_accum_windows
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features
from research.ipot_explore import gorengan_trade as gt
from research.ipot_explore.breakout_backing import norm_p, bh
from research.ipot_explore.lead_1d import demean2, twoway_t
from research.ipot_explore.mover_footprint import TOP, OUT

BROKERS = ["AK", "BK", "ZP", "YU", "KZ", "RX", "GA", "CC", "OD", "NI", "DX", "MG", "CP", "XL", "XC", "YP", "PD"]
WINDOWS = (20, 40)
HS = (5, 10, 20)
CTRL = ["log_val20", "atr_pct", "past_ret_10d", "pret_1d", "pret_5d", "vol_ratio", "range60_pos", "log_close"]


def window_shares(raw: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    a = features.broker_day_table(raw, "all", "all")[["ticker", "date", "broker", "net_val_mid"]].dropna()
    a = a[a.broker.isin(BROKERS)].groupby(["ticker", "date", "broker"], as_index=False)["net_val_mid"].sum()
    val = px.assign(value=px["close"] * px["volume"]).set_index(["ticker", "date"])["value"]
    have = a[["ticker", "date"]].drop_duplicates()
    out = []
    for tk, g in have.groupby("ticker"):
        dts = pd.DatetimeIndex(sorted(px.loc[px.ticker == tk, "date"].unique()))
        if len(dts) < 30:
            continue
        covered = pd.Series(dts.isin(g["date"]), index=dts)
        v = val.xs(tk, level="ticker").reindex(dts).fillna(0.0) * covered
        nt = a[a.ticker == tk].pivot_table(index="date", columns="broker", values="net_val_mid", aggfunc="sum").reindex(dts).fillna(0.0)
        for W in WINDOWS:
            den = v.rolling(W, min_periods=int(0.7 * W)).sum()
            nobs = covered.astype(float).rolling(W, min_periods=1).sum()
            num = nt.rolling(W, min_periods=int(0.7 * W)).sum()
            sh = num.div(den.where(den > 0), axis=0)
            sh = sh.where(nobs >= 0.7 * W)
            for b in nt.columns:
                s = sh[b].dropna()
                if len(s):
                    out.append(pd.DataFrame({"ticker": tk, "date": s.index, "broker": b, "W": W, "share": s.to_numpy()}))
    return pd.concat(out, ignore_index=True)


def main():
    f, pxs, px_all, tdi = gt.build()
    raw = pd.concat([common.load_ipot(TOP), common.load_ipot(OUT)], ignore_index=True)
    sub = px_all[px_all.ticker.isin(f.ticker.unique())]
    S = window_shares(raw, sub)
    print(f"window-share rows {len(S)}; brokers present {sorted(S.broker.unique())}")
    # rel_20d (features.outcomes only has 1,3,5,10): same definition, open(t+1) to close(t+20) minus the liquid median of the date
    p = px_all.sort_values(["ticker", "date"]).copy()
    p["val20"] = p.groupby("ticker")["value"].transform(lambda s: s.rolling(20, min_periods=10).mean()) if "value" in p.columns else (p["close"] * p["volume"]).groupby(p["ticker"]).transform(lambda s: s.rolling(20, min_periods=10).mean())
    entry = p.groupby("ticker")["open"].shift(-1)
    p["ret_20d"] = p.groupby("ticker")["close"].shift(-20) / entry - 1
    med = p[p["val20"] >= 1e9].groupby("date")["ret_20d"].median().rename("med20")
    p = p.merge(med, on="date", how="left")
    p["rel_20d"] = p["ret_20d"] - p["med20"]
    f = f.merge(p[["ticker", "date", "rel_20d"]], on=["ticker", "date"], how="left")
    rows = []
    thr = {}
    for (b, W), g in S.groupby(["broker", "W"]):
        pos = g.loc[g.share > 0, "share"]
        thr[(b, W)] = max(0.02, float(pos.quantile(0.95))) if len(pos) > 200 else np.nan
    for b in BROKERS:
        for W in WINDOWS:
            t_ = thr.get((b, W), np.nan)
            if np.isnan(t_):
                continue
            ev = S[(S.broker == b) & (S.W == W) & (S.share >= t_)][["ticker", "date"]].assign(E=1.0)
            d0 = f.merge(ev, on=["ticker", "date"], how="left")
            d0["E"] = d0["E"].fillna(0.0)
            for h in HS:
                ycol = f"rel_{h}d"
                d = d0.dropna(subset=[ycol] + CTRL).copy()
                d["y"] = d[ycol] * 100
                if d.E.sum() < 40:
                    continue
                M = demean2(d[["E"] + CTRL + ["y"]].to_numpy(), d["ticker"].to_numpy(), d["date"].to_numpy())
                coef, t = twoway_t(M[:, :-1], M[:, -1], d["ticker"].to_numpy(), d["date"].to_numpy(), 0)
                u = np.sort(d["date"].unique())
                half = d["date"] <= u[len(u) // 2]
                sg = []
                for m in (half, ~half):
                    x = d[m]
                    sg.append(float(np.sign(x[x.E == 1]["y"].mean() - x[x.E == 0]["y"].mean())) if (x.E == 1).sum() >= 10 else 0.0)
                rows.append(dict(broker=b, W=W, h=h, n_ev=int(d.E.sum()), n_tick=int(d[d.E == 1].ticker.nunique()), share_thr=t_, coef=coef, t=t, halves=sg))
    G = pd.DataFrame(rows)
    G["p"] = G["t"].map(norm_p)
    G["bh"] = bh(G["p"].to_numpy())
    print(f"\nFAMILY: {len(G)} tests | nominal |t|>=2: {int((G.t.abs() >= 2).sum())} (expected by chance ~{0.05 * len(G):.0f}) | |t|>=3: {int((G.t.abs() >= 3).sum())} | BH q<0.05 survivors: {int(G.bh.sum())}")
    pd.set_option("display.width", 200)
    print("\nstrongest cells (coef = pp of relative return when the broker is in heavy accumulation, vs not, same stock/same day, controlled):")
    print(G.assign(a=G.t.abs()).sort_values("a", ascending=False).head(16)[["broker", "W", "h", "n_ev", "n_tick", "share_thr", "coef", "t", "halves", "bh"]].round(3).to_string(index=False))
    print("\nby broker (all 6 cells): mean coef and number of cells with t>=+2 / t<=-2")
    agg = G.groupby("broker").agg(cells=("t", "size"), mean_coef=("coef", "mean"), pos2=("t", lambda s: int((s >= 2).sum())), neg2=("t", lambda s: int((s <= -2).sum())), best_t=("t", "max"), worst_t=("t", "min")).round(2)
    print(agg.reindex(BROKERS).dropna().to_string())
    last = S[(S.date == S.date.max()) & (S.W == 40) & (S.broker.isin(["AK", "BK", "ZP", "MG"]))].copy()
    print(f"\nDESCRIPTIVE ONLY (not a recommendation; the backtest above is the evidence): 40-day net accumulation share as of {S.date.max().date()}")
    for b in ("AK", "BK", "ZP", "MG"):
        x = last[(last.broker == b) & (last.share >= thr.get((b, 40), 0.02))].sort_values("share", ascending=False).head(6)
        print(f"   {b}: " + (", ".join(f"{r.ticker} {100 * r.share:.1f}%" for r in x.itertuples()) or "none above its own P95"))


if __name__ == "__main__":
    main()
