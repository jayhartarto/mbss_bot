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
import engine.backbone as backbone_engine


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

    Opsi "/screendaytrade rank" | "prob" | "danger" (AB-RC1 backbone, user
    request) — kandidat yang ditampilkan TETAP sama (lane/seleksi V5 tidak
    berubah), cuma urutan tampilannya diurutkan ulang berdasarkan angka
    backbone, buat cepat lihat mana yang paling bagus/aman di antara
    pilihan yang cukup banyak.
    """
    use_issi = len(context.args) > 0 and context.args[0].lower() == "issi"
    use_live = len(context.args) > 0 and context.args[0].lower() == "live"
    # AB-RC1 backbone (MBSS v2, user request — "sort by rank, probability,
    # danger"): "/screendaytrade rank|prob|danger" re-urutkan TAMPILAN
    # kandidat yang SAMA (tidak mengubah lane/seleksi V5 yang sudah ada)
    # berdasarkan angka backbone, biar cepat lihat mana yang paling
    # aman/mungkin di antara pilihan yang sudah banyak.
    sort_mode = context.args[0].lower() if len(context.args) > 0 and context.args[0].lower() in ("rank", "prob", "danger") else None
    # MBSS v2 (user request — tag smart money di semua tools): ini BACA CACHE
    # saja (broksum_250, sudah di-fetch tiap malam), TIDAK fetch Index Alpha
    # baru — jadi tetap konsisten dengan aturan lama "screendaytrade tidak
    # pakai kuota Index Alpha" (yang dijaga itu KUOTA baru, bukan baca cache).
    broksum_data = nightly_engine.load_broksum_250()

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

    # AB-RC1 backbone (MBSS v2, user backtest — lihat
    # backtest/MBSS_v317_AB_RC1_Final_Implementation_and_Research-1.md):
    # saring ke kandidat yang lolos Danger Gate malam ini SEBELUM V5
    # pre-select — Danger Gate menyaring risiko, logika lane/scoring V5 di
    # bawah TIDAK berubah. Fallback ke `results` mentah kalau backbone
    # belum pernah dihitung (misal /eodscan belum jalan lagi sejak fitur
    # ini di-deploy) — tidak pernah hard-block.
    backbone_result, backbone_staleness = nightly_engine.load_backbone_daily_allow_stale()
    results_before_gate = len(results)
    results = backbone_engine.filter_to_gate_survivors(results, backbone_result)
    if backbone_result:
        print(f"🧱 /screendaytrade: {len(results)}/{results_before_gate} lolos Danger Gate ({(backbone_result or {}).get('market_regime', '?')}).")
    if not results:
        await core.safe_reply(update.message, "⚠️ Tidak ada kandidat yang lolos Danger Gate malam ini. Coba lagi setelah /eodscan berikutnya.")
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
        top_candidates = core.rank_screendaytrade_refactor(live_pool, core.DAYTRADE_FINAL_PICKS_COUNT, (backbone_result or {}).get("market_regime"))
        if len(ready_pool) >= core.DAYTRADE_FINAL_PICKS_COUNT:
            filter_tier_note = filter_tier_note + " + Positive Bias lane refactor + live active breakout context"
        else:
            filter_tier_note = filter_tier_note + " + Positive Bias lane refactor (fallback karena kandidat READY terbatas)"

    if sort_mode:
        def _bb(r):
            return (backbone_result or {}).get("all_scored", {}).get(r["ticker"], {})
        if sort_mode == "rank":
            top_candidates.sort(key=lambda r: (_bb(r).get("entry_rank") is None, _bb(r).get("entry_rank") or 0))
        elif sort_mode == "prob":
            top_candidates.sort(key=lambda r: (_bb(r).get("probability_score") is not None, _bb(r).get("probability_score") or -1), reverse=True)
        else:  # danger
            top_candidates.sort(key=lambda r: (_bb(r).get("predicted_danger") is None, _bb(r).get("predicted_danger") or 0))
        filter_tier_note = filter_tier_note + f" — diurutkan ulang backbone: {sort_mode}"

    if use_live and not top_candidates:
        await core.safe_reply(
            update.message,
            "⚠️ Tidak ada saham dengan data live aktif saat ini — mode \"live\" cuma berguna "
            "saat jam bursa berlangsung. Coba /screendaytrade biasa untuk radar EOD."
        )
        return

    # Kunci picks hari ini untuk uji winrate — idempotent (tidak duplikat kalau
    # /screendaytrade dipanggil berkali-kali di hari yang sama).
    await asyncio.to_thread(
        core.lock_daily_daytrade_picks, top_candidates, "screendaytrade_live" if use_live else "screendaytrade",
        (backbone_result or {}).get("all_scored", {})
    )
    await asyncio.to_thread(core.save_latest_screendaytrade_picks, top_candidates)

    if use_live:
        # MBSS v2 (RapidAPI integration): warm the intraday checkpoint ONCE
        # here, in a background thread, before the loop below calls
        # format_market_mover_tag per candidate — that function does a
        # synchronous cache read, but the checkpoint refresh itself (an
        # HTTP call, only on the FIRST caller after 09:30/14:30 WIB passes)
        # must not happen inline inside the loop, or it'd block the event
        # loop for the duration of that one live request.
        await asyncio.to_thread(broker_engine.get_or_refresh_intraday_market_snapshot)

        lines = ["⚡ SCREENING DAY TRADE — AKTIF SEKARANG (live VWAP + vol pace)\n"]
        if backbone_staleness:
            lines.insert(0, backbone_staleness)
        lines.append(f"{filter_tier_note}\n")
        lines.append("Catatan: Diurutkan MURNI dari sinyal live (bukan lane EOD) — proxy terdekat ke \"orderbook condong ke buyer\" dari data yang tersedia (bot ini TIDAK punya akses order-book bid/ask asli). Ini RADAR, bukan entry final.\n")
        for i, r in enumerate(top_candidates, 1):
            ab = r["active_breakout"]
            label = ab.get("label", "-")
            wr = core.get_winrate_for_label(label)
            label_str = f"{label} (WR {wr})" if wr else label
            bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
            backbone_note = (
    f" | Entry Rank #{bb_info['entry_rank']}/{bb_info['entry_rank_total']} (prob {bb_info['probability_score']:.0f}, danger {bb_info['predicted_danger']:.0f})"
    if bb_info and "entry_rank" in bb_info else ""
)
            lines.append(
                f"{i}. {r['ticker']} — {label_str} ({ab.get('score', 0)}/100){backbone_note}\n"
                f"   Harga {r.get('price')} | VWAP {ab.get('vwap', '-')} (jarak {ab.get('vwap_distance_pct', '-')}%) | Vol pace {ab.get('volume_pace_ratio', '-')}x\n"
                f"   Trigger {ab.get('trigger_price', '-')} | Invalid <{ab.get('invalidation_level', '-')}\n"
                f"   {ab.get('notes', '') or '-'}{market_engine.format_sector_tag(r.get('sector'))}{broker_engine.format_smart_money_tag(r['ticker'], broksum_data)}{broker_engine.format_market_mover_tag(r['ticker'])}{nightly_engine.format_breakout_alert_tag(r['ticker'])}"
            )

        # MBSS v2 (user request — kasus TALF/IATA/SGRO): bagian TERPISAH untuk
        # lonjakan volume ekstrem mentah — lihat docstring detect_volume_spikes()
        # untuk kenapa ini tidak bisa digabung ke ranking di atas.
        spikes = detect_volume_spikes(live_pool, count=8)
        # MBSS v2 (user request — "/fastscan kalau dilakukan setelah
        # screendaytrade live, ambil LONJAKAN VOL EKSTREM sebagai tambahan
        # union"): simpan dengan timestamp supaya /fastscan bisa cek
        # kesegarannya nanti — spike 5m ini basi cepat, /fastscan HARUS
        # cek umur sebelum dipakai, bukan dipakai selamanya.
        await asyncio.to_thread(core.save_latest_live_volume_spikes, [r["ticker"] for r in spikes])
        if spikes:
            lines.append(f"\n🔥 LONJAKAN VOLUME EKSTREM (vol_ref ≥{VOLUME_SPIKE_THRESHOLD:.0f}x, di luar ranking di atas — bisa jadi belum \"rapi\" secara struktur, RISIKO LEBIH TINGGI)")
            for r in spikes:
                ab = r["active_breakout"]
                vol_ref = max(ab.get("last_bar_rvol") or 0, ab.get("volume_pace_ratio") or 0)
                lines.append(
                    f"  • {r['ticker']} — vol {vol_ref:.1f}x normal | Harga {r.get('price')} | "
                    f"vs VWAP {ab.get('vwap_distance_pct', '-')}% | {ab.get('label', '-')} ({ab.get('score', 0)}/100)"
                )

        buttons_live = core.build_check_buttons([r["ticker"] for r in top_candidates] + [r["ticker"] for r in spikes])
        await core.safe_reply(update.message, "\n\n".join(lines), reply_markup=buttons_live)
        return

    lines = ["⚡ SCREENING DAY TRADE - RADAR BREAKOUT V5 ACTIVITY\n"]
    if backbone_staleness:
        lines.insert(0, backbone_staleness)
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

        # MBSS v2 (user request — inline winrate per label): "Lane" ini
        # PERSIS field yang tersimpan sebagai signal_label buat winrate
        # (r["_positive_lane"], lihat lock_daily_daytrade_picks) — lookup
        # langsung pakai nilai yang sama, tidak ada celah mismatch.
        lane = r.get("_positive_lane", "-")
        wr = core.get_winrate_for_label(lane)
        lane_str = f"{lane} (WR {wr})" if wr else lane

        # AB-RC1 backbone (MBSS v2, user backtest) — angka UNIVERSAL yang
        # sama tampil di /hc & /consensus juga, biar konsisten lintas-tool.
        bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
        backbone_note = (
    f" | Entry Rank #{bb_info['entry_rank']}/{bb_info['entry_rank_total']} (prob {bb_info['probability_score']:.0f}, danger {bb_info['predicted_danger']:.0f})"
    if bb_info and "entry_rank" in bb_info else ""
)

        lines.append(
            f"{i}. {r['ticker']} — {v5['label']}\n"
            f"   Total {v5['total']}/100 | Bias {r.get('_positive_bias', '-')}/100 | Lane {lane_str} | B {br['score']} | C {cont['score']} | Act {v5['activity']['score']} | VolQ {volq['score']} | Room {room['score']} | Safety {risk['score']}{src_live}{backbone_note}\n"
            f"   Harga {r.get('price')} | Valid >{v5['valid_level']} | Ideal {v5['ideal']} | Invalid <{v5['invalid']}\n"
            f"   Room: {room['label']} ({room['dist_high_pct']}% ke high, upside TP1 {room['upside_tp1_pct']}%) | VolQ: {volq['label']} | Continuation: {cont['label']}\n"
            f"   Note: {v5['note']}{market_engine.format_sector_tag(r.get('sector'))}{broker_engine.format_smart_money_tag(r['ticker'], broksum_data)}{nightly_engine.format_breakout_alert_tag(r['ticker'])}"
            f"{core.format_fast_candidate_tag(r)}"
        )

    # MBSS v2 (user request — "ini harus keluar ke rekomendasi SDT",
    # positioning SDT = cari setup SEBELUM breakout vs HC = follow breakout
    # yang SUDAH terjadi): section TERPISAH, BUKAN dicampur ke ranking
    # FRESH/CONT di atas — kandidat pre-breakout secara struktural akan
    # gagal kriteria breakout/continuation lane (sama alasan PRIORITY
    # ACCUMULATION jadi lane sendiri). Sumber: SELURUH pool Danger Gate
    # survivor malam ini (`results`, bukan cuma top_candidates yang sudah
    # dipersempit V5), supaya tidak kehilangan kandidat yang secara
    # definisi belum breakout. Backtest OHLCV lokal saja (n=24.727, 3
    # iterasi — lihat compute_factor_scoring), BELUM ada histori /winrate
    # LIVE — dikunci TERPISAH (source="screendaytrade_macd_approach")
    # supaya validasi forward bisa mulai, TIDAK dicampur ke skor/lane
    # FRESH/CONT yang sudah tervalidasi live.
    macd_approach_candidates = [
        r for r in results
        if r.get("macd_approach_tier") in ("SWEET_SPOT", "SQUEEZE_RESCUE", "PULLBACK_RESUME")
    ]
    if macd_approach_candidates:
        # BUGFIX (user report, ditemukan lewat cek chart manual — real case
        # MNCN/BMTR/PTBA: urutan lama pakai probability_score backbone,
        # yang sama sekali TIDAK mengukur seberapa matang/dekat setup MACD
        # ini ke centerline — MNCN (cross 1 hari lalu, paling MENTAH) malah
        # tampil #1 karena probability_score-nya kebetulan tinggi, sementara
        # PTBA (cross 11 hari lalu, PALING DEKAT ke centerline saat dicek
        # manual di chart) malah tampil TERAKHIR). Urutan baru: tier dulu,
        # lalu KEMATANGAN spesifik-MACD di dalam tier:
        # - PULLBACK_RESUME: sinyal PALING SEGAR (signal-line cross HARI
        #   INI, bukan sedang berkembang seperti dua tier lain) dengan
        #   evidence win-rate tertinggi di antara ketiganya (backtest
        #   research_macd_cross_winner_profile_v2.py + research_macd_
        #   pullback_resume_threshold.py) -- diprioritaskan PALING ATAS.
        #   Di dalam tier ini, dist_to_sma50_pct makin besar makin
        #   diprioritaskan (quintile teratas = win-rate tertinggi).
        # - SWEET_SPOT: makin dekat ke bin puncak backtest (16-19 hari,
        #   tengah ~17) makin diprioritaskan -- lihat catatan riset di
        #   compute_factor_scoring.
        # - SQUEEZE_RESCUE: cross_days_ago PALING BESAR (paling lama sejak
        #   cross, dalam rentang 0-11) diprioritaskan duluan -- proxy
        #   "paling jauh sudah berkembang menuju centerline", PERSIS urutan
        #   yang cocok dengan cek manual user (PTBA 11 > BMTR 10 > MNCN 1).
        # probability_score backbone TETAP ditampilkan di tiap baris (info),
        # cuma bukan lagi kunci pengurutan section ini.
        SWEET_SPOT_PEAK_DAY = 17  # tengah bin 16-19 hari, bucket backtest terbaik (fwd10d +2.13%)
        def _macd_maturity_key(r):
            tier = r.get("macd_approach_tier")
            days = r.get("macd_cross_days_ago") or 0
            if tier == "PULLBACK_RESUME":
                return (0, -(r.get("dist_to_sma50_pct") or 0))
            if tier == "SWEET_SPOT":
                return (1, abs(days - SWEET_SPOT_PEAK_DAY))
            return (2, -days)  # SQUEEZE_RESCUE: days makin besar (0..11) makin diprioritaskan
        macd_approach_candidates.sort(key=_macd_maturity_key)
        macd_approach_candidates = macd_approach_candidates[:8]

        await asyncio.to_thread(
            core.lock_daily_daytrade_picks, macd_approach_candidates, "screendaytrade_macd_approach",
            (backbone_result or {}).get("all_scored", {})
        )

        lines.append(
            "\n📐 SETUP PRA-BREAKOUT — MACD approach (BACKTEST OHLCV lokal, BELUM ada histori /winrate live — watchlist, bukan entry final)"
        )
        MACD_TIER_LABELS = {"SWEET_SPOT": "SWEET SPOT", "SQUEEZE_RESCUE": "SQUEEZE RESCUE", "PULLBACK_RESUME": "PULLBACK RESUME"}
        for r in macd_approach_candidates:
            tier = r.get("macd_approach_tier")
            tier_label = MACD_TIER_LABELS.get(tier, tier)
            bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
            backbone_note = (
                f" | Entry Rank #{bb_info['entry_rank']}/{bb_info['entry_rank_total']} (prob {bb_info['probability_score']:.0f}, danger {bb_info['predicted_danger']:.0f})"
                if bb_info and "entry_rank" in bb_info else ""
            )
            if tier == "PULLBACK_RESUME":
                detail = f"cross bullish HARI INI, {r.get('dist_to_sma50_pct', '-')}% di atas SMA50"
            else:
                detail = f"cross bullish {r.get('macd_cross_days_ago', '-')} hari lalu" + (" + squeeze aktif" if tier == "SQUEEZE_RESCUE" else "")
            lines.append(
                f"  • {r['ticker']} — {tier_label}, {detail} | Harga {r.get('price')}{backbone_note}"
                f"{market_engine.format_sector_tag(r.get('sector'))}"
            )

    # MBSS v2 (user request — "cocoknya menggantikan EXPLOSIVE LANE... untuk
    # dimunculkan di SDT, jadi 2 picks terpisah": Pick #1 = pengganti
    # Explosive Lane (bobot gain tinggi), Pick #2 = pengganti SDT utama
    # (diurut probability win tertinggi). Keputusan user setelah quintile
    # sweep + uji Q1 (research_explosive_lane_v2_quintile_sweep.py +
    # research_explosive_q1_profile_test.py): CUTOFF Q2/Q3 (dead-zone,
    # ~6.5% explosive-rate — sudah di-reject di _explosive_score), Q1
    # dapat jatah TERBATAS 1-2 pick (edge real ~13% TAPI arah tidak bisa
    # diprediksi lebih lanjut dari MACD line/histogram, Cohen's d di dalam
    # Q1 semua kecil — TIDAK diranking halus, cuma tie-break probability_
    # score seadanya), Q4/Q5 (arah jelas) jadi pool utama buat KEDUA pick.
    explosive_candidates = []  # (score, tier, r)
    for r in results:
        score, rejected, _reason = _explosive_score(r, results)
        if rejected:
            continue
        tier = _macd_trend_clarity_tier(r, results)
        if tier is None:
            continue  # data trend-clarity tidak cukup -- exclude dari kedua pick, bukan ditebak
        explosive_candidates.append((score, tier, r))

    EXPLOSIVE_Q1_SLOTS = 2
    EXPLOSIVE_MAIN_SLOTS = 5  # MBSS v2 (user request): expand Q4/Q5 main slots 3->5 kalau memang ada kandidatnya, tetap urut skor explosive tertinggi dulu
    PROBABILITY_PICKS_COUNT = 5

    def _bb_prob(ticker):
        bb = (backbone_result or {}).get("all_scored", {}).get(ticker) if backbone_result else None
        return bb.get("probability_score", 0) if bb else 0

    q45_pool = [item for item in explosive_candidates if item[1] in ("Q4", "Q5")]
    q1_pool = [item for item in explosive_candidates if item[1] == "Q1"]
    q45_pool.sort(key=lambda item: (item[0], _bb_prob(item[2]["ticker"])), reverse=True)
    q1_pool.sort(key=lambda item: _bb_prob(item[2]["ticker"]), reverse=True)

    explosive_picks = q45_pool[:EXPLOSIVE_MAIN_SLOTS] + q1_pool[:EXPLOSIVE_Q1_SLOTS]
    probability_picks = sorted(q45_pool, key=lambda item: _bb_prob(item[2]["ticker"]), reverse=True)[:PROBABILITY_PICKS_COUNT]

    if explosive_picks:
        await asyncio.to_thread(
            core.lock_daily_daytrade_picks, [r for _, _, r in explosive_picks], "screendaytrade_explosive",
            (backbone_result or {}).get("all_scored", {})
        )
        lines.append(
            "\n🚀 EXPLOSIVE CANDIDATES — potensi gain besar (Pick #1, pengganti Explosive Lane. BACKTEST research_macd_explosive_gain_profile.py, BELUM ada histori /winrate live)"
        )
        for score, tier, r in explosive_picks:
            bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
            backbone_note = (
                f" | Entry Rank #{bb_info['entry_rank']}/{bb_info['entry_rank_total']} (prob {bb_info['probability_score']:.0f}, danger {bb_info['predicted_danger']:.0f})"
                if bb_info and "entry_rank" in bb_info else ""
            )
            tier_note = " ⚠️ Q1 WILDCARD (edge nyata ~13% tapi arah tidak terbaca dari MACD line/histogram)" if tier == "Q1" else f" [{tier}]"
            lines.append(
                f"  • {r['ticker']} — Explosive Score {score:.0f}/100{tier_note} | {r.get('dist_to_sma50_pct', '-')}% di atas SMA50, "
                f"day-range {r.get('day_range_pct_10d', '-')}% | Harga {r.get('price')}{backbone_note}"
                f"{market_engine.format_sector_tag(r.get('sector'))}"
            )

    if probability_picks:
        await asyncio.to_thread(
            core.lock_daily_daytrade_picks, [r for _, _, r in probability_picks], "screendaytrade_probability",
            (backbone_result or {}).get("all_scored", {})
        )
        lines.append(
            "\n🎯 HIGH PROBABILITY PICKS — diurut probability_score Backbone (Pick #2, pengganti radar SDT utama), pool MACD trend-clarity Q4/Q5 (arah jelas). BELUM ada histori /winrate live"
        )
        for _, _, r in probability_picks:
            bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
            backbone_note = (
                f" | Entry Rank #{bb_info['entry_rank']}/{bb_info['entry_rank_total']} (prob {bb_info['probability_score']:.0f}, danger {bb_info['predicted_danger']:.0f})"
                if bb_info and "entry_rank" in bb_info else ""
            )
            lines.append(
                f"  • {r['ticker']} — Harga {r.get('price')}{backbone_note}"
                f"{market_engine.format_sector_tag(r.get('sector'))}"
            )

    buttons = core.build_check_buttons(
        [r["ticker"] for r in top_candidates] + [r["ticker"] for r in macd_approach_candidates]
        + [r["ticker"] for _, _, r in explosive_picks] + [r["ticker"] for _, _, r in probability_picks]
    )
    await core.safe_reply(update.message, "\n\n".join(lines), reply_markup=buttons)

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

    # MBSS v2 (RapidAPI integration, "diskusi trader" session, user request):
    # bonus conviction kecil kalau ticker JUGA lolos multibagger scan
    # (>=75) — gptpick fokus jangka pendek, multibagger fokus 6-12 bulan;
    # kalau keduanya kompak, itu sinyal momentum jangka pendek DIDUKUNG
    # kualitas struktural jangka panjang (foreign accumulation berturut-
    # turut, harga masih jauh dari 52w high), bukan sekadar volatilitas
    # sesaat. Bounded (+1.5 dari maks 10), tidak menentukan sendirian.
    multibagger = None
    ticker_for_mb = scoring.get("ticker")
    if ticker_for_mb:
        try:
            multibagger = nightly_engine.get_multibagger_candidate_for_ticker(ticker_for_mb)
        except Exception:
            multibagger = None
    if multibagger and (multibagger.get("multibagger_score") or 0) >= 75:
        conviction = min(10.0, conviction + 1.5)

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
    if multibagger and (multibagger.get("multibagger_score") or 0) >= 75:
        reasons.append(f"juga lolos multibagger scan ({multibagger['multibagger_score']}/100, {multibagger.get('potential_return', '-')})")

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
    # BUGFIX (user report — "selaraskan format SDT", ditemukan saat menelusuri
    # kenapa /hc & /gptpick bisa menampilkan WR beda dari /screendaytrade):
    # scoring.get("_positive_lane") SELALU None di sini -- field itu cuma
    # diisi rank_screendaytrade_refactor() (khusus /screendaytrade), TIDAK
    # PERNAH lewat pipeline GPTPICK (fetch_tickers_scored_with_cache). Baris
    # ini jadi dead code sejak ditulis -- wr_note nyaris selalu kosong.
    # Dihitung langsung di sini (REUSE compute_screendaytrade_positive_bias,
    # sama seperti /hc & /consensus), bukan lagi bergantung field yang tidak
    # pernah terisi.
    try:
        lane = core.compute_screendaytrade_positive_bias(scoring).get("lane")
    except Exception:
        lane = None
    wr = core.get_winrate_for_label(lane) if lane else ""
    wr_note = f"\n  📊 Lane {lane} (WR {wr})" if wr else (f"\n  📊 Lane {lane}" if lane else "")

    return (
        f"{scoring.get('ticker', '-')}: {g.get('bucket', '-') } {g.get('final', 0):.1f}/100 | {g.get('confidence', '-')}\n"
        f"  LQ {g.get('liquidity', 0):.1f} | DT {g.get('daytrade', 0):.1f} | RS {g.get('rs', 0):.1f} | RR {g.get('rr', 0):.1f} | FLOW {g.get('flow', 0):.1f}\n"
        f"  Buy {buy} | TP1 {tp1} | SL {sl} | RR@max {rr_text}"
        f"{rr_warning}\n"
        f"  {', '.join(g.get('reasons', [])) if g.get('reasons') else '—'}"
        f"{wr_note}"
        f"{broker_engine.format_smart_money_tag(scoring.get('ticker', ''), nightly_engine.load_broksum_250(), prefix=chr(10) + '  ')}"
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

    keyboard_rows = [
        [
            InlineKeyboardButton("Top 3", callback_data="gptpick:3"),
            InlineKeyboardButton("Top 5", callback_data="gptpick:5"),
        ]
    ]
    # MBSS v2 (user request — tombol cek di semua tools): gabungkan DENGAN
    # keyboard Top 3/Top 5 yang sudah ada, bukan menimpanya — Telegram cuma
    # terima 1 reply_markup per pesan.
    check_markup = core.build_check_buttons([r["ticker"] for r in picks])
    if check_markup:
        keyboard_rows.extend(check_markup.inline_keyboard)
    keyboard = InlineKeyboardMarkup(keyboard_rows)

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

    /hc rank | /hc prob | /hc danger — urutkan berdasarkan angka backbone
    AB-RC1 (entry_rank/probability_score/predicted_danger, sama yang
    ditampilkan di tiap baris) — user request, buat cepat lihat mana yang
    paling bagus/aman di antara kandidat HIGH CONVICTION yang cukup banyak.

    Murni baca cache (nightly_engine.load_daily_scan_cache) — TIDAK fetch
    apa pun, jadi instan. is_high_conviction sudah dihitung penuh saat
    /eodscan (7-kriteria Minervini/IBD-style breakout check), tinggal
    filter+urutkan di sini.
    """
    sort_mode = context.args[0].lower() if len(context.args) > 0 else None  # None (default), "rr", "rank", "prob", "danger"

    # MBSS v2 (user request): tampilkan data LAMA (dari scan malam
    # sebelumnya, atau formula versi lama) dengan keterangan jelas,
    # daripada menolak total — lebih berguna lihat pick kemarin yang
    # ditandai basi daripada tidak lihat apa-apa. staleness_note None
    # kalau cache genuinely masih current hari ini.
    scored, staleness_note = nightly_engine.load_daily_scan_cache_allow_stale()
    if not scored:
        await core.safe_reply(
            update.message,
            "⚠️ Cache /eodscan belum pernah ada — jalankan /eodscan dulu."
        )
        return

    # AB-RC1 backbone (MBSS v2, user backtest) — saring ke kandidat yang
    # lolos Danger Gate malam ini SEBELUM kriteria HIGH CONVICTION di bawah.
    # Fallback ke `scored` mentah kalau backbone belum pernah dihitung.
    backbone_result, backbone_staleness = nightly_engine.load_backbone_daily_allow_stale()
    if backbone_staleness:
        staleness_note = f"{staleness_note}\n{backbone_staleness}" if staleness_note else backbone_staleness
    pool = backbone_engine.filter_to_gate_survivors(list(scored.values()), backbone_result)
    scored = {r["ticker"]: r for r in pool}

    # MBSS v2 (user request — "HC dan SDT justru butuh regime aware"):
    # recheck regime-scaled (HC_MET_FRACTION_BY_REGIME, engine/scoring.py)
    # di atas is_high_conviction mentah — regime lebih ketat dari R1 butuh
    # criteria_met lebih tinggi dari fraction 0.70 lama.
    hc_market_regime = (backbone_result or {}).get("market_regime")
    candidates = [r for r in scored.values() if scoring_engine.is_high_conviction_regime_aware(r, hc_market_regime)]
    if not candidates:
        await core.safe_reply(update.message, "📋 Tidak ada saham HIGH CONVICTION di cache hari ini.")
        return

    if sort_mode == "rr":
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
    elif sort_mode in ("rank", "prob", "danger"):
        # AB-RC1 backbone (MBSS v2, user request — "sort by rank, probability,
        # danger"): urut ulang kandidat HC berdasarkan angka backbone yang
        # SAMA dipakai di /screendaytrade/consensus/check, bukan cuma pakai
        # compute_daytrade_score. Ticker tanpa data backbone (di luar cakupan
        # /eodscan malam ini) ditaruh PALING BELAKANG, bukan dibuang.
        def _bb(r):
            return (backbone_result or {}).get("all_scored", {}).get(r["ticker"], {})
        if sort_mode == "rank":
            candidates.sort(key=lambda r: (_bb(r).get("entry_rank") is None, _bb(r).get("entry_rank") or 0))
            sort_label = "Entry Rank backbone (terbaik dulu)"
        elif sort_mode == "prob":
            candidates.sort(key=lambda r: (_bb(r).get("probability_score") is not None, _bb(r).get("probability_score") or -1), reverse=True)
            sort_label = "probability backbone tertinggi"
        else:  # danger
            candidates.sort(key=lambda r: (_bb(r).get("predicted_danger") is None, _bb(r).get("predicted_danger") or 0))
            sort_label = "danger backbone terendah (paling aman dulu)"
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
    broksum_data = nightly_engine.load_broksum_250()  # dimuat sekali di luar loop, murni baca cache (tidak fetch)

    # MBSS v2 (user request — ditemukan lewat penelusuran manual /winrate: /hc
    # TIDAK PERNAH terlacak sama sekali sebelumnya). Kunci lewat mekanisme
    # yang SAMA dengan screendaytrade/gptpick/testbrief, source="hc" —
    # dipakai untuk KEDUA mode urutan (final/rr), karena kriteria eligibility
    # dasarnya (is_high_conviction) sama, cuma urutan tampilan yang beda.
    # Gagal-lunak — kalau lock gagal, tetap tampilkan hasil seperti biasa.
    try:
        await asyncio.to_thread(core.lock_daily_daytrade_picks, top10, "hc", (backbone_result or {}).get("all_scored", {}))
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
    if staleness_note:
        lines.insert(0, staleness_note)
    for i, r in enumerate(top10, 1):
        s = r.get("scores", {})
        hc = r.get("high_conviction", {})
        t = r.get("targets", {})
        rr = t.get("risk_reward_at_max")
        rr_str = f"1:{rr:.2f}" if isinstance(rr, (int, float)) else "-"

        # Ceiling asterisk — SEKARANG prioritaskan broksum_250 (otomatis,
        # cakupan 250 saham), jatuh ke screenshot manual kalau di luar
        # cakupan itu (MBSS v2, user request).
        ceiling_str = ""
        ceiling = broker_engine.get_best_available_ceiling(r["ticker"], broksum_data)
        if ceiling:
            ceiling_str = f" / {ceiling['avg_price']:.0f}*"

        streak_any = core.compute_consecutive_appearance_streak_any_source(r["ticker"], pick_date_today, history_for_streak)
        streak_hc = core.compute_consecutive_appearance_streak(r["ticker"], "hc", pick_date_today, history_for_streak)
        streak_str = f" 🔁 {streak_any}x berturut-turut (lintas-tool)" if streak_any > 1 else ""
        if streak_hc > 1 and streak_hc != streak_any:
            streak_str += f", {streak_hc}x khusus /hc"

        daytrade_note = f" | DT {r['_daytrade_score_hc']:.1f}" if "_daytrade_score_hc" in r else ""

        # AB-RC1 backbone (MBSS v2, user backtest) — rank/skor UNIVERSAL yang
        # sama dipakai buat filter gate DAN ditampilkan di /screendaytrade,
        # /consensus — supaya angkanya konsisten dilihat di semua tool, bukan
        # cuma dipakai diam-diam buat filter (user request eksplisit).
        bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
        backbone_note = (
    f" | Entry Rank #{bb_info['entry_rank']}/{bb_info['entry_rank_total']} (prob {bb_info['probability_score']:.0f}, danger {bb_info['predicted_danger']:.0f})"
    if bb_info and "entry_rank" in bb_info else ""
)

        sector_note = ""
        sector_info = market_engine.get_sector_rank_info(r.get("sector"))
        if sector_info:
            sector_note = f"\n   🏭 Sektor {sector_info['sector']}: #{sector_info['rank']}/{sector_info['total_sectors']} terkuat ({sector_info['avg_return_pct']:+.1f}% avg)"
        smart_money_note = broker_engine.format_smart_money_tag(r["ticker"], broksum_data)
        breakout_alert_note = nightly_engine.format_breakout_alert_tag(r["ticker"])

        # MBSS v2 (user request — inline winrate per label, supaya tidak perlu
        # recall/cross-reference manual): tampilkan angka winrate historis
        # PERSIS untuk label yang sedang ditunjukkan di sini (action_label_id).
        label = r.get("action_label_id", "-")
        wr = core.get_winrate_for_label(label)
        label_str = f"{label} (winrate {wr})" if wr else label

        # MBSS v2 (user request — "selaraskan, ikuti format SDT": EXCL nyata
        # menampilkan "SINYAL CAMPURAN (winrate 55%)" di /hc TAPI "EXTENDED /
        # CHASE WATCH (WR 74%)" di /screendaytrade untuk ticker yang SAMA,
        # user bingung ini kontradiksi atau bukan). action_label_id (di atas)
        # dan lane SDT adalah DUA LENSA BEDA (skor blend Value/Momentum/
        # Sentimen vs klasifikasi momentum day-trade) — bukan bug, tapi
        # supaya konsisten & tidak membingungkan, /hc SEKARANG JUGA
        # menampilkan lane SDT + WR-nya sendiri, format PERSIS sama dengan
        # /screendaytrade dan /gptpick (lihat _gptpick_format_item), bukan
        # cuma satu sisi yang kelihatan.
        try:
            sdt_lane = core.compute_screendaytrade_positive_bias(r, hc_market_regime).get("lane")
        except Exception:
            sdt_lane = None
        sdt_wr = core.get_winrate_for_label(sdt_lane) if sdt_lane else ""
        sdt_lane_note = f"\n   📊 Lane {sdt_lane} (WR {sdt_wr})" if sdt_lane and sdt_wr else (f"\n   📊 Lane {sdt_lane}" if sdt_lane else "")

        # MBSS v2 (user request — "HC boleh pakai data dari SDT dari study
        # macd... bisa masuk HC untuk diproduksi sebagai sedang breaking
        # ataupun continuation, ataupun watch for pullback"): 3-state
        # lifecycle (macd_lifecycle_state, engine/scoring.py), GANTI dari
        # note flat macd_fresh_breakout_confirmed sebelumnya. Cross-
        # reference ke tag SDT sebelumnya (find_recent_sdt_macd_tag)
        # berlaku utk BREAKING & CONTINUATION (dua-duanya bisa jadi
        # kelanjutan dari kandidat yang SDT tandai lebih dulu).
        macd_lifecycle_note = ""
        lifecycle = r.get("macd_lifecycle_state")
        if lifecycle == "BREAKING":
            macd_lifecycle_note = (
                f"\n   📈 MACD BREAKING (BACKTEST): centerline cross hari ini, "
                f"{r.get('macd_cross_days_ago', '-')} hari dari signal cross awal — follow-through terbaik secara historis"
            )
        elif lifecycle == "CONTINUATION":
            macd_lifecycle_note = "\n   📊 MACD CONTINUATION: sudah di atas centerline, momentum terjaga"
        elif lifecycle == "WATCH_PULLBACK":
            macd_lifecycle_note = "\n   ⏳ MACD WATCH PULLBACK: masih di atas centerline tapi histogram melemah 3 hari terakhir — pertimbangkan tunggu pullback"
        if lifecycle in ("BREAKING", "CONTINUATION"):
            sdt_prior = core.find_recent_sdt_macd_tag(r["ticker"], pick_date_today, history_for_streak)
            if sdt_prior:
                macd_lifecycle_note += (
                    f"\n   🔗 SDT sudah tandai {sdt_prior['tier']} {sdt_prior['days_gap']} hari lalu — sekarang HC konfirmasi lanjutannya"
                )

        lines.append(
            f"{i}. {r['ticker']} — Final {s.get('final', 0):.1f}{daytrade_note}{streak_str} "
            f"(Nilai {s.get('value', 0):.1f} | Momentum {s.get('momentum', 0):.1f} | Sentimen {s.get('sentiment', 0):.1f})\n"
            f"   {hc.get('criteria_met', 0)}/{hc.get('criteria_checkable', 0)} kriteria | "
            f"RR {rr_str} | {label_str}{backbone_note}{sdt_lane_note}{macd_lifecycle_note}\n"
            f"   Entry {t.get('buy_range', '-')}{ceiling_str}{sector_note}{smart_money_note}{breakout_alert_note}"
            f"{core.format_fast_candidate_tag(r)}"
        )

    # MBSS v2 (RapidAPI integration, "diskusi trader" session, user request):
    # blok TAMBAHAN, independen dari 8-kriteria HC di atas (yang secara
    # struktural mensyaratkan breakout SUDAH terjadi — konfirmasi close,
    # body candle kuat, volume tinggi) — saham dengan whitelist broker
    # net-buy kuat TAPI belum breakout tidak akan pernah lolos kriteria HC
    # manapun, jadi selalu terlewat sampai SETELAH ramai. Sinyal sudah
    # dihitung SEKALI di batch malam (apply_whitelist_accumulation_adjustment),
    # di sini tinggal baca + filter, tidak fetch/hitung ulang apa pun. Sama
    # seperti pola "2 hal independen, jangan saling menghentikan" yang sudah
    # dipakai di /consensus.
    hc_tickers = {r["ticker"] for r in top10}
    accumulation_candidates = [
        r for r in scored.values()
        if r.get("ticker") not in hc_tickers
        and r.get("whitelist_accumulation_net_pct") is not None
        and r["whitelist_accumulation_net_pct"] >= 15
        and (r.get("whitelist_num_brokers") or 0) >= 2
    ]
    accumulation_candidates.sort(key=lambda r: r["whitelist_accumulation_net_pct"], reverse=True)
    accumulation_candidates = accumulation_candidates[:5]

    if accumulation_candidates:
        # MBSS v2 (RapidAPI integration, user request — "confidence breakout
        # N hari, dari data historis kita sendiri"): dihitung dari track
        # record /winrate LANE "PRIORITY ACCUMULATION" itu sendiri (median
        # hari kalender dari pick sampai TP), bukan klaim tebakan — kosong
        # kalau sampelnya masih <3 (lane ini baru, wajar belum ada datanya).
        days_note = core.get_days_to_breakout_for_label("PRIORITY ACCUMULATION")
        days_str = f" | historis: {days_note}" if days_note else " | belum cukup data historis untuk estimasi hari"
        lines.append(f"\n🔍 AKUMULASI / PRA-BREAKOUT — {len(accumulation_candidates)} kandidat (belum breakout, whitelist broker net-buy kuat){days_str}\n")
        for i, r in enumerate(accumulation_candidates, 1):
            s = r.get("scores", {})
            lines.append(
                f"{i}. {r['ticker']} — Final {s.get('final', 0):.1f} | "
                f"net-buy whitelist {r['whitelist_accumulation_net_pct']:+.0f}% ({r.get('whitelist_num_brokers', 0)} broker)"
            )
        lines.append("⚠️ Belum ada konfirmasi harga — risiko timing lebih tinggi dari kandidat HC di atas.")

    # MBSS v2 (user request — "rapihkan HC, banyak section": section SQUEEZE
    # / PRA-BREAKOUT lama DIHAPUS -- REDUNDAN sejak SDT punya macd_approach_
    # tier="SQUEEZE_RESCUE" (engine/scoring.py) yang mensyaratkan bollinger_
    # squeeze DENGAN backtest kualitas jauh lebih kuat (n=24.727 ticker-hari,
    # dikombinasi cross_days_ago) dibanding section ini yang cuma filter
    # bollinger_squeeze mentah tanpa validasi forward apa pun. Pointer
    # singkat ke situ, bukan duplikasi coverage di dua tempat.
    lines.append("\nKandidat squeeze pra-breakout (backtest tervalidasi): lihat /screendaytrade, lane SQUEEZE RESCUE.")
    lines.append("Detail lengkap: /check TICKER")
    all_tickers = [r["ticker"] for r in top10] + [r["ticker"] for r in accumulation_candidates]
    buttons = core.build_check_buttons(all_tickers)
    await core.safe_reply(update.message, "\n\n".join(lines), reply_markup=buttons)




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

