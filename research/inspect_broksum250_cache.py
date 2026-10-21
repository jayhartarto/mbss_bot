"""
Read-only inspection of the nightly broksum_250 cache (cache/broksum_250.pkl). Run ON THE VPS from ~/mbss:
    python3 research/inspect_broksum250_cache.py

Prints cache age, ticker count, brokers per ticker, most common broker codes, one sample row, and writes
research/broksum250_snapshot.csv (one row per ticker x broker) so it can be copied to the laptop.
Uses no Index Alpha quota and writes nothing besides that CSV.
"""
import collections
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine.cache import cache_manager  # noqa: E402
from engine import nightly as n  # noqa: E402

FIELDS = ["code", "buy_value", "sell_value", "buy_avg", "sell_avg", "buy_volume", "sell_volume", "buy_freq", "sell_freq"]


def main():
    print("meta:", cache_manager.get_meta("broksum_250"))
    print("age:", n.get_broksum_250_age_info())
    d = n.load_broksum_250()
    print("tickers:", len(d), "| lookback_days:", getattr(n, "BROKSUM_250_LOOKBACK_DAYS", None))
    if not d:
        return
    nb = sorted(len(v) for v in d.values())
    print("brokers/ticker min/median/max:", nb[0], nb[len(nb) // 2], nb[-1])
    codes = collections.Counter(r.get("code") for v in d.values() for r in v)
    print("distinct broker codes:", len(codes), codes.most_common(12))
    first = next(iter(d))
    print("sample:", first, d[first][0])
    out = ROOT / "research" / "broksum250_snapshot.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker"] + FIELDS)
        for tk, rows in d.items():
            for r in rows:
                w.writerow([tk] + [r.get(k) for k in FIELDS])
    print("saved", out)


if __name__ == "__main__":
    main()
