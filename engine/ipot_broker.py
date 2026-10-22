"""
engine/ipot_broker.py — MBSS v2 (2026-10-22).

Daily per-ticker broker summary from IPOT (Indo Premier), no key. Stored in research/ipot_broker_daily.sqlite, kept apart from
broker_flow_history.sqlite on purpose: IPOT gives GROSS buy and GROSS sell per broker, only the top 10 per side ranked by LOT,
whereas broker_flow holds signed NET values. A broker absent from a side means "below that side's 10th", not zero.

Endpoint (verified 2026-10-22, history reaches back to at least 2019):
  https://www.indopremier.com/module/saham/include/data-brokersummary.php?code=T&start=MM/DD/YYYY&end=MM/DD/YYYY&fd=all|F|D&board=all|RG|TN|NG
Answers an HTML fragment; weekend/holiday = ten blank rows; unknown code = "Invalid stock code!" with HTTP 200; nginx sometimes
answers a spurious 404 in bursts (retry with doubling pauses).

research/ipot_backfill.py is the bulk historical backfill (same schema); collect_day() is the nightly top-up.
"""
from __future__ import annotations

import datetime
import gzip
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "research", "ipot_broker_daily.sqlite")
OHLCV_DB = os.path.join(ROOT, "mbss_ohlcv.db")
PICK_FILES = ["macd_confirm_pillar_picks.json", "oversold_bounce_v2_picks.json", "buy_on_strength_picks.json",
              "vcp_pillar_picks.json", "buy_on_weakness_picks.json"]
DEFAULT_VARIANTS = (("all", "all"), ("F", "all"), ("D", "all"), ("all", "NG"))
TOP_LIQUID = 150
MIN_HOUR_WIB_FOR_TODAY = 17  # IDX closes 16:00 WIB (Fri 16:00 too); give IPOT time to finalise the day

URL = ("https://www.indopremier.com/module/saham/include/data-brokersummary.php"
       "?code={t}&start={d}&end={d}&fd={fd}&board={bd}")