# MBSS v2 (user request, dari riset /finance — "buy the dip DALAM uptrend"
# terbukti (literatur umum + IDX-specific findings.md) lebih baik dari
# "bottom fishing oversold murni". Lane BARU, BERDAMPINGAN dengan 6
# kriteria momentum di atas, arah BEDA: cari saham yang MASIH uptrend
# jangka pendek TAPI baru selesai pullback sehat & reclaim EMA9 — bukan
# cari yang lagi kencang naik. Ambang REUSE dari findings.md's
# MOMENTUM_PULLBACK (IDX-validated, 63.0% win, n=671), BUKAN dikarang
# baru): "above SMA50, 20d ROC>5%, dip lalu reclaim EMA9".
BSJP_PULLBACK_MIN_ROC_20D = 5.0  # PERSIS threshold MOMENTUM_PULLBACK di findings.md
BSJP_PULLBACK_DIP_LOOKBACK_DAYS = 3  # placeholder -- findings.md tidak spesifikkan window pastinya, perlu revalidasi forward

# MBSS v2 (user request — BSJP-ARA "pola GIAA"):
BSJP_ARA_MIN_MOMENTUM_PCT = 5.0   # harga hari ini vs open hari ini, minimal naik segini buat jadi kandidat
BSJP_ARA_MIN_VOLQ = 3.0           # direvisi dari 5.0 (user request) — lebih banyak kandidat, referensi GIAA tetap 13x jauh di atas ini


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

        # MBSS v2 (RapidAPI integration, "diskusi trader" session, user
        # request): konfirmasi breakout dari nightly sweep — INFORMASI saja,
        # tidak menggugurkan, sama seperti akumulasi_note di atas.
        try:
            alert = nightly_engine.get_breakout_alert_for_ticker(ticker)
        except Exception:
            alert = None
        if alert and str(alert.get("severity", "")).upper() == "HIGH":
            breakout_prob = (alert.get("indicators") or {}).get("breakout_probability", "-")
            akumulasi_note += f" | Breakout {breakout_prob}%"

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
            # MBSS v2 (user request — "review /bsjp apakah perlu optimasi
            # dengan adanya data /fastscan"): cross-reference tag FAST EOD,
            # murni informasional (BUKAN filter/threshold baru -- n=3 pick
            # /bsjp masih terlalu kecil buat ubah formula apa pun).
            "fast_tag_note": core.format_fast_candidate_tag(r),
        })

    if not results:
        await core.safe_reply(
            update.message,
            "📋 Tidak ada kandidat yang lolos SEMUA 6 kriteria wajib saat ini "
            "(formula diperketat — wajar kalau hasilnya sedikit/kosong, itu justru tujuannya)."
        )
        return

    results.sort(key=lambda r: r["gain_pct"], reverse=True)

    # MBSS v2 (RapidAPI integration): warm the intraday checkpoint ONCE
    # here (background thread) before the loop below calls
    # format_market_mover_tag per candidate — /bsjp runs near close
    # (15:50-16:00 WIB), exactly the pre-close checkpoint window this
    # mechanism was built for.
    await asyncio.to_thread(broker_engine.get_or_refresh_intraday_market_snapshot)

    lines = [f"🚀 BSJP MOMENTUM (diperketat) — {len(results)} kandidat lolos SEMUA 6 kriteria wajib\n"]
    lines.append("⚠️ Ambang jarak ARA (0-15%) masih SEMENTARA/permisif — cuma informasi, belum jadi filter.")
    lines.append("⚠️ Checklist struktur (CMF/OBV/MACD/volume-shape/multi-timeframe) KONFIRMASI saja, TIDAK menggugurkan — belum cukup data buat dikunci jadi wajib.\n")
    for i, r in enumerate(results, 1):
        struct_str = f" | Struktur {r['structure']['met']}/{r['structure']['total']}" if r["structure"]["total"] else ""
        label = r.get("action_label_id")
        wr = core.get_winrate_for_label(label) if label else ""
        wr_note = f" | WR {wr}" if wr else ""
        lines.append(
            f"{i}. {r['ticker']} — {r['current_price']:.0f} ({r['gain_pct']:+.1f}%){struct_str}{wr_note}\n"
            f"   Vol {r['vol_vs_ma20']}x MA20 | {r['vol_vs_prev']}x kemarin | SMA5 {r['sma5']:.0f}\n"
            f"   Value {r['value_traded_today']/1e9:.1f}M | RSI {r['rsi']} | Jarak ARA {r['ara_distance']}% | "
            f"Closing {r['close_pos']*100:.0f}% dari range{r['akumulasi_note']}{broker_engine.format_market_mover_tag(r['ticker'])}"
            f"{r.get('fast_tag_note', '')}"
        )
        for c in r["structure"]["checklist"]:
            icon = "✅" if c["ok"] else "➖"
            lines.append(f"   {icon} {c['nama']}: {c['detail']}")

    buttons = core.build_check_buttons([r["ticker"] for r in results])
    await core.safe_reply(update.message, "\n\n".join(lines), reply_markup=buttons)
    try:
        await asyncio.to_thread(core.lock_daily_daytrade_picks, results, "bsjp")
    except Exception as e:
        print(f"⚠️ Gagal mengunci picks /bsjp untuk /winrate: {e}")


