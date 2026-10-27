"""
research/ipot_explore/vcp_avoid_marker.py — inside the loosened VCP episodes (V4, V5; one entry per 12 trading days) look for an AVOID marker: features that predict a LOSER.
Loser = the TP3 mechanic hits the -10% stop (r_tp3 <= -0.0999); secondary view = net return < 0 after 0.4% cost.
Same machinery as the winner marker: on TRAIN episodes only, scan every feature (incl. technicals and IHSG regime dummies) for |t| >= 2.5 with the same sign in every train tercile, score = mean(sign*z),
flag = score >= train P80 (the 20% most loser-like). Folds are time terciles of the episodes with a 20-day embargo (A->B, A+B->C, C->A); every episode is tested once. Reported: stop-rate and net returns of
flagged vs not flagged, and a 2x2 with the winner marker (ON/OFF) from vcp_marker_combined.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.vcp_avoid_marker
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common
from research.ipot_explore.mover_footprint import panel, TOP, OUT, SKIP
from research.ipot_explore.mover_rule_walkforward import realised, SIZE, score
from research.ipot_explore.mover_rule_by_regime import split, selected
from research.ipot_explore.vcp_marker_ladder import vcp_flags, vcp_returns
from research.ipot_explore.vcp_marker_combined import episodes
from research.ipot_explore.xl_study import ihsg_phase

COST = 0.004


def select_y(train: pd.DataFrame, feats: list[str], y: str, tmin: float = 2.5):
    cell = train["blk"].astype(str)
    yy = train[y] - train.groupby(cell)[y].transform("mean")
    kept = []
    for c in feats:
        x = train[c].astype(float).replace([np.inf, -np.inf], np.nan)
        if x.notna().mean() < 0.7 or x.nunique() < 3:
            continue
        lo, hi = x.quantile([.01, .99]); xc = x.clip(lo, hi)
        xs = (xc - xc.groupby(cell).transform("mean")) / xc.std(); m = xs.notna()
        xv, yv = xs[m].to_numpy(), yy[m].to_numpy()
        beta = (xv * yv).sum() / (xv * xv).sum(); e = yv - beta * xv
        cs = pd.Series(xv * e).groupby(train.loc[m, "ticker"].to_numpy()).sum()
        t = beta / (np.sqrt((cs ** 2).sum()) / (xv * xv).sum())
        sg = [np.sign((xv[(train.loc[m, "blk"] == b).to_numpy()] * yv[(train.loc[m, "blk"] == b).to_numpy()]).sum()) for b in sorted(train.blk.unique())]
        if abs(t) >= tmin and all(s == sg[0] for s in sg) and sg[0] != 0:
            kept.append((c, float(np.sign(beta)), float(xc.mean()), float(xc.std()), lo, hi))
    return kept


def main():
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
        tr, te = split(d, tb); tr = tr[tr.blk.isin(trb)]; parts.append(selected(tr, te, feats)[0])
    P = pd.concat(parts)[["ticker", "date", "sel"]]
    reg = ihsg_phase()
    D = d.merge(flags, on=["ticker", "date"], how="left").merge(vr, on=["ticker", "date"], how="left").merge(P, on=["ticker", "date"], how="left").merge(reg, on="date", how="left")
    D["regime_phase"] = D["regime_phase"].fillna("NA")
    for r in ("BEARISH_GRIND", "BOUNCE_OFF_LOW", "CRASH_FAST", "NEUTRAL_BULLISH", "PULLBACK_IN_BOUNCE"):
        D[f"reg_{r}"] = (D["regime_phase"] == r).astype(float)
    feats2 = feats + [c for c in D.columns if c.startswith("reg_")]
    for v in ("V4", "V5"):
        E = episodes(D, v, tdi).copy()
        E["stop"] = (E["r_tp3"] <= -0.0999).astype(float); E["net3"] = E["r_tp3"] - COST; E["net1"] = E["r_tp1"] - COST
        E["loss"] = (E["net3"] < 0).astype(float)
        print(f"\n########## {v}: {len(E)} episodes, {E.ticker.nunique()} tickers | stop rate {100 * E.stop.mean():.1f}% | net TP3 {100 * E.net3.mean():+.2f}% TP1 {100 * E.net1.mean():+.2f}% | loss share {100 * E.loss.mean():.1f}% ##########")
        for ycol, lab in (("stop", "STOP-hit (-10%)"), ("loss", "net-negative")):
            out = []
            print(f"\n  target = {lab}")
            for tb, trb in (([2], [1]), ([3], [1, 2]), ([1], [3])):
                tr, te = split(E, tb); tr = tr[tr.blk.isin(trb)]
                kept = select_y(tr, feats2, ycol)
                if not kept:
                    print(f"    test tercile {tb[0]}: no feature passed on train"); continue
                thr = score(tr, kept).quantile(0.8); te = te.copy(); te["avoid"] = score(te, kept) >= thr
                out.append(te)
                print(f"    test tercile {tb[0]}: {len(kept)} feats: " + ", ".join(f"{c}({'+' if sg > 0 else '-'})" for c, sg, *_ in kept)[:200])
            if not out:
                continue
            T = pd.concat(out); a, b = T[T.avoid], T[~T.avoid]
            def t_of(col):
                x = T.avoid.astype(float) - T.avoid.astype(float).mean(); y = T[col]
                be = (x * y).sum() / (x * x).sum(); e = y - y.mean() - be * x; cs = (x * e).groupby(T.ticker).sum()
                return be, be / (np.sqrt((cs ** 2).sum()) / (x * x).sum())
            bs, ts = t_of("stop"); b3, t3 = t_of("net3")
            print(f"    POOLED test episodes {len(T)}: flagged {len(a)} | stop-rate flagged {100 * a.stop.mean():.1f}% vs rest {100 * b.stop.mean():.1f}% (diff {100 * bs:+.1f}pp, t {ts:+.1f}) "
                  f"| net TP3 flagged {100 * a.net3.mean():+.2f}% vs rest {100 * b.net3.mean():+.2f}% (diff {100 * b3:+.2f}pp, t {t3:+.1f}) | all {100 * T.net3.mean():+.2f}% -> after removing flagged {100 * b.net3.mean():+.2f}%")
            if ycol == "stop":
                T["avoid"] = T["avoid"].astype(bool); T["sel"] = T["sel"].fillna(False).astype(bool)
                print("    2x2 with the winner marker (net TP3 mean, n):")
                for on in (True, False):
                    row = []
                    for av in (False, True):
                        x = T[(T.sel == on) & (T.avoid == av)]
                        row.append(f"{'avoid' if av else 'keep '}: {100 * x.net3.mean():+.2f}% (n{len(x)}, stop {100 * x.stop.mean():.0f}%)" if len(x) >= 10 else f"{'avoid' if av else 'keep '}: n{len(x)} -")
                    print(f"      winner-marker {'ON ' if on else 'OFF'} | " + " | ".join(row))


if __name__ == "__main__":
    main()
