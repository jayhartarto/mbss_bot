"""
research/ipot_backfill.py — backfill per-ticker daily broker summary from IPOT (Indo Premier), no key needed.

Source: https://www.indopremier.com/module/saham/include/data-brokersummary.php?code=T&start=MM/DD/YYYY&end=MM/DD/YYYY&fd=all&board=all
Each answer is the top-10 BUYERS and top-10 SELLERS of one ticker for the window, ranked by LOT. Figures are GROSS buy / GROSS
sell per broker (not net); a broker present on both sides gets net = buy - sell, present on one side only has the other side
unknown (< that side's 10th value). Values are rounded to 0.1 (M/B/T suffix).

Writes its own file (default research/ipot_broker_daily.sqlite) so the gross semantics never mix with broker_flow's net rows.
Resumable: every call (incl. empty / weekend / holiday answers) is logged in ipot_fetch_log and skipped on re-run.

Usage:
  python research/ipot_backfill.py --pilot                       # 40 liquid tickers x last 60 trading days
  python research/ipot_backfill.py --top 150 --start 2025-10-01 --end 2026-10-21   # option A; later drop --top for all 668
  python research/ipot_backfill.py --start 2024-10-01 --end 2026-10-21 [--workers 4] [--max-calls N]
"""
from __future__ import annotations

import argparse
import gzip
import datetime
import re
import sqlite3
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

URL = ("https://www.indopremier.com/module/saham/include/data-brokersummary.php"
       "?code={t}&start={d}&end={d}&fd={fd}&board={bd}")