async def _run_bsjp_pullback(update):
    """
    BSJP PULLBACK lane (MBSS v2, user request — "satu command, 2 tagging").
    Arah BEDA dari _run_bsjp_6criteria (lane MOMENTUM, chase kekuatan hari
    ini): lane ini cari saham yang MASIH uptrend jangka pendek TAPI baru
    saja selesai pullback SEHAT dan reclaim EMA9 — bukan yang lagi kencang
    naik. Dasar riset /finance: literatur umum (buy-the-dip DALAM uptrend
    > bottom-fishing oversold murni, risiko "dead cat bounce" jauh lebih
    rendah) + findings.md's MOMENTUM_PULLBACK (IDX-specific, independent
    backtest, 63.0% win n=671) — kriteria REUSE definisi itu, bukan
    dikarang baru.

    BERDAMPINGAN dengan lane momentum & BSJP-ARA — TIDAK saling
    menggantikan/menggagalkan, source /winrate terpisah ("bsjp_pullback").
    """
    scored = nightly_engine.load_daily_scan_cache()
    if not scored:
        return  # pesan error sudah ditampilkan _run_bsjp_6criteria, tidak perlu diulang

    # Pra-filter murah: masih di atas SMA50 (uptrend jangka pendek). SENGAJA
    # tidak filter EMA9 di sini -- justru butuh yang SEMPAT di bawah EMA9,
    # itu dicek di loop live pakai histori harian.
    pre_candidates = [r for r in scored.values() if r.get("is_below_sma50") is False]
    pre_candidates.sort(key=lambda r: r.get("relative_strength_vs_ihsg", 0), reverse=True)
    pre_candidates = pre_candidates[:BSJP_PREFILTER_COUNT]
    if not pre_candidates:
        return

    results = []
    for r in pre_candidates:
        ticker = r["ticker"]
        try:
            hist_daily = await asyncio.to_thread(core.get_ohlcv_smart, ticker, BSJP_DAILY_HISTORY_LOOKBACK)
        except Exception as e:
            print(f"⚠️ BSJP Pullback: gagal fetch histori harian {ticker}: {e}")
            continue
        if hist_daily is None or hist_daily.empty or len(hist_daily) < 25:
            continue

        closes = hist_daily["Close"]
        current_price = float(closes.iloc[-1])

        # 20d ROC -- konfirmasi uptrend jangka pendek MASIH genuinely ada,
        # bukan cuma kebetulan di atas SMA50 tapi sudah datar/melemah.
        if len(closes) < 21:
            continue
        close_20d_ago = float(closes.iloc[-21])
        roc_20d = (current_price - close_20d_ago) / close_20d_ago * 100 if close_20d_ago else None
        if roc_20d is None or roc_20d < BSJP_PULLBACK_MIN_ROC_20D:
            continue

        # Dip-then-reclaim EMA9: HARI INI sudah di atas EMA9, tapi salah
        # satu dari N hari terakhir SEMPAT di bawahnya (pullback genuine,
        # bukan uptrend yang belum pernah mundur sama sekali).
        ema9_series = closes.ewm(span=9, adjust=False).mean()
        today_ema9 = float(ema9_series.iloc[-1])
        today_above_ema9 = current_price > today_ema9
        recent_dip = any(
            float(closes.iloc[-1 - i]) < float(ema9_series.iloc[-1 - i])
            for i in range(1, BSJP_PULLBACK_DIP_LOOKBACK_DAYS + 1)
            if len(closes) > i
        )
        if not (today_above_ema9 and recent_dip):
            continue

        # Likuiditas -- floor SAMA dengan lane momentum, konsisten (bukan
        # angka baru yang dikarang terpisah).
        volume_today = float(hist_daily["Volume"].iloc[-1])
        value_traded_today = current_price * volume_today
        if value_traded_today < BSJP_MIN_VALUE_TRADED_IDR:
            continue

        results.append({
            "ticker": ticker, "current_price": current_price, "roc_20d": round(roc_20d, 1),
            "rsi": r.get("rsi"), "value_traded_today": value_traded_today,
            "ema9": round(today_ema9, 0),
            "action_label_id": r.get("action_label_id"),
            "fast_tag_note": core.format_fast_candidate_tag(r),
        })

    if not results:
        await core.safe_reply(
            update.message,
            "🔄 BSJP PULLBACK: tidak ada kandidat (uptrend jangka pendek + dip-reclaim EMA9) saat ini."
        )
        return

    results.sort(key=lambda r: r["roc_20d"], reverse=True)

    lines = [
        f"🔄 BSJP PULLBACK — {len(results)} kandidat (uptrend jangka pendek + reclaim EMA9)\n",
        "Arah BEDA dari BSJP MOMENTUM di atas: lane ini cari pullback SEHAT yang baru selesai "
        "(riset: buy-the-dip DALAM uptrend > bottom-fishing oversold murni), BUKAN saham yang lagi kencang naik. "
        "Kriteria REUSE dari findings.md's MOMENTUM_PULLBACK (IDX-validated, 63.0% win n=671).\n",
    ]
    for i, r in enumerate(results, 1):
        label = r.get("action_label_id")
        wr = core.get_winrate_for_label(label) if label else ""
        wr_note = f" | WR {wr}" if wr else ""
        lines.append(
            f"{i}. {r['ticker']} — {r['current_price']:.0f} (ROC20d {r['roc_20d']:+.1f}%){wr_note}\n"
            f"   RSI {r['rsi']} | EMA9 {r['ema9']} | Value {r['value_traded_today']/1e9:.1f}M"
            f"{r.get('fast_tag_note', '')}"
        )

    buttons = core.build_check_buttons([r["ticker"] for r in results])
    await core.safe_reply(update.message, "\n\n".join(lines), reply_markup=buttons)
    try:
        await asyncio.to_thread(core.lock_daily_daytrade_picks, results, "bsjp_pullback")
    except Exception as e:
        print(f"⚠️ Gagal mengunci picks /bsjp pullback untuk /winrate: {e}")


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

    # MBSS v2 (user request — "satu command tapi 2 tagging"): lane KEDUA,
    # arah beda (pullback-dalam-uptrend, bukan chase momentum) — sama
    # prinsip independen dengan BSJP-ARA di bawah, satu lane gagal/kosong
    # tidak menghentikan yang lain.
    try:
        await _run_bsjp_pullback(update)
    except Exception as e:
        print(f"⚠️ BSJP Pullback lane gagal: {e}")

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
            "catalyst_category": c.get("catalyst_category"), "catalyst_score": c.get("catalyst_score"),
            "catalyst_reasoning": c.get("catalyst_reasoning"),
            "targets": eod_r.get("targets", {}), "action_label_id": eod_r.get("action_label_id"),
        })

    if not ara_results:
        await core.safe_reply(
            update.message,
            f"🌆 BSJP-ARA: {len(ara_candidates)} kandidat dari pre-filter semalam (sudah lolos filter katalis positif), "
            f"tapi belum ada yang naik ≥{BSJP_ARA_MIN_MOMENTUM_PCT:.0f}% sejak open DAN volume ≥{BSJP_ARA_MIN_VOLQ:.0f}x normal. Coba cek lagi nanti."
        )
        return

    CATALYST_LABEL = {"strong_bullish": "🔥 Strong Bullish", "bullish": "🟢 Bullish"}
    ara_results.sort(key=lambda r: (r.get("catalyst_score") or 0, r["momentum_pct"]), reverse=True)
    ara_lines = [f"🌆 BSJP-ARA — {len(ara_results)} kandidat (katalis positif + naik ≥{BSJP_ARA_MIN_MOMENTUM_PCT:.0f}% sejak open + volume ≥{BSJP_ARA_MIN_VOLQ:.0f}x normal)\n"]
    ara_lines.append("⚠️ Metode TERPISAH dari 6 kriteria di atas — pola \"diam kemarin, meledak hari ini\" (referensi GIAA). Belum ada rekam jejak, pantau /winrate source=bsjp_ara.\n")
    for i, r in enumerate(ara_results, 1):
        news_str = f"\n   📰 {r['news_titles'][0]}" if r["news_titles"] else ""
        sector_str = market_engine.format_sector_tag(r.get("sector"))
        cat_label = CATALYST_LABEL.get(r.get("catalyst_category"), r.get("catalyst_category") or "-")
        catalyst_str = f"\n   {cat_label} ({r.get('catalyst_score', 0)}/100): {r.get('catalyst_reasoning', '-')}"
        ara_lines.append(
            f"{i}. {r['ticker']} — {r['current_price']:.0f} (open {r['today_open']:.0f}, {r['momentum_pct']:+.1f}% sejak open)\n"
            f"   VolQ {r['volq_ratio']}x | Jarak ARA {r['ara_distance']}%{catalyst_str}{news_str}{sector_str}"
        )

    buttons = core.build_check_buttons([r["ticker"] for r in ara_results])
    await core.safe_reply(update.message, "\n\n".join(ara_lines), reply_markup=buttons)
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
    # MBSS v2 (user request): tampilkan data LAMA dengan keterangan jelas,
    # bukan tolak total — sama seperti /hc.
    scored, staleness_note = nightly_engine.load_daily_scan_cache_allow_stale()
    if not scored:
        await core.safe_reply(update.message, "⚠️ Cache /eodscan belum pernah ada — jalankan /eodscan dulu.")
        return

    candidates = [r for r in scored.values() if r.get("action_id") == "STRONG_BUY"]
    if not candidates:
        await core.safe_reply(update.message, "📋 Tidak ada saham STRONG_BUY di cache hari ini.")
        return

    candidates.sort(key=lambda r: r.get("scores", {}).get("final", 0), reverse=True)
    broksum_data = nightly_engine.load_broksum_250()

    # MBSS v2 (user request — inline winrate): semua kandidat di sini SAMA
    # persis label-nya (STRONG_BUY), jadi cukup 1x di atas, bukan diulang
    # tiap baris.
    wr = core.get_winrate_for_label("STRONG_BUY")
    wr_line = f" (winrate historis: {wr})" if wr else ""
    lines = [f"💪 SEMUA STRONG_BUY{wr_line} — {len(candidates)} saham (cache /eodscan, tidak difilter likuiditas/lane apa pun)\n"]
    if staleness_note:
        lines.insert(0, staleness_note)
    for i, r in enumerate(candidates, 1):
        s = r.get("scores", {})
        t = r.get("targets", {})
        rr = t.get("risk_reward_at_max")
        rr_str = f"1:{rr:.2f}" if isinstance(rr, (int, float)) else "-"
        value_traded = r.get("value_traded")
        liq_note = f" | Value {value_traded/1e9:.1f}M" if isinstance(value_traded, (int, float)) else ""

        ceiling_str = ""
        ceiling = broker_engine.get_best_available_ceiling(r["ticker"], broksum_data)
        if ceiling:
            ceiling_str = f" / {ceiling['avg_price']:.0f}*"

        lines.append(
            f"{i}. {r['ticker']} — Final {s.get('final', 0):.1f} "
            f"(Nilai {s.get('value', 0):.1f} | Momentum {s.get('momentum', 0):.1f} | Sentimen {s.get('sentiment', 0):.1f})\n"
            f"   RR {rr_str}{liq_note}\n"
            f"   Entry {t.get('buy_range', '-')}{ceiling_str}{market_engine.format_sector_tag(r.get('sector'))}{broker_engine.format_smart_money_tag(r['ticker'], broksum_data)}"
        )

    lines.append("\nDetail lengkap: /check TICKER")
    buttons = core.build_check_buttons([r["ticker"] for r in candidates])
    await core.safe_reply(update.message, "\n\n".join(lines), reply_markup=buttons)


