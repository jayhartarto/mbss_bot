"""
commands/scan.py — Command Layer: scan/screening group (MBSS v2 Sprint 1, Phase 5a)

Telegram handlers for /screendaytrade, /gptpick (+ its inline-button
callback), /executiongate, and /eodscan (+ /nightlyscan alias).

Scope note — "thin handler" vs "keep the deep logic where it lives"
---------------------------------------------------------------------
Following the same principle as Phases 2-4: only the actual Telegram-facing
entrypoint moves here. Deep scoring/ranking helpers that these handlers
call — `select_screendaytrade_v5_candidates`, `enrich_live_breakout_for_candidates`,
`rank_screendaytrade_refactor`, `compute_daytrade_v5_summary`,
`get_executiongate_session_status`, `get_executiongate_screendaytrade_autopicks`,
`build_executiongate_extra_candidates`, `evaluate_executiongate_watchlist`,
`compute_factor_scoring`, `compute_daytrade_score`, `safe_reply`, etc. — all
stay in engine/legacy_core.py, accessed via `core.xxx`. Moving those too
would mean dragging along most of the scoring engine; they're shared with
other command groups (`/check`, portfolio) that haven't been split yet.

EXCEPTION: the GPTPICK scoring helpers (`_gptpick_score` and its
sub-scores, `_gptpick_candidate_filter`, `_gptpick_format_item`, the
`GPTPICK_*` constants) moved here WHOLESALE, unlike everywhere else in this
refactor — confirmed via full-codebase search that nothing outside the
gptpick cluster itself ever calls them. They're self-contained and
exclusively gptpick's own scoring logic, so there was no reason to leave a
`core.` bridge for them. (If a dedicated `engine/gptscore.py` is ever split
out — the Executive Summary names one — these are the natural candidate to
move again from here into that module.)

Same circular-import rule as engine/nightly.py, engine/market.py, engine/broker.py
-----------------------------------------------------------------------------------
This is the FIRST module in the refactor where the dependency direction is
reversed from Phases 2-4: `engine/legacy_core.py`'s `build_app()` needs
these handler functions to register them with python-telegram-bot
(`CommandHandler("screendaytrade", commands_scan.screen_daytrade)` etc.),
while this module needs `core.xxx` for the deep helpers above — still a
two-way dependency, same fix: MODULE imports on both sides
(`import commands.scan as commands_scan` in legacy_core.py / `import
engine.legacy_core as core` here), never `from module import name`.
"""
from __future__ import annotations

import asyncio
import copy

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import engine.legacy_core as core
import engine.nightly as nightly_engine
import engine.broker as broker_engine
import engine.scoring as scoring_engine
import engine.market as market_engine


# ---------------------------------------------------------------------
# /eodscan (+ /nightlyscan) — manual trigger for the NightlyEngine pipeline
# ---------------------------------------------------------------------
async def eodscan_command(update, context):
    """
    /eodscan — jalankan full EOD scan manual sekali jalan:
    refresh DB Yahoo Finance -> hitung semua skor -> simpan cache bersama.
    Setelah ini /testbrief, /screendaytrade, /gptpick, /executiongate, dan
    /winrate bisa memakai cache yang sama tanpa fetch ulang.
    """
    await core.safe_reply(
        update.message,
        "🌙 Memulai EOD scan manual...\n"
        "Universe: ISSI eligible (Yahoo whitelist, ~389 ticker)\n"
        "Langkah: refresh DB → compute semua skor → hitung breadth → simpan cache → selesai"
    )
    try:
        await nightly_engine.run_nightly_full_scan(context)
    except Exception as e:
        await core.safe_reply(update.message, f"⚠️ EOD scan gagal: {str(e)[:250]}")


# ---------------------------------------------------------------------
# /screendaytrade
# ---------------------------------------------------------------------
VOLUME_SPIKE_THRESHOLD = 3.0  # vol_ref (last_bar_rvol atau volume_pace_ratio, mana yang lebih tinggi) >= ini dianggap "lonjakan ekstrem"


def detect_volume_spikes(candidates: list, count: int = 8) -> list:
    """
    MBSS v2 (user request, dari kasus nyata TALF/IATA/SGRO — lonjakan volume
    5-10x+ normal dalam 1 hari): deteksi MURNI dari rasio volume mentah
    (last_bar_rvol / volume_pace_ratio, tanpa cap), TERPISAH dari
    active_breakout.score.

    Kenapa terpisah, bukan cuma dipakai sebagai tie-break di rank_by_live_activity:
    compute_active_breakout_score() SENGAJA membatasi kontribusi volume ke skor
    di ambang 2.5x (vol_ref>=2.5 -> +20 poin, MAKSIMAL — lonjakan 10x dapat
    bonus yang SAMA dengan 2.5x), dan MENGHUKUM pola yang justru sering muncul
    di awal lonjakan liar (\"risiko chase tinggi\" kalau geraknya jauh dari open,
    skor dibatasi max 60 kalau harga di bawah VWAP). Jadi saham yang benar-benar
    meledak (uncapped, kadang belum \"rapi\" secara struktur) bisa dapat
    active_breakout.score BIASA SAJA meski volume-nya luar biasa — persis kasus
    yang mau ditangkap di sini, jadi TIDAK bisa cuma mengandalkan skor itu.
    """
    ranked = []
    for r in candidates:
        ab = r.get("active_breakout", {}) or {}
        if not ab.get("available"):
            continue
        vol_ref = max(ab.get("last_bar_rvol") or 0, ab.get("volume_pace_ratio") or 0)
        if vol_ref >= VOLUME_SPIKE_THRESHOLD:
            ranked.append((r, vol_ref))

    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return [r for r, _ in ranked[:count]]


def rank_by_live_activity(candidates: list, count: int = 12) -> list:
    """
    MBSS v2 (user request, dari cerita real trading — MDIA/FAST/IATA/DWGL/JGLE
    dimenangkan dengan cara ini): urutkan MURNI dari sinyal LIVE (bukan lane
    EOD seperti rank_screendaytrade_refactor) — proxy terdekat yang sudah ada
    ke "orderbook lagi aktif condong ke buyer": posisi harga vs VWAP + volume
    pace saat ini. Bot ini tidak punya akses order-book bid/ask asli (beda
    dari yang terlihat di app trading — itu perlu sumber data terpisah, belum
    ada) — VWAP+vol pace adalah pendekatan terbaik dari data yang sudah
    tersedia (5m bars via enrich_live_breakout_for_candidates).

    HANYA memasukkan kandidat yang benar-benar punya active_breakout live
    (available=True) — kalau dipanggil di luar jam bursa, hasilnya akan
    kosong (bukan bug, live 5m bars memang tidak ada artinya saat market tutup).

    Urutan: active_breakout score (utama) -> volume_pace_ratio (tie-break) ->
    jarak ke VWAP paling dekat (tie-break kedua, makin dekat makin siap gerak).
    """
    live_ready = [r for r in candidates if r.get("active_breakout", {}).get("available")]

    def sort_key(r):
        ab = r["active_breakout"]
        score = ab.get("score", 0) or 0
        vol_pace = ab.get("volume_pace_ratio") or 0
        vwap_dist = ab.get("vwap_distance_pct")
        # Closer to VWAP = tie-break priority (abs distance, smaller wins) —
        # invert to a positive number since sort is reverse=True below.
        vwap_closeness = -abs(vwap_dist) if vwap_dist is not None else -999
        return (score, vol_pace, vwap_closeness)

    live_ready.sort(key=sort_key, reverse=True)
    return live_ready[:count]


