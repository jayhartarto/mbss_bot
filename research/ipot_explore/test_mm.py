"""
research/ipot_explore/test_mm.py — synthetic checks for the MM-ceiling module.  Run:  python -m research.ipot_explore.test_mm
Scenario (20 trading days, traded value 1e9/day): 'MM' trades both sides equally (buys at 1000, sells at 1005), 'AA' accumulates one-way,
'XL' (retail-app class) is a net SELLER, 'S9' is a pure seller. Price rises 1% a day, high = close * 1.01.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import mm_ceiling


def _row(tk, d, broker, buy=None, sell=None, buy_px=1000.0, sell_px=1000.0):
    r = {"ticker": tk, "date": d, "fd": "all", "board": "all", "broker": broker}
    for side, v, p in (("buy", buy, buy_px), ("sell", sell, sell_px)):
        if v is None:
            r.update({f"{side}_lot": np.nan, f"{side}_val": np.nan, f"{side}_avg": np.nan, f"{side}_rank": np.nan})
        else:
            r.update({f"{side}_lot": v / (p * 100.0), f"{side}_val": v, f"{side}_avg": p, f"{side}_rank": 1})
    return r


def _synthetic():
    dates = pd.bdate_range("2026-09-01", periods=20)
    rows = []
    for d in dates:
        rows.append(_row("TST", d, "MM", buy=1.0e8, sell=1.0e8, buy_px=1000.0, sell_px=1005.0))
        rows.append(_row("TST", d, "AA", buy=1.0e8, sell=2.0e7))
        rows.append(_row("TST", d, "XL", buy=1.0e7, sell=1.5e8))
        rows.append(_row("TST", d, "S9", sell=1.0e8))
    raw = pd.DataFrame(rows)
    close = 1000.0 * 1.01 ** np.arange(20)
    px = pd.DataFrame({"ticker": "TST", "date": dates, "open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1.0e9 / close})
    px["value"] = px["close"] * px["volume"]          # 1e9 per day
    return raw, px


def test_mm_identification_and_refs():
    raw, px = _synthetic()
    prof = mm_ceiling.flag_mm(mm_ceiling.broker_window_profile(raw, px))
    last = prof[prof["date"] == prof["date"].max()].set_index("broker")
    assert bool(last.loc["MM", "mm_flag"]), last.loc["MM"].to_dict()
    assert not bool(last.loc["AA", "mm_flag"])                      # one-way accumulator: net_ratio too high
    assert not bool(last.loc["XL", "mm_flag"])                      # large two-sided but net seller: net_ratio too high
    assert not bool(last.loc["S9", "mm_flag"])                      # never on both sides
    assert abs(last.loc["MM", "gross_share"] - 0.10) < 1e-6, last.loc["MM", "gross_share"]
    assert abs(last.loc["MM", "buy_vwap"] - 1000.0) < 1e-6 and abs(last.loc["MM", "sell_vwap"] - 1005.0) < 1e-6
    assert abs(last.loc["MM", "spread"] - 0.005) < 1e-9
    assert last.loc["XL", "net_ratio"] > 0.5 and last.loc["AA", "net_ratio"] > 0.5
    print("ok identification: MM flagged, AA/XL/S9 not; gross_share=%.3f spread=%.4f XL net_ratio=%.2f" % (
        last.loc["MM", "gross_share"], last.loc["MM", "spread"], last.loc["XL", "net_ratio"]))


def test_ceiling_table_and_retail_exit():
    raw, px = _synthetic()
    prof = mm_ceiling.flag_mm(mm_ceiling.broker_window_profile(raw, px))
    ct = mm_ceiling.ceiling_table(prof, px).set_index("date").sort_index()
    d = ct.index[10]                                                # a mid date with 5 days ahead
    row = ct.loc[d]
    assert row["mm_broker"] == "MM"
    i = list(px["date"]).index(d)
    expected_maxhigh5 = px["high"].iloc[i + 1: i + 6].max()
    assert abs(row["maxhigh_5d"] - expected_maxhigh5) < 1e-9
    assert abs(row["up_vs_mm_sell_5d"] - (expected_maxhigh5 / 1005.0 - 1)) < 1e-9
    assert bool(row["ceiling_hit_5d"]) is True                      # price kept rising: it passed the MM's average sell
    assert row["retail_net_share"] < -0.1, row["retail_net_share"]  # XL nets -1.4e8/day on 1e9 value -> strongly negative over the window
    assert abs(row["close_vs_mm_sell"] - (px["close"].iloc[i] / 1005.0 - 1)) < 1e-9
    print("ok ceiling table: maxhigh_5d, up_vs_mm_sell_5d=%.4f, ceiling_hit, retail_net_share=%.3f" % (row["up_vs_mm_sell_5d"], row["retail_net_share"]))


def test_counterparty():
    dates = pd.bdate_range("2026-09-01", periods=20)
    rows = []
    for i, d in enumerate(dates):
        up = i % 2 == 0
        rows.append(_row("CPT", d, "XL", buy=1.5e8 if up else 5e7, sell=5e7 if up else 1.5e8))          # retail alternates net buy / net sell (+-1e8)
        rows.append(_row("CPT", d, "CPX", buy=5e7 if up else 1.5e8, sell=1.5e8 if up else 5e7, sell_px=1010.0))   # takes the other side
        rows.append(_row("CPT", d, "AA", buy=1.0e8, sell=2.0e7))                                           # one-way accumulator, unrelated to retail
    raw = pd.DataFrame(rows)
    close = np.full(20, 1000.0)
    px = pd.DataFrame({"ticker": "CPT", "date": dates, "open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1.0e9 / close})
    px["value"] = px["close"] * px["volume"]
    prof = mm_ceiling.flag_mm(mm_ceiling.broker_window_profile(raw, px))
    last = prof[prof["date"] == prof["date"].max()].set_index("broker")
    assert last.loc["CPX", "cp_corr"] < -0.9, last.loc["CPX", "cp_corr"]
    assert bool(last.loc["CPX", "cp_flag"]) and not bool(last.loc["XL", "cp_flag"])              # retail class is never its own counterparty
    assert not bool(last.loc["AA", "cp_flag"]), last.loc["AA", "cp_corr"]                          # constant net: no co-movement with retail
    ct = mm_ceiling.ceiling_table(prof, px, mode="counterparty")
    assert ct["mm_broker"].iloc[-1] == "CPX" and ct["ref_mode"].iloc[-1] == "counterparty"
    print("ok counterparty: CPX cp_corr=%.2f flagged, XL/AA not" % last.loc["CPX", "cp_corr"])


def _placebo_frames(ref_mult, others_mult, high_fn, n_tickers=8, n_days=40, seed=3):
    rng = np.random.default_rng(seed)
    rp, rc, rx = [], [], []
    for d in pd.bdate_range("2026-09-01", periods=n_days):
        for k in range(n_tickers):
            tk, close = f"T{k}", 1000.0
            rx.append({"ticker": tk, "date": d, "open": close, "high": high_fn(rng, close), "low": close * 0.99, "close": close, "volume": 1e6, "value": close * 1e6})
            rp.append({"ticker": tk, "date": d, "broker": "REF", "gross_share": 0.08, "sell_vwap": close * ref_mult(rng)})
            for j, m in enumerate(others_mult):
                rp.append({"ticker": tk, "date": d, "broker": f"O{j}", "gross_share": 0.08, "sell_vwap": close * m(rng)})
            rc.append({"ticker": tk, "date": d, "mm_broker": "REF"})
    return pd.DataFrame(rp), pd.DataFrame(rc), pd.DataFrame(rx)


def test_placebo_other_brokers():
    # the REFERENCE broker's sell VWAP sits 5% above the close and is never reached (max high = +3%); two other sizeable brokers sit at +1% / +2% and ARE reached.
    prof, ct, px = _placebo_frames(lambda r: 1.05, [lambda r: 1.01, lambda r: 1.02], lambda r, c: c * 1.03)
    pc = mm_ceiling.placebo_other_brokers(prof, ct, px, horizon=1, B=100)
    row = pc[pc["bucket"] == "ALL"].iloc[0]
    assert row["ref_hit"] == 0.0 and row["random_candidate_hit"] > 0.4, row.to_dict()
    assert row["p_ref_lower"] < 0.05, row["p_ref_lower"]
    print("ok placebo: reference hit=%.2f vs random sizeable broker=%.2f (p_ref_lower=%.3f)" % (row["ref_hit"], row["random_candidate_hit"], row["p_ref_lower"]))


def test_placebo_no_effect():
    # the reference broker is just another broker: references are drawn from the same distribution -> no difference, no false ceiling
    prof, ct, px = _placebo_frames(lambda r: 1 + r.uniform(0.005, 0.06), [lambda r: 1 + r.uniform(0.005, 0.06)] * 2,
                                   lambda r, c: c * (1 + r.uniform(0.0, 0.06)), n_tickers=10)
    pc = mm_ceiling.placebo_other_brokers(prof, ct, px, horizon=1, B=100)
    row = pc[pc["bucket"] == "ALL"].iloc[0]
    assert abs(row["diff"]) < 0.08 and row["p_ref_lower"] > 0.05, row.to_dict()
    print("ok placebo null: diff=%.3f p_ref_lower=%.2f (no false ceiling when the reference is just another broker)" % (row["diff"], row["p_ref_lower"]))


if __name__ == "__main__":
    test_placebo_other_brokers()
    test_placebo_no_effect()
    test_mm_identification_and_refs()
    test_counterparty()
    test_ceiling_table_and_retail_exit()
    print("ALL OK")