def compute_consensus_candidates(scored: dict, broksum_data: dict, market_regime: str | None = None) -> tuple[list, list]:
    """
    Shared cross-tool tagging logic behind /consensus, extracted so /tanya can
    reuse the SAME "which tools agree on this ticker" computation instead of
    re-deriving a parallel version (REUSE daripada bikin formula baru lagi).

    Returns (qualifying, multi_broker_lines):
    - qualifying: tickers tagged by >=2 independent tools (HIGH CONVICTION,
      STRONG_BUY, SCREENDAYTRADE lane, GPTPICK, MULTIBAGGER, SMART MONEY),
      each dict is the original scored record + "_consensus_tools"/"_multibagger",
      sorted by tool-count then final score (both descending).
    - multi_broker_lines: [(net_value_idr, "TICKER: broker detail"), ...] for
      tickers net-bought by >1 whitelist broker AND tagged by >=1 other tool,
      sorted by net value descending.
    """
    GOOD_SDT_LANES = {"PRIORITY FRESH", "PRIORITY CONT"}  # SECONDARY WATCH/LOW EDGE sengaja TIDAK dihitung (lihat revisi minggu lalu)
    GPTPICK_MIN_SCORE = 65  # kira-kira ambang bawah yang biasanya masuk top 3-5 nyata

    qualifying = []
    # MBSS v2 (user request — irisan lintas-tool buat filter section
    # akumulasi & multi-broker di bawah): ticker yang kena tag dari SALAH
    # SATU tool non-bandarmology (HC/STRONG_BUY/SCREENDAYTRADE/GPTPICK/
    # MULTIBAGGER) — dipakai supaya sinyal broker/akumulasi yang tampil
    # bukan yang berdiri sendiri tanpa validasi tool lain sama sekali.
    other_tool_tickers = set()
    # Dibaca SEKALI di luar loop (cache_manager baca disk tiap get() —
    # jangan panggil per-ticker di loop ~389 ticker di bawah).
    multibagger_by_ticker = {
        c["symbol"]: c
        for c in (nightly_engine.load_rapidapi_market_intelligence().get("multibagger") or {}).get("candidates", [])
        if c.get("symbol")
    }
    for r in scored.values():
        tools = []

        if scoring_engine.is_high_conviction_regime_aware(r, market_regime):
            tools.append("HIGH CONVICTION")

        if r.get("action_id") == "STRONG_BUY":
            tools.append("STRONG_BUY")

        try:
            bias = core.compute_screendaytrade_positive_bias(r, market_regime)
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

        # MBSS v2 (user request — longer-horizon intersection): ticker yang
        # JUGA lolos multibagger scan RapidAPI dihitung sebagai 1 dimensi
        # tool tambahan — sinyal "akumulasi + potensi hold panjang" yang
        # kompak dengan pick day-trade lain (contoh kasus user: SLIS).
        multibagger = multibagger_by_ticker.get(r["ticker"])
        if multibagger:
            tools.append(f"MULTIBAGGER ({multibagger.get('multibagger_score', '-')}/100)")

        if tools:
            other_tool_tickers.add(r["ticker"])

        # MBSS v2 (user request — smart money sebagai dimensi konsensus):
        # kalau ADA broker whitelist yang net buy ticker ini (dari
        # broksum_250), hitung sebagai 1 "tool" tambahan — bandarmology
        # sekarang ikut jadi salah satu lensa konsensus, bukan cuma tag
        # terpisah.
        smart_money = broker_engine.get_smart_money_accumulation(r["ticker"], broksum_data)
        if smart_money:
            codes = ", ".join(a["code"] for a in smart_money)
            tools.append(f"SMART MONEY ({codes})")

        if len(tools) >= 2:
            qualifying.append({**r, "_consensus_tools": tools, "_multibagger": multibagger})

    # MBSS v2 (user request — section terpisah, bandarmology paling kuat):
    # ticker yang di-net-buy LEBIH DARI 1 broker whitelist SEKALIGUS —
    # scan SELURUH broksum_250 (bukan cuma yang qualifying dari 4 tool
    # lain), karena ini sinyal berdiri sendiri, TIDAK BOLEH ikut
    # ke-block kalau qualifying (5-tool) kosong — makanya dihitung di sini,
    # SEBELUM early-return di bawah (pola sama seperti fix BSJP-ARA
    # sebelumnya: 2 hal independen, jangan saling menghentikan).
    #
    # BUGFIX (user report — saham non-syariah seperti BBCA/BBRI/BMRI
    # muncul di sini): broksum_250 sekarang berisi hasil sweep RapidAPI
    # per broker yang mencakup SELURUH bursa (sudah difilter ke universe
    # syariah di sumbernya, engine/nightly.py). Sebagai lapis kedua DAN
    # supaya sinyal yang tampil di sini genuinely "diiriskan dengan tools
    # lain" (bukan cuma berdiri sendiri) seperti diminta user, saring juga
    # ke other_tool_tickers — ticker harus muncul di scored (otomatis
    # syariah) DAN kena tag dari minimal 1 tool lain.
    multi_broker_lines = []
    for ticker, rows in broksum_data.items():
        if ticker not in other_tool_tickers:
            continue
        accum = broker_engine.get_smart_money_accumulation(ticker, broksum_data)
        if len(accum) >= 2:
            parts = ", ".join(f"{a['code']} @ avg {a['buy_avg_price']:.0f}" for a in accum)
            multi_broker_lines.append((sum(a["net_value_idr"] for a in accum), f"{ticker}: {parts}"))
    multi_broker_lines.sort(key=lambda x: x[0], reverse=True)

    qualifying.sort(key=lambda r: (len(r["_consensus_tools"]), r.get("scores", {}).get("final", 0)), reverse=True)
    return qualifying, multi_broker_lines


