"""
backtest/research_macd_pullback_resume_threshold.py — MBSS v2, user request
lanjutan setelah research_macd_cross_winner_profile_v2.py.

v2 menemukan sub-populasi SIGNAL_CROSS "ABOVE_CENTERLINE" (pullback-resume
dalam uptrend mapan -- MACD line SUDAH di atas centerline saat histogram
cross terjadi) punya win-rate jauh lebih tinggi (24.6% vs 16.2% n=1458/2764)
DAN effect size jauh lebih besar di hampir semua fitur dibanding sub-populasi
BELOW_CENTERLINE (approach) yang jadi basis SWEET_SPOT/SQUEEZE_RESCUE yang
sudah live. TIGA fitur teratas by Cohen's d di situ:
  dist_to_sma50_pct        d=0.717
  dist_to_ema21_pct        d=0.693 (korelasi tinggi dgn dist_to_sma50, redundant secara struktural)
  macd_line_pct_of_price   d=0.659
  rsi                      d=0.570

Script ini BUKAN mengulang Cohen's-d ranking (sudah dilakukan) -- ini
BUCKET SWEEP tiga fitur utama (dist_to_sma50_pct, macd_line_pct_of_price,
rsi) dalam populasi ABOVE_CENTERLINE itu sendiri, supaya threshold gate
yang diusulkan nanti punya dasar dari BENTUK relasinya (monoton naik terus?
ada titik jenuh/sweet-spot lalu turun, seperti cross_days_ago dulu?), BUKAN
cuma dari mean winner vs loser. Sama disiplin dengan pencarian sweet-spot
cross_days_ago sebelumnya (research_macd_approach_score_v2/v3.py) -- jangan
tebak angka cutoff dari perbandingan mean saja.

Quintile DATA-DRIVEN (dari distribusi populasi ABOVE_CENTERLINE itu sendiri,
bukan angka tetap yang dikarang) per fitur, dilaporkan: n, win-rate (naik
>=4% dlm 3hari), avg max_fwd_return.

Run di server:
    python backtest/research_macd_pullback_resume_threshold.py

Pakai OHLCV lokal saja, zero fetch/network cost. Murni observasi, belum
mengubah formula produksi apa pun.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import engine.legacy_core as core
import engine.nightly as nightly_engine

FORWARD_DAYS = 3
BREAKOUT_THRESHOLD_PCT = 4.0
MIN_BARS = 150
WARMUP_BARS = 55
EVAL_TAIL_BUFFER = FORWARD_DAYS + 2
N_QUINTILES = 5

THRESHOLD_FEATURES = ["dist_to_sma50_pct", "macd_line_pct_of_price", "rsi"]


def collect_above_centerline_events(tickers: list) -> pd.DataFrame:
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
        volume = hist_df["Volume"].astype(float)

        n = len(hist_df)
        eval_end = n - EVAL_TAIL_BUFFER
        if eval_end <= WARMUP_BARS:
            continue

        macd_line, signal_line, macd_hist = core.calculate_macd(close)
        rsi_s = core.calculate_rsi(close)
        sma50 = close.rolling(50).mean()

        for i in range(WARMUP_BARS, eval_end):
            signal_cross = bool(macd_hist.iloc[i - 1] <= 0 and macd_hist.iloc[i] > 0)
            if not signal_cross:
                continue
            if not (macd_line.iloc[i] >= 0):  # scope: HANYA sub-populasi ABOVE_CENTERLINE
                continue

            price_i = float(close.iloc[i])
            if price_i <= 0:
                continue
            sma50_i = sma50.iloc[i]
            if not sma50_i or pd.isna(sma50_i):
                continue

            fwd_returns = [
                (float(close.iloc[i + k]) - price_i) / price_i * 100
                for k in range(1, FORWARD_DAYS + 1)
            ]
            hit = any(r >= BREAKOUT_THRESHOLD_PCT for r in fwd_returns)

            events.append({
                "ticker": ticker,
                "date": str(hist_df.index[i].date()),
                "hit": hit,
                "max_fwd_return": round(max(fwd_returns), 2),
                "dist_to_sma50_pct": (price_i - sma50_i) / sma50_i * 100,
                "macd_line_pct_of_price": float(macd_line.iloc[i]) / price_i * 100,
                "rsi": rsi_s.iloc[i],
            })

    print(f"Ticker dilewati (histori lokal <{MIN_BARS} bar): {skipped}/{len(tickers)}")
    return pd.DataFrame(events)


def report_quintiles(df: pd.DataFrame, feature: str):
    g = df.dropna(subset=[feature]).copy()
    if len(g) < N_QUINTILES * 10:
        print(f"\n⚠️ {feature}: sampel terlalu kecil buat quintile ({len(g)}).")
        return

    try:
        g["bucket"] = pd.qcut(g[feature], N_QUINTILES, duplicates="drop")
    except ValueError:
        print(f"\n⚠️ {feature}: gagal bikin quintile (distribusi terlalu sempit/banyak nilai sama).")
        return

    print(f"\n--- {feature} (quintile, data-driven dari distribusi populasi ini sendiri) ---")
    print(f"{'Range':<28} {'n':>6} {'win-rate':>10} {'avg max_fwd':>14}")
    for bucket, sub in g.groupby("bucket", observed=True):
        if sub.empty:
            continue
        wr = sub["hit"].mean() * 100
        avg_fwd = sub["max_fwd_return"].mean()
        print(f"{str(bucket):<28} {len(sub):>6} {wr:>9.1f}% {avg_fwd:>13.2f}%")


def main():
    scored, _ = nightly_engine.load_daily_scan_cache_allow_stale()
    tickers = sorted(scored.keys()) if scored else []
    print(f"Universe: {len(tickers)} ticker (dari cache /eodscan terakhir)\n")
    if not tickers:
        print("⚠️ Cache /eodscan kosong — jalankan /eodscan dulu di server.")
        return

    df = collect_above_centerline_events(tickers)
    print(f"Total event SIGNAL_CROSS + ABOVE_CENTERLINE: {len(df)}")
    if df.empty:
        print("⚠️ Tidak ada event.")
        return
    overall_wr = df["hit"].mean() * 100
    print(f"Baseline win-rate populasi ini (tanpa threshold tambahan apa pun): {overall_wr:.1f}%\n")

    for feat in THRESHOLD_FEATURES:
        report_quintiles(df, feat)

    print("\nBaca ini: cari quintile mana yang win-rate-nya PALING TINGGI per fitur -- kalau di")
    print("quintile TERTINGGI (nilai fitur paling besar) DAN monoton naik dari quintile 1->5,")
    print("threshold gate yang wajar adalah 'di atas quintile median/atas'. Kalau ada titik jenuh")
    print("(naik lalu turun/plateau sebelum quintile terakhir), itu sweet-spot genuine -- JANGAN")
    print("pakai 'semakin tinggi semakin baik tanpa batas' kalau datanya tidak bilang begitu.")


if __name__ == "__main__":
    main()