async def screen_daytrade(update, context):
    """
    Screening khusus day trade — TERPISAH dari brief pagi (bobot berbeda: fokus
    volatilitas/momentum/aktivitas sekarang, bukan value seimbang), dan TIDAK
    memakai Index Alpha sama sekali (data broker EOD, lag 1 hari, tidak cocok
    untuk horizon day trade jam-menit — kuota Index Alpha sebaiknya disimpan
    untuk /myportfolio yang horizonnya swing 2-10 hari, sesuai kesepakatan).

    Opsi "/screendaytrade issi" — universe JAUH lebih luas (ISSI, ratusan saham
    syariah, bukan cuma ISSI yang 70) dengan filter likuiditas tambahan
    (harga + volume rata-rata 10hr bursa >= 500rb lembar). Cache 2 MINGGU (14 hari kalender dari waktu build
    Sabtu) — build pertama di minggu itu MAHAL (bisa 30+ menit), builds
    berikutnya dalam minggu yang sama pakai cache, gratis/cepat.

    Opsi "/screendaytrade live" — MODE BARU (user request): urutkan MURNI
    dari sinyal live (VWAP + volume pace SEKARANG), bukan lane EOD. Untuk
    perburuan "saham mana yang lagi aktif/dikejar buyer saat ini" — HANYA
    berguna saat jam bursa (di luar jam bursa, tidak ada kandidat live untuk
    ditampilkan, karena memang tidak ada apa pun yang "aktif sekarang").
    """
    use_issi = len(context.args) > 0 and context.args[0].lower() == "issi"
    use_live = len(context.args) > 0 and context.args[0].lower() == "live"

    if use_issi:
        await core.safe_reply(
            update.message,
            "🔎 Screening universe ISSI (lebih luas dari ISSI), mohon tunggu — "
            "kalau ini pemakaian pertama dalam 2 minggu terakhir, build whitelist likuid bisa "
            "makan waktu beberapa menit (via Zapi bulk, jauh lebih cepat dari sebelumnya). "
            "Setelah itu, pemakaian berikutnya dalam 2 minggu ini akan instan (pakai cache)."
        )
        tickers = await asyncio.to_thread(core.load_or_build_issi_liquid_whitelist)
    elif use_live:
        await core.safe_reply(
            update.message,
            "🔎 Mencari saham AKTIF SEKARANG (VWAP + volume pace live), mohon tunggu — "
            "mode ini cuma berguna & akan kosong kalau dipakai di luar jam bursa..."
        )
        # MBSS v2 (user request, revisi): pakai universe LEBAR yang sama dengan
        # /eodscan & /testbrief (389 ticker), BUKAN ISSI-liquid yang sempit (212)
        # — tujuan mode ini justru menangkap saham yang skor EOD-nya biasa saja
        # (makanya tidak masuk radar screendaytrade/executiongate manapun) tapi
        # mendadak aktif live, jadi membatasi ke universe sempit dari awal
        # kontradiktif dengan tujuannya sendiri. Cache /eodscan sudah mencakup
        # universe ini dengan baik, jadi seharusnya tetap cepat (mayoritas cache
        # hit, sama seperti pengalaman /testbrief).
        sharia_universe = core.fetch_online_sharia_list()
        tickers = await asyncio.to_thread(core.load_or_build_whitelist, list(sharia_universe))
    else:
        await core.safe_reply(update.message, "🔎 Screening kandidat day trade, mohon tunggu (~beberapa menit, sama seperti /testbrief)...")
        sharia_universe = core.fetch_online_sharia_list()
        tickers = await asyncio.to_thread(core.load_or_build_whitelist, list(sharia_universe))

    scan_timeout = 5400  # 90 menit: cukup untuk universe besar dan cache Yahoo
    try:
        # fetch_tickers_scored_with_cache() cek cache scan malam dulu — kalau
        # dipanggil setelah jam 22:00 di hari yang sama, mode "issi" (universe
        # SAMA dengan yang di-scan job malam) bisa jadi nyaris instan.
        results, skip_reasons = await asyncio.wait_for(
            asyncio.to_thread(nightly_engine.fetch_tickers_scored_with_cache, tickers), timeout=scan_timeout
        )
    except asyncio.TimeoutError:
        await core.safe_reply(update.message, f"⏱️ Screening melebihi batas waktu {scan_timeout // 60} menit. Coba lagi nanti.")
        return

    if not results:
        await core.safe_reply(update.message, "⚠️ Tidak ada data yang berhasil diambil. Coba lagi nanti.")
        return

    # Stage 1 V5: setup lane + active closing momentum lane. Stage 2: live intraday breakout on shortlist only.
    # In "live" mode, widen BOTH the pre-candidate pool and the live-enrichment
    # pool — the default pool (top-20 by EOD V5 score) can miss a ticker
    # that's exploding live RIGHT NOW but looked "quiet" in EOD history, which
    # is exactly the gap this mode exists to close.
    pre_count = 40 if use_live else max(core.DAYTRADE_FINAL_PICKS_COUNT, 20)
    live_limit = 35 if use_live else 25
    pre_candidates, filter_tier_note = core.select_screendaytrade_v5_candidates(results, count=pre_count)
    live_pool = await asyncio.to_thread(
        core.enrich_live_breakout_for_candidates, pre_candidates, live_limit, True
    )
    ready_pool = [r for r in live_pool if r.get("active_breakout", {}).get("available") and r["active_breakout"].get("score", 0) >= 60]

    if use_live:
        top_candidates = rank_by_live_activity(live_pool, core.DAYTRADE_FINAL_PICKS_COUNT)
        filter_tier_note = f"AKTIF SEKARANG (VWAP+vol pace live) dari {len(pre_candidates)} kandidat EOD, {len(ready_pool)} punya data live tersedia"
    else:
        # Refactor ranking:
        # Tidak lagi murni scalping_readiness snapshot.
        # Ranking final memisahkan Fresh Breakout dan Strong Continuation.
        top_candidates = core.rank_screendaytrade_refactor(live_pool, core.DAYTRADE_FINAL_PICKS_COUNT)
        if len(ready_pool) >= core.DAYTRADE_FINAL_PICKS_COUNT:
            filter_tier_note = filter_tier_note + " + Positive Bias lane refactor + live active breakout context"
        else:
            filter_tier_note = filter_tier_note + " + Positive Bias lane refactor (fallback karena kandidat READY terbatas)"

    if use_live and not top_candidates:
        await core.safe_reply(
            update.message,
            "⚠️ Tidak ada saham dengan data live aktif saat ini — mode \"live\" cuma berguna "
            "saat jam bursa berlangsung. Coba /screendaytrade biasa untuk radar EOD."
        )
        return

    # Kunci picks hari ini untuk uji winrate — idempotent (tidak duplikat kalau
    # /screendaytrade dipanggil berkali-kali di hari yang sama).
    await asyncio.to_thread(core.lock_daily_daytrade_picks, top_candidates, "screendaytrade_live" if use_live else "screendaytrade")
    await asyncio.to_thread(core.save_latest_screendaytrade_picks, top_candidates)

    if use_live:
        lines = ["⚡ SCREENING DAY TRADE — AKTIF SEKARANG (live VWAP + vol pace)\n"]
        lines.append(f"{filter_tier_note}\n")
        lines.append("Catatan: Diurutkan MURNI dari sinyal live (bukan lane EOD) — proxy terdekat ke \"orderbook condong ke buyer\" dari data yang tersedia (bot ini TIDAK punya akses order-book bid/ask asli). Ini RADAR, bukan entry final.\n")
        for i, r in enumerate(top_candidates, 1):
            ab = r["active_breakout"]
            lines.append(
                f"{i}. {r['ticker']} — {ab.get('label', '-')} ({ab.get('score', 0)}/100)\n"
                f"   Harga {r.get('price')} | VWAP {ab.get('vwap', '-')} (jarak {ab.get('vwap_distance_pct', '-')}%) | Vol pace {ab.get('volume_pace_ratio', '-')}x\n"
                f"   Trigger {ab.get('trigger_price', '-')} | Invalid <{ab.get('invalidation_level', '-')}\n"
                f"   {ab.get('notes', '') or '-'}{market_engine.format_sector_tag(r.get('sector'))}"
            )

        # MBSS v2 (user request — kasus TALF/IATA/SGRO): bagian TERPISAH untuk
        # lonjakan volume ekstrem mentah — lihat docstring detect_volume_spikes()
        # untuk kenapa ini tidak bisa digabung ke ranking di atas.
        spikes = detect_volume_spikes(live_pool, count=8)
        if spikes:
            lines.append(f"\n🔥 LONJAKAN VOLUME EKSTREM (vol_ref ≥{VOLUME_SPIKE_THRESHOLD:.0f}x, di luar ranking di atas — bisa jadi belum \"rapi\" secara struktur, RISIKO LEBIH TINGGI)")
            for r in spikes:
                ab = r["active_breakout"]
                vol_ref = max(ab.get("last_bar_rvol") or 0, ab.get("volume_pace_ratio") or 0)
                lines.append(
                    f"  • {r['ticker']} — vol {vol_ref:.1f}x normal | Harga {r.get('price')} | "
                    f"vs VWAP {ab.get('vwap_distance_pct', '-')}% | {ab.get('label', '-')} ({ab.get('score', 0)}/100)"
                )

        await core.safe_reply(update.message, "\n\n".join(lines))
        return

    lines = ["⚡ SCREENING DAY TRADE - RADAR BREAKOUT V5 ACTIVITY\n"]
    lines.append(f"Kriteria: {filter_tier_note}\n")
    lines.append("Catatan: Ini RADAR, bukan entry final. Entry live wajib lewat /executiongate atau /check.\n")
    lines.append("Legend: B=Breakout, C=Continuation, Act=Activity/Liquidity, VolQ=Volume Breakout Quality, Room=Entry Room, Risk=risiko teknikal.\n")

    for i, r in enumerate(top_candidates, 1):
        v5 = core.compute_daytrade_v5_summary(r)
        ab = r.get("active_breakout", {})
        src_live = ""
        if ab.get("available"):
            src_live = f" | Active {ab.get('score')}/100 {ab.get('label')}"
        room = v5["room"]
        cont = v5["continuation"]
        risk = v5["risk"]
        br = v5["breakout"]
        volq = v5["volq"]
        lines.append(
            f"{i}. {r['ticker']} — {v5['label']}\n"
            f"   Total {v5['total']}/100 | Bias {r.get('_positive_bias', '-')}/100 | Lane {r.get('_positive_lane', '-')} | B {br['score']} | C {cont['score']} | Act {v5['activity']['score']} | VolQ {volq['score']} | Room {room['score']} | Safety {risk['score']}{src_live}\n"
            f"   Harga {r.get('price')} | Valid >{v5['valid_level']} | Ideal {v5['ideal']} | Invalid <{v5['invalid']}\n"
            f"   Room: {room['label']} ({room['dist_high_pct']}% ke high, upside TP1 {room['upside_tp1_pct']}%) | VolQ: {volq['label']} | Continuation: {cont['label']}\n"
            f"   Note: {v5['note']}{market_engine.format_sector_tag(r.get('sector'))}"
        )

    await core.safe_reply(update.message, "\n\n".join(lines))

    # Tombol upload Broker Summary ALL 3 hari untuk 12 saham hasil radar.
    try:
        buttons = []
        row = []
        for r in top_candidates:
            t = str(r.get("ticker", "")).upper().strip()
            if not t:
                continue
            row.append(InlineKeyboardButton(t, callback_data=f"bsdt_{t}"))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        if buttons:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "📊 Tambahkan Broker Summary untuk kandidat di atas.\n\n"
                    "Pilih ticker, lalu upload screenshot:\n"
                    "• Tab ALL\n"
                    "• Rentang 3 hari bursa\n"
                    "• Mode Net aktif\n\n"
                    "Prioritas upload: kandidat Fresh / Continuation terbaik."
                ),
                reply_markup=InlineKeyboardMarkup(buttons),
            )
    except Exception as e:
        print(f"⚠️ Gagal kirim tombol Broker Summary /screendaytrade: {e}")


