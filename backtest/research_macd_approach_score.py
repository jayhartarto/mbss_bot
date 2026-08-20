"""
backtest/research_macd_approach_score.py — MBSS v2, user request (diskusi
lanjutan setelah cek manual 15 sampel dari research_macd_squeeze_frequency.py).
Revisi 3 poin dari feedback manual:

1. "macd centerline breakout" sebagai TAG saja sudah terlambat — user minta
   ETA (perkiraan berapa hari lagi MACD line akan cross centerline/garis 0),
   bukan cuma notifikasi setelah kejadian.
2. Signal-line cross (histogram lewat 0) boleh discan mundur s/d 20 hari:
   makin BARU cross-nya + MACD line masih naik + makin DEKAT ke centerline =
   skor makin tinggi. Reuse EXACT logic yang sudah ada di compute_factor_
   scoring (macd_cross_days_ago/macd_cross_direction, engine/scoring.py
   baris ~1372) — bukan bikin ulang.
3. Bollinger squeeze BUKAN filter wajib lagi (kasus ASGR minggu lalu:
   squeeze aktif TAPI MACD line masih turun = false positive; kasus CARE:
   band belum melebar tapi entry tetap valid) — di sini squeeze dijadikan
   SPLIT perbandingan (skor sama, dengan-squeeze vs tanpa-squeeze), bukan
   syarat lolos/tidak.

DESAIN SKOR (composite, 0-100, informational/uji dulu — BUKAN skor
production):
  - precondition (WAJIB, bukan skor): ada bullish signal-line cross dalam
    20 hari terakhir (macd_hist regime sekarang positif, umurnya <=20 hari)
    DAN macd_line MASIH DI BAWAH 0 (belum cross centerline — ini scope
    "approaching", bukan "sudah breakout") DAN macd_line slope 5-hari
    POSITIF (fix kasus ASGR: histogram menyusut tapi macd_line-nya sendiri
    masih turun -> gagal precondition, tidak diberi skor sama sekali).
  - recency_score (bobot 50%): makin BARU cross-nya (days_ago kecil), makin
    tinggi.
  - eta_score (bobot 50%): ekstrapolasi linear dari slope 5-hari MACD line
    -> perkiraan berapa hari lagi macd_line capai 0. Makin dekat (ETA
    kecil), makin tinggi.
  - squeeze: TIDAK di-blend ke composite (sesuai instruksi user) —
    dilaporkan sebagai cohort split terpisah supaya efek marginalnya
    kelihatan jelas, bukan disembunyikan di dalam satu angka.

VALIDASI: untuk setiap hari yang lolos precondition di histori lokal
(get_ohlcv_daily_from_db, TIDAK fetch), hitung:
  (a) forward return @3d/5d/10d (bukti apakah skor tinggi = hasil lebih
      baik)
  (b) apakah macd_line BENERAN cross centerline dalam 10 hari ke depan
      (bukti langsung apakah "ETA" ini valid prediktif, bukan cuma
      kelihatan masuk akal)
Dikelompokkan per skor (quartile) dan per squeeze/non-squeeze.

Run di server:
    python backtest/research_macd_approach_score.py

BUKAN backtest winrate /eodscan (sinyal ini belum pernah jadi pick) — ini
backtest RETROSPEKTIF LANGSUNG dari OHLCV lokal (forward return dihitung
dari bar yang SUDAH ADA di histori, bukan menunggu /winrate live). Murni
observasi, TIDAK mengubah formula produksi apa pun.
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import engine.legacy_core as core
import engine.nightly as nightly_engine

CROSS_LOOKBACK_DAYS = 20   # scan mundur maksimal berapa hari buat cari bullish signal-line cross
MACD_LINE_SLOPE_DAYS = 5   # window buat hitung slope MACD line (syarat wajib: harus positif)
FORWARD_HORIZONS = (3, 5, 10)  # hari ke depan buat cek forward return
CENTERLINE_HIT_HORIZON = 10    # dalam berapa hari ke depan kita cek apakah MACD line BENERAN cross 0
MIN_BARS = 150
EVAL_TAIL_BUFFER = CENTERLINE_HIT_HORIZON + max(FORWARD_HORIZONS) + 2  # hari terakhir yang di-skip karena butuh data masa depan buat validasi


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
    """
    REUSE exact logic dari compute_factor_scoring's macd_cross_days_ago/
    macd_cross_direction (engine/scoring.py) — cari sudah berapa hari
    histogram berada di regime SEKARANG. None kalau regime sekarang bukan
    bullish (histogram negatif), atau regime-nya sudah lebih tua dari
    max_days_back (cross-nya terlalu lama, di luar scope "baru saja").
    """
    if i < 1 or macd_hist.iloc[i] <= 0:
        return None
    current_sign = True  # bullish
    for days_back in range(1, min(max_days_back + 1, i + 1)):
        idx = i - days_back
        if idx < 0:
            break
        past_sign = macd_hist.iloc[idx] > 0
        if past_sign != current_sign:
            return days_back
    return None  # regime sudah berlangsung > max_days_back hari


def compute_macd_approach_score(macd_line: pd.Series, macd_hist: pd.Series, i: int) -> dict | None:
    """Return None kalau precondition tidak terpenuhi (bukan kandidat approach sama sekali)."""
    cross_days_ago = _find_recent_bullish_cross_days_ago(macd_hist, i, CROSS_LOOKBACK_DAYS)
    if cross_days_ago is None:
        return None
    if macd_line.iloc[i] >= 0:
        return None  # sudah cross centerline -- di luar scope "approaching"
    if i < MACD_LINE_SLOPE_DAYS:
        return None
    slope = macd_line.iloc[i] - macd_line.iloc[i - MACD_LINE_SLOPE_DAYS]
    if slope <= 0:
        return None  # syarat wajib: MACD line SENDIRI masih naik (fix kasus ASGR)

    recency_score = max(0.0, 100.0 - (cross_days_ago / CROSS_LOOKBACK_DAYS) * 100.0)

    daily_rate = slope / MACD_LINE_SLOPE_DAYS
    remaining = -macd_line.iloc[i]
    eta_days = remaining / daily_rate if daily_rate > 0 else None
    eta_score = max(0.0, 100.0 - min(eta_days, 30.0) / 30.0 * 100.0) if eta_days is not None else 0.0

    composite = round(recency_score * 0.5 + eta_score * 0.5, 1)
    return {
        "composite": composite,
        "recency_score": round(recency_score, 1),
        "eta_score": round(eta_score, 1),
        "eta_days": round(eta_days, 1) if eta_days is not None else None,
        "cross_days_ago": cross_days_ago,
    }


def _bucket_label(score: float) -> str:
    if score >= 75: return "Q4 (75-100, tertinggi)"
    if score >= 50: return "Q3 (50-74)"
    if score >= 25: return "Q2 (25-49)"
    return "Q1 (0-24, terendah)"


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
        bandwidth = _bandwidth_series(closes)

        n = len(hist_df)
        eval_end = n - EVAL_TAIL_BUFFER  # sisakan buffer di ujung buat forward return/centerline-hit validation
        if eval_end <= MACD_LINE_SLOPE_DAYS:
            continue

        for i in range(MACD_LINE_SLOPE_DAYS, eval_end):
            r = compute_macd_approach_score(macd_line, macd_hist, i)
            if r is None:
                continue

            squeeze = _is_squeeze_on_day(bandwidth, i)
            price_i = closes.iloc[i]

            fwd_returns = {}
            for h in FORWARD_HORIZONS:
                fwd_returns[h] = round((closes.iloc[i + h] - price_i) / price_i * 100, 2)

            centerline_hit = any(macd_line.iloc[i + k] >= 0 for k in range(1, CENTERLINE_HIT_HORIZON + 1))

            rows.append({
                "ticker": ticker,
                "date": str(hist_df.index[i].date()),
                **r,
                "squeeze": squeeze,
                "centerline_hit_10d": centerline_hit,
                **{f"fwd_{h}d": fwd_returns[h] for h in FORWARD_HORIZONS},
            })

    print(f"Ticker dilewati (histori lokal <{MIN_BARS} bar): {skipped}/{len(tickers)}")
    print(f"Total observasi (ticker-hari) lolos precondition: {len(rows)}\n")

    if not rows:
        print("⚠️ TIDAK ADA observasi yang lolos precondition sama sekali (bullish cross <=20 hari + MACD line masih naik + belum cross centerline). Precondition mungkin masih terlalu ketat -- coba longgarkan CROSS_LOOKBACK_DAYS atau syarat slope.")
        return

    df = pd.DataFrame(rows)

    print("=" * 78)
    print("HASIL PER BUCKET SKOR COMPOSITE (recency 50% + eta 50%, squeeze TIDAK di-blend)")
    print("=" * 78)
    df["bucket"] = df["composite"].apply(_bucket_label)
    for bucket in ["Q4 (75-100, tertinggi)", "Q3 (50-74)", "Q2 (25-49)", "Q1 (0-24, terendah)"]:
        g = df[df["bucket"] == bucket]
        if g.empty:
            print(f"  {bucket:<26} n=0")
            continue
        hit_rate = g["centerline_hit_10d"].mean() * 100
        line = f"  {bucket:<26} n={len(g):<5}"
        for h in FORWARD_HORIZONS:
            line += f" | fwd{h}d avg={g[f'fwd_{h}d'].mean():+5.2f}%"
        line += f" | centerline-hit@10d={hit_rate:5.1f}%"
        print(line)

    print("\nBaca ini: kalau Q4 (skor tertinggi) punya forward return DAN centerline-hit rate")
    print("yang JELAS lebih baik daripada Q1 (skor terendah) secara konsisten di semua horizon,")
    print("komponen recency+eta ini genuinely prediktif. Kalau berantakan/tidak monoton,")
    print("bobot 50/50 atau definisi eta perlu direvisi.")

    print("\n" + "=" * 78)
    print("EFEK SQUEEZE (cohort split, BUKAN filter — apakah squeeze menambah nilai?)")
    print("=" * 78)
    for squeeze_flag, label in [(True, "DENGAN squeeze"), (False, "TANPA squeeze")]:
        g = df[df["squeeze"] == squeeze_flag]
        if g.empty:
            print(f"  {label:<18} n=0")
            continue
        hit_rate = g["centerline_hit_10d"].mean() * 100
        line = f"  {label:<18} n={len(g):<5}"
        for h in FORWARD_HORIZONS:
            line += f" | fwd{h}d avg={g[f'fwd_{h}d'].mean():+5.2f}%"
        line += f" | centerline-hit@10d={hit_rate:5.1f}%"
        print(line)

    print("\nBaca ini: kalau 'DENGAN squeeze' JELAS lebih baik dari 'TANPA squeeze' (di skor")
    print("bucket yang sama), squeeze layak jadi BONUS skor (bukan filter wajib, sesuai")
    print("instruksi). Kalau hampir sama atau malah lebih jelek, squeeze TIDAK perlu")
    print("ditambahkan sama sekali ke formula ini -- cukup dilaporkan sebagai info, bukan skor.")

    print("\n" + "=" * 78)
    print("CONTOH OBSERVASI SKOR TERTINGGI (top 15 by composite)")
    print("=" * 78)
    top = df.sort_values("composite", ascending=False).head(15)
    for _, row in top.iterrows():
        fwd_str = " ".join(f"{h}d={row[f'fwd_{h}d']:+.1f}%" for h in FORWARD_HORIZONS)
        print(f"  {row['ticker']:<6} {row['date']}  composite={row['composite']:5.1f} (recency={row['recency_score']:.0f} eta={row['eta_score']:.0f}, ETA~{row['eta_days']}d, cross {row['cross_days_ago']}d lalu) squeeze={row['squeeze']}  fwd: {fwd_str}  centerline_hit_10d={row['centerline_hit_10d']}")


if __name__ == "__main__":
    main()
