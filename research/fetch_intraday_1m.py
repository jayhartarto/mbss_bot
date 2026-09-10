"""
Fetch 1-minute intraday OHLCV bars for the MBSS universe and accumulate them
into a durable local SQLite database, working around Yahoo Finance's hard
~30-day rolling retention window for 1m-interval data.

Yahoo Finance does NOT let you backfill 1m bars older than ~30 days -- there
is no historical-archive endpoint for intraday data the way there is for
foreign flow (research/fetch_foreign_flow.py) or daily OHLCV. The only way
to build a genuinely long 1m history is to run this script regularly (once
per trading day, after market close) and let it accumulate forward over
time -- each day it runs adds one more permanent day of history that is no
longer subject to Yahoo's rolling window.

This session's intraday research (BSJP early-breakout screening, REBOUND's
live delay-aware mechanism, the ping-pong range-trade screen) was
consistently constrained to n<50 by the local DB only covering
2026-08-10 -> 2026-09-08 (~20 usable trading days). This script exists to
grow that sample size going forward.

Output: research/mbss_1m_20260905.sqlite (SAME file the existing scratchpad
research scripts already query -- kept as-is despite the stale-looking
filename, so nothing downstream needs to change path).
  table ohlcv_1m(ticker TEXT, ts_utc TEXT, open REAL, high REAL, low REAL,
                 close REAL, volume INTEGER, PRIMARY KEY(ticker, ts_utc))

Idempotent: INSERT OR REPLACE keyed on (ticker, ts_utc) -- safe to re-run,
overlapping days just get overwritten with the same values.

Universe: reuses research/ohlcv_backtest_raw.csv's ticker set (~645
tickers, the same universe this session's daily-bar research already
uses) rather than inventing a different one.

Usage:
    python research/fetch_intraday_1m.py

Design note (fetch window): yfinance's 1m interval caps a single request's
lookback at ~7-8 calendar days regardless of the `period` requested beyond
that limit -- period='8d' is used here (not '30d') to stay within a range
Yahoo actually honors in one shot; daily re-runs (see the JobQueue wiring
in engine/legacy_core.py) keep the rolling accumulation continuous, so the
8-day window is never a real coverage gap between runs.
"""

import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("yfinance tidak terinstall -- pip install yfinance")
    sys.exit(1)

DB_PATH = Path(__file__).parent / "mbss_1m_20260905.sqlite"
UNIVERSE_CSV = Path(__file__).parent / "ohlcv_backtest_raw.csv"
FETCH_PERIOD = "8d"      # aman utk 1m interval (Yahoo cap ~7-8 hari per request)
CHUNK_SIZE = 60          # ticker per batch yf.download -- hindari 1 request raksasa
CHUNK_PAUSE_SEC = 2.0    # jeda antar-batch, jaga2 rate-limit Yahoo (lihat memory
                          # feedback_bot_freeze_duplicate_process_diagnosis.md dkk)


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv_1m (
            ticker TEXT NOT NULL,
            ts_utc TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY (ticker, ts_utc)
        )
    """)
    conn.commit()


def get_universe() -> list[str]:
    df = pd.read_csv(UNIVERSE_CSV, usecols=["ticker"])
    return sorted(df["ticker"].unique().tolist())


def fetch_and_store(conn: sqlite3.Connection, tickers: list[str]) -> tuple[int, int]:
    """Fetch one chunk, upsert into DB. Returns (rows_written, tickers_ok)."""
    syms = [t + ".JK" for t in tickers]
    try:
        data = yf.download(syms, period=FETCH_PERIOD, interval="1m",
                            group_by="ticker", threads=True, progress=False)
    except Exception as e:
        print(f"⚠️ Chunk gagal fetch ({len(tickers)} ticker): {e}")
        return 0, 0

    if data is None or data.empty:
        return 0, 0

    rows = []
    ok = 0
    for t, sym in zip(tickers, syms):
        try:
            df = data[sym].dropna(how="all")
        except Exception:
            continue
        if df.empty:
            continue
        ok += 1
        for ts, bar in df.iterrows():
            if pd.isna(bar.get("Close")):
                continue
            ts_utc = ts.tz_convert("UTC").isoformat()
            rows.append((
                t, ts_utc,
                float(bar["Open"]) if pd.notna(bar.get("Open")) else None,
                float(bar["High"]) if pd.notna(bar.get("High")) else None,
                float(bar["Low"]) if pd.notna(bar.get("Low")) else None,
                float(bar["Close"]),
                int(bar["Volume"]) if pd.notna(bar.get("Volume")) else None,
            ))

    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO ohlcv_1m(ticker, ts_utc, open, high, low, close, volume) "
            "VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return len(rows), ok


def run_daily_1m_accumulation() -> dict:
    """
    Entry point dipanggil job JobQueue (lihat engine/legacy_core.py) ATAU
    manual (`python research/fetch_intraday_1m.py`). Return summary dict --
    caller (job wrapper) yg tanggung jawab log/notifikasi, fungsi ini
    sendiri print progress ke stdout spt script CLI biasa.
    """
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    cur = conn.execute("SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc), COUNT(DISTINCT ticker) FROM ohlcv_1m")
    before_count, before_min, before_max, before_tickers = cur.fetchone()
    print(f"📊 Sebelum: {before_count:,} baris, {before_tickers} ticker, {before_min} s/d {before_max}")

    universe = get_universe()
    print(f"🌐 Universe: {len(universe)} ticker, fetch period={FETCH_PERIOD}")

    total_rows, total_ok, total_fail = 0, 0, 0
    for i in range(0, len(universe), CHUNK_SIZE):
        chunk = universe[i:i + CHUNK_SIZE]
        rows, ok = fetch_and_store(conn, chunk)
        total_rows += rows
        total_ok += ok
        total_fail += (len(chunk) - ok)
        print(f"  [{i + len(chunk)}/{len(universe)}] chunk: {rows} baris baru/update, {ok}/{len(chunk)} ticker ada data")
        if i + CHUNK_SIZE < len(universe):
            time.sleep(CHUNK_PAUSE_SEC)

    cur = conn.execute("SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc), COUNT(DISTINCT ticker) FROM ohlcv_1m")
    after_count, after_min, after_max, after_tickers = cur.fetchone()
    conn.close()

    summary = {
        "before_count": before_count, "before_range": (before_min, before_max),
        "after_count": after_count, "after_range": (after_min, after_max),
        "after_tickers": after_tickers, "rows_written_this_run": total_rows,
        "tickers_with_data": total_ok, "tickers_failed": total_fail,
    }
    print(f"📊 Sesudah: {after_count:,} baris (+{after_count - before_count:,}), "
          f"{after_tickers} ticker, {after_min} s/d {after_max}")
    return summary


if __name__ == "__main__":
    run_daily_1m_accumulation()