# ---------------------------------------------------------------------
# /executiongate
# ---------------------------------------------------------------------
async def executiongate_command(update, context):
    status = core.get_executiongate_session_status()
    if not status["allowed"]:
        await core.safe_reply(update.message, f"⛔ /executiongate hanya aktif saat market live atau break siang. Status sekarang: {status['label']}.")
        return

    await core.safe_reply(update.message, f"🧭 Execution Gate berjalan ({status['label']}). Mengambil screendaytrade + testbrief + myportfolio/watchlist (tanpa top gainer)...")

    try:
        sharia_universe = list(core.fetch_online_sharia_list())
        wl = await asyncio.to_thread(core.load_or_build_whitelist, sharia_universe)
        autopicks = await asyncio.wait_for(asyncio.to_thread(core.get_executiongate_screendaytrade_autopicks, core.EXECUTION_GATE_AUTOPICKS), timeout=1800)
        for r in autopicks:
            r["executiongate_source"] = "screendaytrade"
        extra_sources = await asyncio.to_thread(core.build_executiongate_extra_candidates, core.EXECUTION_GATE_MAX_WATCHLIST)
        portfolio_watchlist = extra_sources.get("portfolio_watchlist", [])
        testbrief = extra_sources.get("testbrief", [])

        scored_by_ticker = {
            r.get("ticker"): r
            for r in autopicks
            if isinstance(r, dict) and r.get("ticker")
        }
        watch = list(scored_by_ticker.values())
        seen = set(scored_by_ticker.keys())

        def append_ticker(ticker, source_label):
            t = str(ticker or "").upper().strip()
            if not t or t in seen:
                return
            base = scoring_engine.compute_factor_scoring(t, include_quote_check=False)
            if not base:
                return
            base["executiongate_source"] = source_label
            watch.append(base)
            seen.add(t)

        for t in portfolio_watchlist:
            append_ticker(t, "myportfolio+watchlist")
            if len(watch) >= core.EXECUTION_GATE_MAX_WATCHLIST:
                break

        if len(watch) < core.EXECUTION_GATE_MAX_WATCHLIST:
            for t in testbrief:
                append_ticker(t, "testbrief")
                if len(watch) >= core.EXECUTION_GATE_MAX_WATCHLIST:
                    break

        evaluated = await asyncio.wait_for(
            asyncio.to_thread(
                core.evaluate_executiongate_watchlist,
                watch[:core.EXECUTION_GATE_MAX_WATCHLIST],
            ),
            timeout=1800
        )
    except asyncio.TimeoutError:
        await core.safe_reply(update.message, "⏱️ Execution Gate timeout. Coba ulang beberapa menit lagi atau pastikan cache screendaytrade sudah tersedia.")
        return
    except Exception as e:
        await core.safe_reply(update.message, f"⚠️ Execution Gate gagal: {str(e)[:200]}")
        return

    lines = [f"🧭 EXECUTION GATE — {status['label']}\n"]
    lines.append("ENTER sangat ketat: harga harus sehat vs VWAP, active breakout valid, vol pace hidup, risk/RR tidak buruk.\n")
    shown = 0
    for r in evaluated[:core.EXECUTION_GATE_MAX_WATCHLIST]:
        shown += 1
        icon = "🟢" if r["decision"] == "ENTER" else ("🟡" if r["decision"] == "WATCH" else "🔴")
        vp = r.get("vol_pace") if r.get("vol_pace") is not None else "-"
        lines.append(
            f"{shown}. {icon} {r['ticker']} — {r['decision']} ({r.get('gate_score',0)}/100) [{r.get('source','?')}]\n"
            f"   Breakout {r.get('breakout_score',0)}/100 | Risk {r.get('risk_score',0)}/100 | Active {r.get('active_score',0)}/100 {r.get('active_label','')}\n"
            f"   Harga {core.smart_round_price(r.get('price',0))} | VWAP dist {r.get('vwap_dist','-')}% | Vol pace {vp}x | Trigger {r.get('trigger') or '-'}\n"
            f"   Aksi: {r.get('action','-')} | Alasan: {', '.join(r.get('reasons',[])[:4])}\n"
        )
    lines.append("\nRule: ENTER boleh dipertimbangkan; WATCH tunggu /check membaik; FAIL no entry.")
    await core.send_long_message(update.message, "\n".join(lines))


# ---------------------------------------------------------------------
# /gptpick — scoring helpers (self-contained, moved wholesale — see module
# docstring for why this cluster is the one exception to the "core.xxx
# bridge" pattern used everywhere else in this file)
# ---------------------------------------------------------------------
GPTPICK_MIN_VALUE_TRADED_IDR = 1_500_000_000
GPTPICK_DEFAULT_TOP_N = 3
GPTPICK_MAX_TOP_N = 5
GPTPICK_TIMEOUT_SECONDS = 5400
GPTPICK_BROKERSUM_ENRICH_LIMIT = 10


def _gptpick_num(v, default=0.0):
    try:
        if v is None or v == "N/A":
            return default
        return float(v)
    except Exception:
        return default


def _gptpick_liquidity_score(scoring: dict) -> float:
    vt = _gptpick_num(scoring.get("value_traded"), 0.0)
    vr = _gptpick_num(scoring.get("vol_ratio"), 1.0)

    if vt >= 10_000_000_000:
        score = 10.0
    elif vt >= 5_000_000_000:
        score = 9.0
    elif vt >= 3_000_000_000:
        score = 8.0
    elif vt >= GPTPICK_MIN_VALUE_TRADED_IDR:
        score = 6.8
    else:
        score = 4.0

    if vr >= 2.5:
        score += 0.8
    elif vr >= 1.4:
        score += 0.4
    elif vr < 0.9:
        score -= 0.6

    return max(0.0, min(10.0, score))


def _gptpick_rs_score(scoring: dict) -> float:
    rs = scoring.get("relative_strength_vs_ihsg")
    rs = None if rs in (None, "N/A") else _gptpick_num(rs, None)
    if rs is None:
        return 5.0
    if rs >= 5:
        return 10.0
    if rs >= 2:
        return 8.6
    if rs >= 0:
        return 7.2
    if rs >= -2:
        return 5.6
    return 3.5


def _gptpick_rr_score(scoring: dict) -> float:
    rr = (scoring.get("targets") or {}).get("risk_reward_at_max")
    rr = _gptpick_num(rr, None)
    if rr is None:
        return 5.0
    if rr >= 3.0:
        return 10.0
    if rr >= 2.2:
        return 8.8
    if rr >= 1.5:
        return 7.4
    if rr >= 1.0:
        return 6.0
    # MBSS v2 Sprint 2 (Tier 1.4, lanjutan — user request): rr < 1.0 berarti
    # RISIKO LEBIH BESAR DARI POTENSI UNTUNG kalau entry di harga sekarang
    # (rr_at_max) — sebelumnya semua kasus 0<rr<1 dilempar ke satu skor datar
    # 4.5 (lumayan, ~45%), yang tidak cukup menghukum di komposit. Dipecah
    # jadi 2 tingkat supaya makin dekat ke 0 (makin buruk) makin ditekan.
    if rr >= 0.5:
        return 2.5
    if rr > 0:
        return 1.0
    return 3.0


def _gptpick_flow_score(scoring: dict) -> float:
    cmf = scoring.get("cmf")
    cmf = None if cmf in (None, "N/A") else _gptpick_num(cmf, None)
    score = 5.0
    if cmf is not None:
        if cmf >= 0.15:
            score = 9.5
        elif cmf >= 0.05:
            score = 8.0
        elif cmf >= 0.0:
            score = 6.5
        elif cmf >= -0.05:
            score = 5.0
        elif cmf >= -0.10:
            score = 3.8
        else:
            score = 2.8

    obv_div = scoring.get("obv_divergence")
    if obv_div == "bullish_divergence":
        score += 0.8
    elif obv_div == "bearish_divergence":
        score -= 1.2

    brokersum = scoring.get("brokersum") or {}
    if brokersum:
        net_flow = _gptpick_num(brokersum.get("net_foreign_flow_pct"), 0.0)
        concentration = _gptpick_num(brokersum.get("broker_concentration_pct"), 0.0)
        if net_flow > 0:
            score += min(1.2, net_flow / 12.0)
        elif net_flow < 0:
            score -= min(1.0, abs(net_flow) / 12.0)

        if concentration >= 20:
            score += 0.4
        elif concentration < 8:
            score -= 0.2

    return max(0.0, min(10.0, score))


def _gptpick_penalty(scoring: dict) -> tuple[float, list[str]]:
    penalties = []
    total = 0.0

    if scoring.get("is_financial_distress_flag"):
        penalties.append("distress")
        total += 2.5
    if scoring.get("is_near_price_floor"):
        penalties.append("near floor")
        total += 1.0
    if scoring.get("is_overbought_caution"):
        penalties.append("overbought")
        total += 0.7
    if scoring.get("is_volume_spike_anomaly"):
        penalties.append("spike anomaly")
        total += 0.7
    if scoring.get("chart_pattern") == "lower_highs_bearish":
        penalties.append("lower highs")
        total += 1.2
    # MBSS v2 Sprint 2 (Tier 1.4, lanjutan — user request): RR@max < 1 berarti
    # entry di harga sekarang menanggung risiko LEBIH BESAR dari potensi
    # untung — bukan sesuatu yang direkayasa/disamarkan (lihat catatan di
    # compute_factor_scoring soal TP1/SL genuine, bukan dipaksa demi RR
    # bagus), tapi juga tidak boleh diam-diam lolos ke top-3 tanpa hukuman
    # apa pun. Severity-nya sengaja mirip "lower highs" (1.2-1.8) — nyata
    # menekan skor, TIDAK otomatis diskualifikasi total (masih bisa naik top
    # kalau faktor lain jauh lebih kuat, yang memang kadang wajar untuk
    # setup momentum sangat kuat).
    rr_at_max = (scoring.get("targets") or {}).get("risk_reward_at_max")
    if rr_at_max is not None and rr_at_max < 1.0:
        penalties.append(f"RR jelek ({rr_at_max:.2f})")
        total += 1.8
    if scoring.get("action_id") == "AVOID_SELL":
        penalties.append("avoid/sell")
        total += 1.5
    elif scoring.get("action_id") == "MIXED_SIGNALS":
        penalties.append("mixed")
        total += 0.4

    return total, penalties


def _gptpick_bucket(score: float) -> str:
    if score >= 85:
        return "A+"
    if score >= 75:
        return "A"
    if score >= 65:
        return "B+"
    if score >= 55:
        return "B"
    return "C"


