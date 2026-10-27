"""
research/ipot_explore/pumpday_dna.py — DNA matching on the PUMP DAY itself (day -1 in the retail-FOMO picture): among stock-days where the price jumped (R5V: day return >= +5% with volume >= 2x its 20-day mean; R10: >= +10%;
R20: >= +20%; V5/V10: volume >= 5x/10x with a day return >= +3%; union, one row per ticker-day), can the features known at that close separate the pumps that continue from the ones that reverse?
Learner: ridge (alpha 300) on cross-sectionally rank-normalised features; models: PRICE (rsi, dist_sma20, atr, range60_pos, pctb, pret_1d/5d, past_ret_10d, vol_ratio, log val20, log close, close_pos, day_ret, trigger flags)
and PRICE+BROKER (+ the scale-free broker features, XL top buyer/seller flags). Targets: rel_5d, rel_10d (pp). Walk-forward over time terciles of the events: B1->B2, B1+B2->B3 (8-day purge).
Metrics on the held-out events: Spearman rank IC, mean realised rel return of the top third minus the bottom third of the model score (ticker-cluster bootstrap 90% CI), and the same for the top third (alone) vs all events.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.pumpday_dna
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, sweep
from research.ipot_explore import gorengan_trade as gt
from research.ipot_explore.breakout_backing import triggers
from research.ipot_explore.breakout_backing2 import triggers2
from research.ipot_explore.lead_1d import prep
from research.ipot_explore.m012 import PRICE, SIZE, rank_norm, ridge
from research.ipot_explore.mover_footprint import TOP, OUT
from research.ipot_explore.xl_seller import xl_flags

FLAGS = ["R5V", "R10", "R20", "V5", "V10"]


def boot_spread(df: pd.DataFrame, score: np.ndarray, ycol: str, rng, n: int = 300):
    d = df.assign(s=score)
    q = d["s"].rank(pct=True)
    top, bot = d[q > 2 / 3], d[q <= 1 / 3]
    spread = top[ycol].mean() - bot[ycol].mean()
    tickers = d["ticker"].unique()
    grp = {t: x for t, x in d.groupby("ticker")}
    bs = []
    for _ in range(n):
        s_ = pd.concat([grp[t] for t in rng.choice(tickers, len(tickers))])
        qq = s_["s"].rank(pct=True)
        bs.append(s_[qq > 2 / 3][ycol].mean() - s_[qq <= 1 / 3][ycol].mean())
    return spread, np.percentile(bs, [5, 95]), top[ycol].mean(), d[ycol].mean()


def main():
    f, pxs, px_all, tdi = gt.build()
    raw = pd.concat([common.load_ipot(TOP), common.load_ipot(OUT)], ignore_index=True)
    f = f.merge(xl_flags(raw), on=["ticker", "date"], how="left")
    for c in ("XLB", "XLS"):
        f[c] = f[c].fillna(False).astype(float)
    sub = pxs[pxs.ticker.isin(f.ticker.unique())]
    TR = triggers(sub).merge(triggers2(sub), on=["ticker", "date"], how="outer")
    for c in FLAGS:
        f = f.merge(TR[["ticker", "date", c]], on=["ticker", "date"], how="left")
        f[c] = f[c].fillna(False).astype(float)
    ohlc = common.load_ohlcv(start="2025-01-01")[["ticker", "date", "open", "high", "low"]]
    if "day_ret" not in f.columns:
        f = f.merge(ohlc.rename(columns={"open": "o_", "high": "h_", "low": "l_"}), on=["ticker", "date"], how="left")
        f["day_ret"] = f["close"] / f["o_"] - 1
        f["close_pos"] = (f["close"] - f["l_"]) / (f["h_"] - f["l_"]).replace(0, np.nan)
    broker = [c for c in sweep.feature_columns(f) if c not in PRICE and not c.endswith(SIZE) and c not in ("y", "day_ret", "close_pos", "close_vs_typ", "val20", "score", "blk", "vol")
              and not c.startswith(("o_", "h_", "l_", "ret_", "rel_", "mfe_")) and c not in FLAGS + ["XLB", "XLS"] and not c.endswith(("_chg", "_r"))]
    price_cols = [c for c in PRICE if c in f.columns] + ["day_ret", "close_pos"]
    XP = pd.concat([rank_norm(f, price_cols), f[FLAGS]], axis=1)
    XPB = pd.concat([rank_norm(f, price_cols + broker), f[FLAGS + ["XLB", "XLS"]]], axis=1)
    ev = f[FLAGS].sum(axis=1) > 0
    E = f[ev].dropna(subset=["rel_5d", "rel_10d"]).copy()
    idx = E.index
    E["y5"], E["y10"] = E["rel_5d"] * 100, E["rel_10d"] * 100
    u = np.sort(E["date"].unique())
    k = len(u) // 3
    E["blk"] = np.where(E["date"] <= u[k], 1, np.where(E["date"] <= u[2 * k], 2, 3))
    print(f"pump-day events {len(E)} ({E.ticker.nunique()} tickers) | by block {E.groupby('blk').size().to_dict()} | mean rel_5d {E.y5.mean():+.2f}pp rel_10d {E.y10.mean():+.2f}pp | broker feats {len(broker)}")
    rng = np.random.default_rng(7)
    for ycol in ("y5", "y10"):
        print(f"\n=== target {ycol} ===")
        for mname, X in (("PRICE", XP), ("PRICE+BROKER", XPB)):
            A = X.loc[idx].to_numpy(float)
            res = []
            for trb, teb in (([1], 2), ([1, 2], 3)):
                tr = E["blk"].isin(trb).to_numpy() & (E["date"] <= u[(k if teb == 2 else 2 * k)] - pd.Timedelta(days=12)).to_numpy()
                te = (E["blk"] == teb).to_numpy()
                yc = E.loc[tr, ycol].to_numpy()
                yr = pd.Series(yc).rank(pct=True).to_numpy() - 0.5
                w = ridge(A[tr], yr, 300.0)
                s = A[te] @ w
                T = E.loc[te]
                ic = pd.Series(s).rank().corr(pd.Series(T[ycol].to_numpy()).rank())
                sp, ci, top, allm = boot_spread(T, s, ycol, rng)
                res.append((teb, len(T), ic, sp, ci, top, allm))
                print(f"   {mname:<13} train blocks {trb} -> test block {teb}: n {len(T):>4} | rank IC {ic:+.3f} | top third {top:+.2f} vs all {allm:+.2f} | top-bottom spread {sp:+.2f}pp [90% CI {ci[0]:+.2f}, {ci[1]:+.2f}]")


if __name__ == "__main__":
    main()