EXPLOSIVE_MIN_SCORE_BY_REGIME = {
    "R1_BULL_STABLE": 50, "R2_BULL_HIGH_VOL": 55, "R3_SIDEWAYS": 60,
    "R4_RISK_OFF": 65, "R5_STRESS": 75, "R0_UNKNOWN": 60,
}  # doc section 15.4/748: R1=50 baseline, others stricter pending forward validation — same "conservative until proven" posture as DANGER_GATE_QUANTILE_BY_REGIME.
EXPLOSIVE_MAX_NAMES = 3
SMART_MONEY_NET_SELL_THRESHOLD = -15.0  # mirrors the +15 threshold /hc's AKUMULASI section already uses for the buy side


def _consensus_sdt_hc_selected(pool: list, market_regime: str | None = None) -> tuple[set, set]:
    """
    EOD-only "selected by /screendaytrade" / "selected by /hc" per AB-RC1
    doc section 6.1 — reuses compute_screendaytrade_positive_bias's lane
    classification (same GOOD_SDT_LANES the old consensus tagging used)
    and is_high_conviction, BOTH already EOD-computable from cache (no live
    intraday re-enrichment needed) — deliberate simplification from the
    doc's literal "selected by the live /screendaytrade command", since
    that would mean re-running live intraday enrichment a second time
    inside /consensus for the same candidates. The lane/HC-criteria
    classification IS the substantive selection signal; live enrichment in
    the /screendaytrade command itself is for precision entry timing
    display, not the core categorization used here.
    """
    GOOD_SDT_LANES = {"PRIORITY FRESH", "PRIORITY CONT"}
    sdt_selected, hc_selected = set(), set()
    for r in pool:
        try:
            if core.compute_screendaytrade_positive_bias(r, market_regime).get("lane") in GOOD_SDT_LANES:
                sdt_selected.add(r["ticker"])
        except Exception:
            pass
        if scoring_engine.is_high_conviction_regime_aware(r, market_regime):
            hc_selected.add(r["ticker"])
    return sdt_selected, hc_selected


def _macd_slope_pct_raw(r: dict) -> float | None:
    """macd_line_slope_3d dinormalisasi harga -- versi MENTAH (bukan macd_slope_percentile
    yang adaptif per-ticker), dipakai supaya persentilnya cross-sectional terhadap `pool`
    yang SAMA seperti dist_to_sma50_pct/ret_5d_pct, konsisten dengan backtest quintile sweep."""
    slope = r.get("macd_line_slope_3d")
    price = r.get("price")
    if slope is None or not price:
        return None
    return float(slope) / float(price) * 100


def _macd_trend_clarity_percentile(r: dict, pool: list) -> float | None:
    """
    MBSS v2 (user request — quintile sweep research_explosive_lane_v2_
    quintile_sweep.py menemukan pola U, BUKAN monoton, di 3 dari 4 dimensi
    Explosive Lane v2: dist_to_sma50_pct/macd_slope_pct/ret_5d_pct. Uji
    lanjutan (research_explosive_q1_profile_test.py) buktikan Q1 (ekstrem
    RENDAH) itu REAL edge (13% explosive-rate, 2x Q2/Q3) TAPI arahnya tidak
    bisa diprediksi lebih lanjut dari MACD line/histogram (Cohen's d di
    dalam Q1 semua kecil, |d|<0.3) -- beda dari Q4/Q5 yang arahnya jelas.
    User keputusan: cutoff Q2/Q3 (dead zone, ~6.5% explosive-rate, tidak
    ada nilai tebus), Q1 dapat jatah TERBATAS (1-2 pick, TIDAK diranking
    halus di dalamnya), Q4/Q5 jadi pool utama buat dua lensa (explosive
    score DAN probability_score).

    Komposit = rata-rata persentil cross-sectional ketiga dimensi (day_
    range_pct_10d TIDAK diikutkan -- itu satu-satunya yang monoton bersih,
    tetap persentil linear apa adanya di skor eksplosif). Return None kalau
    data tidak cukup (ticker ini ATAU pool-nya).
    """
    dist_sma50 = r.get("dist_to_sma50_pct")
    slope_raw = _macd_slope_pct_raw(r)
    ret5d = r.get("ret_5d_pct")
    if dist_sma50 is None or slope_raw is None or ret5d is None:
        return None

    dist_values = [x.get("dist_to_sma50_pct") for x in pool if x.get("dist_to_sma50_pct") is not None]
    slope_values = [v for v in (_macd_slope_pct_raw(x) for x in pool) if v is not None]
    ret5d_values = [x.get("ret_5d_pct") for x in pool if x.get("ret_5d_pct") is not None]
    if not dist_values or not slope_values or not ret5d_values:
        return None

    dist_pctl = backbone_engine.percentile_rank_list(dist_values, dist_sma50) * 100
    slope_pctl = backbone_engine.percentile_rank_list(slope_values, slope_raw) * 100
    ret5d_pctl = backbone_engine.percentile_rank_list(ret5d_values, ret5d) * 100
    return (dist_pctl + slope_pctl + ret5d_pctl) / 3


def _macd_trend_clarity_tier(r: dict, pool: list) -> str | None:
    """Q1 (0-20 persentil) / Q2 (20-40) / Q3 (40-60) / Q4 (60-80) / Q5 (80-100), atau None kalau data kurang."""
    pctl = _macd_trend_clarity_percentile(r, pool)
    if pctl is None:
        return None
    if pctl < 20: return "Q1"
    if pctl < 40: return "Q2"
    if pctl < 60: return "Q3"
    if pctl < 80: return "Q4"
    return "Q5"


def _explosive_score(r: dict, pool: list) -> tuple[float, bool, str]:
    """
    MBSS v2 (user request — GANTI formula lama, dari backtest research_
    macd_explosive_gain_profile.py, n=53.841 ticker-hari "dalam regime MACD
    bullish aktif"): formula lama (Room 32% + RR 30% + Activity 23% +
    Controlled-Vol 15%, doc AB-RC1 15.4) TIDAK dikondisikan pada state MACD
    sama sekali, dan volatilitas diperlakukan sebagai "controlled/moderate"
    (dihukum kalau terlalu ekstrem) -- backtest baru justru buktikan
    SEBALIKNYA: day_range_pct_10d yang makin TINGGI makin baik (d=0.563 utk
    >=10% gain, NAIK ke d=0.779 utk >=20%/proxy-ARA -- BUKAN "puncak di
    tengah" seperti formula lama asumsikan).

    EMPAT dimensi baru, SEMUA scale makin kuat dari EXPLOSIVE (>=10% dlm
    5hr) ke proxy-ARA (>=20%) -- pola paling konsisten dari seluruh riset
    MACD sesi ini (REUSE cuma 4 dari 10+ fitur signifikan yang SANGAT
    berkorelasi satu sama lain -- dist_to_ema9/21/sma20 semua ukur hal yang
    sama dengan dist_to_sma50, tidak ditambah semua sekaligus, hindari
    redundansi struktural persis pelajaran kriteria HC 2&7):
      - dist_to_sma50_pct (trend extension): d 0.408 -> 0.705
      - day_range_pct_10d (aktivitas/volatilitas): d 0.563 -> 0.779
      - macd_slope_percentile (momentum MACD, SUDAH adaptif per-ticker vs
        histori sendiri, reuse field yang sama dipakai gate macd_approach_
        tier): versi mentahnya (macd_slope_pct) d 0.497 -> 0.677
      - ret_5d_pct (momentum harga): d 0.471 -> 0.747

    Bobot 30/25/25/20 -- trend extension & aktivitas paling kuat, momentum
    MACD & harga PELENGKAP (secara struktural berkorelasi tapi cukup beda
    buat dipertahankan berdua). SEMUA dipersentilkan cross-sectional
    terhadap `pool` (SAMA konvensi dengan RR-percentile formula lama),
    KECUALI macd_slope_percentile yang SUDAH persentil (adaptif per-ticker).

    Hard-reject list DIPERTAHANKAN APA ADANYA (safety checks, tidak terkait
    pertanyaan "seberapa besar potensi gain", tidak ada alasan diubah).
    Returns (score, hard_rejected, reject_reason).
    """
    rr_now = backbone_engine.compute_rr_at_current_price(r)

    drp = r.get("day_range_pct_10d")
    drp_values = [x.get("day_range_pct_10d") for x in pool if x.get("day_range_pct_10d") is not None]
    drp_percentile = (backbone_engine.percentile_rank_list(drp_values, drp) * 100) if drp is not None and drp_values else 50.0

    dist_sma50 = r.get("dist_to_sma50_pct")
    dist_sma50_values = [x.get("dist_to_sma50_pct") for x in pool if x.get("dist_to_sma50_pct") is not None]
    dist_sma50_percentile = (backbone_engine.percentile_rank_list(dist_sma50_values, dist_sma50) * 100) if dist_sma50 is not None and dist_sma50_values else 50.0

    ret5d = r.get("ret_5d_pct")
    ret5d_values = [x.get("ret_5d_pct") for x in pool if x.get("ret_5d_pct") is not None]
    ret5d_percentile = (backbone_engine.percentile_rank_list(ret5d_values, ret5d) * 100) if ret5d is not None and ret5d_values else 50.0

    macd_slope_pctl = r.get("macd_slope_percentile")
    macd_slope_component = float(macd_slope_pctl) if macd_slope_pctl is not None else 50.0

    score = (
        dist_sma50_percentile * 0.30
        + drp_percentile * 0.25
        + macd_slope_component * 0.25
        + ret5d_percentile * 0.20
    )

    # Precondition SESUAI cakupan backtest (research_macd_explosive_gain_
    # profile.py): populasi yang diuji SELALU histogram MACD bullish aktif
    # -- di luar itu, formula ini extrapolasi tanpa dasar data.
    if r.get("macd_state") != "bullish":
        return score, True, "MACD histogram belum bullish — di luar cakupan backtest formula ini"
    # MBSS v2 (user request — cutoff Q2/Q3, lihat catatan _macd_trend_
    # clarity_percentile): dead-zone, explosive-rate ~6.5% (setengah dari
    # Q1, sepertiga dari Q5), tidak ada nilai tebus dibanding tier lain.
    _tier = _macd_trend_clarity_tier(r, pool)
    if _tier in ("Q2", "Q3"):
        return score, True, f"trend clarity {_tier} — zona lemah/dead-zone (explosive-rate ~6.5%, backtest research_explosive_lane_v2_quintile_sweep.py)"
    if r.get("is_near_price_floor"):
        return score, True, "dekat batas bawah harga IDX"
    if rr_now < 0.30:
        return score, True, "RR di harga sekarang sangat tipis"
    if r.get("obv_divergence") == "bearish_divergence":
        return score, True, "bearish OBV divergence"
    if r.get("macd_bearish_cross") and r.get("is_below_sma50"):
        return score, True, "MACD bearish cross + di bawah SMA50"
    if drp_percentile >= 95:
        return score, True, "volatilitas di ekor ekstrem (persentil >=95)"
    # BUGFIX (user report, real case: RAJA di-tag Explosive/FAST MOMENTUM
    # padahal action_id-nya HINDARI/JUAL keesokan harinya, danger meroket
    # ke 73/gagal gate — Explosive Lane TIDAK PERNAH cek action_id sama
    # sekali, formula Room/RR/Activity/ControlledVol murni independen dari
    # blend Value/Momentum/Sentimen yang menghasilkan AVOID_SELL). Section
    # ini didesain jadi daftar entry SIAP PAKAI — kontradiksi kalau tag
    # "opportunity" berdampingan dengan sinyal "avoid/sell" utk ticker yang
    # SAMA. Reject keras, sejajar dengan reject lain di atas.
    if r.get("action_id") == "AVOID_SELL":
        return score, True, "action_id AVOID_SELL (HINDARI/JUAL) — kontradiksi dgn status Explosive"
    return score, False, ""


