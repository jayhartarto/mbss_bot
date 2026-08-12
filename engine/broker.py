"""
engine/broker.py — BrokerEngine (MBSS v2 Sprint 1, Phase 4)

Scope note (please read before extending this file)
-----------------------------------------------------
This module holds the FETCH / COMPUTE / CACHE layer for broker-summary
(foreign flow, broker concentration, accumulation pattern) data — from
three sources:

  - **Index Alpha** (`fetch_broker_summary_raw`, `compute_brokersum_metrics`) —
    the primary, most detailed source (real per-broker Rupiah values), but
    rate-limited hard at 5 requests/day on the free tier.
  - **Zapi** (`fetch_zapi_stock_summary`, `compute_brokersum_metrics_zapi`) —
    an alternate source, no per-ticker broker breakdown, foreign flow is an
    ESTIMATE (volume × close), not a real Rupiah value like Index Alpha.
  - **Screenshot OCR** (`extract_brokersum_from_screenshot`,
    `compute_brokersum_from_screenshot_data`) — user sends a screenshot of
    their own trading app, read via Gemini vision. Free, no API quota.

Deliberately NOT moved here — these APPLY broker data to a stock's score
and stay with the rest of the scoring/ranking logic in legacy_core.py:
  - `apply_brokersum_adjustment()` / `_apply_brokersum_adjustment_original()`
    — folds a brokersum dict into a scoring dict.
  - `compute_brokersum_priority()` — ranks scored candidates using their
    (already-attached) brokersum data.
  - `PENDING_BROKERSUM_CHECKS` / `PENDING_BROKERSUM_TIMEOUT_MINUTES` — this
    is Telegram conversation-flow state (which chat is waiting for a
    screenshot reply), not broker data — stays with the command handlers.

Same circular-import rule as engine/nightly.py and engine/market.py
-------------------------------------------------------------------
`compute_brokersum_metrics()` needs `core.INDEXALPHA_BASE_URL`/
`core.INDEXALPHA_HEADERS`, `compute_brokersum_metrics_zapi()` needs
`core.ZAPI_BASE_URL`/`core.ZAPI_HEADERS` (these stay in legacy_core.py
because `load_or_build_issi_liquid_whitelist()` — NOT part of BrokerEngine —
also needs the Zapi ones), and `extract_brokersum_from_screenshot()` needs
`core._gemini_image_text()`. legacy_core.py calls back into this module
from several command handlers (`/check`, `/gptpick`, `/executiongate`,
`/testbrief`). Both sides use MODULE imports (`import engine.broker as
broker_engine` / `import engine.legacy_core as core`), never `from module
import name` — see engine/nightly.py's docstring for why the named form
breaks depending on import order.
"""
from __future__ import annotations

import datetime
import json
import os
import re

import requests

from engine import legacy_core as core

BROKERSUM_LOOKBACK_DAYS = 7  # trading-day window for the aggregated multi-day view
BROKERSUM_CACHE_FILE = os.path.join(core.PROJECT_ROOT, "brokersum_cache.json")

# ==========================================
# 📋 KLASIFIKASI JENIS BROKER (MBSS v2, user request)
# ⚠️ DISCLAIMER PENTING: data ini dikumpulkan manual dari sumber publik
# (bukan API resmi IDX real-time) — kode broker BISA BERUBAH sewaktu-waktu
# (merger, ganti nama, izin dicabut). JANGAN dianggap 100% akurat/terkini,
# treat sebagai konteks tambahan, bukan fakta mutlak. Update manual kalau
# ketemu yang sudah tidak sesuai.
#
# 4 kategori (BUKAN biner asing/lokal) — ini penting: beberapa broker
# "asing" (kepemilikan) basis nasabahnya JUSTRU SANGAT RITEL (mis. YP/Mirae,
# ZP/Maybank — sering muncul di HAMPIR SEMUA saham karena populer sebagai
# platform ritel, BUKAN karena "smart money institusional" masuk). Kalau
# diklasifikasi biner asing=institusi, itu MENYESATKAN — YP yang sering
# muncul sebagai top seller di kasus-kasus sebelumnya (OPMS, DOSS, IATA)
# bukan bukti institusi asing kabur, itu cuma broker ritel populer yang
# transaksinya besar karena basis usernya luas.
# ==========================================
BROKER_CODE_TYPE = {
    # Bank investasi asing, basis nasabah institusional/wealth (bukan app ritel massal)
    "AK": ("UBS Sekuritas Indonesia", "asing_institusional"),
    "BK": ("J.P. Morgan Sekuritas Indonesia", "asing_institusional"),
    "KZ": ("CLSA Sekuritas Indonesia", "asing_institusional"),
    "RX": ("Macquarie Sekuritas Indonesia", "asing_institusional"),
    "TP": ("OCBC Sekuritas Indonesia", "asing_institusional"),
    "HD": ("KGI Sekuritas Indonesia", "asing_institusional"),
    "DR": ("RHB Sekuritas Indonesia", "asing_institusional"),
    "KK": ("Phillip Sekuritas Indonesia", "asing_institusional"),
    "CP": ("KB Valbury Sekuritas", "asing_institusional"),
    # Broker kepemilikan asing TAPI basis nasabah ritel besar — sering muncul
    # di banyak saham karena populer, BUKAN sinyal institusi otomatis
    "YP": ("Mirae Asset Sekuritas Indonesia", "asing_basis_ritel"),
    "ZP": ("Maybank Sekuritas Indonesia", "asing_basis_ritel"),
    "YU": ("CGS International Sekuritas Indonesia", "asing_basis_ritel"),
    "XA": ("NH Korindo Sekuritas Indonesia", "asing_basis_ritel"),
    # Domestik institusional (BUMN/bank besar, sering transaksi block)
    "CC": ("Mandiri Sekuritas", "domestik_institusional"),
    "DX": ("Bahana Sekuritas", "domestik_institusional"),
    "OD": ("Danareksa Sekuritas", "domestik_institusional"),
    "NI": ("BNI Sekuritas", "domestik_institusional"),
    "GR": ("Panin Sekuritas", "domestik_institusional"),
    # Domestik ritel (aplikasi trading populer, basis nasabah individu)
    "XC": ("Ajaib Sekuritas Asia", "domestik_ritel"),
    "PD": ("Indo Premier Sekuritas", "domestik_ritel"),
    "XL": ("Stockbit Sekuritas Digital", "domestik_ritel"),
    "MG": ("Semesta Indovest Sekuritas", "domestik_ritel"),
    "LG": ("Trimegah Sekuritas Indonesia", "domestik_ritel"),
    "LS": ("Reliance Sekuritas Indonesia", "domestik_ritel"),
    "RS": ("Yulie Sekuritas Indonesia", "domestik_ritel"),
    "MU": ("Minna Padi Investama Sekuritas", "domestik_ritel"),
}

