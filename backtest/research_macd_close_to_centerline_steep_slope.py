"""
backtest/research_macd_close_to_centerline_steep_slope.py — MBSS v2, user
request lanjutan setelah episode backtest slope Q3-Q4 (episode win-rate
47.8%, tapi P(cross centerline sama sekali)=52.1% jadi bottleneck, sementara
P(menang | benar-benar cross)=91.7% sangat kuat).

User usul konsep BARU: prioritaskan kandidat SDT yang HARI INI sudah DEKAT
ke centerline DAN slope-nya sedang CURAM (bukan kandidat yang baru pertama
kali ketemu, masih jauh di bawah) -- dipakai sebagai RANKING harian (top 5),
bukan gate keras.

PERINGATAN METODOLOGIS PENTING (harus dicek eksplisit, bukan diasumsikan
aman): research_macd_approach_score_v1.py (backtest PALING AWAL sesi ini)
sudah menguji versi awal ide ini (skor gabungan recency+ETA-ke-centerline)
dan MENEMUKAN ARAH TERBALIK -- skor "paling dekat ke centerline" ternyata
DIDOMINASI kasus cross BARU SAJA (1 hari lalu, spike simultan), dan forward
return-nya JUSTRU PALING JELEK dari semua skor (momentum sudah "kepakai").
Jadi "dekat centerline" BUKAN otomatis aman dipakai lagi -- HARUS dicek
ulang di sini apakah "dekat + CURAM" (kombinasi baru, beda dari v1 yang
cuma "dekat" independen) menghindari confound itu, dengan melaporkan
cross_days_ago rata-rata per sel supaya kelihatan jelas apakah kandidat
"CLOSE+STEEP" itu genuinely matang (bukan cuma fresh spike berkedok slope
curam).

Metodologi: cross-tab 3x3 (closeness_to_centerline tercile x slope_
magnitude tercile), forward return fwd3/5/10d + cross_days_ago rata-rata
per sel, dari POPULASI SAMA dengan research_macd_approach_depth_and_range.py
(macd_hist regime bullish, macd_line<0, slope 3hari positif) -- data-driven
tercile, bukan angka dikarang.

Run di server:
    python backtest/research_macd_close_to_centerline_steep_slope.py

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
MACD_LINE_SLOPE_DAYS = 3
CROSS_LOOKBACK_DAYS = 30


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

        for i in range(MACD_LINE_SLOPE_DAYS, eval_end):
            if not (macd_hist.iloc[i] > 0) or macd_line.iloc[i] >= 0:
                continue
            price_i = float(close.iloc[i])
            if price_i <= 0:
                continue
            slope = float(macd_line.iloc[i] - macd_line.iloc[i - MACD_LINE_SLOPE_DAYS])
            if slope <= 0:
                continue

            cross_days_ago = _find_recent_bullish_cross_days_ago(macd_hist, i, CROSS_LOOKBACK_DAYS)

            depth_pct = float(macd_line.iloc[i]) / price_i * 100  # negatif
            closeness_abs = abs(depth_pct)  # makin KECIL = makin DEKAT ke centerline
            slope_pct = slope / price_i * 100  # makin BESAR = makin CURAM

            fwd = {h: round((float(close.iloc[i + h]) - price_i) / price_i * 100, 2) for h in FORWARD_HORIZONS}

            events.append({
                "ticker": ticker,
                "date": str(hist_df.index[i].date()),
                "closeness_abs": closeness_abs,
                "slope_pct": slope_pct,
                "cross_days_ago": cross_days_ago if cross_days_ago is not None else -1,
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
    print(f"Total observasi (precondition SWEET_SPOT/SQUEEZE_RESCUE): {len(df)}\n")
    if df.empty:
        print("⚠️ Tidak ada observasi.")
        return

    # Tercile DATA-DRIVEN dari populasi ini sendiri.
    df["close_tercile"] = pd.qcut(df["closeness_abs"], 3, labels=["CLOSE", "MID", "FAR"], duplicates="drop")
    df["slope_tercile"] = pd.qcut(df["slope_pct"], 3, labels=["FLAT", "MID", "STEEP"], duplicates="drop")

    print("=" * 100)
    print("CROSS-TAB: closeness ke centerline x magnitude slope (3x3, data-driven tercile)")
    print("=" * 100)
    print(f"{'Closeness':<8} {'Slope':<8} {'n':>6} | {'fwd3d':>8} {'fwd5d':>8} {'fwd10d':>8} | {'avg cross_days_ago':>18}")
    for close_t in ["CLOSE", "MID", "FAR"]:
        for slope_t in ["FLAT", "MID", "STEEP"]:
            g = df[(df["close_tercile"] == close_t) & (df["slope_tercile"] == slope_t)]
            if g.empty:
                continue
            valid_cda = g[g["cross_days_ago"] >= 0]["cross_days_ago"]
            cda_avg = valid_cda.mean() if len(valid_cda) else float("nan")
            print(
                f"{close_t:<8} {slope_t:<8} {len(g):>6} | "
                f"{g['fwd_3d'].mean():>+7.2f}% {g['fwd_5d'].mean():>+7.2f}% {g['fwd_10d'].mean():>+7.2f}% | "
                f"{cda_avg:>17.1f}h"
            )
        print()

    print("Baca ini: cari sel CLOSE+STEEP -- kalau fwd return-nya JELAS lebih baik DARI SEMUA sel lain")
    print("DAN avg cross_days_ago-nya TIDAK sangat rendah (bukan 0-3 hari, confound 'fresh spike' yang")
    print("sudah terbukti jelek di v1) -- baru usul prioritisasi 'dekat+curam' ini genuinely valid.")
    print("Kalau avg cross_days_ago sel CLOSE rendah (dekat 0-3), itu confound yang sama dengan v1 --")
    print("'dekat centerline' cuma proxy 'baru saja spike', BUKAN sinyal matang independen.\n")

    # Perbandingan langsung dengan kriteria PRODUKSI/USULAN sebelumnya.
    print("=" * 100)
    print("PERBANDINGAN dengan kriteria lain yang sudah diuji sesi ini")
    print("=" * 100)
    close_steep = df[(df["close_tercile"] == "CLOSE") & (df["slope_tercile"] == "STEEP")]
    sweet_spot = df[(df["cross_days_ago"] >= 14) & (df["cross_days_ago"] <= 23)]
    squeeze_rescue_like = df[(df["cross_days_ago"] >= 0) & (df["cross_days_ago"] <= 11)]

    def _row(label, g):
        if g.empty:
            print(f"  {label:<28} n=0")
            return
        print(f"  {label:<28} n={len(g):<6} | fwd3d={g['fwd_3d'].mean():+.2f}% fwd5d={g['fwd_5d'].mean():+.2f}% fwd10d={g['fwd_10d'].mean():+.2f}%")

    _row("CLOSE+STEEP (usulan baru)", close_steep)
    _row("SWEET_SPOT (14-23 hari, produksi)", sweet_spot)
    _row("0-11 hari (basis SQUEEZE_RESCUE)", squeeze_rescue_like)


if __name__ == "__main__":
    main()