def _compute_backbone_top3(pool: list, sdt_selected: set, hc_selected: set, backbone_result: dict) -> list:
    """
    Shared antara /consensus (EOD) dan /consensus live (MBSS v2, user
    request — "consensus live kok tidak tracking top3 entry backbone" —
    extracted supaya kedua tempat pakai definisi universe yang SAMA
    PERSIS, bukan dua salinan yang bisa diam-diam menyimpang seiring waktu).

    Universe: seluruh pool (Danger Gate survivor) yang kena tag SDT (lane
    bagus) atau HC, DIPERLUAS dengan lane SDT "EXTENDED / CHASE WATCH"
    (WR 74% real, n=23) dan streak kemunculan berturut-turut lintas-source
    >=3 hari (breakdown /winrate "Streak 3x", WR terbaik). Diurutkan ke
    probability_score tertinggi, dipangkas ke 3.

    Returns [(r, tags), ...] — tags = list string ("SDT"/"HC"/"EXTENDED-WR
    74%"/"STREAK Nx") buat ditampilkan pemanggil.
    """
    history = core.load_daytrade_picks_history()
    today_marker = core.get_current_trading_day_close_marker()
    universe = []
    for r in pool:
        t = r["ticker"]
        tags = []
        if t in sdt_selected: tags.append("SDT")
        if t in hc_selected: tags.append("HC")
        try:
            lane = core.compute_screendaytrade_positive_bias(r).get("lane")
        except Exception:
            lane = None
        if lane == "EXTENDED / CHASE WATCH" and "SDT" not in tags:
            tags.append("EXTENDED-WR 74%")
        streak = core.compute_consecutive_appearance_streak_any_source(t, today_marker, history)
        if streak >= 3:
            tags.append(f"STREAK {streak}x")
        # NOTE: fast_candidate SENGAJA cuma anotasi tambahan pada ticker yang
        # SUDAH qualify lewat tag lain di atas -- BUKAN kriteria qualifying
        # sendiri (masih tag-and-track, belum filter/gate, sesuai kesepakatan
        # user: validasi forward dulu sebelum dipakai menyaring apa pun).
        if tags and core.compute_fast_candidate_tag(r).get("is_fast_candidate"):
            tags.append("🚀 FAST")
        if tags:
            universe.append((r, tags))

    def _probscore(r):
        return (backbone_result.get("all_scored", {}).get(r["ticker"], {}) or {}).get("probability_score", 0)

    universe.sort(key=lambda pair: _probscore(pair[0]), reverse=True)
    return universe[:3]


def _load_fast_candidates() -> tuple[list, dict, str | None]:
    """
    Shared antara /fast dan /fastscan (MBSS v2, user request) — extract
    supaya kedua tempat pakai definisi kandidat FAST malam ini yang SAMA
    PERSIS. Returns (fast_picks sorted by probability_score desc,
    backbone_result, staleness_note_or_None). fast_picks kosong (bukan
    exception) kalau cache belum ada — caller cek backbone_result is None
    utk membedakan "belum ada eodscan" dari "ada tapi 0 kandidat fast".
    """
    scored, staleness_note = nightly_engine.load_daily_scan_cache_allow_stale()
    if not scored:
        return [], None, staleness_note
    backbone_result, backbone_staleness = nightly_engine.load_backbone_daily_allow_stale()
    if not backbone_result:
        return [], None, backbone_staleness

    pool = backbone_engine.filter_to_gate_survivors(list(scored.values()), backbone_result)
    fast_picks = [r for r in pool if core.compute_fast_candidate_tag(r).get("is_fast_candidate")]

    def _probscore(r):
        return (backbone_result.get("all_scored", {}).get(r["ticker"], {}) or {}).get("probability_score", 0)
    fast_picks.sort(key=_probscore, reverse=True)
    return fast_picks, backbone_result, backbone_staleness


async def fast_candidates_command(update, context):
    """
    /fast — MBSS v2 (user request): daftar BERDIRI SENDIRI kandidat
    fast_candidate malam ini (lihat compute_fast_candidate_tag, engine/
    legacy_core.py) — sebelumnya tag ini cuma nempel di /hc/screendaytrade/
    consensus, tidak ada cara lihat SEMUA kandidat fast sekaligus tanpa
    scroll command lain. Tetap dibatasi ke Danger Gate survivor (pool) —
    fast TANPA lolos gate bukan sinyal aman buat prioritas entry di open.
    """
    fast_picks, backbone_result, backbone_staleness = _load_fast_candidates()
    if backbone_result is None:
        await core.safe_reply(update.message, backbone_staleness or "⚠️ Cache /eodscan belum pernah ada — jalankan /eodscan dulu.")
        return

    scored, _ = nightly_engine.load_daily_scan_cache_allow_stale()
    pool = backbone_engine.filter_to_gate_survivors(list(scored.values()), backbone_result)
    sdt_selected, hc_selected = _consensus_sdt_hc_selected(pool, backbone_result.get("market_regime"))

    lines = [f"🚀 FAST CANDIDATES — {len(fast_picks)} saham (speed direlaks vol_ratio>=1.5x & day_range_10d>=15% + WAJIB dijaga bandar, lolos Danger Gate)"]
    if backbone_staleness:
        lines.insert(0, backbone_staleness)
    lines.append(
        "Prioritas entry saat OPEN besok, jangan tunggu konfirmasi tactical 30-40 menit. "
        "Kriteria v2.0 (speed + sinyal dijaga bandar/Bias Bandar) — BELUM ada data forward sama sekali, "
        "murni hipotesis dari observasi manual (YELO gagal di kriteria lama, BAIK jadi acuan baru). "
        "Tag ini MASIH tag-and-track — bukan jaminan.\n"
    )
    if not fast_picks:
        lines.append("Tidak ada kandidat fast malam ini.")
    for i, r in enumerate(fast_picks, 1):
        t = r["ticker"]
        info = backbone_result.get("all_scored", {}).get(t, {}) or {}
        also = []
        if t in sdt_selected: also.append("SDT")
        if t in hc_selected: also.append("HC")
        also_str = f" | {', '.join(also)}" if also else ""
        lines.append(
            f"{i}. {t} — Entry Rank #{info.get('entry_rank', '-')}/{info.get('entry_rank_total', '-')} "
            f"(prob {info.get('probability_score', '-')}, danger {info.get('predicted_danger', '-')}){also_str}\n"
            f"   Vol ratio {r.get('vol_ratio')}x | Day range 10D {r.get('day_range_pct_10d')}% | "
            f"Bias Bandar {r.get('bias_bandar', '-')} ({r.get('whitelist_num_brokers', 0)} broker)"
        )

    buttons = core.build_check_buttons([r["ticker"] for r in fast_picks])
    await core.safe_reply(update.message, "\n".join(lines), reply_markup=buttons)


def _load_fastscan_candidacy_union(max_hc_names: int = 5) -> tuple[list, dict, dict, str | None]:
    """
    MBSS v2 (user request — "explosive lane/fast momentum, high conviction
    nilai tertinggi, bisa kah digabungkan?"): universe /fastscan DIPERLUAS
    dari FAST tag SAJA jadi UNION 4 sumber (masuk salah SATU cukup, bukan
    AND):
    1. FAST tag EOD (_load_fast_candidates)
    2. Explosive Lane malam ini (TRUE EXPLOSIVE + FAST MOMENTUM, reuse
       _explosive_score PERSIS sama dengan /consensus)
    3. Top HC by final score (reuse is_high_conviction, sudah termasuk
       AVOID_SELL exclusion dari fix sebelumnya)
    4. LONJAKAN VOLUME EKSTREM dari /screendaytrade live TERBARU, KALAU
       masih segar (<=20 menit — lihat load_latest_live_volume_spikes)

    "Highest live vol pace" (SDT live main ranking) SENGAJA TIDAK
    diikutkan — itu discovery tool 5m yang sudah live-confirmed sendiri,
    beda peran dari 3+1 sumber di atas yang murni EOD (kecuali #4 yang
    sudah live tapi eksplisit "belum rapi struktur", exactly yang perlu
    dikonfirmasi ulang).

    Returns (union_records, tags_by_ticker, backbone_result, staleness_note).
    """
    fast_picks, backbone_result, staleness_note = _load_fast_candidates()
    if backbone_result is None:
        return [], {}, None, staleness_note

    scored, _ = nightly_engine.load_daily_scan_cache_allow_stale()
    pool = backbone_engine.filter_to_gate_survivors(list(scored.values()), backbone_result)
    pool_by_ticker = {r["ticker"]: r for r in pool}
    market_regime = backbone_result.get("market_regime", "R0_UNKNOWN")

    union = {}
    tags = {}

    def _add(ticker, r, tag):
        if ticker not in union:
            union[ticker] = r
            tags[ticker] = set()
        tags[ticker].add(tag)

    for r in fast_picks:
        _add(r["ticker"], r, "FAST")

    min_score = EXPLOSIVE_MIN_SCORE_BY_REGIME.get(market_regime, EXPLOSIVE_MIN_SCORE_BY_REGIME["R0_UNKNOWN"])
    for r in pool:
        score, rejected, _reason = _explosive_score(r, pool)
        if not rejected and score >= min_score:
            _add(r["ticker"], r, "Explosive")

    hc_candidates = [r for r in pool if scoring_engine.is_high_conviction_regime_aware(r, market_regime)]
    hc_candidates.sort(key=lambda r: r.get("scores", {}).get("final", 0), reverse=True)
    for r in hc_candidates[:max_hc_names]:
        _add(r["ticker"], r, "HC")

    for t in core.load_latest_live_volume_spikes():
        if t in pool_by_ticker:
            _add(t, pool_by_ticker[t], "LiveSpike")

    return list(union.values()), tags, backbone_result, staleness_note


async def fast_scan_command(update, context):
    """
    /fastscan — MBSS v2 (user request): scan 1-MENIT LIVE atas union
    kandidat malam ini (FAST ∪ Explosive Lane ∪ Top HC ∪ live-spike segar
    dari /screendaytrade live — lihat _load_fastscan_candidacy_union),
    cari ledakan volume + spike harga. Dipicu MANUAL oleh user (bukan
    cron/auto) — paling efektif dijalankan di 2 window: 08:50-09:30 (chase
    opening) dan menjelang tutup Sesi 1 (~11:20-12:00, buat chase
    carry-over ke open Sesi 2). Universe SENGAJA dibatasi ke union di atas
    (bukan seluruh pool /eodscan) — 1m memang dari awal dimaksudkan cuma
    untuk shortlist kecil (lihat get_intraday_session_bars).
    """
    union_picks, source_tags, backbone_result, staleness_note = _load_fastscan_candidacy_union()
    if backbone_result is None:
        await core.safe_reply(update.message, staleness_note or "⚠️ Cache /eodscan belum pernah ada — jalankan /eodscan dulu.")
        return
    if not union_picks:
        await core.safe_reply(update.message, "⚠️ Tidak ada kandidat FAST/Explosive/HC/live-spike malam ini — tidak ada yang bisa discan live.")
        return

    await core.safe_reply(update.message, f"🔍 Scan 1m untuk {len(union_picks)} kandidat (FAST∪Explosive∪HC∪LiveSpike), mohon tunggu...")

    checked = []
    for r in union_picks:
        t = r["ticker"]
        try:
            detection = await asyncio.to_thread(core.detect_intraday_explosion, t)
        except Exception as e:
            print(f"⚠️ /fastscan: gagal cek {t}: {e}")
            continue
        if detection:
            checked.append((t, detection))

    exploded = [(t, d) for t, d in checked if d["is_explosion"]]
    exploded.sort(key=lambda pair: pair[1]["volume_ratio"], reverse=True)

    lines = [f"🔥 FASTSCAN 1m — {len(exploded)} dari {len(union_picks)} kandidat (FAST∪Explosive∪HC∪LiveSpike) menunjukkan ledakan live"]
    lines.append(
        "Kriteria PLACEHOLDER, BELUM ada data forward sama sekali: volume_ratio>=3.0x (3 bar terakhir vs "
        "baseline 15 bar sebelumnya) DAN price spike>=1.5% (3 bar terakhir). Paling efektif dijalankan manual "
        "08:50-09:30 (chase opening) atau menjelang tutup Sesi 1 (chase carry-over ke Sesi 2). "
        "Verifikasi manual sebelum entry — ini sinyal awal, bukan konfirmasi final.\n"
    )
    if not exploded:
        lines.append("Belum ada ledakan terdeteksi saat ini — coba lagi beberapa menit lagi.")
    for t, d in exploded:
        tag_str = f" [{', '.join(sorted(source_tags.get(t, [])))}]" if source_tags.get(t) else ""
        lines.append(f"🔥 {t}{tag_str} — {d['price']:.0f} | Vol ratio {d['volume_ratio']}x | Spike {d['price_spike_pct']:+.2f}% (3 bar terakhir)")

    skipped = len(union_picks) - len(checked)
    if skipped:
        lines.append(f"\n({skipped} kandidat dilewati — data 1m belum cukup atau di luar jam bursa)")

    buttons = core.build_check_buttons([t for t, _ in exploded])
    await core.safe_reply(update.message, "\n".join(lines), reply_markup=buttons)


