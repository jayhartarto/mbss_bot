"""
backtest/research_intraday_predictive.py — MBSS v2, user request ("gimana
sinyal intraday beneran bisa diterjemahkan sebagai real breakout di hari
itu atau hari berikutnya? atau sebaliknya"). Run on the server:

    python backtest/research_intraday_predictive.py

Cross-references every /check tactical snapshot logged in
tactical_shadow_log.json (Phase 1 AB-RC3 shadow logging — see
save_tactical_shadow_snapshot in engine/backbone.py) against what price
ACTUALLY did on the following trading day(s), using the daily EOD close
already cached locally (get_ohlcv_smart, zero extra API cost).

CAVEAT (be upfront about this, it's a real limitation): the log does NOT
store the exact live price at snapshot time (only ticker/timestamp/state/
rank/decision) — this script approximates using the CLOSING price of the
snapshot's own calendar day as the reference point, then compares to the
next 1-2 trading days' closes. That's a same-day-close-to-close read, not
a precise "return since the exact moment you looked" — directionally
useful, not surgically precise. If this research proves valuable, logging
the exact live price at snapshot time going forward would sharpen it.

This is likely to be THIN on data early on (Phase 1 logging just started)
— treat any bucket with n<15-20 as "not enough yet", not a conclusion.
"""
import datetime
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.legacy_core as core
import engine.backbone as backbone_engine


def _forward_return(hist, snapshot_date, days_ahead):
    """% change from close ON/BEFORE snapshot_date to close `days_ahead` trading days later. None if not enough data."""
    if hist is None or hist.empty:
        return None
    dates = hist.index.date
    on_or_before = [i for i, d in enumerate(dates) if d <= snapshot_date]
    if not on_or_before:
        return None
    base_idx = on_or_before[-1]
    target_idx = base_idx + days_ahead
    if target_idx >= len(hist):
        return None
    base_close = float(hist["Close"].iloc[base_idx])
    target_close = float(hist["Close"].iloc[target_idx])
    if base_close <= 0:
        return None
    return (target_close - base_close) / base_close * 100


def main():
    if not os.path.exists(backbone_engine.TACTICAL_SHADOW_LOG_FILE):
        print("❌ tactical_shadow_log.json tidak ditemukan — belum ada /check yang jalan dengan versi tactical ini.")
        sys.exit(1)

    with open(backbone_engine.TACTICAL_SHADOW_LOG_FILE, encoding="utf-8") as f:
        log = json.load(f)
    print(f"📋 Total snapshot di tactical_shadow_log.json: {len(log)}\n")
    if len(log) < 30:
        print("⚠️ Sampel sangat kecil (<30 snapshot total) — Phase 1 logging baru mulai, hasil di bawah ini BARU indikasi awal.\n")

    by_state = {}
    ohlcv_cache = {}
    skipped_no_data = 0

    for entry in log:
        ticker = entry.get("ticker")
        ts = entry.get("timestamp")
        state = entry.get("validity_state")
        if not ticker or not ts or not state:
            continue
        try:
            snapshot_date = datetime.datetime.fromisoformat(ts).astimezone(core.WIB).date()
        except Exception:
            continue

        if ticker not in ohlcv_cache:
            try:
                ohlcv_cache[ticker] = core.get_ohlcv_smart(ticker, limit=40)
            except Exception:
                ohlcv_cache[ticker] = None
        hist = ohlcv_cache[ticker]

        ret_1d = _forward_return(hist, snapshot_date, 1)
        ret_2d = _forward_return(hist, snapshot_date, 2)
        if ret_1d is None and ret_2d is None:
            skipped_no_data += 1
            continue

        by_state.setdefault(state, []).append({"ticker": ticker, "ret_1d": ret_1d, "ret_2d": ret_2d})

    if skipped_no_data:
        print(f"(dilewati {skipped_no_data} snapshot karena data OHLCV forward belum cukup — biasanya snapshot dari 1-2 hari terakhir)\n")

    print("=" * 78)
    print(f"{'STATE':<20}{'n':<6}{'avg ret+1d':<14}{'%positif+1d':<14}{'avg ret+2d':<14}{'%positif+2d'}")
    print("=" * 78)
    for state, entries in sorted(by_state.items(), key=lambda kv: -len(kv[1])):
        r1 = [e["ret_1d"] for e in entries if e["ret_1d"] is not None]
        r2 = [e["ret_2d"] for e in entries if e["ret_2d"] is not None]
        avg1 = statistics.mean(r1) if r1 else None
        avg2 = statistics.mean(r2) if r2 else None
        pos1 = (sum(1 for x in r1 if x > 0) / len(r1) * 100) if r1 else None
        pos2 = (sum(1 for x in r2 if x > 0) / len(r2) * 100) if r2 else None
        avg1_s = f"{avg1:+.2f}%" if avg1 is not None else "-"
        avg2_s = f"{avg2:+.2f}%" if avg2 is not None else "-"
        pos1_s = f"{pos1:.0f}%" if pos1 is not None else "-"
        pos2_s = f"{pos2:.0f}%" if pos2 is not None else "-"
        print(f"{state:<20}{len(entries):<6}{avg1_s:<14}{pos1_s:<14}{avg2_s:<14}{pos2_s}")

    print("\nBaca hasil ini sebagai VALIDASI ARAH, bukan angka presisi (lihat caveat di docstring atas):")
    print("- VALID/EXTENDED_CHASE idealnya avg ret POSITIF dan %positif >50% (state 'bullish' terbukti beneran lanjut naik)")
    print("- INVALID idealnya avg ret NEGATIF/flat (state 'terbantahkan' terbukti beneran lemah)")
    print("- Kalau ternyata KEBALIKAN atau tidak beda jauh antar state, itu sinyal formula tactical butuh dikaji ulang -- tapi tunggu n cukup besar dulu sebelum simpulkan.")


if __name__ == "__main__":
    main()