def _gptpick_score(scoring: dict) -> dict:
    daytrade = core.compute_daytrade_score(scoring)          # 0..10
    liquidity = _gptpick_liquidity_score(scoring)           # 0..10
    rs = _gptpick_rs_score(scoring)                         # 0..10
    rr = _gptpick_rr_score(scoring)                         # 0..10
    flow = _gptpick_flow_score(scoring)                     # 0..10
    high_conv = scoring.get("high_conviction") or {}
    try:
        criteria_met = float(high_conv.get("criteria_met", 0) or 0)
        criteria_checkable = float(high_conv.get("criteria_checkable", 0) or 0) or 1.0
        conviction = max(0.0, min(10.0, (criteria_met / criteria_checkable) * 10.0))
    except Exception:
        conviction = 5.0

    penalty, penalty_labels = _gptpick_penalty(scoring)

    raw = (
        daytrade * 4.4 +
        liquidity * 2.2 +
        rs * 1.2 +
        rr * 1.0 +
        flow * 0.8 +
        conviction * 0.4
    ) - (penalty * 2.3)

    final = max(0.0, min(100.0, round(raw * 1.05, 1)))
    confidence = "LOW"
    if final >= 82 and penalty <= 1.0:
        confidence = "HIGH"
    elif final >= 70 and penalty <= 2.5:
        # MBSS v2 Sprint 2 (Tier 1.4, lanjutan — user request): sama seperti
        # HIGH, skor akhir sendirian tidak cukup — kalau penalti menumpuk
        # (mis. overbought + spike anomaly + RR jelek sekaligus, kasus nyata
        # FAST di real output), faktor lain yang kuat bisa "menutupi" itu di
        # angka akhir tapi labelnya tetap kelihatan meyakinkan. Ambang 2.5
        # dipilih supaya SATU penalti ringan (mis. cuma overbought, 0.7)
        # masih lolos MED-HIGH, tapi kombinasi 2+ flag (mis. overbought+RR
        # jelek = 2.5, atau spike anomaly+RR jelek = 2.5) sudah didorong
        # turun ke MEDIUM — confidence lebih jujur mencerminkan risiko yang
        # sebenarnya menumpuk di baliknya.
        confidence = "MED-HIGH"
    elif final >= 60:
        confidence = "MEDIUM"

    # BUGFIX (ditemukan saat menambah peringatan RR jelek, MBSS v2 Sprint 2
    # Tier 1.4): penalty SEBELUMNYA selalu ditambahkan TERAKHIR ke reasons,
    # lalu keseluruhan list dipotong ke 5 item (reasons[:5]) — jadi kalau
    # sudah ada 5 alasan positif duluan (value/vol/RS/EMA21/SMA50), penalty
    # APA PUN JENISNYA (distress, overbought, lower highs, RR jelek, dst)
    # selalu diam-diam terpotong, tidak pernah kelihatan di pesan. Diperbaiki:
    # penalty masuk DULUAN (lebih penting untuk terlihat — itu peringatan,
    # bukan sekadar data pendukung), baru diikuti alasan positif mengisi
    # slot yang tersisa.
    reasons = []
    if penalty_labels:
        reasons.append("penalty: " + ", ".join(penalty_labels[:3]))
    if _gptpick_num(scoring.get("value_traded"), 0.0) >= 3_000_000_000:
        reasons.append(f"value {_gptpick_num(scoring.get('value_traded'), 0.0):,.0f}")
    if _gptpick_num(scoring.get("vol_ratio"), 0.0) >= 1.4:
        reasons.append(f"vol x{_gptpick_num(scoring.get('vol_ratio'), 0.0):.2f}")
    if _gptpick_num(scoring.get("relative_strength_vs_ihsg"), 0.0) > 0:
        reasons.append(f"RS +{_gptpick_num(scoring.get('relative_strength_vs_ihsg'), 0.0):.1f}%")
    if not scoring.get("is_below_ema21", False):
        reasons.append("above EMA21")
    if not scoring.get("is_below_sma50", False):
        reasons.append("above SMA50")
    if scoring.get("brokersum"):
        bs = scoring["brokersum"]
        reasons.append(
            f"broker net { _gptpick_num(bs.get('net_foreign_flow_pct'), 0.0):+.1f}% "
            f"conc { _gptpick_num(bs.get('broker_concentration_pct'), 0.0):.1f}%"
        )

    return {
        "final": final,
        "bucket": _gptpick_bucket(final),
        "confidence": confidence,
        "daytrade": round(daytrade, 1),
        "liquidity": round(liquidity, 1),
        "rs": round(rs, 1),
        "rr": round(rr, 1),
        "flow": round(flow, 1),
        "conviction": round(conviction, 1),
        "penalty": round(penalty, 1),
        "reasons": reasons[:5],
    }


def _gptpick_candidate_filter(scoring: dict) -> bool:
    if not scoring:
        return False
    if scoring.get("is_financial_distress_flag"):
        return False
    if scoring.get("chart_pattern") == "lower_highs_bearish":
        return False
    if scoring.get("is_near_price_floor"):
        return False
    # MBSS v2 Sprint 2 (Tier 1.4, lanjutan — user request, ditemukan lewat
    # kasus nyata COCO): AVOID_SELL adalah rank TERENDAH di seluruh sistem
    # (ACTION_RANK = {"AVOID_SELL": 0, ...}, "HINDARI / JUAL") — sudah
    # diperlakukan sebagai sinyal exit/disqualifying di tempat lain (lihat
    # EXIT_CANDIDATE priority classification). Sebelumnya cuma kena penalti
    # kecil (-1.5) di _gptpick_penalty, yang bisa "kalah" dari faktor lain
    # yang kuat — terbukti nyata: COCO tetap masuk top-3 dengan MED-HIGH
    # walau statusnya sendiri HINDARI/JUAL. Sekarang dikeluarkan total dari
    # kandidat, bukan sekadar dikurangi skornya — kontradiktif kalau
    # shortlist "saham layak beli" memuat saham yang sistem sendiri bilang
    # jangan dibeli.
    if scoring.get("action_id") == "AVOID_SELL":
        return False
    if _gptpick_num(scoring.get("value_traded"), 0.0) < GPTPICK_MIN_VALUE_TRADED_IDR:
        return False
    return True


def _gptpick_format_item(scoring: dict) -> str:
    g = scoring.get("_gptpick") or {}
    t = scoring.get("targets") or {}
    buy = t.get("buy_range", "-")
    tp1 = t.get("tp_1", "-")
    sl = t.get("cut_loss", "-")
    rr = t.get("risk_reward_at_max", None)
    rr_text = f"{rr:.2f}" if isinstance(rr, (int, float)) else "-"
    # MBSS v2 Sprint 2 (Tier 1.4, lanjutan — user request): peringatan RR
    # jelek DIJAMIN tampil di sini, terpisah dari "reasons" (yang dibatasi
    # 5 item dan bisa saja tidak menyisakan slot buat ini kalau kandidatnya
    # punya banyak reasons lain). Angka RR itu sendiri genuine/tidak
    # direkayasa (lihat catatan di compute_factor_scoring) — ini cuma
    # memastikan angka jujur itu benar-benar KELIHATAN, bukan diam-diam
    # terkubur di antara 4-5 angka lain di dashboard.
    rr_warning = ""
    if isinstance(rr, (int, float)) and rr < 1.0:
        rr_warning = f"\n  ⚠️ RR@max {rr_text} — risiko lebih besar dari potensi untung kalau entry di harga sekarang"
    return (
        f"{scoring.get('ticker', '-')}: {g.get('bucket', '-') } {g.get('final', 0):.1f}/100 | {g.get('confidence', '-')}\n"
        f"  LQ {g.get('liquidity', 0):.1f} | DT {g.get('daytrade', 0):.1f} | RS {g.get('rs', 0):.1f} | RR {g.get('rr', 0):.1f} | FLOW {g.get('flow', 0):.1f}\n"
        f"  Buy {buy} | TP1 {tp1} | SL {sl} | RR@max {rr_text}"
        f"{rr_warning}\n"
        f"  {', '.join(g.get('reasons', [])) if g.get('reasons') else '—'}"
        f"{market_engine.format_sector_tag(scoring.get('sector'), prefix=chr(10) + '  ')}"
    )


async def _run_gptpick(update, context, top_n: int = GPTPICK_DEFAULT_TOP_N):
    top_n = max(1, min(GPTPICK_MAX_TOP_N, int(top_n)))
    message = update.effective_message

    await core.safe_reply(message, f"🔎 GPTPICK syariah mulai diproses (top {top_n})...")

    try:
        tickers = await asyncio.to_thread(core.load_or_build_issi_liquid_whitelist)
        if not tickers:
            await core.safe_reply(message, "⚠️ Whitelist ISSI liquid kosong.")
            return

        results, skip_reasons = await asyncio.wait_for(
            asyncio.to_thread(nightly_engine.fetch_tickers_scored_with_cache, tickers),
            timeout=GPTPICK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await core.safe_reply(message, f"⏱️ GPTPICK melebihi batas {GPTPICK_TIMEOUT_SECONDS // 60} menit.")
        return
    except Exception as e:
        await core.safe_reply(message, f"⚠️ GPTPICK gagal memuat data: {e}")
        return

    filtered = [copy.deepcopy(r) for r in results if _gptpick_candidate_filter(r)]
    if not filtered:
        await core.safe_reply(message, "⚠️ Tidak ada kandidat yang lolos filter GPTPICK.")
        return

    # Base ranking dulu, lalu refresh broker flow untuk kandidat teratas saja.
    for r in filtered:
        r["_gptpick"] = _gptpick_score(r)

    filtered.sort(
        key=lambda x: (
            x.get("_gptpick", {}).get("final", 0),
            _gptpick_num(x.get("relative_strength_vs_ihsg"), -99.0),
            _gptpick_num(x.get("value_traded"), 0.0),
        ),
        reverse=True,
    )

    broker_pool = filtered[:min(GPTPICK_BROKERSUM_ENRICH_LIMIT, len(filtered))]
    enriched = []
    for r in broker_pool:
        try:
            if not r.get("brokersum"):
                # MBSS v2 (user request, ditemukan lewat log nyata — 4 dari 10
                # kandidat gagal "Index Alpha error...: None"): cek cache
                # bersama dulu (gratis, sumber apa pun), TAPI kalau butuh fetch
                # BARU, pakai Zapi — BUKAN Index Alpha. Index Alpha kuotanya
                # cuma 5x/hari dan dipakai bersama /myportfolio brokersum
                # (posisi ASLI user, lebih penting) — GPTPick refresh top-10
                # tiap dipanggil bisa menghabiskan SELURUH kuota harian dalam
                # satu run, menyisakan nol untuk /myportfolio. Zapi kuotanya
                # jauh lebih longgar (~100/menit), aman dipakai sesering ini.
                cached = broker_engine.get_cached_brokersum(r.get("ticker"))
                if cached:
                    r["brokersum"] = cached
                else:
                    r["brokersum"] = await asyncio.to_thread(
                        broker_engine.compute_brokersum_metrics_zapi,
                        r.get("ticker"),
                        r.get("cmf"),
                        r.get("obv_divergence"),
                    )
            if r.get("brokersum"):
                r = scoring_engine.apply_brokersum_adjustment(r, r["brokersum"])
        except Exception as e:
            print(f"⚠️ GPTPICK brokersum enrich gagal untuk {r.get('ticker')}: {e}")
        r["_gptpick"] = _gptpick_score(r)
        enriched.append(r)

    # Re-rank enriched top pool + remaining untouched tail (to keep all candidates valid).
    rest = filtered[GPTPICK_BROKERSUM_ENRICH_LIMIT:]
    combined = enriched + rest
    combined.sort(
        key=lambda x: (
            x.get("_gptpick", {}).get("final", 0),
            _gptpick_num(x.get("relative_strength_vs_ihsg"), -99.0),
            _gptpick_num(x.get("value_traded"), 0.0),
        ),
        reverse=True,
    )

    picks = combined[:top_n]

    # MBSS v2 Sprint 2 (Tier 1.4, revisi setelah dicek ulang): pakai lane
    # classifier YANG SAMA dengan /screendaytrade (compute_screendaytrade_
    # positive_bias) supaya signal_label di /winrate genuinely sebanding
    # lintas source — bukan tier kasar hasil mapping manual. Ini AMAN &
    # GRATIS di sini: fungsinya murni dari field EOD yang sudah ada di
    # `scoring` (ret_1d_pct, close_pos_day, dst — lihat komentar
    # "Radar labels, not live entry signals" di compute_daytrade_v5_summary),
    # BUKAN dari data live/intraday. GPTPick tidak fetch active_breakout
    # (bonus kecil khusus live) — fungsi ini sudah menangani itu dengan
    # aman (default 0 kalau field-nya tidak ada), jadi lane-nya tetap valid,
    # cuma tanpa bonus live yang memang tidak relevan untuk radar EOD.
    for r in picks:
        try:
            bias = core.compute_screendaytrade_positive_bias(r)
            r["_positive_lane"] = bias["lane"]
        except Exception as e:
            print(f"⚠️ Gagal hitung lane untuk {r.get('ticker')}: {e}")

    # MBSS v2 Sprint 2 (Tier 1.4): kunci pick GPTPick lewat mekanisme yang
    # SAMA dengan /screendaytrade (source="gptpick" membedakan keduanya),
    # supaya /winrate bisa menunjukkan performa GPTPick juga tanpa sistem
    # tracking terpisah. Gagal-lunak — kalau lock gagal, tetap kirim hasil
    # GPTPick ke user seperti biasa, jangan sampai fitur utama ikut gagal.
    try:
        await asyncio.to_thread(core.lock_daily_daytrade_picks, picks, "gptpick")
    except Exception as e:
        print(f"⚠️ Gagal mengunci picks GPTPick untuk /winrate: {e}")

    lines = [
        "🏆 GPTPICK SYARIAH SHORTLIST",
        f"Universe: ISSI liquid | filter min value traded Rp{GPTPICK_MIN_VALUE_TRADED_IDR:,.0f}",
        f"Lolos filter: {len(filtered)} kandidat | broker refresh: {min(GPTPICK_BROKERSUM_ENRICH_LIMIT, len(filtered))} kandidat | top {top_n}",
        "",
    ]
    for i, r in enumerate(picks, 1):
        lines.append(f"{i}. {_gptpick_format_item(r)}")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Top 3", callback_data="gptpick:3"),
            InlineKeyboardButton("Top 5", callback_data="gptpick:5"),
        ]
    ])

    try:
        await message.reply_text("\n\n".join(lines), reply_markup=keyboard)
    except Exception as e:
        # fallback plain text if keyboard send fails
        print(f"⚠️ GPTPICK final send with keyboard failed: {e}")
        await core.safe_reply(message, "\n\n".join(lines))


