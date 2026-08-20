"""
backtest/research_macd_cross_winner_profile_v2.py — MBSS v2, user request
lanjutan setelah research_macd_cross_winner_profile.py (v1). DUA perbaikan/
tambahan atas v1, per instruksi user ("keduanya"):

1. BUGFIX (leakage ditemukan dari hasil v1 sendiri — ret_1d_pct punya
   Cohen's d 1.451 di tabel CENTERLINE_CROSS, jauh melampaui semua fitur
   lain, mencurigakan): v1 menggeser baseline entry CENTERLINE_CROSS ke
   hari SEBELUM cross (i-1, per instruksi user sebelumnya) TAPI fitur
   karakterisasi masih diambil di hari CROSS ITU SENDIRI (i) -- padahal
   hari i itu SEKARANG bagian dari window forward-return yang dipakai
   menentukan "hit". ret_1d_pct di hari i SECARA MATEMATIS adalah
   komponen PERTAMA dari forward-return itu sendiri -- bukan fitur
   prediktif, itu SEBAGIAN si hasil. v2: fitur CENTERLINE_CROSS SEKARANG
   diambil di hari i-1 (SAMA dengan hari baseline entry) -- konsisten,
   tidak ada lagi overlap antara "apa yang diketahui" dan "apa yang mau
   diprediksi". SIGNAL_CROSS TIDAK berubah (window forward-nya sudah
   strict SETELAH hari event, tidak pernah overlap -- v1 untuk itu sudah
   valid).

2. EKSPLORASI POLA BARU (temuan v1 — SIGNAL_CROSS populasi penuh punya
   macd_line_above_zero d=+0.259, winner LEBIH SERING sudah di atas
   centerline saat cross, bukan di bawahnya -- pola pullback-resume dalam
   uptrend mapan, DI LUAR cakupan SWEET_SPOT/SQUEEZE_RESCUE yang
   mensyaratkan macd_line<0). v2 memecah tabel SIGNAL_CROSS jadi DUA
   sub-populasi terpisah, masing-masing dengan Cohen's-d ranking sendiri:
   - SIGNAL_CROSS (macd_line < 0 saat cross -- "approach", cakupan
     SWEET_SPOT/SQUEEZE_RESCUE yang sudah live)
   - SIGNAL_CROSS (macd_line >= 0 saat cross -- "pullback-resume dalam
     uptrend", kandidat kategori BARU, belum ada di produksi)
   supaya bisa dibandingkan langsung: mana yang win-rate-nya lebih tinggi,
   dan apa karakter pembeda winner DI DALAM masing-masing sub-populasi
   (bisa jadi beda -- squeeze relevan di approach, belum tentu relevan di
   pullback-resume).

Metodologi tetap SAMA dengan v1 (Cohen's d effect-size ranking, konvensi
research/screener_v2/findings.md), fitur & precondition SAMA (lihat v1
untuk daftar lengkap ~20 fitur causal + alasan REUSE fungsi indikator
existing).

Run di server:
    python backtest/research_macd_cross_winner_profile_v2.py

Pakai OHLCV lokal saja, zero fetch/network cost. Murni observasi, belum
mengubah formula produksi apa pun.
"""
import math
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
WARMUP_BARS = 55  # SMA50 + sedikit buffer
EVAL_TAIL_BUFFER = FORWARD_DAYS + 2
TOP_N_FEATURES = 18

FEATURE_COLS = [
    "vol_ratio", "adx", "rsi", "cmf", "day_range_pct_10d", "close_pos_day",
    "dist_to_sma20_pct", "dist_to_sma50_pct", "dist_to_ema9_pct", "dist_to_ema21_pct",
    "bollinger_bandwidth_percentile", "bollinger_squeeze",
    "macd_hist_pct_of_price", "macd_line_pct_of_price", "macd_line_slope_3d_pct",
    "regime_age_days", "macd_line_above_zero", "value_traded_bn",
    "obv_bearish_divergence", "obv_bullish_divergence", "ret_1d_pct", "ret_5d_pct",
]


def _bandwidth_series(closes: pd.Series) -> pd.Series:
    sma20 = closes.rolling(20).mean()
    std20 = closes.rolling(20).std()
    upper = sma20 + 2 * std20
    lower = sma20 - 2 * std20
    return (upper - lower) / sma20


