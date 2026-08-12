"""
test_broksum_250.py — skrip mandiri (MBSS v2, user request)

Jalankan fetch BROKSUM 250 (ticker berskor tertinggi dari cache /eodscan
TERAKHIR yang tersimpan, 5 panggilan batch x 50 ticker) SEKARANG JUGA —
tidak perlu nunggu /eodscan malam ini jalan lagi dengan kode terbaru.
Sekaligus tes reverse-lookup: kode broker apa saja yang aktif di ticker
mana.

⚠️ Ini akan memakai kuota API Index Alpha (sampai 5x panggilan batch =
seluruh kuota harian Anda). Jalankan cuma kalau memang siap pakai kuota
hari ini untuk ini.

Cara pakai:
    cd ~/mbss
    source venv/bin/activate   # kalau di VM
    python3 test_broksum_250.py AK
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine.nightly as nightly_engine
import engine.broker as broker_engine

if len(sys.argv) < 2:
    print("Format: python3 test_broksum_250.py KODE_BROKER")
    print("Contoh: python3 test_broksum_250.py AK")
    sys.exit(1)

broker_code = sys.argv[1].upper()

print("📋 Membaca cache /eodscan terakhir untuk daftar ticker berskor tertinggi...")
scored = nightly_engine.load_daily_scan_cache()
if not scored:
    print("❌ Cache /eodscan kosong/basi — jalankan /eodscan dulu (lewat bot atau `python3 bot.py --eodscan`) sebelum skrip ini.")
    sys.exit(1)

results = list(scored.values())
print(f"✅ {len(results)} ticker ditemukan di cache.\n")

print("💹 Fetch BROKSUM 250 (250 ticker berskor tertinggi, 5 panggilan batch)...")
print("   ⚠️ Ini akan memakai kuota API — pastikan memang siap pakai hari ini.\n")

data = nightly_engine.build_broksum_250(results)

if not data:
    print("\n❌ Tidak ada data yang berhasil di-fetch — cek pesan error di atas.")
    sys.exit(1)

print(f"\n✅ Berhasil fetch broker-summary untuk {len(data)} ticker.")

# Simpan ke cache juga, supaya /broksum di bot langsung bisa pakai hasil ini
nightly_engine.save_broksum_250(data)
print("💾 Tersimpan ke cache/broksum_250.pkl — /broksum di bot sekarang bisa langsung dipakai.\n")

# Reverse-lookup: broker_code yang diminta, aktif di ticker mana saja
print(f"🔍 Aktivitas broker {broker_code}:\n")
activity = []
for ticker, rows in data.items():
    broker_row = next((r for r in rows if r.get("broker_code") == broker_code or r.get("broker") == broker_code), None)
    if broker_row:
        activity.append({
            "ticker": ticker,
            "net_value_idr": broker_row.get("net_value_idr") or broker_row.get("net_value"),
            "avg_buy_price": broker_row.get("avg_buy_price"),
            "avg_sell_price": broker_row.get("avg_sell_price"),
        })

if not activity:
    print(f"📋 {broker_code} tidak terdeteksi aktif di {len(data)} ticker yang di-cek.")
else:
    activity.sort(key=lambda a: a.get("net_value_idr") or 0, reverse=True)
    for a in activity:
        arah = "🟢 NET BELI" if (a["net_value_idr"] or 0) > 0 else "🔴 NET JUAL"
        print(f"{a['ticker']} — {arah} Rp{abs(a['net_value_idr'] or 0):,.0f} "
              f"(avg beli {a.get('avg_buy_price', '-')} | avg jual {a.get('avg_sell_price', '-')})")

print(f"\n{'=' * 60}")
print("Cek dashboard/usage Index Alpha Anda sekarang — seharusnya kuota")
print("harian berkurang 5 (5 panggilan batch), bulanan berkurang 5 juga.")
print("=" * 60)
