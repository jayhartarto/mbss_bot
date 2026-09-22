"""
Fetch per-ticker daily net foreign investor flow from IDX's internal
GetStockSummary JSON API and store it locally for later merge into the
MBSS v2 scoring pipeline as a supplementary flow indicator.

Data source: https://www.idx.co.id/primary/TradingSummary/GetStockSummary
One API call returns ALL tickers for a single trading day (foreignBuy,
foreignSell, plus full OHLCV) -- so we iterate day-by-day over the same
date range as research/ohlcv_backtest_raw.csv, not ticker-by-ticker.

Output: research/foreign_flow_2y.sqlite
  table foreign_flow_daily(ticker TEXT, date TEXT, foreign_buy REAL,
                            foreign_sell REAL, foreign_net REAL,
                            PRIMARY KEY(ticker, date))

Resumable: on startup, reads which dates are already fully recorded in the
sqlite DB (via a small `fetch_log` table) and skips them. Writes are
committed after every trading day so an interrupted run loses at most the
day in progress.

Usage:
    python research/fetch_foreign_flow.py
"""

import csv
import datetime as dt
import sqlite3
import sys
import time
from pathlib import Path

try:
    from curl_cffi import requests as creq
    BACKEND = "curl_cffi"
except ImportError:
    import requests as creq  # type: ignore
    BACKEND = "requests"

BASE_DIR = Path(__file__).resolve().parent
OHLCV_CSV = BASE_DIR / "ohlcv_backtest_raw.csv"
DB_PATH = BASE_DIR / "foreign_flow_2y.sqlite"

IDX_HOME = "https://www.idx.co.id/"
IDX_API = "https://www.idx.co.id/primary/TradingSummary/GetStockSummary"

HEADERS = {
    "Referer": "https://www.idx.co.id/en/market-data/trading-summary/stock-summary",
    "Accept": "application/json, text/plain, */*",
}

REQUEST_DELAY_SEC = 0.35  # starting/floor delay between requests
MAX_DELAY_SEC = 5.0       # ceiling the adaptive throttle will back off to
MAX_RETRIES = 4
BACKOFF_BASE_SEC = 2.0


class AdaptiveThrottle:
    """Speeds up toward REQUEST_DELAY_SEC on clean runs, backs off sharply
    the moment we see a retry-worthy response (429/5xx/timeout), so we go
    as fast as possible without tripping a hard block."""

    def __init__(self, floor=REQUEST_DELAY_SEC, ceiling=MAX_DELAY_SEC):
        self.floor = floor
        self.ceiling = ceiling
        self.delay = floor
        self.clean_streak = 0

    def note_success(self):
        self.clean_streak += 1
        if self.clean_streak >= 10 and self.delay > self.floor:
            self.delay = max(self.floor, self.delay * 0.7)
            self.clean_streak = 0

    def note_retry(self):
        self.clean_streak = 0
        self.delay = min(self.ceiling, max(self.delay * 1.8, self.floor * 2))

    def sleep(self):
        time.sleep(self.delay)


