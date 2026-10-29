"""OSB-v2 SL -8% + break-even stop, BOW SL -12%; legacy stored sl_price honoured."""
import pandas as pd

import engine.oversold_bounce_v2 as osb
import engine.buy_on_weakness as bow


def _df(low, high, close, date="2026-10-29"):
    return pd.DataFrame({"Low": [low], "High": [high], "Close": [close]}, index=pd.to_datetime([date]))


def _run(monkeypatch, pick, df):
    monkeypatch.setattr(osb.core, "get_ohlcv_daily_from_db", lambda *_a, **_k: df)
    return osb._resolve_active_pick(pick)


def _pick(**kw):
    return {"ticker": "AAA", "entry_ref_price": 1000.0, "age_days": 1, "sl_price": 920, **kw}


def test_constants():
    assert osb.SL_PCT == -8.0 and osb.ALERT_MAX_AGE_DAYS == 8 and bow.SL_PCT == 12.0


def test_sl_hit_without_be(monkeypatch):
    p = _run(monkeypatch, _pick(), _df(910, 1000, 950))
    assert p["status"] == "SL_HIT" and p["resolved_ret_pct"] == -8.0


def test_be_arms_then_stops_at_entry_plus_cost(monkeypatch):
    p = _run(monkeypatch, _pick(), _df(990, 1060, 1040, "2026-10-28"))
    assert p["status"] == "ALIVE" if "status" in p else True
    assert p["be_armed"] is True and p["age_days"] == 2
    p = _run(monkeypatch, p, _df(995, 1030, 1000, "2026-10-29"))   # dips below entry+0.2%
    assert p["status"] == "BE_HIT" and p["resolved_ret_pct"] == 0.2


def test_be_not_applied_same_bar(monkeypatch):
    # touching +5% and dropping below entry on the SAME bar is not a BE exit (matches the backtest)
    p = _run(monkeypatch, _pick(), _df(985, 1060, 1000))
    assert p.get("status") != "BE_HIT" and p["be_armed"]


def test_legacy_stored_sl_kept(monkeypatch):
    p = _run(monkeypatch, _pick(sl_price=800), _df(900, 950, 920))
    assert p.get("status") != "SL_HIT"


def test_expires_after_8_days(monkeypatch):
    p = _run(monkeypatch, _pick(age_days=8), _df(990, 1010, 1005))
    assert p["status"] == "EXPIRED"