async def gptpick_command(update, context):
    top_n = GPTPICK_DEFAULT_TOP_N
    if context.args:
        try:
            top_n = int(context.args[0])
        except Exception:
            top_n = GPTPICK_DEFAULT_TOP_N
    await _run_gptpick(update, context, top_n=top_n)


async def gptpick_callback(update, context):
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    top_n = GPTPICK_DEFAULT_TOP_N
    try:
        _, raw_n = query.data.split(":", 1)
        top_n = int(raw_n)
    except Exception:
        pass
    await _run_gptpick(update, context, top_n=top_n)


async def high_conviction_command(update, context):
    """
    /hc — top 10 saham HIGH CONVICTION dari cache /eodscan malam terakhir,
    diurutkan skor final tertinggi (MBSS v2, user request — kasus nyata
    DMAS: skor 8.4, Nilai 9.2, ceiling asterisk dari broker screenshot).

    /hc rr — sama, tapi diurutkan risk_reward_at_max TERTINGGI (user
    request lanjutan) — ticker tanpa RR yang bisa dihitung otomatis
    ditaruh paling belakang, TIDAK di-exclude (tetap HIGH CONVICTION,
    cuma datanya belum lengkap untuk dibandingkan RR-nya).

    Murni baca cache (nightly_engine.load_daily_scan_cache) — TIDAK fetch
    apa pun, jadi instan. is_high_conviction sudah dihitung penuh saat
    /eodscan (7-kriteria Minervini/IBD-style breakout check), tinggal
    filter+urutkan di sini.
    """
    sort_by_rr = len(context.args) > 0 and context.args[0].lower() == "rr"

    scored = nightly_engine.load_daily_scan_cache()
    if not scored:
        await core.safe_reply(
            update.message,
            "⚠️ Cache /eodscan belum ada atau sudah basi untuk hari ini — jalankan /eodscan dulu."
        )
        return

    candidates = [r for r in scored.values() if r.get("high_conviction", {}).get("is_high_conviction")]
    if not candidates:
        await core.safe_reply(update.message, "📋 Tidak ada saham HIGH CONVICTION di cache hari ini.")
        return

    if sort_by_rr:
        # None-safe: ticker tanpa RR terhitung ditaruh PALING BELAKANG (bukan
        # dibuang) — pakai (punya_rr, nilai_rr) sebagai key gabungan supaya
        # yang punya RR selalu menang dibanding yang tidak, baru di antara
        # yang sama-sama punya RR diurutkan dari tertinggi.
        candidates.sort(
            key=lambda r: (
                isinstance(r.get("targets", {}).get("risk_reward_at_max"), (int, float)),
                r.get("targets", {}).get("risk_reward_at_max") or 0,
            ),
            reverse=True,
        )
        sort_label = "RR tertinggi"
    else:
        # MBSS v2 (user request — /hc positioning vs /screendaytrade): urutan
        # DEFAULT diubah dari "Skor Final" (Value/Momentum/Sentimen 25/45/30%,
        # formula UNIVERSAL yang juga dipakai /myportfolio buat posisi jangka
        # menengah — TIDAK diubah bobotnya di situ) ke compute_daytrade_score
        # — formula momentum-murni yang SUDAH ADA & SUDAH teruji (dipakai
        # screendaytrade/gptpick, sudah dapat perbaikan arah-ADX). Dipilih
        # REUSE daripada bikin formula momentum baru lagi (persis yang mau
        # dihindari — jangan tambah formula paralel lagi). "Skor Final" tetap
        # DITAMPILKAN di output (Nilai/Momentum/Sentimen breakdown) buat
        # transparansi, cuma bukan lagi KUNCI urutnya.
        for r in candidates:
            r["_daytrade_score_hc"] = core.compute_daytrade_score(r)
        candidates.sort(key=lambda r: r["_daytrade_score_hc"], reverse=True)
        sort_label = "momentum (compute_daytrade_score, bukan Skor Final)"
    top10 = candidates[:10]

    # MBSS v2 (user request — ditemukan lewat penelusuran manual /winrate: /hc
    # TIDAK PERNAH terlacak sama sekali sebelumnya). Kunci lewat mekanisme
    # yang SAMA dengan screendaytrade/gptpick/testbrief, source="hc" —
    # dipakai untuk KEDUA mode urutan (final/rr), karena kriteria eligibility
    # dasarnya (is_high_conviction) sama, cuma urutan tampilan yang beda.
    # Gagal-lunak — kalau lock gagal, tetap tampilkan hasil seperti biasa.
    try:
        await asyncio.to_thread(core.lock_daily_daytrade_picks, top10, "hc")
    except Exception as e:
        print(f"⚠️ Gagal mengunci picks /hc untuk /winrate: {e}")

    # MBSS v2 (user request — gap ditemukan: streak tersimpan tapi TIDAK
    # PERNAH ditampilkan real-time, cuma muncul di ringkasan agregat
    # /winrate). Hitung & tampilkan LANGSUNG di sini supaya kelihatan saat
    # itu juga kalau sebuah saham lagi "beruntun" muncul, bukan cuma
    # ketahuan belakangan lewat /winrate.
    history_for_streak = core.load_daytrade_picks_history()
    pick_date_today = core.get_current_trading_day_close_marker()

    lines = [f"🔥 TOP {len(top10)} HIGH CONVICTION — urut {sort_label} (dari {len(candidates)} kandidat, cache /eodscan)\n"]
    for i, r in enumerate(top10, 1):
        s = r.get("scores", {})
        hc = r.get("high_conviction", {})
        t = r.get("targets", {})
        rr = t.get("risk_reward_at_max")
        rr_str = f"1:{rr:.2f}" if isinstance(rr, (int, float)) else "-"

        # Ceiling asterisk (persis pola /check) — cuma kalau brokersum SUDAH
        # ter-cache same-day untuk ticker ini (dari screenshot/Index Alpha
        # sebelumnya), TIDAK fetch baru di sini.
        ceiling_str = ""
        cached_bs = broker_engine.get_cached_brokersum(r["ticker"])
        if cached_bs:
            ceiling = broker_engine.get_broker_entry_ceiling(cached_bs)
            if ceiling:
                ceiling_str = f" / {ceiling['avg_price']:.0f}*"

        streak_any = core.compute_consecutive_appearance_streak_any_source(r["ticker"], pick_date_today, history_for_streak)
        streak_hc = core.compute_consecutive_appearance_streak(r["ticker"], "hc", pick_date_today, history_for_streak)
        streak_str = f" 🔁 {streak_any}x berturut-turut (lintas-tool)" if streak_any > 1 else ""
        if streak_hc > 1 and streak_hc != streak_any:
            streak_str += f", {streak_hc}x khusus /hc"

        daytrade_note = f" | DT {r['_daytrade_score_hc']:.1f}" if "_daytrade_score_hc" in r else ""

        sector_note = ""
        sector_info = market_engine.get_sector_rank_info(r.get("sector"))
        if sector_info:
            sector_note = f"\n   🏭 Sektor {sector_info['sector']}: #{sector_info['rank']}/{sector_info['total_sectors']} terkuat ({sector_info['avg_return_pct']:+.1f}% avg)"

        lines.append(
            f"{i}. {r['ticker']} — Final {s.get('final', 0):.1f}{daytrade_note}{streak_str} "
            f"(Nilai {s.get('value', 0):.1f} | Momentum {s.get('momentum', 0):.1f} | Sentimen {s.get('sentiment', 0):.1f})\n"
            f"   {hc.get('criteria_met', 0)}/{hc.get('criteria_checkable', 0)} kriteria | "
            f"RR {rr_str} | {r.get('action_label_id', '-')}\n"
            f"   Entry {t.get('buy_range', '-')}{ceiling_str}{sector_note}"
        )

    lines.append("\nDetail lengkap: /check TICKER")
    await core.safe_reply(update.message, "\n\n".join(lines))




