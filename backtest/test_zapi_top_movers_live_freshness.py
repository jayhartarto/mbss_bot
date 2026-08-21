"""
backtest/test_zapi_top_movers_live_freshness.py — MBSS v2, user request:
sebelum dipakai sebagai filter awal kandidat "sedang breaking" intraday
(supaya tidak perlu scan seluruh universe ~300+ ticker pakai data 1m),
endpoint Zapi finance:idx/top-movers (engine/broker.py's
fetch_zapi_top_movers, REAL FIND user via Zapi SDK snippet) perlu dites DUA
hal:
  1. Bentuk respons JSON yang SEBENARNYA (parsing di fetch_zapi_top_movers
     masih defensif/tebakan, belum dikonfirmasi field-nya persis).
  2. Apakah datanya genuinely update CEPAT selama jam bursa berlangsung
     (freshness), atau delayed/cache-an -- kalau delayed, filter awal ini
     bisa MELEWATKAN saham yang justru sedang breaking SEKARANG.

WAJIB dijalankan SAAT JAM BURSA BERLANGSUNG (09:00-11:30 atau 13:30-15:49
WIB) -- di luar itu, top-movers pasti data basi/EOD, tesnya tidak
bermakna. WAJIB dijalankan di SERVER (ZAPI_API_KEY ada di .env server,
TIDAK ada di sandbox riset Claude).

Biaya: 3 ronde x 2 mover_type (gainer, frequent) = 6 call Zapi, ditambah
jeda 3 menit antar ronde (total ~6 menit runtime) -- dari kuota Zapi
600/bulan, 100/menit yang SUDAH ada (shared dengan stock-summary/
orderbook/running-trades). KECIL, tapi tetap nyata -- jangan dijalankan
berulang-ulang tanpa perlu.

Run di server (SAAT JAM BURSA):
    python backtest/test_zapi_top_movers_live_freshness.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.broker as broker_engine

MOVER_TYPES = ["gainer", "frequent"]
ROUNDS = 3
GAP_SECONDS = 180  # 3 menit antar ronde


def _extract_code(item: dict) -> str:
    for key in ("code", "symbol", "ticker", "stock_code"):
        if key in item:
            return str(item[key])
    return "?"


def main():
    print(f"Test freshness Zapi top-movers — {ROUNDS} ronde, jeda {GAP_SECONDS}s, tipe: {MOVER_TYPES}")
    print("PASTIKAN ini dijalankan SAAT JAM BURSA BERLANGSUNG, bukan di luar jam bursa.\n")

    history = {mt: [] for mt in MOVER_TYPES}

    for round_i in range(1, ROUNDS + 1):
        print("=" * 90)
        print(f"RONDE {round_i} — {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 90)
        for mt in MOVER_TYPES:
            rows = broker_engine.fetch_zapi_top_movers(mover_type=mt, result_count=10)
            if rows is None:
                print(f"  [{mt}] ⚠️ Gagal fetch (None) — cek log error di atas, atau kuota habis.")
                history[mt].append(None)
                continue
            if round_i == 1:
                print(f"  [{mt}] RAW item pertama (buat cek field asli): {rows[0] if rows else '(kosong)'}")
            codes = [_extract_code(r) for r in rows]
            print(f"  [{mt}] {len(rows)} item: {codes}")
            history[mt].append(codes)
        if round_i < ROUNDS:
            print(f"\n(menunggu {GAP_SECONDS} detik sebelum ronde berikutnya...)\n")
            time.sleep(GAP_SECONDS)

    print("\n" + "=" * 90)
    print("RINGKASAN PERUBAHAN ANTAR RONDE (indikator freshness)")
    print("=" * 90)
    for mt in MOVER_TYPES:
        print(f"\n[{mt}]")
        for i in range(1, len(history[mt])):
            prev, curr = history[mt][i - 1], history[mt][i]
            if prev is None or curr is None:
                print(f"  Ronde {i}->{i+1}: data tidak lengkap, dilewati.")
                continue
            same_order = prev == curr
            same_set = set(prev) == set(curr)
            print(f"  Ronde {i}->{i+1}: urutan {'SAMA PERSIS' if same_order else 'BERUBAH'}, "
                  f"anggota {'SAMA' if same_set else 'BERUBAH'} "
                  f"(masuk baru: {set(curr) - set(prev)}, keluar: {set(prev) - set(curr)})")

    print("\nBaca ini: kalau urutan/anggota TIDAK PERNAH berubah sama sekali antar 3 ronde (9 menit)")
    print("padahal market jelas bergerak (cek manual harga beberapa ticker top gainer) -- data ini")
    print("KEMUNGKINAN delayed/cache-an, JANGAN dipakai sebagai filter real-time. Kalau berubah wajar")
    print("(beberapa masuk/keluar, urutan bergeser) -- update-nya cukup hidup, layak dipakai sebagai")
    print("filter awal.")


if __name__ == "__main__":
    main()
