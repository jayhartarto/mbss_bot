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
  - **RapidAPI IDX Market Intelligence** (`fetch_rapidapi_broker_activity`,
    `fetch_rapidapi_sentiment`, `fetch_rapidapi_bandar_accumulation`, near the
    bottom of this file) — interim real-broker-data source while Index
    Alpha's monthly quota is exhausted. Basic free plan: 500 requests/month,
    1 req/sec — see `RAPIDAPI_IDX_MONTHLY_BUDGET` and
    `_rapidapi_idx_quota_check_and_increment` for the enforced monthly cap
    (separate budget from Index Alpha's own). `fetch_rapidapi_broker_activity`
    is broker-scoped, not ticker-scoped — one call returns that broker's
    activity across every ticker on the exchange for a date range, which is
    what makes sweeping `SMART_MONEY_BROKER_WHITELIST` (13 calls) cheaper
    than a per-ticker approach; `rapidapi_broker_activity_to_broksum_rows`
    reshapes its response into this file's existing per-ticker row shape so
    it can merge straight into the `broksum_250` pipeline in
    `engine/nightly.py` with no changes to any downstream consumer.

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
        # MBSS v2 (RapidAPI integration, Top Buyer Persistence): broker codes
        # from top_net_buyers, added so compute_top_buyer_persistence() can
        # detect the same broker(s) recurring as a top buyer across days —
        # older entries simply won't have this key, callers use .get(..., []).
        "top_buyer_codes": [b["code"] for b in brokersum.get("top_net_buyers", []) if b.get("code")],
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
    if not core._zapi_quota_check_and_increment("stock-summary"):
        return None
    try:
        params = {"length": "1", "start": "0", "code": ticker}
        if date_str:
            params["date"] = date_str
        resp = requests.get(
            f"{core.ZAPI_BASE_URL}/finance:idx/stock-summary",
            params=params, headers=core.ZAPI_HEADERS, timeout=20,
        )
        if resp.status_code == 429:
            print(f"⚠️ Zapi rate-limited (429) untuk stock-summary/{ticker} — backing off.")
            return None
        if resp.status_code != 200:
            print(f"⚠️ Zapi stock-summary {ticker}: HTTP {resp.status_code}")
            return None
        data = resp.json()
        rows = data.get("data", {}).get("data", [])
        if not rows:
            print(f"⚠️ Zapi stock-summary: tidak ada data untuk {ticker}")
            return None
        return rows[0]
    except Exception as e:
        print(f"⚠️ Zapi stock-summary fetch gagal untuk {ticker}: {e}")
        return None


def fetch_zapi_orderbook(ticker: str) -> dict | None:
    """
    MBSS v2 (user request, real find — endpoint order book ASLI dari Zapi,
    /v1/finance:pluang/orderbook): langsung menjawab keterbatasan yang
    berkali-kali disebut sepanjang sesi ini ("bot TIDAK punya akses
    order-book bid/ask asli") — ini BENERAN order book, bukan proxy OHLCV.

    Dipakai HANYA on-demand (flag "zapi" di /check), TIDAK PERNAH di-bulk-
    scan — kuota Zapi 600/bulan, 100/menit, SHARED semua endpoint termasuk
    stock-summary yang sudah lama dipakai. Return None kalau gagal/kuota
    habis — TIDAK pernah menggagalkan /check, cuma bagian tampilan itu yang
    kosong.
    """
    if not core._zapi_quota_check_and_increment("orderbook"):
        return None
    try:
        resp = requests.get(
            f"{core.ZAPI_BASE_URL}/finance:pluang/orderbook",
            params={"code": ticker}, headers=core.ZAPI_HEADERS, timeout=20,
        )
        if resp.status_code == 429:
            print(f"⚠️ Zapi rate-limited (429) untuk orderbook/{ticker} — backing off.")
            return None
        if resp.status_code != 200:
            print(f"⚠️ Zapi orderbook {ticker}: HTTP {resp.status_code}")
            return None
        data = resp.json()
        if not data.get("bestBid") and not data.get("bestAsk"):
            return None
        return data
    except Exception as e:
        print(f"⚠️ Zapi orderbook fetch gagal untuk {ticker}: {e}")
        return None


def fetch_zapi_running_trades(ticker: str, action: str = None, min_lot: int = None) -> dict | None:
    """
    MBSS v2 (user request, real find — endpoint running trade ASLI dari
    Zapi, /v1/finance:pluang/running-trades): tape/time-and-sales riil per
    cetakan transaksi, dengan filter aggressor side (BUY/SELL) dan ambang
    lot (big-print filter) — ini yang dipakai buat deteksi "cetakan beli
    besar berulang" (real case user: order buy tebal yang menjaga level
    harga tertentu).

    SATU HALAMAN SAJA (tidak menyusuri cursor/nextCursor) — sengaja, biar
    biaya kuota tetap 1 call per pemanggilan, bukan berpotensi banyak call
    kalau history panjang. `count`/`fetched` di respons Zapi bisa jauh
    lebih besar dari yang benar-benar dikembalikan; itu OK, kita cuma
    perlu cetakan PALING BARU untuk baca tekanan beli SAAT INI, bukan
    seluruh histori hari itu.
    """
    if not core._zapi_quota_check_and_increment("running-trades"):
        return None
    try:
        params = {"code": ticker}
        if action:
            params["action"] = action
        if min_lot:
            params["minLot"] = min_lot
        resp = requests.get(
            f"{core.ZAPI_BASE_URL}/finance:pluang/running-trades",
            params=params, headers=core.ZAPI_HEADERS, timeout=20,
        )
        if resp.status_code == 429:
            print(f"⚠️ Zapi rate-limited (429) untuk running-trades/{ticker} — backing off.")
            return None
        if resp.status_code != 200:
            print(f"⚠️ Zapi running-trades {ticker}: HTTP {resp.status_code}")
            return None
        return resp.json()
    except Exception as e:
        print(f"⚠️ Zapi running-trades fetch gagal untuk {ticker}: {e}")
        return None


def compute_orderflow_snapshot_zapi(ticker: str, big_print_min_lot: int = 300) -> dict | None:
    """
    MBSS v2 (user request — "enrich entry buy call" dari order book +
    running trade Zapi): satu ringkasan siap-tampil, MENGGABUNGKAN 2 call
    (orderbook + running-trades BUY big-print) — TOTAL 2 kuota Zapi per
    pemanggilan (di atas stock-summary yang sudah ada kalau flag "zapi"
    dipakai bersamaan, jadi bisa sampai 3 call total per /check TICKER zapi).

    Murni INFORMASIONAL — TIDAK menggating apa pun (sama disiplin dengan
    tight_trailing_support/fast_candidate), belum ada bukti forward bahwa
    order-book imbalance atau big-print count ini genuinely prediktif di
    IDX. `big_print_min_lot=300` placeholder (kira-kira 30rb lembar,
    signifikan tapi belum divalidasi) — sesuaikan berdasarkan observasi.
    """
    orderbook = fetch_zapi_orderbook(ticker)
    trades = fetch_zapi_running_trades(ticker, action="BUY", min_lot=big_print_min_lot)

    result = {"available": bool(orderbook or trades)}
    if orderbook:
        result["best_bid"] = orderbook.get("bestBid")
        result["best_ask"] = orderbook.get("bestAsk")
        result["bid_percent"] = orderbook.get("bidPercent")
        result["ask_percent"] = orderbook.get("askPercent")
        result["bid_lots"] = sum(b.get("lots", 0) for b in (orderbook.get("bids") or []))
        result["ask_lots"] = sum(a.get("lots", 0) for a in (orderbook.get("asks") or []))

    if trades:
        items = trades.get("items") or []
        result["big_buy_print_count"] = len(items)
        result["big_buy_print_total_lots"] = sum(i.get("lots", 0) for i in items)
        if items:
            result["big_buy_print_latest_price"] = items[0].get("price")
            result["big_buy_print_min_lot_threshold"] = big_print_min_lot

    return result


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


def find_broker_activity_across_tickers(broker_code: str, tickers_data: dict) -> list:
    """
    MBSS v2 (user request, REVISI setelah field API asli dikonfirmasi —
    field tebakan awal SALAH TOTAL: bukan "broker_code"/"net_value_idr",
    field asli dari Index Alpha adalah "code", "buy_value", "sell_value",
    "buy_volume", "buy_avg", dst — net value TIDAK ada langsung, harus
    dihitung sendiri buy_value - sell_value):

    Reverse-lookup SATU broker across BEBERAPA ticker (dari cache
    broksum_250, bukan fetch baru) — return CUMA yang NET BUY (tujuan
    eksplisit: follow smart money AKUMULASI, bukan tampilkan jual-beli
    campur), diurutkan dari net buy value terbesar, dengan avg_buy_price
    dan buy_volume (lot) eksplisit — sesuai kebutuhan "confidence entry
    dekat avg price broker smart money".
    """
    activity = []
    for ticker, rows in tickers_data.items():
        row = next((r for r in rows if r.get("code") == broker_code), None)
        if not row:
            continue
        net_value = (row.get("buy_value") or 0) - (row.get("sell_value") or 0)
        if net_value <= 0:
            continue  # HANYA net buy — sesuai tujuan follow smart money akumulasi
        # BUGFIX (ditemukan lewat kroscek manual user terhadap aplikasi
        # trading asli — DSSA/AK: bot tampilkan 5,1 juta lot, aplikasi asli
        # cuma 1,46 juta lot (net); IATA/AK: bot 8,55 juta lot vs aplikasi
        # 854 ribu lot — hampir PERSIS 10x lipat): sebelumnya lot volume
        # pakai buy_volume MENTAH (kotor, total beli SAJA), padahal
        # net_value_idr di atas SUDAH benar dikurangi jual — lot volume-nya
        # lupa. Sekarang KEDUANYA konsisten net (buy - sell).
        net_volume_lot = ((row.get("buy_volume") or 0) - (row.get("sell_volume") or 0)) / 100
        activity.append({
            "ticker": ticker,
            "net_value_idr": net_value,
            "buy_volume_lot": round(net_volume_lot),  # 1 lot = 100 lembar
            "buy_avg_price": row.get("buy_avg"),
            "buy_freq": row.get("buy_freq"),
        })
    activity.sort(key=lambda a: a["net_value_idr"], reverse=True)
    return activity


# MBSS v2 (user request — whitelist broker smart-money, REVISI dari daftar
# awal): diperluas dari temuan user sendiri, BOLEH diedit lagi kapan saja
# begitu ada referensi lebih solid — ini BUKAN daftar final/terverifikasi
# dari sumber otoritatif tunggal (riset web tidak menemukan satu sumber
# yang secara eksplisit me-ranking kode-kode ini sebagai "smart money" —
# konsisten dengan kebijaksanaan komunitas informal soal broker
# institusi/asing besar, tapi validasi paling kuat tetap dari data winrate
# kita sendiri seiring waktu).
SMART_MONEY_BROKER_WHITELIST = ["ZP", "AK", "BK", "YU", "AI", "SQ", "BB", "KZ", "RX", "RF", "DX", "HP", "KI"]


def get_smart_money_accumulation(ticker: str, tickers_data: dict) -> list:
    """
    Untuk 1 ticker: broker mana dari SMART_MONEY_BROKER_WHITELIST yang NET
    BUY di ticker ini, beserta avg price & volume — dipakai buat tag
    tambahan di /check, /hc, /consensus, dll ("Akumulasi AK: XX lot @ avg
    Rp XXX"). Return list kosong kalau ticker tidak ada di cache broksum_250
    (di luar 250 ticker berskor tertinggi) atau tidak ada whitelist broker
    yang net buy di situ.
    """
    rows = tickers_data.get(ticker, [])
    result = []
    for row in rows:
        code = row.get("code")
        if code not in SMART_MONEY_BROKER_WHITELIST:
            continue
        net_value = (row.get("buy_value") or 0) - (row.get("sell_value") or 0)
        if net_value <= 0:
            continue
        net_volume_lot = ((row.get("buy_volume") or 0) - (row.get("sell_volume") or 0)) / 100
        result.append({
            "code": code,
            "net_value_idr": net_value,
            "buy_volume_lot": round(net_volume_lot),
            "buy_avg_price": row.get("buy_avg"),
        })
    result.sort(key=lambda r: r["net_value_idr"], reverse=True)
    return result


def format_smart_money_tag(ticker: str, tickers_data: dict, prefix: str = "\n   ") -> str:
    """Formatter satu pintu, konsisten dipakai di semua tools yang menampilkan tag ini."""
    accum = get_smart_money_accumulation(ticker, tickers_data)
    if not accum:
        return ""
    parts = [f"{a['code']} {a['buy_volume_lot']:,} lot @ avg {a['buy_avg_price']:.0f}" for a in accum]
    return f"{prefix}💰 Akumulasi smart money: {', '.join(parts)}"


def format_market_mover_tag(ticker: str, prefix: str = "\n   ") -> str:
    """
    MBSS v2 (RapidAPI integration) — formatter satu pintu untuk tools live/
    intraday (mengikuti konvensi format_sector_tag/format_smart_money_tag
    yang sama persis), tag KALAU ticker ini muncul di checkpoint intraday
    (09:30/14:30 WIB, get_or_refresh_intraday_market_snapshot) sebagai top
    gainer hari ini. Sumber data TIDAK disebut di teks (konvensi user-
    facing sesi ini) — tampil sebagai konfirmasi "aktif hari ini", bukan
    callout API eksternal. String kosong kalau tidak ada data/ticker tidak
    muncul (None-safe, sama seperti tag lain).
    """
    try:
        mover = get_market_mover_for_ticker(ticker)
    except Exception:
        mover = None
    if not mover:
        return ""
    change_pct = (mover.get("change") or {}).get("percentage", 0)
    net_foreign_buy = (mover.get("net_foreign_buy") or {}).get("formatted")
    fbuy_note = f", asing net beli {net_foreign_buy}" if net_foreign_buy and net_foreign_buy != "-" else ""
    return f"{prefix}📶 Aktif hari ini: top gainer {change_pct:+.1f}%{fbuy_note}"


def get_broker_entry_ceiling_from_broksum250(ticker: str, tickers_data: dict) -> dict | None:
    """
    MBSS v2 (user request — jadikan broksum_250 SUMBER UTAMA ceiling
    asterisk, gantikan ketergantungan ke screenshot manual untuk 250 saham
    teratas): ambil avg_buy_price TERTINGGI di antara broker WHITELIST yang
    NET BUY di ticker ini — logika ceiling SAMA seperti versi screenshot
    lama (get_broker_entry_ceiling: "bahkan buyer besar paling agresif pun
    cuma berani sampai sini"), cuma sumber datanya beda (batch API
    otomatis, bukan upload manual).

    Return None kalau ticker tidak ada di broksum_250 (di luar top 250)
    atau tidak ada broker whitelist yang net buy di situ.
    """
    accum = get_smart_money_accumulation(ticker, tickers_data)
    if not accum:
        return None
    highest = max(accum, key=lambda a: a["buy_avg_price"])
    return {"avg_price": highest["buy_avg_price"], "code": highest["code"], "source": "broksum_250"}


def get_best_available_ceiling(ticker: str, tickers_data: dict) -> dict | None:
    """
    MBSS v2 (user request): fungsi terpadu — PRIORITASKAN broksum_250
    (otomatis, cakupan luas 250 saham, update tiap malam) DULU, baru kalau
    ticker itu di luar cakupan (bukan top-250) atau kebetulan tidak ada
    broker whitelist yang net buy di situ, JATUH ke brokersum screenshot
    manual (cakupan sempit tapi bisa ticker apa saja yang user cek).
    Dipakai sebagai pengganti langsung pola lama
    "get_cached_brokersum + get_broker_entry_ceiling" di semua tools.
    """
    ceiling = get_broker_entry_ceiling_from_broksum250(ticker, tickers_data)
    if ceiling:
        return ceiling
    cached_bs = get_cached_brokersum(ticker)
    if cached_bs:
        legacy_ceiling = get_broker_entry_ceiling(cached_bs)
        if legacy_ceiling:
            legacy_ceiling["source"] = "screenshot"
            return legacy_ceiling
    return None


def classify_bias_bandar(ticker: str, daily_history: dict, price_change_pct: float = None) -> dict:
    """
    MBSS v2 (user request — studi kasus manual TMPO/MDIA/JGLE/DOOH/ICON):
    klasifikasi 5 kategori dari TREND histori harian broker whitelist
    (bukan snapshot statis) — AKUMULASI SEGAR, PULLBACK DIDUKUNG, DISTRIBUSI,
    AKUMULASI BASI, TANPA DUKUNGAN. Fallback "BELUM CUKUP DATA" kalau histori
    masih terlalu pendek (<2 hari) — genuinely mulai dari nol, tidak retroaktif.

    price_change_pct opsional: persentase gerak harga HARI INI (bukan histori)
    — dipakai buat bedakan AKUMULASI SEGAR vs PULLBACK DIDUKUNG (sama-sama
    net-buy aktif, beda cuma apakah harga lagi naik atau baru turun).

    ⚠️ KETERBATASAN JUJUR: histori ini dari fetch BATCH SEMALAM (bagian
    /eodscan) — TIDAK termasuk aktivitas broker HARI INI kalau baru mulai
    masuk saat rally sedang berjalan. Baru akan terdeteksi BESOK malam
    setelah /eodscan berikutnya.
    """
    entries = daily_history.get(ticker, [])
    if len(entries) < 2:
        return {"label": "BELUM CUKUP DATA", "detail": f"baru {len(entries)} hari histori terkumpul (minimal 2)"}

    daily_nets = []
    for entry in entries:
        net = sum((b.get("buy_value") or 0) - (b.get("sell_value") or 0) for b in entry["brokers"])
        daily_nets.append(net)

    latest_net = daily_nets[-1]
    prev_net = daily_nets[-2]
    total_net_all_days = sum(daily_nets)
    positive_days = sum(1 for n in daily_nets if n > 0)

    # Weighted avg beli ATAS SELURUH histori (buat referensi "avg bandar")
    total_buy_value, total_buy_volume = 0.0, 0.0
    for entry in entries:
        for b in entry["brokers"]:
            if b.get("buy_avg") and b.get("buy_value"):
                total_buy_value += b["buy_value"]
                total_buy_volume += b["buy_value"] / b["buy_avg"]
    overall_avg_buy = (total_buy_value / total_buy_volume) if total_buy_volume > 0 else None

    # Weighted avg jual HARI TERAKHIR saja (buat cek "realisasi untung" di DISTRIBUSI)
    latest_entry = entries[-1]
    sell_val = sum(b.get("sell_value") or 0 for b in latest_entry["brokers"])
    sell_vol = sum((b.get("sell_value") or 0) / (b.get("sell_avg") or 1) for b in latest_entry["brokers"] if b.get("sell_avg"))
    latest_sell_avg = (sell_val / sell_vol) if sell_vol > 0 else None

    # 1. DISTRIBUSI — net jual di hari terbaru
    if latest_net < 0:
        detail = "net jual di hari terakhir"
        if overall_avg_buy and latest_sell_avg and latest_sell_avg > overall_avg_buy:
            detail += f" — jual @ avg {latest_sell_avg:.0f}, LEBIH TINGGI dari avg beli historis {overall_avg_buy:.0f} (realisasi untung)"
        return {"label": "DISTRIBUSI", "detail": detail, "overall_avg_buy": overall_avg_buy}

    # 2. TANPA DUKUNGAN — nyaris tidak ada net-buy berarti sepanjang histori
    if total_net_all_days <= 0 or positive_days == 0:
        return {"label": "TANPA DUKUNGAN", "detail": "tidak ada net-buy broker whitelist berarti dalam histori", "overall_avg_buy": overall_avg_buy}

    # 3 & 4. Net masih positif — bedakan SEGAR/PULLBACK (aktif) vs BASI (melemah)
    masih_aktif = latest_net >= prev_net * 0.5  # tidak mengecil drastis dari hari sebelumnya
    if masih_aktif:
        if price_change_pct is not None and price_change_pct < 0:
            return {"label": "PULLBACK DIDUKUNG", "detail": f"harga turun hari ini ({price_change_pct:+.1f}%) TAPI broker whitelist tetap net-buy", "overall_avg_buy": overall_avg_buy}
        return {"label": "AKUMULASI SEGAR", "detail": f"net-buy masih aktif, Rp{latest_net:,.0f} hari terakhir", "overall_avg_buy": overall_avg_buy}

    return {"label": "AKUMULASI BASI", "detail": "net-buy historis positif tapi aktivitas terbaru sudah melemah jauh", "overall_avg_buy": overall_avg_buy}


# ==========================================
# 🏦 RAPIDAPI IDX MARKET INTELLIGENCE — 4th brokersum source (MBSS v2,
# RapidAPI integration). Interim real-broker-data source while Index Alpha's
# monthly quota is exhausted (resets next month). Basic free plan: 500
# requests/month total, 1 req/sec — separate budget from Index Alpha's own,
# tracked here via _rapidapi_idx_quota_check_and_increment (raw JSON file,
# same pattern as BROKERSUM_CACHE_FILE above, not engine.cache's
# cache_manager — that abstraction is engine/nightly.py's convention for
# the nightly-refreshed pipeline, this file's own caches have always been
# plain JSON).
# ==========================================
RAPIDAPI_IDX_QUOTA_FILE = os.path.join(core.PROJECT_ROOT, "rapidapi_idx_quota.json")
RAPIDAPI_IDX_MONTHLY_LIMIT = 500
RAPIDAPI_IDX_MONTHLY_BUDGET = 400  # soft cap, leaves headroom for overage safety
RAPIDAPI_ONDEMAND_CACHE_FILE = os.path.join(core.PROJECT_ROOT, "rapidapi_idx_ondemand_cache.json")
RAPIDAPI_WHITELIST_SWEEP_LOOKBACK_DAYS = 10  # matches BROKSUM_250_LOOKBACK_DAYS in engine/nightly.py


def _load_rapidapi_idx_quota() -> dict:
    if not os.path.exists(RAPIDAPI_IDX_QUOTA_FILE):
        return {}
    try:
        with open(RAPIDAPI_IDX_QUOTA_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca RapidAPI IDX quota tracker: {e}")
        return {}


def _save_rapidapi_idx_quota(quota: dict):
    try:
        with open(RAPIDAPI_IDX_QUOTA_FILE, "w") as f:
            json.dump(quota, f, indent=2)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan RapidAPI IDX quota tracker: {e}")


def _rapidapi_idx_quota_check_and_increment(endpoint: str) -> bool:
    """
    Hard monthly budget gate shared by every RapidAPI IDX fetcher below — a
    real overage cost is at stake past RAPIDAPI_IDX_MONTHLY_BUDGET (soft cap,
    below the hard RAPIDAPI_IDX_MONTHLY_LIMIT), so this is checked BEFORE any
    HTTP call, never after. Returns False (no HTTP call should happen) once
    the month's budget is used up; every caller must degrade to its existing
    fallback rather than crash/block when this returns False — same
    discipline as every other API integration in this file.
    """
    quota = _load_rapidapi_idx_quota()
    current_month = datetime.datetime.now(core.WIB).strftime("%Y-%m")
    if quota.get("month") != current_month:
        quota = {"month": current_month, "calls": {}}
    calls = quota.setdefault("calls", {})
    total_used = sum(calls.values())
    if total_used >= RAPIDAPI_IDX_MONTHLY_BUDGET:
        print(f"⚠️ RapidAPI IDX monthly budget ({RAPIDAPI_IDX_MONTHLY_BUDGET}) reached for {current_month} — skipping {endpoint}.")
        return False
    calls[endpoint] = calls.get(endpoint, 0) + 1
    _save_rapidapi_idx_quota(quota)
    return True


def get_rapidapi_idx_quota_status() -> dict:
    """Read-only status for diagnostics/tuning — never gates anything itself."""
    current_month = datetime.datetime.now(core.WIB).strftime("%Y-%m")
    quota = _load_rapidapi_idx_quota()
    if quota.get("month") != current_month:
        return {"used": 0, "budget": RAPIDAPI_IDX_MONTHLY_BUDGET, "remaining": RAPIDAPI_IDX_MONTHLY_BUDGET, "month": current_month, "by_endpoint": {}}
    calls = quota.get("calls", {})
    used = sum(calls.values())
    return {
        "used": used,
        "budget": RAPIDAPI_IDX_MONTHLY_BUDGET,
        "remaining": max(0, RAPIDAPI_IDX_MONTHLY_BUDGET - used),
        "month": current_month,
        "by_endpoint": calls,
    }


def fetch_rapidapi_broker_activity(broker_code: str, from_date: str, to_date: str, limit: int = 50) -> dict | None:
    """
    GET /api/market-detector/broker-activity/{code} — confirmed live (session
    testing): a single call returns ONE broker's net buy/sell activity across
    EVERY ticker on the exchange for the given date range (transactionType=
    TRANSACTION_TYPE_NET — each row is single-sided, buy OR sell, not both).
    This is what makes sweeping SMART_MONEY_BROKER_WHITELIST (13 calls) cover
    the whole market for those brokers, far cheaper than a per-ticker
    approach. Returns the {"bandar_detector": {...}, "broker_summary":
    {"brokers_buy": [...], "brokers_sell": [...]}} payload, or None on any
    failure (quota exhausted, network error, non-success response) — never
    raises.
    """
    if not _rapidapi_idx_quota_check_and_increment("broker_activity"):
        return None
    try:
        resp = requests.get(
            f"{core.RAPIDAPI_IDX_BASE_URL}/api/market-detector/broker-activity/{broker_code}",
            params={
                "limit": limit, "marketBoard": "MARKET_BOARD_ALL", "page": 1,
                "investorType": "INVESTOR_TYPE_ALL", "from": from_date, "to": to_date,
                "transactionType": "TRANSACTION_TYPE_NET",
            },
            headers=core.RAPIDAPI_IDX_HEADERS,
            timeout=30,
        )
        if resp.status_code == 429:
            print(f"⚠️ RapidAPI IDX rate-limited (429) for broker-activity/{broker_code} — backing off.")
            return None
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ RapidAPI IDX error for broker-activity/{broker_code}: {data}")
            return None
        return data.get("data", {}).get("data", {})
    except Exception as e:
        print(f"⚠️ RapidAPI IDX fetch failed for broker-activity/{broker_code}: {e}")
        return None
    finally:
        core.time.sleep(1.1)  # respect confirmed 1 req/sec limit


def fetch_rapidapi_sentiment(ticker: str, days: int = 7) -> dict | None:
    """
    GET /api/analysis/sentiment/{ticker} — retail-vs-bandar divergence, the
    one signal in this whole integration with no OHLCV-derivable proxy at
    all (Sprint 2 roadmap's blocked "Retail vs Institution Estimate" item).
    Returns the {"symbol", "retail_sentiment", "bandar_sentiment",
    "divergence", "top_brokers", "summary", ...} payload, or None on failure.
    """
    if not _rapidapi_idx_quota_check_and_increment("sentiment"):
        return None
    try:
        resp = requests.get(
            f"{core.RAPIDAPI_IDX_BASE_URL}/api/analysis/sentiment/{ticker}",
            params={"days": days},
            headers=core.RAPIDAPI_IDX_HEADERS,
            timeout=30,
        )
        if resp.status_code == 429:
            print(f"⚠️ RapidAPI IDX rate-limited (429) for sentiment/{ticker} — backing off.")
            return None
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ RapidAPI IDX error for sentiment/{ticker}: {data}")
            return None
        return data.get("data", {})
    except Exception as e:
        print(f"⚠️ RapidAPI IDX fetch failed for sentiment/{ticker}: {e}")
        return None
    finally:
        core.time.sleep(1.1)


def fetch_rapidapi_bandar_accumulation(ticker: str, days: int = 30) -> dict | None:
    """
    GET /api/analysis/bandar/accumulation/{ticker} — real broker-derived
    accumulation score/status/confidence/recommendation. Used only by
    /executiongate's on-demand fallback (a specific per-decision check, not a
    bulk nightly fetch) to validate/augment the existing OHLCV-only
    compute_executiongate_bandarmology_proxy() heuristic in legacy_core.py.
    Returns the {"symbol", "accumulation_score", "status", "confidence",
    "indicators", "signals", "recommendation", "entry_zone", ...} payload, or
    None on failure.
    """
    if not _rapidapi_idx_quota_check_and_increment("bandar_accumulation"):
        return None
    try:
        resp = requests.get(
            f"{core.RAPIDAPI_IDX_BASE_URL}/api/analysis/bandar/accumulation/{ticker}",
            params={"days": days},
            headers=core.RAPIDAPI_IDX_HEADERS,
            timeout=30,
        )
        if resp.status_code == 429:
            print(f"⚠️ RapidAPI IDX rate-limited (429) for bandar/accumulation/{ticker} — backing off.")
            return None
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ RapidAPI IDX error for bandar/accumulation/{ticker}: {data}")
            return None
        return data.get("data", {})
    except Exception as e:
        print(f"⚠️ RapidAPI IDX fetch failed for bandar/accumulation/{ticker}: {e}")
        return None
    finally:
        core.time.sleep(1.1)


def rapidapi_broker_activity_to_broksum_rows(activity_data: dict) -> dict:
    """
    Reshape one broker's RapidAPI activity response (TRANSACTION_TYPE_NET)
    into this file's existing per-ticker row shape — the same shape Index
    Alpha's fetch_broker_summary_raw/fetch_broker_summary_batch_raw already
    produce: {ticker: [{code, buy_value, sell_value, buy_volume, sell_volume,
    buy_avg, sell_avg, buy_freq, sell_freq}, ...]}. This is what lets the
    whitelist sweep merge straight into build_broksum_250's output in
    engine/nightly.py with zero changes to any consumer
    (get_smart_money_accumulation, classify_bias_bandar,
    format_smart_money_tag, get_broker_entry_ceiling, etc. all already read
    this exact shape).

    Fields confirmed live: buy rows have netbs_broker_code, netbs_stock_code,
    bval, blot, netbs_buy_avg_price; sell rows have the same but
    sval/slot/netbs_sell_avg_price, with sval/slot already NEGATIVE (net
    query — each row is single-sided, buy OR sell, never both) — abs() them
    here. Because each row is single-sided, the "other side" of every
    reshaped row is left as 0/None — this stays arithmetically compatible
    with existing consumers like get_smart_money_accumulation, which compute
    net = buy_value - sell_value regardless of source.
    """
    by_ticker: dict = {}
    bs = activity_data.get("broker_summary", {}) or {}
    for row in bs.get("brokers_buy", []) or []:
        ticker = row.get("netbs_stock_code")
        if not ticker:
            continue
        by_ticker.setdefault(ticker, []).append({
            "code": row.get("netbs_broker_code"),
            "buy_value": row.get("bval") or 0,
            "sell_value": 0,
            "buy_volume": row.get("blot") or 0,
            "sell_volume": 0,
            "buy_avg": row.get("netbs_buy_avg_price"),
            "sell_avg": None,
            "buy_freq": row.get("freq"),
            "sell_freq": None,
        })
    for row in bs.get("brokers_sell", []) or []:
        ticker = row.get("netbs_stock_code")
        if not ticker:
            continue
        by_ticker.setdefault(ticker, []).append({
            "code": row.get("netbs_broker_code"),
            "buy_value": 0,
            "sell_value": abs(row.get("sval") or 0),
            "buy_volume": 0,
            "sell_volume": abs(row.get("slot") or 0),
            "buy_avg": None,
            "sell_avg": row.get("netbs_sell_avg_price"),
            "buy_freq": None,
            "sell_freq": row.get("freq"),
        })
    return by_ticker


def _load_rapidapi_ondemand_cache() -> dict:
    if not os.path.exists(RAPIDAPI_ONDEMAND_CACHE_FILE):
        return {}
    try:
        with open(RAPIDAPI_ONDEMAND_CACHE_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca RapidAPI on-demand cache: {e}")
        return {}


def _save_rapidapi_ondemand_cache(cache: dict):
    try:
        with open(RAPIDAPI_ONDEMAND_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan RapidAPI on-demand cache: {e}")


def get_cached_rapidapi_idx(ticker: str, endpoint: str):
    """
    Read-only same-trading-day cache lookup — mirrors get_cached_brokersum's
    contract exactly (never fetches on its own, returns None if the entry is
    missing or from a stale trading day).
    """
    cache = _load_rapidapi_ondemand_cache()
    entry = cache.get(ticker, {}).get(endpoint)
    if not entry:
        return None
    if entry.get("date") != get_last_published_trading_day():
        return None
    return entry.get("data")


def get_cached_or_fetch_rapidapi_bandar_accumulation(ticker: str) -> dict | None:
    """
    Cache-check → fetch → cache-write, same orchestration shape as
    get_cached_or_fetch_brokersum. Used only by /executiongate's on-demand
    fallback for tickers outside the nightly whitelist sweep / sentiment
    shortlist coverage — same-day cached so repeated /executiongate calls on
    the same ticker within a trading day never spend a second live call.
    """
    cached = get_cached_rapidapi_idx(ticker, "bandar_accumulation")
    if cached is not None:
        return cached
    data = fetch_rapidapi_bandar_accumulation(ticker)
    if data is None:
        return None
    cache = _load_rapidapi_ondemand_cache()
    cache.setdefault(ticker, {})["bandar_accumulation"] = {
        "date": get_last_published_trading_day(),
        "data": data,
    }
    _save_rapidapi_ondemand_cache(cache)
    return data


def compute_top_buyer_persistence(ticker: str, lookback_entries: int = 5) -> dict:
    """
    Top Buyer Persistence (Sprint 2 roadmap item, engine/broker.py) — zero
    new API cost, pure computation over brokersum_history.json (already
    populated by append_brokersum_history on every real brokersum fetch, no
    matter the source — Index Alpha, Zapi, screenshot, or now RapidAPI via
    the nightly whitelist sweep). Counts which broker codes recur as a
    top-net-buyer across the last N history entries for this ticker.

    Complementary to (not a duplicate of) the whitelist-only Bias Bandar
    system (classify_bias_bandar above, fed by append_broksum_daily_history
    in engine/nightly.py) — that one only tracks
    SMART_MONEY_BROKER_WHITELIST codes, this tracks whichever brokers
    actually showed up as top buyers, whitelist or not.
    """
    history = _load_brokersum_history().get(ticker, [])
    recent = history[-lookback_entries:] if lookback_entries > 0 else history
    if not recent:
        return {"persistent_buyers": [], "has_persistent_buyer": False}

    counts: dict = {}
    for entry in recent:
        for code in entry.get("top_buyer_codes", []) or []:
            counts[code] = counts.get(code, 0) + 1

    persistent = [
        {"code": code, "days_present": n, "of_last": len(recent)}
        for code, n in sorted(counts.items(), key=lambda x: x[1], reverse=True)
        if n >= 2  # showed up as a top buyer on at least 2 of the recent entries
    ]
    return {"persistent_buyers": persistent, "has_persistent_buyer": bool(persistent)}


# ==========================================
# 💹 /check "BROKER INFO" (MBSS v2, RapidAPI integration) — replaces the old
# "💹 BROKER RIIL" block + the old ceiling logic in commands/check.py with a
# single, uniformly RapidAPI-sourced view (user request — mixing Index
# Alpha/Zapi/RapidAPI in the same display made day-to-day numbers
# incomparable, and having two different "ceiling" concepts in one message
# was confusing). Deliberately reuses market-detector/broker-summary/{ticker}
# — the one RapidAPI endpoint earlier excluded from the nightly pipeline as
# redundant — because /check needs the FULL per-ticker broker breakdown
# (whoever actually traded, not just the smart-money whitelist), which is
# exactly what this endpoint (and only this endpoint) provides.
# ==========================================


def fetch_rapidapi_broker_summary(ticker: str, from_date: str, to_date: str, limit: int = 25) -> dict | None:
    """
    GET /api/market-detector/broker-summary/{ticker} — full per-ticker broker
    breakdown (every broker that transacted, not just the smart-money
    whitelist). Same TRANSACTION_TYPE_NET row shape as
    fetch_rapidapi_broker_activity (confirmed live: identical field names),
    reshaped with the same rapidapi_broker_activity_to_broksum_rows helper.
    Returns the raw {"bandar_detector": {...}, "broker_summary":
    {"brokers_buy": [...], "brokers_sell": [...]}} payload, or None on any
    failure.
    """
    if not _rapidapi_idx_quota_check_and_increment("broker_summary"):
        return None
    try:
        resp = requests.get(
            f"{core.RAPIDAPI_IDX_BASE_URL}/api/market-detector/broker-summary/{ticker}",
            params={
                "limit": limit, "marketBoard": "MARKET_BOARD_ALL",
                "transactionType": "TRANSACTION_TYPE_NET", "investorType": "INVESTOR_TYPE_ALL",
                "from": from_date, "to": to_date,
            },
            headers=core.RAPIDAPI_IDX_HEADERS,
            timeout=30,
        )
        if resp.status_code == 429:
            print(f"⚠️ RapidAPI IDX rate-limited (429) for broker-summary/{ticker} — backing off.")
            return None
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ RapidAPI IDX error for broker-summary/{ticker}: {data}")
            return None
        return data.get("data", {}).get("data", {})
    except Exception as e:
        print(f"⚠️ RapidAPI IDX fetch failed for broker-summary/{ticker}: {e}")
        return None
    finally:
        core.time.sleep(1.1)


def get_cached_or_fetch_rapidapi_broker_summary(ticker: str, lookback_days: int = 10) -> dict | None:
    """
    Cache-check → fetch → cache-write, same-day cached like every other
    on-demand RapidAPI function — repeated /check calls on the same ticker
    within a trading day never spend a second live call. 10-day lookback by
    default (matches RAPIDAPI_WHITELIST_SWEEP_LOOKBACK_DAYS's convention) —
    widening the date range costs nothing extra, it's still one call, just a
    more representative multi-day read instead of a single noisy day.
    """
    cached = get_cached_rapidapi_idx(ticker, "broker_summary")
    if cached is not None:
        return cached
    to_date = datetime.datetime.now(core.WIB).date()
    from_date = to_date - datetime.timedelta(days=lookback_days)
    data = fetch_rapidapi_broker_summary(ticker, from_date.isoformat(), to_date.isoformat())
    if data is None:
        return None
    cache = _load_rapidapi_ondemand_cache()
    cache.setdefault(ticker, {})["broker_summary"] = {
        "date": get_last_published_trading_day(),
        "data": data,
    }
    _save_rapidapi_ondemand_cache(cache)
    return data


def append_dominance_history(ticker: str, dominance_pct: float):
    """
    Zero-extra-cost trend tracking (user request — "kalau memang ada saham
    yang sinyalnya muncul berulang, data ini jadi pelengkap yang bagus").
    Records TODAY's top-3 net-buy dominance % into the SAME
    brokersum_history.json used by append_brokersum_history/
    compute_top_buyer_persistence — merged into today's existing entry if
    one already exists (so this never clobbers fields another call wrote
    earlier the same day), or creates a new entry if not. Organic and
    opportunistic: only fills in on days /check actually ran for this
    ticker, gaps are real gaps, never interpolated. Read back via
    compute_dominance_trend().
    """
    history = _load_brokersum_history()
    entries = history.setdefault(ticker, [])
    today = get_last_published_trading_day()
    existing = next((e for e in entries if e.get("date") == today), None)
    if existing:
        existing["dominance_pct"] = dominance_pct
    else:
        entries.append({"date": today, "dominance_pct": dominance_pct})
    history[ticker] = sorted(entries, key=lambda e: e["date"])[-BROKERSUM_HISTORY_MAX_ENTRIES_PER_TICKER:]
    _save_brokersum_history(history)


def compute_dominance_trend(ticker: str, lookback_entries: int = 5) -> str | None:
    """
    Formats a simple trend string (e.g. "37% → 41% → 44%") from whatever
    dominance_pct entries exist in history. Returns None if fewer than 2
    data points exist — caller should omit the trend line entirely in that
    case, not show a 1-point "trend".
    """
    history = _load_brokersum_history().get(ticker, [])
    recent = [e for e in history[-lookback_entries:] if e.get("dominance_pct") is not None]
    if len(recent) < 2:
        return None
    return " → ".join(f"{e['dominance_pct']:.0f}%" for e in recent)


def compute_check_broker_info(ticker: str, broksum_data: dict, lookback_days: int = RAPIDAPI_WHITELIST_SWEEP_LOOKBACK_DAYS) -> dict | None:
    """
    /check's "💹 BROKER INFO" block — MBSS v2 (user request, quota
    conservation, RapidAPI production burn far faster than projected: ~30%
    of the 500/month plan gone in the first 2 days live). Previously this
    live-fetched market-detector/broker-summary/{ticker} +
    analysis/bandar/accumulation/{ticker} PER TICKER on every /check for a
    ticker not yet checked that day — same-day cached, but still a fresh
    spend for every new ticker, and that on-demand volume turned out to be
    the single biggest quota driver in practice. Now reads PURELY from
    `broksum_data` (nightly_engine.load_broksum_250() — Index Alpha top-100
    + the RapidAPI whitelist-sweep's 13 brokers, already fetched once
    during /eodscan) — ZERO live calls from /check itself, ever.

    Consequences the user explicitly accepted:
    - Returns None (no BROKER INFO shown) for any ticker outside tonight's
      cache coverage — no live fallback anymore. Checked elsewhere (other
      apps) if needed for those tickers.
    - `dominance_pct` (net-buy as % of ticker's TOTAL market transaction
      value) is dropped — that total-market figure only ever came from the
      live broker-summary response's bandar_detector block, with no
      equivalent in the reshaped nightly rows. `dominance_trend` (history
      of past dominance_pct readings) still works off history already
      recorded before this change, it just stops accruing new points.
    - `accumulation` (bandar/accumulation status/confidence/target) is
      dropped entirely — its only populator was /executiongate's own
      on-demand fallback, which has ALSO been cut (same reason: single
      biggest per-call-volume spend in production, ~42 calls in the first
      2 days live). Nothing fetches this endpoint anywhere anymore. The
      whitelist-sweep accumulation signal (whitelist_accumulation_net_pct,
      already computed nightly at zero extra cost) covers the same
      "who's accumulating this" question — see /hc's "AKUMULASI /
      PRA-BREAKOUT" section.
    - Tickers covered ONLY by the whitelist sweep (not in Index Alpha's
      top-100 that night) show net positions from just the 13
      SMART_MONEY_BROKER_WHITELIST codes, not the full "everyone who
      transacted" breakdown the live endpoint gave — same limitation
      /consensus's multi-broker section already lives with.

    IMPORTANT (user-facing text convention, decided this session): nothing
    in this function's OUTPUT should ever be rendered as "RapidAPI ___" in
    Telegram text — the caller (commands/check.py) must phrase this as a
    natural extension of the bot's existing broker/bandar vocabulary.
    """
    rows = broksum_data.get(ticker, [])
    if not rows:
        return None

    net_by_code: dict = {}
    for row in rows:
        code = row.get("code")
        if not code:
            continue
        entry = net_by_code.setdefault(code, {"code": code, "net_value": 0, "net_volume": 0, "buy_avg": None})
        entry["net_value"] += (row.get("buy_value") or 0) - (row.get("sell_value") or 0)
        entry["net_volume"] += (row.get("buy_volume") or 0) - (row.get("sell_volume") or 0)
        if row.get("buy_avg"):
            entry["buy_avg"] = row["buy_avg"]

    top3 = sorted(
        (e for e in net_by_code.values() if e["net_value"] > 0),
        key=lambda e: e["net_value"], reverse=True,
    )[:3]
    if not top3:
        return None
    for e in top3:
        e["tag"] = "smart money" if e["code"] in SMART_MONEY_BROKER_WHITELIST else "broker"

    net_buy_top3_value = sum(e["net_value"] for e in top3)
    net_buy_top3_volume = sum(e["net_volume"] for e in top3)
    priced = [e for e in top3 if e["buy_avg"]]
    ceiling = max(priced, key=lambda e: e["buy_avg"]) if priced else None

    return {
        "top_brokers": top3,  # [{code, net_value, net_volume, buy_avg, tag}, ...] up to 3
        "lookback_days": lookback_days,
        "net_buy_top3_value": net_buy_top3_value,
        "net_buy_top3_volume": net_buy_top3_volume,
        "dominance_pct": None,  # no longer derivable without the live broker-summary call — see docstring
        "dominance_trend": compute_dominance_trend(ticker),
        "ceiling_price": ceiling["buy_avg"] if ceiling else None,
        "ceiling_code": ceiling["code"] if ceiling else None,
    }


# ==========================================
# 📡 RAPIDAPI MARKET-WIDE SCANNERS (MBSS v2, RapidAPI integration) — each
# covers the ENTIRE exchange in one call (confirmed live: 30 alerts / N
# candidates per call), a fundamentally cheaper cost profile than the
# per-ticker/per-broker endpoints above. Nightly-batch only (see
# engine/nightly.py's build_rapidapi_market_intelligence_sweep) — never
# called per-ticker/on-demand.
# ==========================================


def fetch_rapidapi_breakout_alerts() -> dict | None:
    """
    GET /api/analysis/retail/breakout/alerts — market-wide volume/price
    breakout scan, no params needed (confirmed live: returns ~30 alerts
    across the whole exchange in one call). Returns the raw {"scan_date",
    "total_alerts", "alerts": [{symbol, name, alert_type, severity, price,
    change_percentage, volume, volume_vs_avg, indicators: {resistance_level,
    support_level, distance_to_resistance, distance_to_support,
    volume_confirmation, price_momentum, breakout_probability}, action,
    entry_trigger, target, stop_loss, timestamp}, ...]} payload, or None on
    any failure.
    """
    if not _rapidapi_idx_quota_check_and_increment("breakout_alerts"):
        return None
    try:
        resp = requests.get(
            f"{core.RAPIDAPI_IDX_BASE_URL}/api/analysis/retail/breakout/alerts",
            headers=core.RAPIDAPI_IDX_HEADERS,
            timeout=30,
        )
        if resp.status_code == 429:
            print("⚠️ RapidAPI IDX rate-limited (429) for breakout/alerts — backing off.")
            return None
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ RapidAPI IDX error for breakout/alerts: {data}")
            return None
        return data.get("data", {})
    except Exception as e:
        print(f"⚠️ RapidAPI IDX fetch failed for breakout/alerts: {e}")
        return None
    finally:
        core.time.sleep(1.1)


def fetch_rapidapi_multibagger_scan(min_score: int = 70, max_results: int = 20) -> dict | None:
    """
    GET /api/analysis/retail/multibagger/scan — market-wide multi-factor
    scanner (technical + volume + foreign_flow + accumulation sub-scores
    combined), 6-12 month timeframe. Deliberately called WITHOUT a `sector`
    param — confirmed live that it doesn't reliably filter by sector, and we
    want full-market coverage for the nightly sweep anyway. Returns the raw
    {"scan_date", "total_candidates", "filters_applied", "candidates":
    [{symbol, name, multibagger_score, potential_return, timeframe,
    current_price, reasons: {technical, volume, foreign_flow, accumulation},
    entry_zone, target_prices, risk_level, stop_loss, sector, market_cap},
    ...]} payload, or None on any failure.
    """
    if not _rapidapi_idx_quota_check_and_increment("multibagger_scan"):
        return None
    try:
        resp = requests.get(
            f"{core.RAPIDAPI_IDX_BASE_URL}/api/analysis/retail/multibagger/scan",
            params={"min_score": min_score, "max_results": max_results},
            headers=core.RAPIDAPI_IDX_HEADERS,
            timeout=30,
        )
        if resp.status_code == 429:
            print("⚠️ RapidAPI IDX rate-limited (429) for multibagger/scan — backing off.")
            return None
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ RapidAPI IDX error for multibagger/scan: {data}")
            return None
        return data.get("data", {})
    except Exception as e:
        print(f"⚠️ RapidAPI IDX fetch failed for multibagger/scan: {e}")
        return None
    finally:
        core.time.sleep(1.1)


# ==========================================
# 🎯 WHITELIST ACCUMULATION/DISTRIBUTION SIGNAL (MBSS v2, RapidAPI
# integration, user request — "diskusi trader") — computed PURELY from the
# nightly whitelist sweep's already-fetched broker rows (zero additional API
# cost), NOT from bandar_accumulation (that endpoint stays reserved for
# on-demand, per-ticker-triggered paths: /executiongate fallback, /check
# Broker Info — calling it for every swept ticker would cost one live call
# per ticker, defeating the whole point of the free whitelist sweep).
# Prioritizes SMART_MONEY_BROKER_WHITELIST codes specifically over generic
# broker concentration — concentration alone doesn't tell you WHO is
# concentrated; a single large retail-serving desk having a big day looks
# identical to genuine informed accumulation without this distinction.
# ==========================================


def compute_whitelist_accumulation_signal(ticker: str, broksum_rows: list) -> dict | None:
    """
    Ticker's whitelist-broker net position from already-fetched
    broksum_250 rows (Index Alpha + RapidAPI whitelist sweep, merged).
    Returns None if no whitelist broker appears in the rows at all, or if
    gross value is zero (can't compute a meaningful ratio).
    """
    whitelist_rows = [r for r in broksum_rows if r.get("code") in SMART_MONEY_BROKER_WHITELIST]
    if not whitelist_rows:
        return None
    net_value = sum((r.get("buy_value") or 0) - (r.get("sell_value") or 0) for r in whitelist_rows)
    gross_value = sum((r.get("buy_value") or 0) + (r.get("sell_value") or 0) for r in whitelist_rows)
    if gross_value <= 0:
        return None
    # Distinct broker codes, not row count (a broker could contribute both
    # a buy-leg and sell-leg row from different source merges).
    num_whitelist_brokers = len({r["code"] for r in whitelist_rows if r.get("code")})
    return {
        "net_pct": round((net_value / gross_value) * 100, 1),
        "net_value": net_value,
        "num_whitelist_brokers": num_whitelist_brokers,
    }


# ==========================================
# 📡 RAPIDAPI MARKET-WIDE SCANNERS, part 2 (MBSS v2, RapidAPI integration) —
# sectorRotation, marketMover, topBrokers, topStocks. Paths confirmed live
# by the user this session. Same shared quota gate, same error-handling
# shape as the rest of this file.
# ==========================================


def fetch_rapidapi_sector_rotation() -> dict | None:
    """
    GET /api/analysis/retail/sector-rotation — market-wide, all ~11 IDX
    sectors in one call, no params. Returns the raw {"analysis_date",
    "market_phase", "hot_sectors", "cold_sectors", "all_sectors": [{sector_id,
    sector_name, momentum_score, status, avg_return_today, total_value,
    foreign_flow, top_stocks, recommendation, companies_count, gainers_count,
    losers_count}, ...], "summary"} payload, or None on any failure.
    """
    if not _rapidapi_idx_quota_check_and_increment("sector_rotation"):
        return None
    try:
        resp = requests.get(
            f"{core.RAPIDAPI_IDX_BASE_URL}/api/analysis/retail/sector-rotation",
            headers=core.RAPIDAPI_IDX_HEADERS,
            timeout=30,
        )
        if resp.status_code == 429:
            print("⚠️ RapidAPI IDX rate-limited (429) for sector-rotation — backing off.")
            return None
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ RapidAPI IDX error for sector-rotation: {data}")
            return None
        return data.get("data", {})
    except Exception as e:
        print(f"⚠️ RapidAPI IDX fetch failed for sector-rotation: {e}")
        return None
    finally:
        core.time.sleep(1.1)


def fetch_rapidapi_market_mover(mover_type: str = "top-gainer") -> dict | None:
    """
    GET /api/movers/{mover_type} — market-wide, confirmed live with
    mover_type="top-gainer" (path segment, not a query param — e.g. would be
    "top-loser"/"top-value"/"top-volume" if those variants exist, unconfirmed).
    filterStocks fixed to MAIN_BOARD + DEVELOPMENT_BOARD (regular tradeable
    universe, excludes odd boards). Returns the raw {"data": {"mover_list":
    [{"stock_detail": {code, name, price, change, value, volume, frequency,
    net_foreign_buy, net_foreign_sell, market_cap, ...}, "mover_type", ...}]}}
    payload, or None on any failure.
    """
    if not _rapidapi_idx_quota_check_and_increment(f"market_mover:{mover_type}"):
        return None
    try:
        resp = requests.get(
            f"{core.RAPIDAPI_IDX_BASE_URL}/api/movers/{mover_type}",
            params={"filterStocks": "FILTER_STOCKS_TYPE_MAIN_BOARD,FILTER_STOCKS_TYPE_DEVELOPMENT_BOARD"},
            headers=core.RAPIDAPI_IDX_HEADERS,
            timeout=30,
        )
        if resp.status_code == 429:
            print(f"⚠️ RapidAPI IDX rate-limited (429) for movers/{mover_type} — backing off.")
            return None
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ RapidAPI IDX error for movers/{mover_type}: {data}")
            return None
        return data.get("data", {})
    except Exception as e:
        print(f"⚠️ RapidAPI IDX fetch failed for movers/{mover_type}: {e}")
        return None
    finally:
        core.time.sleep(1.1)


def fetch_rapidapi_top_brokers(period: str = "TB_PERIOD_LAST_1_DAY") -> dict | None:
    """
    GET /api/market-detector/top-broker — market-wide broker turnover
    ranking. CAVEAT (confirmed live, see module notes): net_value/buy_value/
    sell_value come back "0" in the tested call — this is a TURNOVER
    ranking (sorted by total_value), NOT a net-buying ranking. Used only for
    the manual /brokerdiscovery command (human-curated candidate discovery
    for SMART_MONEY_BROKER_WHITELIST) — never wired into an automated
    scoring path. Returns the raw {"data": {"date": {...}, "list": [{code,
    name, investor_type, total_value, net_value, buy_value, sell_value,
    total_volume, total_frequency, group}, ...]}} payload, or None on
    failure.
    """
    if not _rapidapi_idx_quota_check_and_increment("top_brokers"):
        return None
    try:
        resp = requests.get(
            f"{core.RAPIDAPI_IDX_BASE_URL}/api/market-detector/top-broker",
            params={
                "marketType": "MARKET_TYPE_ALL", "period": period,
                "order": "ORDER_BY_ASC", "sort": "TB_SORT_BY_TOTAL_VALUE",
            },
            headers=core.RAPIDAPI_IDX_HEADERS,
            timeout=30,
        )
        if resp.status_code == 429:
            print("⚠️ RapidAPI IDX rate-limited (429) for top-broker — backing off.")
            return None
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ RapidAPI IDX error for top-broker: {data}")
            return None
        return data.get("data", {})
    except Exception as e:
        print(f"⚠️ RapidAPI IDX fetch failed for top-broker: {e}")
        return None
    finally:
        core.time.sleep(1.1)


def fetch_rapidapi_top_stocks(value_type: str = "VALUE_TYPE_TOTAL") -> dict | None:
    """
    GET /api/market-detector/top-stock — market-wide ticker ranking by
    trading value, with foreign_value per ticker. start/end pinned to
    today (WIB) — the confirmed sample used a single-day range. Returns the
    raw {"data": {"top_buy": [...], "top_sell": [...], "total": [{rank,
    code, value, lot, average, foreign_value, frequency}, ...],
    "response_info": {...}}} payload, or None on any failure.
    """
    if not _rapidapi_idx_quota_check_and_increment("top_stocks"):
        return None
    today = datetime.datetime.now(core.WIB).date().isoformat()
    try:
        resp = requests.get(
            f"{core.RAPIDAPI_IDX_BASE_URL}/api/market-detector/top-stock",
            params={
                "start": today, "end": today, "valueType": value_type,
                "investorType": "INVESTOR_TYPE_ALL", "marketType": "MARKET_TYPE_ALL", "page": 1,
            },
            headers=core.RAPIDAPI_IDX_HEADERS,
            timeout=30,
        )
        if resp.status_code == 429:
            print("⚠️ RapidAPI IDX rate-limited (429) for top-stock — backing off.")
            return None
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ RapidAPI IDX error for top-stock: {data}")
            return None
        return data.get("data", {})
    except Exception as e:
        print(f"⚠️ RapidAPI IDX fetch failed for top-stock: {e}")
        return None
    finally:
        core.time.sleep(1.1)


# ==========================================
# ⏱️ INTRADAY CHECKPOINT (MBSS v2, RapidAPI integration) — getMarketMover +
# getTopStocks pulled OUT of the nightly sweep entirely: a last-night
# snapshot isn't useful for commands that run DURING market hours
# (/executiongate, /testopening, /screendaytrade live, /bsjp all want
# CURRENT data). Replaced with a lazy checkpoint at 09:30/14:30 WIB — this
# bot has NO in-process scheduler (bot.py: "no JobQueue — request-driven
# only"), so checkpoints are checked on every relevant command call instead
# of via internal cron. Only the FIRST caller after a checkpoint time
# passes actually spends an API call; everyone else that window reads cache.
# ==========================================
RAPIDAPI_INTRADAY_CHECKPOINT_FILE = os.path.join(core.PROJECT_ROOT, "rapidapi_intraday_checkpoint.json")
RAPIDAPI_INTRADAY_CHECKPOINTS = ["09:30", "14:30"]  # WIB — market open read, pre-close read


def _load_intraday_checkpoint() -> dict:
    if not os.path.exists(RAPIDAPI_INTRADAY_CHECKPOINT_FILE):
        return {}
    try:
        with open(RAPIDAPI_INTRADAY_CHECKPOINT_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca intraday checkpoint: {e}")
        return {}


def _save_intraday_checkpoint(state: dict):
    try:
        with open(RAPIDAPI_INTRADAY_CHECKPOINT_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan intraday checkpoint: {e}")


def get_or_refresh_intraday_market_snapshot() -> dict:
    """
    Call this from any live/intraday command before reading mover/top-stock
    data. If the current WIB time has passed a checkpoint not yet recorded
    for today, fetches fresh data and updates the checkpoint marker;
    otherwise returns whatever's already cached. A failed refresh just
    means the PREVIOUS checkpoint's data (or nothing, before today's first
    checkpoint) keeps serving — same graceful-degradation contract as every
    other RapidAPI fetcher in this file.
    """
    today = core.get_current_calendar_date_marker()
    state = _load_intraday_checkpoint()
    if state.get("date") != today:
        state = {"date": today, "last_checkpoint": None, "snapshot": {}}

    now_wib = datetime.datetime.now(core.WIB).strftime("%H:%M")
    passed = [cp for cp in RAPIDAPI_INTRADAY_CHECKPOINTS if now_wib >= cp]
    target_checkpoint = passed[-1] if passed else None

    if target_checkpoint and state.get("last_checkpoint") != target_checkpoint:
        mover = fetch_rapidapi_market_mover("top-gainer")
        top_stocks = fetch_rapidapi_top_stocks()
        # Non-destructive per-key merge — a failed fetch for one key must
        # not wipe out the other key's (or the previous checkpoint's) data.
        merged = dict(state.get("snapshot") or {})
        if mover is not None:
            merged["market_mover"] = mover
        if top_stocks is not None:
            merged["top_stocks"] = top_stocks
        state["snapshot"] = merged
        state["last_checkpoint"] = target_checkpoint
        _save_intraday_checkpoint(state)

    return state.get("snapshot") or {}


def get_market_mover_for_ticker(ticker: str) -> dict | None:
    """Cheap indexed lookup from the current checkpoint snapshot — triggers a
    checkpoint refresh check first (see get_or_refresh_intraday_market_snapshot),
    but only actually fetches if a new checkpoint window has opened."""
    snapshot = get_or_refresh_intraday_market_snapshot()
    movers = (snapshot.get("market_mover") or {}).get("mover_list") or []
    for m in movers:
        sd = m.get("stock_detail") or {}
        if sd.get("code") == ticker:
            return sd
    return None
