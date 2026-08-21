"""
backtest/research_macd_slope_q3q4_episode_backtest.py — MBSS v2, user
request lanjutan setelah quintile sweep slope magnitude (research_macd_
approach_depth_and_range.py, Report D): Q3-Q4 (slope 3-hari MACD line,
dinormalisasi harga, kira-kira 0.37%-1.19% per 3 hari) jadi kandidat range
sinyal "kekuatan arus naik". User minta metodologi BEDA dari semua script
MACD sebelumnya di sesi ini — bukan snapshot cross-sectional (tiap
ticker-hari dihitung sebagai observasi terpisah, satu episode sinyal yang
bertahan N hari bisa ke-COUNT N kali), tapi EPISODE-BASED, mensimulasikan
siklus hidup 1 "trade" per kejadian, mirip /winrate live:

  1. ENTRY = hari PALING AWAL dalam satu regime histogram bullish (sejak
     signal-line cross) di mana macd_line MASIH <0 DAN slope 3-hari ada
     di range Q3-Q4 (dihitung ULANG di sini dari populasi script ini
     sendiri, bukan angka hardcode dari research_macd_approach_depth_and_
     range.py -- self-consistent).
  2. RESOLVE = jalan maju hari demi hari dari ENTRY sampai SALAH SATU:
     a. macd_line akhirnya cross centerline (>=0) -> hitung kenaikan
        harga TERTINGGI dalam 5 hari SETELAH hari cross itu, relatif ke
        harga ENTRY (bukan harga hari cross) -- "resolve" value.
        WIN kalau kenaikan itu >0% (harga PERNAH naik dari entry dalam
        window itu), LOSE kalau sampai crossing+5hari TETAP tidak pernah
        melebihi harga entry ("gagal naik dari entry").
     b. regime histogram MATI duluan (balik bearish) SEBELUM sempat cross
        centerline -> LOSE ("gagal cross centerline").
     c. timeout (>40 hari sejak entry, belum cross & regime masih hidup)
        -> LOSE (jarang, tapi dibatasi supaya tidak nunggu tanpa batas).
  3. SATU regime histogram = SATU sample maksimal (bukan tiap hari yang
     lolos syarat slope Q3-Q4 dihitung terpisah -- "jangan deduplicate
     tickersnya" user berarti TICKER BOLEH muncul berkali-kali kalau
     genuinely py punya beberapa EPISODE terpisah di 400 hari histori,
     TAPI tiap episode cuma dihitung SATU KALI, di hari entry paling awal
     -- bukan setiap hari dia terus-menerus lolos syarat).

Run di server:
    python backtest/research_macd_slope_q3q4_episode_backtest.py

Pakai OHLCV lokal saja, zero fetch/network cost. Murni observasi, belum
mengubah formula produksi apa pun.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import engine.legacy_core as core
import engine.nightly as nightly_engine

MIN_BARS = 150
MACD_LINE_SLOPE_DAYS = 3
RESOLVE_WINDOW_DAYS = 5    # kenaikan tertinggi dalam N hari SETELAH cross centerline
MAX_WAIT_DAYS = 40         # timeout kalau belum cross & regime masih hidup


def collect_slope_population(tickers: list) -> pd.DataFrame:
    """Populasi SAMA dengan research_macd_approach_depth_and_range.py's precondition -- dipakai HANYA untuk menghitung batas Q3/Q4 slope secara self-consistent, bukan buat episode."""
    rows = []
    for ticker in tickers:
        try:
            hist_df = core.get_ohlcv_daily_from_db(ticker, limit=400)
        except Exception:
            continue
        if hist_df is None or hist_df.empty or len(hist_df) < MIN_BARS:
            continue
        close = hist_df["Close"].astype(float)
        macd_line, _, macd_hist = core.calculate_macd(close)
        n = len(hist_df)
        for i in range(MACD_LINE_SLOPE_DAYS, n):
            if not (macd_hist.iloc[i] > 0) or macd_line.iloc[i] >= 0:
                continue
            price_i = float(close.iloc[i])
            if price_i <= 0:
                continue
            slope = float(macd_line.iloc[i] - macd_line.iloc[i - MACD_LINE_SLOPE_DAYS])
            if slope <= 0:
                continue
            rows.append(slope / price_i * 100)
    return pd.Series(rows)