BROKER_TYPE_LABEL_ID = {
    "asing_institusional": "🏦 Asing (institusional)",
    "asing_basis_ritel": "🌐 Asing (basis ritel besar)",
    "domestik_institusional": "🏛️ Domestik (institusional)",
    "domestik_ritel": "🛒 Domestik (ritel)",
    "tidak_diketahui": "❔ Tidak diketahui",
}


def classify_broker_type(code: str) -> dict:
    """
    Kembalikan {"name": ..., "type": ..., "label": ...} untuk satu kode
    broker. Kalau tidak ada di tabel (ada 90+ broker resmi, tabel ini cuma
    yang paling sering muncul), kembalikan "tidak_diketahui" — JANGAN
    menebak, lebih baik jujur tidak tahu daripada salah klasifikasi di
    konteks finansial.
    """
    entry = BROKER_CODE_TYPE.get(code)
    if not entry:
        return {"name": None, "type": "tidak_diketahui", "label": BROKER_TYPE_LABEL_ID["tidak_diketahui"]}
    name, btype = entry
    return {"name": name, "type": btype, "label": BROKER_TYPE_LABEL_ID[btype]}
BROKERSUM_HISTORY_FILE = os.path.join(core.PROJECT_ROOT, "brokersum_history.json")
BROKERSUM_HISTORY_MAX_ENTRIES_PER_TICKER = 60  # ~3 bulan pemakaian rutin, cukup untuk analisis tren tanpa file membengkak


def fetch_broker_summary_raw(ticker: str, from_date: str, to_date: str, investor: str = "all"):
    """
    Raw fetch from Index Alpha's broker-summary endpoint. Returns the list of
    per-broker records, or None on failure. investor: 'all', 'f' (foreign), 'd'
    (domestic), 'or'.

    Paces every call with a fixed delay AFTER it (not just relying on the 150/month
    budget) — the free tier also has a separate 10 requests/minute cap that was
    missed initially, causing every call in a multi-ticker /myportfolio brokersum
    run to fail with a rate-limit rejection (success:False, error:None) once more
    than ~10 requests fired in quick succession.
    """
    try:
        resp = requests.get(
            f"{core.INDEXALPHA_BASE_URL}/stocks/broker-summary",
            params={"ticker": ticker, "from": from_date, "to": to_date, "investor": investor},
            headers=core.INDEXALPHA_HEADERS,
            timeout=20,
        )
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ Index Alpha error for {ticker} ({investor}): {data.get('error')}")
            return None
        return data.get("data", [])
    except Exception as e:
        print(f"⚠️ Index Alpha fetch failed for {ticker} ({investor}): {e}")
        return None
    finally:
        # ~8.5 req/min, safely under the confirmed 10/min free-tier cap, applied
        # regardless of success/failure so pacing holds even after an error.
        core.time.sleep(7)


def get_last_published_trading_day() -> str:
    """
    Returns YYYY-MM-DD of the most recent trading day whose broker summary data
    has ACTUALLY been published (Index Alpha updates once/day at 19:00 WIB).
    Used as the cache key instead of raw calendar date — so running brokersum
    Sunday night and again Monday morning (before Monday's own data publishes at
    19:00 WIB) correctly hits the same cache entry (both really do show Friday's
    data) instead of wasting a real API call on data that hasn't changed.

    Simplification: accounts for weekends only, not specific IDX public holidays —
    a holiday just causes one extra harmless cache miss (one wasted-but-not-wrong
    fetch), not stale or incorrect data being served.
    """
    now = datetime.datetime.now(core.WIB)
    reference_date = now.date() if now.hour >= 19 else now.date() - datetime.timedelta(days=1)
    while reference_date.weekday() >= 5:  # 5=Saturday, 6=Sunday
        reference_date -= datetime.timedelta(days=1)
    return reference_date.strftime("%Y-%m-%d")


