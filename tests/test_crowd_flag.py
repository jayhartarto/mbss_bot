"""Crowd-avoid flag on synthetic IPOT/OHLCV DBs: scalper >=3%, retail_app >=5% hide; otherwise / missing data fail-open."""
import sqlite3

import engine.crowd_flag as cf

DATES = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]


def _dbs(rows):
    ic = sqlite3.connect(":memory:")
    ic.execute("CREATE TABLE ipot_broker_daily (ticker, trade_date, broker_code, buy_lot, sell_lot, fd, board)")
    oc = sqlite3.connect(":memory:")
    oc.execute("CREATE TABLE ohlcv_daily (ticker, date, close, volume)")
    for d in DATES:
        oc.execute("INSERT INTO ohlcv_daily VALUES ('AAA', ?, 100, 1000000)", (d,))   # 10,000 lots/day -> 50,000 over 5d
    for d in DATES:
        for broker, buy, sell in rows:
            ic.execute("INSERT INTO ipot_broker_daily VALUES ('AAA', ?, ?, ?, ?, 'all', 'all')", (d, broker, buy, sell))
    return ic, oc


def test_scalper_hide():
    ic, oc = _dbs([("MG", 400, 0), ("ZZ", 0, 1)])           # +400 lots/day * 5 = 2000 -> vs 50,000 = 4%
    assert cf.compute("AAA", DATES[-1], ic, oc)["hide"] is True


def test_retail_app_hide_and_below_threshold():
    ic, oc = _dbs([("XL", 600, 0), ("ZZ", 0, 1)])           # 3000/50000 = 6% >= 5%
    assert cf.compute("AAA", DATES[-1], ic, oc)["hide"] is True
    ic, oc = _dbs([("XL", 400, 0), ("ZZ", 0, 1)])           # 2000/50000 = 4% < 5%
    assert cf.compute("AAA", DATES[-1], ic, oc)["hide"] is False


def test_missing_data_fail_open():
    ic, oc = _dbs([("MG", 400, 0)])
    assert cf.compute("NOPE", DATES[-1], ic, oc) is None


def test_partition_passthrough_old_picks_and_other_lanes():
    picks = [{"ticker": "AAA", "age_days": 3, "trigger_date": DATES[-1]}]
    assert cf.partition("BOW", picks) == (picks, [])
    assert cf.partition("OSB-v2", picks) == (picks, [])