# ==========================================
# 🌆 BSJP SCREENING (MBSS v2, user request — REVISI diperketat berdasarkan
# riset formula komunitas, termasuk yang tampak official dari Stockbit)
# "Beli Sore Jual Pagi" — momentum/continuation variant (BEDA dari varian
# reversal/rebound top-loser — riset dikonfirmasi ada 2 varian berbeda
# dengan nama sama). Harus dijalankan MANUAL menjelang closing (15:50-16:00
# WIB) — butuh data intraday live, TIDAK bisa dari cache /eodscan semalam.
# Filosofi (dari riset): "Ikuti Uang Besar".
# ==========================================
BSJP_MIN_GAIN_PCT = 5.0                # harga >= 1.05x previous close
BSJP_VOL_VS_MA20_MULTIPLIER = 2.0      # volume hari ini >= 2x rata-rata 20 hari
BSJP_VOL_VS_PREV_MULTIPLIER = 1.0      # volume hari ini >= 1x volume kemarin
BSJP_RSI_MIN = 60                      # dipertahankan lebih ketat dari riset (RSI>=50)
BSJP_RSI_MAX = 85
BSJP_MIN_VALUE_TRADED_IDR = 5_000_000_000  # dinaikkan dari 1M, SEKARANG WAJIB (bukan peringatan)
BSJP_ARA_DISTANCE_MIN_PCT = 0   # SEMENTARA permisif — user eksplisit minta lihat data real dulu
BSJP_ARA_DISTANCE_MAX_PCT = 15  # sebelum kunci rentang final (ganti setelah lihat sebaran nyata)
BSJP_PREFILTER_COUNT = 40  # berapa kandidat dari cache EOD yang di-live-check
BSJP_DAILY_HISTORY_LOOKBACK = 25  # buat hitung SMA5 & volume MA20 (butuh >=20 bar + buffer)

# MBSS v2 (user request — BSJP-ARA "pola GIAA"):
BSJP_ARA_MIN_MOMENTUM_PCT = 5.0   # harga hari ini vs open hari ini, minimal naik segini buat jadi kandidat
BSJP_ARA_MIN_VOLQ = 5.0           # volume hari ini vs avg 20hr, referensi GIAA 13x, ambang valid >5x


def compute_bsjp_obv(hist_daily) -> "pd.Series":
    """OBV = kumulatif volume (searah pergerakan harga day-over-day)."""
    close = hist_daily["Close"]
    vol = hist_daily["Volume"]
    direction = close.diff().apply(lambda d: 1 if d > 0 else (-1 if d < 0 else 0))
    return (direction * vol).cumsum()


async def compute_bsjp_structure_checklist(ticker: str, r: dict, hist_prior, current_price: float, volume_today_so_far: float) -> dict:
    """
    MBSS v2 (user request — diadopsi dari metodologi komunitas Threads: CMF>0,
    OBV naik, MACD golden cross, hindari volume spike-lalu-turun, konfirmasi
    multi-timeframe 15m/5m/1m searah). SENGAJA bukan kriteria wajib tambahan
    (6 kriteria asli MASIH satu-satunya yang wajib) — kalau ke-11 kriteria
    digabung jadi AND, hasil /bsjp nyaris pasti selalu kosong. Ini checklist
    KONFIRMASI terpisah, ditampilkan sebagai skor X/5.
    """
    checklist = []

    # 1. CMF > 0 (dari cache EOD kemarin — sudah dihitung compute_factor_scoring)
    cmf = r.get("cmf")
    checklist.append({"nama": "CMF > 0", "ok": cmf is not None and cmf > 0, "detail": f"{cmf:.2f}" if cmf is not None else "N/A"})

    # 2. OBV naik (5 hari terakhir dari histori harian, TIDAK termasuk hari ini
    # karena OBV butuh urutan closing yang stabil, bukan harga live yang masih bergerak)
    try:
        obv = compute_bsjp_obv(hist_prior.tail(10))
        obv_rising = len(obv) >= 5 and obv.iloc[-1] > obv.iloc[-5]
        checklist.append({"nama": "OBV naik (5hr)", "ok": obv_rising, "detail": f"{obv.iloc[-1]:,.0f} vs 5hr lalu {obv.iloc[-5]:,.0f}" if len(obv) >= 5 else "data kurang"})
    except Exception:
        checklist.append({"nama": "OBV naik (5hr)", "ok": False, "detail": "gagal dihitung"})

    # 3. MACD bullish (dari cache EOD kemarin)
    macd_state = r.get("macd_state")
    checklist.append({"nama": "MACD bullish", "ok": macd_state == "bullish", "detail": macd_state or "N/A"})

    # 4. HINDARI pola volume spike-lalu-turun: puncak volume 4 hari terakhir
    # (termasuk hari ini) TIDAK boleh terjadi 2+ hari lalu dengan tren turun sejak itu
    try:
        recent_vols = list(hist_prior["Volume"].tail(3)) + [volume_today_so_far]  # 3 hari lalu + hari ini
        peak_idx = recent_vols.index(max(recent_vols))
        is_spike_then_drop = peak_idx <= 1 and recent_vols[-1] < recent_vols[peak_idx] * 0.7  # puncak 2+ hari lalu, sudah turun signifikan
        checklist.append({"nama": "Bukan spike-lalu-turun", "ok": not is_spike_then_drop, "detail": f"vol 4hr: {[int(v) for v in recent_vols]}"})
    except Exception:
        checklist.append({"nama": "Bukan spike-lalu-turun", "ok": False, "detail": "gagal dihitung"})

    # 5. Multi-timeframe searah (15m dan 1m JUGA candle positif, selaras 5m yang
    # sudah dipakai di kriteria wajib) — INI YANG PALING MAHAL, 2 fetch tambahan per kandidat
    mtf_aligned = True
    mtf_detail = []
    for interval in ("15m", "1m"):
        try:
            bars_tf = await asyncio.to_thread(core.get_intraday_session_bars, ticker, interval, "1d")
            if bars_tf is None or bars_tf.empty:
                mtf_aligned = False
                mtf_detail.append(f"{interval}=N/A")
                continue
            tf_open = float(bars_tf["Open"].iloc[0])
            tf_close = float(bars_tf["Close"].iloc[-1])
            tf_positive = tf_close >= tf_open
            mtf_aligned = mtf_aligned and tf_positive
            mtf_detail.append(f"{interval}={'✅' if tf_positive else '❌'}")
        except Exception:
            mtf_aligned = False
            mtf_detail.append(f"{interval}=error")
    checklist.append({"nama": "Multi-timeframe searah (15m/5m/1m)", "ok": mtf_aligned, "detail": ", ".join(mtf_detail) + ", 5m=✅ (sudah dari kriteria wajib)"})

    met = sum(1 for c in checklist if c["ok"])
    return {"checklist": checklist, "met": met, "total": len(checklist)}


