"""
backtest/research_macd_histogram_thin_before_flip.py — MBSS v2, user
request: mengurangi noise SDT dari kandidat yang histogram bearish-nya
"masih tebal/parabolic" saat flip ke bullish. Klarifikasi user penting:
BUKAN berarti histori bearish yang DALAM harus dibuang (boleh saja stock
pernah sangat bearish jauh sebelumnya) -- yang dimaksud adalah: CALL
produksi ke SDT baru layak ketika histogram SUDAH TIPIS (magnitude kecil,
mendekati nol) PERSIS SEBELUM flip terjadi, bukan flip mendadak dari
histogram yang masih dalam/tebal.

Ini hipotesis BARU, belum pernah diuji di sesi ini (beda dari semua studi
macd_line/centerline sebelumnya yang fokus ke SETELAH histogram sudah
positif) -- fokusnya di sini justru fase SEBELUM flip (histogram masih
negatif, tapi seberapa dalam/tipis PERSIS sebelum berbalik).

Populasi: semua SIGNAL_CROSS (histogram bullish cross, macd_hist <=0 -> >0)
di ticker/hari manapun -- precondition SAMA dengan research_macd_cross_
winner_profile_v1.py's SIGNAL_CROSS (populasi besar, statistik kuat).

Dua metrik dihitung SEHARI SEBELUM flip (hari i-1, histogram masih
negatif):
  - thinness_abs = |macd_hist_pct_of_price| di hari i-1 -- makin KECIL
    makin "tipis" (dekat nol), makin BESAR makin "tebal/dalam".
  - decelerating = apakah thinness SEKARANG (i-1) lebih kecil dari
    thinness 3 hari sebelumnya (i-4) -- histogram SEDANG MENYUSUT menuju
    nol (deselerasi halus) vs TIDAK (masih dalam/melebar, "parabolic"
    dalam pengertian belum melandai sampai detik terakhir).

Dua laporan:
  A. Quintile thinness_abs -- forward return fwd3/5/10d dari HARI FLIP itu
     sendiri (bukan sehari sebelum) -- apakah histogram tipis SEBELUM flip
     memprediksi hasil lebih baik SETELAH flip?
  B. Cross-tab thinness tercile x decelerating (YA/TIDAK) -- apakah tren
     MENYUSUT (bukan cuma nilai statis) menambah info tambahan?

Run di server:
    python backtest/research_macd_histogram_thin_before_flip.py

Pakai OHLCV lokal saja, zero fetch/network cost. Murni observasi, belum
mengubah formula produksi apa pun.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import engine.legacy_core as core
import engine.nightly as nightly_engine

FORWARD_HORIZONS = (3, 5, 10)
MIN_BARS = 150
EVAL_TAIL_BUFFER = max(FORWARD_HORIZONS) + 2
DECEL_LOOKBACK_DAYS = 3


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
        eval_end = n - EVAL_TAIL_BUFFER
        if eval_end <= DECEL_LOOKBACK_DAYS + 1:
            continue

        macd_line, _, macd_hist = core.calculate_macd(close)

        for i in range(DECEL_LOOKBACK_DAYS + 1, eval_end):
            # SIGNAL_CROSS: histogram baru lewat 0 hari ini.
            if not (macd_hist.iloc[i - 1] <= 0 and macd_hist.iloc[i] > 0):
                continue

            price_before = float(close.iloc[i - 1])
            price_3d_ago = float(close.iloc[i - 1 - DECEL_LOOKBACK_DAYS])
            if price_before <= 0 or price_3d_ago <= 0:
                continue

            thinness_now = abs(float(macd_hist.iloc[i - 1]) / price_before * 100)
            thinness_3d_ago = abs(float(macd_hist.iloc[i - 1 - DECEL_LOOKBACK_DAYS]) / price_3d_ago * 100)
            decelerating = bool(thinness_now < thinness_3d_ago)

            price_i = float(close.iloc[i])
            if price_i <= 0:
                continue
            fwd = {h: round((float(close.iloc[i + h]) - price_i) / price_i * 100, 2) for h in FORWARD_HORIZONS}

            events.append({
                "ticker": ticker,
                "date": str(hist_df.index[i].date()),
                "thinness_abs": thinness_now,
                "decelerating": decelerating,
                **{f"fwd_{h}d": fwd[h] for h in FORWARD_HORIZONS},
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
    print(f"Total event SIGNAL_CROSS: {len(df)}\n")
    if df.empty:
        print("⚠️ Tidak ada event.")
        return

    print("=" * 96)
    print("A. QUINTILE thinness_abs (magnitude histogram bearish SEHARI SEBELUM flip -- makin kecil makin 'tipis')")
    print("=" * 96)
    df["thin_q"] = pd.qcut(df["thinness_abs"], 5, duplicates="drop")
    for q, g in sorted(df.groupby("thin_q", observed=True), key=lambda kv: kv[0].left):
        line = f"  {str(q):<24} n={len(g):<6}"
        for h in FORWARD_HORIZONS:
            line += f" | fwd{h}d avg={g[f'fwd_{h}d'].mean():+5.2f}%"
        print(line)

    print("\nBaca ini: kalau quintile PALING TIPIS (kiri, nilai kecil) fwd return-nya JELAS lebih baik")
    print("dari quintile PALING TEBAL (kanan) -- 'histogram tipis sebelum flip' genuinely sinyal kualitas,")
    print("layak jadi filter noise SDT. Kalau flat/berantakan -- magnitude histogram sebelum flip TIDAK")
    print("cukup prediktif dipakai sendirian.")

    print("\n" + "=" * 96)
    print("B. CROSS-TAB: thinness tercile x decelerating (histogram MENYUSUT menuju nol, YA/TIDAK)")
    print("=" * 96)
    df["thin_tercile"] = pd.qcut(df["thinness_abs"], 3, labels=["TIPIS", "SEDANG", "TEBAL"], duplicates="drop")
    for tercile in ["TIPIS", "SEDANG", "TEBAL"]:
        for decel in [True, False]:
            g = df[(df["thin_tercile"] == tercile) & (df["decelerating"] == decel)]
            if g.empty:
                continue
            label = f"{tercile} + {'menyusut' if decel else 'TIDAK menyusut'}"
            line = f"  {label:<28} n={len(g):<6}"
            for h in FORWARD_HORIZONS:
                line += f" | fwd{h}d avg={g[f'fwd_{h}d'].mean():+5.2f}%"
            print(line)

    print("\nBaca ini: kalau di dalam tercile YANG SAMA, 'menyusut' konsisten lebih baik dari 'TIDAK")
    print("menyusut' -- tren deselerasi (bukan cuma nilai statis) menambah info tambahan, layak masuk")
    print("kriteria bersama thinness. Kalau tidak ada beda -- cukup pakai thinness_abs saja, decelerating")
    print("tidak perlu ditambahkan (redundant).")


if __name__ == "__main__":
    main()
