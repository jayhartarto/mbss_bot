"""
backtest/research_macd_approach_score_v2.py — MBSS v2, user request lanjutan
setelah v1 (research_macd_approach_score.py). Temuan v1 (n=23.508,
konsisten di 3/5/10 hari): composite recency+eta TERBALIK arah — skor
TERTINGGI (cross paling baru + ETA paling dekat ke centerline) justru
forward return PALING JELEK (fwd3d -0.01%, fwd10d +0.34%), sementara skor
TERENDAH justru fwd10d +1.60%. v1 TIDAK dihapus — hasilnya sendiri jadi
bukti kenapa pendekatan "skor gabungan recency+eta" ditinggalkan, bukan
ditambal dengan tebak bobot baru.

Revisi user, berbasis temuan v1 ("formulanya aja belum ketemu"):
1. macd_line slope window diperpendek dari 5 hari -> 3 hari ("macdline
   positif di 3 hari terakhir" = bergerak naik).
2. BUKAN skor composite recency+eta lagi (itu yang terbukti terbalik) —
   precondition biner: macd_line slope 3-hari POSITIF + macd_line MASIH DI
   BAWAH 0 (belum cross centerline) + ada bullish signal-line cross dalam
   0-20 hari terakhir (reuse macd_cross_days_ago logic, sama dengan v1).
3. Forward return di-BUCKET LANGSUNG per `cross_days_ago` (0-20), BUKAN per
   skor gabungan — v1 tidak bisa menjawab "hari ke berapa paling produktif"
   karena recency & eta digabung jadi satu angka yang saling menutupi.
4. bollinger_squeeze tetap cohort split (bukan filter wajib), sama seperti
   v1 -- squeeze SUDAH tervalidasi kuat & konsisten di v1 (fwd10d +1.83%
   dengan squeeze vs +0.46% tanpa), jadi tetap diukur di sini sebagai
   pembanding independen dari perbaikan #1-3 di atas.

Run di server:
    python backtest/research_macd_approach_score_v2.py

Pakai OHLCV lokal saja (get_ohlcv_daily_from_db), zero fetch/network cost,
sama disiplin dengan v1. Murni observasi -- belum mengubah formula
produksi apa pun.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import engine.legacy_core as core
import engine.nightly as nightly_engine

MACD_LINE_SLOPE_DAYS = 3   # diperpendek dari 5 (v1) -> 3, sesuai revisi user
CROSS_LOOKBACK_DAYS = 20
FORWARD_HORIZONS = (3, 5, 10)
MIN_BARS = 150
EVAL_TAIL_BUFFER = max(FORWARD_HORIZONS) + 2

CROSS_DAYS_BINS = [(0, 3), (4, 7), (8, 11), (12, 15), (16, 20)]  # granularitas buat cari "sweet spot"


def _bandwidth_series(closes: pd.Series) -> pd.Series:
    sma20 = closes.rolling(20).mean()
    std20 = closes.rolling(20).std()
    upper = sma20 + 2 * std20
    lower = sma20 - 2 * std20
    return (upper - lower) / sma20


def _is_squeeze_on_day(bandwidth: pd.Series, i: int) -> bool:
    trailing = bandwidth.iloc[max(0, i - core.MIN_HISTORY_FOR_ADAPTIVE):i].dropna()
    if len(trailing) < 20:
        return False
    pct = core.percentile_rank(trailing, bandwidth.iloc[i])
    return pct <= 0.20


def _find_recent_bullish_cross_days_ago(macd_hist: pd.Series, i: int, max_days_back: int) -> int | None:
    """REUSE exact logic dari v1/compute_factor_scoring's macd_cross_days_ago."""
    if i < 1 or macd_hist.iloc[i] <= 0:
        return None
    for days_back in range(1, min(max_days_back + 1, i + 1)):
        idx = i - days_back
        if idx < 0:
            break
        past_sign = macd_hist.iloc[idx] > 0
        if not past_sign:  # regime dulu bearish, sekarang bullish -> ini titik cross-nya
            return days_back
    return None  # regime sudah berlangsung > max_days_back hari


def macd_approach_precondition(macd_line: pd.Series, macd_hist: pd.Series, i: int) -> int | None:
    """Return cross_days_ago kalau precondition terpenuhi, None kalau tidak."""
    cross_days_ago = _find_recent_bullish_cross_days_ago(macd_hist, i, CROSS_LOOKBACK_DAYS)
    if cross_days_ago is None:
        return None
    if macd_line.iloc[i] >= 0:
        return None  # sudah cross centerline -- di luar scope
    if i < MACD_LINE_SLOPE_DAYS:
        return None
    slope = macd_line.iloc[i] - macd_line.iloc[i - MACD_LINE_SLOPE_DAYS]
    if slope <= 0:
        return None  # syarat wajib: MACD line sendiri naik 3 hari terakhir
    return cross_days_ago


def _bin_label(days_ago: int) -> str:
    for lo, hi in CROSS_DAYS_BINS:
        if lo <= days_ago <= hi:
            return f"{lo}-{hi} hari lalu"
    return "?"


