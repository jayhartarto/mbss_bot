"""
research/ipot_explore/mover_footprint.py — event-study direction: start from the stocks that MOVED (+10% before -7% within 20d from open(t+1)) and ask what the broker footprint looked like at t
(the last published day before the move), versus stocks that did not move in the SAME date block and the SAME ATR tercile.

Run (project root):  python -W ignore -m research.ipot_explore.mover_footprint
Per feature: standardized difference (winner - loser)/sd after removing block x ATR-tercile cell means, ticker-clustered t, and sign agreement across the date blocks. Two populations:
  top150   research/ipot_broker_daily_vps_2026_10_22.sqlite (1 year, 3 blocks, features.pkl)
  outside  research/ipot_outside_top150.sqlite (3 months, 2 blocks)
A feature is a candidate only if the sign agrees in EVERY block of BOTH populations and |t| >= 2.5 in at least one. Exploratory: ~70 features are scanned, so expect ~1-2 false |t|>2.5.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features, dna_shallow
from research.ipot_explore.bandar_definition import label, block_of

TOP = "research/ipot_broker_daily_vps_2026_10_22.sqlite"
OUT = "research/ipot_outside_top150.sqlite"
SKIP = {"ticker", "date", "close", "value", "volume", "val20", "dom_vwap", "mfe_5d", "mae_5d", "tp_first", "days_to_tp", "win", "atr", "blk", "vol"}


def panel(db: str, nblk: int, cached: str | None) -> pd.DataFrame:
    raw = common.load_ipot(db)
    px_all = common.load_ohlcv(start="2025-06-01")
    tk = raw.ticker.unique()
    pxa = px_all[px_all.ticker.isin(tk)].sort_values(["ticker", "date"]).copy()
    if cached:
        base = pd.read_pickle(cached)
    else:
        b = features.ticker_day_features(raw, pxa)
        p = features.rolling_pattern_features(raw, pxa)
        base = b.merge(p, on=["ticker", "date"], how="left")
    base = base[[c for c in base.columns if not (c.startswith(("ret_", "rel_")) or c in ("mfe_5d", "mae_5d"))]]
    base = dna_shallow.add_technicals(base)
    pc = pxa.groupby("ticker")["close"].shift(1)
    tr = pd.concat([pxa.high - pxa.low, (pxa.high - pc).abs(), (pxa.low - pc).abs()], axis=1).max(axis=1)
    pxa["atr"] = tr.groupby(pxa.ticker).transform(lambda s: s.rolling(14).mean()) / pxa.close
    d = base.merge(label(pxa), on=["ticker", "date"]).dropna(subset=["tp_first"]).merge(pxa[["ticker", "date", "atr"]], on=["ticker", "date"]).dropna(subset=["atr"]).reset_index(drop=True)
    u = np.sort(d["date"].unique()); k = len(u) // nblk
    d["blk"] = np.minimum(np.searchsorted(u, d["date"].to_numpy(), side="left") // max(k, 1), nblk - 1) + 1
    d["vol"] = pd.qcut(d["atr"], 3, labels=False)
    return d


def scan(d: pd.DataFrame, nblk: int) -> pd.DataFrame:
    cols = [c for c in d.columns if c not in SKIP and (pd.api.types.is_numeric_dtype(d[c]) or pd.api.types.is_bool_dtype(d[c]))]
    cell = d["blk"].astype(str) + "_" + d["vol"].astype(str)
    y = d["tp_first"] - d.groupby(cell)["tp_first"].transform("mean")
    rows = []
    for c in cols:
        x = d[c].astype(float).replace([np.inf, -np.inf], np.nan)
        if x.notna().mean() < 0.7 or x.nunique() < 5:
            continue
        lo, hi = x.quantile([.01, .99]); x = x.clip(lo, hi)
        xs = (x - x.groupby(cell).transform("mean")) / x.std()
        m = xs.notna()
        xv, yv = xs[m].to_numpy(), y[m].to_numpy()
        beta = (xv * yv).sum() / (xv * xv).sum()
        e = yv - beta * xv
        cs = pd.Series(xv * e).groupby(d.loc[m, "ticker"].to_numpy()).sum()
        t = beta / (np.sqrt((cs ** 2).sum()) / (xv * xv).sum())
        sb = []
        for b in range(1, nblk + 1):
            mb = (d.loc[m, "blk"] == b).to_numpy()
            sb.append(np.sign((xv[mb] * yv[mb]).sum()))
        rows.append(dict(feature=c, beta=beta * 100, t=t, signs=sb, same=all(s == sb[0] for s in sb) and sb[0] != 0, n=int(m.sum())))
    return pd.DataFrame(rows)


def main():
    d1 = panel(TOP, 3, common.OUT_DIR + "/features.pkl")
    d2 = panel(OUT, 2, None)
    print(f"top150: {len(d1)} labelled rows, {d1.ticker.nunique()} tickers, base hit {d1.tp_first.mean():.3f} | outside: {len(d2)} rows, {d2.ticker.nunique()} tickers, base hit {d2.tp_first.mean():.3f}")
    r1, r2 = scan(d1, 3), scan(d2, 2)
    m = r1.merge(r2, on="feature", suffixes=("_top", "_out"))
    print(f"features scanned: top150 {len(r1)}, outside {len(r2)}, common {len(m)}")
    cand = m[(m.same_top) & (m.same_out) & (np.sign(m.beta_top) == np.sign(m.beta_out)) & ((m.t_top.abs() >= 2.5) | (m.t_out.abs() >= 2.5))].copy()
    cand["score"] = cand.t_top.abs() + cand.t_out.abs()
    pd.set_option("display.width", 220, "display.max_colwidth", 40)
    print("\nCANDIDATES (same sign in every block of both populations, |t|>=2.5 in at least one); beta = pp change in hit-rate per 1 sd of the feature")
    print(cand.sort_values("score", ascending=False)[["feature", "beta_top", "t_top", "beta_out", "t_out"]].round(2).to_string(index=False) if len(cand) else "  none")
    print("\nstrongest in top150 alone (|t|>=3), whether or not they replicate outside:")
    s = m[m.t_top.abs() >= 3].sort_values("t_top", key=abs, ascending=False)
    print(s[["feature", "beta_top", "t_top", "same_top", "beta_out", "t_out", "same_out"]].round(2).head(15).to_string(index=False) if len(s) else "  none")
    print("\nstrongest outside alone (|t|>=3):")
    s = m[m.t_out.abs() >= 3].sort_values("t_out", key=abs, ascending=False)
    print(s[["feature", "beta_out", "t_out", "same_out", "beta_top", "t_top", "same_top"]].round(2).head(15).to_string(index=False) if len(s) else "  none")


if __name__ == "__main__":
    main()