def _squeeze_and_percentile(bandwidth: pd.Series, i: int):
    trailing = bandwidth.iloc[max(0, i - core.MIN_HISTORY_FOR_ADAPTIVE):i].dropna()
    if len(trailing) < 20:
        return False, None
    pct = core.percentile_rank(trailing, bandwidth.iloc[i])
    return bool(pct <= 0.20), round(pct * 100, 1)


def _find_recent_bullish_cross_days_ago(macd_hist: pd.Series, i: int, max_days_back: int) -> int | None:
    """REUSE exact logic dari macd_cross_days_ago (engine/scoring.py) / backtest v1-v3."""
    if i < 1 or macd_hist.iloc[i] <= 0:
        return None
    for days_back in range(1, min(max_days_back + 1, i + 1)):
        idx = i - days_back
        if idx < 0:
            break
        if not (macd_hist.iloc[idx] > 0):
            return days_back
    return None


def cohens_d(a: pd.Series, b: pd.Series) -> float:
    a = a.dropna().astype(float)
    b = b.dropna().astype(float)
    if len(a) < 5 or len(b) < 5:
        return 0.0
    va, vb = a.var(ddof=1), b.var(ddof=1)
    pooled_std = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2))
    if not pooled_std or math.isnan(pooled_std):
        return 0.0
    return float((a.mean() - b.mean()) / pooled_std)


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
        volume = hist_df["Volume"].astype(float)

        n = len(hist_df)
        eval_end = n - EVAL_TAIL_BUFFER
        if eval_end <= WARMUP_BARS:
            continue

        macd_line, signal_line, macd_hist = core.calculate_macd(close)
        adx_s = core.calculate_adx(high, low, close)
        rsi_s = core.calculate_rsi(close)
        cmf_s = core.calculate_cmf(high, low, close, volume)
        obv_s = core.calculate_obv(close, volume)
        bandwidth = _bandwidth_series(close)
        sma20 = close.rolling(20).mean()
        sma50 = close.rolling(50).mean()
        ema9 = close.ewm(span=9, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        vol_avg20_prev = volume.rolling(20).mean().shift(1)
        vol_ratio_s = volume / vol_avg20_prev.replace(0, np.nan)
        day_range_10d_s = (high.rolling(10).max() - low.rolling(10).min()) / low.rolling(10).min().replace(0, np.nan) * 100
        close_pos_day_s = (close - low) / (high - low).replace(0, np.nan)
        price_chg20_s = close.pct_change(20) * 100
        obv_start20 = obv_s.shift(20)
        obv_chg20_pct_s = (obv_s - obv_start20) / obv_start20.abs().replace(0, np.nan) * 100
        bearish_div_s = (price_chg20_s > -2) & (obv_chg20_pct_s < -5)
        bullish_div_s = (price_chg20_s < 2) & (obv_chg20_pct_s > 5)
        ret1d_s = close.pct_change(1) * 100
        ret5d_s = close.pct_change(5) * 100

        def snapshot(idx: int) -> dict:
            """Fitur causal PADA index idx — dipanggil dengan idx BERBEDA
            tergantung cross_type (lihat catatan file di atas kenapa)."""
            price_idx = float(close.iloc[idx])
            squeeze_flag, bw_pct = _squeeze_and_percentile(bandwidth, idx)
            regime_age = _find_recent_bullish_cross_days_ago(macd_hist, idx, 40)
            sma20_i, sma50_i, ema9_i, ema21_i = sma20.iloc[idx], sma50.iloc[idx], ema9.iloc[idx], ema21.iloc[idx]
            return {
                "vol_ratio": vol_ratio_s.iloc[idx],
                "adx": adx_s.iloc[idx],
                "rsi": rsi_s.iloc[idx],
                "cmf": cmf_s.iloc[idx],
                "day_range_pct_10d": day_range_10d_s.iloc[idx],
                "close_pos_day": close_pos_day_s.iloc[idx],
                "dist_to_sma20_pct": (price_idx - sma20_i) / sma20_i * 100 if sma20_i else np.nan,
                "dist_to_sma50_pct": (price_idx - sma50_i) / sma50_i * 100 if sma50_i else np.nan,
                "dist_to_ema9_pct": (price_idx - ema9_i) / ema9_i * 100 if ema9_i else np.nan,
                "dist_to_ema21_pct": (price_idx - ema21_i) / ema21_i * 100 if ema21_i else np.nan,
                "bollinger_bandwidth_percentile": bw_pct,
                "bollinger_squeeze": 1.0 if squeeze_flag else 0.0,
                "macd_hist_pct_of_price": float(macd_hist.iloc[idx]) / price_idx * 100 if price_idx else np.nan,
                "macd_line_pct_of_price": float(macd_line.iloc[idx]) / price_idx * 100 if price_idx else np.nan,
                "macd_line_slope_3d_pct": (float(macd_line.iloc[idx]) - float(macd_line.iloc[idx - 3])) / price_idx * 100 if idx >= 3 and price_idx else np.nan,
                "regime_age_days": float(regime_age) if regime_age is not None else 0.0,
                "macd_line_above_zero": 1.0 if macd_line.iloc[idx] >= 0 else 0.0,
                "value_traded_bn": price_idx * float(volume.iloc[idx]) / 1e9,
                "obv_bearish_divergence": 1.0 if bool(bearish_div_s.iloc[idx]) else 0.0,
                "obv_bullish_divergence": 1.0 if bool(bullish_div_s.iloc[idx]) else 0.0,
                "ret_1d_pct": ret1d_s.iloc[idx],
                "ret_5d_pct": ret5d_s.iloc[idx],
            }

        for i in range(WARMUP_BARS, eval_end):
            signal_cross = bool(macd_hist.iloc[i - 1] <= 0 and macd_hist.iloc[i] > 0)
            centerline_cross = bool(macd_line.iloc[i - 1] < 0 and macd_line.iloc[i] >= 0)
            if not signal_cross and not centerline_cross:
                continue

            price_i = float(close.iloc[i])
            if price_i <= 0:
                continue
            date_str = str(hist_df.index[i].date())

            if signal_cross:
                # Window forward strict SETELAH hari event -- tidak pernah
                # overlap dengan fitur di hari i, snapshot di hari i AMAN.
                fwd_returns_signal = [
                    (float(close.iloc[i + k]) - price_i) / price_i * 100
                    for k in range(1, FORWARD_DAYS + 1)
                ]
                hit_s = any(r >= BREAKOUT_THRESHOLD_PCT for r in fwd_returns_signal)
                # Sub-populasi (EKSPLORASI POLA BARU): apakah MACD line
                # SUDAH di atas centerline saat signal-cross ini terjadi
                # (pullback-resume dalam uptrend) atau MASIH di bawah
                # (approach, cakupan SWEET_SPOT/SQUEEZE_RESCUE existing).
                above_zero_at_event = bool(macd_line.iloc[i] >= 0)
                events.append({
                    "ticker": ticker, "date": date_str, "cross_type": "SIGNAL_CROSS",
                    "regime_at_cross": "ABOVE_CENTERLINE" if above_zero_at_event else "BELOW_CENTERLINE",
                    "hit": hit_s, "max_fwd_return": round(max(fwd_returns_signal), 2),
                    **snapshot(i),
                })

            if centerline_cross:
                # BUGFIX v2: baseline entry TETAP di hari SEBELUM cross
                # (i-1, per instruksi user), TAPI fitur SEKARANG JUGA
                # diambil di i-1 -- konsisten, tidak ada lagi overlap
                # fitur/label (lihat catatan file di atas).
                price_before = float(close.iloc[i - 1]) if i >= 1 else price_i
                if price_before <= 0:
                    continue
                fwd_returns_centerline = [
                    (float(close.iloc[i - 1 + k]) - price_before) / price_before * 100
                    for k in range(1, FORWARD_DAYS + 1)
                ]
                hit_c = any(r >= BREAKOUT_THRESHOLD_PCT for r in fwd_returns_centerline)
                events.append({
                    "ticker": ticker, "date": date_str, "cross_type": "CENTERLINE_CROSS",
                    "regime_at_cross": "N/A",
                    "hit": hit_c, "max_fwd_return": round(max(fwd_returns_centerline), 2),
                    **snapshot(i - 1),
                })

    print(f"Ticker dilewati (histori lokal <{MIN_BARS} bar): {skipped}/{len(tickers)}")
    return pd.DataFrame(events)


def report_profile(g: pd.DataFrame, label: str):
    if g.empty:
        print(f"\n⚠️ Tidak ada event {label}.")
        return

    winners = g[g["hit"]]
    losers = g[~g["hit"]]
    hit_rate = len(winners) / len(g) * 100

    print("\n" + "=" * 100)
    print(f"{label} — n={len(g)} | winner (naik >={BREAKOUT_THRESHOLD_PCT}% dalam {FORWARD_DAYS}hari) = {len(winners)} ({hit_rate:.1f}%) | loser = {len(losers)}")
    print("=" * 100)

    rows = []
    for feat in FEATURE_COLS:
        d = cohens_d(winners[feat], losers[feat])
        rows.append({
            "feature": feat,
            "mean_winner": winners[feat].dropna().mean(),
            "mean_loser": losers[feat].dropna().mean(),
            "cohens_d": d,
        })
    ranked = sorted(rows, key=lambda r: abs(r["cohens_d"]), reverse=True)

    print(f"{'Fitur':<32} {'Mean Winner':>14} {'Mean Loser':>14} {'Cohens d':>10}")
    for r in ranked[:TOP_N_FEATURES]:
        mw = f"{r['mean_winner']:.2f}" if pd.notna(r["mean_winner"]) else "-"
        ml = f"{r['mean_loser']:.2f}" if pd.notna(r["mean_loser"]) else "-"
        print(f"{r['feature']:<32} {mw:>14} {ml:>14} {r['cohens_d']:>10.3f}")


def main():
    scored, _ = nightly_engine.load_daily_scan_cache_allow_stale()
    tickers = sorted(scored.keys()) if scored else []
    print(f"Universe: {len(tickers)} ticker (dari cache /eodscan terakhir)\n")
    if not tickers:
        print("⚠️ Cache /eodscan kosong — jalankan /eodscan dulu di server.")
        return

    df = collect_events(tickers)
    print(f"Total event (baris, SIGNAL_CROSS + CENTERLINE_CROSS): {len(df)}\n")
    if df.empty:
        print("⚠️ Tidak ada event MACD cross yang ditemukan.")
        return

    signal_df = df[df["cross_type"] == "SIGNAL_CROSS"]
    centerline_df = df[df["cross_type"] == "CENTERLINE_CROSS"]

    print("#" * 100)
    print("# 1. CENTERLINE_CROSS — DIPERBAIKI (fitur sekarang di hari i-1, konsisten dgn baseline entry)")
    print("#" * 100)
    report_profile(centerline_df, "CENTERLINE_CROSS (fixed)")

    print("\n" + "#" * 100)
    print("# 2. SIGNAL_CROSS — dipecah per regime saat cross (eksplorasi pola baru)")
    print("#" * 100)
    below = signal_df[signal_df["regime_at_cross"] == "BELOW_CENTERLINE"]
    above = signal_df[signal_df["regime_at_cross"] == "ABOVE_CENTERLINE"]
    below_hr = below["hit"].mean() * 100 if len(below) else 0.0
    above_hr = above["hit"].mean() * 100 if len(above) else 0.0
    print(f"\nRingkas win-rate: BELOW_CENTERLINE (approach, cakupan SWEET_SPOT/SQUEEZE_RESCUE existing) = {below_hr:.1f}% (n={len(below)})")
    print(f"                  ABOVE_CENTERLINE (pullback-resume dalam uptrend, KANDIDAT BARU)        = {above_hr:.1f}% (n={len(above)})")
    report_profile(below, "SIGNAL_CROSS — BELOW_CENTERLINE (approach)")
    report_profile(above, "SIGNAL_CROSS — ABOVE_CENTERLINE (pullback-resume, kandidat baru)")

    print("\nBaca ini: |Cohen's d| >=0.2 kecil, >=0.5 sedang, >=0.8 besar. d POSITIF besar = winner")
    print("nilainya LEBIH TINGGI (kandidat kriteria BONUS); d NEGATIF besar = winner nilainya LEBIH")
    print("RENDAH (kandidat kriteria PEMBATAS). Bandingkan JUGA win-rate baseline antar sub-populasi")
    print("di atas -- kalau ABOVE_CENTERLINE win-rate-nya jelas lebih tinggi dari BELOW_CENTERLINE,")
    print("itu alasan kuat mempertimbangkan kategori BARU di /screendaytrade (di luar SWEET_SPOT/")
    print("SQUEEZE_RESCUE yang sekarang cuma cover BELOW_CENTERLINE).")


if __name__ == "__main__":
    main()