def _load_brokersum_cache() -> dict:
    if not os.path.exists(BROKERSUM_CACHE_FILE):
        return {}
    try:
        with open(BROKERSUM_CACHE_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Failed to read brokersum cache: {e}")
        return {}


def _save_brokersum_cache(cache: dict):
    try:
        with open(BROKERSUM_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"⚠️ Failed to save brokersum cache: {e}")


def get_cached_brokersum(ticker: str):
    """
    Read-only cache lookup — used by /check to pick up ALREADY-FETCHED same-
    trading-day data for free, without spending any Index Alpha quota. Returns
    None if there's no cache entry for the current trading-day window (never
    fetches on its own).
    """
    cache = _load_brokersum_cache()
    entry = cache.get(ticker)
    if not entry:
        return None
    current_trading_day = get_last_published_trading_day()
    if entry.get("date") != current_trading_day:
        return None  # data has genuinely moved on (new trading day published), don't reuse
    return entry.get("data")


def extract_brokersum_from_screenshot(image_bytes: bytes, mime_type: str, expected_ticker: str) -> dict:
    """
    Uses Gemini's vision capability to read a Broker Sum screenshot from the
    user's own trading app — free, doesn't touch Index Alpha's quota at all.
    Deliberately conservative: if the image doesn't clearly show real Broker Sum
    data matching the expected ticker, returns success=False rather than
    guessing — a misread number here would silently feed into real score
    adjustment (apply_brokersum_adjustment), so this must fail loudly, not
    quietly produce a plausible-looking wrong number.

    NOTE: not live-tested against the real Gemini vision API from this
    environment (no network access to verify) — built from the documented
    google-genai SDK pattern. Verify on first real use.
    """
    extraction_prompt = f"""
You are extracting data from a screenshot of an Indonesian stock trading app's
"Broker Sum" (broker summary) screen for ticker {expected_ticker}.

Return ONLY valid JSON, no markdown, no commentary, in this exact shape:
{{
  "success": true or false,
  "ticker_visible": "the ticker shown in the image, or null",
  "reason_if_failed": "brief reason if success=false, else null",
  "date_range_text": "the date range shown in the image if visible (e.g. '27 Jul 2026 - 01 Agu 2026'), else null",
  "toggle_visible": "All" or "Domestic" or "Foreign" or null,
  "foreign_buy_value_idr": number or null,
  "foreign_sell_value_idr": number or null,
  "foreign_net_value_idr": number or null,
  "top_brokers": [
    {{"code": "XX", "net_idr": number, "avg_price": number or null, "volume_lot": number or null}},
    ...
  ] or []
}}

REQUIRED for success=true (this is the only thing that actually matters — the
AGGREGATE totals, usually under a "Total Value" or "Foreign" section header):
- foreign_buy_value_idr and foreign_sell_value_idr (or foreign_net_value_idr
  directly if that's what's shown). These are the ONLY fields required for a
  successful extraction.

- "toggle_visible": Indonesian trading apps show three tab/toggle buttons near
  the top of the Broker Sum screen, usually labeled "All", "Domestic", and
  "Foreign" — exactly ONE is highlighted/active (different color/background
  from the other two). Read which one is actively selected and report it
  exactly as "All", "Domestic", or "Foreign". This matters a lot: it tells us
  whether the buy/sell/net numbers below represent ALL brokers combined, or
  ONLY domestic, or ONLY foreign-flagged brokers — get this wrong and every
  number's meaning is wrong. If you cannot clearly tell which toggle is
  active, return null — do NOT guess or default to "All".

The buy/sell/net aggregate values you extract represent whatever toggle_visible
says (could be ALL-broker combined, domestic-only, or foreign-only) — for
backward compatibility with existing bot code, still return these aggregate
values in the legacy JSON keys named foreign_buy_value_idr, foreign_sell_value_idr,
and foreign_net_value_idr regardless of which toggle was actually active
(the field names are historical, not a claim about which toggle was used —
toggle_visible is the actual source of truth for that).

OPTIONAL, NEVER a reason to fail the whole extraction:
- "top_brokers" (per-broker monetary net breakdown, avg_price, volume_lot) is
  a NICE-TO-HAVE bonus, not a requirement. Many broker summary screens only
  show VOLUME (lot count) per broker, not a monetary value per broker — that
  is completely normal and expected. If you cannot determine a reliable
  Rupiah net_idr value per individual broker, simply return "top_brokers": []
  (empty list) and still set success=true, AS LONG AS the required aggregate
  totals above are readable. Do NOT fail the entire extraction just because
  top_brokers can't be populated — that field existing or not has no bearing
  on success.
- "date_range_text": read whatever date range is displayed in the image
  (often near a calendar icon). This is PURELY informational — used to tell
  the person which window this data actually covers (could be 1 day, 3 days,
  7 days, or a full month — do NOT assume any fixed window, just report what
  you see). If not visible, return null, never guess.

Broker breakdown is important for bandarmology:
- Read visible top BUY broker codes, their buy volume/lot, and average buy price.
- Read visible top SELL broker codes, their sell volume/lot, and average sell price.
- If monetary value per broker is not shown, estimate broker value as:
  volume_lot x 100 x average_price.
- For top_brokers, use positive net_idr for buyers and negative net_idr for sellers.
- avg_price: the "Avg" price column shown for that specific broker row (NOT the
  stock's current price) — this is the volume-weighted average price that
  broker transacted at, used later as a potential support/resistance reference.
  Return null if genuinely not visible/readable, never guess a number.
- volume_lot: the lot volume for that specific broker row, null if not visible.
- Do NOT fail just because some broker-level details are incomplete, as long as
  aggregate Buy/Sell/Net is readable.
- Do NOT classify a broker as "bandar" only from its code. Just extract the data.

NUMBER FORMAT — Indonesian trading apps commonly show values with suffixes.
These are FOUR DISTINCT magnitudes, do not confuse them:
- "K" = thousand = x1,000 (e.g. "1.35 K" = 1,350)
- "M" = million = x1,000,000 (e.g. "93.10 M" = 93,100,000)
- "B" = billion = x1,000,000,000 (e.g. "51.65 B" = 51,650,000,000)
- "T" = trillion = x1,000,000,000,000 (e.g. "2.49 T" = 2,490,000,000,000)
B and T are NOT the same magnitude — T is 1,000x larger than B. Always convert
to the FULL raw number in the JSON output using the correct multiplier above.

success=false ONLY if: the image isn't clearly a Broker Sum screen, the ticker
shown doesn't match "{expected_ticker}", OR the REQUIRED aggregate buy/sell/net
values above are genuinely illegible/not visible. Do not guess a number you
can't actually read — leave that specific field null and explain in
reason_if_failed. But do not fail success on top_brokers alone.
"""
    try:
        raw_text = core._gemini_image_text(image_bytes, mime_type, extraction_prompt).strip()
        # Strip markdown code fences if the model added them despite instructions
        raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.MULTILINE).strip()
        result = json.loads(raw_text)
        return result
    except json.JSONDecodeError as e:
        print(f"⚠️ Brokersum screenshot extraction returned non-JSON: {e}")
        return {"success": False, "reason_if_failed": "Gagal memproses respons AI — coba lagi atau kirim screenshot yang lebih jelas."}
    except Exception as e:
        print(f"⚠️ Brokersum screenshot extraction failed: {e}")
        return {"success": False, "reason_if_failed": f"Error teknis: {str(e)[:100]}"}


