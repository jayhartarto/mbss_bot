"""
research/ipot_explore/holdout_eval.py — SOFT retroactive holdout: every rule is re-fitted on rows dated BEFORE the cutoff minus a 28-calendar-day embargo, then evaluated on rows dated >= cutoff.
Soft because the earlier exploration already looked at the whole period (which rules/mechanics to test were chosen with that knowledge); the only clean test is the prospective one from 2026-10-26.
Rules evaluated: (1) mover footprint score (all rows), (2) VCP V4 / V5 episodes with the marker ON vs OFF (TP3 / TP1 mechanics, 0.4% cost), (3) XL as #1 net buyer (demeaned rel_5d / rel_10d; ticker means from the pre-cutoff period).
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.holdout_eval [--cutoff 2026-08-24]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from research.ipot_explore import common, features
from research.ipot_explore.mover_footprint import panel, TOP, OUT, SKIP
from research.ipot_explore.mover_rule_walkforward import realised, select, score, SIZE
from research.ipot_explore.vcp_marker_ladder import vcp_flags, vcp_returns
from research.ipot_explore.vcp_marker_combined import episodes, diff_t

COST = 0.004


def main(cutoff: str):
    cut = pd.Timestamp(cutoff); emb = cut - pd.Timedelta(days=28)
    px_all = common.load_ohlcv(start="2025-01-01")
    dates = pd.DatetimeIndex(sorted(px_all["date"].unique())); tdi = {d: i for i, d in enumerate(dates)}
    flags, vr = vcp_flags(px_all), vcp_returns(px_all)
    rl = realised(common.load_ohlcv(start="2025-06-01"))
    d1 = panel(TOP, 3, common.OUT_DIR + "/features.pkl").merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"])
    d2 = panel(OUT, 2, None).merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"])
    d = pd.concat([d1, d2[~d2.set_index(["ticker", "date"]).index.isin(d1.set_index(["ticker", "date"]).index)]], ignore_index=True)
    feats = [c for c in d.columns if c in d1.columns and c in d2.columns and c not in SKIP and c != "ret" and not c.endswith(SIZE) and c not in ("value", "volume")
             and (pd.api.types.is_numeric_dtype(d[c]) or pd.api.types.is_bool_dtype(d[c]))]
    tr = d[d["date"] < emb].copy(); te = d[d["date"] >= cut].copy()
    u = np.sort(tr["date"].unique()); k = len(u) // 3
    tr["blk"] = np.where(tr["date"] <= u[k], 1, np.where(tr["date"] <= u[2 * k], 2, 3))
    tr["vol"] = pd.qcut(tr["atr"], 3, labels=False); te["vol"] = pd.qcut(te["atr"], 3, labels=False); te["blk"] = 1
    print(f"cutoff {cutoff}: train rows {len(tr)} ({tr.date.min().date()}..{tr.date.max().date()}), holdout rows {len(te)} ({te.date.min().date()}..{te.date.max().date()}, {te.ticker.nunique()} tickers)")
    kept = select(tr, feats)
    print("features kept on train:", ", ".join(f"{c}({'+' if sg > 0 else '-'})" for c, sg, *_ in kept) or "none")
    thr = score(tr, kept).quantile(0.8); te["score"] = score(te, kept); te["sel"] = te["score"] >= thr
    on, off = te[te.sel], te[~te.sel]
    base = te.groupby("vol")["tp_first"].transform("mean")
    lift = (on.tp_first - base.loc[on.index]).mean()
    cs = (on.tp_first - base.loc[on.index]).groupby(on.ticker).apply(lambda v: float((v - lift).sum())); t = lift / (np.sqrt((cs ** 2).sum()) / len(on))
    print(f"\n(1) MOVER SCORE on holdout: ON n {len(on)} hit {100 * on.tp_first.mean():.1f}% ret {100 * on.ret.mean():+.2f}% | OFF n {len(off)} hit {100 * off.tp_first.mean():.1f}% ret {100 * off.ret.mean():+.2f}% | "
          f"ATR-matched lift {100 * lift:+.1f}pp (t {t:+.1f}) | base hit {100 * te.tp_first.mean():.1f}%")
    D = te.merge(flags, on=["ticker", "date"], how="left").merge(vr, on=["ticker", "date"], how="left")
    print("\n(2) VCP episodes in the holdout (net of 0.4%), marker ON vs OFF:")
    for v in ("V3", "V4", "V5"):
        s = episodes(D, v, tdi)
        if len(s) < 40 or s.sel.sum() < 8:
            print(f"   {v}: episodes {len(s)} (ON {int(s.sel.sum())}) too few"); continue
        s = s.assign(a=s.r_tp1 - COST, b=s.r_tp3 - COST); da, ta = diff_t(s, "a"); db, tb = diff_t(s, "b")
        print(f"   {v}: episodes {len(s)} ON {int(s.sel.sum())} / OFF {int((~s.sel).sum())} | TP3 ON {100 * s[s.sel].b.mean():+.2f}% vs OFF {100 * s[~s.sel].b.mean():+.2f}% (diff {100 * db:+.2f}pp, t {tb:+.1f}) | TP1 diff {100 * da:+.2f}pp (t {ta:+.1f})")
    print("\n(3) XL as #1 net buyer, demeaned relative return (%), holdout dates only:")
    raw = common.load_ipot(TOP); feat = pd.read_pickle(common.OUT_DIR + "/features.pkl")[["ticker", "date", "rel_5d", "rel_10d"]]
    a = features.broker_day_table(raw, "all", "all"); a = a[a["net_val_mid"] > 0].copy()
    a["rk"] = a.groupby(["ticker", "date"])["net_val_mid"].rank(ascending=False, method="first")
    ev = a[(a.rk == 1) & (a.broker == "XL")][["ticker", "date"]]
    for h in ("rel_5d", "rel_10d"):
        mu = feat[feat.date < emb].groupby("ticker")[h].mean()
        f = feat.assign(y=feat[h] - feat["ticker"].map(mu)).dropna(subset=["y"]); f = f[f.date >= cut]
        e = f.merge(ev, on=["ticker", "date"]); rest = f.merge(ev.assign(x=1), on=["ticker", "date"], how="left"); rest = rest[rest.x.isna()]
        m = e.y.mean(); cs = e.groupby("ticker")["y"].apply(lambda v: float((v - m).sum())); tt = m / (np.sqrt((cs ** 2).sum()) / len(e))
        print(f"   {h}: XL events n {len(e)} mean {100 * m:+.2f}% (t {tt:+.1f}) | all other rows {100 * rest.y.mean():+.2f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--cutoff", default="2026-08-24")
    main(ap.parse_args().cutoff)
