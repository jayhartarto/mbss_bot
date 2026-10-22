"""
research/ipot_explore/test_features.py — synthetic checks that the pattern definitions in features.py behave as intended.
Run from the project root:  python -m research.ipot_explore.test_features
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import features


def _row(tk, d, broker, buy_val=None, sell_val=None, price=1000.0):
    r = {"ticker": tk, "date": d, "fd": "all", "board": "all", "broker": broker}
    for side, v in (("buy", buy_val), ("sell", sell_val)):
        if v is None:
            r.update({f"{side}_lot": np.nan, f"{side}_val": np.nan, f"{side}_avg": np.nan, f"{side}_rank": np.nan})
        else:
            r.update({f"{side}_lot": v / (price * 100.0), f"{side}_val": v, f"{side}_avg": price, f"{side}_rank": 1})
    return r


def _synthetic():
    dates = pd.bdate_range("2026-09-01", periods=25)
    rows = []
    for i, d in enumerate(dates):
        rows.append(_row("TST", d, "AA", buy_val=1.0e8, sell_val=2.0e7))                  # steady accumulator: net +8e7 every day
        rows.append(_row("TST", d, "BB", buy_val=3.0e8 if i == len(dates) - 1 else 2.0e7))  # quiet buyer, then a burst on the last day
        rows.append(_row("TST", d, "CC", buy_val=1.5e8, sell_val=1.5e8))                  # churn: both sides equal
        rows.append(_row("TST", d, "S1", sell_val=2.0e8))                                 # a seller
    raw = pd.DataFrame(rows)
    px = pd.DataFrame({"ticker": "TST", "date": dates, "open": 1000.0, "high": 1000.0, "low": 1000.0, "close": 1000.0,
                       "volume": 1.0e6})
    px["value"] = px["close"] * px["volume"]            # 1e9 per day
    return raw, px


def test_net_bounds():
    # 10 sellers (min 1e7) so the sell cutoff is real, one buy-only broker with 5e7 -> net in [4e7, 5e7]
    d = pd.Timestamp("2026-09-01")
    rows = [_row("T2", d, f"S{i}", sell_val=1e7 * (i + 1)) for i in range(10)] + [_row("T2", d, "X", buy_val=5e7)]
    t = features.broker_day_table(pd.DataFrame(rows))
    x = t[t["broker"] == "X"].iloc[0]
    assert abs(x["net_val_lo"] - 4e7) < 1 and abs(x["net_val_hi"] - 5e7) < 1, (x["net_val_lo"], x["net_val_hi"])
    s1 = t[t["broker"] == "S0"].iloc[0]           # sell-only broker: net in [-sell, -sell + cut_buy], buy side has 1 broker -> cut 0
    assert abs(s1["net_val_lo"] + 1e7) < 1 and abs(s1["net_val_hi"] + 1e7) < 1
    two = t[t["is_two"]]
    assert two.empty                                # nobody is on both sides in this mini case
    print("ok net bounds")


def test_patterns():
    raw, px = _synthetic()
    pat = features.rolling_pattern_features(raw, px).set_index("date").sort_index()
    last, prev = pat.iloc[-1], pat.iloc[-2]
    assert last["routine_gross_score"] >= 0.9, last["routine_gross_score"]                            # AA / CC are in the top-3 buyers every day (gross)
    assert last["routine_net_broker"] == "AA" and last["routine_net_score"] >= 0.9                     # only AA is a steady NET buyer (CC nets to zero)
    assert last["dom_pos_days"] >= 0.9, last["dom_pos_days"]
    assert last["dom_broker"] == "AA", last["dom_broker"]                                             # largest cumulative net inventory
    assert abs(last["dom_inventory_share"] - 0.08) < 0.01, last["dom_inventory_share"]               # 8e7 per 1e9
    assert abs(last["dom_vwap"] - 1000.0) < 1e-6 and abs(last["dom_vwap_dist"]) < 1e-9
    assert last["burst_ratio"] > 3.0, last["burst_ratio"]                                             # 0.30 / 0.08 = 3.75 on the burst day
    assert abs(prev["burst_ratio"] - 1.0) < 0.05, prev["burst_ratio"]                                 # no burst the day before
    assert bool(last["quiet_accum"]) is True                                                          # routine + flat price + inventory building
    print("ok patterns: routine_net=%.2f dom=%s inv_share=%.3f burst=%.2f (prev %.2f)" % (
        last["routine_net_score"], last["dom_broker"], last["dom_inventory_share"], last["burst_ratio"], prev["burst_ratio"]))


def test_ticker_day():
    raw, px = _synthetic()
    f = features.ticker_day_features(raw, px).set_index("date").sort_index()
    last = f.iloc[-1]
    assert last["n_two"] == 2                                       # AA and CC are on both sides
    assert last["churn_share"] > 0.2, last["churn_share"]
    assert abs(last["buy_top1"] - 3.0e8 / 5.5e8) < 1e-6, last["buy_top1"]
    assert abs(last["top_net_buyer_share"] - 0.30) < 1e-6          # BB net +3e8 on 1e9 traded value
    assert abs(last["buy_top1_avg_vs_close"]) < 1e-9
    print("ok ticker-day: churn=%.2f top1=%.2f top_net_buyer_share=%.2f" % (last["churn_share"], last["buy_top1"], last["top_net_buyer_share"]))


if __name__ == "__main__":
    test_net_bounds()
    test_patterns()
    test_ticker_day()
    print("ALL OK")