async def consensus_command(update, context):
    """
    /consensus — ringkasan brief AB-RC1 (MBSS v2, user backtest — lihat
    backtest/MBSS_v317_AB_RC1_Final_Implementation_and_Research-1.md,
    source of truth). Menggantikan sistem tagging ">=2 dari 5 tool" lama
    dengan struktur backbone-first sesuai dokumen §6-7:

    - CONSENSUS PRIME: irisan PERSIS Backbone Top-8 ∩ SDT-lane-positif ∩
      HC-high-conviction pada hari yang sama. Tidak dilonggarkan biar
      dipaksa ada output — bisa 0. STATE-AWARE (doc §16-17, disederhanakan
      dari NEW/ACTIVE/UPGRADED jadi NEW/ACTIVE — lihat catatan di
      engine/backbone.py soal UPGRADED yang belum dikerjakan): ticker yang
      SUDAH punya posisi aktif tertandai tidak dianggap sinyal beli baru
      lagi (cukup "konfirmasi ulang"), dan ticker yang BARU kena SL masuk
      cooldown 3 hari bursa sebelum bisa direkomendasikan ulang.
    - EXPLOSIVE LANE: 1-3 nama dari kandidat lolos Danger Gate (boleh di
      luar Consensus Prime), formula §15.4, gate keras + ambang minimum
      per regime.
    - SMART-MONEY OVERLAY: bonus tag dari whitelist_accumulation_net_pct
      (sudah dihitung gratis saat /eodscan) — netral kalau data kosong.
    - LONG-HORIZON WATCH: multibagger RapidAPI, horizon terpisah, tidak
      dicampur ke skor Day 1-5.

    Murni deterministik Python — TIDAK ada narasi Gemini lagi (beda dari
    versi lama), karena format §7 dokumen sudah eksplisit/terstruktur,
    tidak butuh interpretasi bahasa natural.
    """
    if context.args and context.args[0].lower() == "live":
        await consensus_live_command(update, context)
        return

    scored, staleness_note = nightly_engine.load_daily_scan_cache_allow_stale()
    if not scored:
        await core.safe_reply(update.message, "⚠️ Cache /eodscan belum pernah ada — jalankan /eodscan dulu.")
        return

    backbone_result, backbone_staleness = nightly_engine.load_backbone_daily_allow_stale()
    if not backbone_result:
        await core.safe_reply(update.message, "⚠️ Backbone belum pernah dihitung — jalankan /eodscan dulu (versi terbaru).")
        return
    if backbone_staleness:
        await core.safe_reply(update.message, backbone_staleness)

    broksum_data = nightly_engine.load_broksum_250()  # dibaca sekali di sini, cache read-only, dipakai buat sort net value SMART-MONEY WATCH di bawah
    pool = backbone_engine.filter_to_gate_survivors(list(scored.values()), backbone_result)
    pool_by_ticker = {r["ticker"]: r for r in pool}
    top8_tickers = [r["ticker"] for r in backbone_result.get("top8", [])]
    market_regime = backbone_result.get("market_regime", "R0_UNKNOWN")
    sdt_selected, hc_selected = _consensus_sdt_hc_selected(pool, market_regime)

    lines = [
        "📊 MARKET REGIME",
        market_regime,
        "",
    ]

    # === BACKBONE TOP-3 (user revisi lanjutan — bukan lagi dibatasi ke
    # Backbone Top-8, itu terlalu sempit: irisan Top-8 (cuma 8 nama) ∩
    # SDT/HC seringkali cuma nyisa 1 nama). Universe SEKARANG: seluruh pool
    # (semua Danger Gate survivor) yang kena tag SDT (lane bagus) ATAU HC,
    # DIPERLUAS lagi dengan 2 tag tambahan yang PUNYA bukti /winrate real
    # (bukan definisi baru dari nol -- REUSE sinyal yang sudah tervalidasi):
    # - lane SDT "EXTENDED / CHASE WATCH" (74% win real, n=23) -- sebelumnya
    #   TIDAK masuk GOOD_SDT_LANES (cuma PRIORITY FRESH/CONT), padahal WR-nya
    #   nyata lebih baik dari beberapa lane yang sudah masuk.
    # - streak kemunculan berturut-turut lintas-source >=3 hari (breakdown
    #   /winrate "Streak 3x" -- WR terbaik di antara panjang streak lain).
    # Baru diurutkan ke probability_score (Entry Rank) tertinggi, ambil 3.
    top3 = _compute_backbone_top3(pool, sdt_selected, hc_selected, backbone_result)
    lines.append(f"🧱 BACKBONE TOP-3 (universe SDT/HC/WR-tag, {len(top3)} saham)")
    if not top3:
        lines.append("Tidak ada kandidat SDT/HC/WR-tag malam ini.")
    for r, tags in top3:
        info = backbone_result.get("all_scored", {}).get(r["ticker"], {}) or {}
        lines.append(
            f"{r['ticker']} — Entry Rank #{info.get('entry_rank', '-')}/{info.get('entry_rank_total', '-')} "
            f"(prob {info.get('probability_score', '-')}, danger {info.get('predicted_danger', '-')}) | Tag: {', '.join(tags)}"
        )
    lines.append("⚠️ Ranking murni, BUKAN sinyal entry siap pakai — cek /check sebelum ambil keputusan.")

    # === CONSENSUS PRIME (state-aware NEW/ACTIVE/COOLDOWN — doc §16-17) ===
    # Resolve tracked positions dulu (TP/SL/time-exit) terhadap harga
    # PENUTUPAN TERBARU (scored, bukan cuma pool yang sudah difilter gate —
    # ticker yang sudah dipegang tetap perlu dicek walau malam ini tidak
    # lolos gate lagi), BARU klasifikasi kandidat Prime hari ini.
    position_state = backbone_engine.load_consensus_position_state()
    backbone_engine.resolve_consensus_positions(position_state, scored)

    prime_tickers_today = [t for t in top8_tickers if t in sdt_selected and t in hc_selected]
    prime_display = []  # (ticker, status) buat ditampilkan sebagai entry beneran
    cooldown_blocked = []
    for t in prime_tickers_today:
        status = backbone_engine.classify_and_update_consensus_entry(t, pool_by_ticker[t], position_state)
        if status == "COOLDOWN_BLOCKED":
            cooldown_blocked.append(t)
        else:
            prime_display.append((t, status))
    backbone_engine.save_consensus_position_state(position_state)
    prime_tickers = [t for t, _ in prime_display]  # dipakai section EXPLOSIVE/tombol di bawah, exclude cooldown-blocked

    lines.append(f"🏆 CONSENSUS PRIME — {len(prime_display)} saham")
    if not prime_display:
        lines.append("Tidak ada irisan Backbone Top-8 ∩ SDT ∩ HC hari ini. Kualitas terbatas, bukan dipaksakan.")
    for i, (t, status) in enumerate(prime_display, 1):
        r = pool_by_ticker[t]
        info = backbone_result["all_scored"].get(t, {})
        hc = r.get("high_conviction", {})
        sm_pct = r.get("whitelist_accumulation_net_pct")
        sm_tag = ""
        if isinstance(sm_pct, (int, float)):
            if sm_pct >= 15 and (r.get("whitelist_num_brokers") or 0) >= 2:
                sm_tag = f"  |  💎 TRIPLE CONFIRMATION (smart money net-buy {sm_pct:+.0f}%, {r.get('whitelist_num_brokers')} broker)"
            elif sm_pct <= SMART_MONEY_NET_SELL_THRESHOLD:
                sm_tag = f"  |  ⚠️ SMART-MONEY DIVERGENCE (net-sell {sm_pct:+.0f}%)"
        st_info = position_state.get(t, {})
        if status == "NEW":
            status_line = "🆕 NEW CONSENSUS — sinyal baru"
        else:  # ACTIVE
            status_line = f"📌 ACTIVE CONSENSUS — sudah dipegang sejak {st_info.get('entry_date', '?')}, ini KONFIRMASI ULANG bukan sinyal beli baru"
        lines.append(
            f"{i}. {t} — {status_line}\n"
            f"   Entry Rank #{info.get('entry_rank', '-')}/{info.get('entry_rank_total', '-')} (prob {info.get('probability_score', '-')}, danger {info.get('predicted_danger', '-')})\n"
            f"   HC: {hc.get('criteria_met', 0)}/{hc.get('criteria_checkable', 0)}{sm_tag}"
            f"{core.format_fast_candidate_tag(r)}"
        )
    if cooldown_blocked:
        lines.append(
            f"🕐 {len(cooldown_blocked)} ticker lolos Backbone∩SDT∩HC TAPI baru kena SL beberapa hari lalu, "
            f"masih cooldown ({', '.join(cooldown_blocked)}) — tidak direkomendasikan ulang, kecuali /check konfirmasi reclaim genuine."
        )

    # === EXPLOSIVE LANE ===
    explosive_pool = [r for r in pool if r["ticker"] not in prime_tickers]
    min_score = EXPLOSIVE_MIN_SCORE_BY_REGIME.get(market_regime, EXPLOSIVE_MIN_SCORE_BY_REGIME["R0_UNKNOWN"])
    explosive_scored = []
    for r in explosive_pool:
        score, rejected, _reason = _explosive_score(r, pool)
        if not rejected and score >= min_score:
            explosive_scored.append((score, r))
    explosive_scored.sort(key=lambda pair: pair[0], reverse=True)
    explosive_picks = explosive_scored[:EXPLOSIVE_MAX_NAMES]

    lines.append(f"\n🚀 EXPLOSIVE LANE — {len(explosive_picks)} saham (min score {min_score}, regime {market_regime})")
    if not explosive_picks:
        lines.append("Tidak ada kandidat lolos ambang explosive hari ini.")
    for i, (score, r) in enumerate(explosive_picks, 1):
        v5 = core.compute_daytrade_v5_summary(r)
        rr_now = backbone_engine.compute_rr_at_current_price(r)
        # MBSS v2 (user request — review label "TRUE EXPLOSIVE"): sebelumnya
        # cuma (RR>=2.0 ATAU Room>=80) -- keduanya sama-sama ukuran "besarnya
        # potensi upside", jadi OR di sini cuma menguji dimensi yang SAMA dua
        # kali dengan rumus beda, bukan dua bukti independen. Sekarang wajib
        # ADA partisipasi volume nyata (Activity>=65, placeholder pending
        # forward data) DAN regime MACD bullish established (bukan cuma
        # histogram baru positif tipis) -- macd_state bullish DAN MACD line
        # di atas 0.
        activity_score = v5["activity"]["score"]
        size_signal = rr_now >= 2.0 or v5["room"]["score"] >= 80
        macd_confirmed = r.get("macd_state") == "bullish" and r.get("macd_line_above_zero")
        label = "TRUE EXPLOSIVE" if (size_signal and activity_score >= 65 and macd_confirmed) else "FAST MOMENTUM"
        lines.append(
            f"{i}. {r['ticker']} — Explosive {score:.0f} [{label}]\n"
            f"   Room {v5['room']['score']} | RR@now {rr_now} | Activity {activity_score}\n"
            f"   Action: /check {r['ticker']} sebelum entry"
            f"{core.format_fast_candidate_tag(r)}"
        )

    # === SMART-MONEY WATCH (akumulasi kuat, belum ke-konfirmasi SDT/HC) ===
    # MBSS v2 (user request — "diurutkan dari top value net buy smartmoney
    # saja?"): threshold kualitas TETAP net_pct>=15% + >=2 broker (sama
    # seperti sebelumnya, sudah divalidasi), tapi URUTANnYA sekarang net
    # value IDR, bukan net_pct — net_pct bisa menyesatkan untuk ticker
    # likuiditas tipis (net-buy 20% dari transaksi Rp50jt ≠ net-buy 20%
    # dari Rp5M), sementara net value IDR langsung mencerminkan besaran
    # uang riil yang dikomit whitelist broker. TIDAK dipakai "menguat
    # tajam"/price momentum -- itu justru bertentangan sama tujuan lane
    # ini ("belum terkonfirmasi teknikal"), kalau harga sudah menguat tajam
    # harusnya sudah lolos SDT/HC dan tidak nyampe ke sini.
    watch_only = []
    for r in pool:
        if r["ticker"] in prime_tickers or r["ticker"] in {rr["ticker"] for _, rr in explosive_picks}:
            continue
        net_pct = r.get("whitelist_accumulation_net_pct")
        if not isinstance(net_pct, (int, float)) or net_pct < 15 or (r.get("whitelist_num_brokers") or 0) < 2:
            continue
        signal = broker_engine.compute_whitelist_accumulation_signal(r["ticker"], broksum_data.get(r["ticker"], []))
        net_value = signal.get("net_value") if signal else None
        watch_only.append((net_value if net_value is not None else 0, r))
    watch_only.sort(key=lambda pair: pair[0], reverse=True)
    watch_only = [r for _, r in watch_only[:3]]
    if watch_only:
        lines.append(f"\n💰 SMART-MONEY WATCH — {len(watch_only)} saham (akumulasi kuat, belum terkonfirmasi teknikal)")
        for r in watch_only:
            lines.append(f"• {r['ticker']} — net-buy whitelist {r['whitelist_accumulation_net_pct']:+.0f}% ({r.get('whitelist_num_brokers')} broker) — status: pantau, bukan entry call")

    # === LONG-HORIZON WATCH (multibagger, horizon terpisah) ===
    multibagger_candidates = (nightly_engine.load_rapidapi_market_intelligence().get("multibagger") or {}).get("candidates", [])
    multibagger_candidates = sorted(multibagger_candidates, key=lambda c: c.get("multibagger_score", 0), reverse=True)[:2]
    if multibagger_candidates:
        lines.append(f"\n🔭 LONG-HORIZON WATCH — {len(multibagger_candidates)} saham (horizon bulan, TIDAK dicampur ke skor Day 1-5)")
        for c in multibagger_candidates:
            sym = c.get("symbol", "?")
            also_in = []
            if sym in prime_tickers: also_in.append("Consensus Prime")
            if sym in {r["ticker"] for _, r in explosive_picks}: also_in.append("Explosive")
            if sym in hc_selected: also_in.append("HC")
            also_str = f" | Juga di: {', '.join(also_in)}" if also_in else ""
            lines.append(f"• {sym} — Multibagger {c.get('multibagger_score', '-')}/100, {c.get('potential_return', '-')} ({c.get('timeframe', '-')}){also_str}")

    lines.append("\nDetail lengkap & konfirmasi live: /check TICKER")
    if staleness_note:
        lines.insert(0, staleness_note)

    # Kunci ke winrate — source="consensus" — Prime + Explosive, sama
    # semangat dengan versi lama (uji apakah backbone-filtered picks
    # genuinely lebih akurat).
    lock_candidates = [pool_by_ticker[t] for t in prime_tickers] + [r for _, r in explosive_picks]
    if lock_candidates:
        try:
            await asyncio.to_thread(core.lock_daily_daytrade_picks, lock_candidates, "consensus", (backbone_result or {}).get("all_scored", {}))
        except Exception as e:
            print(f"⚠️ Gagal mengunci picks /consensus untuk /winrate: {e}")

    all_tickers = prime_tickers + [r["ticker"] for _, r in explosive_picks] + [r["ticker"] for r in watch_only]
    buttons = core.build_check_buttons(all_tickers)
    await core.safe_reply(update.message, "\n".join(lines), reply_markup=buttons)


