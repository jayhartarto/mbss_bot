"""
research/ipot_explore/test_skill.py — synthetic checks for engine 4.  Run:  python -m research.ipot_explore.test_skill
Skill scenario: 3 of 10 brokers have a TRUE effect on the 5-day relative return when they are the top net buyer (+2%, +1%, -1.5%); the rest are pure noise.
Null scenario: no broker has any effect.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import broker_skill

BROKERS = [f"B{i}" for i in range(10)]
EFFECT = {"B0": 0.02, "B1": 0.01, "B2": -0.015}


def _make(skill: bool, n_tickers=20, n_days=120, seed=4):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2026-01-05", periods=n_days)
    rows, outc = [], []
    for k in range(n_tickers):
        tk = f"T{k}"
        for d in dates:
            chosen = rng.choice(BROKERS, size=4, replace=False)
            nets = rng.uniform(1e7, 1e8, size=4)
            for b, nv in zip(chosen, nets):
                rows.append({"ticker": tk, "date": d, "fd": "all", "board": "all", "broker": b, "buy_val": nv, "buy_lot": nv / 1e5, "buy_avg": 1000.0,
                             "sell_val": np.nan, "sell_lot": np.nan, "sell_avg": np.nan})
            top = chosen[int(np.argmax(nets))]
            eff = EFFECT.get(top, 0.0) if skill else 0.0
            rel5 = rng.normal(0, 0.03) + eff
            outc.append({"ticker": tk, "date": d, "rel_1d": rng.normal(0, 0.01), "rel_3d": rng.normal(0, 0.02), "rel_5d": rel5, "rel_10d": np.nan})
    return pd.DataFrame(rows), pd.DataFrame(outc)


def test_skill_detected():
    raw, out = _make(skill=True)
    res = broker_skill.run(raw, out, side="buy", top_n=1, n_min=60, B=100, verbose=False)[5]
    post = res["est"].set_index("broker")
    assert post["posterior"].idxmax() == "B0" and post["posterior"].idxmin() == "B2", post["posterior"].round(4).to_dict()
    assert post.loc["B0", "posterior"] > 0.008 and post.loc["B2", "posterior"] < -0.005
    assert res["tau2"] > 0 and res["p_maxz"] < 0.05, (res["tau2"], res["p_maxz"])
    assert res["rho"] > 0.3, res["rho"]                      # the same brokers are good in both halves
    print("ok skill: B0 %+.2f%%, B2 %+.2f%%, tau=%.2f%%, max|z| p=%.3f, rho=%.2f" % (
        post.loc["B0", "posterior"] * 100, post.loc["B2", "posterior"] * 100, np.sqrt(res["tau2"]) * 100, res["p_maxz"], res["rho"]))


def test_null_no_skill():
    raw, out = _make(skill=False, seed=8)
    res = broker_skill.run(raw, out, side="buy", top_n=1, n_min=60, B=100, verbose=False)[5]
    assert res["p_D"] > 0.05 and res["p_maxz"] > 0.05, (res["p_D"], res["p_maxz"])
    assert np.sqrt(res["tau2"]) < 0.005, res["tau2"]                  # EB spread of true effects ~ 0
    post = res["est"]["posterior"]
    assert post.abs().max() < 0.004, post.round(4).to_dict()           # everything shrinks toward the mean
    print("ok null: tau=%.2f%%, p(D)=%.2f, p(max|z|)=%.2f, max |posterior|=%.2f%%" % (np.sqrt(res["tau2"]) * 100, res["p_D"], res["p_maxz"], post.abs().max() * 100))


if __name__ == "__main__":
    test_skill_detected()
    test_null_no_skill()
    print("ALL OK")