def compute_brokersum_from_screenshot_data(extracted: dict) -> dict:
    """
    Converts a successfully-extracted screenshot into the SAME shape
    compute_brokersum_metrics() produces, so it can feed into the identical
    apply_brokersum_adjustment() / decide_action() pipeline as Index Alpha data
    — no separate scoring logic needed for screenshot-sourced data.

    Returns None (NOT a fake 0% result) if the core buy/sell/net values are all
    genuinely missing — treating "couldn't read the number" the same as "the
    number is exactly zero" was a real bug (confirmed on a real RALS screenshot:
    silently produced 0% net flow when the actual data showed +Rp51.65M). A
    missing number must fail loudly here, not quietly become a wrong number
    that then gets used to adjust the real score.
    """
    foreign_buy_raw = extracted.get("foreign_buy_value_idr")
    foreign_sell_raw = extracted.get("foreign_sell_value_idr")
    net_foreign_raw = extracted.get("foreign_net_value_idr")

    # Fail loudly if there's genuinely nothing to work with — don't let "unknown"
    # silently become "zero", which is a completely different, misleading claim.
    if net_foreign_raw is None and foreign_buy_raw is None and foreign_sell_raw is None:
        return None

    foreign_buy = foreign_buy_raw or 0
    foreign_sell = foreign_sell_raw or 0
    net_foreign = net_foreign_raw if net_foreign_raw is not None else (foreign_buy - foreign_sell)
    foreign_gross = foreign_buy + foreign_sell

    net_foreign_flow_pct = (net_foreign / foreign_gross * 100) if foreign_gross > 0 else 0

    top_brokers = extracted.get("top_brokers") or []
    top_3_net_buy = sum(b.get("net_idr", 0) for b in sorted(top_brokers, key=lambda x: x.get("net_idr", 0), reverse=True)[:3] if b.get("net_idr", 0) > 0)
    broker_concentration_pct = (top_3_net_buy / foreign_gross * 100) if foreign_gross > 0 else 0

    top_net_buyers = sorted([b for b in top_brokers if b.get("net_idr", 0) > 0], key=lambda x: x["net_idr"], reverse=True)[:3]
    top_net_sellers = sorted([b for b in top_brokers if b.get("net_idr", 0) < 0], key=lambda x: x["net_idr"])[:3]
    for b in top_net_buyers + top_net_sellers:
        b["broker"] = classify_broker_type(b.get("code", ""))

    # BUGFIX (ditemukan dari pertanyaan user soal toggle Foreign vs All):
    # sebelumnya HARDCODE "ALL_BROKERS_3D" tanpa syarat, padahal user
    # konsisten kirim screenshot dengan toggle "Foreign" aktif — flow_scope
    # yang tersimpan selama ini salah label untuk SEMUA pemakaian nyata user.
    # Sekarang pakai toggle_visible yang benar-benar dibaca dari gambar.
    toggle = extracted.get("toggle_visible")
    flow_scope_map = {"Foreign": "FOREIGN_ONLY", "Domestic": "DOMESTIC_ONLY", "All": "ALL_BROKERS"}
    flow_scope = flow_scope_map.get(toggle, "ALL_BROKERS_UNKNOWN_TOGGLE")  # jujur kalau tidak yakin, bukan asumsi diam-diam

    return {
        "ticker": extracted.get("ticker_visible"),
        "source": "screenshot",  # distinguishes from Index Alpha's "api" source for transparency
        "flow_scope": flow_scope,
        "toggle_visible": toggle,
        "net_all_flow_idr": int(net_foreign),
        # Diasumsikan 7 hari (kalender) sesuai komitmen user untuk selalu mengambil
        # screenshot dengan window 5 hari bursa / 7 hari kalender — sama dengan
        # BROKERSUM_LOOKBACK_DAYS yang dipakai Index Alpha, supaya data dari kedua
        # sumber tetap sebanding/konsisten saat ditampilkan atau dibandingkan tren.
        "lookback_days": BROKERSUM_LOOKBACK_DAYS,
        "net_foreign_flow_idr": int(net_foreign),
        "net_foreign_flow_pct": round(net_foreign_flow_pct, 1),
        "broker_concentration_pct": round(broker_concentration_pct, 1),
        "proxy_agreement": "not_available",  # computed separately once merged with cmf/obv context
        "top_net_buyers": top_net_buyers,
        "top_net_sellers": top_net_sellers,
    }


