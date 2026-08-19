"""
backtest/research_ema9_productivity.py — MBSS v2, user request ("coba
revisit juga teknikal above EMA9 apakah lebih produktif?"). Run on the
server:

    python backtest/research_ema9_productivity.py

HC's criteria 8 (compute_high_conviction_score, engine/scoring.py) requires
price ABOVE BOTH EMA9 AND SMA20 as a single AND'd criterion — that combined
boolean isn't stored anywhere for retrospective analysis (only the
aggregate high_conviction_met/checkable COUNT is, in feature_snapshot).
This script RECOMPUTES price-vs-EMA9/SMA20 retroactively for every
RESOLVED /winrate pick, using OHLCV already cached locally in the OHLCV
SQLite DB (get_ohlcv_smart — zero extra API cost, no live fetch), and
buckets outcomes by:
  - above EMA9 only (not necessarily above SMA20)
  - above SMA20 only (not necessarily above EMA9)
  - above BOTH (= HC's actual current criterion)
  - above NEITHER

This tells us whether EMA9 alone is doing real work, or whether the
AND-with-SMA20 requirement is adding/subtracting value versus either
condition alone.

CAVEAT: EMA9 is recomputed here from whatever trailing bars are available
in the LOCAL OHLCV cache up to (and including) pick_date — not always the
full listing history. EWM decays fast (span=9) so this converges well
within ~30-40 bars, but if a ticker has very little local history before
its pick_date, the EMA9 read here may be less precise than what the live
system saw with a fuller series at the time.

Purely descriptive — no formula/weight change. Any actual change to HC's
criteria (e.g. dropping the AND-with-SMA20, or dropping EMA9 entirely)
should only happen after this shows a real, large-enough-sample pattern.
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.legacy_core as core

MIN_BARS_FOR_EMA9 = 20  # sama ambang dengan compute_high_conviction_score's kriteria 8


def _bucket_stats(label, group):
    if not group:
        print(f"  {label:<24} n=0")
        return
    wins = [p for p in group if p["status"] in ("win", "win_timebased")]
    winrate = len(wins) / len(group) * 100
    avg_gain = statistics.mean(p["pnl_pct"] for p in group if p.get("pnl_pct") is not None)
    print(f"  {label:<24} n={len(group):<5} winrate={winrate:5.1f}%  avg_gain={avg_gain:+5.1f}%")


def main():
    history = core.load_daytrade_picks_history()
    resolved = [p for p in history if p.get("status") in ("win", "lose", "win_timebased", "lose_timebased")]
    print(f"📋 Total picks di history: {len(history)}, resolved: {len(resolved)}\n")
    if len(resolved) < 30:
        print("⚠️ Sampel masih kecil (<30) — hasil di bawah BARU indikasi awal.\n")

    ohlcv_cache = {}
    buckets = {"above_both": [], "above_ema9_only": [], "above_sma20_only": [], "above_neither": []}
    skipped = 0

    for p in resolved:
        ticker = p.get("ticker")
        pick_date = p.get("pick_date")
        if not ticker or not pick_date:
            skipped += 1
            continue

        if ticker not in ohlcv_cache:
            try:
                ohlcv_cache[ticker] = core.get_ohlcv_smart(ticker, limit=500)
            except Exception:
                ohlcv_cache[ticker] = None
        hist = ohlcv_cache[ticker]
        if hist is None or hist.empty:
            skipped += 1
            continue

        try:
            pick_date_ts = pd_timestamp(pick_date)
        except Exception:
            skipped += 1
            continue

        slice_ = hist[hist.index <= pick_date_ts]
        if len(slice_) < MIN_BARS_FOR_EMA9:
            skipped += 1
            continue

        closes = slice_["Close"].astype(float)
        ema9 = closes.ewm(span=9, adjust=False).mean().iloc[-1]
        sma20 = closes.tail(20).mean()
        price = float(closes.iloc[-1])

        above_ema9 = price > ema9
        above_sma20 = price > sma20

        if above_ema9 and above_sma20:
            buckets["above_both"].append(p)
        elif above_ema9 and not above_sma20:
            buckets["above_ema9_only"].append(p)
        elif above_sma20 and not above_ema9:
            buckets["above_sma20_only"].append(p)
        else:
            buckets["above_neither"].append(p)

    if skipped:
        print(f"(dilewati {skipped} pick — data OHLCV lokal belum cukup di tanggal itu, ticker delisted, atau pick_date tidak terbaca)\n")

    print("=" * 70)
    print("Winrate berdasarkan posisi harga vs EMA9/SMA20 SAAT pick dikunci")
    print("=" * 70)
    _bucket_stats("Above BOTH (kriteria HC saat ini)", buckets["above_both"])
    _bucket_stats("Above EMA9 ONLY (bukan SMA20)", buckets["above_ema9_only"])
    _bucket_stats("Above SMA20 ONLY (bukan EMA9)", buckets["above_sma20_only"])
    _bucket_stats("Above NEITHER", buckets["above_neither"])

    print("\nBaca hasil ini begini:")
    print("- Kalau 'Above BOTH' JELAS lebih baik dari 'EMA9 ONLY' dan 'SMA20 ONLY' (DAN n cukup, >=15-20), kriteria AND saat ini sudah tepat -- EMA9 dan SMA20 saling melengkapi.")
    print("- Kalau 'EMA9 ONLY' hampir sama baiknya dengan 'Above BOTH', EMA9 sendiri sudah cukup produktif -- syarat SMA20 mungkin cuma memperketat tanpa manfaat winrate (mengurangi jumlah kandidat lolos HC tanpa alasan kuat).")
    print("- Kalau 'EMA9 ONLY' JAUH lebih jelek dari 'SMA20 ONLY', EMA9 mungkin kurang produktif dibanding SMA20 utk kriteria ini -- pertimbangkan re-evaluasi.")
    print("- Kalau semua bucket mirip (tidak ada beda jelas), kriteria posisi MA ini kemungkinan bukan pembeda kuat sama sekali utk HC's horizon 1-2 hari.")


def pd_timestamp(date_str):
    import pandas as pd
    return pd.Timestamp(date_str)


if __name__ == "__main__":
    main()