async def _run_bsjp_6criteria(update):
    """
    Logika 6 kriteria wajib BSJP asli — diekstrak jadi fungsi TERPISAH dari
    bsjp_screening_command() (MBSS v2, user request) supaya early-return di
    sini (cache kosong, tidak ada kandidat lolos, dst) TIDAK ikut
    menghentikan bagian BSJP-ARA yang jalan setelahnya di command utama.
    """
    scored = nightly_engine.load_daily_scan_cache()
    if not scored:
        await core.safe_reply(update.message, "⚠️ Cache /eodscan belum ada/basi — jalankan /eodscan dulu (dari kemarin sore, bukan hari ini).")
        return

    await core.safe_reply(update.message, f"🌆 Screening BSJP (formula diperketat) dari {len(scored)} kandidat cache, mengecek data live untuk ~{BSJP_PREFILTER_COUNT} teratas...")

    # Pra-filter murah: momentum positif dari cache kemarin (RS + di atas EMA9/SMA20)
    pre_candidates = [
        r for r in scored.values()
        if (r.get("relative_strength_vs_ihsg") or -999) > 0
        and r.get("high_conviction", {}).get("above_ma20_and_ma50")
    ]
    pre_candidates.sort(key=lambda r: r.get("relative_strength_vs_ihsg", 0), reverse=True)
    pre_candidates = pre_candidates[:BSJP_PREFILTER_COUNT]

    if not pre_candidates:
        await core.safe_reply(update.message, "📋 Tidak ada kandidat momentum positif dari cache kemarin untuk dicek live.")
        return

    results = []
    for r in pre_candidates:
        ticker = r["ticker"]
        prev_close = r.get("price")  # harga di cache KEMARIN = prev_close untuk hari ini

        try:
            bars = await asyncio.to_thread(core.get_intraday_session_bars, ticker, "5m", "1d")
        except Exception as e:
            print(f"⚠️ BSJP: gagal fetch intraday {ticker}: {e}")
            continue
        if bars is None or bars.empty:
            continue

        try:
            hist_daily = await asyncio.to_thread(core.get_ohlcv_smart, ticker, BSJP_DAILY_HISTORY_LOOKBACK)
        except Exception as e:
            print(f"⚠️ BSJP: gagal fetch histori harian {ticker}: {e}")
            continue
        if hist_daily is None or hist_daily.empty or len(hist_daily) < 20:
            continue  # butuh minimal 20 hari buat volume MA20 yang valid

        current_price = float(bars["Close"].iloc[-1])
        today_open = float(bars["Open"].iloc[0])
        today_high = float(bars["High"].max())
        today_low = float(bars["Low"].min())
        volume_today_so_far = float(bars["Volume"].sum())

        # Kriteria 1: harga naik >=5% dari kemarin
        gain_pct = (current_price / prev_close - 1) * 100 if prev_close else None
        c1_ok = gain_pct is not None and gain_pct >= BSJP_MIN_GAIN_PCT

        # Kriteria 2: volume vs MA20 dan vs kemarin (dari histori harian, TIDAK termasuk hari ini)
        hist_prior = hist_daily.iloc[:-1] if len(hist_daily) > 20 else hist_daily  # buang bar hari ini kalau kebetulan sudah ke-upsert
        vol_ma20 = hist_prior["Volume"].tail(20).mean()
        vol_yesterday = hist_prior["Volume"].iloc[-1]
        c2_ok = (volume_today_so_far >= vol_ma20 * BSJP_VOL_VS_MA20_MULTIPLIER
                 and volume_today_so_far >= vol_yesterday * BSJP_VOL_VS_PREV_MULTIPLIER)

        # Kriteria 3: harga >= SMA5 (dari histori harian, TIDAK termasuk hari ini — SMA5 kemarin ke belakang)
        sma5 = hist_prior["Close"].tail(5).mean()
        c3_ok = current_price >= sma5

        # Kriteria 4: candle hari ini positif
        c4_ok = current_price >= today_open

        # Kriteria 5: value traded (WAJIB sekarang)
        value_traded_today = float((bars["Volume"] * bars["Close"]).sum())
        c5_ok = value_traded_today >= BSJP_MIN_VALUE_TRADED_IDR

        # Kriteria 6: RSI (dari cache kemarin — indikator harian, wajar tidak direcompute intraday)
        rsi = r.get("rsi")
        c6_ok = rsi is not None and BSJP_RSI_MIN <= rsi <= BSJP_RSI_MAX

        wajib_lolos = c1_ok and c2_ok and c3_ok and c4_ok and c5_ok and c6_ok
        if not wajib_lolos:
            continue

        ara_distance = core.compute_ara_distance_pct(current_price, prev_close)
        close_pos = (current_price - today_low) / max(today_high - today_low, 1e-9)

        # Bandarmology — INFORMASI saja, tidak menggugurkan (data cuma ada utk sebagian kecil saham)
        akumulasi_note = ""
        cached_bs = broker_engine.get_cached_brokersum(ticker)
        if cached_bs:
            try:
                acc = broker_engine.assess_smart_accumulation(r, cached_bs)
                if acc["checklist"]:
                    akumulasi_note = f" | Akumulasi: {acc['criteria_met']}/{acc['criteria_checkable']}"
            except Exception:
                pass

        # Checklist konfirmasi struktur (TIDAK menggugurkan) — cuma dihitung
        # untuk kandidat yang SUDAH lolos 6 kriteria wajib, supaya multi-timeframe
        # fetch (paling mahal) tidak dilakukan untuk kandidat yang toh akan tersaring.
        try:
            structure = await compute_bsjp_structure_checklist(ticker, r, hist_prior, current_price, volume_today_so_far)
        except Exception as e:
            print(f"⚠️ BSJP: gagal hitung checklist struktur {ticker}: {e}")
            structure = {"checklist": [], "met": 0, "total": 0}

        results.append({
            "ticker": ticker, "current_price": current_price, "gain_pct": gain_pct,
            "vol_vs_ma20": round(volume_today_so_far / max(vol_ma20, 1e-9), 2),
            "vol_vs_prev": round(volume_today_so_far / max(vol_yesterday, 1e-9), 2),
            "sma5": round(sma5, 0), "value_traded_today": value_traded_today,
            "rsi": rsi, "ara_distance": ara_distance, "close_pos": close_pos,
            "akumulasi_note": akumulasi_note, "structure": structure,
            "targets": r.get("targets", {}), "action_label_id": r.get("action_label_id"),
        })

    if not results:
        await core.safe_reply(
            update.message,
            "📋 Tidak ada kandidat yang lolos SEMUA 6 kriteria wajib saat ini "
            "(formula diperketat — wajar kalau hasilnya sedikit/kosong, itu justru tujuannya)."
        )
        return

    results.sort(key=lambda r: r["gain_pct"], reverse=True)
    lines = [f"🌆 BSJP SCREENING (diperketat) — {len(results)} kandidat lolos SEMUA 6 kriteria wajib\n"]
    lines.append("⚠️ Ambang jarak ARA (0-15%) masih SEMENTARA/permisif — cuma informasi, belum jadi filter.")
    lines.append("⚠️ Checklist struktur (CMF/OBV/MACD/volume-shape/multi-timeframe) KONFIRMASI saja, TIDAK menggugurkan — belum cukup data buat dikunci jadi wajib.\n")
    for i, r in enumerate(results, 1):
        struct_str = f" | Struktur {r['structure']['met']}/{r['structure']['total']}" if r["structure"]["total"] else ""
        lines.append(
            f"{i}. {r['ticker']} — {r['current_price']:.0f} ({r['gain_pct']:+.1f}%){struct_str}\n"
            f"   Vol {r['vol_vs_ma20']}x MA20 | {r['vol_vs_prev']}x kemarin | SMA5 {r['sma5']:.0f}\n"
            f"   Value {r['value_traded_today']/1e9:.1f}M | RSI {r['rsi']} | Jarak ARA {r['ara_distance']}% | "
            f"Closing {r['close_pos']*100:.0f}% dari range{r['akumulasi_note']}"
        )
        for c in r["structure"]["checklist"]:
            icon = "✅" if c["ok"] else "➖"
            lines.append(f"   {icon} {c['nama']}: {c['detail']}")

    await core.safe_reply(update.message, "\n\n".join(lines))
    try:
        await asyncio.to_thread(core.lock_daily_daytrade_picks, results, "bsjp")
    except Exception as e:
        print(f"⚠️ Gagal mengunci picks /bsjp untuk /winrate: {e}")


async def bsjp_screening_command(update, context):
    """
    /bsjp — screening BSJP (Beli Sore Jual Pagi), varian momentum/continuation.
    WAJIB dijalankan menjelang closing (idealnya 15:50-16:00 WIB) — market
    HARUS masih buka supaya bisa fetch data live (baik intraday bar maupun
    histori harian terkini).

    REVISI (diperketat, berdasarkan riset formula komunitas + Stockbit):
    6 kriteria WAJIB semua lolos (AND, bukan mayoritas):
      1. Harga >= 1.05x previous close (naik >=5% dari kemarin)
      2. Volume hari ini >= 2x volume MA20 DAN >= 1x volume kemarin
      3. Harga >= SMA5
      4. Candle hari ini positif (harga >= open hari ini)
      5. Value traded >= Rp5 miliar (SEKARANG WAJIB, bukan peringatan lagi)
      6. RSI 60-85 (dipertahankan lebih ketat dari riset RSI>=50)
    Plus di atas EMA9/SMA20 dari pra-filter cache (dipertahankan dari desain awal).
    Jarak ke ARA & checklist akumulasi bandarmology (kalau ada cache broker)
    ditampilkan sebagai INFORMASI, tidak menggugurkan.
    """
    session = core.get_current_idx_session()
    if session is None:
        await core.safe_reply(
            update.message,
            "⚠️ /bsjp cuma berguna saat jam bursa (idealnya menjelang closing, 15:50-16:00 WIB, atau pra-penutupan) — "
            "di luar itu tidak ada data live untuk dicek."
        )
        return

    # MBSS v2 (user request — 2 pesan terpisah, independen): logika 6 kriteria
    # asli diekstrak ke fungsi TERPISAH supaya early-return di dalamnya (mis.
    # cache eodscan kosong, tidak ada kandidat lolos) TIDAK ikut menghentikan
    # bagian BSJP-ARA di bawahnya — dua metode harus tetap berjalan
    # independen, sesuai kesepakatan "berdampingan, bukan saling gantung".
    await _run_bsjp_6criteria(update)

    # ==========================================
    # 🌆 PESAN KE-2: BSJP-ARA — "pola GIAA" (MBSS v2, user request)
    # Baca cache yang SUDAH di-pre-filter + fetch berita semalam (bagian
    # /eodscan) — di sini TINGGAL cek live yang murah: momentum sejak open
    # hari ini, dan VolQ (harus SANGAT meledak, >5x, referensi GIAA 13x).
    # BERDAMPINGAN dengan 6 kriteria di atas, TIDAK saling menggantikan —
    # source terpisah di /winrate ("bsjp_ara") supaya bisa dibandingkan
    # akurasinya dari data nyata, bukan ditebak sekarang.
    # ==========================================
    ara_candidates = nightly_engine.load_bsjp_ara_candidates()
    if not ara_candidates:
        await core.safe_reply(
            update.message,
            "🌆 BSJP-ARA: tidak ada kandidat dari pre-filter semalam (harga<500, gerak kemarin datar, "
            "volume kemarin rendah) — atau /eodscan belum jalan dengan versi terbaru."
        )
        return

    ara_results = []
    scored_cache = nightly_engine.load_daily_scan_cache()  # dipakai buat reuse targets, di luar loop biar tidak reload tiap iterasi
    for c in ara_candidates:
        ticker = c["ticker"]
        try:
            bars = await asyncio.to_thread(core.get_intraday_session_bars, ticker, "5m", "1d")
        except Exception as e:
            print(f"⚠️ BSJP-ARA: gagal fetch live {ticker}: {e}")
            continue
        if bars is None or bars.empty:
            continue

        today_open = float(bars["Open"].iloc[0])
        current_price = float(bars["Close"].iloc[-1])
        momentum_pct = (current_price / today_open - 1) * 100 if today_open else 0

        if momentum_pct < BSJP_ARA_MIN_MOMENTUM_PCT:
            continue  # belum "naik kuat" sejak open, sesuai spesifikasi user — cek murah dulu sebelum fetch histori

        # VolQ: volume hari ini (live, sejauh sesi berjalan) vs rata-rata 20
        # hari — user minta ambang >5x (referensi GIAA 13x). Butuh histori
        # harian pendek buat vol_ma20 genuine (bukan sekadar re-use vol_ratio
        # KEMARIN yang skalanya beda) — fetch cuma untuk kandidat yang SUDAH
        # lolos momentum>=5% di atas, supaya biayanya terbatas.
        try:
            hist_short = await asyncio.to_thread(core.get_ohlcv_smart, ticker, 25)
        except Exception as e:
            print(f"⚠️ BSJP-ARA: gagal fetch histori volume {ticker}: {e}")
            continue
        if hist_short is None or hist_short.empty or len(hist_short) < 20:
            continue
        vol_ma20 = hist_short["Volume"].tail(20).mean()
        volume_today_so_far = float(bars["Volume"].sum())
        volq_ratio = volume_today_so_far / max(vol_ma20, 1e-9)
        if volq_ratio < BSJP_ARA_MIN_VOLQ:
            continue

        ara_distance = core.compute_ara_distance_pct(current_price, c.get("prev_close"))
        news_titles = [n["title"] for n in (c.get("news") or [])[:2]]

        eod_r = scored_cache.get(ticker, {})
        ara_results.append({
            "ticker": ticker, "current_price": current_price, "momentum_pct": momentum_pct,
            "today_open": today_open, "ara_distance": ara_distance, "volq_ratio": round(volq_ratio, 1),
            "sector": c.get("sector"), "news_titles": news_titles,
            "targets": eod_r.get("targets", {}), "action_label_id": eod_r.get("action_label_id"),
        })

    if not ara_results:
        await core.safe_reply(
            update.message,
            f"🌆 BSJP-ARA: {len(ara_candidates)} kandidat dari pre-filter semalam, "
            f"tapi belum ada yang naik ≥{BSJP_ARA_MIN_MOMENTUM_PCT:.0f}% sejak open DAN volume ≥{BSJP_ARA_MIN_VOLQ:.0f}x normal. Coba cek lagi nanti."
        )
        return

    ara_results.sort(key=lambda r: r["momentum_pct"], reverse=True)
    ara_lines = [f"🌆 BSJP-ARA — {len(ara_results)} kandidat naik ≥{BSJP_ARA_MIN_MOMENTUM_PCT:.0f}% sejak open & volume ≥{BSJP_ARA_MIN_VOLQ:.0f}x normal (dari {len(ara_candidates)} pre-filter semalam)\n"]
    ara_lines.append("⚠️ Metode TERPISAH dari 6 kriteria di atas — pola \"diam kemarin, meledak hari ini\" (referensi GIAA). Belum ada rekam jejak, pantau /winrate source=bsjp_ara.\n")
    for i, r in enumerate(ara_results, 1):
        news_str = f"\n   📰 {r['news_titles'][0]}" if r["news_titles"] else ""
        sector_str = market_engine.format_sector_tag(r.get("sector"))
        ara_lines.append(
            f"{i}. {r['ticker']} — {r['current_price']:.0f} (open {r['today_open']:.0f}, {r['momentum_pct']:+.1f}% sejak open)\n"
            f"   VolQ {r['volq_ratio']}x | Jarak ARA {r['ara_distance']}%{news_str}{sector_str}"
        )

    await core.safe_reply(update.message, "\n\n".join(ara_lines))
    try:
        await asyncio.to_thread(core.lock_daily_daytrade_picks, ara_results, "bsjp_ara")
    except Exception as e:
        print(f"⚠️ Gagal mengunci picks /bsjp_ara untuk /winrate: {e}")