UA = "Mozilla/5.0"
HERE = __file__.replace("\\", "/").rsplit("/", 1)[0]
OHLCV = HERE + "/../mbss_ohlcv.db"
FLOW = HERE + "/broker_flow_history.sqlite"

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
-- gzipped raw answer, so a parser fix never costs new requests (fd: all|F|D, board: all|RG|TN|NG)
CREATE TABLE IF NOT EXISTS ipot_raw (
    ticker TEXT NOT NULL, trade_date TEXT NOT NULL, fd TEXT NOT NULL, board TEXT NOT NULL, html_gz BLOB,
    PRIMARY KEY (ticker, trade_date, fd, board)
) WITHOUT ROWID;
"""

_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_MULT = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def money(s: str):
    s = s.strip().replace(",", "")
    if not s:
        return None
    m = re.fullmatch(r"(-?[\d.]+)\s*([KMBT]?)", s)
    return float(m.group(1)) * _MULT[m.group(2)] if m else None


def parse(html: str):
    """-> ('ok'|'empty'|'invalid'|'blocked', buyers, sellers); each side a list of (code, lot, val, avg, rank)."""
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


def fetch(ticker: str, day: str, fd: str, bd: str):
    d = datetime.date.fromisoformat(day).strftime("%m/%d/%Y")
    url = URL.format(t=ticker, d=d, fd=fd, bd=bd)
    last = "error"
    for attempt in range(5):  # IPOT answers spurious nginx 404s in bursts; retry with doubling pauses
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=30) as r:
                html = r.read().decode("utf-8", "replace")
                return parse(html) + (html,)
        except urllib.error.HTTPError as e:
            last = "blocked" if e.code in (403, 429) else "error"
            if e.code in (403, 429):
                time.sleep(30 * (attempt + 1))
            else:
                time.sleep(0.5 * (2 ** attempt))
        except Exception:
            time.sleep(0.5 * (2 ** attempt))
    return last, [], [], None


def trading_days(start: str, end: str) -> list[str]:
    c = sqlite3.connect(OHLCV)
    rows = c.execute("SELECT DISTINCT date FROM ohlcv_daily WHERE date BETWEEN ? AND ? ORDER BY date",
                     (start, end)).fetchall()
    days = [r[0][:10] for r in rows]
    if days:
        return days
    d0, d1 = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)  # fallback: weekdays
    return [(d0 + datetime.timedelta(i)).isoformat() for i in range((d1 - d0).days + 1)
            if (d0 + datetime.timedelta(i)).weekday() < 5]


def universe(top: int) -> list[str]:
    """top>0: the `top` most liquid tickers (20d value traded); top=0: every ticker in broker_flow_history."""
    if top:
        c = sqlite3.connect(OHLCV)
        last = c.execute("SELECT MAX(date) FROM ohlcv_daily").fetchone()[0]
        rows = c.execute("""SELECT ticker FROM ohlcv_daily WHERE date >= date(?, '-20 day')
                            GROUP BY ticker ORDER BY SUM(close*volume) DESC LIMIT ?""", (last, top)).fetchall()
        return [r[0].replace(".JK", "") for r in rows]
    c = sqlite3.connect(FLOW)
    return [r[0] for r in c.execute("SELECT DISTINCT ticker FROM broker_flow ORDER BY ticker")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--top", type=int, default=0, help="only the N most liquid tickers (0 = all 668)")
    ap.add_argument("--tickers", help="explicit comma list (e.g. the current picks); overrides --top")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-calls", type=int, default=0)
    ap.add_argument("--variants", default="all/all,F/all,D/all,all/NG",
                    help="comma list of fd/board; fd: all|F|D, board: all|RG|TN|NG")
    ap.add_argument("--db", default=HERE + "/ipot_broker_daily.sqlite")
    a = ap.parse_args()

    end = a.end or datetime.date.today().isoformat()
    start = a.start or (datetime.date.fromisoformat(end) - datetime.timedelta(days=90)).isoformat()
    days = trading_days(start, end)
    if a.pilot:
        days = days[-60:]
    tickers = a.tickers.split(",") if a.tickers else universe(40 if a.pilot else a.top)

    conn = sqlite3.connect(a.db)
    conn.executescript(SCHEMA)
    done = set(conn.execute("SELECT ticker, trade_date, fd, board FROM ipot_fetch_log WHERE status IN ('ok','empty','invalid')"))
    variants = [tuple(v.split("/")) for v in a.variants.split(",")]
    todo = [(t, d, fd, bd) for d in days for t in tickers for fd, bd in variants if (t, d, fd, bd) not in done]
    if a.max_calls:
        todo = todo[:a.max_calls]
    print(f"{len(tickers)} tickers x {len(days)} days -> {len(todo)} calls to make ({len(done)} already logged)", flush=True)

    stop = threading.Event()
    counts = {"ok": 0, "empty": 0, "invalid": 0, "blocked": 0, "error": 0}
    t0 = time.time()

    def work(t, d, fd, bd):
        if stop.is_set():
            return t, d, fd, bd, None
        time.sleep(0.05)
        return t, d, fd, bd, fetch(t, d, fd, bd)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(work, *x) for x in todo]
        for i, f in enumerate(as_completed(futs), 1):
            t, d, fd, bd, res = f.result()
            if res is None:
                continue
            status, buys, sells, html = res
            counts[status] += 1
            if status in ("ok", "empty", "invalid", "blocked", "error"):
                conn.execute("INSERT OR REPLACE INTO ipot_fetch_log VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (t, d, fd, bd, status, len(buys), len(sells),
                              min((x[2] for x in buys if x[2] is not None), default=None),
                              min((x[2] for x in sells if x[2] is not None), default=None),
                              datetime.datetime.now().isoformat(timespec="seconds")))
            if html is not None and status in ("ok", "invalid"):
                conn.execute("INSERT OR REPLACE INTO ipot_raw VALUES (?,?,?,?,?)",
                             (t, d, fd, bd, gzip.compress(html.encode("utf-8"))))
            if status == "ok":
                rows = {}
                for code, lot, val, avg, rk in buys:
                    rows.setdefault(code, {}).update(buy_lot=lot, buy_val=val, buy_avg=avg, buy_rank=rk)
                for code, lot, val, avg, rk in sells:
                    rows.setdefault(code, {}).update(sell_lot=lot, sell_val=val, sell_avg=avg, sell_rank=rk)
                for code, r in rows.items():
                    conn.execute("INSERT OR REPLACE INTO ipot_broker_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (t, d, fd, bd, code, r.get("buy_lot"), r.get("buy_val"), r.get("buy_avg"), r.get("buy_rank"),
                                  r.get("sell_lot"), r.get("sell_val"), r.get("sell_avg"), r.get("sell_rank")))
            if i % 200 == 0:
                conn.commit()
                el = time.time() - t0
                print(f"{i}/{len(todo)}  {counts}  {i / el:.1f} calls/s  eta {(len(todo) - i) / (i / el) / 60:.0f} min",
                      flush=True)
            if counts["blocked"] >= 20 and counts["ok"] == 0:
                print("looks blocked (no ok answers, 20+ blocked) - stopping", file=sys.stderr)
                stop.set()
    conn.commit()
    print("done", counts)


if __name__ == "__main__":
    main()