def _load_brokersum_history() -> dict:
    if not os.path.exists(BROKERSUM_HISTORY_FILE):
        return {}
    try:
        with open(BROKERSUM_HISTORY_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca brokersum history: {e}")
        return {}


def _save_brokersum_history(history: dict):
    try:
        with open(BROKERSUM_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan brokersum history: {e}")


def append_brokersum_history(ticker: str, brokersum: dict):
    """
    Menyimpan HASIL BARU sebagai entri baru (bukan menimpa yang lama) — supaya
    riwayat menumpuk seiring waktu lewat pemakaian rutin, tanpa panggilan API
    tambahan sama sekali. Dipangkas ke BROKERSUM_HISTORY_MAX_ENTRIES_PER_TICKER
    entri terbaru per ticker agar file tidak membengkak tanpa batas.
    """
    history = _load_brokersum_history()
    entries = history.setdefault(ticker, [])
    entries.append({
        "date": get_last_published_trading_day(),
        "net_foreign_flow_idr": brokersum.get("net_foreign_flow_idr"),
        "net_foreign_flow_pct": brokersum.get("net_foreign_flow_pct"),
        "broker_concentration_pct": brokersum.get("broker_concentration_pct"),
    })
    # Dedup: kalau entri untuk tanggal yang sama sudah ada (dipanggil >1x di hari
    # bursa yang sama), timpa yang lama, jangan duplikat.
    seen_dates = {}
    for e in entries:
        seen_dates[e["date"]] = e
    deduped = sorted(seen_dates.values(), key=lambda e: e["date"])
    history[ticker] = deduped[-BROKERSUM_HISTORY_MAX_ENTRIES_PER_TICKER:]
    _save_brokersum_history(history)


def compute_brokersum_trend(ticker: str, current_net_flow_idr) -> dict:
    """
    Membandingkan hasil HARI INI vs entri log SEBELUMNYA untuk ticker yang sama —
    memberi sinyal ARAH TREN (menguat/melemah/stabil), BUKAN angka "net hari ini"
    yang presisi. Ini penting dijelaskan jujur: karena setiap panggilan adalah
    jumlah bergulir 7 hari, selisih dua panggilan berurutan = (kontribusi hari
    baru) - (kontribusi hari yang "jatuh" dari ujung window lama) — bukan murni
    net satu hari itu sendiri. Hanya diberi label "hari bursa sebelumnya" (bukan
    tren jangka panjang) kalau entri pembanding benar dari 1 hari bursa sebelum
    hari ini — kalau gap-nya lebih jauh, interpretasi "pergeseran 1 hari" tidak
    berlaku lagi, jadi ditandai sebagai perbandingan histori biasa saja.
    """
    history = _load_brokersum_history()
    entries = history.get(ticker, [])
    if not entries:
        return {"trend": "tidak_ada_histori", "delta_idr": None, "is_single_day_shift": False}

    today = get_last_published_trading_day()
    prior_entries = [e for e in entries if e["date"] < today]
    if not prior_entries:
        return {"trend": "tidak_ada_histori", "delta_idr": None, "is_single_day_shift": False}

    most_recent = prior_entries[-1]
    delta_idr = current_net_flow_idr - (most_recent.get("net_foreign_flow_idr") or 0)

    # Cek apakah entri pembanding ini benar dari 1 hari bursa sebelumnya (bukan
    # gap beberapa hari/minggu) — untuk menentukan apakah framing "pergeseran
    # window 1 hari" masih valid secara wajar.
    today_dt = datetime.datetime.strptime(today, "%Y-%m-%d").date()
    prior_dt = datetime.datetime.strptime(most_recent["date"], "%Y-%m-%d").date()
    gap_calendar_days = (today_dt - prior_dt).days
    is_single_day_shift = gap_calendar_days <= 3  # toleransi akhir pekan

    total_gross_approx = abs(current_net_flow_idr) + abs(most_recent.get("net_foreign_flow_idr") or 0)
    if total_gross_approx == 0:
        trend = "STABIL"
    else:
        delta_ratio = delta_idr / max(total_gross_approx, 1)
        if delta_ratio > 0.15:
            trend = "MENGUAT"
        elif delta_ratio < -0.15:
            trend = "MELEMAH"
        else:
            trend = "STABIL"

    return {
        "trend": trend, "delta_idr": int(delta_idr),
        "is_single_day_shift": is_single_day_shift,
        "compared_to_date": most_recent["date"],
    }


def fetch_zapi_stock_summary(ticker: str, date_str: str = None) -> dict:
    """
    Sumber brokersum ALTERNATIF via Zapi (finance:idx/stock-summary) — TERPISAH
    dari Index Alpha, tidak menggantikan. Belum diverifikasi apakah update
    genuinely intraday atau harian (verifikasi live-update sedang berjalan) —
    diperlakukan sebagai data EOD sampai terbukti sebaliknya, jadi HANYA dipakai
    di /check dan /myportfolio (horizon swing), TIDAK di /screendaytrade — sama
    alasan strukturalnya dengan kenapa Index Alpha juga tidak dipakai di situ.

    CATATAN PENTING: ForeignBuy/ForeignSell dari Zapi adalah VOLUME LEMBAR,
    BUKAN nilai Rupiah (beda dari Index Alpha yang langsung kasih value) —
    net_foreign_flow_idr di sini adalah ESTIMASI (volume x harga penutupan),
    bukan angka pasti seperti dari Index Alpha.
    """
    try:
        params = {"length": "1", "start": "0", "code": ticker}
        if date_str:
            params["date"] = date_str
        resp = requests.get(
            f"{core.ZAPI_BASE_URL}/finance:idx/stock-summary",
            params=params, headers=core.ZAPI_HEADERS, timeout=20,
        )
        data = resp.json()
        rows = data.get("data", {}).get("data", [])
        if not rows:
            print(f"⚠️ Zapi stock-summary: tidak ada data untuk {ticker}")
            return None
        return rows[0]
    except Exception as e:
        print(f"⚠️ Zapi stock-summary fetch gagal untuk {ticker}: {e}")
        return None


def compute_brokersum_metrics_zapi(ticker: str, cmf=None, obv_divergence=None) -> dict:
    """
    Versi Zapi dari compute_brokersum_metrics() — struktur output SAMA persis
    supaya kompatibel dengan apply_brokersum_adjustment()/decide_action() yang
    sama. Tidak ada breakdown per-broker (top_net_buyers/sellers) dari sumber
    ini, karena stock-summary hanya kasih total foreign buy/sell per ticker,
    bukan per broker seperti Index Alpha.

    SUDAH DIKONFIRMASI (jangan dieksplorasi ulang): endpoint finance:idx/
    broker-summary Zapi TIDAK BISA difilter per-saham dalam kondisi apa pun —
    diuji langsung: code=BBCA vs code=TLKM menghasilkan recordsTotal dan angka
    Volume per broker yang IDENTIK PERSIS (data gabungan market-wide, bukan
    per-saham). Dicoba juga 4 nama parameter alternatif (stockCode, symbol,
    ticker, stock) — semua diterima tanpa error tapi diam-diam diabaikan,
    hasilnya tetap sama. Kesimpulan: endpoint ini secara desain untuk
    "broker paling aktif se-pasar hari ini", bukan "siapa yang menggerakkan
    saham X" — bukan keterbatasan yang bisa disiasati dengan parameter yang
    tepat. Breakdown broker per-saham HANYA tersedia dari Index Alpha.
    """
    row = fetch_zapi_stock_summary(ticker)
    if row is None:
        return None

    foreign_buy_shares = row.get("ForeignBuy", 0) or 0
    foreign_sell_shares = row.get("ForeignSell", 0) or 0
    close_price = row.get("Close", 0) or 0
    foreign_gross_shares = foreign_buy_shares + foreign_sell_shares

    net_foreign_shares = foreign_buy_shares - foreign_sell_shares
    net_foreign_flow_pct = (net_foreign_shares / foreign_gross_shares * 100) if foreign_gross_shares > 0 else 0
    net_foreign_flow_idr_estimate = int(net_foreign_shares * close_price)

    # Broker concentration tidak tersedia dari sumber ini (bukan breakdown per
    # broker) — default 0 supaya apply_brokersum_adjustment() otomatis skip
    # penyesuaian skor (threshold minimal 10%), tetap tampil sebagai info saja.
    broker_concentration_pct = 0

    proxy_agreement = "not_available"
    if cmf is not None and isinstance(cmf, (int, float)):
        proxy_bullish = cmf > 0
        real_bullish = net_foreign_flow_pct > 0
        if obv_divergence == "bearish_divergence" and net_foreign_flow_pct > 5:
            proxy_agreement = "CONTRADICTION: proxy showed bearish OBV divergence but real broker flow (Zapi) is net positive"
        elif obv_divergence == "bullish_divergence" and net_foreign_flow_pct < -5:
            proxy_agreement = "CONTRADICTION: proxy showed bullish OBV divergence but real broker flow (Zapi) is net negative"
        elif proxy_bullish == real_bullish:
            proxy_agreement = "confirms_proxy"
        else:
            proxy_agreement = "diverges_from_proxy"

    return {
        "ticker": ticker,
        "source": "zapi",
        "lookback_days": 1,  # stock-summary hanya kasih snapshot 1 hari, bukan agregat multi-hari seperti Index Alpha
        "net_foreign_flow_idr": net_foreign_flow_idr_estimate,
        "net_foreign_flow_idr_is_estimate": True,  # transparansi: ini estimasi (volume x close), bukan value asli
        "net_foreign_flow_pct": round(net_foreign_flow_pct, 1),
        "broker_concentration_pct": broker_concentration_pct,
        "proxy_agreement": proxy_agreement,
        "top_net_buyers": [],
        "top_net_sellers": [],
        # Field tambahan yang khas dari Zapi, tidak ada di Index Alpha:
        "bid": row.get("Bid"), "bid_volume": row.get("BidVolume"),
        "offer": row.get("Offer"), "offer_volume": row.get("OfferVolume"),
        "non_regular_volume": row.get("NonRegularVolume"),
        "non_regular_value": row.get("NonRegularValue"),
        "listed_shares": row.get("ListedShares"),
        "tradeble_shares": row.get("TradebleShares"),
    }


def get_cached_or_fetch_brokersum(ticker: str, cmf=None, obv_divergence=None, lookback_days=BROKERSUM_LOOKBACK_DAYS):
    """
    Used by /myportfolio brokersum — checks the cache first, keyed by the last
    PUBLISHED trading day (not raw calendar date), so running this Sunday night
    and again Monday morning both correctly hit the same cache entry (both are
    really showing Friday's data until Monday's own data publishes at 19:00 WIB) —
    avoiding a wasted API call for data that hasn't actually changed. Fetches
    fresh only on a genuine cache miss, and writes the result back for /check to
    pick up later for free.
    """
    cached = get_cached_brokersum(ticker)
    if cached is not None:
        print(f"📋 Using cached brokersum for {ticker} (same trading day as last fetch)")
        return cached

    result = compute_brokersum_metrics(ticker, cmf, obv_divergence, lookback_days)
    if result is not None:
        result["trend"] = compute_brokersum_trend(ticker, result["net_foreign_flow_idr"])
        cache = _load_brokersum_cache()
        cache[ticker] = {"date": get_last_published_trading_day(), "data": result}
        _save_brokersum_cache(cache)
        append_brokersum_history(ticker, result)  # hanya saat fetch BARU, bukan cache hit
    return result


def compute_brokersum_metrics(ticker: str, cmf=None, obv_divergence=None, lookback_days=BROKERSUM_LOOKBACK_DAYS):
    """
    Computes real, derived broker-flow metrics for one ticker over a multi-day
    window — 1 API call (investor=f only). Reduced from the original 2-call
    (foreign+all) design after discovering Index Alpha's free tier has a HARD
    5 requests/day cap (confirmed via a real 403 rejection), not just the 150/
    month pool we originally designed around. At 1 call/ticker this supports
    5 tickers/day instead of 2 — real coverage is more valuable than the
    domestic-comparison depth we're giving up (flow_agreement, foreign
    participation %), given how scarce daily calls are.

    Every field here is a genuine computed ratio from real data, never a
    fabricated score/confidence. Returns None if data wasn't available.
    """
    to_date = datetime.datetime.now(core.WIB).date()
    from_date = to_date - datetime.timedelta(days=lookback_days + 4)  # pad for weekends
    from_str, to_str = from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d")

    foreign_brokers = fetch_broker_summary_raw(ticker, from_str, to_str, investor="f")
    if not foreign_brokers:
        return None

    foreign_buy = sum(b.get("buy_value", 0) for b in foreign_brokers)
    foreign_sell = sum(b.get("sell_value", 0) for b in foreign_brokers)
    net_foreign = foreign_buy - foreign_sell
    foreign_gross = foreign_buy + foreign_sell

    net_foreign_flow_pct = (net_foreign / foreign_gross * 100) if foreign_gross > 0 else 0

    # Concentration within broker flow specifically (can't compare vs total market
    # without the second call anymore) — top-3 net foreign buyers vs total foreign
    # gross activity. Still catches "a few players moving decisively" vs "diffuse,
    # low-conviction foreign activity" even at this narrower scope.
    net_by_broker = sorted(
        [(b["code"], b.get("buy_value", 0) - b.get("sell_value", 0)) for b in foreign_brokers],
        key=lambda x: x[1], reverse=True,
    )
    top_3_net_buy = sum(v for _, v in net_by_broker[:3] if v > 0)
    broker_concentration_pct = (top_3_net_buy / foreign_gross * 100) if foreign_gross > 0 else 0

    # Transaction pattern: uses buy_freq/sell_freq (transaction COUNT), previously
    # fetched but discarded. Compares each top broker's average transaction size
    # against the OVERALL average for this stock (adaptive, not a fixed threshold —
    # same principle as the adaptive RSI/volume scoring elsewhere) to distinguish
    # genuine gradual accumulation (many small orders — the real bandarmology
    # pattern) from a one-off block trade (few large orders — weaker signal for
    # continuation, often just institutional rebalancing).
    total_foreign_freq = sum(b.get("buy_freq", 0) + b.get("sell_freq", 0) for b in foreign_brokers)
    overall_avg_size = (foreign_gross / total_foreign_freq) if total_foreign_freq > 0 else 0

    broker_lookup = {b["code"]: b for b in foreign_brokers}

    def classify_pattern(code):
        b = broker_lookup.get(code, {})
        freq = b.get("buy_freq", 0) + b.get("sell_freq", 0)
        value = b.get("buy_value", 0) + b.get("sell_value", 0)
        if freq == 0 or overall_avg_size == 0:
            return "tidak_diketahui"
        avg_size = value / freq
        ratio = avg_size / overall_avg_size
        if ratio < 0.7:
            return "akumulasi_bertahap"  # many small orders relative to the norm
        elif ratio > 1.5:
            return "transaksi_block"  # few large orders — likely one-off/rebalancing
        else:
            return "normal"

    top_net_buyers = [
        {"code": c, "net_idr": int(v), "pattern": classify_pattern(c), **{"broker": classify_broker_type(c)}}
        for c, v in net_by_broker[:3] if v > 0
    ]
    top_net_sellers = [
        {"code": c, "net_idr": int(v), "pattern": classify_pattern(c), **{"broker": classify_broker_type(c)}}
        for c, v in net_by_broker[-3:] if v < 0
    ]

    # Compare against our own existing technical proxies — never silently pick one
    # side when they disagree, same discipline as the news-dismissal rule elsewhere.
    proxy_agreement = "not_available"
    if cmf is not None and isinstance(cmf, (int, float)):
        proxy_bullish = cmf > 0
        real_bullish = net_foreign_flow_pct > 0
        if obv_divergence == "bearish_divergence" and net_foreign_flow_pct > 5:
            proxy_agreement = "CONTRADICTION: proxy showed bearish OBV divergence but real broker flow is net positive"
        elif obv_divergence == "bullish_divergence" and net_foreign_flow_pct < -5:
            proxy_agreement = "CONTRADICTION: proxy showed bullish OBV divergence but real broker flow is net negative"
        elif proxy_bullish == real_bullish:
            proxy_agreement = "confirms_proxy"
        else:
            proxy_agreement = "diverges_from_proxy"

    return {
        "ticker": ticker,
        "lookback_days": lookback_days,
        "net_foreign_flow_idr": int(net_foreign),
        "net_foreign_flow_pct": round(net_foreign_flow_pct, 1),
        "broker_concentration_pct": round(broker_concentration_pct, 1),
        "proxy_agreement": proxy_agreement,
        "top_net_buyers": top_net_buyers,
        "top_net_sellers": top_net_sellers,
    }


def assess_smart_accumulation(scoring: dict, brokersum: dict) -> dict:
    """
    MBSS v2 (user request — kerangka bandarmology 4-langkah dari user).
    Gabungkan sinyal teknikal yang SUDAH ADA (day_range_pct_10d, vol_ratio,
    close_pos_day) dengan data broker (konsentrasi, jenis broker, avg price)
    jadi SATU checklist transparan — BUKAN satu verdict "ya/tidak akumulasi"
    yang menyesatkan. Setiap kriteria ditampilkan apa adanya, boleh sebagian
    terpenuhi sebagian tidak — user yang menyimpulkan, bot cuma menyusun
    fakta secara rapi.

    PENTING: ini TIDAK bisa dipakai untuk scan universe (lihat disclaimer di
    percakapan) — cuma untuk 1 ticker yang SUDAH punya data broker (dari
    Index Alpha cache atau screenshot upload).
    """
    checklist = []

    # Kriteria 1: sideways + broker net beli terkonsentrasi (akumulasi diam-diam)
    day_range = scoring.get("day_range_pct_10d")
    net_flow_pct = brokersum.get("net_foreign_flow_pct")
    concentration = brokersum.get("broker_concentration_pct")
    if day_range is not None and net_flow_pct is not None:
        is_sideways = day_range < 15
        is_net_buy_concentrated = net_flow_pct > 5 and (concentration or 0) >= 10
        checklist.append({
            "kriteria": "Sideways + broker net-beli terkonsentrasi",
            "terpenuhi": is_sideways and is_net_buy_concentrated,
            "detail": f"Range 10hr {day_range:.1f}% ({'sempit' if is_sideways else 'lebar'}), "
                      f"net broker {net_flow_pct:+.1f}%, konsentrasi {concentration or 0:.1f}%",
        })

    # Kriteria 2: volume spike + closing dekat high hari itu
    vol_ratio = scoring.get("vol_ratio")
    close_pos = scoring.get("close_pos_day")
    if vol_ratio is not None and close_pos is not None:
        is_spike = vol_ratio >= 2.0
        is_strong_close = close_pos >= 0.7
        checklist.append({
            "kriteria": "Lonjakan volume + closing dekat high hari itu",
            "terpenuhi": is_spike and is_strong_close,
            "detail": f"Vol {vol_ratio:.2f}x normal, posisi closing {close_pos*100:.0f}% dari rentang hari itu "
                      f"({'dekat high' if close_pos >= 0.7 else 'dekat low' if close_pos <= 0.3 else 'tengah'})",
        })

    # Kriteria 3: jenis broker pembeli vs penjual (⚠️ konteks, bukan kepastian —
    # lihat disclaimer BROKER_CODE_TYPE soal YP/ZP yang basis ritel besar)
    buyers = brokersum.get("top_net_buyers") or []
    sellers = brokersum.get("top_net_sellers") or []
    buyer_types = [b.get("broker", {}).get("type") for b in buyers if b.get("broker")]
    seller_types = [s.get("broker", {}).get("type") for s in sellers if s.get("broker")]
    institutional_buyers = sum(1 for t in buyer_types if t in ("asing_institusional", "domestik_institusional"))
    retail_sellers = sum(1 for t in seller_types if t in ("asing_basis_ritel", "domestik_ritel"))
    if buyers or sellers:
        checklist.append({
            "kriteria": "Pembeli institusional vs penjual ritel (⚠️ konteks, bukan kepastian)",
            "terpenuhi": institutional_buyers >= 1 and retail_sellers >= 1,
            "detail": (
                f"Buyer: {', '.join(b.get('broker', {}).get('label', '❔') + ' ' + b.get('code', '') for b in buyers) or '-'}. "
                f"Seller: {', '.join(s.get('broker', {}).get('label', '❔') + ' ' + s.get('code', '') for s in sellers) or '-'}."
            ),
        })

    # Kriteria 4: avg price buyer institusional sebagai level support acuan
    # (cuma tersedia dari SCREENSHOT — Index Alpha tidak kasih avg_price per broker)
    price = scoring.get("price")
    buyers_with_avg = [b for b in buyers if b.get("avg_price") and price]
    if buyers_with_avg:
        lowest_avg = min(b["avg_price"] for b in buyers_with_avg)
        below_current = lowest_avg < price
        checklist.append({
            "kriteria": "Avg price buyer sebagai acuan support",
            "terpenuhi": below_current,
            "detail": f"Avg beli terendah dari buyer teridentifikasi: {lowest_avg:,.0f} "
                      f"({'di bawah' if below_current else 'di atas'} harga sekarang {price:,.0f})",
        })

    met_count = sum(1 for c in checklist if c["terpenuhi"])
    return {
        "checklist": checklist,
        "criteria_met": met_count,
        "criteria_checkable": len(checklist),
        "summary_label": (
            f"{met_count}/{len(checklist)} kriteria terpenuhi — "
            + ("cukup banyak sinyal selaras, tapi tetap bukan kepastian" if met_count >= len(checklist) * 0.75
               else "sinyal campuran, baca detail per kriteria" if met_count >= 1
               else "sinyal minim/tidak mendukung akumulasi institusional")
        ) if checklist else "Data tidak cukup untuk checklist ini",
    }


BROKER_ENTRY_CEILING_MIN_SHARE_PCT = 15  # broker harus mewakili >=15% dari total net-buy teridentifikasi untuk dianggap "besar"


def get_broker_entry_ceiling(brokersum: dict, min_share_pct: float = BROKER_ENTRY_CEILING_MIN_SHARE_PCT) -> dict | None:
    """
    MBSS v2 (user request): avg price broker BESAR (top-3, porsi net-buy
    >=min_share_pct dari total net-buy teridentifikasi) sebagai referensi
    "jangan kejar di atas ini" — stopper psikologis, BUKAN target/level
    teknikal resmi. Kalau harga sedang lari cepat, ini kasih patokan: broker
    besar sendiri rata-rata cuma berani beli sampai harga segini.

    Ambil avg_price TERTINGGI di antara buyer yang lolos ambang (bukan
    terendah) — logikanya "bahkan buyer besar paling agresif pun cuma
    berani sampai sini", jadi framing-nya benar sebagai CEILING/stopper,
    bukan support/entry-bawah (itu peran level teknikal yang sudah ada).

    Return None kalau tidak ada buyer yang lolos ambang ATAU tidak ada
    avg_price yang terbaca (paling sering karena sumbernya Index Alpha,
    yang TIDAK punya avg_price per broker sama sekali — cuma screenshot
    yang bisa mengisi field ini).
    """
    buyers = brokersum.get("top_net_buyers") or []
    buyers_with_avg = [b for b in buyers if b.get("avg_price") and b.get("net_idr", 0) > 0]
    if not buyers_with_avg:
        return None

    total_buy = sum(b.get("net_idr", 0) for b in buyers if b.get("net_idr", 0) > 0)
    if total_buy <= 0:
        return None

    qualifying = [b for b in buyers_with_avg if (b["net_idr"] / total_buy * 100) >= min_share_pct]
    if not qualifying:
        return None

    biggest_avg = max(qualifying, key=lambda b: b["avg_price"])
    return {
        "avg_price": biggest_avg["avg_price"],
        "code": biggest_avg.get("code"),
        "share_pct": round(biggest_avg["net_idr"] / total_buy * 100, 1),
        "broker_label": biggest_avg.get("broker", {}).get("label", "❔"),
    }


def fetch_broker_summary_batch_raw(tickers: list, from_date: str, to_date: str, investor: str = "all") -> dict | None:
    """
    MBSS v2 (user request — solusi resmi buat kebutuhan broker-wide, ditemukan
    lewat dokumentasi API Index Alpha sendiri): POST /stocks/broker-summary/batch
    — sampai 50 ticker sekaligus dalam 1 panggilan, didesain khusus buat
    screener/watchlist. Return dict {ticker: [baris broker, ...]}, atau None
    kalau gagal.

    ⚠️ BELUM DIPASTIKAN: apakah 1 panggilan batch ini dihitung "1x" dari
    budget bulanan ~150, atau dihitung sejumlah ticker di dalamnya (mis. 50
    ticker = 50x). SENGAJA belum dipakai buat cakupan luas (615 ticker)
    sebelum ini diverifikasi — coba dulu dengan batch KECIL (5-10 ticker),
    cek dashboard/usage Index Alpha Anda setelahnya, baru diperluas.
    """
    if len(tickers) > 50:
        print(f"⚠️ Batch broker-summary maksimal 50 ticker per panggilan, dapat {len(tickers)} — dipotong ke 50 pertama.")
        tickers = tickers[:50]
    try:
        resp = requests.post(
            f"{core.INDEXALPHA_BASE_URL}/stocks/broker-summary/batch",
            json={"tickers": tickers, "from": from_date, "to": to_date, "investor": investor},
            headers=core.INDEXALPHA_HEADERS,
            timeout=30,
        )
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ Index Alpha batch error ({len(tickers)} ticker): {data.get('error')}")
            return None
        return data.get("data", {})
    except Exception as e:
        print(f"⚠️ Index Alpha batch fetch gagal ({len(tickers)} ticker): {e}")
        return None
    finally:
        core.time.sleep(7)  # pacing sama seperti single-ticker, jaga-jaga rate limit per-menit tetap berlaku


def find_broker_activity_across_tickers(broker_code: str, tickers: list, from_date: str, to_date: str) -> list:
    """
    MBSS v2 (user request — "broker AK dalam 10 hari, akumulasi/distribusi
    saham apa?"): reverse-lookup pakai batch endpoint — cek SATU broker
    across BEBERAPA ticker sekaligus, urutkan dari net-buy terbesar ke
    net-sell terbesar. Cakupannya SEBATAS ticker yang di-passing (bukan
    seluruh market otomatis) — pemanggil yang menentukan daftar ticker mana
    yang mau dicek (mis. watchlist/portofolio user, atau subset ISSI-liquid).
    """
    result = fetch_broker_summary_batch_raw(tickers, from_date, to_date, investor="all")
    if not result:
        return []

    activity = []
    for ticker, rows in result.items():
        broker_row = next((r for r in rows if r.get("broker_code") == broker_code or r.get("broker") == broker_code), None)
        if broker_row:
            activity.append({
                "ticker": ticker,
                "net_value_idr": broker_row.get("net_value_idr") or broker_row.get("net_value"),
                "buy_volume": broker_row.get("buy_volume"), "sell_volume": broker_row.get("sell_volume"),
                "avg_buy_price": broker_row.get("avg_buy_price"), "avg_sell_price": broker_row.get("avg_sell_price"),
            })
    activity.sort(key=lambda a: a.get("net_value_idr") or 0, reverse=True)
    return activity
