"""
backtest/research_macd_explosive_gain_profile.py — MBSS v2, user request
lanjutan dari seluruh riset MACD sesi ini (approach tier, FAR+STEEP,
histogram thinness/decelerating, dst). Pertanyaan BARU: dari populasi
"entry sehat" (dalam rentang bullish MACD, sudah terbukti downside
ter-manage), APA profil teknikal yang membedakan yang cuma naik SEDIKIT
dari yang EKSPLOSIF (>=10%) atau bahkan mendekati ARA (>=20%, proxy kasar
-- band auto-reject IDX riil 20-35% tergantung tier harga, BUKAN angka
persis)?

Beda dari research_macd_cross_winner_profile_v1/v2.py (yang fokus di HARI
EVENT signal/centerline cross saja, threshold win tunggal >=4%): script ini
(a) populasi LEBIH LUAS -- SETIAP hari selama regime histogram bullish
aktif (bukan cuma hari cross), sesuai kalimat user "dalam rentang bullish
macd tersebut"; (b) DUA threshold outcome sekaligus (>=10% "explosive",
>=20% "mendekati ARA") supaya kelihatan apakah profil pembeda BERUBAH saat
bar dinaikkan, bukan cuma satu potongan.

Fitur: SAMA ~20 fitur causal dari winner_profile_v1/v2.py (REUSE, bukan
re-derive) DITAMBAH 2 fitur baru dari temuan sesi ini: macd_slope_pct
(magnitude slope 3-hari, dinormalisasi harga) dan closeness_to_centerline_
abs (jarak absolut MACD line ke centerline, dinormalisasi harga -- makin
kecil makin dekat). Metodologi tetap Cohen's d winner/loser ranking,
konvensi riset proyek sendiri (research/screener_v2/findings.md).

Run di server:
    python backtest/research_macd_explosive_gain_profile.py

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
EXPLOSIVE_THRESHOLD_PCT = 10.0
ARA_PROXY_THRESHOLD_PCT = 20.0  # proxy kasar, BUKAN band ARA riil per-tier harga
MIN_BARS = 150
WARMUP_BARS = 55
EVAL_TAIL_BUFFER = FORWARD_WINDOW_DAYS + 2
TOP_N_FEATURES = 18

FEATURE_COLS = [
    "vol_ratio", "adx", "rsi", "cmf", "day_range_pct_10d", "close_pos_day",
    "dist_to_sma20_pct", "dist_to_sma50_pct", "dist_to_ema9_pct", "dist_to_ema21_pct",
    "bollinger_bandwidth_percentile", "bollinger_squeeze",
    "macd_hist_pct_of_price", "macd_line_pct_of_price", "macd_slope_pct",
    "closeness_to_centerline_abs", "regime_age_days", "macd_line_above_zero",
    "value_traded_bn", "obv_bearish_divergence", "obv_bullish_divergence",
    "ret_1d_pct", "ret_5d_pct",
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
        macd_slope_s = macd_line.diff(3)

        for i in range(WARMUP_BARS, eval_end):
            # Populasi LUAS: SETIAP hari selama regime histogram bullish aktif
            # (bukan cuma hari cross) -- sesuai "dalam rentang bullish macd".
            if not (macd_hist.iloc[i] > 0):
                continue
            price_i = float(close.iloc[i])
            if price_i <= 0:
                continue

            regime_age = _find_recent_bullish_cross_days_ago(macd_hist, i, 40)
            squeeze_flag, bw_pct = _squeeze_and_percentile(bandwidth, i)
            sma20_i, sma50_i, ema9_i, ema21_i = sma20.iloc[i], sma50.iloc[i], ema9.iloc[i], ema21.iloc[i]

            fwd_window_end = min(i + FORWARD_WINDOW_DAYS, n - 1)
            max_price_fwd = float(close.iloc[i:fwd_window_end + 1].max())
            max_gain_pct = round((max_price_fwd - price_i) / price_i * 100, 2)

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
                "macd_slope_pct": float(macd_slope_s.iloc[i]) / price_i * 100 if pd.notna(macd_slope_s.iloc[i]) else np.nan,
                "closeness_to_centerline_abs": abs(float(macd_line.iloc[i]) / price_i * 100),
                "regime_age_days": float(regime_age) if regime_age is not None else 0.0,
                "macd_line_above_zero": 1.0 if macd_line.iloc[i] >= 0 else 0.0,
                "value_traded_bn": price_i * float(volume.iloc[i]) / 1e9,
                "obv_bearish_divergence": 1.0 if bool(bearish_div_s.iloc[i]) else 0.0,
                "obv_bullish_divergence": 1.0 if bool(bullish_div_s.iloc[i]) else 0.0,
                "ret_1d_pct": ret1d_s.iloc[i],
                "ret_5d_pct": ret5d_s.iloc[i],
            }

            events.append({
                "ticker": ticker,
                "date": str(hist_df.index[i].date()),
                "max_gain_pct": max_gain_pct,
                "explosive_10": max_gain_pct >= EXPLOSIVE_THRESHOLD_PCT,
                "ara_proxy_20": max_gain_pct >= ARA_PROXY_THRESHOLD_PCT,
                **feat,
            })

    print(f"Ticker dilewati (histori lokal <{MIN_BARS} bar): {skipped}/{len(tickers)}")
    return pd.DataFrame(events)


def report_profile(df: pd.DataFrame, label_col: str, title: str):
    winners = df[df[label_col]]
    losers = df[~df[label_col]]
    rate = len(winners) / len(df) * 100

    print("\n" + "=" * 100)
    print(f"{title} — n_total={len(df)} | tercapai={len(winners)} ({rate:.1f}%) | tidak tercapai={len(losers)}")
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

    print(f"{'Fitur':<32} {'Mean Tercapai':>14} {'Mean Tidak':>14} {'Cohens d':>10}")
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
    print(f"Total observasi (SETIAP hari selama regime MACD bullish aktif): {len(df)}\n")
    if df.empty:
        print("⚠️ Tidak ada observasi.")
        return

    print("=" * 100)
    print(f"BASE RATE dalam populasi 'bullish MACD' (window {FORWARD_WINDOW_DAYS} hari ke depan)")
    print("=" * 100)
    print(f"  naik >=5%  : {(df['max_gain_pct'] >= 5).mean() * 100:5.1f}%")
    print(f"  naik >=10% (EXPLOSIVE) : {(df['max_gain_pct'] >= 10).mean() * 100:5.1f}%")
    print(f"  naik >=20% (proxy ARA) : {(df['max_gain_pct'] >= 20).mean() * 100:5.1f}%")

    report_profile(df, "explosive_10", "PROFIL >=10% (EXPLOSIVE)")
    report_profile(df, "ara_proxy_20", "PROFIL >=20% (PROXY MENDEKATI ARA)")

    print("\nBaca ini: |Cohen's d| >=0.2 kecil, >=0.5 sedang, >=0.8 besar. Bandingkan urutan fitur di")
    print("kedua tabel -- kalau fitur PALING KUAT di explosive JUGA paling kuat di proxy-ARA (arah &")
    print("besaran mirip), itu sinyal ROBUST dipakai lintas-tingkat gain. Kalau beda jauh, artinya profil")
    print("'naik sedikit lebih' vs 'naik ekstrem' itu didorong hal yang BERBEDA -- jangan disamakan.")


if __name__ == "__main__":
    main()
