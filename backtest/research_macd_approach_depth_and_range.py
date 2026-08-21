"""
backtest/research_macd_approach_depth_and_range.py — MBSS v2, user request
lanjutan setelah macd_approach_tier (SWEET_SPOT/SQUEEZE_RESCUE/PULLBACK_
RESUME) & macd_fresh_breakout_confirmed live. User curiga dua hal soal
tier SDT "approach" (SWEET_SPOT/SQUEEZE_RESCUE, macd_line MASIH di bawah
centerline):

1. "posisi macd line masih terlalu jauh dibawah centerline meski sudah
   upward dan crossing" perlu di-filter out -- SWEET_SPOT/SQUEEZE_RESCUE
   SAAT INI tidak punya syarat SEBERAPA JAUH macd_line di bawah centerline
   (cuma syarat cross_days_ago + slope positif + opsional squeeze).
   Hipotesis: kandidat yang MASIH SANGAT DALAM di bawah centerline
   (downtrend berat, baru mulai membalik) kualitasnya lebih rendah dari
   yang cuma sedikit di bawah, walau cross_days_ago & slope-nya sama.
2. "range cross bisa diperluas 4-23 hari saja" -- gabungkan SWEET_SPOT
   (14-23) & SQUEEZE_RESCUE (0-11+squeeze) jadi SATU window 4-23,
   BUANG 0-3 (v1-v3 sudah tunjukkan ini konsisten paling lemah). Hipotesis:
   kalau depth-filter di poin 1 sudah menyaring kualitas, mekanisme
   squeeze-rescue (yang SEKARANG mengkompensasi window 0-11 yang lemah)
   mungkin sudah tidak perlu lagi -- window 4-23 seragam sudah cukup baik
   TANPA syarat squeeze terpisah.

DUA hipotesis ini BELUM divalidasi -- script ini nguji, BUKAN langsung
mengubah production code (v1-v3's cross_days_ago findings sudah established,
tapi depth belum pernah diuji sama sekali).

Metodologi: REUSE persis precondition SWEET_SPOT/SQUEEZE_RESCUE (macd_hist
regime bullish, macd_line<0, slope 3hari positif) dari compute_factor_
scoring/backtest v1-v3, forward return fwd3d/5d/10d (SAMA dgn v1-v3, bukan
"hit >=4%" ala winner_profile -- supaya hasil sebanding LANGSUNG dgn temuan
SWEET_SPOT/SQUEEZE_RESCUE yang sudah ada). Depth = macd_line_pct_of_price
(negatif di populasi ini -- "seberapa jauh di bawah centerline").

EMPAT laporan:
  A. Quintile depth SAJA (independen dari cross_days_ago) -- apakah makin
     dalam makin jelek?
  B. Cross-tab cross_days_ago bucket x depth tercile (SHALLOW/MID/DEEP) --
     apakah depth "menyelamatkan" bucket 4-11 yang lemah tanpa squeeze?
  C. Perbandingan langsung: kriteria PRODUKSI SEKARANG (SWEET_SPOT 14-23 +
     SQUEEZE_RESCUE 0-11+squeeze) vs USULAN (unified 4-23, dengan/tanpa
     depth filter tambahan) -- angka agregat side-by-side.
  D. Quintile MAGNITUDE slope 3-hari (user request lanjutan: "apakah bisa
     dideteksi sekuat apa upward macd 3 hari terakhir, sebagai alat deteksi
     kekuatan arus naik?") -- production SEKARANG cuma cek slope>0 (biner),
     laporan ini nguji apakah MAGNITUDE slope (bukan cuma tanda) juga
     menambah info, layak jadi dimensi skor tambahan atau tidak.

Run di server:
    python backtest/research_macd_approach_depth_and_range.py

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

FORWARD_HORIZONS = (3, 5, 10)
MIN_BARS = 150
EVAL_TAIL_BUFFER = max(FORWARD_HORIZONS) + 2
MACD_LINE_SLOPE_DAYS = 3
CROSS_LOOKBACK_DAYS = 30  # sedikit lebih lebar dari 23 supaya bucket 24-27+ tetap kelihatan sebagai pembanding

CROSS_DAYS_BINS = [(0, 3), (4, 7), (8, 11), (12, 15), (16, 19), (20, 23), (24, 27), (28, 30)]


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
    return bool(pct <= 0.20)


def _find_recent_bullish_cross_days_ago(macd_hist: pd.Series, i: int, max_days_back: int) -> int | None:
    if i < 1 or macd_hist.iloc[i] <= 0:
        return None
    for days_back in range(1, min(max_days_back + 1, i + 1)):
        idx = i - days_back
        if idx < 0:
            break
        if not (macd_hist.iloc[idx] > 0):
            return days_back
    return None


def _bin_label(days: int) -> str:
    for lo, hi in CROSS_DAYS_BINS:
        if lo <= days <= hi:
            return f"{lo}-{hi}"
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
        eval_end = n - EVAL_TAIL_BUFFER
        if eval_end <= MACD_LINE_SLOPE_DAYS:
            continue

        macd_line, _, macd_hist = core.calculate_macd(close)
        bandwidth = _bandwidth_series(close)

        for i in range(MACD_LINE_SLOPE_DAYS, eval_end):
            # Precondition IDENTIK dengan SWEET_SPOT/SQUEEZE_RESCUE production
            # (engine/scoring.py): regime histogram bullish, MACD line masih
            # <0, slope 3hari positif.
            if not (macd_hist.iloc[i] > 0):
                continue
            price_i = float(close.iloc[i])
            if price_i <= 0 or macd_line.iloc[i] >= 0:
                continue
            slope = float(macd_line.iloc[i] - macd_line.iloc[i - MACD_LINE_SLOPE_DAYS])
            if slope <= 0:
                continue

            cross_days_ago = _find_recent_bullish_cross_days_ago(macd_hist, i, CROSS_LOOKBACK_DAYS)
            if cross_days_ago is None:
                continue

            depth_pct = float(macd_line.iloc[i]) / price_i * 100  # negatif -- makin negatif makin dalam
            slope_3d_pct = slope / price_i * 100  # dinormalisasi harga, konsisten dgn macd_line_pct_of_price -- MAGNITUDE arus naik, bukan cuma tanda positif/negatif
            squeeze = _is_squeeze_on_day(bandwidth, i)

            fwd = {h: round((float(close.iloc[i + h]) - price_i) / price_i * 100, 2) for h in FORWARD_HORIZONS}

            events.append({
                "ticker": ticker,
                "date": str(hist_df.index[i].date()),
                "cross_days_ago": cross_days_ago,
                "depth_pct": depth_pct,
                "slope_3d_pct": slope_3d_pct,
                "squeeze": squeeze,
                **{f"fwd_{h}d": fwd[h] for h in FORWARD_HORIZONS},
            })

    print(f"Ticker dilewati (histori lokal <{MIN_BARS} bar): {skipped}/{len(tickers)}")
    return pd.DataFrame(events)


def _fmt_row(label: str, g: pd.DataFrame) -> str:
    line = f"  {label:<20} n={len(g):<6}"
    for h in FORWARD_HORIZONS:
        line += f" | fwd{h}d avg={g[f'fwd_{h}d'].mean():+5.2f}%"
    return line


def main():
    scored, _ = nightly_engine.load_daily_scan_cache_allow_stale()
    tickers = sorted(scored.keys()) if scored else []
    print(f"Universe: {len(tickers)} ticker (dari cache /eodscan terakhir)\n")
    if not tickers:
        print("⚠️ Cache /eodscan kosong — jalankan /eodscan dulu di server.")
        return

    df = collect_events(tickers)
    print(f"Total observasi (precondition SWEET_SPOT/SQUEEZE_RESCUE, semua cross_days_ago 0-{CROSS_LOOKBACK_DAYS}): {len(df)}\n")
    if df.empty:
        print("⚠️ Tidak ada observasi.")
        return

    # ================================================================
    # A. Quintile depth SAJA
    # ================================================================
    print("=" * 96)
    print("A. QUINTILE DEPTH (macd_line_pct_of_price, makin NEGATIF = makin dalam di bawah centerline)")
    print("=" * 96)
    dfa = df.copy()
    try:
        dfa["depth_q"] = pd.qcut(dfa["depth_pct"], 5, duplicates="drop")
        for q, g in sorted(dfa.groupby("depth_q", observed=True), key=lambda kv: kv[0].left):
            print(_fmt_row(str(q), g))
    except ValueError:
        print("⚠️ Gagal bikin quintile depth (distribusi terlalu sempit).")
    print("\nBaca ini: kalau quintile PALING DALAM (paling negatif, biasanya tercetak paling kiri/pertama)")
    print("punya fwd10d JELAS lebih jelek dari quintile lain -- itu bukti 'terlalu dalam' layak di-filter.")

    # ================================================================
    # B. Cross-tab cross_days_ago x depth tercile
    # ================================================================
    print("\n" + "=" * 96)
    print("B. CROSS-TAB: cross_days_ago bucket x depth tercile (SHALLOW/MID/DEEP, data-driven)")
    print("=" * 96)
    dfb = df.copy()
    dfb["bin"] = dfb["cross_days_ago"].apply(_bin_label)
    try:
        dfb["depth_tercile"] = pd.qcut(dfb["depth_pct"], 3, labels=["DEEP", "MID", "SHALLOW"], duplicates="drop")
    except ValueError:
        dfb["depth_tercile"] = "N/A"
    for lo, hi in CROSS_DAYS_BINS:
        label = f"{lo}-{hi}"
        gday = dfb[dfb["bin"] == label]
        if gday.empty:
            continue
        print(f"\n {label} hari sejak signal cross (n total={len(gday)}):")
        for tercile in ["SHALLOW", "MID", "DEEP"]:
            gsub = gday[gday["depth_tercile"] == tercile]
            if gsub.empty:
                continue
            print("  " + _fmt_row(tercile, gsub))

    # ================================================================
    # C. Perbandingan produksi SEKARANG vs USULAN
    # ================================================================
    print("\n" + "=" * 96)
    print("C. PERBANDINGAN: kriteria PRODUKSI sekarang vs USULAN (unified 4-23 +/- depth filter)")
    print("=" * 96)

    sweet_spot_now = df[(df["cross_days_ago"] >= 14) & (df["cross_days_ago"] <= 23)]
    squeeze_rescue_now = df[(df["cross_days_ago"] <= 11) & (df["squeeze"])]
    print(_fmt_row("SWEET_SPOT (14-23)", sweet_spot_now).replace("  ", "  [PRODUKSI] ", 1))
    print(_fmt_row("SQUEEZE_RESCUE (0-11+sqz)", squeeze_rescue_now).replace("  ", "  [PRODUKSI] ", 1))

    unified_4_23 = df[(df["cross_days_ago"] >= 4) & (df["cross_days_ago"] <= 23)]
    print(_fmt_row("UNIFIED 4-23 (tanpa depth)", unified_4_23).replace("  ", "  [USULAN]   ", 1))

    # Depth filter: pakai median depth POPULASI INI SENDIRI sebagai cutoff percobaan
    # (data-driven, bukan angka dikarang) -- exclude yang lebih dalam dari median (lebih negatif).
    median_depth = df["depth_pct"].median()
    unified_4_23_shallow = unified_4_23[unified_4_23["depth_pct"] >= median_depth]
    print(_fmt_row(f"UNIFIED 4-23 + depth>={median_depth:.2f}%", unified_4_23_shallow).replace("  ", "  [USULAN]   ", 1))

    print(f"\n(median depth_pct populasi ini: {median_depth:.2f}% -- cutoff depth di atas cuma PERCOBAAN,")
    print(" bukan angka final. Kalau baris terakhir JELAS lebih baik dari 'UNIFIED 4-23 tanpa depth' DAN")
    print(" sebanding/lebih baik dari gabungan SWEET_SPOT+SQUEEZE_RESCUE produksi sekarang, unified+depth")
    print(" layak jadi pengganti. Kalau tidak, tetap pertahankan split SWEET_SPOT/SQUEEZE_RESCUE yang ada.)")

    # ================================================================
    # D. Quintile MAGNITUDE slope 3-hari (user request lanjutan)
    # ================================================================
    print("\n" + "=" * 96)
    print("D. QUINTILE MAGNITUDE slope MACD line 3-hari (production sekarang cuma cek slope>0, biner)")
    print("=" * 96)
    dfd = df.copy()
    try:
        dfd["slope_q"] = pd.qcut(dfd["slope_3d_pct"], 5, duplicates="drop")
        for q, g in sorted(dfd.groupby("slope_q", observed=True), key=lambda kv: kv[0].left):
            print(_fmt_row(str(q), g))
    except ValueError:
        print("⚠️ Gagal bikin quintile slope (distribusi terlalu sempit).")
    print("\nBaca ini: kalau quintile slope PALING CURAM (paling kanan/besar) fwd10d JELAS lebih tinggi")
    print("dari quintile paling landai -- magnitude slope genuinely alat ukur kekuatan arus, layak jadi")
    print("dimensi skor tambahan (bukan cuma syarat biner slope>0 seperti sekarang). Kalau flat/tidak ada")
    print("beda jelas antar quintile, magnitude TIDAK menambah informasi di atas syarat biner yang sudah ada.")


if __name__ == "__main__":
    main()