def get_date_range_from_ohlcv():
    """Read min/max date from the existing OHLCV dataset."""
    min_d = None
    max_d = None
    with open(OHLCV_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row["date"]
            if min_d is None or d < min_d:
                min_d = d
            if max_d is None or d > max_d:
                max_d = d
    return min_d, max_d


def daterange(start_date, end_date):
    cur = start_date
    one_day = dt.timedelta(days=1)
    while cur <= end_date:
        yield cur
        cur += one_day


def init_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS foreign_flow_daily (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            foreign_buy REAL,
            foreign_sell REAL,
            foreign_net REAL,
            PRIMARY KEY (ticker, date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fetch_log (
            date TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            ticker_count INTEGER,
            fetched_at TEXT
        )
        """
    )
    conn.commit()


def already_fetched_dates(conn):
    rows = conn.execute("SELECT date FROM fetch_log WHERE status IN ('ok','empty')").fetchall()
    return {r[0] for r in rows}


def make_session():
    if BACKEND == "curl_cffi":
        s = creq.Session(impersonate="chrome")
    else:
        s = creq.Session()
        s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            }
        )
    # Warm up: visit homepage to acquire session cookies.
    s.get(IDX_HOME, timeout=20)
    return s


def fetch_day(session, date_str_yyyymmdd, throttle):
    """Fetch one trading day. Returns list of row dicts (may be empty)."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(
                IDX_API,
                params={"date": date_str_yyyymmdd, "start": 0, "length": 9999},
                headers=HEADERS,
                timeout=30,
            )
            if resp.status_code == 200:
                payload = resp.json()
                throttle.note_success()
                return payload.get("data", [])
            elif resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {resp.status_code}"
                throttle.note_retry()
            else:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                break
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            throttle.note_retry()
        sleep_s = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
        time.sleep(sleep_s)
    raise RuntimeError(f"failed after {MAX_RETRIES} attempts: {last_err}")


def main():
    min_d_str, max_d_str = get_date_range_from_ohlcv()
    start_date = dt.datetime.strptime(min_d_str, "%Y-%m-%d").date()
    # extend to today (or max available) to capture most recent data too
    today = dt.date.today()
    csv_max_date = dt.datetime.strptime(max_d_str, "%Y-%m-%d").date()
    end_date = max(csv_max_date, today)

    print(f"[foreign_flow] backend={BACKEND}")
    print(f"[foreign_flow] OHLCV date range: {min_d_str} .. {max_d_str}")
    print(f"[foreign_flow] fetch range (extended to today): {start_date} .. {end_date}")

    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)
    done_dates = already_fetched_dates(conn)
    print(f"[foreign_flow] {len(done_dates)} dates already recorded, resuming")

    session = make_session()

    all_dates = list(daterange(start_date, end_date))
    todo = [d for d in all_dates if d.isoformat() not in done_dates]
    print(f"[foreign_flow] {len(todo)} / {len(all_dates)} dates to fetch")

    t_start = time.time()
    n_fetched = 0
    n_consecutive_failures = 0
    total_rows_written = 0
    failed_dates = []
    throttle = AdaptiveThrottle()

    for i, d in enumerate(todo, 1):
        date_iso = d.isoformat()
        date_api = d.strftime("%Y%m%d")
        try:
            rows = fetch_day(session, date_api, throttle)
        except Exception as e:  # noqa: BLE001
            print(f"[foreign_flow] FAIL {date_iso}: {e} (delay now {throttle.delay:.2f}s)")
            failed_dates.append(date_iso)
            n_consecutive_failures += 1
            conn.execute(
                "INSERT OR REPLACE INTO fetch_log(date, status, ticker_count, fetched_at) VALUES (?,?,?,?)",
                (date_iso, "error", 0, dt.datetime.now().isoformat()),
            )
            conn.commit()
            if n_consecutive_failures >= 15:
                print(
                    f"[foreign_flow] {n_consecutive_failures} consecutive failures -- "
                    "stopping (possible hard block). Investigate before re-running."
                )
                break
            throttle.sleep()
            continue

        n_consecutive_failures = 0

        if not rows:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_log(date, status, ticker_count, fetched_at) VALUES (?,?,?,?)",
                (date_iso, "empty", 0, dt.datetime.now().isoformat()),
            )
            conn.commit()
        else:
            recs = []
            for r in rows:
                ticker = r.get("StockCode")
                if not ticker:
                    continue
                fbuy = r.get("ForeignBuy")
                fsell = r.get("ForeignSell")
                fnet = None
                if fbuy is not None and fsell is not None:
                    fnet = fbuy - fsell
                recs.append((ticker, date_iso, fbuy, fsell, fnet))
            conn.executemany(
                """
                INSERT OR REPLACE INTO foreign_flow_daily
                    (ticker, date, foreign_buy, foreign_sell, foreign_net)
                VALUES (?, ?, ?, ?, ?)
                """,
                recs,
            )
            conn.execute(
                "INSERT OR REPLACE INTO fetch_log(date, status, ticker_count, fetched_at) VALUES (?,?,?,?)",
                (date_iso, "ok", len(recs), dt.datetime.now().isoformat()),
            )
            conn.commit()
            total_rows_written += len(recs)

        n_fetched += 1
        if n_fetched % 20 == 0 or i == len(todo):
            elapsed = time.time() - t_start
            print(
                f"[foreign_flow] progress: {n_fetched}/{len(todo)} days fetched "
                f"(last={date_iso}, rows_last={len(rows)}, "
                f"total_rows={total_rows_written}, elapsed={elapsed:.0f}s, "
                f"delay={throttle.delay:.2f}s)"
            )

        throttle.sleep()

    conn.close()

    print("[foreign_flow] DONE")
    print(f"[foreign_flow] days processed this run: {n_fetched}/{len(todo)}")
    print(f"[foreign_flow] rows written this run: {total_rows_written}")
    if failed_dates:
        print(f"[foreign_flow] {len(failed_dates)} failed dates: {failed_dates[:30]}{'...' if len(failed_dates) > 30 else ''}")


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