def main():
    scored, _ = nightly_engine.load_daily_scan_cache_allow_stale()
    tickers = sorted(scored.keys()) if scored else []
    print(f"Universe: {len(tickers)} ticker (dari cache /eodscan terakhir)\n")
    if not tickers:
        print("⚠️ Cache /eodscan kosong — jalankan /eodscan dulu di server.")
        return

    rows = []
    skipped = 0

    for ticker in tickers:
        try:
            hist_df = core.get_ohlcv_daily_from_db(ticker, limit=400)
        except Exception:
            skipped += 1
            continue
        if hist_df is None or hist_df.empty or len(hist_df) < MIN_BARS:
            skipped += 1
            continue

        closes = hist_df["Close"].astype(float)
        macd_line, _, macd_hist = core.calculate_macd(closes)
        bandwidth = _bandwidth_series(closes)

        n = len(hist_df)
        eval_end = n - EVAL_TAIL_BUFFER
        if eval_end <= MACD_LINE_SLOPE_DAYS:
            continue

        for i in range(MACD_LINE_SLOPE_DAYS, eval_end):
            cross_days_ago = macd_approach_precondition(macd_line, macd_hist, i)
            if cross_days_ago is None:
                continue

            squeeze = _is_squeeze_on_day(bandwidth, i)
            price_i = closes.iloc[i]
            fwd = {h: round((closes.iloc[i + h] - price_i) / price_i * 100, 2) for h in FORWARD_HORIZONS}

            rows.append({
                "ticker": ticker,
                "date": str(hist_df.index[i].date()),
                "cross_days_ago": cross_days_ago,
                "squeeze": squeeze,
                **{f"fwd_{h}d": fwd[h] for h in FORWARD_HORIZONS},
            })

    print(f"Ticker dilewati (histori lokal <{MIN_BARS} bar): {skipped}/{len(tickers)}")
    print(f"Total observasi (ticker-hari) lolos precondition: {len(rows)}\n")

    if not rows:
        print("⚠️ TIDAK ADA observasi lolos precondition. Precondition mungkin masih terlalu ketat.")
        return

    df = pd.DataFrame(rows)

    # Baseline pembanding: forward return UNIVERSE TANPA syarat apa pun (semua ticker-hari lokal),
    # supaya kita tahu apakah precondition ini beneran outperform "beli acak", bukan cuma positif karena market lagi naik.
    baseline_rows = []
    for ticker in tickers:
        hist_df = core.get_ohlcv_daily_from_db(ticker, limit=400)
        if hist_df is None or hist_df.empty or len(hist_df) < MIN_BARS:
            continue
        closes = hist_df["Close"].astype(float)
        n = len(hist_df)
        eval_end = n - EVAL_TAIL_BUFFER
        # Sample tiap 5 hari (bukan semua ticker-hari) -- baseline murni buat orde-besaran, bukan presisi tinggi
        for i in range(MACD_LINE_SLOPE_DAYS, eval_end, 5):
            price_i = closes.iloc[i]
            baseline_rows.append({h: round((closes.iloc[i + h] - price_i) / price_i * 100, 2) for h in FORWARD_HORIZONS})
    baseline_df = pd.DataFrame(baseline_rows)

    print("=" * 82)
    print("BASELINE (semua ticker-hari lokal, sample tiap 5 hari, TANPA syarat MACD apa pun)")
    print("=" * 82)
    line = f"  n={len(baseline_df):<6}"
    for h in FORWARD_HORIZONS:
        line += f" | fwd{h}d avg={baseline_df[h].mean():+5.2f}%"
    print(line)

    print("\n" + "=" * 82)
    print(f"BUCKET per cross_days_ago (precondition: macd_line slope {MACD_LINE_SLOPE_DAYS}hari POSITIF, masih <0, cross 0-{CROSS_LOOKBACK_DAYS}hari lalu)")
    print("=" * 82)
    df["bin"] = df["cross_days_ago"].apply(_bin_label)
    for lo, hi in CROSS_DAYS_BINS:
        label = f"{lo}-{hi} hari lalu"
        g = df[df["bin"] == label]
        if g.empty:
            print(f"  {label:<16} n=0")
            continue
        line = f"  {label:<16} n={len(g):<6}"
        for h in FORWARD_HORIZONS:
            line += f" | fwd{h}d avg={g[f'fwd_{h}d'].mean():+5.2f}%"
        print(line)

    print("\nBaca ini: cari bin mana yang forward return-nya PALING TINGGI (bukan cuma bin")
    print("pertama/terakhir) -- itu 'sweet spot' cross_days_ago yang layak jadi syarat SDT.")
    print("Bandingkan juga tiap bin vs baseline di atas -- kalau semua bin cuma seputar")
    print("baseline, precondition ini TIDAK ADA edge riil, cuma ikut tren pasar umum.")

    print("\n" + "=" * 82)
    print("EFEK SQUEEZE (cohort split, BUKAN filter)")
    print("=" * 82)
    for squeeze_flag, label in [(True, "DENGAN squeeze"), (False, "TANPA squeeze")]:
        g = df[df["squeeze"] == squeeze_flag]
        if g.empty:
            print(f"  {label:<18} n=0")
            continue
        line = f"  {label:<18} n={len(g):<6}"
        for h in FORWARD_HORIZONS:
            line += f" | fwd{h}d avg={g[f'fwd_{h}d'].mean():+5.2f}%"
        print(line)


if __name__ == "__main__":
    main()
