"""
backtest/research_macd_approach_score_v3.py — MBSS v2, user request (uji
terakhir malam ini sebelum implementasi ke SDT). Lanjutan v2
(research_macd_approach_score_v2.py).

v2 finding (n=23.049, precondition: macd_line slope 3hari positif + masih
<0 + cross 0-20 hari lalu): forward return MONOTON NAIK dari bin 0-3 hari
(fwd10d +0.56%, DI BAWAH baseline pasar +1.29%) sampai bin 16-20 hari
(fwd10d +2.13%, JELAS DI ATAS baseline) — cross yang BARU justru lebih
jelek dari acak, cross yang agak lama (sambil MACD line masih menanjak
lagi) justru edge-nya nyata. TAPI bin terbaik (16-20 hari) itu kebetulan
juga UJUNG window scan (CROSS_LOOKBACK_DAYS=20) — trennya monoton naik
terus sampai ujung, jadi belum jelas apakah 16-20 itu genuinely puncak
atau cuma titik terakhir yang sempat diukur (window terlalu pendek).

v3 dua perbaikan:
1. CROSS_LOOKBACK_DAYS diperpanjang 20 -> 40, bin lebih halus di ekor
   (0-3, 4-7, 8-11, 12-15, 16-19, 20-23, 24-27, 28-31, 32-35, 36-40) —
   supaya kelihatan apakah tren terus naik, plateau, atau justru membalik
   setelah hari ke-20 (window lama kepotong sebelum itu kelihatan).
2. Cross-tab squeeze x rentang cross_days_ago (3 grup kasar: AWAL 0-13,
   TENGAH 14-26, AKHIR 27-40) — supaya jelas apakah efek squeeze (sudah
   tervalidasi kuat & independen di v1/v2) dan efek "cross lama" itu
   ADDITIVE (saling menguatkan kalau digabung) atau cuma proxy satu sama
   lain (correlated, bukan dua sinyal independen).

Run di server:
    python backtest/research_macd_approach_score_v3.py

Pakai OHLCV lokal saja, zero fetch/network cost. Murni observasi terakhir
sebelum keputusan implementasi — TIDAK mengubah formula produksi apa pun.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import engine.legacy_core as core
import engine.nightly as nightly_engine

MACD_LINE_SLOPE_DAYS = 3
CROSS_LOOKBACK_DAYS = 40  # v2: 20 -> v3: 40, supaya titik puncak sebenarnya (kalau ada) kelihatan
FORWARD_HORIZONS = (3, 5, 10)
MIN_BARS = 150
EVAL_TAIL_BUFFER = max(FORWARD_HORIZONS) + 2

CROSS_DAYS_BINS = [(0, 3), (4, 7), (8, 11), (12, 15), (16, 19), (20, 23), (24, 27), (28, 31), (32, 35), (36, 40)]
CROSS_DAYS_GROUPS_COARSE = [(0, 13, "AWAL 0-13"), (14, 26, "TENGAH 14-26"), (27, 40, "AKHIR 27-40")]


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
    if i < 1 or macd_hist.iloc[i] <= 0:
        return None
    for days_back in range(1, min(max_days_back + 1, i + 1)):
        idx = i - days_back
        if idx < 0:
            break
        if not (macd_hist.iloc[idx] > 0):
            return days_back
    return None


def macd_approach_precondition(macd_line: pd.Series, macd_hist: pd.Series, i: int) -> int | None:
    cross_days_ago = _find_recent_bullish_cross_days_ago(macd_hist, i, CROSS_LOOKBACK_DAYS)
    if cross_days_ago is None:
        return None
    if macd_line.iloc[i] >= 0:
        return None
    if i < MACD_LINE_SLOPE_DAYS:
        return None
    slope = macd_line.iloc[i] - macd_line.iloc[i - MACD_LINE_SLOPE_DAYS]
    if slope <= 0:
        return None
    return cross_days_ago


def _bin_label(days_ago: int) -> str:
    for lo, hi in CROSS_DAYS_BINS:
        if lo <= days_ago <= hi:
            return f"{lo}-{hi}"
    return "?"


def _coarse_group_label(days_ago: int) -> str:
    for lo, hi, label in CROSS_DAYS_GROUPS_COARSE:
        if lo <= days_ago <= hi:
            return label
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
    ohlcv_cache = {}

    for ticker in tickers:
        try:
            hist_df = core.get_ohlcv_daily_from_db(ticker, limit=400)
        except Exception:
            skipped += 1
            continue
        if hist_df is None or hist_df.empty or len(hist_df) < MIN_BARS:
            skipped += 1
            continue
        ohlcv_cache[ticker] = hist_df

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
        print("⚠️ TIDAK ADA observasi lolos precondition.")
        return

    df = pd.DataFrame(rows)

    # Baseline (sample tiap 5 hari, semua ticker, TANPA syarat MACD)
    baseline_rows = []
    for ticker, hist_df in ohlcv_cache.items():
        closes = hist_df["Close"].astype(float)
        n = len(hist_df)
        eval_end = n - EVAL_TAIL_BUFFER
        for i in range(MACD_LINE_SLOPE_DAYS, eval_end, 5):
            price_i = closes.iloc[i]
            baseline_rows.append({h: round((closes.iloc[i + h] - price_i) / price_i * 100, 2) for h in FORWARD_HORIZONS})
    baseline_df = pd.DataFrame(baseline_rows)

    print("=" * 90)
    print("BASELINE (semua ticker-hari lokal, sample tiap 5 hari, TANPA syarat MACD apa pun)")
    print("=" * 90)
    line = f"  n={len(baseline_df):<6}"
    for h in FORWARD_HORIZONS:
        line += f" | fwd{h}d avg={baseline_df[h].mean():+5.2f}%"
    print(line)

    print("\n" + "=" * 90)
    print(f"BUCKET HALUS per cross_days_ago (window diperpanjang ke {CROSS_LOOKBACK_DAYS} hari)")
    print("=" * 90)
    df["bin"] = df["cross_days_ago"].apply(_bin_label)
    for lo, hi in CROSS_DAYS_BINS:
        label = f"{lo}-{hi}"
        g = df[df["bin"] == label]
        if g.empty:
            print(f"  {label:<10} hari lalu  n=0")
            continue
        line = f"  {label:<10} hari lalu  n={len(g):<6}"
        for h in FORWARD_HORIZONS:
            line += f" | fwd{h}d avg={g[f'fwd_{h}d'].mean():+5.2f}%"
        print(line)

    print("\nBaca ini: apakah trennya (a) TERUS NAIK sampai bin terakhir (36-40) -- berarti")
    print("window masih kurang panjang, sweet spot sebenarnya lebih jauh lagi; (b) NAIK lalu")
    print("PLATEAU/TURUN di suatu titik -- itu baru sweet spot genuine; atau (c) berantakan --")
    print("tidak ada pola jelas di luar bin-bin awal v2.")

    print("\n" + "=" * 90)
    print("CROSS-TAB: squeeze x rentang cross_days_ago (additive atau proxy satu sama lain?)")
    print("=" * 90)
    df["coarse_group"] = df["cross_days_ago"].apply(_coarse_group_label)
    for _, _, glabel in CROSS_DAYS_GROUPS_COARSE:
        for squeeze_flag, slabel in [(True, "DENGAN squeeze"), (False, "TANPA squeeze")]:
            g = df[(df["coarse_group"] == glabel) & (df["squeeze"] == squeeze_flag)]
            if g.empty:
                print(f"  {glabel:<14} | {slabel:<16} n=0")
                continue
            line = f"  {glabel:<14} | {slabel:<16} n={len(g):<6}"
            for h in FORWARD_HORIZONS:
                line += f" | fwd{h}d avg={g[f'fwd_{h}d'].mean():+5.2f}%"
            print(line)

    print("\nBaca ini: kalau di SETIAP grup (AWAL/TENGAH/AKHIR) 'DENGAN squeeze' konsisten lebih")
    print("baik dari 'TANPA squeeze' DENGAN MARGIN MIRIP -- squeeze itu efek ADDITIVE, independen")
    print("dari cross_days_ago (dua sinyal beda, boleh digabung). Kalau margin squeeze cuma")
    print("kelihatan di grup AWAL tapi hilang di grup AKHIR (atau sebaliknya) -- kedua sinyal ini")
    print("saling tumpang tindih (proxy), tidak perlu dipakai bersamaan.")


if __name__ == "__main__":
    main()
