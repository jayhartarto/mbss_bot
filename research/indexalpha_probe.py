"""
Read-only probe of Index Alpha's batch broker-summary endpoint, to decide whether a
historical backfill is worth paying for. Run ON THE VPS (that's where the API token lives),
at a time when the nightly job has not yet used up today's free quota (free tier = 5/day).

Uses at most 3 requests: one batch for an OLD date, one batch for a RECENT date, one /usage.

What it checks (print-only, writes nothing, touches no production state):
  1. does batch with from == to return a single day, for an old date (history depth)?
  2. how many brokers per ticker come back (all brokers, or only a top-N)?
  3. which fields are present (freq / volume / value / avg)?
  4. do the numbers look day-isolated (compare old-date totals vs a 2-day window is NOT done
     to save quota; instead sanity-check buy_value totals against sell_value totals)
  5. remaining quota before/after (from /usage) -> did one batch of 5 tickers cost 1 request or 5?

Usage (from ~/mbss):
    python3 research/indexalpha_probe.py
    python3 research/indexalpha_probe.py --old 2025-03-03 --recent 2026-10-19
"""
import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import legacy_core as core  # noqa: E402  (loads .env, gives base URL + headers)

TICKERS = ["BBCA", "BRMS", "ENRG", "TLKM", "MDKA"]


def usage():
    try:
        r = requests.get(f"{core.INDEXALPHA_BASE_URL}/usage", headers=core.INDEXALPHA_HEADERS, timeout=20)
        return r.status_code, r.text[:400]
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def batch(day):
    r = requests.post(
        f"{core.INDEXALPHA_BASE_URL}/stocks/broker-summary/batch",
        json={"tickers": TICKERS, "from": day, "to": day, "investor": "all"},
        headers=core.INDEXALPHA_HEADERS,
        timeout=45,
    )
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"raw": r.text[:300]}
    return r.status_code, body


def summarize(day, status, body):
    print(f"\n=== batch {day}: HTTP {status}, success={body.get('success')}, error={body.get('error')}")
    data = body.get("data") or {}
    if not isinstance(data, dict) or not data:
        print("   no data returned:", json.dumps(body)[:300])
        return
    for t in TICKERS:
        rows = data.get(t) or []
        if not rows:
            print(f"   {t}: 0 broker rows")
            continue
        bv = sum((x.get("buy_value") or 0) for x in rows)
        sv = sum((x.get("sell_value") or 0) for x in rows)
        has_freq = any(x.get("buy_freq") for x in rows)
        print(f"   {t}: {len(rows)} brokers | sum buy_value {bv/1e9:,.1f}bn vs sell_value {sv/1e9:,.1f}bn | freq present={has_freq}")
    first = next((v for v in data.values() if v), None)
    if first:
        print("   fields:", sorted(first[0].keys()))
        print("   sample row:", json.dumps(first[0]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="2025-03-03", help="old trading day to test history depth")
    ap.add_argument("--recent", default="2026-10-19", help="recent trading day")
    a = ap.parse_args()

    print("usage BEFORE:", usage())
    for day in (a.old, a.recent):
        status, body = batch(day)
        summarize(day, status, body)
    print("\nusage AFTER:", usage())
    print("\nRead it as: if usage dropped by 2 (not 10), a 5-ticker batch costs 1 request -> backfill math holds.")
    print("If the old date returns data with all brokers and freq present, history + granularity are OK.")


if __name__ == "__main__":
    main()