UA = "Mozilla/5.0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ipot_broker_daily (
    ticker TEXT NOT NULL, trade_date TEXT NOT NULL, fd TEXT NOT NULL, board TEXT NOT NULL, broker_code TEXT NOT NULL,
    buy_lot REAL, buy_val REAL, buy_avg REAL, buy_rank INTEGER,
    sell_lot REAL, sell_val REAL, sell_avg REAL, sell_rank INTEGER,
    PRIMARY KEY (ticker, trade_date, fd, board, broker_code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ipot_fetch_log (
    ticker TEXT NOT NULL, trade_date TEXT NOT NULL, fd TEXT NOT NULL, board TEXT NOT NULL,
    status TEXT NOT NULL,            -- ok | empty | invalid | blocked | error
    n_buy INTEGER, n_sell INTEGER, min_buy_val REAL, min_sell_val REAL, fetched_at TEXT,
    PRIMARY KEY (ticker, trade_date, fd, board)
) WITHOUT ROWID;
-- gzipped raw answer, so a parser fix never costs new requests
CREATE TABLE IF NOT EXISTS ipot_raw (
    ticker TEXT NOT NULL, trade_date TEXT NOT NULL, fd TEXT NOT NULL, board TEXT NOT NULL, html_gz BLOB,
    PRIMARY KEY (ticker, trade_date, fd, board)
) WITHOUT ROWID;
"""

_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_MULT = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)  # the bulk backfill may be writing the same file
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def money(s: str):
    s = s.strip().replace(",", "")
    if not s:
        return None
    m = re.fullmatch(r"(-?[\d.]+)\s*([KMBT]?)", s)
    return float(m.group(1)) * _MULT[m.group(2)] if m else None


def parse(html: str):
    """-> (status, buyers, sellers); status ok|empty|invalid|blocked; each side a list of (code, lot, val, avg, rank)."""
    if "Invalid stock code" in html:
        return "invalid", [], []
    if "table-summary" not in html:
        return "blocked", [], []
    buys, sells = [], []
    for body in _ROW.findall(html.split("<tbody>", 1)[-1]):
        c = [_TAG.sub("", x).replace("&nbsp;", " ").strip() for x in _CELL.findall(body)]
        if len(c) != 9:
            continue
        rank = int(c[4]) if c[4].isdigit() else None
        if c[0]:
            buys.append((c[0], money(c[1]), money(c[2]), money(c[3]), rank))
        if c[5]:
            sells.append((c[5], money(c[6]), money(c[7]), money(c[8]), rank))
    return ("ok" if buys or sells else "empty"), buys, sells


def fetch(ticker: str, day: str, fd: str = "all", bd: str = "all"):
    """-> (status, buyers, sellers, html). Never raises."""
    d = datetime.date.fromisoformat(day).strftime("%m/%d/%Y")
    url = URL.format(t=ticker, d=d, fd=fd, bd=bd)
    last = "error"
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=30) as r:
                html = r.read().decode("utf-8", "replace")
            return parse(html) + (html,)
        except urllib.error.HTTPError as e:
            last = "blocked" if e.code in (403, 429) else "error"
            time.sleep(30 * (attempt + 1) if e.code in (403, 429) else 0.5 * (2 ** attempt))
        except Exception:  # noqa: BLE001
            time.sleep(0.5 * (2 ** attempt))
    return last, [], [], None


def save(conn: sqlite3.Connection, t: str, d: str, fd: str, bd: str, res) -> None:
    status, buys, sells, html = res
    conn.execute("INSERT OR REPLACE INTO ipot_fetch_log VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (t, d, fd, bd, status, len(buys), len(sells),
                  min((x[2] for x in buys if x[2] is not None), default=None),
                  min((x[2] for x in sells if x[2] is not None), default=None),
                  datetime.datetime.now().isoformat(timespec="seconds")))
    if html is not None and status in ("ok", "invalid"):
        conn.execute("INSERT OR REPLACE INTO ipot_raw VALUES (?,?,?,?,?)", (t, d, fd, bd, gzip.compress(html.encode("utf-8"))))
    if status == "ok":
        rows: dict = {}
        for code, lot, val, avg, rk in buys:
            rows.setdefault(code, {}).update(buy_lot=lot, buy_val=val, buy_avg=avg, buy_rank=rk)
        for code, lot, val, avg, rk in sells:
            rows.setdefault(code, {}).update(sell_lot=lot, sell_val=val, sell_avg=avg, sell_rank=rk)
        for code, r in rows.items():
            conn.execute("INSERT OR REPLACE INTO ipot_broker_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (t, d, fd, bd, code, r.get("buy_lot"), r.get("buy_val"), r.get("buy_avg"), r.get("buy_rank"),
                          r.get("sell_lot"), r.get("sell_val"), r.get("sell_avg"), r.get("sell_rank")))


def active_pick_tickers(root: str = ROOT) -> list[str]:
    """Tickers of every still-open pick across the lane pick files."""
    out: set = set()
    for f in PICK_FILES:
        try:
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                for r in json.load(fh):
                    if r.get("ticker") and not r.get("resolved_date"):
                        out.add(r["ticker"].replace(".JK", ""))
        except Exception:  # noqa: BLE001 — a missing/corrupt pick file just contributes nothing
            pass
    return sorted(out)


def liquid_tickers(n: int = TOP_LIQUID) -> list[str]:
    c = sqlite3.connect(OHLCV_DB, timeout=30)
    try:
        last = c.execute("SELECT MAX(date) FROM ohlcv_daily").fetchone()[0]
        rows = c.execute("""SELECT ticker FROM ohlcv_daily WHERE date >= date(?, '-20 day')
                            GROUP BY ticker ORDER BY SUM(close*volume) DESC LIMIT ?""", (last, n)).fetchall()
    finally:
        c.close()
    return [r[0].replace(".JK", "") for r in rows]


def recent_weekdays(end: datetime.date, n: int) -> list[str]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= datetime.timedelta(days=1)
    return sorted(out)


def collect_days(days: list[str], tickers: list[str], variants=DEFAULT_VARIANTS, workers: int = 5,
                 db_path: str = DB_PATH) -> dict:
    """Fetch every (ticker, day, variant) not yet logged as ok/empty/invalid and store it. Returns status counts. Never raises
    on a fetch failure (logged as blocked/error and retried by the next call)."""
    conn = connect(db_path)
    try:
        done = set(conn.execute("SELECT ticker, trade_date, fd, board FROM ipot_fetch_log "
                                "WHERE status IN ('ok','empty','invalid')"))
        todo = [(t, d, fd, bd) for d in days for t in tickers for fd, bd in variants if (t, d, fd, bd) not in done]
        counts = {"ok": 0, "empty": 0, "invalid": 0, "blocked": 0, "error": 0, "todo": len(todo)}
        if not todo:
            return counts

        def work(x):
            time.sleep(0.05)
            return x, fetch(*x)

        i = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for c0 in range(0, len(todo), 500):  # bounded chunks: queuing everything at once is hundreds of MB on the 1 GB VPS
                for x, res in ex.map(work, todo[c0:c0 + 500]):
                    i += 1
                    counts[res[0]] += 1
                    save(conn, *x, res)
                    if i % 200 == 0:
                        conn.commit()
        conn.commit()
        return counts
    finally:
        conn.close()


def collect_nightly(end: datetime.date | None = None, lookback_weekdays: int = 3) -> dict:
    """Nightly top-up: active picks + most liquid names, the last few weekdays (so one missed night heals itself)."""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    if end is None:
        # IPOT serves the running day intraday; a partial day logged as ok would never be refreshed, so wait until after close
        end = now.date() if now.hour >= MIN_HOUR_WIB_FOR_TODAY else now.date() - datetime.timedelta(days=1)
    tickers = sorted(set(active_pick_tickers()) | set(liquid_tickers()))
    return collect_days(recent_weekdays(end, lookback_weekdays), tickers) | {"tickers": len(tickers)}


def merge_into(dest_path: str, src_path: str) -> dict:
    """INSERT OR IGNORE every table of src into dest (e.g. the laptop's 10-day picks backfill into the VPS file)."""
    dest = connect(dest_path)
    try:
        dest.execute("ATTACH DATABASE ? AS src", (src_path,))
        out = {}
        for t in ("ipot_broker_daily", "ipot_fetch_log", "ipot_raw"):
            before = dest.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            dest.execute(f"INSERT OR IGNORE INTO {t} SELECT * FROM src.{t}")
            out[t] = dest.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] - before
        dest.commit()
        dest.execute("DETACH DATABASE src")
        return out
    finally:
        dest.close()
