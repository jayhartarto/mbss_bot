"""
research/ipot_explore/bandar_outside_top150.py — the bandar-accumulation definitions of bandar_definition.py re-run on the stocks OUTSIDE the liquid top-150 (research/ipot_outside_top150.sqlite,
516 tickers, ~3 months, variants all/all + F/all). Same target (+10% before -7% within 20 trading days from open(t+1)), lift measured against the base rate of the SAME date block and
the SAME ATR tercile. Only ~1 month of rows can be labelled (3-month data, 20-day forward target, 5-20 day feature windows), so TWO date blocks are used, and the numbers are exploratory.
Run (project root):  python -W ignore -m research.ipot_explore.bandar_outside_top150
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features
from research.ipot_explore.bandar_definition import label

DB = "research/ipot_outside_top150.sqlite"


def main():
    raw = common.load_ipot(DB)
    px_all = common.load_ohlcv(start="2025-06-01")
    tk = raw.ticker.unique()
    px = px_all[px_all.ticker.isin(tk)].sort_values(["ticker", "date"]).copy()
    pc = px.groupby("ticker")["close"].shift(1)
    tr = pd.concat([px.high - px.low, (px.high - pc).abs(), (px.low - pc).abs()], axis=1).max(axis=1)
    px["atr"] = tr.groupby(px.ticker).transform(lambda s: s.rolling(14).mean()) / px.close
    lab = label(px)
    allv = raw[(raw.fd == "all") & (raw.board == "all")].groupby("broker")["buy_val"].sum()
    fv = raw[(raw.fd == "F") & (raw.board == "all")].groupby("broker")["buy_val"].sum()
    fshare = (fv / allv).fillna(0)
    print(f"tickers {len(tk)} | broker rows {len(raw)} | IPOT dates {raw.date.min().date()}..{raw.date.max().date()}")
    for W in (5, 10):
        pat = features.rolling_pattern_features(raw, px, window=W).dropna(subset=["dom_broker", "dom_vwap_dist", "dom_inventory_share"])
        d = pat.merge(lab, on=["ticker", "date"]).dropna(subset=["tp_first"]).merge(px[["ticker", "date", "atr"]], on=["ticker", "date"]).dropna(subset=["atr"]).reset_index(drop=True)
        d = d.merge(px.assign(pr=px.groupby("ticker")["close"].pct_change(W))[["ticker", "date", "pr"]], on=["ticker", "date"], how="left")
        u = np.sort(d["date"].unique()); d["blk"] = np.where(d["date"] <= u[len(u) // 2], 1, 2)
        d["vol"] = pd.qcut(d["atr"], 3, labels=["lowvol", "midvol", "highvol"])
        d["foreign"] = d["dom_broker"].map(fshare) > .5
        strong = d.dom_inventory_share >= d.dom_inventory_share.quantile(.8)
        one = d.dom_pos_days >= .7; quiet = d.pr.abs() <= .05; near = d.dom_vwap_dist.between(-.03, .05)
        defs = {"D1 strong+oneway": strong & one, "D2 +quiet": strong & one & quiet, "D3 +near cost": strong & one & quiet & near, "D1 domestic": strong & one & ~d.foreign, "D1 foreign": strong & one & d.foreign}
        base = d.groupby(["blk", "vol"], observed=True)["tp_first"].mean()
        print(f"\n== W={W} | labelled rows {len(d)} ({d.ticker.nunique()} tickers, {d.date.min().date()}..{d.date.max().date()}) | base hit by block: "
              + " ".join(f"b{b}={d[d.blk == b].tp_first.mean():.3f}" for b in (1, 2)) + f" | days_to_TP median {d.days_to_tp.median():.0f}")
        for name, m in defs.items():
            s = d[m].copy()
            if len(s) < 50:
                print(f"  {name:<18} n={len(s)} too few"); continue
            s["lift"] = s.tp_first - np.array([base[(b, v)] for b, v in zip(s.blk, s.vol)])
            mu = s.lift.mean(); cs = s.groupby("ticker")["lift"].apply(lambda v: float((v - mu).sum())); t = mu / (np.sqrt((cs ** 2).sum()) / len(s))
            byb = " ".join(f"b{b}:{100 * s[s.blk == b].lift.mean():+.1f}(n{(s.blk == b).sum()})" for b in (1, 2))
            byv = " | ".join(f"{v} {100 * s[s.vol == v].lift.mean():+.1f}(n{(s.vol == v).sum()})" for v in ["lowvol", "midvol", "highvol"])
            print(f"  {name:<18} n={len(s):>5} (tickers {s.ticker.nunique()}) lift {100 * mu:+.1f}pp (t {t:+.1f}) | {byb} | {byv} | raw hit {100 * s.tp_first.mean():.1f}%")


if __name__ == "__main__":
    main()
