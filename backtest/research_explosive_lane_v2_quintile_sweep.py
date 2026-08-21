"""
backtest/research_explosive_lane_v2_quintile_sweep.py — MBSS v2, user
request lanjutan setelah _explosive_score (Explosive Lane) diganti formula
baru (commands/scan.py) berbasis 4 dimensi dari research_macd_explosive_
gain_profile.py: dist_to_sma50_pct, day_range_pct_10d, macd_slope_pct,
ret_5d_pct. Formula BARU pakai percentile-linear (persentil makin tinggi
makin baik, TANPA batas) -- asumsi ini BELUM divalidasi bentuknya. Sesi ini
sudah pernah nemu kasus slope MAGNITUDE yang TIDAK monoton (Q5/paling
curam sedikit LEBIH JELEK dari Q4, research_macd_approach_depth_and_range.py
Report D) -- perlu dicek APAKAH pola serupa (jenuh/reversal di ekor
tertinggi) berlaku juga di 4 dimensi baru ini, SEBELUM formula linear-tanpa-
batas dipercaya penuh.

Populasi: SAMA dengan research_macd_explosive_gain_profile.py (SETIAP hari
selama regime histogram MACD bullish aktif). Outcome: max_gain_pct dalam 5
hari ke depan (SAMA definisi).

Quintile sweep INDEPENDEN untuk masing-masing dari 4 dimensi (bukan cross-
tab -- tujuannya cuma cek BENTUK hubungan tiap dimensi, monoton naik terus
vs ada titik jenuh/turun di ekor tertinggi).

Run di server:
    python backtest/research_explosive_lane_v2_quintile_sweep.py

Pakai OHLCV lokal saja, zero fetch/network cost. Murni observasi, belum
mengubah formula produksi apa pun (formula produksi SUDAH diganti duluan,
atas dasar Cohen's d yang sudah kuat -- script ini validasi TAMBAHAN untuk
kalibrasi lanjutan, misal cap persentil kalau ekor ternyata jenuh).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import engine.legacy_core as core
import engine.nightly as nightly_engine

FORWARD_WINDOW_DAYS = 5
MIN_BARS = 150
WARMUP_BARS = 55
EVAL_TAIL_BUFFER = FORWARD_WINDOW_DAYS + 2
N_QUINTILES = 5

DIMENSIONS = ["dist_to_sma50_pct", "day_range_pct_10d", "macd_slope_pct", "ret_5d_pct"]


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

        high = hist_df["High"].astype(float)
        low = hist_df["Low"].astype(float)
        close = hist_df["Close"].astype(float)

        n = len(hist_df)
        eval_end = n - EVAL_TAIL_BUFFER
        if eval_end <= WARMUP_BARS:
            continue

        macd_line, _, macd_hist = core.calculate_macd(close)
        sma50 = close.rolling(50).mean()
        day_range_10d_s = (high.rolling(10).max() - low.rolling(10).min()) / low.rolling(10).min().replace(0, np.nan) * 100
        macd_slope_s = macd_line.diff(3)
        ret5d_s = close.pct_change(5) * 100

        for i in range(WARMUP_BARS, eval_end):
            if not (macd_hist.iloc[i] > 0):
                continue
            price_i = float(close.iloc[i])
            if price_i <= 0:
                continue

            sma50_i = sma50.iloc[i]
            dist_sma50 = (price_i - sma50_i) / sma50_i * 100 if sma50_i else np.nan
            macd_slope_pct = float(macd_slope_s.iloc[i]) / price_i * 100 if pd.notna(macd_slope_s.iloc[i]) else np.nan

            fwd_window_end = min(i + FORWARD_WINDOW_DAYS, n - 1)
            max_price_fwd = float(close.iloc[i:fwd_window_end + 1].max())
            max_gain_pct = round((max_price_fwd - price_i) / price_i * 100, 2)

            events.append({
                "ticker": ticker,
                "date": str(hist_df.index[i].date()),
                "dist_to_sma50_pct": dist_sma50,
                "day_range_pct_10d": day_range_10d_s.iloc[i],
                "macd_slope_pct": macd_slope_pct,
                "ret_5d_pct": ret5d_s.iloc[i],
                "max_gain_pct": max_gain_pct,
                "explosive_10": max_gain_pct >= 10.0,
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
    print(f"Total observasi (regime MACD bullish aktif): {len(df)}\n")
    if df.empty:
        print("⚠️ Tidak ada observasi.")
        return

    for dim in DIMENSIONS:
        g = df.dropna(subset=[dim]).copy()
        if len(g) < N_QUINTILES * 20:
            print(f"⚠️ {dim}: sampel terlalu kecil.")
            continue
        try:
            g["q"] = pd.qcut(g[dim], N_QUINTILES, duplicates="drop")
        except ValueError:
            print(f"⚠️ {dim}: gagal bikin quintile.")
            continue

        print("=" * 96)
        print(f"QUINTILE: {dim}")
        print("=" * 96)
        for q, sub in sorted(g.groupby("q", observed=True), key=lambda kv: kv[0].left):
            wr = sub["explosive_10"].mean() * 100
            avg_gain = sub["max_gain_pct"].mean()
            median_gain = sub["max_gain_pct"].median()
            print(f"  {str(q):<26} n={len(sub):<6} | explosive-rate={wr:5.1f}% | avg_gain={avg_gain:+6.2f}% | median_gain={median_gain:+6.2f}%")
        print()

    print("Baca ini: kalau quintile TERTINGGI (Q5) punya avg_gain/explosive-rate PALING TINGGI dari")
    print("semua quintile -- monoton bersih, formula percentile-linear (produksi sekarang) SUDAH pas,")
    print("tidak perlu diubah. Kalau Q5 justru TURUN dari Q4 (seperti kasus slope magnitude minggu")
    print("lalu) -- ada titik jenuh, pertimbangkan CAP persentil (mis. skor maksimal dicapai di ~P80,")
    print("bukan P100) supaya tidak over-reward kandidat paling ekstrem yang justru sedikit lebih lemah.")


if __name__ == "__main__":
    main()
