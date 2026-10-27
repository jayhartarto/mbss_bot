"""
research/ipot_explore/freeze_rules.py — fit and FREEZE the broker-marker rules on all data up to the freeze date, for the PROSPECTIVE holdout (dates strictly after 2026-10-26).
Writes research/ipot_explore/frozen/mover_rule_<date>.json (features, signs, z-parameters, P80 threshold). Do NOT refit or edit afterwards; evaluate only on dates > the holdout start.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.freeze_rules
"""
from __future__ import annotations

import json
import subprocess

import numpy as np
import pandas as pd

from research.ipot_explore import common
from research.ipot_explore.mover_footprint import panel, TOP, OUT, SKIP
from research.ipot_explore.mover_rule_walkforward import realised, select, score, SIZE

FREEZE_DATE = "2026-10-24"
HOLDOUT_START = "2026-10-26"


def main():
    rl = realised(common.load_ohlcv(start="2025-06-01"))
    d1 = panel(TOP, 3, common.OUT_DIR + "/features.pkl").merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"])
    d2 = panel(OUT, 2, None).merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"])
    d = pd.concat([d1, d2[~d2.set_index(["ticker", "date"]).index.isin(d1.set_index(["ticker", "date"]).index)]], ignore_index=True)
    u = np.sort(d["date"].unique()); k = len(u) // 3
    d["blk"] = np.where(d["date"] <= u[k], 1, np.where(d["date"] <= u[2 * k], 2, 3)); d["vol"] = pd.qcut(d["atr"], 3, labels=False)
    feats = [c for c in d.columns if c in d1.columns and c in d2.columns and c not in SKIP and c != "ret" and not c.endswith(SIZE) and c not in ("value", "volume")
             and (pd.api.types.is_numeric_dtype(d[c]) or pd.api.types.is_bool_dtype(d[c]))]
    kept = select(d, feats)
    thr = float(score(d, kept).quantile(0.8))
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception:
        commit = ""
    spec = {
        "frozen_on": FREEZE_DATE, "holdout_start_after": HOLDOUT_START, "git_head": commit, "rows_used": int(len(d)), "labelled_through": str(d.date.max().date()),
        "mover_rule": {"features": [{"name": c, "sign": sg, "mean": mu, "sd": sd, "clip_lo": lo, "clip_hi": hi} for c, sg, mu, sd, lo, hi in kept],
                       "threshold_P80_of_train_score": thr, "marker_ON": "score >= threshold", "target": "TP+10% / SL-7% / 20 trading days from open(t+1)"},
        "xl_rule": {"event": "broker XL is the #1 net buyer (net_val_mid > 0, rank 1) of a ticker-day in the all/all variant", "expected_sign": "negative demeaned rel_5d and rel_10d",
                    "ticker_mean_frozen_through": "2026-10-23", "decision": "pass if mean < 0 and date-clustered one-sided p < 0.10 after >= 8 weeks"},
        "vcp_rule": {"signal": "V4 (see vcp_marker_ladder.vcp_flags) with one entry per 12 trading days", "marker": "mover_rule ON", "mechanic": "entry open(t+1), TP3 +7% / SL -10% / 12 days, cost 0.4% round trip",
                     "expected": "ON minus OFF net return > 0", "decision": "pass if the difference is > 0 with one-sided p < 0.10 after >= 12 weeks + 12 trading days"},
    }
    p = f"research/ipot_explore/frozen/mover_rule_{FREEZE_DATE}.json"
    json.dump(spec, open(p, "w"), indent=2, default=float)
    print("frozen ->", p); print("features:", ", ".join(f"{c}({'+' if sg > 0 else '-'})" for c, sg, *_ in kept), "| threshold", round(thr, 4))


if __name__ == "__main__":
    main()
