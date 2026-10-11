"""
update_foreign_flow_via_zapi_2026_10_13.py — VPS-native foreign-flow updater.

Replaces the laptop-only workflow for KEEPING foreign_flow_2y.sqlite
CURRENT (NOT for the original 2-year backfill, which stays as-is --
see caveat below). The old workflow: research/fetch_foreign_flow.py
scrapes idx.co.id directly from a residential IP (Cloudflare blocks the
VPS's datacenter IP for direct scraping), then push_foreign_flow_history.py
commits+pushes the whole binary sqlite file to git so the VPS can `git
pull` it before /eodscan. Confirmed 2026-10-11/13: Zapi's `finance:idx/
foreign-flow` endpoint (a 3rd-party API gateway, not direct idx.co.id
scraping) works live from the VPS with no block -- so the VPS can now
update its OWN local foreign_flow_2y.sqlite directly, no laptop/git-push
round-trip needed, for RECENT days.

CAVEAT -- Zapi free-tier history is short (~7 days per Zapi's own
pricing page). This script is for closing a SMALL recent gap (the
normal case: 1-3 missed days), not a deep backfill. If the gap is
larger than Zapi can serve, those older missing days will come back
"empty" from the API and get logged as such -- they will NOT silently
get the old laptop-sourced data overwritten with wrong data, but they
also won't be filled by this script. Run push_foreign_flow_history.py
(the old laptop path) if you ever need to backfill further back than
Zapi's own retention window.

Same schema/resumability convention as research/fetch_foreign_flow.py
(fetch_log table, status in ok/empty/error, INSERT OR REPLACE on the
(ticker, date) primary key) -- intentionally kept identical so both
paths can coexist without conflict.

Usage (run on the VPS, or anywhere engine.broker's ZAPI_API_KEY is set):
    python update_foreign_flow_via_zapi_2026_10_13.py [max_days_back]

max_days_back (optional, default 10): how many days back from today to
consider "missing and worth trying" -- deliberately small given the
~7-day Zapi retention; raising this just means more wasted quota calls
on days Zapi will return empty for anyway.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys

sys.path.insert(0, ".")
import engine.broker as broker_engine

DB_PATH = "research/foreign_flow_2y.sqlite"
DEFAULT_MAX_DAYS_BACK = 10


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


def already_fetched_dates(conn) -> set[str]:
    rows = conn.execute("SELECT date FROM fetch_log WHERE status IN ('ok', 'empty')").fetchall()
    return {r[0] for r in rows}


def run_foreign_flow_zapi_update(max_days_back: int = DEFAULT_MAX_DAYS_BACK) -> dict:
    """Core logic, extracted so both this script's CLI and a Telegram
    command (commands/scan.py's ffupdate_command) can call it the same
    way. Returns a summary dict: {n_ok, n_empty, n_error, rows_written,
    dates_ok, dates_empty}."""
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    done_dates = already_fetched_dates(conn)

    today = dt.date.today()
    candidates = [(today - dt.timedelta(days=i)) for i in range(max_days_back, -1, -1)]
    todo = [d for d in candidates if d.isoformat() not in done_dates]
    print(f"[ffzapi] {len(done_dates)} dates already recorded (any history). "
          f"{len(todo)}/{len(candidates)} candidate dates to try (last {max_days_back}d window).")

    total_rows_written = 0
    n_ok, n_empty, n_error = 0, 0, 0
    dates_ok, dates_empty = [], []

    for d in todo:
        date_api = d.strftime("%Y%m%d")
        date_iso = d.isoformat()
        try:
            result = broker_engine.fetch_full_day_foreign_flow(date_str=date_api)
        except Exception as e:
            print(f"[ffzapi] FAIL {date_iso}: {e}")
            conn.execute(
                "INSERT OR REPLACE INTO fetch_log(date, status, ticker_count, fetched_at) VALUES (?,?,?,?)",
                (date_iso, "error", 0, dt.datetime.now().isoformat()),
            )
            conn.commit()
            n_error += 1
            continue

        if result is None:
            # quota exhausted, rate-limited, or a page failed mid-pagination
            # -- NOT the same as "Zapi has no data for this date" (empty),
            # don't mark it done so a later re-run retries it.
            print(f"[ffzapi] SKIP {date_iso}: fetch_full_day_foreign_flow returned None "
                  "(quota/rate-limit/partial-page failure) -- will retry next run, not marked done.")
            n_error += 1
            continue

        rows = result["rows"]
        resolved_date = result["date"] or date_iso
        if not rows:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_log(date, status, ticker_count, fetched_at) VALUES (?,?,?,?)",
                (resolved_date, "empty", 0, dt.datetime.now().isoformat()),
            )
            conn.commit()
            n_empty += 1
            dates_empty.append(resolved_date)
            print(f"[ffzapi] EMPTY {resolved_date} (requested {date_iso}) -- Zapi has no data "
                  "(non-trading day, or past its retention window).")
            continue

        recs = [(r["ticker"], resolved_date, r["foreign_buy"], r["foreign_sell"], r["foreign_net"]) for r in rows]
        conn.executemany(
            "INSERT OR REPLACE INTO foreign_flow_daily(ticker, date, foreign_buy, foreign_sell, foreign_net) "
            "VALUES (?, ?, ?, ?, ?)",
            recs,
        )
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log(date, status, ticker_count, fetched_at) VALUES (?,?,?,?)",
            (resolved_date, "ok", len(recs), dt.datetime.now().isoformat()),
        )
        conn.commit()
        total_rows_written += len(recs)
        n_ok += 1
        dates_ok.append(resolved_date)
        print(f"[ffzapi] OK {resolved_date} (requested {date_iso}): {len(recs)} tickers written "
              f"({(len(rows) + 199) // 200} Zapi pages).")

    conn.close()
    print(f"\n[ffzapi] DONE. ok={n_ok} empty={n_empty} error_or_skip={n_error} "
          f"rows_written={total_rows_written}")
    print("[ffzapi] NOTE: this only updates foreign_flow_2y.sqlite on THIS machine -- "
          "no git push, nothing to pull elsewhere. Run this directly on the VPS.")

    return {"n_ok": n_ok, "n_empty": n_empty, "n_error": n_error,
            "rows_written": total_rows_written, "dates_ok": dates_ok, "dates_empty": dates_empty}


def main():
    max_days_back = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MAX_DAYS_BACK
    run_foreign_flow_zapi_update(max_days_back)


if __name__ == "__main__":
    main()
