"""Hybrid IHSG gate: long gate, rebound gate, closed, fail-open."""
import numpy as np
import pandas as pd

import engine.ihsg_gate as gate


def _s(vals):
    return pd.Series(vals, dtype=float)


def test_long_gate_open_in_uptrend():
    g = gate.evaluate(_s(np.linspace(6000, 7000, 80)))
    assert g["open"] and "SMA50" in g["reason"]


def test_closed_in_downtrend_without_rebound():
    g = gate.evaluate(_s(np.linspace(7000, 5500, 80)))
    assert not g["open"]


def test_rebound_gate_opens_below_sma50():
    # long slide, then a sharp >4% bounce off the 10d low that clears SMA5
    v = list(np.linspace(8000, 5600, 70)) + [5600] * 4 + [5650, 5700, 5800, 5900, 5950, 5900]
    s = _s(v)
    assert s.iloc[-1] / s.rolling(50).mean().iloc[-1] - 1 < -0.011
    g = gate.evaluate(s)
    assert g["open"] and "rebound" in g["reason"]


def test_fail_open_on_data_error(monkeypatch):
    gate._cache.update(date=None, result=None)
    monkeypatch.setattr(gate.core, "get_yf_ticker", lambda *_: (_ for _ in ()).throw(RuntimeError("x")))
    g = gate.get_gate_today()
    assert g["open"] and g["reason"] == "data_error"
    gate._cache.update(date=None, result=None)


def test_filter_drops_and_logs_when_closed(monkeypatch, tmp_path):
    closed = {"open": False, "reason": "t", "date": "2026-10-29"}
    monkeypatch.setattr(gate, "get_gate_today", lambda: closed)
    monkeypatch.setattr(gate, "DROP_LOG", str(tmp_path / "d.jsonl"))
    gate.log_dropped.__defaults__ = (str(tmp_path / "d.jsonl"),)
    kept, g = gate.filter_picks("BOW", [{"ticker": "AAA", "entry_ref_price": 100}])
    assert kept == [] and not g["open"]
    assert "AAA" in (tmp_path / "d.jsonl").read_text()
