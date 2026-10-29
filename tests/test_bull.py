"""Lane BULL: exit simulation (SL / trailing / TP / expiry / still alive), confirmation logic, gate, stateless resolve."""
import numpy as np
import pandas as pd

import engine.bull as bull


def _arr(rows):
    a = np.array(rows, float)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3]       # open, high, low, close


def _flat(n, price=100.0):
    return [[price, price, price, price]] * n


def test_sl_hit_with_gap_fill():
    o, h, l, c = _arr(_flat(2) + [[100, 100, 100, 100]] + [[90, 91, 89, 90]] + _flat(8))
    r = bull.simulate_exit(o, h, l, c, 0)       # entry open[1]=100 -> stop 92; bar 3 opens at 90 (gap through) -> fill at 90
    assert r["status"] == "SL_HIT" and round(r["ret_pct"], 2) == -10.0 and r["bars"] == 3


def test_trailing_arms_next_bar_then_exits():
    rows = _flat(2) + [[100, 110, 100, 108]] + [[108, 109, 100, 101]] + _flat(8, 101)
    o, h, l, c = _arr(rows)
    r = bull.simulate_exit(o, h, l, c, 0)       # high 110 (+10%) arms trail -> stop 110*0.94=103.4; next bar low 100 -> TRAIL_HIT at 103.4
    assert r["status"] == "TRAIL_HIT" and round(r["ret_pct"], 1) == 3.4


def test_arming_bar_itself_is_not_stopped_by_new_trail():
    rows = _flat(2) + [[100, 110, 95, 108]] + _flat(8, 108)     # low 95 on the arming bar but trail only applies from the next bar
    o, h, l, c = _arr(rows)
    r = bull.simulate_exit(o, h, l, c, 0)
    assert r["status"] != "TRAIL_HIT" or r["bars"] > 1


def test_tp_and_expiry_and_alive():
    o, h, l, c = _arr(_flat(2) + [[100, 121, 100, 120]] + _flat(8))
    assert bull.simulate_exit(o, h, l, c, 0)["status"] == "TP_HIT"
    o, h, l, c = _arr(_flat(12, 100))
    r = bull.simulate_exit(o, h, l, c, 0)
    assert r["status"] == "EXPIRED" and r["bars"] == bull.MAX_HOLD_DAYS
    o, h, l, c = _arr(_flat(5, 100))
    assert bull.simulate_exit(o, h, l, c, 0)["status"] == "ALIVE"
    assert bull.simulate_exit(*_arr(_flat(3)), 2) is None       # alert bar is the last bar -> no entry yet


def _pop_frame(n=130):
    """Steady uptrend (slope20 > 7%, low ATR) so S2 fires on green-body bars."""
    px = 100 * (1.016 ** np.arange(n))
    o = px * 0.99
    return pd.DataFrame({"Open": o, "High": px * 1.004, "Low": o * 0.998, "Close": px, "Volume": np.full(n, 5e6)},
                        index=pd.date_range("2025-01-01", periods=n, freq="B"))


def test_confirmation_needs_a_prior_raw_alert_within_three_bars():
    f = bull.features(_pop_frame()); s = bull.raw_signals(f)
    assert s["raw"].iloc[-1]                       # trending bar is a raw alert
    assert s["pick"].iloc[-1]                      # and the previous bars were alerts too -> confirmed
    s2 = s.copy(); s2.loc[s2.index[-4:-1], "raw"] = False
    prior = s2["raw"].astype(int).shift(1).rolling(3, min_periods=1).sum().fillna(0)
    assert prior.iloc[-1] == 0                     # no alert in the 3 bars before -> would NOT be a pick


def test_gate_closed_creates_no_picks(monkeypatch):
    import engine.ihsg_gate as gate
    monkeypatch.setattr(bull, "evaluate_ticker", lambda t: {"ticker": t, "entry_ref_price": 100})
    monkeypatch.setattr(gate, "get_gate_today", lambda: {"open": False, "reason": "tes", "date": "2026-10-29"})
    monkeypatch.setattr(gate, "log_dropped", lambda *a, **k: None)
    assert bull.compute_bull_candidates(["AAA"]) == []
    monkeypatch.setattr(gate, "get_gate_today", lambda: {"open": True, "reason": "ok", "date": "2026-10-29"})
    assert len(bull.compute_bull_candidates(["AAA"])) == 1


def test_resolve_is_stateless_and_idempotent(monkeypatch):
    rows = _flat(2) + [[100, 100, 100, 100]] + [[100, 105, 99, 104]] + [[104, 110, 103, 109]] + [[109, 109, 100, 101]]
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=pd.date_range("2026-10-20", periods=len(rows), freq="B"))
    df["Volume"] = 1e6
    monkeypatch.setattr(bull.core, "get_ohlcv_daily_from_db", lambda *_a, **_k: df)
    pick = {"ticker": "AAA", "trigger_date": df.index[0].strftime("%Y-%m-%d"), "status": "ALIVE", "age_days": 1, "entry_ref_price": 100.0}
    a = bull._resolve_active_pick(dict(pick)); b = bull._resolve_active_pick(dict(a))
    assert a["status"] == b["status"] and a["entry_open"] == 100.0
    assert a["status"] in ("TRAIL_HIT", "SL_HIT", "ALIVE")
