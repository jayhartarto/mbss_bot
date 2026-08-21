"""
backtest/research_macd_day_before_cross_distance_range.py — MBSS v2, user
request lanjutan setelah outlook hari-ke-cross terbukti terlalu lebar
(P25-P75: 8-20 hari, tidak ada puncak tajam -- H-1/H-2 by SCHEDULE tidak
bisa diandalkan, lihat research_macd_far_steep_days_to_cross_outlook.py).

Reformulasi user, BEDA SUMBU total dari waktu (hari) ke JARAK (persentase):
alih-alih menebak "hari ke berapa", ukur langsung "SEHARI SEBELUM centerline
BENAR-BENAR di-cross, seberapa jauh macd_line dari centerline (dalam %)?"
Kalau jarak ini konsisten di rentang sempit lintas ratusan kejadian cross
riil, itu jadi PEMICU LIVE berbasis JARAK (dicek tiap hari saat /eodscan
jalan, bukan dijadwalkan N hari ke depan) -- SDT bisa surface ulang ticker
begitu jaraknya masuk rentang itu, HARI APAPUN itu terjadi, tanpa perlu
menebak kapan.

Populasi: SEMUA event centerline cross bullish riil (macd_line<0 -> >=0,
histogram bullish saat itu -- precondition SAMA dengan research_macd_cross_
winner_profile_v1/v2.py's CENTERLINE_CROSS, populasi JAUH lebih besar
daripada yang lewat FAR+STEEP saja, supaya distribusi jarak lebih presisi).
depth_before = macd_line_pct_of_price di HARI SEBELUM cross (i-1) -- field
yang SAMA persis dipakai di seluruh studi MACD sesi ini.

Dua laporan:
  A. Distribusi depth_before KESELURUHAN (persentil + histogram bin) --
     jawaban langsung "rentang seberapa jauh, tipikalnya".
  B. Win-rate per quintile depth_before (WIN = max harga naik dalam 5 hari
     setelah cross > harga hari SEBELUM cross) -- apakah rentang TERTENTU
     (bukan cuma "yang mana paling umum") justru berkorelasi win-rate lebih
     tinggi, jadi acuan band yang lebih baik dari sekadar "paling sering
     muncul".

Run di server:
    python backtest/research_macd_day_before_cross_distance_range.py

Pakai OHLCV lokal saja, zero fetch/network cost. Murni observasi, belum
mengubah formula produksi apa pun.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import engine.legacy_core as core
import engine.nightly as nightly_engine

MIN_BARS = 150
RESOLVE_WINDOW_DAYS = 5
DEPTH_BINS = [
    (0.0, 0.1), (0.1, 0.25), (0.25, 0.5), (0.5, 1.0),
    (1.0, 2.0), (2.0, 5.0), (5.0, 10.0), (10.0, 1000.0),
]  # dalam NILAI ABSOLUT (jarak, positif) -- makin kecil makin dekat centerline


def _bin_label(dist_abs: float) -> str:
    for lo, hi in DEPTH_BINS:
        if lo <= dist_abs < hi:
            return f"{lo}-{hi}%" if hi < 1000 else f">{lo}%"
    return "?"


def collect_events(tickers: list) -> pd.DataFrame:
    events = []
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

        close = hist_df["Close"].astype(float)
        n = len(hist_df)
        eval_end = n - RESOLVE_WINDOW_DAYS - 1
        if eval_end <= 1:
            continue

        macd_line, _, macd_hist = core.calculate_macd(close)

        for i in range(1, eval_end):
            if not (macd_line.iloc[i - 1] < 0 and macd_line.iloc[i] >= 0):
                continue
            if not (macd_hist.iloc[i] > 0):
                continue  # precondition SAMA dgn CENTERLINE_CROSS di script2 sebelumnya

            price_before = float(close.iloc[i - 1])
            if price_before <= 0:
                continue
            depth_before = float(macd_line.iloc[i - 1]) / price_before * 100  # negatif
            dist_abs = abs(depth_before)

            window_end = min(i + RESOLVE_WINDOW_DAYS, n - 1)
            max_price = float(close.iloc[i:window_end + 1].max())
            max_gain_pct = round((max_price - price_before) / price_before * 100, 2)
            win = bool(max_gain_pct > 0)

            events.append({
                "ticker": ticker,
                "date": str(hist_df.index[i].date()),
                "depth_before": depth_before,
                "dist_abs": dist_abs,
                "max_gain_pct": max_gain_pct,
                "win": win,
            })

    print(f"Ticker dilewati (histori lokal <{MIN_BARS} bar): {skipped}/{len(tickers)}")
    return pd.DataFrame(events)


def main():
    scored, _ = nightly_engine.load_daily_scan_cache_allow_stale()
    tickers = sorted(scored.keys()) if scored else []
    print(f"Universe: {len(tickers)} ticker (dari cache /eodscan terakhir)\n")
    if not tickers:
        print("⚠️ Cache /eodscan kosong — jalankan /eodscan dulu di server.")
        return

    df = collect_events(tickers)
    print(f"Total event centerline cross bullish riil: {len(df)}\n")
    if df.empty:
        print("⚠️ Tidak ada event.")
        return

    print("=" * 90)
    print("A. DISTRIBUSI depth_before (jarak macd_line ke centerline, SEHARI SEBELUM cross)")
    print("=" * 90)
    d = df["dist_abs"]
    print(f"  n={len(d)} | min={d.min():.3f}% | P10={d.quantile(0.10):.3f}% | P25={d.quantile(0.25):.3f}% | "
          f"MEDIAN={d.median():.3f}% | P75={d.quantile(0.75):.3f}% | P90={d.quantile(0.90):.3f}% | max={d.max():.3f}%")
    print("\n  Histogram (bin jarak absolut):")
    df["bin"] = df["dist_abs"].apply(_bin_label)
    for lo, hi in DEPTH_BINS:
        label = f"{lo}-{hi}%" if hi < 1000 else f">{lo}%"
        g = df[df["bin"] == label]
        if g.empty:
            continue
        pct = len(g) / len(df) * 100
        bar = "#" * int(pct / 2)
        print(f"    {label:<10} n={len(g):<5} ({pct:4.1f}%) {bar}")

    print("\n" + "=" * 90)
    print("B. WIN-RATE per quintile depth_before (apakah rentang TERTENTU lebih baik dari sekadar 'paling umum'?)")
    print("=" * 90)
    df["depth_q"] = pd.qcut(df["dist_abs"], 5, duplicates="drop")
    for q, g in sorted(df.groupby("depth_q", observed=True), key=lambda kv: kv[0].left):
        wr = g["win"].mean() * 100
        avg_gain = g["max_gain_pct"].mean()
        median_gain = g["max_gain_pct"].median()
        print(f"  jarak {str(q):<22} n={len(g):<6} | win-rate={wr:5.1f}% | avg_gain={avg_gain:+6.2f}% | median_gain={median_gain:+6.2f}%")

    print("\nBaca ini: kalau quintile jarak PALING DEKAT (nilai kecil) punya win-rate & gain JELAS lebih")
    print("baik dari quintile jauh -- 'sehari sebelum cross, jarak X%' genuinely bisa jadi pemicu live")
    print("harian (BUKAN jadwal hari, tapi threshold jarak yang dicek tiap /eodscan). Kalau berantakan/")
    print("flat -- jarak sehari sebelum cross TIDAK cukup prediktif dipakai sendirian sebagai pemicu.")


if __name__ == "__main__":
    main()
