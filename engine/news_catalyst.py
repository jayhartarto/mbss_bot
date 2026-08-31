# -*- coding: utf-8 -*-
"""
MBSS v2 (user request 2026-08-31, "explore ke news... apakah ada
pengaruh?"): tagging LOG-ONLY (belum jadi sinyal produksi) untuk berita
akuisisi/M&A per ticker, dibangun via engine/legacy_core.py's
fetch_company_news (Google News RSS, gratis, sudah ada, dipakai /check).

Kenapa CUMA akuisisi (dari beberapa kategori yg dites sesi ini): pilot
backtest 2 pass independen (n=8 & n=12, 30 hari, universe liquid) SAMA-SAMA
nunjukkan edge kuat & konsisten arah (D+3 median +4-9%, 58-75% positif,
JAUH di atas baseline random-date +0-0.4%/31-37%) -- SATU-SATUNYA kategori
yg begitu. earnings_growth (+campuran, tidak konsisten), dividen (tidak ada
edge, kemungkinan besar krn sudah terjadwal/anticipated bukan surprise),
buyback (n terlalu kecil + 1 false positive tertangkap -- "BANK" match
Deutsche Bank asing krn query ticker mentah, bukan nama perusahaan),
rating_upgrade/ekspansi_pabrik/index_inclusion (0-1 kejadian dlm window,
terlalu jarang utk diukur), "banyak dibeli asing" (dicoba tapi campur-aduk:
foreign flow + insider buying + parent-buys-subsidiary + individual investor
terkenal, semua pakai frasa "borong saham" yg sama -- DAN sudah ada versi
lebih presisi di produksi: whitelist_accumulation_net_pct/Bias Bandar dari
data broker riil, bukan proxy teks berita).

KETERBATASAN STRUKTURAL (sama seperti retensi data 1m sesi ini, tapi lebih
parah): Google News RSS TIDAK punya arsip historis -- fetch_company_news
HANYA bisa lihat berita yg MASIH muncul di index RSS SEKARANG (efektif
~30 hari terakhir, dan bahkan itu tidak lengkap, search-driven bukan
date-indexed). TIDAK ADA cara mem-backtest properly dgn data 6 bulan-2
tahun spt fitur lain sesi ini. Modul ini SENGAJA cuma nge-LOG kejadian
BARU tiap malam (bukan re-proses ulang 30 hari tiap kali) -- histori
GENUINE baru terbentuk seiring waktu berjalan, bukan instan. TIDAK dipakai
sbg sinyal /go atau command manapun sampai n cukup besar utk divalidasi
proper (chronological discovery/validation split, sama disiplin fitur lain).
"""
from __future__ import annotations

import datetime
import email.utils
import json
import os

import engine.legacy_core as core

NEWS_CATALYST_LOG_FILE = os.path.join(core.PROJECT_ROOT, "news_catalyst_log.json")

# MBSS v2: HANYA akuisisi/M&A -- lihat docstring modul di atas utk alasan
# kategori lain di-drop. "merger" ditambah (ditemukan di sample eksplorasi,
# "Skenario Merger MTEL-TBIG" -- tapi "skenario" masuk EXCLUDE, jadi hanya
# merger yg SUDAH terjadi/dikonfirmasi yg match).
ACQUISITION_KEYWORDS = ["akuisisi", "caplok", "diakuisisi", "mengakuisisi", "merger"]

# Denial/rumor/noise -- kalau match SALAH SATU ini, TOLAK meskipun ada
# keyword akuisisi (mis. "JARR bantah kabar akuisisi..." BUKAN konfirmasi).
EXCLUDE_KEYWORDS = ["bantah", "rumor", "spekulasi", "dikaitkan", "skenario", "batal"]

NEWS_CATALYST_DAYS_BACK = 3  # jendela kecil -- job ini jalan tiap malam, cukup overlap utk jaga2 libur/gagal fetch


def classify_headline(title: str) -> str | None:
    t = title.lower()
    if any(kw in t for kw in EXCLUDE_KEYWORDS):
        return None
    if any(kw in t for kw in ACQUISITION_KEYWORDS):
        return "akuisisi"
    return None


def load_news_catalyst_log() -> list:
    if not os.path.exists(NEWS_CATALYST_LOG_FILE):
        return []
    try:
        with open(NEWS_CATALYST_LOG_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca news catalyst log: {e}")
        return []


def _save_news_catalyst_log(log: list):
    with open(NEWS_CATALYST_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, default=core._json_default_numpy_safe)


def scan_news_catalysts(results: list) -> int:
    """
    Dipanggil dari run_nightly_full_scan (soft-fail, TIDAK menggagalkan
    /eodscan kalau error) -- SATU RSS fetch per ticker LIQUID (value_traded
    & price floor SAMA dgn _daytrade_wr_tp1, commands/scan.py, supaya
    scope-nya konsisten dgn populasi DAY TRADE) di `results` (sudah
    di-scan malam ini, company_name sudah tersedia dari compute_factor_
    scoring). Dedup by (ticker, title) -- headline yg SAMA persis TIDAK
    di-log ulang tiap malam selama masih muncul di RSS. Return jumlah
    entri BARU yg ditambahkan.
    """
    log = load_news_catalyst_log()
    existing_keys = {(e["ticker"], e["title"]) for e in log}
    today = datetime.datetime.now(core.WIB).strftime("%Y-%m-%d")

    added = 0
    for r in results:
        price = r.get("price")
        value_traded = r.get("value_traded")
        if not price or price <= 50 or not value_traded or value_traded <= 500_000_000:
            continue
        ticker = r.get("ticker")
        company_name = r.get("company_name") or ticker
        try:
            headlines = core.fetch_company_news(ticker, company_name, max_items=10, days_back=NEWS_CATALYST_DAYS_BACK)
        except Exception as e:
            print(f"⚠️ News catalyst scan gagal utk {ticker}: {e}")
            continue
        for h in headlines:
            title = h["title"]
            if (ticker, title) in existing_keys:
                continue
            category = classify_headline(title)
            if not category:
                continue
            log.append({
                "ticker": ticker, "title": title, "published": h.get("published"),
                "category": category, "logged_date": today,
            })
            existing_keys.add((ticker, title))
            added += 1

    if added:
        _save_news_catalyst_log(log)
        print(f"📰 News catalyst log: {added} entri akuisisi/M&A baru malam ini (total {len(log)}).")
    return added
