"""
research/ipot_explore/vcp_marker_combined.py — VCP ladder with the broker-footprint marker on the COMBINED population (top150 year + outside-top150 3 months), one entry per episode.

Marker: time-blocked walk-forward on the pooled rows (3 date terciles of the pooled sample; folds A->B, A+B->C, C->A with a 20-day embargo; every pooled row is a test row exactly once).
Episodes: per ticker and ladder step, a VCP signal day starts an episode and the next episode may start only 12 trading days later (the lane's expiry), so ON and OFF are compared on the SAME
episode starts; the marker status is the one of the episode's first day. Costs: round trip 0.4% (commission+tax, `--cost` to change) subtracted from every trade; a 0.8% variant is also printed.
Mechanic as vcp_marker_ladder.py (TP1 +2.5% / TP3 +7%, SL -10%, 12 days, entry open(t+1)). Split reported by pooled date tercile and by IHSG regime of the entry day.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.vcp_marker_combined
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common
from research.ipot_explore.mover_footprint import panel, TOP, OUT, SKIP
from research.ipot_explore.mover_rule_walkforward import realised, SIZE
from research.ipot_explore.mover_rule_by_regime import split, selected
from research.ipot_explore.vcp_marker_ladder import vcp_flags, vcp_returns
from research.ipot_explore.xl_study import ihsg_phase

COST = 0.004


def episodes(d: pd.DataFrame, flag: str, tdi: dict, gap: int = 12) -> pd.DataFrame:
    s = d[d[flag].fillna(False)].dropna(subset=["r_tp1", "r_tp3"]).copy()
    s["t"] = s["date"].map(tdi)
    keep = []
    for tk, g in s.sort_values(["ticker", "date"]).groupby("ticker"):
        last = None
        for idx, t in zip(g.index, g["t"]):
            if last is None or t - last >= gap:
                keep.append(idx); last = t
    return s.loc[keep]


def diff_t(s: pd.DataFrame, col: str):
    x = s.sel.astype(float) - s.sel.astype(float).mean(); y = s[col]
    if (x * x).sum() == 0:
        return np.nan, np.nan
    b = (x * y).sum() / (x * x).sum(); e = y - y.mean() - b * x
    cs = (x * e).groupby(s.ticker).sum()
    return b, b / (np.sqrt((cs ** 2).sum()) / (x * x).sum())


def main(cost: float = COST):
    px_all = common.load_ohlcv(start="2025-01-01")
    dates = pd.DatetimeIndex(sorted(px_all["date"].unique())); tdi = {d: i for i, d in enumerate(dates)}
    flags, vr = vcp_flags(px_all), vcp_returns(px_all)
    rl = realised(common.load_ohlcv(start="2025-06-01"))
    d1 = panel(TOP, 3, common.OUT_DIR + "/features.pkl").merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"])
    d2 = panel(OUT, 2, None).merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"])
    d = pd.concat([d1, d2[~d2.set_index(["ticker", "date"]).index.isin(d1.set_index(["ticker", "date"]).index)]], ignore_index=True)
    u = np.sort(d["date"].unique()); k = len(u) // 3
    d["blk"] = np.where(d["date"] <= u[k], 1, np.where(d["date"] <= u[2 * k], 2, 3)); d["vol"] = pd.qcut(d["atr"], 3, labels=False)
    feats = [c for c in d.columns if c in d1.columns and c in d2.columns and c not in SKIP and c != "ret" and not c.endswith(SIZE) and c not in ("value", "volume")
             and (pd.api.types.is_numeric_dtype(d[c]) or pd.api.types.is_bool_dtype(d[c]))]
    parts = []
    for tb, trb in (([2], [1]), ([3], [1, 2]), ([1], [3])):
        tr, te = split(d, tb); tr = tr[tr.blk.isin(trb)]
        t, kept = selected(tr, te, feats); parts.append(t)
        print(f"fold test block {tb[0]}: features {len(kept) if kept else 0}: " + (", ".join(f"{c}({'+' if sg > 0 else '-'})" for c, sg, *_ in kept) if kept else "-"))
    P = pd.concat(parts).merge(flags, on=["ticker", "date"], how="left").merge(vr, on=["ticker", "date"], how="left")
    P["ALL"] = True
    reg = ihsg_phase(); P = P.merge(reg, on="date", how="left"); P["regime_phase"] = P["regime_phase"].fillna("NA")
    print(f"\npooled rows {len(P)} ({P.ticker.nunique()} tickers), marker ON {100 * P.sel.mean():.0f}%, dates {P.date.min().date()}..{P.date.max().date()}")
    for c in (cost, 2 * cost):
        print(f"\n===== one entry per episode, round-trip cost {100 * c:.1f}% =====")
        print("  step | episodes (tickers) | ON n / OFF n | TP1 net mean ret ON vs OFF (diff, t) | TP3 net ON vs OFF (diff, t) | all-episode TP1/TP3 net")
        for v in ("V0", "V1", "V2", "V3", "V4", "V5", "ALL"):
            s = episodes(P, v, tdi)
            if v == "ALL":
                s = s.iloc[::12]
            if len(s) < 60 or s.sel.sum() < 20:
                print(f"  {v:<4} | n={len(s)} (ON {int(s.sel.sum())}) too few"); continue
            s = s.assign(a=s.r_tp1 - c, b=s.r_tp3 - c)
            da, ta = diff_t(s, "a"); db, tb = diff_t(s, "b")
            print(f"  {v:<4} | {len(s):>5} ({s.ticker.nunique():>3}) | {int(s.sel.sum()):>4} / {int((~s.sel).sum()):>4} | {100 * s[s.sel].a.mean():+.2f}% vs {100 * s[~s.sel].a.mean():+.2f}% ({100 * da:+.2f}pp, t {ta:+.1f}) "
                  f"| {100 * s[s.sel].b.mean():+.2f}% vs {100 * s[~s.sel].b.mean():+.2f}% ({100 * db:+.2f}pp, t {tb:+.1f}) | {100 * s.a.mean():+.2f}% / {100 * s.b.mean():+.2f}%")
    print(f"\n===== stability at cost {100 * cost:.1f}%: V1 and V4, diff ON-OFF (net TP3 / TP1), by pooled date tercile and IHSG regime =====")
    for v in ("V1", "V4"):
        s = episodes(P, v, tdi).assign(a=lambda x: x.r_tp1 - cost, b=lambda x: x.r_tp3 - cost)
        print(f"  {v}: episodes {len(s)}")
        for lab, col in (("tercile", "blk"), ("regime", "regime_phase")):
            for g, x in s.groupby(col):
                if x.sel.sum() < 12 or (~x.sel).sum() < 12:
                    print(f"     {lab} {g}: ON {int(x.sel.sum())} OFF {int((~x.sel).sum())} too few"); continue
                da, ta = diff_t(x, "a"); db, tb = diff_t(x, "b")
                print(f"     {lab} {str(g):<18} ON {int(x.sel.sum()):>3} OFF {int((~x.sel).sum()):>4} | TP3 {100 * db:+.2f}pp (t {tb:+.1f}) [ON {100 * x[x.sel].b.mean():+.2f}% OFF {100 * x[~x.sel].b.mean():+.2f}%] | TP1 {100 * da:+.2f}pp (t {ta:+.1f})")


if __name__ == "__main__":
    main()
