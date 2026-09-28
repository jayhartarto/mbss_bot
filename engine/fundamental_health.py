# -*- coding: utf-8 -*-
"""
Fundamental Health Score -- seberapa "sehat"/solid perusahaan secara
fundamental, TERPISAH dari Value/Momentum/Sentiment (engine/scoring.py) dan
dari sinyal pilar teknikal (BOW/VCP/MACD-confirm/BSJP). Skala 1-10.

Desain disepakati user 2026-09-27 (langkah pertama dari rencana "susun
scoring satu per satu, fundamental dulu"): skor ini SENGAJA tidak dirancang
merespons pergerakan harga jangka pendek -- tujuannya cuma menjawab
"perusahaan ini solid atau tidak", terpisah dari "apakah sahamnya lagi
murah/mahal/bergerak".

5 pilar (referensi Piotroski F-Score/MSCI Quality -- keduanya SENGAJA
mengeluarkan valuasi dari skor "quality", karena perusahaan solid bisa saja
mahal & sebaliknya saham murah bisa value-trap):
    Profitabilitas      30%  (ROE, ROA, Net Profit Margin)
    Kesehatan Keuangan  25%  (Debt/Equity, Current Ratio, kualitas laba
                               OCF vs Net Income)
    Pertumbuhan         20%  (revenue growth YoY, earnings growth YoY)
    Dividen             15%  (yield, payout-ratio sustainability)
    Valuasi             10%  (PE, PB -- inverted, lebih murah = skor lebih
                               tinggi -- SANITY CHECK "jangan kemahalan",
                               BUKAN indikator sehat, makanya bobot kecil)

Setiap field dinilai PERSENTIL LINTAS-SEKTOR (idx_ic_sector_map.csv, 962
emiten/11 sektor, dibangun 2026-09-25) di dalam snapshot bulan berjalan --
BEDA dari Momentum/Sentiment yang persentil-vs-histori-waktu-sendiri (data
fundamental cuma berubah kuartalan, histori-sendiri kurang relevan; ROE bank
juga cuma bermakna dibanding bank lain, bukan dibanding tambang batu bara).
Field kosong -> netral (5.0), TIDAK dihukum -- konvensi rumah project ini.

Refresh BULANAN (bukan tiap /eodscan, per keputusan user): data intinya
(ROE/margin/D-E/growth) cuma berubah saat rilis laporan kuartalan (~4x/thn).
PE/PB/yield (10%+sebagian Dividen) memang harian, tapi bobotnya kecil dan
skor ini memang tidak dirancang mengejar pergerakan jangka pendek. Efek
samping: memotong beban fetch .info per-ticker ke Yahoo yang sebelumnya live
tiap kali dipanggil di scoring.py (lihat skip_live_fundamentals bugfix di
sana) -- di sini cuma sebulan sekali untuk seluruh universe.

RISET SUMBER ALTERNATIF (2026-09-27, sebelum modul ini ditulis): RapidAPI
IDX yang sudah terintegrasi TIDAK punya endpoint fundamental (9 path dicoba,
semua 404). Endpoint resmi IDX (primary/ListedCompany/GetFinancialReport)
ada tapi param yang dicoba selalu 0 hasil, dan sekalipun ketemu itu cuma
metadata laporan (perlu unduh+parse XBRL terpisah, bukan sekali panggil).
sectors.app datanya lengkap tapi API cuma paket berbayar. yfinance .info
tetap dipakai -- cakupan snapshot field diukur 90-100% (sampel 15 ticker
besar) untuk field yang dipakai modul ini, VS cuma ~64% untuk laporan
tahunan multi-tahun (makanya modul ini TIDAK memakai income_stmt/
balance_sheet mentah, cuma field .info yang sudah dihitung Yahoo).
"""
from __future__ import annotations

import datetime
import json
import os
import time

import pandas as pd

import engine.legacy_core as core

FUNDAMENTAL_HEALTH_FILE = os.path.join(core.PROJECT_ROOT, "fundamental_health.json")
SECTOR_MAP_FILE = os.path.join(core.PROJECT_ROOT, "idx_ic_sector_map.csv")

FUNDAMENTAL_HEALTH_FORMULA_VERSION = "1.0"

PILLAR_WEIGHTS = {
    "profitability": 0.30,
    "financial_health": 0.25,
    "growth": 0.20,
    "dividend": 0.15,
    "valuation": 0.10,
}

