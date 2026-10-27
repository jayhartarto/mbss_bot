"""
research/ipot_explore/heuristics_replication.py — Engine 8: replicate the popular retail broker-summary heuristics AS STATED (no tuning), on the same target as the mover study:
TP +10% / SL -7% / 20-day timeout from open(t+1). Thresholds are fixed BEFORE running (see RULES); where a vendor gives no number the pooled P90 / P10 of the feature is used.

Run (project root):  python -W ignore -m research.ipot_explore.heuristics_replication
Rules (sources in IPOT_BROKER_RESEARCH_FRAMEWORK.md section 16):
  H1a Big Accumulation: top-3 net buyers' net value minus top-3 net sellers' value, over traded value, >= P90 (H1b: top-1 and H1c: top-5 versions; H1d Big Distribution = <= P10)
  H2  Broker Index: sum of the top-3 net buyers' net value / traded value >= 20% (Pluang's stated 'concentrated' level)
  H3  Foreign uptrend: foreign (F variant) buy value today / its mean over the previous 20 available days >= 1.5
  H4  Foreign dominant buyer (10-day inventory) AND price within +-3% of its average buy price
Each rule: cell-matched lift of hit rate (same date block x ATR tercile), mean realised return vs all rows, ticker-clustered t, per-block lift, non-overlapping entries.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features
from research.ipot_explore.mover_footprint import panel, TOP, OUT
from research.ipot_explore.mover_rule_walkforward import realised, nonoverlap


def topn(raw: pd.DataFrame) -> pd.DataFrame:
    a = features.broker_day_table(raw, "all", "all")[["ticker", "date", "net_val_mid"]].dropna()
    rows = []
    for (tk, dt), g in a.groupby(["ticker", "date"]):
        v = np.sort(g["net_val_mid"].to_numpy())
        buy = v[::-1][v[::-1] > 0]; sell = -v[v < 0]
        rows.append((tk, dt, buy[:1].sum(), buy[:3].sum(), buy[:5].sum(), sell[:1].sum(), sell[:3].sum(), sell[:5].sum()))
    return pd.DataFrame(rows, columns=["ticker", "date", "b1", "b3", "b5", "s1", "s3", "s5"])


def run(name: str, db: str, nblk: int, cached: str | None, rl: pd.DataFrame):
    raw = common.load_ipot(db)
    d = panel(db, nblk, cached).merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"]).reset_index(drop=True)
    d = d.merge(topn(raw), on=["ticker", "date"], how="left")
    val = d["value"].replace(0, np.nan)
    for k in (1, 3, 5):
        d[f"net{k}"] = (d[f"b{k}"] - d[f"s{k}"]) / val
    d["buy3_ratio"] = d["b3"] / val
    d = d.sort_values(["ticker", "date"])
    fb = d["F_buy_vis"].astype(float)
    d["f_ratio"] = fb / fb.groupby(d["ticker"]).transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    allv = raw[(raw.fd == "all") & (raw.board == "all")].groupby("broker")["buy_val"].sum()
    fv = raw[(raw.fd == "F") & (raw.board == "all")].groupby("broker")["buy_val"].sum()
    foreign = (fv / allv).fillna(0) > .5
    d["foreign_dom"] = d["dom_broker"].map(foreign).fillna(False).astype(bool)
    rules = {
        "H1a BigAcc top3 (>=P90)": d["net3"] >= d["net3"].quantile(.9),
        "H1b BigAcc top1 (>=P90)": d["net1"] >= d["net1"].quantile(.9),
        "H1c BigAcc top5 (>=P90)": d["net5"] >= d["net5"].quantile(.9),
        "H1d BigDist top3 (<=P10)": d["net3"] <= d["net3"].quantile(.1),
        "H2  BrokerIndex top3>=20%": d["buy3_ratio"] >= 0.20,
        "H3  Foreign uptrend >=1.5x": d["f_ratio"] >= 1.5,
        "H4  Foreign dom & near cost": d["foreign_dom"] & d["dom_vwap_dist"].between(-.03, .03),
    }
    base = d.groupby(["blk", "vol"])["tp_first"].transform("mean")
    print(f"\n===== {name}: {len(d)} rows, {d.ticker.nunique()} tickers | base hit {100 * d.tp_first.mean():.1f}% ret {100 * d.ret.mean():+.2f}% =====")
    for r, m in rules.items():
        s = d[m.fillna(False)]
        if len(s) < 100:
            print(f"  {r:<28} n={len(s)} too few"); continue
        lift = s.tp_first - base.loc[s.index]
        mu = lift.mean(); cs = lift.groupby(s["ticker"]).apply(lambda v: float((v - mu).sum())); t = mu / (np.sqrt((cs ** 2).sum()) / len(s))
        byb = " ".join(f"b{b}:{100 * lift[s.blk == b].mean():+.1f}" for b in sorted(d.blk.unique()))
        no = nonoverlap(s)
        print(f"  {r:<28} n={len(s):>5} hit {100 * s.tp_first.mean():4.1f}% lift {100 * mu:+5.1f}pp (t {t:+.1f}) | {byb} | ret {100 * s.ret.mean():+.2f}% (all {100 * d.ret.mean():+.2f}%) | non-overlap n{len(no)} ret {100 * no.ret.mean():+.2f}%")


def main():
    rl = realised(common.load_ohlcv(start="2025-06-01"))
    run("top150 (1 year, 3 blocks)", TOP, 3, common.OUT_DIR + "/features.pkl", rl)
    run("outside top150 (3 months, 2 blocks)", OUT, 2, None, rl)


if __name__ == "__main__":
    main()
