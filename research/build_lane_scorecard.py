"""
research/build_lane_scorecard.py — MBSS v2 (2026-10-22).

Builds engine/lane_scorecard_table.json: per lane x IHSG regime phase (and per lane overall), the backtest statistics the BELI-A / BELI-B
tags are decided from (n, win%, mean%, median%, ticker-cluster bootstrap CI of the mean, Wilson CI of the win rate).

Population: research/.hsc_out/wf/pooled_flagged.pkl = the lane trade simulations (production exit mechanics, HSC excluded,
2023-06..2026-10, 14,956 trades: BOW v3, BOS, VCP, MACD BOL, OSB-v2, BSJP Lane1, BSJP Lane2). Regime = engine/ihsg_regime.py phase on the
signal day (for BSJP Lane2 the signal day is D0, the day the move happened).

Re-run when new lane simulations exist; the engine only reads the JSON (static snapshot, provenance inside).
Usage: python research/build_lane_scorecard.py
"""
import datetime
import json
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine.legacy_core as core          # noqa: E402
import engine.ihsg_regime as ihsg_regime   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POP = os.path.join(ROOT, "research", ".hsc_out", "wf", "pooled_flagged.pkl")
OUT = os.path.join(ROOT, "engine", "lane_scorecard_table.json")
LANE_KEY = {"BOW v3": "BOW", "BOS": "BOS", "VCP": "VCP", "MACD BOL": "MACD-confirm", "OSB-v2": "OSB-v2",
            "BSJP Lane1": "BSJP-L1", "BSJP Lane2": "BSJP-L2"}
rng = np.random.default_rng(2026)


def wilson(w, n, z=1.96):
    ph = w / n
    den = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / den
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return (c - h) * 100, (c + h) * 100


def cluster_ci(ret, tick, B=1000):
    g = pd.DataFrame({"r": ret.values, "t": tick.values}).groupby("t").r.agg(["sum", "count"])
    s, n = g["sum"].values, g["count"].values
    m = len(g)
    if m < 3:
        return None, None
    v = [s[i].sum() / n[i].sum() for i in (rng.integers(0, m, m) for _ in range(B))]
    lo, hi = np.percentile(v, [2.5, 97.5])
    return float(lo), float(hi)


def cell(sub):
    n = len(sub)
    w = int((sub.ret > 0).sum())
    wl, wh = wilson(w, n)
    lo, hi = cluster_ci(sub.ret, sub.ticker)
    return {"n": n, "win": round(w / n * 100, 1), "win_lo": round(wl, 1), "win_hi": round(wh, 1),
            "mean": round(float(sub.ret.mean()), 2), "median": round(float(sub.ret.median()), 2),
            "mean_lo": None if lo is None else round(lo, 2), "mean_hi": None if hi is None else round(hi, 2),
            "tickers": int(sub.ticker.nunique())}


def main():
    df = pd.read_pickle(POP)
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    hist = core.get_yf_ticker("^JKSE").history(period="max", timeout=60)
    reg = ihsg_regime._build_regime(hist)
    reg["date"] = pd.to_datetime(reg["date"]).dt.tz_localize(None).dt.normalize().astype("datetime64[ns]")
    reg = reg[["date", "regime_phase"]].sort_values("date")
    df = pd.merge_asof(df.sort_values("date"), reg, on="date", direction="backward")
    table = {}
    for lane_bt, key in LANE_KEY.items():
        sub = df[df.lane == lane_bt]
        table[key] = {"ALL": cell(sub)}
        for ph, g in sub.groupby("regime_phase"):
            table[key][ph] = cell(g)
    payload = {
        "built": datetime.date.today().isoformat(),
        "population": "research/.hsc_out/wf/pooled_flagged.pkl (lane trade sims, HSC excluded)",
        "date_range": [df.date.min().strftime("%Y-%m-%d"), df.date.max().strftime("%Y-%m-%d")],
        "n_trades": int(len(df)),
        "note": "ret = lane-sim return %; win = ret>0; mean_lo/hi = 95% ticker-cluster bootstrap CI; regime = phase on the signal day",
        "lanes": table,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("wrote", OUT)
    for k, v in table.items():
        print(k, {ph: (c["n"], c["win"], c["mean"]) for ph, c in v.items()})


if __name__ == "__main__":
    main()