def find_episodes(ticker: str, hist_df: pd.DataFrame, slope_lo: float, slope_hi: float) -> list:
    close = hist_df["Close"].astype(float)
    n = len(hist_df)
    macd_line, _, macd_hist = core.calculate_macd(close)

    episodes = []
    i = MACD_LINE_SLOPE_DAYS
    while i < n:
        # Cari AWAL regime histogram bullish (signal-line cross).
        is_regime_start = bool(macd_hist.iloc[i] > 0 and macd_hist.iloc[i - 1] <= 0) if i >= 1 else bool(macd_hist.iloc[i] > 0)
        if not is_regime_start or macd_line.iloc[i] >= 0:
            i += 1
            continue

        # Scan MAJU dalam regime ini, cari hari PALING AWAL yang slope-nya di band Q3-Q4.
        entry_idx = None
        j = i
        while j < n and macd_hist.iloc[j] > 0 and macd_line.iloc[j] < 0:
            if j >= MACD_LINE_SLOPE_DAYS:
                price_j = float(close.iloc[j])
                if price_j > 0:
                    slope_j = float(macd_line.iloc[j] - macd_line.iloc[j - MACD_LINE_SLOPE_DAYS])
                    slope_pct = slope_j / price_j * 100
                    if slope_lo <= slope_pct <= slope_hi:
                        entry_idx = j
                        break
            j += 1

        if entry_idx is None:
            # Regime ini tidak pernah masuk band Q3-Q4 -- tidak ada sample, lanjut cari regime BERIKUTNYA setelah regime ini berakhir.
            i = j + 1
            continue

        # RESOLVE: jalan maju dari entry_idx.
        entry_price = float(close.iloc[entry_idx])
        d = entry_idx + 1
        outcome = None
        fail_reason = None
        days_to_resolve = None
        max_gain_pct = None

        while d < n:
            if macd_line.iloc[d] >= 0:
                # Cross centerline hari ini -- ukur kenaikan tertinggi dari entry dalam RESOLVE_WINDOW_DAYS ke depan.
                window_end = min(d + RESOLVE_WINDOW_DAYS, n - 1)
                max_price = float(close.iloc[d:window_end + 1].max())
                max_gain_pct = round((max_price - entry_price) / entry_price * 100, 2)
                days_to_resolve = d - entry_idx
                outcome = "WIN" if max_gain_pct > 0 else "LOSE"
                fail_reason = None if outcome == "WIN" else "gagal_naik_dari_entry"
                break
            if not (macd_hist.iloc[d] > 0):
                outcome = "LOSE"
                fail_reason = "gagal_cross_centerline"
                days_to_resolve = d - entry_idx
                break
            if d - entry_idx > MAX_WAIT_DAYS:
                outcome = "LOSE"
                fail_reason = "timeout"
                days_to_resolve = d - entry_idx
                break
            d += 1

        if outcome is not None:
            episodes.append({
                "ticker": ticker,
                "entry_date": str(hist_df.index[entry_idx].date()),
                "outcome": outcome,
                "fail_reason": fail_reason,
                "days_to_resolve": days_to_resolve,
                "max_gain_pct": max_gain_pct,
            })
        # else: belum resolve sampai akhir data lokal -- dilewati (bukan sample valid, data belum cukup panjang)

        # Lanjut cari regime BERIKUTNYA setelah regime historigram INI berakhir
        # (bukan setelah resolve -- resolve bisa jauh lewat batas regime kalau
        # sudah cross; regime histogram tetap yang jadi acuan "SATU regime =
        # SATU sample maksimal").
        k = entry_idx
        while k < n and macd_hist.iloc[k] > 0:
            k += 1
        i = k + 1

    return episodes


