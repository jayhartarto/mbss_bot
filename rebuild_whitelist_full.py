#!/usr/bin/env python3
"""
rebuild_whitelist_full.py

MBSS v2 (user request 2026-08-24 — live case: GRIA/SAFE, Sharia-compliant &
price-eligible, tidak pernah muncul di ALERT karena tidak ada di
ticker_whitelist.json sama sekali). Investigasi: ticker_whitelist.json cuma
mencakup 380/576 konstituen ISSI saat ini (eligible+excluded gabungan) --
227 ticker (termasuk GRIA/SAFE/PJHB) TIDAK PERNAH diproses sama sekali,
padahal file mengklaim generated_month bulan ini. `load_or_build_whitelist`'s
cache-hit branch (dipanggil tiap /eodscan lewat run_nightly_full_scan) HANYA
mem-filter list yg SUDAH ADA di cache, tidak pernah menambah ticker baru yg
belum pernah dicek -- jadi begitu file jadi partial (sebab aslinya tidak
diketahui, mungkin build lama sblm universe diperluas), dia TIDAK PERNAH
self-correct kecuali force_rebuild=True dipanggil eksplisit, atau bulan
kalender berganti (dan bahkan itu pun cuma valid kalau rebuild bulanan
otomatis benar2 jalan penuh sampai selesai).

Jalankan SEKALI (python rebuild_whitelist_full.py) -- akan memproses SELURUH
~576 ticker ISSI terkunci via yfinance/SQLite (get_ohlcv_smart, DB-first),
pace sama seperti load_or_build_whitelist aslinya (jeda 20 detik tiap 25
ticker). Estimasi durasi: beberapa menit sampai belasan menit tergantung
berapa banyak ticker yg BELUM ada di SQLite lokal (representative harus di
server, karena DB lokal server jauh lebih lengkap dari dev clone).
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
from engine import legacy_core as core  # noqa: E402


def main():
    print("📡 Fetch daftar ISSI terkunci...")
    sharia_universe = core.fetch_online_sharia_list()
    full_universe_list = list(sharia_universe)
    print(f"✅ {len(full_universe_list)} ticker ISSI -- mulai force rebuild whitelist penuh "
          f"(ini akan cek SEMUA, bukan cuma yg sudah ada di cache lama)...")

    eligible = core.load_or_build_whitelist(full_universe_list, force_rebuild=True)

    print(f"\n✅ Selesai: {len(eligible)} ticker eligible dari {len(full_universe_list)} ISSI terkunci.")
    print(f"File tersimpan: {core.WHITELIST_CACHE_FILE}")


if __name__ == "__main__":
    main()
