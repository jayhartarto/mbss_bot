"""
research/ipot_explore/bandar_definition.py — how to define "a broker is accumulating a stock and the stock then moves within 20 days", with definitions fixed BEFORE looking at outcomes.

Run (project root):  python -m research.ipot_explore.bandar_definition

Target ("short-term move"): from open(t+1), the daily high reaches +10% before the daily low reaches -7% within 20 trading days (same-day touch of both counts as the stop). Base rate is
reported per date block; a definition is useful only if its hit rate beats the base rate of the SAME block in all 3 blocks.
Accumulation features per window W in (5, 10, 20): dominant net buyer (argmax of rolling net_val_mid), its inventory share of traded value, share of days it was a net buyer (one-way),
ramp (last 3 days vs the rest), price vs its average buy price, past W-day return of the stock (quiet = |ret| <= 5%), foreign/domestic by the broker's own foreign share.
Definitions (fixed list): D1 strong one-way | D2 = D1 + quiet price | D3 = D2 + price within [-3%, +5%] of the dominant's average buy | D4 = D3 + ramp>0 | D1 split foreign / domestic.
"Strong" = inventory share >= P80 of all rows. Events are also counted as first-of-streak (same ticker+dominant not flagged in the previous 5 trading days).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features

DB = "research/ipot_broker_daily_vps_2026_10_22.sqlite"
TP, SL, HOR = 0.10, -0.07, 20


def label(px: pd.DataFrame) -> pd.DataFrame:
    out = []
    for tk, g in px.sort_values(["ticker", "date"]).groupby("ticker"):
        o, h, l = g["open"].to_numpy(), g["high"].to_numpy(), g["low"].to_numpy()
        n = len(g)
        hit = np.full(n, np.nan); tt = np.full(n, np.nan)
        for i in range(n - 1):
            if i + 1 + HOR > n - 1 and i + 1 + 5 > n - 1:
                continue
            e = o[i + 1]
            if not e > 0:
                continue
            last = min(n - 1, i + HOR)
            if last - i < HOR:
                continue                      # incomplete horizon: leave unlabeled
            res = 0.0
            for k in range(i + 1, last + 1):
                if l[k] / e - 1 <= SL:
                    res = 0.0; break
                if h[k] / e - 1 >= TP:
                    res = 1.0; tt[i] = k - i; break
            hit[i] = res
        out.append(pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy(), "tp_first": hit, "days_to_tp": tt}))
    return pd.concat(out, ignore_index=True)


def block_of(dates: pd.Series) -> pd.Series:
    u = np.sort(dates.unique()); k = len(u) // 3
    return pd.Series(np.select([dates <= u[k], dates <= u[2 * k]], [1, 2], 3), index=dates.index)


def first_of_streak(d: pd.DataFrame, mask: pd.Series) -> pd.Series:
    key = d["ticker"] + "|" + d["dom_broker"].astype(str)
    m = mask.copy()
    prev = d.assign(m=m.astype(int), key=key).sort_values(["key", "date"]).groupby("key")["m"].transform(lambda s: s.shift(1).rolling(5, min_periods=1).max()).reindex(d.index).fillna(0)
    return m & (prev == 0)


def main():
    raw = common.load_ipot(DB)
    px_all = common.load_ohlcv(start="2025-01-01")
    px = px_all[px_all.ticker.isin(raw.ticker.unique())]
    lab = label(px_all[px_all.ticker.isin(raw.ticker.unique())])
    allv = raw[(raw.fd == "all") & (raw.board == "all")].groupby("broker")["buy_val"].sum()
    fv = raw[(raw.fd == "F") & (raw.board == "all")].groupby("broker")["buy_val"].sum()
    fshare = (fv / allv).fillna(0.0)
    feat = pd.read_pickle(common.OUT_DIR + "/features.pkl")[["ticker", "date", "rel_10d"]]
    summary = []
    for W in (5, 10, 20):
        pat = features.rolling_pattern_features(raw, px, window=W).dropna(subset=["dom_broker", "dom_vwap_dist", "dom_inventory_share"])
        d = pat.merge(lab, on=["ticker", "date"], how="inner").dropna(subset=["tp_first"]).merge(feat, on=["ticker", "date"], how="left").reset_index(drop=True)
        d["blk"] = block_of(d["date"])
        d["foreign"] = d["dom_broker"].map(fshare) > 0.5
        strong = d["dom_inventory_share"] >= d["dom_inventory_share"].quantile(0.8)
        oneway = d["dom_pos_days"] >= 0.7
        quiet = d["past_ret_10d"].abs() <= 0.05 if W == 10 else None
        if W != 10:
            c = px_all.sort_values(["ticker", "date"]).copy()
            c["pr"] = c.groupby("ticker")["close"].pct_change(W)
            d = d.merge(c[["ticker", "date", "pr"]], on=["ticker", "date"], how="left")
            quiet = d["pr"].abs() <= 0.05
        near = d["dom_vwap_dist"].between(-0.03, 0.05)
        defs = {"D1 strong+oneway": strong & oneway, "D2 +quiet": strong & oneway & quiet, "D3 +near cost": strong & oneway & quiet & near,
                "D4 +ramp>0": strong & oneway & quiet & near & (d["dom_ramp"] > 0), "D1 foreign": strong & oneway & d["foreign"], "D1 domestic": strong & oneway & ~d["foreign"]}
        base = d.groupby("blk")["tp_first"].mean()
        print(f"\n===== W={W} | labelled rows {len(d)} | base hit (TP+10% before SL-7% in 20d): " + " ".join(f"b{b}={base[b]:.3f}" for b in base.index) + f" | base mean rel_10d {d['rel_10d'].mean() * 100:+.2f}% =====")
        print("  definition              n (first-of-streak) | hit% b1 / b2 / b3  (lift pp vs block base)       | mean rel_10d % | days_to_TP median")
        for name, m in defs.items():
            fs = first_of_streak(d, m)
            s = d[m]
            cells = []
            for b in (1, 2, 3):
                sb = s[s.blk == b]
                cells.append(f"{100 * sb['tp_first'].mean():4.1f} ({100 * (sb['tp_first'].mean() - base[b]):+4.1f}; n{len(sb)})" if len(sb) >= 30 else f"  -  (n{len(sb)})")
            fsm = d[fs]
            print(f"  {name:<22} {m.sum():>6} ({fs.sum():>5}) | " + " / ".join(cells) + f" | {100 * s['rel_10d'].mean():+.2f} | {s['days_to_tp'].median():.0f}"
                  + f" | FS hit {100 * fsm['tp_first'].mean():.1f}")
            summary.append((W, name, len(s)))
    return None


if __name__ == "__main__":
    main()
