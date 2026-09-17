#!/usr/bin/env python3
"""
backfill_buy_on_weakness_picks.py

MBSS v2 (user request 2026-09-16): BUY ON WEAKNESS is a brand new lane —
without a backfill, its first real nightly run would only "see" whatever
fires on that one day, silently missing any setup that actually started
a few days earlier (this lane's alerts are meant to stay ALIVE and track
age for up to 5 trading days). This script reconstructs pick history from
BACKFILL_START forward, so day 1 in production already has correctly-aged
alerts instead of starting cold.

Cara kerja -- REUSE fungsi produksi asli (compute_buy_on_weakness_candidates
+ update_and_save_picks dari engine/buy_on_weakness.py), BUKAN reimplementasi
formula, mengikuti pola PERSIS backfill_lane_pick_history.py:
  1. Untuk tiap hari bursa dalam window backfill, monkey-patch
     core.get_ohlcv_daily_from_db supaya HANYA mengembalikan bar SAMPAI
     hari itu (no-lookahead genuine).
  2. Panggil compute_buy_on_weakness_candidates() + update_and_save_picks()
     ASLI untuk hari itu -- age/status/TP1-touch/TP2/SL berkembang persis
     seperti kalau nightly job benar-benar jalan tiap malam sejak
     BACKFILL_START.
  3. Restore get_ohlcv_daily_from_db asli setelah selesai.

Jalankan SEKALI di server (python backfill_buy_on_weakness_picks.py) --
buy_on_weakness_picks.json gitignored, per-deployment, sama seperti
daytrade_picks_history.json.

NOTE: unlike backfill_lane_pick_history.py, this does NOT need to no-op
core.record_direct_evidence_blacklist/record_fetch_result -- those are
side effects of get_ohlcv_smart's staleness detection specifically, and
this lane deliberately uses the plain core.get_ohlcv_daily_from_db reader
(no staleness/blacklist logic in it) precisely to avoid that landmine.
"""
from __future__ import annotations

import sys

import pandas as pd

sys.path.insert(0, ".")
from engine import legacy_core as core  # noqa: E402
import engine.buy_on_weakness as buy_on_weakness_engine  # noqa: E402

BACKFILL_START = "2026-09-11"


def _make_truncated_fetcher(real_fetch, as_of_date):
    def _fetcher(ticker, limit=250):
        df = real_fetch(ticker, limit=limit + 40)  # extra buffer before truncating
        if df is None or df.empty:
            return df
        idx_dates = pd.to_datetime(df.index).date
        mask = idx_dates <= as_of_date
        return df[mask].tail(limit)
    return _fetcher


def _trading_dates_from_db(min_date: str) -> list:
    """Distinct trading dates across the WHOLE ohlcv_daily table, not one
    sample ticker. BUGFIX (2026-09-17, live incident): the original version
    used a single hardcoded ticker (BBCA) to read the available date range
    -- turned out BBCA itself had stale data (stuck at 2026-08-21, a
    pre-existing eodscan "already up to date" staleness-detection bug
    unrelated to this script) even though the DB as a whole was current
    to 2026-09-16, so the backfill wrongly reported "no trading days
    available." Querying the date column directly is robust to any single
    ticker's staleness."""
    import sqlite3
    conn = sqlite3.connect(core.OHLCV_DB_FILE)
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM ohlcv_daily WHERE date >= ? ORDER BY date", (min_date,)
        ).fetchall()
    finally:
        conn.close()
    return [pd.Timestamp(r[0]).date() for r in rows]


def main():
    with open(core.WHITELIST_CACHE_FILE) as f:
        import json
        universe = json.load(f).get("eligible_tickers", [])
    print(f"Universe: {len(universe)} ticker (ticker_whitelist.json)")

    real_get_ohlcv_daily_from_db = core.get_ohlcv_daily_from_db
    trading_dates = _trading_dates_from_db(BACKFILL_START)
    if not trading_dates:
        raise SystemExit(f"Tidak ada baris di ohlcv_daily >= {BACKFILL_START} -- cek mbss_ohlcv.db sudah ter-update (lihat diagnose_ohlcv_db.py).")

    backfill_start_date = pd.Timestamp(BACKFILL_START).date()
    backfill_dates = [d for d in trading_dates if d >= backfill_start_date]
    if not backfill_dates:
        raise SystemExit(f"Tidak ada hari bursa >= {BACKFILL_START} di mbss_ohlcv.db -- cek data sudah ter-update.")
    print(f"Backfill window: {backfill_dates[0]} .. {backfill_dates[-1]} ({len(backfill_dates)} hari bursa)")

    for as_of in backfill_dates:
        core.get_ohlcv_daily_from_db = _make_truncated_fetcher(real_get_ohlcv_daily_from_db, as_of)
        candidates = buy_on_weakness_engine.compute_buy_on_weakness_candidates(universe)
        history = buy_on_weakness_engine.update_and_save_picks(candidates)
        print(f"  {as_of}: {len(candidates)} kandidat baru hari ini, {len(history)} total di history")

    core.get_ohlcv_daily_from_db = real_get_ohlcv_daily_from_db  # WAJIB restore

    final_history = buy_on_weakness_engine.load_buy_on_weakness_picks()
    by_status = {}
    for p in final_history:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    print(f"\n✅ Backfill selesai: {len(final_history)} pick di {buy_on_weakness_engine.PICKS_FILE}")
    print(f"   Status breakdown: {by_status}")


if __name__ == "__main__":
    main()