# yfinance .info field -> (pillar, invert). invert=True berarti nilai LEBIH
# RENDAH lebih baik (dipakai score_from_percentile(pct, invert=True)).
FIELD_SPEC = {
    "returnOnEquity": ("profitability", False),
    "returnOnAssets": ("profitability", False),
    "profitMargins": ("profitability", False),
    "debtToEquity": ("financial_health", True),
    "currentRatio": ("financial_health", False),
    "earnings_quality_ratio": ("financial_health", False),  # OCF / Net Income, dihitung, bukan field .info langsung
    "revenueGrowth": ("growth", False),
    "earningsGrowth": ("growth", False),
    "dividendYield": ("dividend", False),
    "payout_sustainability": ("dividend", False),  # payoutRatio ditransformasi, lihat _payout_sustainability
    "pe_sane": ("valuation", True),
    "pb_sane": ("valuation", True),
}

RAW_INFO_FIELDS = [
    "returnOnEquity", "returnOnAssets", "profitMargins", "debtToEquity", "currentRatio",
    "operatingCashflow", "netIncomeToCommon", "revenueGrowth", "earningsGrowth",
    "dividendYield", "payoutRatio", "trailingPE", "priceToBook", "bookValue", "currentPrice",
    "regularMarketPrice",
]

# Sama persis dengan sanity bound PE/PB di engine/scoring.py (kasus nyata IATA/
# TOBA/ADRO dkk -- field priceToBook yfinance rusak sistemik untuk saham IDX).
# Duplikat sengaja -- modul ini independen (fetch bulanan sendiri), jaga tetap
# sinkron kalau salah satu ambang direvisi.
PB_SANITY_MAX = 100
PE_SANITY_MAX = 500


def _load_sector_map() -> dict:
    if not os.path.exists(SECTOR_MAP_FILE):
        return {}
    df = pd.read_csv(SECTOR_MAP_FILE, usecols=["ticker", "sector"])
    return dict(zip(df["ticker"], df["sector"]))