def main():
    scored, _ = nightly_engine.load_daily_scan_cache_allow_stale()
    tickers = sorted(scored.keys()) if scored else []
    print(f"Universe: {len(tickers)} ticker (dari cache /eodscan terakhir)\n")
    if not tickers:
        print("⚠️ Cache /eodscan kosong — jalankan /eodscan dulu di server.")
        return

    print("Menghitung batas Q3/Q4 slope (self-consistent dari populasi script ini)...")
    slope_pop = collect_slope_population(tickers)
    if len(slope_pop) < 50:
        print("⚠️ Populasi slope terlalu kecil buat quintile.")
        return
    quintiles = slope_pop.quantile([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    slope_lo = float(quintiles.iloc[2])  # batas bawah Q3 (persentil 40)
    slope_hi = float(quintiles.iloc[4])  # batas atas Q4 (persentil 80)
    print(f"Range slope Q3-Q4 (persentil 40-80): {slope_lo:.3f}% s/d {slope_hi:.3f}% (per 3 hari, dinormalisasi harga)\n")

    all_episodes = []
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
        all_episodes.extend(find_episodes(ticker, hist_df, slope_lo, slope_hi))

    print(f"Ticker dilewati (histori lokal <{MIN_BARS} bar): {skipped}/{len(tickers)}")
    print(f"Total EPISODE ter-resolve (1 sample = 1 regime, TIDAK di-dedup di level ticker): {len(all_episodes)}")
    unique_tickers = len({e['ticker'] for e in all_episodes})
    print(f"Ticker unik yang berkontribusi >=1 episode: {unique_tickers} (avg {len(all_episodes)/max(1,unique_tickers):.1f} episode/ticker)\n")

    if not all_episodes:
        print("⚠️ Tidak ada episode ter-resolve.")
        return

    df = pd.DataFrame(all_episodes)
    wins = df[df["outcome"] == "WIN"]
    loses = df[df["outcome"] == "LOSE"]
    win_rate = len(wins) / len(df) * 100

    print("=" * 90)
    print(f"HASIL: n={len(df)} episode | WIN={len(wins)} ({win_rate:.1f}%) | LOSE={len(loses)}")
    print("=" * 90)
    if not wins.empty:
        print(f"  WIN  -- avg max_gain_pct={wins['max_gain_pct'].mean():+.2f}% | median={wins['max_gain_pct'].median():+.2f}% | avg days_to_resolve={wins['days_to_resolve'].mean():.1f}")
    for reason in ["gagal_cross_centerline", "gagal_naik_dari_entry", "timeout"]:
        sub = loses[loses["fail_reason"] == reason]
        if not sub.empty:
            print(f"  LOSE ({reason:<24}) n={len(sub):<5} ({len(sub)/len(df)*100:4.1f}% dari total) | avg days_to_resolve={sub['days_to_resolve'].mean():.1f}")

    print("\nContoh 10 episode WIN dengan max_gain_pct tertinggi:")
    for _, row in wins.sort_values("max_gain_pct", ascending=False).head(10).iterrows():
        print(f"  {row['ticker']:<6} entry {row['entry_date']} -> resolve {row['days_to_resolve']}hari, max_gain {row['max_gain_pct']:+.2f}%")

    print("\nBaca ini: win_rate & avg max_gain di atas itu simulasi 1-trade-per-episode (bukan cross-")
    print("sectional harian) -- lebih dekat ke realita 'kalau saya masuk saat entry dan tunggu sampai")
    print("breakout beneran terjadi, seberapa sering untung dan seberapa besar'. Bandingkan win_rate ini")
    print("dengan intuisi win-rate /winrate live yang sudah ada (biasanya 40-60% dianggap sehat untuk")
    print("setup day-trade) -- kalau JAUH di bawah itu, range Q3-Q4 slope BELUM cukup sebagai kriteria")
    print("tunggal, mungkin perlu digabung syarat lain (cross_days_ago, depth, dst).")


if __name__ == "__main__":
    main()