async def strong_buy_command(update, context):
    """
    /strongbuy — SEMUA saham dengan action_id STRONG_BUY dari cache /eodscan,
    terlepas apakah mereka muncul di /screendaytrade, /gptpick, atau /hc atau
    tidak (MBSS v2, user request — screendaytrade/gptpick punya filter
    likuiditas/lane sendiri yang bisa "membuang" saham yang sebenarnya
    STRONG_BUY murni dari sisi decide_action()).

    Murni baca cache — instan, tidak fetch apa pun. Likuiditas (value_traded)
    ditampilkan sebagai INFORMASI, TIDAK memfilter — beda dari screendaytrade
    yang memang dirancang sekitar likuiditas untuk day-trade, /strongbuy ini
    tujuannya visibilitas penuh, keputusan tetap di tangan user.
    """
    scored = nightly_engine.load_daily_scan_cache()
    if not scored:
        await core.safe_reply(update.message, "⚠️ Cache /eodscan belum ada/basi — jalankan /eodscan dulu.")
        return

    candidates = [r for r in scored.values() if r.get("action_id") == "STRONG_BUY"]
    if not candidates:
        await core.safe_reply(update.message, "📋 Tidak ada saham STRONG_BUY di cache hari ini.")
        return

    candidates.sort(key=lambda r: r.get("scores", {}).get("final", 0), reverse=True)

    lines = [f"💪 SEMUA STRONG_BUY — {len(candidates)} saham (cache /eodscan, tidak difilter likuiditas/lane apa pun)\n"]
    for i, r in enumerate(candidates, 1):
        s = r.get("scores", {})
        t = r.get("targets", {})
        rr = t.get("risk_reward_at_max")
        rr_str = f"1:{rr:.2f}" if isinstance(rr, (int, float)) else "-"
        value_traded = r.get("value_traded")
        liq_note = f" | Value {value_traded/1e9:.1f}M" if isinstance(value_traded, (int, float)) else ""

        ceiling_str = ""
        cached_bs = broker_engine.get_cached_brokersum(r["ticker"])
        if cached_bs:
            ceiling = broker_engine.get_broker_entry_ceiling(cached_bs)
            if ceiling:
                ceiling_str = f" / {ceiling['avg_price']:.0f}*"

        lines.append(
            f"{i}. {r['ticker']} — Final {s.get('final', 0):.1f} "
            f"(Nilai {s.get('value', 0):.1f} | Momentum {s.get('momentum', 0):.1f} | Sentimen {s.get('sentiment', 0):.1f})\n"
            f"   RR {rr_str}{liq_note}\n"
            f"   Entry {t.get('buy_range', '-')}{ceiling_str}{market_engine.format_sector_tag(r.get('sector'))}"
        )

    lines.append("\nDetail lengkap: /check TICKER")
    await core.safe_reply(update.message, "\n\n".join(lines))


async def consensus_command(update, context):
    """
    /consensus — saham yang muncul sebagai kandidat POSITIF di >=2 dari 4
    lensa screening independen kita (MBSS v2, user request, tindak lanjut
    diskusi positioning /hc vs /screendaytrade): HIGH CONVICTION (pola
    chart), STRONG_BUY (verdict inti value/momentum/sentimen), SCREENDAYTRADE
    (lane timing entry), GPTPICK (shortlist momentum/likuiditas/RR).

    SEMUA murni dari cache /eodscan (TIDAK fetch live apa pun) — lane
    screendaytrade & skor gptpick dihitung ulang di sini dari data cache
    (keduanya EOD-computable, tidak butuh data live, sudah dikonfirmasi
    sebelumnya), jadi instan.

    Kandidat yang lolos dikirim ke Gemini untuk ringkasan naratif KENAPA
    beberapa lensa berbeda ini kompak, plus risiko dari data mentah —
    bukan Gemini yang menentukan lolos/tidak, itu murni deterministik
    Python duluan.
    """
    scored = nightly_engine.load_daily_scan_cache()
    if not scored:
        await core.safe_reply(update.message, "⚠️ Cache /eodscan belum ada/basi — jalankan /eodscan dulu.")
        return

    await core.safe_reply(update.message, f"🔗 Mencari konsensus lintas-tool dari {len(scored)} kandidat cache...")

    GOOD_SDT_LANES = {"PRIORITY FRESH", "PRIORITY CONT"}  # SECONDARY WATCH/LOW EDGE sengaja TIDAK dihitung (lihat revisi minggu lalu)
    GPTPICK_MIN_SCORE = 65  # kira-kira ambang bawah yang biasanya masuk top 3-5 nyata

    qualifying = []
    for r in scored.values():
        tools = []

        if r.get("high_conviction", {}).get("is_high_conviction"):
            tools.append("HIGH CONVICTION")

        if r.get("action_id") == "STRONG_BUY":
            tools.append("STRONG_BUY")

        try:
            bias = core.compute_screendaytrade_positive_bias(r)
            if bias.get("lane") in GOOD_SDT_LANES:
                tools.append(f"SCREENDAYTRADE ({bias['lane']})")
        except Exception:
            pass

        try:
            if _gptpick_candidate_filter(r):
                g = _gptpick_score(r)
                if g["final"] >= GPTPICK_MIN_SCORE:
                    tools.append(f"GPTPICK ({g['final']:.0f})")
        except Exception:
            pass

        if len(tools) >= 2:
            qualifying.append({**r, "_consensus_tools": tools})

    if not qualifying:
        await core.safe_reply(update.message, "📋 Tidak ada saham yang muncul di >=2 tool sekaligus hari ini.")
        return

    qualifying.sort(key=lambda r: (len(r["_consensus_tools"]), r.get("scores", {}).get("final", 0)), reverse=True)

    # Kunci ke winrate juga — source="consensus", supaya bisa diukur apakah
    # "beberapa tool setuju" genuinely lebih akurat dari 1 tool sendirian.
    try:
        await asyncio.to_thread(core.lock_daily_daytrade_picks, qualifying[:15], "consensus")
    except Exception as e:
        print(f"⚠️ Gagal mengunci picks /consensus untuk /winrate: {e}")

    # MBSS v2 (user request — gap yang sama seperti /hc: streak tersimpan
    # tapi tidak pernah ditampilkan real-time). Dihitung di sini, dikirim
    # ke Gemini SEBAGAI KONTEKS (boleh disebut di narasi), TAPI juga
    # ditambahkan sebagai baris deterministik terpisah setelahnya — supaya
    # tetap kelihatan pasti walau Gemini kebetulan tidak menyebutnya.
    history_for_streak = core.load_daytrade_picks_history()
    pick_date_today = core.get_current_trading_day_close_marker()
    streaks = {
        r["ticker"]: core.compute_consecutive_appearance_streak_any_source(r["ticker"], pick_date_today, history_for_streak)
        for r in qualifying[:15]
    }

    # Susun data ringkas buat Gemini (bukan seluruh field mentah scoring, biar fokus)
    gemini_input = [{
        "ticker": r["ticker"], "tools": r["_consensus_tools"],
        "final_score": r.get("scores", {}).get("final"),
        "action_id": r.get("action_id"), "rsi": r.get("rsi"), "adx": r.get("adx"), "cmf": r.get("cmf"),
        "risk_reward_at_max": r.get("targets", {}).get("risk_reward_at_max"),
        "is_overbought_caution": r.get("is_overbought_caution"), "obv_divergence": r.get("obv_divergence"),
        "is_financial_distress_flag": r.get("is_financial_distress_flag"),
        "entry": r.get("targets", {}).get("buy_range"),
        "consecutive_appearance_streak": streaks[r["ticker"]],
        "sector_strength": market_engine.get_sector_rank_info(r.get("sector")),
    } for r in qualifying[:15]]

    streak_lines = [f"🔁 {t}x — {tk}" for tk, t in streaks.items() if t > 1]
    streak_block = ("\n\nStreak kemunculan berturut-turut (lintas semua tool, bukan cuma /consensus):\n" + "\n".join(streak_lines)) if streak_lines else ""

    sector_lines = [f"{r['ticker']}{market_engine.format_sector_tag(r.get('sector'), prefix=' — ')}" for r in qualifying[:15] if market_engine.get_sector_rank_info(r.get("sector"))]
    sector_block = ("\n\nKekuatan sektor:\n" + "\n".join(sector_lines)) if sector_lines else ""

    try:
        summary_text = await asyncio.to_thread(core.ask_gemini_to_analyze, gemini_input, core.CONSENSUS_BRIEF_INSTRUCTION)
        await core.safe_reply(update.message, summary_text + streak_block + sector_block)
    except Exception as e:
        print(f"⚠️ Gemini consensus brief gagal: {e}")
        # Gagal-lunak — tetap kasih daftar mentah kalau Gemini error, jangan diam saja
        lines = [f"🔗 CONSENSUS (Gemini gagal, tampilan mentah) — {len(qualifying)} saham lolos >=2 tool\n"]
        for r in qualifying[:15]:
            lines.append(f"{r['ticker']} ({len(r['_consensus_tools'])} tool: {', '.join(r['_consensus_tools'])})")
        await core.safe_reply(update.message, "\n".join(lines))
