"""
backtest/research_explosive_q1_profile_test.py — MBSS v2, user request
lanjutan setelah quintile sweep research_explosive_lane_v2_quintile_sweep.py
menemukan pola U (BUKAN monoton) di 3 dari 4 dimensi -- Q1 (ekstrem RENDAH:
jauh DI BAWAH SMA50, slope landai/negatif, momentum 5hari negatif) justru
mengalahkan Q2/Q3 (zona tengah/flat). User curiga ini "jalur journey
berbeda" (bukan noise) -- sebelum diputuskan cutoff atau dipertahankan,
diuji dulu apakah Q1 genuinely profil terpisah yang koheren atau cuma
kebetulan/tumpang-tindih.

EMPAT uji:
  A. OVERLAP -- seberapa besar tumpang tindih anggota Q1 di 3 dimensi
     (dist_to_sma50/macd_slope/ret_5d)? Kalau overlap-nya tinggi, ini SATU
     populasi yang keliatan 3x di quintile sweep terpisah, BUKAN 3 bukti
     independen.
  B. REGIME AGE -- apakah Q1 (komposit, bottom quintile di ketiganya)
     rata-rata regime_age_days-nya LEBIH MUDA dari Q5? Kalau ya, mendukung
     hipotesis "journey" (Q1 = tahap awal SAMA perjalanan yang nanti jadi
     Q5) -- BUKAN mekanisme terpisah.
  C. WINNER PROFILE DI DALAM Q1 -- Cohen's d winner/loser KHUSUS di dalam
     populasi Q1 komposit -- apakah ada pola pembeda yang koheren (bukan
     random), pakai fitur causal yang sama dengan winner_profile scripts.
  D. JOURNEY CHECK -- untuk Q1 komposit, apakah dist_to_sma50_pct 5 hari
     KEMUDIAN (kalau data tersedia) BERGERAK ke arah Q4/Q5 (pulih) LEBIH
     BESAR pada kasus yang explosive dibanding yang tidak -- test langsung
     hipotesis "journey" vs "mekanisme rebound tajam berdiri sendiri".

Run di server:
    python backtest/research_explosive_q1_profile_test.py

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

FORWARD_WINDOW_DAYS = 5
JOURNEY_CHECK_DAYS = 5
MIN_BARS = 150
WARMUP_BARS = 55
EVAL_TAIL_BUFFER = max(FORWARD_WINDOW_DAYS, JOURNEY_CHECK_DAYS) + 2
TOP_N_FEATURES = 15

FEATURE_COLS = [
    "vol_ratio", "adx", "rsi", "cmf", "close_pos_day",
    "bollinger_bandwidth_percentile", "bollinger_squeeze",
    "macd_hist_pct_of_price", "macd_line_pct_of_price",
    "regime_age_days", "macd_line_above_zero", "value_traded_bn",
    "obv_bearish_divergence", "obv_bullish_divergence", "ret_1d_pct",
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

        macd_line, _, macd_hist = core.calculate_macd(close)
        adx_s = core.calculate_adx(high, low, close)
        rsi_s = core.calculate_rsi(close)
        cmf_s = core.calculate_cmf(high, low, close, volume)
        obv_s = core.calculate_obv(close, volume)
        bandwidth = _bandwidth_series(close)
        sma50 = close.rolling(50).mean()
        low_10d = low.rolling(10).min()
        high_10d = high.rolling(10).max()
        close_pos_day_s = (close - low) / (high - low).replace(0, np.nan)
        vol_avg20_prev = volume.rolling(20).mean().shift(1)
        vol_ratio_s = volume / vol_avg20_prev.replace(0, np.nan)
        price_chg20_s = close.pct_change(20) * 100
        obv_start20 = obv_s.shift(20)
        obv_chg20_pct_s = (obv_s - obv_start20) / obv_start20.abs().replace(0, np.nan) * 100
        bearish_div_s = (price_chg20_s > -2) & (obv_chg20_pct_s < -5)
        bullish_div_s = (price_chg20_s < 2) & (obv_chg20_pct_s > 5)
        ret1d_s = close.pct_change(1) * 100
        ret5d_s = close.pct_change(5) * 100
        macd_slope_s = macd_line.diff(3)
        dist_sma50_s = (close - sma50) / sma50 * 100

        for i in range(WARMUP_BARS, eval_end):
            if not (macd_hist.iloc[i] > 0):
                continue
            price_i = float(close.iloc[i])
            if price_i <= 0 or pd.isna(dist_sma50_s.iloc[i]) or pd.isna(macd_slope_s.iloc[i]):
                continue

            regime_age = _find_recent_bullish_cross_days_ago(macd_hist, i, 60)
            squeeze_flag, bw_pct = _squeeze_and_percentile(bandwidth, i)

            fwd_window_end = min(i + FORWARD_WINDOW_DAYS, n - 1)
            max_price_fwd = float(close.iloc[i:fwd_window_end + 1].max())
            max_gain_pct = round((max_price_fwd - price_i) / price_i * 100, 2)

            journey_idx = i + JOURNEY_CHECK_DAYS
            dist_sma50_later = float(dist_sma50_s.iloc[journey_idx]) if journey_idx < n and pd.notna(dist_sma50_s.iloc[journey_idx]) else None

            events.append({
                "ticker": ticker,
                "date": str(hist_df.index[i].date()),
                "dist_to_sma50_pct": float(dist_sma50_s.iloc[i]),
                "macd_slope_pct": float(macd_slope_s.iloc[i]) / price_i * 100,
                "ret_5d_pct": ret5d_s.iloc[i],
                "dist_to_sma50_pct_later": dist_sma50_later,
                "explosive_10": max_gain_pct >= 10.0,
                "max_gain_pct": max_gain_pct,
                "vol_ratio": vol_ratio_s.iloc[i],
                "adx": adx_s.iloc[i],
                "rsi": rsi_s.iloc[i],
                "cmf": cmf_s.iloc[i],
                "close_pos_day": close_pos_day_s.iloc[i],
                "bollinger_bandwidth_percentile": bw_pct,
                "bollinger_squeeze": 1.0 if squeeze_flag else 0.0,
                "macd_hist_pct_of_price": float(macd_hist.iloc[i]) / price_i * 100,
                "macd_line_pct_of_price": float(macd_line.iloc[i]) / price_i * 100,
                "regime_age_days": float(regime_age) if regime_age is not None else 0.0,
                "macd_line_above_zero": 1.0 if macd_line.iloc[i] >= 0 else 0.0,
                "value_traded_bn": price_i * float(volume.iloc[i]) / 1e9,
                "obv_bearish_divergence": 1.0 if bool(bearish_div_s.iloc[i]) else 0.0,
                "obv_bullish_divergence": 1.0 if bool(bullish_div_s.iloc[i]) else 0.0,
                "ret_1d_pct": ret1d_s.iloc[i],
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
    print(f"Total observasi: {len(df)}\n")
    if df.empty:
        print("⚠️ Tidak ada observasi.")
        return

    q1_sma50 = df["dist_to_sma50_pct"] <= df["dist_to_sma50_pct"].quantile(0.20)
    q1_slope = df["macd_slope_pct"] <= df["macd_slope_pct"].quantile(0.20)
    q1_ret5d = df["ret_5d_pct"] <= df["ret_5d_pct"].quantile(0.20)
    q5_sma50 = df["dist_to_sma50_pct"] >= df["dist_to_sma50_pct"].quantile(0.80)

    print("=" * 96)
    print("A. OVERLAP anggota Q1 di 3 dimensi (dist_to_sma50 / macd_slope / ret_5d)")
    print("=" * 96)
    n_any = (q1_sma50 | q1_slope | q1_ret5d).sum()
    n_all3 = (q1_sma50 & q1_slope & q1_ret5d).sum()
    print(f"  Q1 di SALAH SATU dimensi (union): {n_any}")
    print(f"  Q1 di KETIGA dimensi sekaligus (intersection): {n_all3} ({n_all3 / max(1, n_any) * 100:.1f}% dari union)")
    print(f"  Overlap pairwise: sma50&slope={  (q1_sma50 & q1_slope).sum()}, sma50&ret5d={(q1_sma50 & q1_ret5d).sum()}, slope&ret5d={(q1_slope & q1_ret5d).sum()}")

    q1_all3 = df[q1_sma50 & q1_slope & q1_ret5d].copy()
    q5_group = df[q5_sma50].copy()
    print(f"\n  n Q1-komposit (dipakai uji B/C/D di bawah): {len(q1_all3)}")

    print("\n" + "=" * 96)
    print("B. REGIME AGE: Q1-komposit vs Q5 (dist_to_sma50)")
    print("=" * 96)
    print(f"  Q1-komposit: avg regime_age_days={q1_all3['regime_age_days'].mean():.1f} | median={q1_all3['regime_age_days'].median():.1f}")
    print(f"  Q5 (dist_to_sma50 atas): avg regime_age_days={q5_group['regime_age_days'].mean():.1f} | median={q5_group['regime_age_days'].median():.1f}")
    print("  Baca ini: kalau Q1 JAUH LEBIH MUDA dari Q5 -- mendukung hipotesis 'journey' (Q1 cuma tahap")
    print("  awal). Kalau regime_age MIRIP -- Q1 bukan sekadar 'lebih muda', genuinely beda mekanisme.")

    if len(q1_all3) >= 50:
        winners = q1_all3[q1_all3["explosive_10"]]
        losers = q1_all3[~q1_all3["explosive_10"]]
        wr = len(winners) / len(q1_all3) * 100
        print("\n" + "=" * 96)
        print(f"C. WINNER PROFILE DI DALAM Q1-KOMPOSIT — n={len(q1_all3)} | explosive-rate={wr:.1f}%")
        print("=" * 96)
        rows = []
        for feat in FEATURE_COLS:
            d = cohens_d(winners[feat], losers[feat])
            rows.append({"feature": feat, "mean_winner": winners[feat].dropna().mean(), "mean_loser": losers[feat].dropna().mean(), "cohens_d": d})
        ranked = sorted(rows, key=lambda r: abs(r["cohens_d"]), reverse=True)
        print(f"{'Fitur':<32} {'Mean Winner':>14} {'Mean Loser':>14} {'Cohens d':>10}")
        for r in ranked[:TOP_N_FEATURES]:
            mw = f"{r['mean_winner']:.2f}" if pd.notna(r["mean_winner"]) else "-"
            ml = f"{r['mean_loser']:.2f}" if pd.notna(r["mean_loser"]) else "-"
            print(f"{r['feature']:<32} {mw:>14} {ml:>14} {r['cohens_d']:>10.3f}")

        print("\n" + "=" * 96)
        print(f"D. JOURNEY CHECK: dist_to_sma50_pct {JOURNEY_CHECK_DAYS} hari kemudian, winner vs loser (Q1-komposit)")
        print("=" * 96)
        j = q1_all3.dropna(subset=["dist_to_sma50_pct_later"])
        jw = j[j["explosive_10"]]
        jl = j[~j["explosive_10"]]
        if not jw.empty and not jl.empty:
            print(f"  Hari ini      : winner avg={jw['dist_to_sma50_pct'].mean():+.2f}% | loser avg={jl['dist_to_sma50_pct'].mean():+.2f}%")
            print(f"  {JOURNEY_CHECK_DAYS} hari kemudian: winner avg={jw['dist_to_sma50_pct_later'].mean():+.2f}% | loser avg={jl['dist_to_sma50_pct_later'].mean():+.2f}%")
            print(f"  Perubahan     : winner {jw['dist_to_sma50_pct_later'].mean() - jw['dist_to_sma50_pct'].mean():+.2f}pp | loser {jl['dist_to_sma50_pct_later'].mean() - jl['dist_to_sma50_pct'].mean():+.2f}pp")
        print("\n  Baca ini: kalau WINNER pulih JAUH LEBIH BESAR menuju SMA50/di atasnya dibanding LOSER --")
        print("  explosive move di Q1 itu ADALAH proses 'pulih ke Q4/Q5' (journey benar). Kalau perubahan")
        print("  mirip/kecil dua-duanya -- winner meledak SAMBIL TETAP di bawah SMA50 (rebound tajam berdiri")
        print("  sendiri, bukan journey menuju profil Q5).")
    else:
        print("\n⚠️ Sampel Q1-komposit terlalu kecil buat uji C/D.")


if __name__ == "__main__":
    main()
