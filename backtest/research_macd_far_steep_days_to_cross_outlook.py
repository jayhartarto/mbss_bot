"""
backtest/research_macd_far_steep_days_to_cross_outlook.py — MBSS v2, user
request lanjutan setelah cross-tab CLOSE/FAR x slope (research_macd_close_
to_centerline_steep_slope.py, temuan: sel FAR+STEEP TERBAIK dari 9 sel, fwd10d
+1.41%, mengalahkan bahkan SWEET_SPOT produksi). User sekarang mau bangun
mekanisme "H-2/H-1 advance warning": begitu SDT ketemu setup FAR+STEEP, JANGAN
langsung tunggu sampai HC konfirmasi cross (bisa lama, rata2 ~8-9 hari, dan
47% GAGAL cross sama sekali) -- re-munculkan lagi di SDT PAS mendekati hari
crossing (H-1/H-2), supaya user dapat peringatan lanjutan tepat waktu.

Untuk itu perlu OUTLOOK: begitu FAR+STEEP terdeteksi (hari-0/entry), berapa
hari sampai dia BENAR-BENAR cross centerline? Hari ke-5? Ke-7? Berapa
sebarannya (bukan cuma rata-rata, karena bisa sangat skewed)?

Metodologi: SAMA persis dengan research_macd_slope_q3q4_episode_backtest.py
(episode-based, 1 regime = 1 sample, entry = hari PALING AWAL yang lolos
syarat, resolve = jalan maju sampai cross/regime mati/timeout) -- HANYA
syarat entry-nya diganti dari "slope Q3-Q4" jadi "closeness FAR tercile DAN
slope STEEP tercile" (dihitung self-consistent dari populasi script ini,
SAMA definisi dengan research_macd_close_to_centerline_steep_slope.py).

OUTPUT UTAMA: distribusi days_to_resolve (bukan cuma rata-rata) untuk
episode yang BENAR-BENAR cross -- persentil (min/25/median/75/max) + bucket
histogram (1-2 hari, 3-4, 5-6, dst) supaya kelihatan hari mana yang paling
sering, bukan cuma rata-rata yang bisa menyesatkan kalau distribusinya skewed.

Run di server:
    python backtest/research_macd_far_steep_days_to_cross_outlook.py

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
RESOLVE_WINDOW_DAYS = 5
MAX_WAIT_DAYS = 40
DAY_BUCKETS = [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10), (11, 15), (16, 20), (21, 40)]


def collect_reference_population(tickers: list) -> pd.DataFrame:
    """Populasi referensi (SAMA precondition dengan script2 sebelumnya) buat hitung batas tercile FAR/STEEP self-consistent."""
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
            depth_pct = float(macd_line.iloc[i]) / price_i * 100
            rows.append({"closeness_abs": abs(depth_pct), "slope_pct": slope / price_i * 100})
    return pd.DataFrame(rows)


def find_episodes(ticker: str, hist_df: pd.DataFrame, close_far_lo: float, slope_steep_lo: float) -> list:
    """Entry = hari PALING AWAL dalam 1 regime di mana closeness_abs >= close_far_lo (tercile FAR) DAN slope_pct >= slope_steep_lo (tercile STEEP)."""
    close = hist_df["Close"].astype(float)
    n = len(hist_df)
    macd_line, _, macd_hist = core.calculate_macd(close)

    episodes = []
    i = MACD_LINE_SLOPE_DAYS
    while i < n:
        is_regime_start = bool(macd_hist.iloc[i] > 0 and macd_hist.iloc[i - 1] <= 0) if i >= 1 else bool(macd_hist.iloc[i] > 0)
        if not is_regime_start or macd_line.iloc[i] >= 0:
            i += 1
            continue

        entry_idx = None
        j = i
        while j < n and macd_hist.iloc[j] > 0 and macd_line.iloc[j] < 0:
            if j >= MACD_LINE_SLOPE_DAYS:
                price_j = float(close.iloc[j])
                if price_j > 0:
                    depth_j = float(macd_line.iloc[j]) / price_j * 100
                    closeness_j = abs(depth_j)
                    slope_j = float(macd_line.iloc[j] - macd_line.iloc[j - MACD_LINE_SLOPE_DAYS]) / price_j * 100
                    if closeness_j >= close_far_lo and slope_j >= slope_steep_lo:
                        entry_idx = j
                        break
            j += 1

        if entry_idx is None:
            i = j + 1
            continue

        entry_price = float(close.iloc[entry_idx])
        d = entry_idx + 1
        outcome = None
        fail_reason = None
        days_to_resolve = None
        max_gain_pct = None

        while d < n:
            if macd_line.iloc[d] >= 0:
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

        k = entry_idx
        while k < n and macd_hist.iloc[k] > 0:
            k += 1
        i = k + 1

    return episodes


def _bucket_label(days: int) -> str:
    for lo, hi in DAY_BUCKETS:
        if lo <= days <= hi:
            return f"{lo}-{hi} hari"
    return "40+ hari"


def main():
    scored, _ = nightly_engine.load_daily_scan_cache_allow_stale()
    tickers = sorted(scored.keys()) if scored else []
    print(f"Universe: {len(tickers)} ticker (dari cache /eodscan terakhir)\n")
    if not tickers:
        print("⚠️ Cache /eodscan kosong — jalankan /eodscan dulu di server.")
        return

    print("Menghitung batas tercile FAR (closeness) & STEEP (slope), self-consistent dari populasi ini...")
    ref = collect_reference_population(tickers)
    if len(ref) < 50:
        print("⚠️ Populasi referensi terlalu kecil.")
        return
    close_far_lo = float(ref["closeness_abs"].quantile(2 / 3))   # batas bawah tercile FAR (1/3 teratas by closeness_abs)
    slope_steep_lo = float(ref["slope_pct"].quantile(2 / 3))     # batas bawah tercile STEEP
    print(f"FAR tercile: closeness_abs >= {close_far_lo:.3f}% | STEEP tercile: slope_pct >= {slope_steep_lo:.3f}%\n")

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
        all_episodes.extend(find_episodes(ticker, hist_df, close_far_lo, slope_steep_lo))

    print(f"Ticker dilewati (histori lokal <{MIN_BARS} bar): {skipped}/{len(tickers)}")
    print(f"Total EPISODE FAR+STEEP ter-resolve: {len(all_episodes)}\n")
    if not all_episodes:
        print("⚠️ Tidak ada episode ter-resolve.")
        return

    df = pd.DataFrame(all_episodes)
    wins = df[df["outcome"] == "WIN"]
    loses = df[df["outcome"] == "LOSE"]
    crossed = df[df["fail_reason"] != "gagal_cross_centerline"]  # WIN + gagal_naik_dari_entry -- SEMUA yang BENAR-BENAR cross centerline
    win_rate = len(wins) / len(df) * 100
    cross_rate = len(crossed) / len(df) * 100

    print("=" * 90)
    print(f"HASIL: n={len(df)} episode | WIN={len(wins)} ({win_rate:.1f}%) | cross_rate={cross_rate:.1f}% ({len(crossed)}/{len(df)})")
    print("=" * 90)
    if not wins.empty:
        print(f"  WIN -- avg max_gain_pct={wins['max_gain_pct'].mean():+.2f}% | median={wins['max_gain_pct'].median():+.2f}%")
    for reason in ["gagal_cross_centerline", "gagal_naik_dari_entry", "timeout"]:
        sub = loses[loses["fail_reason"] == reason]
        if not sub.empty:
            print(f"  LOSE ({reason:<24}) n={len(sub):<5} ({len(sub)/len(df)*100:4.1f}%)")

    print("\n" + "=" * 90)
    print("OUTLOOK: berapa hari dari ENTRY (deteksi FAR+STEEP) sampai centerline BENAR-BENAR cross?")
    print("(hanya episode yang BENAR-BENAR cross -- WIN + gagal_naik_dari_entry, TIDAK termasuk yang gagal cross sama sekali)")
    print("=" * 90)
    if crossed.empty:
        print("⚠️ Tidak ada episode yang cross.")
    else:
        d = crossed["days_to_resolve"]
        print(f"  n={len(d)} | min={d.min()} | P25={d.quantile(0.25):.1f} | MEDIAN={d.median():.1f} | P75={d.quantile(0.75):.1f} | max={d.max()}")
        print("\n  Histogram (bucket hari):")
        crossed = crossed.copy()
        crossed["bucket"] = crossed["days_to_resolve"].apply(_bucket_label)
        for lo, hi in DAY_BUCKETS:
            label = f"{lo}-{hi} hari"
            n_bucket = len(crossed[crossed["bucket"] == label])
            if n_bucket:
                pct = n_bucket / len(crossed) * 100
                bar = "#" * int(pct / 2)
                print(f"    {label:<12} n={n_bucket:<5} ({pct:4.1f}%) {bar}")
        n_tail = len(crossed[crossed["bucket"] == "40+ hari"])
        if n_tail:
            print(f"    {'40+ hari':<12} n={n_tail:<5} ({n_tail/len(crossed)*100:4.1f}%)")

    print("\nBaca ini: MEDIAN di atas itu jawaban langsung 'hari ke berapa paling sering' (bukan MEAN,")
    print("yang bisa ketarik outlier). Kalau distribusinya sempit di sekitar median (P25-P75 rapat),")
    print("H-1/H-2 advance-warning MASUK AKAL dipasang di [median-1, median-2]. Kalau sebarannya lebar")
    print("(P25-P75 jauh), satu angka H-N saja TIDAK cukup presisi -- perlu re-cek posisi tiap hari,")
    print("bukan jadwal tetap.")


if __name__ == "__main__":
    main()