async def consensus_live_command(update, context):
    """
    /consensus live — AB-RC3 tactical check-up ATAS pilihan EOD /consensus
    malam ini (MBSS v2, user request). Dua bagian:

    1. STATUS LIVE Consensus Prime + Explosive Lane: reuse PERSIS pipeline
       /check (classify_signal_validity -> compute_tactical_live_rank ->
       compute_tactical_decision) per ticker, fetch intraday live sekali per
       ticker (yfinance saja, TIDAK ada panggilan RapidAPI — aman dari sisi
       kuota walau dipanggil berkali-kali sehari). Sengaja TIDAK menyentuh/
       memutasi consensus position_state (cooldown dsb) — itu urusan
       /consensus versi EOD, di sini murni observasi read-only.
    2. TEMUAN DI LUAR TOOL: ticker yang di-/check MANUAL hari ini tapi TIDAK
       ada di Prime/Explosive malam ini — filter KETAT (user confirmed):
       cuma EXTENDED_CHASE dengan winrate_tag real, atau live_rank tinggi
       (>=60, placeholder pending forward data). Lihat
       backbone_engine.load_todays_notable_offradar_checks.
    """
    scored, staleness_note = nightly_engine.load_daily_scan_cache_allow_stale()
    if not scored:
        await core.safe_reply(update.message, "⚠️ Cache /eodscan belum pernah ada — jalankan /eodscan dulu.")
        return
    backbone_result, backbone_staleness = nightly_engine.load_backbone_daily_allow_stale()
    if not backbone_result:
        await core.safe_reply(update.message, "⚠️ Backbone belum pernah dihitung — jalankan /eodscan dulu (versi terbaru).")
        return

    pool = backbone_engine.filter_to_gate_survivors(list(scored.values()), backbone_result)
    pool_by_ticker = {r["ticker"]: r for r in pool}
    top8_tickers = [r["ticker"] for r in backbone_result.get("top8", [])]
    market_regime = backbone_result.get("market_regime", "R0_UNKNOWN")
    sdt_selected, hc_selected = _consensus_sdt_hc_selected(pool, market_regime)

    prime_tickers = [t for t in top8_tickers if t in sdt_selected and t in hc_selected]

    explosive_pool = [r for r in pool if r["ticker"] not in prime_tickers]
    min_score = EXPLOSIVE_MIN_SCORE_BY_REGIME.get(market_regime, EXPLOSIVE_MIN_SCORE_BY_REGIME["R0_UNKNOWN"])
    explosive_scored = []
    for r in explosive_pool:
        score, rejected, _reason = _explosive_score(r, pool)
        if not rejected and score >= min_score:
            explosive_scored.append((score, r))
    explosive_scored.sort(key=lambda pair: pair[0], reverse=True)
    explosive_tickers = [r["ticker"] for _, r in explosive_scored[:EXPLOSIVE_MAX_NAMES]]

    # MBSS v2 (user request — "consensus live kok tidak tracking top3 entry
    # backbone"): Backbone Top-3 ikut dicek live juga sekarang, reuse
    # PERSIS _compute_backbone_top3 yang sama dipakai /consensus EOD, biar
    # definisi universe-nya tidak menyimpang antar dua command.
    backbone_top3_tickers = [r["ticker"] for r, _tags in _compute_backbone_top3(pool, sdt_selected, hc_selected, backbone_result)]

    watchlist = list(dict.fromkeys(prime_tickers + explosive_tickers + backbone_top3_tickers))  # dedup, preserve order

    await core.safe_reply(update.message, f"🔄 Cek tactical live untuk {len(watchlist)} pilihan EOD Consensus, mohon tunggu...")

    portfolio = core.load_portfolio()
    held_positions = set(portfolio.get("positions", {}).keys())
    daily_broksum_history = nightly_engine.load_broksum_daily_history()

    state_icon = {
        "VALID": "✅", "RETEST": "🔁", "INVALID": "❌", "UNKNOWN": "❔",
        "EXTENDED_CHASE": "🔥", "EXTENDED_NO_CHASE": "⚠️",
    }

    lines = [f"🎯 CONSENSUS LIVE — {market_regime}", ""]
    lines.append(f"📌 STATUS LIVE — {len(watchlist)} pilihan EOD (Prime+Explosive+Backbone Top-3)")
    if not watchlist:
        lines.append("Tidak ada Consensus Prime/Explosive/Backbone Top-3 malam ini untuk dicek live.")

    for t in watchlist:
        base = pool_by_ticker[t]
        try:
            intraday_ctx = await asyncio.to_thread(core.fetch_intraday_market_context, t)
            r = dict(base)
            r["active_breakout"] = intraday_ctx.get("active_breakout", {"available": False})
            r["intraday_momentum"] = intraday_ctx.get("momentum", {"available": False})
            r["vwap_movement"] = intraday_ctx.get("vwap_movement", {"available": False, "overall_signal": "N/A"})
            if intraday_ctx.get("price") is not None:
                r["price"] = intraday_ctx["price"]
            hist = core.get_ohlcv_smart(t, limit=60)
            r["intraday_targets"] = await asyncio.to_thread(core.compute_intraday_targets, t, r, hist if not hist.empty else None)

            validity = backbone_engine.classify_signal_validity(r)
            bb_info = backbone_result.get("all_scored", {}).get(t)
            tactical_rank = backbone_engine.compute_tactical_live_rank(r, bb_info, daily_broksum_history)
            is_held = t in held_positions
            tactical_decision = backbone_engine.compute_tactical_decision(r, validity, is_held)
            backbone_engine.save_tactical_shadow_snapshot(t, validity, tactical_rank, tactical_decision, is_held)

            state = validity["state"]
            icon = state_icon.get(state, "❔")
            wr_str = f" [{validity.get('winrate_tag')}]" if validity.get("winrate_tag") else ""
            decision_label = tactical_decision["decision"].replace("_", " ")
            delta = tactical_rank["delta"]
            delta_str = f" ({delta:+.1f})" if delta else ""
            tag_bits = []
            if t in prime_tickers: tag_bits.append("Prime")
            if t in explosive_tickers: tag_bits.append("Explosive")
            if t in backbone_top3_tickers: tag_bits.append("Backbone Top-3")
            tag = f" [{', '.join(tag_bits)}]" if tag_bits else ""
            held_tag = " 💼" if is_held else ""

            # MBSS v2 (user request — "1-minute bar ini pakai juga untuk
            # consensus live bisa?"): TAG TAMBAHAN, bukan pengganti tactical
            # state di atas (yang tetap 5m -- sengaja, biar tidak tambah
            # flip-flop yang sudah dikeluhkan). Pool consensus live kecil
            # (~5-10 ticker), sama order-of-magnitude dengan shortlist
            # /fastscan, jadi murah buat dicek juga per ticker di sini.
            explosion_str = ""
            try:
                explosion = await asyncio.to_thread(core.detect_intraday_explosion, t)
                if explosion and explosion.get("is_explosion"):
                    explosion_str = f" | 🔥 LEDAKAN 1m (vol {explosion['volume_ratio']}x, spike {explosion['price_spike_pct']:+.2f}%)"
            except Exception as e:
                print(f"⚠️ /consensus live: gagal cek ledakan 1m {t}: {e}")

            lines.append(
                f"{icon} {t}{tag}{held_tag} — {state}{wr_str} — {decision_label} | "
                f"Live Rank {tactical_rank['live_rank']:.0f}/100{delta_str}{explosion_str}"
            )
        except Exception as e:
            print(f"⚠️ /consensus live: gagal cek tactical {t}: {e}")
            lines.append(f"❔ {t} — gagal ambil data live ({e})")

    offradar = backbone_engine.load_todays_notable_offradar_checks(exclude_tickers=set(watchlist))
    if offradar:
        lines.append(f"\n🔎 TEMUAN DI LUAR TOOL — {len(offradar)} ticker (dari /check manual hari ini, di luar Prime/Explosive)")
        for entry in offradar:
            icon = state_icon.get(entry.get("validity_state"), "❔")
            wr_str = f" [{entry['winrate_tag']}]" if entry.get("winrate_tag") else ""
            decision_label = str(entry.get("decision", "")).replace("_", " ")
            lr = entry.get("live_rank")
            lr_str = f"{lr:.0f}/100" if lr is not None else "-"
            lines.append(f"{icon} {entry['ticker']} — {entry.get('validity_state')}{wr_str} — {decision_label} | Live Rank {lr_str}")

    lines.append("\nDetail lengkap: /check TICKER")
    if staleness_note:
        lines.insert(0, staleness_note)

    buttons = core.build_check_buttons(watchlist + [e["ticker"] for e in offradar])
    await core.safe_reply(update.message, "\n".join(lines), reply_markup=buttons)


async def broksum_command(update, context):
    """
    /broksum KODE [hari] — reverse-lookup broker (MBSS v2, user request,
    direname dari /brokeraktivitas): satu kode broker, saham apa saja yang
    di-akumulasi/distribusi. Baca dari cache broksum_250 yang di-fetch
    SEKALI tiap malam (bagian /eodscan) — TIDAK fetch live sama sekali di
    sini, supaya bisa dipakai berkali-kali sehari tanpa rebutan kuota
    dengan proses nightly.

    MBSS v2 (RapidAPI integration): broksum_250 sekarang gabungan 2 sumber
    — batch Index Alpha (250 ticker berskor tertinggi, kalau kuota masih
    ada) DAN whitelist sweep RapidAPI (broker_engine.
    build_rapidapi_broker_whitelist_sweep — TIDAK dibatasi 250, mencakup
    SEMUA ticker yang disentuh broker whitelist, window rolling 10 hari).
    Makanya cakupan/lookback di sini TIDAK LAGI angka tunggal yang pasti —
    teks tampilan sengaja tidak mengklaim "250 ticker" atau "7 hari" lagi.

    Parameter [hari] SENGAJA tidak mengubah rentang fetch — kalau ada,
    cuma dipakai buat catatan tampilan, bukan parameter fetch baru.
    """
    if not context.args:
        await core.safe_reply(update.message, "Format: /broksum KODE\nContoh: /broksum AK")
        return

    broker_code = context.args[0].upper()
    top_n = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 5

    broksum_data = nightly_engine.load_broksum_250()
    if not broksum_data:
        await core.safe_reply(update.message, "⚠️ Cache BROKSUM 250 belum pernah terisi — jalankan /eodscan dulu.")
        return

    activity = broker_engine.find_broker_activity_across_tickers(broker_code, broksum_data)

    if not activity:
        await core.safe_reply(update.message, f"📋 {broker_code} tidak terdeteksi NET BUY di {len(broksum_data)} ticker yang tercakup malam ini.")
        return

    top_activity = activity[:top_n]
    age_info = nightly_engine.get_broksum_250_age_info()
    age_note = ""
    if age_info and age_info["days_lagging"] > 0:
        age_note = f" ⚠️ Data dari {age_info['last_fetch_date']} ({age_info['days_lagging']} hari lalu, bukan hari ini)"
    lines = [f"💰 TOP {len(top_activity)} AKUMULASI {broker_code} — beberapa hari terakhir (dari {len(activity)} saham net-buy, {len(broksum_data)} ticker tercakup){age_note}\n"]
    for i, a in enumerate(top_activity, 1):
        lines.append(f"{i}. {a['ticker']}, net buy {a['buy_volume_lot']:,} lot, {a['buy_avg_price']:.0f} avg price")
    buttons = core.build_check_buttons([a["ticker"] for a in top_activity])
    await core.safe_reply(update.message, "\n".join(lines), reply_markup=buttons)


# Alias lama, tetap didukung sementara supaya transisi tidak mendadak (MBSS v2)
broker_activity_command = broksum_command


async def broker_discovery_command(update, context):
    """
    /brokerdiscovery — MBSS v2 (RapidAPI integration, user request):
    discovery broker BARU, human-curated, TIDAK PERNAH otomatis. Menampilkan
    broker dari ranking top-broker malam ini (atau cache terakhir) yang
    BELUM ada di SMART_MONEY_BROKER_WHITELIST — user meninjau manual, edit
    sendiri konstantanya di engine/broker.py kalau memang relevan.

    Murni baca cache kalau ada (di-refresh tiap malam ke-10 sebagai bagian
    /eodscan) — cuma fetch live kalau cache benar-benar kosong.
    """
    intel = nightly_engine.load_rapidapi_market_intelligence()
    top_brokers_data = intel.get("top_brokers")
    if not top_brokers_data:
        await core.safe_reply(update.message, "🔎 Mengambil data top broker (belum ada di cache)...")
        top_brokers_data = await asyncio.to_thread(broker_engine.fetch_rapidapi_top_brokers)
    if not top_brokers_data:
        await core.safe_reply(update.message, "⚠️ Data top broker tidak tersedia (kuota habis atau API gagal).")
        return

    broker_list = top_brokers_data.get("list", [])
    candidates = [b for b in broker_list if b.get("code") not in broker_engine.SMART_MONEY_BROKER_WHITELIST]
    if not candidates:
        await core.safe_reply(update.message, "📋 Semua broker teraktif sudah ada di whitelist.")
        return

    group_label_id = {"BROKER_GROUP_FOREIGN": "asing", "BROKER_GROUP_LOCAL": "lokal", "BROKER_GROUP_GOVERNMENT": "pemerintah"}
    lines = [f"🔎 KANDIDAT BROKER BARU — {len(candidates)} broker aktif TAPI belum di whitelist\n"]
    for i, b in enumerate(candidates[:15], 1):
        group_label = group_label_id.get(b.get("group"), "-")
        lines.append(f"{i}. {b.get('code')} — {b.get('name', '-')} ({group_label})")
    lines.append(
        "\n⚠️ Ini ranking TURNOVER (aktivitas transaksi), BUKAN ranking "
        "net-buy — aktif ramai bukan berarti 'smart money'. Tinjau manual, "
        "tambahkan ke SMART_MONEY_BROKER_WHITELIST (engine/broker.py) "
        "kalau memang relevan — tidak ada yang otomatis di sini."
    )
    await core.safe_reply(update.message, "\n".join(lines))
