# -*- coding: utf-8 -*-
"""
Sentimen berita umum (positif/negatif) per ticker -- LOG-ONLY, sama disiplin
dengan engine/news_catalyst.py (tagger M&A): dibangun dari Google News RSS
yang sudah ada (core.fetch_company_news), TIDAK punya arsip historis
(~30 hari rolling, search-driven bukan date-indexed) -- jadi TIDAK bisa
dibacktest langsung 2 tahun spt indikator teknikal lain di project ini.

Keputusan user 2026-09-27 (redesign Sentiment score): parsing sentimen dari
berita SEBAGAI booster/reducer -- tapi berbeda dari klaim M&A yang sudah
lolos pilot backtest (n=8/12, ada edge nyata), sentimen umum ini BELUM
punya bukti apa pun. Modul ini CUMA mencatat setiap malam (dedup by
ticker+title, sama pola dgn news_catalyst_log.json), TIDAK dipakai di
formula skor manapun sampai n cukup besar utk divalidasi (kemungkinan
2-3 bulan, sama seperti status broksum_daily_history).

Beda dari news_catalyst.py: itu cuma tag SATU kategori (akuisisi/merger).
Modul ini klasifikasi SETIAP headline jadi positif/negatif/None via kata
kunci Indonesia umum -- lebih luas cakupannya, tapi juga lebih kasar
(keyword matching, bukan NLP/model bahasa) -- riset eksternal 2026-09-27
(lihat memory) sendiri bilang sentimen-NLP dari berita secara umum LEMAH
di literatur akademik ("tidak ada model yang mengalahkan baseline mayoritas
48.4%") -- jangan overclaim keyword matcher sesederhana ini akan lebih baik.
"""
from __future__ import annotations

import datetime
import json
import os

import engine.legacy_core as core

NEWS_SENTIMENT_LOG_FILE = os.path.join(core.PROJECT_ROOT, "news_sentiment_log.json")

# Kata kunci Indonesia umum -- kasar (keyword matching), bukan model bahasa.
# Kalau SALAH SATU exclude keyword match, TOLAK apa pun kondisinya (bantahan/
# rumor/spekulasi bukan konfirmasi arah manapun) -- sama pola dgn news_catalyst.py.
POSITIVE_KEYWORDS_ID = [
    "laba naik", "laba melonjak", "laba tumbuh", "untung besar", "kinerja moncer",
    "ekspansi", "akuisisi", "kontrak baru", "kerja sama strategis", "right issue disambut",
    "buyback disetujui", "rating dinaikkan", "upgrade rating", "prospek cerah",
    "penjualan meningkat", "pendapatan tumbuh", "raih penghargaan", "IPO sukses",
    "saham melesat", "rekor tertinggi", "dividen jumbo", "stock split",
]
NEGATIVE_KEYWORDS_ID = [
    "rugi", "anjlok", "turun tajam", "gugatan", "disuspensi", "suspensi",
    "phk", "restrukturisasi utang", "gagal bayar", "downgrade", "rating diturunkan",
    "delisting", "pailit", "bangkrut", "skandal", "dugaan korupsi", "OJK sanksi",
    "kena sanksi", "penipuan", "investigasi", "kebakaran pabrik", "tutup pabrik",
]
EXCLUDE_KEYWORDS = ["bantah", "rumor", "spekulasi", "dikaitkan", "skenario", "batal", "klarifikasi"]


def classify_headline_sentiment(title: str) -> str | None:
    t = title.lower()
    if any(kw in t for kw in EXCLUDE_KEYWORDS):
        return None
    has_pos = any(kw in t for kw in POSITIVE_KEYWORDS_ID)
    has_neg = any(kw in t for kw in NEGATIVE_KEYWORDS_ID)
    if has_pos and not has_neg:
        return "positive"
    if has_neg and not has_pos:
        return "negative"
    return None  # tidak match, atau match dua-duanya (ambigu) -- jangan dipaksa


def load_news_sentiment_log() -> list:
    if not os.path.exists(NEWS_SENTIMENT_LOG_FILE):
        return []
    try:
        with open(NEWS_SENTIMENT_LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca news sentiment log: {e}")
        return []


def _save_news_sentiment_log(log: list):
    with open(NEWS_SENTIMENT_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=1, default=core._json_default_numpy_safe)


NEWS_SENTIMENT_DAYS_BACK = 3  # sama alasan dgn news_catalyst.py: jalan tiap malam, cukup overlap jaga2 libur/gagal fetch


def scan_news_sentiment(results: list) -> int:
    """
    Dipanggil dari run_nightly_full_scan (soft-fail). Populasi SAMA persis dgn
    news_catalyst.scan_news_catalysts (price>50, value_traded>500jt) supaya
    scope konsisten. Dedup by (ticker, title). Return jumlah entri baru.
    """
    log = load_news_sentiment_log()
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
            headlines = core.fetch_company_news(ticker, company_name, max_items=10, days_back=NEWS_SENTIMENT_DAYS_BACK)
        except Exception as e:
            print(f"⚠️ News sentiment scan gagal utk {ticker}: {e}")
            continue
        for h in headlines:
            title = h["title"]
            if (ticker, title) in existing_keys:
                continue
            sentiment = classify_headline_sentiment(title)
            if not sentiment:
                continue
            log.append({
                "ticker": ticker, "title": title, "published": h.get("published"),
                "sentiment": sentiment, "logged_date": today,
            })
            existing_keys.add((ticker, title))
            added += 1

    if added:
        _save_news_sentiment_log(log)
        print(f"📰 News sentiment log: {added} entri baru malam ini (total {len(log)}).")
    return added


def get_recent_sentiment_tally(ticker: str, days_back: int = 7) -> dict | None:
    """
    {"positive": n, "negative": n, "net": n} dari log_date dlm days_back
    terakhir -- INFORMASIONAL, belum jadi input skor manapun.
    """
    log = load_news_sentiment_log()
    cutoff = (datetime.datetime.now(core.WIB) - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d")
    entries = [e for e in log if e["ticker"] == ticker and e["logged_date"] >= cutoff]
    if not entries:
        return None
    pos = sum(1 for e in entries if e["sentiment"] == "positive")
    neg = sum(1 for e in entries if e["sentiment"] == "negative")
    return {"positive": pos, "negative": neg, "net": pos - neg, "headlines": entries}


def format_check_block(tally: dict | None) -> str:
    if not tally:
        return ""
    icon = "🟢" if tally["net"] > 0 else ("🔴" if tally["net"] < 0 else "⚪")
    return (
        f"\n📰 Sentimen berita (7 hari, BELUM masuk skor): {icon} "
        f"{tally['positive']} positif / {tally['negative']} negatif"
    )
