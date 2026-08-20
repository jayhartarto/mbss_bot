"""
backtest/research_macd_squeeze_frequency.py — MBSS v2, user request ("saya
kuatir filtering ini malah tidak mengeluarkan rekomendasi sama sekali, apa
bisa kita uji dulu dengan data yang ada?"). SEBELUM macd_approaching_cross
diimplementasi ke compute_factor_scoring, uji dulu FREKUENSI kandidat yang
lolos kriteria draft:

    macd_hist < 0 (belum cross) DAN menyusut HIST_SHRINK_DAYS hari
    berturut-turut (magnitude negatif mengecil monoton) DAN bollinger_squeeze
    (bandwidth persentil <=20% histori ~6bln) — persis definisi draft dari
    diskusi sebelumnya, SMA50 SUDAH dicabut (tidak relevan buat sinyal early
    signal-line cross ini, lihat riset sesi sebelumnya).

Run di server:
    python backtest/research_macd_squeeze_frequency.py

BUKAN backtest winrate — sinyal ini belum pernah jadi pick, tidak ada
histori /winrate buat ditelusuri. Murni tes COVERAGE: apakah kriteria ini
genuinely pernah muncul di data IDX riil, seberapa sering, dan seberapa
restriktif syarat squeeze dibanding tanpa squeeze (supaya kalau hasilnya
nol/nyaris nol, kita tahu PERSIS bagian mana yang perlu dilonggarkan
sebelum implementasi, bukan menebak).

Pakai OHLCV LOKAL yang sudah ada di SQLite (get_ohlcv_daily_from_db) —
TIDAK fetch apa pun, zero network/API cost, sama disiplin dengan
research_ema9_productivity.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import engine.legacy_core as core
import engine.nightly as nightly_engine

LOOKBACK_DAYS = 30   # berapa hari trading terakhir per ticker yang diuji
HIST_SHRINK_DAYS = 3  # syarat draft: histogram negatif mengecil N hari berturut-turut
MIN_BARS = 150  # minimal bar lokal supaya squeeze percentile (butuh trailing histori) valid


def _bandwidth_series(closes: pd.Series) -> pd.Series:
    """Sama persis definisi compute_factor_scoring (engine/scoring.py) — bandwidth = (upper-lower)/sma20."""
    sma20 = closes.rolling(20).mean()
    std20 = closes.rolling(20).std()
    upper = sma20 + 2 * std20
    lower = sma20 - 2 * std20
    return (upper - lower) / sma20


def _is_squeeze_on_day(bandwidth: pd.Series, i: int) -> bool:
    """Persentil bandwidth[i] vs histori SEBELUM hari itu (trailing MIN_HISTORY_FOR_ADAPTIVE) — sama definisi bollinger_squeeze."""
    trailing = bandwidth.iloc[max(0, i - core.MIN_HISTORY_FOR_ADAPTIVE):i].dropna()
    if len(trailing) < 20:
        return False
    pct = core.percentile_rank(trailing, bandwidth.iloc[i])
    return pct <= 0.20


def _hist_shrinking(hist: pd.Series, i: int, days: int) -> bool:
    """Histogram negatif SELURUH window, magnitude mengecil (naik monoton menuju 0) `days` hari berturut-turut menuju hari ke-i."""
    if i < days:
        return False
    window = hist.iloc[i - days: i + 1]
    if (window >= 0).any():
        return False
    return all(window.iloc[k] > window.iloc[k - 1] for k in range(1, len(window)))


def main():
    scored, _ = nightly_engine.load_daily_scan_cache_allow_stale()
    tickers = sorted(scored.keys()) if scored else []
    print(f"Universe: {len(tickers)} ticker (dari cache /eodscan terakhir)\n")
    if not tickers:
        print("⚠️ Cache /eodscan kosong — jalankan /eodscan dulu di server.")
        return

    total_ticker_days = 0
    fired_without_squeeze = 0  # syarat histogram menyusut SAJA -- pembanding, seberapa restriktif syarat squeeze
    fired_with_squeeze = 0     # kriteria draft PENUH (histogram menyusut + squeeze)
    fired_tickers = {}         # ticker -> [tanggal, ...] yang lolos kriteria penuh
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
        _, _, macd_hist = core.calculate_macd(closes)
        bandwidth = _bandwidth_series(closes)

        n = len(hist_df)
        start = max(HIST_SHRINK_DAYS, n - LOOKBACK_DAYS)
        for i in range(start, n):
            total_ticker_days += 1
            if not _hist_shrinking(macd_hist, i, HIST_SHRINK_DAYS):
                continue
            fired_without_squeeze += 1
            if _is_squeeze_on_day(bandwidth, i):
                fired_with_squeeze += 1
                fired_tickers.setdefault(ticker, []).append(str(hist_df.index[i].date()))

    print("=" * 70)
    print(f"Ticker-hari diuji total: {total_ticker_days} ({len(tickers) - skipped}/{len(tickers)} ticker punya data lokal cukup, {skipped} dilewati karena histori <{MIN_BARS} bar)")
    print(f"Lolos 'histogram menyusut {HIST_SHRINK_DAYS} hari' SAJA (tanpa squeeze): {fired_without_squeeze} ({fired_without_squeeze / max(1, total_ticker_days) * 100:.2f}% dari ticker-hari)")
    print(f"Lolos + bollinger_squeeze juga (kriteria draft PENUH): {fired_with_squeeze} ({fired_with_squeeze / max(1, total_ticker_days) * 100:.2f}%), {len(fired_tickers)} ticker unik pernah kena")
    print("=" * 70)

    if fired_tickers:
        print("\nContoh ticker yang pernah kena sinyal (tanggal terakhir, maks 3):")
        for t, dates in list(fired_tickers.items())[:20]:
            print(f"  {t}: {', '.join(dates[-3:])}")
        print(f"\n(total {len(fired_tickers)} ticker unik ditemukan di atas {LOOKBACK_DAYS} hari terakhir)")
    else:
        print(
            "\n⚠️ TIDAK ADA ticker yang lolos kriteria draft PENUH dalam window ini. "
            "Bandingkan dengan baris 'tanpa squeeze' di atas: kalau baris itu JUGA nol, "
            "masalahnya ada di syarat histogram-menyusut (HIST_SHRINK_DAYS mungkin "
            "terlalu ketat). Kalau baris 'tanpa squeeze' PUNYA hasil tapi baris "
            "'dengan squeeze' nol, syarat squeeze-lah yang terlalu ketat -- "
            "pertimbangkan longgarkan persentil bollinger_squeeze (mis. <=30% bukan "
            "<=20%) atau HIST_SHRINK_DAYS=2."
        )


if __name__ == "__main__":
    main()
