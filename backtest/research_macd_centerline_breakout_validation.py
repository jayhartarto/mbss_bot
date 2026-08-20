"""
backtest/research_macd_centerline_breakout_validation.py — MBSS v2, user
request lanjutan setelah macd_approach_tier (SWEET_SPOT/SQUEEZE_RESCUE)
diimplementasi ke /screendaytrade. research_macd_approach_score_v1/v2/v3.py
mengukur forward return SELAMA MACD line MASIH DI BAWAH centerline (fase
"approach", belum breakout) — dibucket per cross_days_ago (berapa hari
sejak signal-line cross histogram).

Script ini mengukur hal yang BEDA dan komplementer: PADA SAAT MACD line
BENERAN cross centerline (macd_line lewat 0), apa yang terjadi SESUDAHNYA?
- Berapa persen harga naik >2% dalam 5 hari ke depan dari HARI CROSSING itu
  sendiri (bukan dari hari approach).
- Berapa persen MACD line MASIH di atas centerline 5 hari kemudian (tidak
  langsung balik turun/gagal) DAN berapa persen MACD line masih terus naik
  (bukan cuma bertahan tipis di atas 0).
Dibucket per "days_from_signal_cross_to_centerline" — berapa hari yang
dibutuhkan DARI signal-line cross AWAL (histogram lewat 0, definisi SAMA
dengan macd_cross_days_ago yang sudah live) SAMPAI ke centerline cross ini
terjadi. Tujuan: apakah cross yang "matang" (butuh waktu lebih lama sampai
ke centerline, ~14-23 hari, sesuai temuan SWEET_SPOT) BENERAN menghasilkan
follow-through breakout lebih baik dibanding cross yang "cepat"/spike
(0-3 hari langsung tembus centerline)?

Run di server:
    python backtest/research_macd_centerline_breakout_validation.py

Pakai OHLCV lokal saja (get_ohlcv_daily_from_db), zero fetch/network cost.
Murni observasi -- belum mengubah formula produksi apa pun.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import engine.legacy_core as core
import engine.nightly as nightly_engine

SIGNAL_CROSS_LOOKBACK_DAYS = 40  # sama dengan v3, batas wajar cari signal-line cross yang mendahului centerline cross ini
FORWARD_HORIZON = 5              # user minta span 5d
BREAKOUT_THRESHOLD_PCT = 2.0     # user minta >2%
STILL_ABOVE_HORIZON = 5          # cek MACD line masih di atas centerline berapa hari setelah cross
MIN_BARS = 150
EVAL_TAIL_BUFFER = max(FORWARD_HORIZON, STILL_ABOVE_HORIZON) + 2

DAYS_TO_CENTERLINE_BINS = [(0, 3), (4, 7), (8, 11), (12, 15), (16, 19), (20, 23), (24, 27), (28, 31), (32, 40)]


def _find_recent_bullish_cross_days_ago(macd_hist: pd.Series, i: int, max_days_back: int) -> int | None:
    """REUSE exact logic dari macd_cross_days_ago (engine/scoring.py) / v1-v3 backtest scripts."""
    if i < 1 or macd_hist.iloc[i] <= 0:
        return None
    for days_back in range(1, min(max_days_back + 1, i + 1)):
        idx = i - days_back
        if idx < 0:
            break
        if not (macd_hist.iloc[idx] > 0):
            return days_back
    return None  # regime sudah berlangsung > max_days_back hari


def _bin_label(days: int) -> str:
    for lo, hi in DAYS_TO_CENTERLINE_BINS:
        if lo <= days <= hi:
            return f"{lo}-{hi}"
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

        n = len(hist_df)
        eval_end = n - EVAL_TAIL_BUFFER
        if eval_end <= 1:
            continue

        for i in range(1, eval_end):
            # Centerline cross HARI INI: kemarin masih <0, hari ini >=0.
            if not (macd_line.iloc[i - 1] < 0 and macd_line.iloc[i] >= 0):
                continue
            # Precondition wajar: histogram positif saat centerline cross ini
            # terjadi (urutan standar signal-line cross -> centerline cross,
            # lihat riset sesi sebelumnya) -- kalau tidak, event ini di luar
            # scope yang mau divalidasi (kemungkinan pola tidak biasa).
            if not (macd_hist.iloc[i] > 0):
                continue

            days_to_centerline = _find_recent_bullish_cross_days_ago(macd_hist, i, SIGNAL_CROSS_LOOKBACK_DAYS)
            if days_to_centerline is None:
                continue  # signal-line cross-nya di luar window 40 hari, atau tidak ketemu

            price_i = closes.iloc[i]
            fwd_return = round((closes.iloc[i + FORWARD_HORIZON] - price_i) / price_i * 100, 2)
            hit_breakout = fwd_return >= BREAKOUT_THRESHOLD_PCT
            still_above = bool(macd_line.iloc[i + STILL_ABOVE_HORIZON] >= 0)
            still_rising = bool(macd_line.iloc[i + STILL_ABOVE_HORIZON] > macd_line.iloc[i])

            rows.append({
                "ticker": ticker,
                "date": str(hist_df.index[i].date()),
                "days_to_centerline": days_to_centerline,
                "fwd_return": fwd_return,
                "hit_breakout": hit_breakout,
                "still_above_centerline": still_above,
                "still_rising": still_rising,
            })

    print(f"Ticker dilewati (histori lokal <{MIN_BARS} bar): {skipped}/{len(tickers)}")
    print(f"Total event centerline cross (bullish) yang lolos precondition: {len(rows)}\n")

    if not rows:
        print("⚠️ TIDAK ADA event centerline cross yang lolos precondition.")
        return

    df = pd.DataFrame(rows)

    print("=" * 96)
    print(f"BASELINE AGREGAT (semua event centerline cross bullish, tanpa dipilah bucket)")
    print("=" * 96)
    print(
        f"  n={len(df):<6} | fwd{FORWARD_HORIZON}d avg={df['fwd_return'].mean():+5.2f}% "
        f"| hit >{BREAKOUT_THRESHOLD_PCT}% @{FORWARD_HORIZON}d = {df['hit_breakout'].mean() * 100:5.1f}% "
        f"| masih di atas centerline @{STILL_ABOVE_HORIZON}d = {df['still_above_centerline'].mean() * 100:5.1f}% "
        f"| MACD line masih naik @{STILL_ABOVE_HORIZON}d = {df['still_rising'].mean() * 100:5.1f}%"
    )

    print("\n" + "=" * 96)
    print("BUCKET per days_from_signal_cross_to_centerline (makin lama sampai centerline = makin 'matang'?)")
    print("=" * 96)
    df["bin"] = df["days_to_centerline"].apply(_bin_label)
    for lo, hi in DAYS_TO_CENTERLINE_BINS:
        label = f"{lo}-{hi}"
        g = df[df["bin"] == label]
        if g.empty:
            print(f"  {label:<8} hari sampai centerline  n=0")
            continue
        print(
            f"  {label:<8} hari sampai centerline  n={len(g):<6} "
            f"| fwd{FORWARD_HORIZON}d avg={g['fwd_return'].mean():+5.2f}% "
            f"| hit >{BREAKOUT_THRESHOLD_PCT}% = {g['hit_breakout'].mean() * 100:5.1f}% "
            f"| masih di atas centerline = {g['still_above_centerline'].mean() * 100:5.1f}% "
            f"| MACD masih naik = {g['still_rising'].mean() * 100:5.1f}%"
        )

    print("\nBaca ini: kalau bucket 14-23 hari (rentang SWEET_SPOT yang sudah dipakai /screendaytrade)")
    print("PUNYA hit-rate breakout & 'masih di atas centerline' yang JELAS lebih baik dari bucket")
    print("0-3/4-7 hari (cross cepat/spike) -- itu KONFIRMASI TAMBAHAN bahwa SWEET_SPOT genuinely")
    print("menangkap setup yang lebih matang, bukan cuma kebetulan di fase approach saja. Kalau")
    print("berantakan/tidak ada beda jelas, follow-through PASCA-centerline TIDAK terkait dengan")
    print("berapa lama fase approach-nya -- dua hal yang independen, jangan disatukan kesimpulannya.")


if __name__ == "__main__":
    main()
