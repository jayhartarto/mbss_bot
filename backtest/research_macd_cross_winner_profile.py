"""
backtest/research_macd_cross_winner_profile.py — MBSS v2, user request
lanjutan setelah research_macd_centerline_breakout_validation.py.

Pertanyaan sebelumnya (v1-v3 + centerline validation) semua tentang KAPAN
(berapa hari sejak cross). Script ini beda sumbu: dari SELURUH kandidat
MACD cross (signal-line cross DAN centerline cross, dua populasi terpisah),
apa KARAKTER TEKNIKAL yang membedakan yang harga naik >4% dalam <=3 hari
("winner") dari yang tidak ("loser")? Metodologi SAMA dengan konvensi riset
proyek ini sendiri (research/screener_v2/findings.md — "loser-profiling via
Cohen's d"): bandingkan mean tiap fitur teknikal antara grup winner vs
loser, urutkan berdasarkan |Cohen's d| (effect size, bukan cuma selisih
mentah yang bisa menyesatkan kalau skala/variance beda).

DUA populasi kandidat, DILAPORKAN TERPISAH (bukan digabung — signal-line
cross dan centerline cross itu momen berbeda dalam hidup satu saham,
menggabungkan bisa mengaburkan pola pembeda masing-masing):
  1. SIGNAL_CROSS: histogram MACD baru lewat 0 (macd_hist <=0 -> >0).
  2. CENTERLINE_CROSS: MACD line baru lewat 0 (macd_line <0 -> >=0).

Fitur teknikal (SEMUA dihitung CAUSAL, hanya pakai data s/d hari event —
tidak ada lookahead, kecuali label hit/max_fwd_return yang MEMANG dari
masa depan): vol_ratio, ADX, RSI, CMF, day_range_pct_10d, close_pos_day,
jarak ke SMA20/SMA50/EMA9/EMA21, bollinger bandwidth percentile + squeeze,
MACD histogram/line dinormalisasi harga, slope MACD line 3 hari
(dinormalisasi harga), umur regime MACD (macd_cross_days_ago), value
traded, OBV divergence (bearish/bullish), return 1 hari & 5 hari terakhir.
Semua REUSE fungsi yang sudah ada di engine/legacy_core.py (calculate_adx/
calculate_rsi/calculate_cmf/calculate_obv/calculate_macd) — tidak
re-derive formula indikator, cuma dihitung sebagai SERIES penuh per ticker
(vektorized, causal by construction lewat rolling/ewm) lalu di-index per
hari event, bukan re-fetch/re-hitung ulang tiap event.

Run di server:
    python backtest/research_macd_cross_winner_profile.py

Pakai OHLCV lokal saja, zero fetch/network cost. Murni observasi — hasil
ini dipakai untuk MENENTUKAN fitur mana yang layak jadi kriteria
prioritisasi tambahan di /screendaytrade (macd_approach_tier), belum
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

        for i in range(WARMUP_BARS, eval_end):
            signal_cross = bool(macd_hist.iloc[i - 1] <= 0 and macd_hist.iloc[i] > 0)
            centerline_cross = bool(macd_line.iloc[i - 1] < 0 and macd_line.iloc[i] >= 0)
            if not signal_cross and not centerline_cross:
                continue

            price_i = float(close.iloc[i])
            if price_i <= 0:
                continue

            # SIGNAL_CROSS: baseline entry = hari event itu sendiri (event
            # ini SUDAH "sebelum centerline cross" secara definisi).
            fwd_returns_signal = [
                (float(close.iloc[i + k]) - price_i) / price_i * 100
                for k in range(1, FORWARD_DAYS + 1)
            ]

            # CENTERLINE_CROSS (user request — "baseline entry hitungan min
            # 4% dari saat SEBELUM cross centerline"): baseline entry
            # DIGESER ke hari SEBELUM cross (i-1, hari terakhir MACD line
            # masih <0) -- bukan hari cross itu sendiri (i). Mensimulasikan
            # entry riil (beli SELAGI approach, sebelum konfirmasi cross),
            # window forward jadi mencakup HARI CROSSING ITU SENDIRI sebagai
            # bagian dari pergerakan yang diukur, bukan diabaikan.
            price_before = float(close.iloc[i - 1]) if i >= 1 else price_i
            fwd_returns_centerline = (
                [
                    (float(close.iloc[i - 1 + k]) - price_before) / price_before * 100
                    for k in range(1, FORWARD_DAYS + 1)
                ]
                if price_before > 0 else fwd_returns_signal
            )

            regime_age = _find_recent_bullish_cross_days_ago(macd_hist, i, 40)
            squeeze_flag, bw_pct = _squeeze_and_percentile(bandwidth, i)

            sma20_i, sma50_i, ema9_i, ema21_i = sma20.iloc[i], sma50.iloc[i], ema9.iloc[i], ema21.iloc[i]
            feat = {
                "vol_ratio": vol_ratio_s.iloc[i],
                "adx": adx_s.iloc[i],
                "rsi": rsi_s.iloc[i],
                "cmf": cmf_s.iloc[i],
                "day_range_pct_10d": day_range_10d_s.iloc[i],
                "close_pos_day": close_pos_day_s.iloc[i],
                "dist_to_sma20_pct": (price_i - sma20_i) / sma20_i * 100 if sma20_i else np.nan,
                "dist_to_sma50_pct": (price_i - sma50_i) / sma50_i * 100 if sma50_i else np.nan,
                "dist_to_ema9_pct": (price_i - ema9_i) / ema9_i * 100 if ema9_i else np.nan,
                "dist_to_ema21_pct": (price_i - ema21_i) / ema21_i * 100 if ema21_i else np.nan,
                "bollinger_bandwidth_percentile": bw_pct,
                "bollinger_squeeze": 1.0 if squeeze_flag else 0.0,
                "macd_hist_pct_of_price": float(macd_hist.iloc[i]) / price_i * 100,
                "macd_line_pct_of_price": float(macd_line.iloc[i]) / price_i * 100,
                "macd_line_slope_3d_pct": (float(macd_line.iloc[i]) - float(macd_line.iloc[i - 3])) / price_i * 100,
                "regime_age_days": float(regime_age) if regime_age is not None else 0.0,
                "macd_line_above_zero": 1.0 if macd_line.iloc[i] >= 0 else 0.0,
                "value_traded_bn": price_i * float(volume.iloc[i]) / 1e9,
                "obv_bearish_divergence": 1.0 if bool(bearish_div_s.iloc[i]) else 0.0,
                "obv_bullish_divergence": 1.0 if bool(bullish_div_s.iloc[i]) else 0.0,
                "ret_1d_pct": ret1d_s.iloc[i],
                "ret_5d_pct": ret5d_s.iloc[i],
            }
            date_str = str(hist_df.index[i].date())

            if signal_cross:
                hit_s = any(r >= BREAKOUT_THRESHOLD_PCT for r in fwd_returns_signal)
                events.append({"ticker": ticker, "date": date_str, "cross_type": "SIGNAL_CROSS", "hit": hit_s, "max_fwd_return": round(max(fwd_returns_signal), 2), **feat})
            if centerline_cross:
                hit_c = any(r >= BREAKOUT_THRESHOLD_PCT for r in fwd_returns_centerline)
                events.append({"ticker": ticker, "date": date_str, "cross_type": "CENTERLINE_CROSS", "hit": hit_c, "max_fwd_return": round(max(fwd_returns_centerline), 2), **feat})

    print(f"Ticker dilewati (histori lokal <{MIN_BARS} bar): {skipped}/{len(tickers)}")
    return pd.DataFrame(events)


def report_profile(df: pd.DataFrame, cross_type: str):
    g = df[df["cross_type"] == cross_type]
    if g.empty:
        print(f"\n⚠️ Tidak ada event {cross_type}.")
        return

    winners = g[g["hit"]]
    losers = g[~g["hit"]]
    hit_rate = len(winners) / len(g) * 100

    print("\n" + "=" * 100)
    print(f"{cross_type} — n={len(g)} | winner (naik >={BREAKOUT_THRESHOLD_PCT}% dalam {FORWARD_DAYS}hari) = {len(winners)} ({hit_rate:.1f}%) | loser = {len(losers)}")
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

    print("\nBaca ini: |Cohen's d| >=0.2 kecil, >=0.5 sedang, >=0.8 besar (konvensi statistik umum).")
    print("Fitur dengan d POSITIF besar = winner punya nilai LEBIH TINGGI dari loser (kandidat")
    print("kriteria BONUS). d NEGATIF besar = winner punya nilai LEBIH RENDAH (kandidat kriteria")
    print("PEMBATAS/hindari nilai tinggi). Fitur dengan d mendekati 0 TIDAK membedakan sama sekali")
    print("— jangan dipakai sebagai kriteria prioritisasi walau kelihatan relevan secara intuisi.")


def main():
    scored, _ = nightly_engine.load_daily_scan_cache_allow_stale()
    tickers = sorted(scored.keys()) if scored else []
    print(f"Universe: {len(tickers)} ticker (dari cache /eodscan terakhir)\n")
    if not tickers:
        print("⚠️ Cache /eodscan kosong — jalankan /eodscan dulu di server.")
        return

    df = collect_events(tickers)
    print(f"Total event (SIGNAL_CROSS + CENTERLINE_CROSS, gabungan baris): {len(df)}\n")
    if df.empty:
        print("⚠️ Tidak ada event MACD cross yang ditemukan.")
        return

    report_profile(df, "SIGNAL_CROSS")
    report_profile(df, "CENTERLINE_CROSS")


if __name__ == "__main__":
    main()