def _load() -> dict:
    if not os.path.exists(FUNDAMENTAL_HEALTH_FILE):
        return {"fetched_at": None, "scores": {}}
    try:
        with open(FUNDAMENTAL_HEALTH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca fundamental health cache: {e}")
        return {"fetched_at": None, "scores": {}}


def _save(store: dict):
    with open(FUNDAMENTAL_HEALTH_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=1, default=core._json_default_numpy_safe)


def is_stale(store: dict | None = None) -> bool:
    """Stale kalau belum pernah fetch, ATAU fetch terakhir bukan di bulan
    kalender yang sama -- bukan hitungan hari, sesuai keputusan user
    ("diupdate setiap awal bulan")."""
    store = store or _load()
    fetched_at = store.get("fetched_at")
    if not fetched_at:
        return True
    try:
        last = datetime.datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    now = datetime.datetime.now(core.WIB)
    return (last.year, last.month) != (now.year, now.month)


def _payout_sustainability(payout_ratio: float | None) -> float | None:
    """Payout ratio dibalik jadi 'skor sustainability mentah' sebelum
    dipersentilkan: 0-60% payout dianggap paling sehat (masih nyisa untuk
    reinvestasi), di atas 100% (bayar dividen lebih besar dari laba, tidak
    sustainable) diberi nilai mentah PALING RENDAH -- bukan cuma linear naik
    terus, karena payout SANGAT tinggi bukan tanda makin sehat."""
    if payout_ratio is None:
        return None
    p = payout_ratio * 100 if payout_ratio < 1.5 else payout_ratio  # yfinance kadang desimal kadang persen
    if p < 0:
        return None  # payout negatif = laba negatif, sudah tertangkap flag distress terpisah
    if p <= 60:
        return 100 - p  # 0-60 -> 100-40, monoton turun tapi tetap tinggi
    if p <= 100:
        return 60 - (p - 60)  # 60-100 -> 40-20, mulai dihukum
    return max(0.0, 20 - (p - 100) * 0.2)  # >100% -> turun terus, tidak sustainable


def fetch_universe_fundamentals(tickers: list[str]) -> dict:
    """
    Satu .info fetch per ticker (yfinance tidak punya endpoint batch untuk
    fundamental) -- SENGAJA lambat+berjeda (mirip pola cooldown monthly
    whitelist build di load_or_build_whitelist), karena ini cuma jalan
    SEBULAN SEKALI, bukan tiap malam. Return {ticker: {field: value}}.
    """
    out = {}
    total = len(tickers)
    for i, ticker in enumerate(tickers, 1):
        try:
            stock = core.get_yf_ticker(f"{ticker}.JK")
            info = core.yf_fetch_with_retry(lambda: stock.info)
            out[ticker] = {f: info.get(f) for f in RAW_INFO_FIELDS}
        except Exception as e:
            print(f"⚠️ Fundamental health: gagal fetch {ticker}: {e}")
            out[ticker] = {}
        if i % 25 == 0:
            print(f"⏳ Fundamental health fetch: {i}/{total} ticker...")
        time.sleep(1.0)
    return out


def _sane_pe_pb(raw: dict) -> tuple[float | None, float | None]:
    """Reuse ambang sanity yang sama dengan engine/scoring.py (lihat docstring
    modul) -- PB rusak (>100x) dicoba dipulihkan dari price/bookValue dulu
    sebelum dibuang jadi None."""
    pe = core._safe_float(raw.get("trailingPE"), default=None)
    pb = core._safe_float(raw.get("priceToBook"), default=None)
    if pe is not None and pe > PE_SANITY_MAX:
        pe = None
    if pb is not None and pb > PB_SANITY_MAX:
        price = core._safe_float(raw.get("currentPrice"), default=None) or core._safe_float(raw.get("regularMarketPrice"), default=None)
        book_value = core._safe_float(raw.get("bookValue"), default=None)
        pb_recomputed = (price / book_value) if (price and book_value and book_value > 0) else None
        pb = pb_recomputed if (pb_recomputed is not None and 0 < pb_recomputed <= PB_SANITY_MAX) else None
    return pe, pb


def _derived_fields(raw: dict) -> dict:
    """Field turunan yang butuh >1 field mentah -- dipisah dari FIELD_SPEC
    biar transformasinya jelas dan tidak numpuk di satu fungsi besar."""
    ocf = core._safe_float(raw.get("operatingCashflow"), default=None)
    ni = core._safe_float(raw.get("netIncomeToCommon"), default=None)
    earnings_quality_ratio = None
    if ocf is not None and ni is not None and ni > 0:
        earnings_quality_ratio = ocf / ni  # >1 = laba didukung kas riil, <1 = laba "kertas"

    pe, pb = _sane_pe_pb(raw)

    return {
        "returnOnEquity": core._safe_float(raw.get("returnOnEquity"), default=None),
        "returnOnAssets": core._safe_float(raw.get("returnOnAssets"), default=None),
        "profitMargins": core._safe_float(raw.get("profitMargins"), default=None),
        "debtToEquity": core._safe_float(raw.get("debtToEquity"), default=None),
        "currentRatio": core._safe_float(raw.get("currentRatio"), default=None),
        "earnings_quality_ratio": earnings_quality_ratio,
        "revenueGrowth": core._safe_float(raw.get("revenueGrowth"), default=None),
        "earningsGrowth": core._safe_float(raw.get("earningsGrowth"), default=None),
        "dividendYield": core._safe_float(raw.get("dividendYield"), default=None),
        "payout_sustainability": _payout_sustainability(core._safe_float(raw.get("payoutRatio"), default=None)),
        "pe_sane": pe,
        "pb_sane": pb,
    }


def _is_distress(raw: dict, derived: dict) -> bool:
    """Flag terpisah, BUKAN cuma menyeret skor turun -- konsisten dengan
    is_financial_distress_flag di engine/scoring.py: laba/ekuitas negatif
    adalah red flag genuine, beda dari "data tidak ada"."""
    ni = core._safe_float(raw.get("netIncomeToCommon"), default=None)
    book_value = core._safe_float(raw.get("bookValue"), default=None)
    return bool((ni is not None and ni < 0) or (book_value is not None and book_value < 0))


def compute_fundamental_health_scores(raw_universe: dict, sector_map: dict) -> dict:
    """
    raw_universe: {ticker: {info_field: value}} dari fetch_universe_fundamentals.
    Return {ticker: {"pillars": {...}, "final": float, "is_distress": bool,
                      "sector": str|None, "n_fields_available": int}}.
    Persentil dihitung LINTAS-SEKTOR di dalam snapshot ini sendiri (cross-
    sectional), bukan lintas-waktu -- lihat docstring modul.
    """
    derived_by_ticker = {t: _derived_fields(raw) for t, raw in raw_universe.items()}
    distress_by_ticker = {t: _is_distress(raw_universe[t], derived_by_ticker[t]) for t in raw_universe}

    rows = []
    for t, d in derived_by_ticker.items():
        row = {"ticker": t, "sector": sector_map.get(t)}
        row.update(d)
        rows.append(row)
    df = pd.DataFrame(rows).set_index("ticker")

    field_names = list(FIELD_SPEC.keys())
    percentile_df = pd.DataFrame(index=df.index, columns=field_names, dtype=float)
    for field in field_names:
        # Persentil per grup sektor -- ticker tanpa sektor dikenal (belum ada
        # di idx_ic_sector_map.csv) dipersentilkan lintas SELURUH universe
        # sbg fallback, drpd dibuang total.
        for sector_value, group in df.groupby(df["sector"].fillna("__UNKNOWN__")):
            valid = group[field].dropna()
            if len(valid) < 2:
                continue
            pct = valid.rank(pct=True, method="average")
            percentile_df.loc[valid.index, field] = pct

    scores = {}
    for t in df.index:
        pillar_components = {p: [] for p in PILLAR_WEIGHTS}
        n_available = 0
        for field, (pillar, invert) in FIELD_SPEC.items():
            pct = percentile_df.loc[t, field]
            if pd.isna(pct):
                score = 5.0  # missing = netral, BUKAN dihukum
            else:
                score = core.score_from_percentile(float(pct), invert=invert)
                n_available += 1
            pillar_components[pillar].append(score)

        pillar_scores = {p: round(sum(vals) / len(vals), 2) for p, vals in pillar_components.items()}
        final = sum(pillar_scores[p] * w for p, w in PILLAR_WEIGHTS.items())

        scores[t] = {
            "pillars": pillar_scores,
            "final": round(final, 2),
            "is_distress": distress_by_ticker.get(t, False),
            "sector": sector_map.get(t),
            "n_fields_available": n_available,
        }
    return scores


def refresh_if_stale() -> bool:
    store = _load()
    if not is_stale(store):
        print(f"📋 Fundamental health: masih segar bulan ini (fetched {store.get('fetched_at')}), skip.")
        return False

    sector_map = _load_sector_map()
    sharia_universe = core.fetch_online_sharia_list()
    tickers = core.load_or_build_whitelist(list(sharia_universe))
    print(f"🩺 Fundamental health: mulai fetch bulanan, {len(tickers)} ticker...")

    raw_universe = fetch_universe_fundamentals(tickers)
    scores = compute_fundamental_health_scores(raw_universe, sector_map)

    store = {
        "fetched_at": datetime.datetime.now(core.WIB).isoformat(timespec="seconds"),
        "formula_version": FUNDAMENTAL_HEALTH_FORMULA_VERSION,
        "scores": scores,
    }
    _save(store)
    n_ok = sum(1 for s in scores.values() if s["n_fields_available"] > 0)
    print(f"🩺 Fundamental health: selesai, {n_ok}/{len(scores)} ticker punya minimal 1 field fundamental.")
    return True


def get_fundamental_health(ticker: str) -> dict | None:
    store = _load()
    return store.get("scores", {}).get(ticker)


PILLAR_LABEL_ID = {
    "profitability": "Profitabilitas",
    "financial_health": "Kesehatan Keuangan",
    "growth": "Pertumbuhan",
    "dividend": "Dividen",
    "valuation": "Valuasi",
}


def format_check_block(fh: dict | None) -> str:
    """Satu blok ringkas utk /check -- kosong kalau data belum pernah fetch."""
    if not fh:
        return ""
    distress_note = " ⚠️ FLAG DISTRESS (laba/ekuitas negatif)" if fh["is_distress"] else ""
    pillar_lines = " | ".join(f"{PILLAR_LABEL_ID[p]} {v:.1f}" for p, v in fh["pillars"].items())
    sector_note = f" (sektor: {fh['sector']})" if fh.get("sector") else ""
    return (
        f"\n🩺 Fundamental Health: {fh['final']:.1f}/10{distress_note}{sector_note}\n"
        f"   {pillar_lines}"
    )
