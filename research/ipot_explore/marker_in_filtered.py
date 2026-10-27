"""
research/ipot_explore/marker_in_filtered.py — does the broker-footprint score add lift INSIDE a filtered (momentum / lane) population?
Same cross-fitted score as mover_rule_by_regime.py (top150 3-fold pooled test rows; outside rows scored by the rule trained on top150). Inside each subset the comparison is
marker ON (score >= train P80) vs marker OFF, both within the subset (hit rate, mean realised return, ticker-clustered t of the difference).
Subsets: ALL | momentum (RSI14>60 & ATR%>3.2%, the engine-2 pattern) | RSI14>60 | trend (close above its 20d mean: dist_sma20>0 & past_ret_10d>0) | actual lane trades (research/.hsc_out/wf/pooled_flagged.pkl).
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.marker_in_filtered
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common
from research.ipot_explore.mover_footprint import panel, TOP, OUT, SKIP
from research.ipot_explore.mover_rule_walkforward import realised, SIZE
from research.ipot_explore.mover_rule_by_regime import split, selected


def compare(name, d):
    on, off = d[d.sel], d[~d.sel]
    if len(on) < 60 or len(off) < 60:
        print(f"  {name:<34} on {len(on)} off {len(off)} too few"); return
    diff = on.tp_first.mean() - off.tp_first.mean(); rd = on.ret.mean() - off.ret.mean()
    y = d.tp_first - d.groupby(d.sel)["tp_first"].transform("mean"); x = d.sel.astype(float) - d.sel.astype(float).mean()
    beta = (x * d.tp_first).sum() / (x * x).sum(); e = d.tp_first - d.tp_first.mean() - beta * x
    cs = (x * e).groupby(d.ticker).sum(); t = beta / (np.sqrt((cs ** 2).sum()) / (x * x).sum())
    yr = d.ret; br = (x * yr).sum() / (x * x).sum(); er = yr - yr.mean() - br * x; csr = (x * er).groupby(d.ticker).sum(); tr_ = br / (np.sqrt((csr ** 2).sum()) / (x * x).sum())
    print(f"  {name:<34} rows {len(d):>6} | ON n{len(on):>5} hit {100 * on.tp_first.mean():4.1f}% ret {100 * on.ret.mean():+.2f}% | OFF n{len(off):>5} hit {100 * off.tp_first.mean():4.1f}% ret {100 * off.ret.mean():+.2f}% "
          f"| diff hit {100 * diff:+.1f}pp (t {t:+.1f}) ret {100 * rd:+.2f}pp (t {tr_:+.1f})")


def main():
    rl = realised(common.load_ohlcv(start="2025-06-01"))
    d1 = panel(TOP, 3, common.OUT_DIR + "/features.pkl").merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"]).reset_index(drop=True)
    d2 = panel(OUT, 2, None).merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"]).reset_index(drop=True)
    feats = [c for c in d1.columns if c in d2.columns and c not in SKIP and c != "ret" and not c.endswith(SIZE) and c not in ("value", "volume")
             and (pd.api.types.is_numeric_dtype(d1[c]) or pd.api.types.is_bool_dtype(d1[c]))]
    parts = []
    for tb, trb in (([2], [1]), ([3], [1, 2]), ([1], [3])):
        tr, te = split(d1, tb); tr = tr[tr.blk.isin(trb)]
        parts.append(selected(tr, te, feats)[0])
    pops = {"top150 (3-fold pooled)": pd.concat(parts), "outside (rule from top150)": selected(d1, d2, feats)[0]}
    lane = pd.read_pickle("research/.hsc_out/wf/pooled_flagged.pkl")[["ticker", "date", "lane", "ret"]].rename(columns={"ret": "lane_ret"})
    for pn, d in pops.items():
        print(f"\n===== {pn}: {len(d)} rows =====")
        subs = {"ALL": pd.Series(True, index=d.index), "momentum RSI>60 & ATR>3.2%": (d.rsi14 > 60) & (d.atr_pct > 0.0317), "RSI14>60": d.rsi14 > 60,
                "trend (dist_sma20>0 & ret10d>0)": (d.dist_sma20 > 0) & (d.past_ret_10d > 0), "below trend (dist_sma20<0)": d.dist_sma20 < 0}
        for sn, m in subs.items():
            compare(sn, d[m.fillna(False)])
        m = d.merge(lane, on=["ticker", "date"], how="inner")
        print(f"  actual lane trades with a mover-label (n={len(m)}):")
        compare("  all lanes (hit=mover label)", m)
        for ln, g in m.groupby("lane"):
            compare(f"   {ln}", g)


if __name__ == "__main__":
    main()
