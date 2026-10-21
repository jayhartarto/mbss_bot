"""
Backfill research/broker_flow_history.sqlite with weekly (Mon-Fri) broker-activity windows from RapidAPI.

    python research/backfill_broker_flow.py --brokers XL,MG,CC,AK,BK --start 2026-06-08 --end 2026-10-16 \
        --max-calls 100 --reserve 225 --dry-run
Drop --dry-run to spend quota. Resumable (windows already stored with rows are skipped), never exceeds
--max-calls, stops when the server-reported remaining quota would fall below --reserve (keeps quota for the
nightly sweep), and never retries inside a run. Uses the key in .env; the quota counter on the VPS is NOT
updated by this script, which is why the server-reported remaining is stored per call.
"""
import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import legacy_core as core  # noqa: E402
from engine import broker_flow_db as db  # noqa: E402

DB_PATH = ROOT / "research" / "broker_flow_history.sqlite"
LIMIT = 100


def week_windows(start: str, end: str):
    d = dt.date.fromisoformat(start)
    d -= dt.timedelta(days=d.weekday())  # Monday
    last = dt.date.fromisoformat(end)
    while d <= last:
        yield d.isoformat(), (d + dt.timedelta(days=4)).isoformat()
        d += dt.timedelta(days=7)


def fetch(code: str, ws: str, we: str):
    r = requests.get(
        f"{core.RAPIDAPI_IDX_BASE_URL}/api/market-detector/broker-activity/{code}",
        params={"page": "1", "limit": str(LIMIT), "investorType": "INVESTOR_TYPE_ALL",
                "transactionType": "TRANSACTION_TYPE_NET", "marketBoard": "MARKET_BOARD_ALL", "from": ws, "to": we},
        headers=core.RAPIDAPI_IDX_HEADERS, timeout=40)
    if r.status_code == 429:  # per-second rate limit, not an answer: wait and retry once
        time.sleep(6)
        r = requests.get(
            f"{core.RAPIDAPI_IDX_BASE_URL}/api/market-detector/broker-activity/{code}",
            params={"page": "1", "limit": str(LIMIT), "investorType": "INVESTOR_TYPE_ALL",
                    "transactionType": "TRANSACTION_TYPE_NET", "marketBoard": "MARKET_BOARD_ALL", "from": ws, "to": we},
            headers=core.RAPIDAPI_IDX_HEADERS, timeout=40)
    rem = r.headers.get("X-RateLimit-Requests-Remaining")
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"raw": r.text[:300]}
    return r.status_code, body, int(rem) if rem and rem.isdigit() else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brokers", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--max-calls", type=int, default=100)
    ap.add_argument("--reserve", type=int, default=225, help="stop if server-reported remaining quota would drop below this")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    conn = db.connect(str(DB_PATH))
    jobs = [(b, ws, we) for ws, we in week_windows(a.start, a.end) for b in a.brokers.split(",")]
    todo = [j for j in jobs if not db.already_fetched(conn, *j)]
    print(f"{len(jobs)} windows planned, {len(jobs) - len(todo)} already stored, {len(todo)} to fetch (cap {a.max_calls}).")
    if a.dry_run:
        for j in todo[: a.max_calls]:
            print("  would fetch", j)
        return
    spent, last_rem = 0, None
    for broker, ws, we in todo[: a.max_calls]:
        if last_rem is not None and last_rem - 1 < a.reserve:
            print(f"reserve guard: remaining {last_rem}, stopping before spending below {a.reserve}")
            break
        status, body, last_rem = fetch(broker, ws, we)
        n = db.save_call(conn, broker, ws, we, body, status, LIMIT, last_rem)
        spent += 1
        print(f"  {broker} {ws}..{we}: HTTP {status}, {n} equity rows, remaining {last_rem}")
        time.sleep(1.1)
    print(f"done: {spent} calls, remaining quota {last_rem}")


if __name__ == "__main__":
    main()
