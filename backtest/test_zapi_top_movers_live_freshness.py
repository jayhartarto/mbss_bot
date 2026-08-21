"""
backtest/test_zapi_top_movers_live_freshness.py — MBSS v2, user request:
sebelum dipakai sebagai filter awal kandidat "sedang breaking" intraday
(supaya tidak perlu scan seluruh universe ~300+ ticker pakai data 1m),
endpoint Zapi finance:idx/top-movers (engine/broker.py's
fetch_zapi_top_movers) perlu dites: apakah polling di cadence yang cocok
dengan cache 1-menitnya (dari dokumentasi resmi Zapi) benar-benar
menangkap saham baru yang mulai breaking, bukan cuma daftar statis.

Bentuk respons & cache 1-menit SUDAH DIKONFIRMASI dari dokumentasi Zapi
langsung (bukan lagi tebakan) -- field Code/Price/Change/Percent/Volume/
Value/Frequency, nesting data.data. Script ini TIDAK LAGI menguji "apakah
datanya live sama sekali" (sudah pasti, dari dokumentasi) -- fokusnya
sekarang: cadence polling ~70-90 detik (sedikit di atas cache 1 menit,
supaya tiap ronde genuinely dapat snapshot BARU, bukan cache yang sama
2x) apakah cukup untuk menangkap perubahan top-mover secara wajar.

WAJIB dijalankan SAAT JAM BURSA BERLANGSUNG (09:00-11:30 atau 13:30-15:49
WIB) -- di luar itu, top-movers pasti data EOD statis, tesnya tidak
bermakna. WAJIB dijalankan di SERVER (ZAPI_API_KEY ada di .env server,
TIDAK ada di sandbox riset Claude).

Biaya: 5 ronde x 2 mover_type (gainer, frequent) = 10 call Zapi, jeda 75
detik antar ronde (total ~6 menit runtime) -- dari kuota Zapi 600/bulan,
100/menit yang SUDAH ada (shared dengan stock-summary/orderbook/
running-trades). KECIL, tapi tetap nyata -- jangan dijalankan berulang-
ulang tanpa perlu.

Run di server (SAAT JAM BURSA):
    python backtest/test_zapi_top_movers_live_freshness.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.broker as broker_engine

MOVER_TYPES = ["gainer", "frequent"]
ROUNDS = 5
GAP_SECONDS = 75  # sedikit di atas cache 1 menit Zapi -- tiap ronde genuinely snapshot baru


def main():
    print(f"Test cadence Zapi top-movers — {ROUNDS} ronde, jeda {GAP_SECONDS}s (>cache 1 menit), tipe: {MOVER_TYPES}")
    print("PASTIKAN ini dijalankan SAAT JAM BURSA BERLANGSUNG, bukan di luar jam bursa.\n")

    history = {mt: [] for mt in MOVER_TYPES}  # list of {code: (Price, Percent, Frequency)} per ronde

    for round_i in range(1, ROUNDS + 1):
        print("=" * 100)
        print(f"RONDE {round_i} — {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 100)
        for mt in MOVER_TYPES:
            rows = broker_engine.fetch_zapi_top_movers(mover_type=mt, result_count=10)
            if rows is None:
                print(f"  [{mt}] ⚠️ Gagal fetch (None) — cek log error di atas, atau kuota habis.")
                history[mt].append(None)
                continue
            snapshot = {}
            for r in rows:
                code = r.get("Code", "?")
                snapshot[code] = (r.get("Price"), r.get("Percent"), r.get("Frequency"))
            print(f"  [{mt}] {len(rows)} item:")
            for code, (price, pct, freq) in snapshot.items():
                print(f"     {code:<6} Price={price} Percent={pct}% Frequency={freq}")
            history[mt].append(snapshot)
        if round_i < ROUNDS:
            print(f"\n(menunggu {GAP_SECONDS} detik sebelum ronde berikutnya...)\n")
            time.sleep(GAP_SECONDS)

    print("\n" + "=" * 100)
    print("RINGKASAN PERUBAHAN ANTAR RONDE")
    print("=" * 100)
    for mt in MOVER_TYPES:
        print(f"\n[{mt}]")
        for i in range(1, len(history[mt])):
            prev, curr = history[mt][i - 1], history[mt][i]
            if prev is None or curr is None:
                print(f"  Ronde {i}->{i+1}: data tidak lengkap, dilewati.")
                continue
            same_set = set(prev.keys()) == set(curr.keys())
            new_in = set(curr.keys()) - set(prev.keys())
            dropped = set(prev.keys()) - set(curr.keys())
            price_changes = []
            for code in set(prev.keys()) & set(curr.keys()):
                p_prev, pct_prev, _ = prev[code]
                p_curr, pct_curr, _ = curr[code]
                if p_prev != p_curr or pct_prev != pct_curr:
                    price_changes.append(f"{code}({p_prev}->{p_curr}, {pct_prev}%->{pct_curr}%)")
            print(f"  Ronde {i}->{i+1}: anggota {'SAMA' if same_set else 'BERUBAH'} "
                  f"(masuk baru: {new_in or '-'}, keluar: {dropped or '-'})")
            print(f"    Perubahan harga pada ticker yang tetap ada: {price_changes or '(tidak ada perubahan)'}")

    print("\nBaca ini: kalau Price/Percent tetap SAMA PERSIS terus-menerus antar ronde (padahal cadence")
    print("sudah di atas cache 1 menit) -- endpoint ini kemungkinan tidak update sesering yang")
    print("didokumentasikan, JANGAN diandalkan sebagai filter real-time. Kalau Price/Percent bergerak")
    print("wajar tiap ronde (2 menit lebih) dan kadang ada ticker baru masuk/keluar -- cadence ~75-90")
    print("detik ini SUDAH cukup untuk filter awal kandidat breaking.")


if __name__ == "__main__":
    main()
