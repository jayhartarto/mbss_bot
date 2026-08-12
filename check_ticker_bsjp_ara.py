"""
check_ticker_bsjp_ara.py — skrip diagnostik (MBSS v2, user request)

Cek kenapa satu ticker tertentu tidak lolos jadi kandidat BSJP-ARA —
menelusuri 3 titik: apakah ada di cache EOD sama sekali, apakah lolos
pre-filter harga/day_change/volume, dan apakah lolos filter katalis berita.

Cara pakai:
    cd ~/mbss
    source venv/bin/activate
    python3 check_ticker_bsjp_ara.py EKAD
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine.nightly as nightly

if len(sys.argv) < 2:
    print("Format: python3 check_ticker_bsjp_ara.py TICKER")
    print("Contoh: python3 check_ticker_bsjp_ara.py EKAD")
    sys.exit(1)

ticker = sys.argv[1].upper()
print(f"🔍 Diagnostik BSJP-ARA untuk {ticker}\n")

# 1. Cek apakah ticker lolos SEMUA tahap (pre-filter + katalis) -- ada di kandidat final
ara_candidates = nightly.load_bsjp_ara_candidates()
ekad_candidate = next((c for c in ara_candidates if c["ticker"] == ticker), None)
print(f"1. Lolos SEMUA tahap (pre-filter + katalis positif)? {'✅ YA' if ekad_candidate else '❌ TIDAK'}")
if ekad_candidate:
    print(f"   Detail: {ekad_candidate}")

print()

# 2. Cek apakah ticker ada di cache EOD sama sekali, dan lihat angka mentah pre-filter
scored = nightly.load_daily_scan_cache()
eod_data = scored.get(ticker)
if not eod_data:
    print(f"2. ❌ {ticker} TIDAK ADA sama sekali di cache EOD kemarin.")
    print("   Kemungkinan: di luar universe syariah, atau kena blacklist (cek failed_fetch_tracking.json).")
else:
    price = eod_data.get("price")
    day_change = eod_data.get("day_change_pct")
    vol_ratio = eod_data.get("vol_ratio")
    print(f"2. {ticker} ADA di cache EOD kemarin:")
    print(f"   price = {price} (syarat: < 1000)")
    print(f"   day_change_pct = {day_change} (syarat: antara -5.0 dan +5.0)")
    print(f"   vol_ratio = {vol_ratio} (syarat: < 1.5)")

    lolos_prefilter = (
        price is not None and price < 1000
        and day_change is not None and abs(day_change) < 5.0
        and vol_ratio is not None and vol_ratio < 1.5
    )
    print(f"   -> Lolos pre-filter harga/volume? {'✅ YA' if lolos_prefilter else '❌ TIDAK'}")

print()
print("=" * 60)
if ekad_candidate:
    print(f"KESIMPULAN: {ticker} lolos semua filter kita (termasuk katalis berita positif).")
    print("Kalau tetap tidak muncul di /bsjp, kemungkinan soal TIMING —")
    print("cek apakah live-check (momentum>=5% sejak open, VolQ>=3x) terpenuhi")
    print("pas /bsjp dijalankan.")
elif eod_data and not (price and price < 1000 and day_change is not None and abs(day_change) < 5.0 and vol_ratio and vol_ratio < 1.5):
    print(f"KESIMPULAN: {ticker} TERSARING di pre-filter harga/day_change/volume")
    print("(lihat detail angka di atas, mana yang tidak memenuhi syarat).")
elif eod_data:
    print(f"KESIMPULAN: {ticker} lolos pre-filter TAPI tersaring di filter KATALIS BERITA")
    print("(kemungkinan besar: tidak ada berita ditemukan, atau berita yang ada")
    print("diklasifikasikan neutral/bearish oleh Gemini, bukan strong_bullish/bullish).")
else:
    print(f"KESIMPULAN: {ticker} tidak ada di cache EOD sama sekali — periksa universe/blacklist.")
print("=" * 60)
