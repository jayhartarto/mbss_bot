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
import datetime
import copy

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import engine.legacy_core as core
import engine.nightly as nightly_engine
import engine.broker as broker_engine
import engine.scoring as scoring_engine
import engine.market as market_engine
import engine.backbone as backbone_engine
import engine.lane_confidence as lane_confidence
import engine.swing_horizon_confidence as swing_horizon_confidence
import engine.daytrade_hc_confidence as daytrade_hc_confidence


# MBSS v2 (user request 2026-08-27 -- TP1/TP2 individual per ticker, boleh
# muncul di EOD juga untuk semua setup MACD): dipakai FRESH CROSS MOMENTUM/
# ABOVE MOMENTUM (/screendaytrade) & CONTINUATION/VALIDATION/MOMENTUM
# EXTENDED (/hc). current_price=None (belum ada harga live malam hari) --
# selalu tampil WR%, TIDAK PERNAH "Tercapai!" di konteks EOD ini. Return ""
# kalau lane tak didukung (FAST_RECOVERY/EARLY_RECOVERY) atau fitur/ref_price
# tak lengkap -- caller fallback ke teks WR historis statis lama.
def _lane_tp_suffix(lane_tag: str, features: dict, ref_price) -> str:
    if lane_tag not in lane_confidence.SUPPORTED_LANES or not ref_price:
        return ""
    tp_info = lane_confidence.compute_tp1_tp2(lane_tag, features, ref_price)
    if tp_info is None:
        return ""
    return "\n   " + "\n   ".join(lane_confidence.format_tp_lines(tp_info, current_price=None))


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


# MBSS v2 (user request 2026-08-28 -- "fungsi untuk panggil kandidat all
# setup", langsung dari kasus audit whitelist-staleness malam ini): view
# gabungan SEMUA 7 lane MACD (FAST_RECOVERY/EARLY_RECOVERY/ABOVE_MOMENTUM
# dari SDT, FCM juga dari SDT, CONTINUATION/VALIDATION/MOMENTUM_EXTENDED
# dari HC) dalam SATU command, PERSIS kriteria yang sudah dipakai
# /screendaytrade & /hc (REUSE murni via reimplementasi kriteria yang
# sama persis -- kedua command itu mendefinisikan kriterianya sbg
# konstanta LOKAL di dalam fungsi masing2, jadi tidak bisa diimpor
# langsung; kalau kriteria di sana berubah, samakan juga di sini).
#
# Beda penting dari /screendaytrade & /hc: candidate yg confidence
# individual-nya <50% (lane_confidence.should_suppress) TETAP ditampilkan
# di sini, ditandai eksplisit "[suppressed]" -- bukan disembunyikan spt
# di 2 command lain. Tujuannya genuinely diagnostic (real case: audit
# 2026-08-28 -- VERN/INET muncul di /check tapi nihil di /screendaytrade &
# /hc, ternyata karena whitelist stale, bukan suppression -- command ini
# akan langsung kelihatan bedanya kalau soal serupa terjadi lagi).
#
# Murni baca cache /eodscan malam terakhir (load_daily_scan_cache_allow_
# stale), TIDAK fetch apa pun -- instan, sama seperti /hc.
async def all_setup_candidates_command(update, context):
    """
    /allsetup — pandangan gabungan semua 9 lane MACD (PRE tiers, FCM,
    EARLY_VALIDATION, CONTINUATION, VALIDATION, LATE_VALIDATION, MOMENTUM_EXTENDED) dalam satu command, untuk
    audit cepat "kandidat mana yg genuinely ada malam ini di semua setup
    sekaligus" tanpa perlu jalankan /screendaytrade + /hc lalu bandingkan
    manual. Lihat catatan panjang di atas untuk detail kenapa & bedanya
    dari 2 command itu.
    """
    scored, staleness_note = nightly_engine.load_daily_scan_cache_allow_stale()
    if not scored:
        await core.safe_reply(update.message, staleness_note or "⚠️ Cache /eodscan belum pernah ada — jalankan /eodscan dulu.")
        return

    backbone_result, _ = nightly_engine.load_backbone_daily_allow_stale()
    all_values = list(scored.values())
    # PRE tiers + FCM (SDT): PERSIS /screendaytrade, dari gate survivor.
    # CONTINUATION/VALIDATION/MOMENTUM_EXTENDED (HC): PERSIS /hc, TANPA gate filter.
    gate_survivors = backbone_engine.filter_to_gate_survivors(all_values, backbone_result) if backbone_result else all_values

    _DIST_SMA20_MIN = 12.0
    _FCM_MAX_DAYS_AGO = 2
    _FCM_RET10_PRE_MIN = 5.0  # MBSS v2 (user request 2026-08-31): 15%->5%, lihat catatan engine/scanalert.py
    _CONT_MAX_CROSS_DAYS = 5
    _EXT_MIN_DAYS_AGO = 6
    _EXT_MAX_DAYS_AGO = 40
    _EXT_GAP_SLOPE_Q4 = 0.3106
    _EXT_RET1D_MIN = 2.5
    _EARLY_VAL_GAIN_MIN = 2.0
    _EARLY_VAL_GAIN_MAX = 3.0
    _LATE_VAL_GAIN_MIN = 3.0
    _LATE_VAL_MIN_DAYS_AGO = 3
    _LATE_VAL_MAX_DAYS_AGO = 6

    lanes = {name: [] for name in
             ["FAST_RECOVERY", "EARLY_RECOVERY", "ABOVE_MOMENTUM", "FCM", "EARLY_VALIDATION",
              "CONTINUATION", "VALIDATION", "LATE_VALIDATION", "MOMENTUM_EXTENDED"]}

    for r in gate_survivors:
        tier = r.get("macd_approach_tier")
        if tier in lanes:
            lanes[tier].append(r)
        if (
            r.get("macd_cross_direction") == "bullish" and r.get("macd_cross_days_ago") is not None
            and r["macd_cross_days_ago"] <= _FCM_MAX_DAYS_AGO
            and r.get("macd_ret10_pre_cross_pct") is not None and r["macd_ret10_pre_cross_pct"] > _FCM_RET10_PRE_MIN
        ):
            lanes["FCM"].append(r)

    for r in all_values:
        gain = r.get("macd_gain_since_cross_pct")
        dist_sma20 = r.get("price_vs_sma20_pct")
        # EARLY_VALIDATION (lane ke-8, 2026-08-31): TANPA gate dist_sma20,
        # SENGAJA -- lihat catatan MACD_EARLY_VALIDATION_GAIN_MIN_PCT di
        # engine/scanalert.py utk alasan & backtest.
        if (
            r.get("macd_cross_direction") == "bullish" and r.get("macd_cross_days_ago") is not None
            and r["macd_cross_days_ago"] <= _CONT_MAX_CROSS_DAYS
            and gain is not None and _EARLY_VAL_GAIN_MIN <= gain < _EARLY_VAL_GAIN_MAX
        ):
            lanes["EARLY_VALIDATION"].append(r)
        caught_by_cont_val = False
        if (
            r.get("macd_cross_direction") == "bullish" and r.get("macd_cross_days_ago") is not None
            and r["macd_cross_days_ago"] <= _CONT_MAX_CROSS_DAYS
            and gain is not None and dist_sma20 is not None and dist_sma20 >= _DIST_SMA20_MIN
        ):
            if 6.0 <= gain < 10.0:
                lanes["CONTINUATION"].append(r)
                caught_by_cont_val = True
            elif 3.0 <= gain < 6.0:
                lanes["VALIDATION"].append(r)
                caught_by_cont_val = True
        # LATE_VALIDATION (lane ke-9, 2026-08-31): TANPA gate dist_sma20,
        # hari 3-6 (BEDA dari EARLY_VALIDATION 1-5) -- HANYA kalau belum
        # tercover CONTINUATION/VALIDATION (dist>=12% & gain 3-10%, hari
        # <=5). Lihat catatan MACD_LATE_VALIDATION_GAIN_MIN_PCT di
        # engine/scanalert.py.
        if (
            not caught_by_cont_val
            and r.get("macd_cross_direction") == "bullish" and r.get("macd_cross_days_ago") is not None
            and _LATE_VAL_MIN_DAYS_AGO <= r["macd_cross_days_ago"] <= _LATE_VAL_MAX_DAYS_AGO
            and gain is not None and gain >= _LATE_VAL_GAIN_MIN
        ):
            lanes["LATE_VALIDATION"].append(r)
        if (
            r.get("macd_regime") == "ABOVE_CENTERLINE" and r.get("macd_episode_had_volume_breakout") is True
            and r.get("macd_cross_days_ago") is not None and _EXT_MIN_DAYS_AGO <= r["macd_cross_days_ago"] <= _EXT_MAX_DAYS_AGO
            and r.get("macd_gap_slope_3d") is not None and r["macd_gap_slope_3d"] >= _EXT_GAP_SLOPE_Q4
            and r.get("ret_1d_pct") is not None and r["ret_1d_pct"] > _EXT_RET1D_MIN
        ):
            lanes["MOMENTUM_EXTENDED"].append(r)

    total = sum(len(v) for v in lanes.values())
    lines = [f"🌐 ALL SETUP MACD — {total} kandidat total dari {len(scored)} ticker discan malam terakhir"]
    if staleness_note:
        lines.insert(0, staleness_note)
    lines.append(
        "Gabungan 9 lane (SDT pra-breakout + HC continuation/validation/extended), PERSIS kriteria "
        "/screendaytrade & /hc. Beda dari 2 command itu: candidate confidence individual <50% TETAP "
        "muncul di sini, ditandai [suppressed] -- bukan disembunyikan.\n"
    )

    LANE_EMOJI = {
        "FAST_RECOVERY": "⚡", "EARLY_RECOVERY": "🔄", "ABOVE_MOMENTUM": "📐", "FCM": "🔥",
        "EARLY_VALIDATION": "🌱", "CONTINUATION": "📈", "VALIDATION": "📊",
        "LATE_VALIDATION": "🍂", "MOMENTUM_EXTENDED": "🚀",
    }
    STATIC_WR = {
        "FAST_RECOVERY": "hit6~62.4%/hit10~38.8% (n=85)",
        "EARLY_RECOVERY": "hit6~59.2%/hit10~43.1% (derivasi, n≈262)",
        # MBSS v2 2026-08-31: bukan hit6 (belum ada model lane_confidence) --
        # peluang lanjut ke gain_since_cross>=4% (zona VALIDATION) dlm 1-2h.
        "EARLY_VALIDATION": "~33% lanjut ke VALIDATION dlm 1-2h (n=661, vs baseline pasar 13.4%)",
    }

    for lane_name, candidates in lanes.items():
        lines.append(f"\n{LANE_EMOJI[lane_name]} {lane_name} ({len(candidates)})")
        if not candidates:
            lines.append("  (kosong)")
            continue
        for r in candidates:
            t = r["ticker"]
            price = r.get("price")
            if lane_name == "FCM":
                features = {"ret10_pre_cross_pct": r.get("macd_ret10_pre_cross_pct"), "pct_b": r.get("pct_b")}
                detail = f"cross {r.get('macd_cross_days_ago', '-')}h lalu, pre-cross +{(r.get('macd_ret10_pre_cross_pct') or 0):.1f}%"
            elif lane_name in ("CONTINUATION", "VALIDATION"):
                features = {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b")}
                detail = f"cross {r.get('macd_cross_days_ago', '-')}h lalu, +{(r.get('macd_gain_since_cross_pct') or 0):.1f}% sejak cross"
            elif lane_name == "LATE_VALIDATION":
                features = {
                    "gain_since_cross": r.get("macd_gain_since_cross_pct"),
                    "dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b"),
                }
                detail = f"cross {r.get('macd_cross_days_ago', '-')}h lalu, +{(r.get('macd_gain_since_cross_pct') or 0):.1f}% sejak cross"
            elif lane_name == "MOMENTUM_EXTENDED":
                features = {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b"), "gap_slope_3d": r.get("macd_gap_slope_3d")}
                detail = f"episode {r.get('macd_cross_days_ago', '-')}h, +{(r.get('ret_1d_pct') or 0):.1f}% hari ini"
            elif lane_name == "ABOVE_MOMENTUM":
                features = {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b")}
                detail = "pre-cross, momentum menguat"
            elif lane_name == "EARLY_VALIDATION":  # tak didukung lane_confidence, spt FAST_RECOVERY/EARLY_RECOVERY
                features = None
                detail = f"cross {r.get('macd_cross_days_ago', '-')}h lalu, +{(r.get('macd_gain_since_cross_pct') or 0):.1f}% sejak cross"
            else:  # FAST_RECOVERY/EARLY_RECOVERY -- tak didukung lane_confidence
                features = None
                detail = "pre-cross, BELOW centerline"

            line = f"  • {t} — {detail} | Harga {price}"
            if features is not None:
                if lane_confidence.should_suppress(lane_name, features, price):
                    line += " [suppressed: confidence individual <50%]"
                else:
                    tp = lane_confidence.compute_tp1_tp2(lane_name, features, price) if price else None
                    if tp:
                        line += "\n     " + " / ".join(lane_confidence.format_tp_lines(tp))
                    else:
                        line += " [confidence individual belum bisa dihitung -- data tak lengkap]"
            else:
                line += f" | {STATIC_WR.get(lane_name, '')}"
            lines.append(line)

    all_tickers = [r["ticker"] for cands in lanes.values() for r in cands]
    buttons = core.build_check_buttons(all_tickers)
    await core.safe_reply(update.message, "\n".join(lines), reply_markup=buttons)


# MBSS v2 (Lane Lifecycle Redesign, user request 2026-08-29/30, riset
# research/MBSS_Lane_Lifecycle_Redesign_Brief.md): dist_to_20d_high_pct
# di cache PRODUKSI = (high_20d-price)/price*100 (POSITIF kalau di
# bawah high) -- engine/swing_horizon_confidence.py dilatih pakai
# (price/high_20d-1)*100 (NEGATIF kalau di bawah high, denominator beda
# jg). Konversi presisi (BUKAN flip tanda naif -- denominator beda):
# high_20d = price*(1+field/100), lalu recompute rumus training persis.
# Ketahuan SEBELUM wiring produksi -- kalau salah, prediksi model
# TERBALIK (jauh dari high dibaca seolah dekat).
def _map_features_for_swing_horizon(r: dict) -> dict:
    dist_prod = r.get("dist_to_20d_high_pct")
    dist_converted = None
    if dist_prod is not None:
        dist_converted = -dist_prod / (1 + dist_prod / 100.0)
    return {
        "dist_to_20d_high": dist_converted,
        "adx14": r.get("adx"),
        "gain_since_cross": r.get("macd_gain_since_cross_pct"),
        "days_ago": r.get("macd_cross_days_ago"),
    }


_DAYTRADE_WR_MIN_VALUE_TRADED = 500_000_000
_DAYTRADE_WR_MIN_PRICE = 50


def _daytrade_wr_tp1(r: dict) -> dict | None:
    """
    Floor likuiditas + daytrade_hc_confidence.compute_tp1() utk SATU ticker
    -- HELPER TUNGGAL (MBSS v2, user request 2026-08-30: "drop saja hc yang
    lama, toh WR nya juga tidak korelasi?") dipakai /go, /hc,
    compute_consensus_candidates, _consensus_sdt_hc_selected -- GANTI TOTAL
    is_high_conviction/compute_high_conviction_score (Minervini 5-6 kriteria)
    di SEMUA consumer produksi. Alasan: audit Task #21 menemukan kriteria
    breakout_close_confirmed dead code STRUKTURAL (resistance dihitung dari
    window yang SAMA dengan hari observasi sendiri, jadi resistance >=
    High(hari ini) >= price SELALU -- 0 True dari 32.719 observasi
    tervalidasi), dan 5 kriteria sisanya saling BERLAWANAN arah
    (consolidation_tight/near_high positif ke close-based return tapi
    negatif ke touch-rate; relative_volume_ok kebalikannya; above_ma20_
    and_ma50 murni noise corr~0.00) -- makanya aggregate criteria_met
    near-zero (corr -0.018 same-day / 0.02 horizon asli HC 5-hari), BUKAN
    sekadar 1 kriteria buggy. Model regresi logistik yang dikalibrasi
    LANGSUNG ke satu target (top-decile WR 69.2%, AUC 0.611, lihat
    engine/daytrade_hc_confidence.py) jauh lebih koheren daripada sum vote
    kriteria yang arahnya campur aduk. compute_high_conviction_score TIDAK
    dihapus dari scoring.py (masih dihitung tiap /eodscan, field legacy
    tanpa consumer produksi lagi) -- keputusan hapus fungsinya sepenuhnya
    ditunda sampai genuinely yakin tidak ada rencana pakai lagi.
    Return tp1_info ({"level_pct","wr_pct","price"}) atau None kalau tidak
    lolos floor likuiditas ATAU tidak lolos floor WR 60% model.
    """
    price = r.get("price")
    value_traded = r.get("value_traded")
    if not price or price <= _DAYTRADE_WR_MIN_PRICE or not value_traded or value_traded <= _DAYTRADE_WR_MIN_VALUE_TRADED:
        return None
    dt_features = {
        "day_range_pct_10d": r.get("day_range_pct_10d"), "vol_ratio": r.get("vol_ratio"),
        "value_traded": value_traded, "relative_strength_vs_ihsg": r.get("relative_strength_vs_ihsg"),
    }
    return daytrade_hc_confidence.compute_tp1(dt_features, price)


def _compute_daytrade_wr_candidates(pool: list) -> list:
    """[(r, tp1_info), ...] terurut WR menurun -- lihat _daytrade_wr_tp1 utk alasan/scoping."""
    out = [(r, tp1) for r in pool for tp1 in [_daytrade_wr_tp1(r)] if tp1]
    out.sort(key=lambda pair: pair[1]["wr_pct"], reverse=True)
    return out


async def go_command(update, context):
    """
    /go -- dashboard gabungan DAY TRADE + SWING TRADE (MBSS Lane
    Lifecycle Redesign, user request 2026-08-29/30, research/MBSS_Lane_
    Lifecycle_Redesign_Brief.md). BSJP SENGAJA tidak ikut -- intraday-
    only (Fase 1 /bsjp akhir sesi 1 + Fase 2 recheck otomatis 09:30-15:50,
    lihat engine/scanalert.py run_bsjp_shortlist_scan/run_bsjp_recheck_
    once), tidak cocok utk command "tarik semua sekarang" yg jalan kapan
    saja.

    DAY TRADE = EOD High Conviction (kandidat HC malam ini, "beli open
    besok, jual hari yang sama", TP1/SL dari targets yang SUDAH ada di
    pipeline HC -- tidak ada model baru).

    SWING TRADE = SATU timeline gabungan lifecycle post-cross (PRE-CROSS/
    FCM/CONTINUATION/VALIDATION/MOMENTUM_EXTENDED disatukan, BUKAN
    kotak2 section terpisah spt /allsetup) -- tiap ticker tampil dgn
    STATE SEKARANG saja (riwayat transisi perlu episode-tracking yg
    SENGAJA belum dibangun, lihat diskusi arsitektur sesi ini) + TP1/TP2/
    SL dari engine/lane_confidence.py (model lane LAMA, sudah proven,
    TETAP primary) + tabel "Ekspektasi cepat" horizon SHORT(1-2D)/
    MEDIUM(1-3D)/SWING(1-10D) dari engine/swing_horizon_confidence.py
    (model BARU, unified lintas seluruh lifecycle, PELENGKAP bukan
    pengganti TP1/TP2 di atas -- lihat catatan arsitektur "old model utk
    TP1/TP2 konkret, new model utk ekspektasi horizon").
    """
    scored, staleness_note = nightly_engine.load_daily_scan_cache_allow_stale()
    if not scored:
        await core.safe_reply(update.message, staleness_note or "⚠️ Cache /eodscan belum pernah ada — jalankan /eodscan dulu.")
        return

    backbone_result, _ = nightly_engine.load_backbone_daily_allow_stale()
    all_values = list(scored.values())
    gate_survivors = backbone_engine.filter_to_gate_survivors(all_values, backbone_result) if backbone_result else all_values

    lines = ["🧭 /go — DAY TRADE & SWING TRADE"]
    if staleness_note:
        lines.insert(0, staleness_note)

    # ---------- DAY TRADE (MBSS v2, user request 2026-08-30, REVISI --
    # is_high_conviction gate DIBUANG: criteria_met terbukti TIDAK
    # prediktif thd same-day (corr=-0.018) MAUPUN horizon asli HC 5-hari
    # (corr=0.02), sementara full-universe + model baru (day_range_pct_
    # 10d/vol_ratio/value_traded/relative_strength_vs_ihsg) tervalidasi
    # 6.4x lebih banyak kandidat/hari (185.8 vs 28.9) di kualitas top-
    # decile yg SAMA (67.6% vs 69.2%) & AUC malah lebih baik (0.684 vs
    # 0.611) -- lihat riwayat chat 2026-08-30. Populasi sekarang: SELURUH
    # universe scored, basic liquidity floor saja (bukan lagi kriteria
    # Minervini) -- model sendiri yg jadi gate via floor WR>=60%
    # (compute_tp1 return None kalau tak lolos, TIDAK ditampilkan).
    # Backlog audit HC individual criteria (TaskList #21) SELESAI 2026-08-30
    # -- is_high_conviction sekarang DIGANTI TOTAL (bukan cuma di /go) oleh
    # _daytrade_wr_tp1/_compute_daytrade_wr_candidates, lihat docstring
    # helper itu utk alasan lengkap. Populasi: SELURUH universe scored,
    # basic liquidity floor saja -- model sendiri yg jadi gate via floor
    # WR>=60% (None kalau tak lolos, TIDAK ditampilkan).
    daytrade_candidates = _compute_daytrade_wr_candidates(all_values)

    lines.append(f"\n\nDAY TRADE ({len(daytrade_candidates)})")
    if daytrade_candidates:
        lines.append(f"⚠️ {daytrade_hc_confidence.DAYTRADE_EXIT_WARNING}")
    if not daytrade_candidates:
        lines.append("  (kosong malam ini)")
    else:
        for r, tp1_info in daytrade_candidates:
            t = r["ticker"]; price = r.get("price")
            targets = r.get("targets") or {}
            cut_loss = targets.get("cut_loss")
            block = [
                f"  • {t} — {price}",
                f"     Setup: EOD High Conviction | Entry {price} (ref: open besok)",
                f"     TP1 {tp1_info['price']:,.0f} / +{tp1_info['level_pct']:.1f}% (WR {tp1_info['wr_pct']:.0f}%)" + (f" | SL {cut_loss}" if cut_loss else ""),
            ]
            lines.append("\n".join(block))
    hc_candidates = [r for r, _ in daytrade_candidates]  # dipakai buttons di akhir command

    # ---------- SWING TRADE (unified lifecycle -- REUSE kriteria PERSIS
    # /allsetup, cuma ditampilkan SATU section bukan 7 blok terpisah) ----------
    _DIST_SMA20_MIN = 12.0
    _FCM_MAX_DAYS_AGO = 2
    _FCM_RET10_PRE_MIN = 5.0  # MBSS v2 (user request 2026-08-31): 15%->5%, lihat catatan engine/scanalert.py
    _CONT_MAX_CROSS_DAYS = 5
    _EXT_MIN_DAYS_AGO = 6
    _EXT_MAX_DAYS_AGO = 40
    _EXT_GAP_SLOPE_Q4 = 0.3106
    _EXT_RET1D_MIN = 2.5
    _EARLY_VAL_GAIN_MIN = 2.0
    _EARLY_VAL_GAIN_MAX = 3.0
    _LATE_VAL_GAIN_MIN = 3.0
    _LATE_VAL_MIN_DAYS_AGO = 3
    _LATE_VAL_MAX_DAYS_AGO = 6

    swing_candidates = []  # [(lane_name, r)]
    for r in gate_survivors:
        tier = r.get("macd_approach_tier")
        if tier in ("FAST_RECOVERY", "EARLY_RECOVERY", "ABOVE_MOMENTUM"):
            swing_candidates.append((tier, r))
        if (
            r.get("macd_cross_direction") == "bullish" and r.get("macd_cross_days_ago") is not None
            and r["macd_cross_days_ago"] <= _FCM_MAX_DAYS_AGO
            and r.get("macd_ret10_pre_cross_pct") is not None and r["macd_ret10_pre_cross_pct"] > _FCM_RET10_PRE_MIN
        ):
            swing_candidates.append(("FCM", r))
    for r in all_values:
        gain = r.get("macd_gain_since_cross_pct")
        dist_sma20 = r.get("price_vs_sma20_pct")
        # EARLY_VALIDATION (lane ke-8, 2026-08-31): TANPA gate dist_sma20,
        # SENGAJA -- lihat catatan MACD_EARLY_VALIDATION_GAIN_MIN_PCT di
        # engine/scanalert.py.
        if (
            r.get("macd_cross_direction") == "bullish" and r.get("macd_cross_days_ago") is not None
            and r["macd_cross_days_ago"] <= _CONT_MAX_CROSS_DAYS
            and gain is not None and _EARLY_VAL_GAIN_MIN <= gain < _EARLY_VAL_GAIN_MAX
        ):
            swing_candidates.append(("EARLY_VALIDATION", r))
        caught_by_cont_val = False
        if (
            r.get("macd_cross_direction") == "bullish" and r.get("macd_cross_days_ago") is not None
            and r["macd_cross_days_ago"] <= _CONT_MAX_CROSS_DAYS
            and gain is not None and dist_sma20 is not None and dist_sma20 >= _DIST_SMA20_MIN
        ):
            if 6.0 <= gain < 10.0:
                swing_candidates.append(("CONTINUATION", r))
                caught_by_cont_val = True
            elif 3.0 <= gain < 6.0:
                swing_candidates.append(("VALIDATION", r))
                caught_by_cont_val = True
        # LATE_VALIDATION (lane ke-9, 2026-08-31): TANPA gate dist_sma20,
        # hari 3-6, HANYA kalau belum tercover CONTINUATION/VALIDATION --
        # lihat catatan MACD_LATE_VALIDATION_GAIN_MIN_PCT di engine/scanalert.py.
        if (
            not caught_by_cont_val
            and r.get("macd_cross_direction") == "bullish" and r.get("macd_cross_days_ago") is not None
            and _LATE_VAL_MIN_DAYS_AGO <= r["macd_cross_days_ago"] <= _LATE_VAL_MAX_DAYS_AGO
            and gain is not None and gain >= _LATE_VAL_GAIN_MIN
        ):
            swing_candidates.append(("LATE_VALIDATION", r))
        if (
            r.get("macd_regime") == "ABOVE_CENTERLINE" and r.get("macd_episode_had_volume_breakout") is True
            and r.get("macd_cross_days_ago") is not None and _EXT_MIN_DAYS_AGO <= r["macd_cross_days_ago"] <= _EXT_MAX_DAYS_AGO
            and r.get("macd_gap_slope_3d") is not None and r["macd_gap_slope_3d"] >= _EXT_GAP_SLOPE_Q4
            and r.get("ret_1d_pct") is not None and r["ret_1d_pct"] > _EXT_RET1D_MIN
        ):
            swing_candidates.append(("MOMENTUM_EXTENDED", r))

    lines.append(f"\n\nSWING TRADE ({len(swing_candidates)})")
    if not swing_candidates:
        lines.append("  (kosong malam ini)")
    else:
        for lane_name, r in swing_candidates:
            t = r["ticker"]; price = r.get("price")
            block = [f"  • {t} — {price}", f"     Setup: {lane_name}"]

            targets = r.get("targets") or {}
            cut_loss = targets.get("cut_loss")

            if lane_name in lane_confidence.SUPPORTED_LANES:
                if lane_name == "FCM":
                    features = {"ret10_pre_cross_pct": r.get("macd_ret10_pre_cross_pct"), "pct_b": r.get("pct_b")}
                elif lane_name == "MOMENTUM_EXTENDED":
                    features = {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b"), "gap_slope_3d": r.get("macd_gap_slope_3d")}
                elif lane_name == "LATE_VALIDATION":
                    features = {
                        "gain_since_cross": r.get("macd_gain_since_cross_pct"),
                        "dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b"),
                    }
                else:
                    features = {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b")}
                if price and lane_confidence.should_suppress(lane_name, features, price):
                    block.append("     [confidence individual <50%, HATI-HATI]")
                else:
                    tp = lane_confidence.compute_tp1_tp2(lane_name, features, price) if price else None
                    if tp:
                        block.append("     " + " / ".join(lane_confidence.format_tp_lines(tp)))
            if cut_loss:
                block.append(f"     SL {cut_loss}")

            # Ekspektasi cepat (pelengkap, model BARU) -- semua lane termasuk
            # FAST_RECOVERY/EARLY_RECOVERY yg tak didukung lane_confidence.
            if price:
                sh_features = _map_features_for_swing_horizon(r)
                expectation = swing_horizon_confidence.compute_horizon_expectation(sh_features, price)
                exp_lines = swing_horizon_confidence.format_expectation_lines(expectation)
                if exp_lines:
                    block.append("     Ekspektasi cepat (pelengkap, bukan pengganti TP di atas):")
                    block.extend(f"       {ln}" for ln in exp_lines)

            lines.append("\n".join(block))

    all_tickers = [r["ticker"] for r in hc_candidates] + [r["ticker"] for _, r in swing_candidates]
    buttons = core.build_check_buttons(sorted(set(all_tickers)))
    await core.safe_reply(update.message, "\n\n".join(lines), reply_markup=buttons)


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

    # MBSS v2 (user request — SDT redesain total: hapus radar "sudah
    # breakout" [dulu section 1], fokus 100% pra-breakout; macd_approach_tier
    # ganti total ke lane Brights-compatible FAST_RECOVERY/EARLY_RECOVERY/
    # ABOVE_MOMENTUM [lihat compute_factor_scoring]; EXPLOSIVE jadi section
    # TERPISAH [bukan digabung ke pra-breakout] berisi subset FAST3
    # [FAST_RECOVERY ∪ EARLY_RECOVERY, BELOW_CENTERLINE] -- tervalidasi
    # Hit+10% D5 TERTINGGI di antara semua lane fully-specified [BELOW_FAST3
    # 42.07%, n=347; EARLY_RECOVERY sendiri malah lebih tinggi dari
    # FAST_RECOVERY -- "near" mempertajam timing bukan conversion mentah,
    # lihat catatan compute_factor_scoring]; section terpisah "HIGH
    # PROBABILITY PICKS" [dulu pool sempit Q4/Q5] DIHAPUS -- probability_
    # score Backbone (sudah termasuk bonus whitelist net-buy smart money)
    # dipakai sbg tie-break urutan di SEMUA section, bukan list sendiri.
    # Universe TIDAK di-cap harga (floor only dari whitelist bulanan,
    # results sudah warisi itu) -- jangan tambah cap baru di sini.
    LANE_INFO = {
        # hit6/hit10 = Hit +6%/+10% D5 (n dari research/brights_imminent_
        # cross_backtest_v1.py + imminent_cross_episode_hypothesis_v2.py,
        # proper development->calibration->validation split). EARLY_RECOVERY
        # DERIVED (BELOW_FAST3 dikurangi BELOW_NEAR_FAST3, n=347-85=262) --
        # bukan langsung dari 1 backtest run terpisah, tapi aljabar murni
        # dari 2 angka yang SUDAH published (bukan tebakan baru).
        "FAST_RECOVERY": {"order": 0, "label": "FAST RECOVERY", "hit6": 62.35, "hit10": 38.82, "n": 85, "derived": False},
        "EARLY_RECOVERY": {"order": 1, "label": "EARLY RECOVERY", "hit6": 59.2, "hit10": 43.1, "n": 262, "derived": True},
        # MBSS v2 (user request 2026-08-27 -- riset conditional filter dist_
        # to_sma20>=12%, 576 ISSI/2thn): angka LAMA (40.98/27.87, n=61) DIGANTI
        # -- sejak gate dist_to_sma20>=12% ditambahkan ke ABOVE_MOMENTUM di
        # engine/scoring.py (macd_approach_tier), populasi produksi SEKARANG
        # adalah subset yang lolos gate itu (hit6 65.3%/hit10 50.7%, n=300),
        # BUKAN lagi populasi lama 41%/n=61 -- update ini WAJIB konsisten dgn
        # kriteria produksi aktual, bukan cuma kosmetik.
        "ABOVE_MOMENTUM": {"order": 2, "label": "ABOVE MOMENTUM", "hit6": 65.3, "hit10": 50.7, "n": 300, "derived": False},
    }

    def _bb_prob(ticker):
        bb = (backbone_result or {}).get("all_scored", {}).get(ticker) if backbone_result else None
        return bb.get("probability_score", 0) if bb else 0

    # MBSS v2 (user request 2026-08-27 -- confidence individual per ticker,
    # suppress kalau <50% di level terdekat): HANYA berlaku utk ABOVE_MOMENTUM
    # (satu2nya lane didukung lane_confidence di antara 3 lane pra-breakout
    # ini) -- FAST_RECOVERY/EARLY_RECOVERY TIDAK disaring (belum didukung).
    def _above_momentum_suppressed(r):
        if r.get("macd_approach_tier") != "ABOVE_MOMENTUM":
            return False
        ref_price = r.get("price")
        if not ref_price:
            return False
        features = {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b")}
        return lane_confidence.should_suppress("ABOVE_MOMENTUM", features, ref_price)

    lane_candidates = [r for r in results if r.get("macd_approach_tier") in LANE_INFO and not _above_momentum_suppressed(r)]
    lane_candidates.sort(key=lambda r: (LANE_INFO[r["macd_approach_tier"]]["order"], -_bb_prob(r["ticker"])))

    if sort_mode:
        def _bb(r):
            return (backbone_result or {}).get("all_scored", {}).get(r["ticker"], {})
        if sort_mode == "rank":
            lane_candidates.sort(key=lambda r: (_bb(r).get("entry_rank") is None, _bb(r).get("entry_rank") or 0))
        elif sort_mode == "prob":
            lane_candidates.sort(key=lambda r: (_bb(r).get("probability_score") is not None, _bb(r).get("probability_score") or -1), reverse=True)
        else:  # danger
            lane_candidates.sort(key=lambda r: (_bb(r).get("predicted_danger") is None, _bb(r).get("predicted_danger") or 0))

    DANGER_WARNING_THRESHOLD = 50.0  # skala 0-100 compute_danger_score, di atas rata-rata malam ini secara kasar

    def _format_candidate_line(r, bullet="  • "):
        lane = r["macd_approach_tier"]
        info = LANE_INFO[lane]
        targets = r.get("targets") or {}
        price = r.get("price")
        tp1 = targets.get("tp_1")
        sl = targets.get("cut_loss")
        rr_now = backbone_engine.compute_rr_at_current_price(r)
        bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None

        extra = []
        sm_pct = r.get("whitelist_accumulation_net_pct")
        sm_brokers = r.get("whitelist_num_brokers") or 0
        if sm_pct is not None and sm_pct >= 15 and sm_brokers >= 2:
            extra.append(f"\n   💰 Smart money: net-buy {sm_pct:+.0f}% ({sm_brokers} broker whitelist)")
        if bb_info and bb_info.get("predicted_danger") is not None and bb_info["predicted_danger"] >= DANGER_WARNING_THRESHOLD:
            extra.append(f"\n   ⚠️ Danger score {bb_info['predicted_danger']:.0f}/100 (di atas rata-rata malam ini)")

        n_note = " (derivasi 2 angka published)" if info["derived"] else ""
        rr_str = f"{rr_now:.2f}" if rr_now is not None else "-"
        tp_suffix = _lane_tp_suffix(lane, {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b")}, price)
        header = (
            f"{bullet}{r['ticker']} — {info['label']} (confidence individual di bawah){tp_suffix}"
            if tp_suffix else
            f"{bullet}{r['ticker']} — {info['label']} (potensi +6% ~{info['hit6']:.0f}% / +10% ~{info['hit10']:.0f}% dlm 5 hari, n={info['n']}{n_note})"
        )
        return (
            f"{header}\n"
            f"   Entry ~{price} (ref: open sesi berikutnya) | TP {tp1} (+6%) | SL {sl} | RR {rr_str}"
            f"{''.join(extra)}{market_engine.format_sector_tag(r.get('sector'))}"
        )

    lines = ["🎯 SCREENING DAY TRADE — SETUP PRA-BREAKOUT\n"]
    if backbone_staleness:
        lines.insert(0, backbone_staleness)
    lines.append(
        "Kriteria: MACD belum cross (pre-cross) tapi gap ke Signal line menyempit -- 3 lane, "
        "lihat research/mbss_macd_production_research_bundle/. Potensi% = Hit-rate historis lane ini "
        "(BUKAN skor individual ticker), n = jumlah episode tervalidasi.\n"
    )
    lines.append("Catatan: Ini RADAR, bukan entry final. Verifikasi live sebelum entry.\n")

    if not lane_candidates:
        lines.append("Tidak ada kandidat pra-breakout malam ini.")
    else:
        for r in lane_candidates:
            lines.append(_format_candidate_line(r, bullet=""))

        await asyncio.to_thread(
            core.lock_daily_daytrade_picks, lane_candidates, "screendaytrade_macd_lane",
            (backbone_result or {}).get("all_scored", {})
        )

    # EXPLOSIVE -- section TERPISAH (bukan digabung pra-breakout di atas),
    # subset FAST3 (BELOW_CENTERLINE, gap_slope_3d cepat) yg Hit+10% D5
    # paling tinggi di antara semua lane fully-specified.
    explosive_candidates = [r for r in lane_candidates if r["macd_approach_tier"] in ("FAST_RECOVERY", "EARLY_RECOVERY")]
    lines.append("\n🚀 EXPLOSIVE — konversi ke +10% tertinggi (FAST_RECOVERY ∪ EARLY_RECOVERY, BELOW centerline)\n")
    if not explosive_candidates:
        lines.append("Tidak ada kandidat explosive malam ini.")
    else:
        for r in explosive_candidates:
            lines.append(_format_candidate_line(r))
        await asyncio.to_thread(
            core.lock_daily_daytrade_picks, explosive_candidates, "screendaytrade_explosive",
            (backbone_result or {}).get("all_scored", {})
        )

    # MBSS v2 (user request 2026-08-24, dari research/macd_research_complete/
    # research_bundle/ -- "Entry = close on MACD bullish-cross day"): fresh
    # cross (hari ini/kemarin) DENGAN konfirmasi pre-cross momentum kuat
    # (trailing 10 hari SEBELUM cross >15%) -- sinyal "act TODAY", beda dari
    # lane pra-breakout di atas (yg justru BELUM cross) dan beda dari HC
    # CONTINUATION/VALIDATION (yg butuh gain PASCA-cross). Divalidasi mandiri
    # 576 ISSI/2thn, n=7816 fresh-cross event gated persis di hari cross:
    # hubungan MONOTON bersih thd ret10_pre_cross, tidak ada plateau/sweet-
    # spot -- >15% jadi ambang produksi (hit6 61-81% tergantung sub-bucket,
    # vs baseline fresh-cross murni 36.28%). MAE selama hold (trade yg
    # eventually hit +6%): median cuma -4.01%, closing di hari low terdalam
    # rata2 SUDAH +2.32% (53% kasus closing hari itu positif) -- pullback
    # intraday sering cuma kaget sehari, bukan breakdown genuine. Kandidat
    # di sini kandidat alami utk live intraday pullback-alert (belum
    # dibangun, diskusi terpisah).
    #
    # cross_days_ago<=2 (bukan <=1) -- user request, dites: evaluated persis
    # di hari 0/1/2 pasca-cross (bukan cuma cross_days_ago==2 sendirian),
    # performa STABIL tanpa decay (hit6 71.1%/69.3%/70.0% di hari 0/1/2,
    # n=890 masing2) -- window gabungan <=2 kasih +50% volume (n=2670 vs
    # 1780) nyaris tanpa kehilangan kualitas (hit6 70.15% vs 70.22%, hit10
    # 56.67% vs 57.02%).
    MACD_FRESH_CROSS_MOMENTUM_MAX_DAYS_AGO = 2
    # MBSS v2 (user request 2026-08-31): 15%->5%, lihat catatan lengkap
    # MACD_FRESH_CROSS_MOMENTUM_RET10_PRE_MIN di engine/scanalert.py.
    MACD_FRESH_CROSS_MOMENTUM_RET10_PRE_MIN = 5.0
    fresh_cross_momentum_candidates = [
        r for r in results
        if r.get("macd_cross_direction") == "bullish"
        and r.get("macd_cross_days_ago") is not None
        and r["macd_cross_days_ago"] <= MACD_FRESH_CROSS_MOMENTUM_MAX_DAYS_AGO
        and r.get("macd_ret10_pre_cross_pct") is not None
        and r["macd_ret10_pre_cross_pct"] > MACD_FRESH_CROSS_MOMENTUM_RET10_PRE_MIN
    ]
    fresh_cross_momentum_candidates.sort(key=lambda r: r["macd_ret10_pre_cross_pct"], reverse=True)
    # MBSS v2 (user request 2026-08-27 -- suppress kalau confidence individual <50% di level terdekat.
    # BUGFIX sore ini: should_suppress (BUKAN compute_tp1_tp2(...) is None) -- fitur tak lengkap
    # [mis. cache /eodscan lama sebelum pct_b ada] harus TIDAK disuppress, lihat lane_confidence.py)
    fresh_cross_momentum_candidates = [
        r for r in fresh_cross_momentum_candidates
        if not lane_confidence.should_suppress(
            "FCM", {"ret10_pre_cross_pct": r.get("macd_ret10_pre_cross_pct"), "pct_b": r.get("pct_b")}, r.get("price")
        )
    ]
    fresh_cross_momentum_candidates = fresh_cross_momentum_candidates[:8]

    lines.append("\n🔥 FRESH CROSS MOMENTUM — cross MACD hari ini/kemarin, momentum kuat sebelum cross (>15% dlm 10 hari) — sinyal 'act hari ini'\n")
    if not fresh_cross_momentum_candidates:
        lines.append("Tidak ada kandidat fresh cross momentum malam ini.")
    else:
        for r in fresh_cross_momentum_candidates:
            targets = r.get("targets") or {}
            price = r.get("price")
            tp1 = targets.get("tp_1")
            sl = targets.get("cut_loss")
            bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
            danger_note = ""
            if bb_info and bb_info.get("predicted_danger") is not None and bb_info["predicted_danger"] >= DANGER_WARNING_THRESHOLD:
                danger_note = f"\n   ⚠️ Danger score {bb_info['predicted_danger']:.0f}/100 (di atas rata-rata malam ini)"
            tp_suffix = _lane_tp_suffix("FCM", {"ret10_pre_cross_pct": r.get("macd_ret10_pre_cross_pct"), "pct_b": r.get("pct_b")}, price)
            tp_entry_note = "" if tp_suffix else " | TP {} (+6%)".format(tp1)  # fallback -- confidence individual tak bisa dihitung (fitur tak lengkap), bukan disuppress
            lines.append(
                f"  {r['ticker']} — cross {r.get('macd_cross_days_ago', 0)} hari lalu, momentum pre-cross +{r['macd_ret10_pre_cross_pct']:.1f}% (10 hari){tp_suffix}\n"
                f"   Entry ~{price} (ref: open sesi berikutnya){tp_entry_note} | SL {sl}"
                f"{danger_note}{market_engine.format_sector_tag(r.get('sector'))}"
            )
        await asyncio.to_thread(
            core.lock_daily_daytrade_picks, fresh_cross_momentum_candidates, "screendaytrade_fresh_cross_momentum",
            (backbone_result or {}).get("all_scored", {})
        )

    # MBSS v2 (user request — "yang aku maksud validation itu relate dengan
    # riset stage D1/D2 yang sudah bergerak naik, probability naik >60%"):
    # REPLIKASI PERSIS metodologi research/brights_imminent_cross_backtest_
    # v1.py, dites di 576 ISSI/2 tahun raw OHLC lokal -- kandidat pre-cross
    # (lane FAST_RECOVERY/EARLY_RECOVERY/ABOVE_MOMENTUM) yang D1 ATAU D2
    # SETELAH terkunci sudah naik >=3% -> Hit+10% D5 65.10%/67.19% (vs
    # baseline pre-cross murni 19.88%) -- JAUH di atas klaim user (>60%),
    # angka riil malah lebih kuat. BUKAN snapshot sesaat (macd_gain_since_
    # cross yang dipakai sebelumnya) -- pakai infrastruktur pick-history
    # yang SUDAH ADA (lock_daily_daytrade_picks + resolve_daytrade_picks,
    # jalan tiap malam via nightly.py) supaya entry_price/day1_pnl_pct/
    # day2_pnl_pct genuinely dari harga OPEN hari setelah pick (bukan proksi
    # apa pun) -- REUSE total, bukan bangun tracking paralel baru.
    VALIDATION_CHECKPOINT_MIN_PCT = 3.0
    VALIDATION_LOOKBACK_TRADING_DAYS = 5  # picks lebih lama dari ini sudah kadaluarsa utk section "baru saja tervalidasi"
    pick_history = core.load_daytrade_picks_history()
    recent_lane_picks = [
        p for p in pick_history
        if p.get("source") == "screendaytrade_macd_lane"
        and p.get("status") == "pending_resolution"
        and (
            (p.get("day1_pnl_pct") is not None and p["day1_pnl_pct"] >= VALIDATION_CHECKPOINT_MIN_PCT)
            or (p.get("day2_pnl_pct") is not None and p["day2_pnl_pct"] >= VALIDATION_CHECKPOINT_MIN_PCT)
        )
    ]
    # Batasi ke N hari bursa terakhir (list pick_date string ASC, ambil unique
    # tanggal, keep kalau pick_date termasuk VALIDATION_LOOKBACK_TRADING_DAYS
    # tanggal paling baru YANG ADA di history -- bukan asumsi kalender).
    all_pick_dates = sorted({p["pick_date"] for p in pick_history if p.get("source") == "screendaytrade_macd_lane"})
    recent_dates = set(all_pick_dates[-VALIDATION_LOOKBACK_TRADING_DAYS:])
    recent_lane_picks = [p for p in recent_lane_picks if p["pick_date"] in recent_dates]
    recent_lane_picks.sort(key=lambda p: max(p.get("day1_pnl_pct") or -999, p.get("day2_pnl_pct") or -999), reverse=True)

    lines.append(
        "\n✅ VALIDATION — D1/D2 sudah naik ≥3% sejak entry (Hit+10% D5 historis "
        "65-67% pada checkpoint ini, vs baseline pre-cross 19.88%)\n"
    )
    if not recent_lane_picks:
        lines.append("Tidak ada kandidat validation dalam 5 hari bursa terakhir.")
    else:
        for p in recent_lane_picks:
            checkpoint_day = "D1" if (p.get("day1_pnl_pct") or -999) >= VALIDATION_CHECKPOINT_MIN_PCT else "D2"
            checkpoint_pct = p.get("day1_pnl_pct") if checkpoint_day == "D1" else p.get("day2_pnl_pct")
            r = next((x for x in results if x["ticker"] == p["ticker"]), None)
            bb_info = (backbone_result or {}).get("all_scored", {}).get(p["ticker"]) if backbone_result else None
            danger_note = ""
            if bb_info and bb_info.get("predicted_danger") is not None and bb_info["predicted_danger"] >= DANGER_WARNING_THRESHOLD:
                danger_note = f" | ⚠️ Danger {bb_info['predicted_danger']:.0f}/100"
            price_now = r.get("price") if r else "-"
            lines.append(
                f"  • {p['ticker']} — {checkpoint_day} +{checkpoint_pct:.1f}% sejak entry {p['entry_price']} "
                f"({p['entry_date']}) | Harga sekarang {price_now} | TP {p['tp1']} (+6%) | SL {p['cut_loss']}{danger_note}"
                f"{broker_engine.format_smart_money_tag(p['ticker'], broksum_data)}"
            )

    buttons = core.build_check_buttons(
        [r["ticker"] for r in lane_candidates] + [p["ticker"] for p in recent_lane_picks]
        + [r["ticker"] for r in fresh_cross_momentum_candidates]
    )
    await core.safe_reply(update.message, "\n\n".join(lines), reply_markup=buttons)

    # Tombol upload Broker Summary ALL 3 hari untuk saham hasil radar.
    try:
        buttons = []
        row = []
        for r in lane_candidates:
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
                    "Prioritas upload: kandidat EXPLOSIVE terlebih dahulu."
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
    /hc — top 10 saham DAY TRADE (EOD High Conviction) dari cache /eodscan
    malam terakhir, diurutkan WR individual tertinggi (MBSS v2, user
    request 2026-08-30: "drop saja hc yang lama, toh WR nya juga tidak
    korelasi?" -- GANTI TOTAL dari Minervini 5-6 kriteria/is_high_conviction
    ke daytrade_hc_confidence, model & populasi PERSIS sama dgn /go DAY
    TRADE, lihat _daytrade_wr_tp1 utk alasan lengkap/riwayat audit Task
    #21). TP1 & WR sekarang individual per ticker (bukan lagi cuma badge
    boolean lolos-tidaknya kriteria).

    /hc rr — sama, tapi diurutkan risk_reward_at_max TERTINGGI (user
    request lanjutan) — ticker tanpa RR yang bisa dihitung otomatis
    ditaruh paling belakang, TIDAK di-exclude.

    /hc rank | /hc prob | /hc danger — urutkan berdasarkan angka backbone
    AB-RC1 (entry_rank/probability_score/predicted_danger, sama yang
    ditampilkan di tiap baris) — user request, buat cepat lihat mana yang
    paling bagus/aman di antara kandidat yang cukup banyak.

    Murni baca cache (nightly_engine.load_daily_scan_cache) — TIDAK fetch
    apa pun, jadi instan.
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

    # MBSS v2 (user request 2026-08-30): populasi + model WR individual --
    # lihat _compute_daytrade_wr_candidates/_daytrade_wr_tp1 (PERSIS dipakai
    # /go DAY TRADE juga, REUSE bukan formula paralel).
    daytrade_wr_pairs = _compute_daytrade_wr_candidates(list(scored.values()))
    candidates = [r for r, _ in daytrade_wr_pairs]
    wr_info_by_ticker = {r["ticker"]: info for r, info in daytrade_wr_pairs}

    # MBSS v2 BUGFIX (user request 2026-08-27 — live case: candidates
    # kosong malam itu, TAPI early-return di sini SEBELUM ini sekaligus
    # membungkam accumulation_candidates/continuation_candidates/
    # validation_candidates di bawah -- tiga section itu computasinya
    # SAMA SEKALI TIDAK bergantung pada DAY TRADE punya kandidat atau tidak.
    # "Tidak ada DAY TRADE" TIDAK BOLEH berarti "tidak ada apa-apa di /hc"
    # -- lanjut proses dgn candidates=[] (sort/top10 aman kosong), jangan
    # return dini.
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
        # MBSS v2 (user request 2026-08-30): urutan DEFAULT = WR individual
        # model daytrade_hc_confidence tertinggi -- `candidates` SUDAH terurut
        # begitu (lihat _compute_daytrade_wr_candidates), tidak perlu dihitung
        # ulang. compute_daytrade_score (momentum-murni, dipakai screendaytrade/
        # gptpick) TIDAK lagi jadi kunci urut default /hc -- WR terkalibrasi
        # langsung ke outcome lebih relevan utk positioning /hc sekarang.
        sort_label = "WR individual tertinggi (daytrade_hc_confidence)"
    top10 = candidates[:10]
    broksum_data = nightly_engine.load_broksum_250()  # dimuat sekali di luar loop, murni baca cache (tidak fetch)

    # MBSS v2 (user request — ditemukan lewat penelusuran manual /winrate: /hc
    # TIDAK PERNAH terlacak sama sekali sebelumnya). Kunci lewat mekanisme
    # yang SAMA dengan screendaytrade/gptpick/testbrief, source="hc" --
    # SAMA source dipertahankan meski model gantinya total (2026-08-30),
    # supaya histori /winrate lane ini tetap satu timeline (bukan pecah jadi
    # source baru tanpa alasan kuat). Gagal-lunak — kalau lock gagal, tetap
    # tampilkan hasil seperti biasa.
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

    # MBSS v2 (user request 2026-08-30, GANTI TOTAL dari versi Minervini yg
    # di-HIDE krn hit6~33%): model baru SUDAH tervalidasi (top-decile WR
    # 69.2%, AUC 0.611, lihat daytrade_hc_confidence.py) -- breakdown
    # per-ticker DITAMPILKAN LANGSUNG lagi, bukan disembunyikan.
    if candidates:
        lines = [f"🔥 DAY TRADE (EOD High Conviction) — {len(top10)} kandidat WR tertinggi dari {len(candidates)} lolos floor likuiditas\n"]
        lines.append(f"⚠️ {daytrade_hc_confidence.DAYTRADE_EXIT_WARNING}\n")
        for r in top10:
            info = wr_info_by_ticker.get(r["ticker"])
            if not info:
                continue
            t = r.get("targets", {})
            cut_loss = t.get("cut_loss")
            bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
            danger_note = ""
            if bb_info and bb_info.get("predicted_danger") is not None and bb_info["predicted_danger"] >= 50:
                danger_note = f" | ⚠️ Danger {bb_info['predicted_danger']:.0f}/100"
            lines.append(
                f"• {r['ticker']} — {r.get('price')}\n"
                f"   TP1 {info['price']:,.0f} / +{info['level_pct']:.1f}% (WR {info['wr_pct']:.0f}%)" + (f" | SL {cut_loss}" if cut_loss else "")
                + f"{danger_note}{broker_engine.format_smart_money_tag(r['ticker'], broksum_data)}"
            )
    else:
        # MBSS v2 BUGFIX (user request 2026-08-27): 0 kandidat DAY TRADE
        # TIDAK BOLEH berarti section CONTINUATION/VALIDATION/AKUMULASI di
        # bawah ikut disembunyikan (lihat catatan early-return yang dihapus
        # di atas) -- pesan ini cuma bilang lane ini kosong, bukan /hc
        # keseluruhan kosong.
        lines = ["🔥 DAY TRADE (EOD High Conviction) — 0 kandidat lolos floor likuiditas/WR malam ini.\n"]
    if staleness_note:
        lines.insert(0, staleness_note)

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
    # singkat ke situ, bukan duplikasi coverage di dua tempat -- lane lama
    # SQUEEZE_RESCUE sudah diganti FAST_RECOVERY/EARLY_RECOVERY, lihat
    # /screendaytrade, section SETUP PRA-BREAKOUT.
    lines.append("\nKandidat pra-breakout (backtest tervalidasi): lihat /screendaytrade, section SETUP PRA-BREAKOUT.")
    lines.append("Detail lengkap: /check TICKER")

    # MBSS v2 (user request — "tambahkan stage continuation di HC: sudah
    # cross, sudah hit 6%, masih potensi hit 10%"): POST-cross DAN SUDAH hit
    # +6% tapi belum +10% (extension target). Cocok utk positioning HC
    # (follow breakout yg SUDAH terjadi, bukan pra-breakout). Sumber SELURUH
    # pool Danger Gate survivor (`scored`), exclude yg sudah tampil di
    # top10/akumulasi di atas.
    #
    # MBSS v2 (user correction — "continuation harus dalam episode yang
    # sama, kalau cross 20d lalu harusnya tidak masuk"): tambah batas
    # MACD_CONTINUATION_MAX_CROSS_DAYS=5, PERSIS "Window observasi maksimum:
    # D5" di 01_GUIDE_PRODUKSI.md -- cross yg sudah lewat window evaluasi
    # bukan lagi episode yg sama, sudah basi utk disebut "continuation".
    MACD_CONTINUATION_MAX_CROSS_DAYS = 5
    # MBSS v2 (user request 2026-08-27 — live case: SSIA/NRCA/PTBA/MPIX/SGER/
    # ADRO/AADI lolos Minervini top10 [hc_tickers] TAPI juga membawa
    # macd_lifecycle_state BREAKING/CONTINUATION -- sebelum hide Minervini,
    # exclude hc_tickers dari sini masuk akal [hindari dobel tampil, sama2
    # kelihatan]. SETELAH hide (Minervini breakdown per-ticker disembunyikan,
    # lihat di atas), exclusion ini jadi KONTRAPRODUKTIF: ticker yg genuinely
    # lolos kriteria CONTINUATION/VALIDATION TERVALIDASI (macd_gain_since_
    # cross_pct, BEDA dari macd_lifecycle_state yg sekadar anotasi generik --
    # lihat engine/scoring.py, WATCH_PULLBACK eksplisit "belum divalidasi
    # backtest terpisah") malah ikut terkubur di section yg sudah di-hide,
    # bukan muncul di section yg SEHARUSNYA jadi rumah sinyal mereka.
    # hc_tickers DIHAPUS dari exclusion -- accumulation_candidates TETAP
    # exclude (konsep beda: pra-breakout vs post-breakout, exclusion itu
    # bukan soal duplikasi tampilan Minervini).
    # MBSS v2 (user request 2026-08-27 -- riset conditional filter, 576
    # ISSI/2thn): dist_to_sma20 ("price_vs_sma20_pct") >=12% adalah gate
    # TERKUAT & KONSISTEN utk CONTINUATION/VALIDATION (sama field & ambang
    # dgn ABOVE_MOMENTUM di engine/scoring.py, jaga sinkron). CONTINUATION
    # 50.0%->60.2% (n=530), VALIDATION 40.5%->57.6% (n=340) -- pola monoton
    # bersih, kombo RSI/ADX/volume tidak menambah apa pun di atasnya.
    MACD_LANE_DIST_SMA20_MIN_PCT = 12.0
    excluded_tickers = {r["ticker"] for r in accumulation_candidates}
    continuation_candidates = [
        r for r in scored.values()
        if r.get("ticker") not in excluded_tickers
        and r.get("macd_cross_direction") == "bullish"
        and r.get("macd_cross_days_ago") is not None
        and r["macd_cross_days_ago"] <= MACD_CONTINUATION_MAX_CROSS_DAYS
        and r.get("macd_gain_since_cross_pct") is not None
        and 6.0 <= r["macd_gain_since_cross_pct"] < 10.0
        and r.get("price_vs_sma20_pct") is not None
        and r["price_vs_sma20_pct"] >= MACD_LANE_DIST_SMA20_MIN_PCT
    ]
    continuation_candidates.sort(key=lambda r: r["macd_gain_since_cross_pct"], reverse=True)
    # MBSS v2 (user request 2026-08-27 -- suppress kalau confidence individual <50% di level terdekat.
    # BUGFIX: should_suppress, bukan compute_tp1_tp2(...) is None -- lihat catatan lane_confidence.py)
    continuation_candidates = [
        r for r in continuation_candidates
        if not lane_confidence.should_suppress(
            "CONTINUATION", {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b")}, r.get("price")
        )
    ]
    continuation_candidates = continuation_candidates[:8]

    if continuation_candidates:
        lines.append(
            f"\n📈 CONTINUATION — {len(continuation_candidates)} kandidat (sudah cross bullish & sudah hit +6%, masih potensi +10%)\n"
            f"Confidence individual per ticker (dist_to_sma20/pct_b masing2, bukan rata-rata grup):\n"
        )
        for r in continuation_candidates:
            t = r.get("targets", {})
            bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
            danger_note = ""
            if bb_info and bb_info.get("predicted_danger") is not None and bb_info["predicted_danger"] >= 50:
                danger_note = f" | ⚠️ Danger {bb_info['predicted_danger']:.0f}/100"
            tp_suffix = _lane_tp_suffix("CONTINUATION", {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b")}, r.get("price"))
            lines.append(
                f"• {r['ticker']} — cross {r.get('macd_cross_days_ago', '-')} hari lalu, "
                f"+{r['macd_gain_since_cross_pct']:.1f}% sejak cross (target +10%){tp_suffix}\n"
                f"   Harga {r.get('price')} | SL {t.get('cut_loss')}{danger_note}"
                f"{broker_engine.format_smart_money_tag(r['ticker'], broksum_data)}"
            )
        try:
            await asyncio.to_thread(
                core.lock_daily_daytrade_picks, continuation_candidates, "hc_continuation",
                (backbone_result or {}).get("all_scored", {})
            )
        except Exception as e:
            print(f"⚠️ Gagal mengunci picks /hc continuation untuk /winrate: {e}")

    # MBSS v2 (user request 2026-08-24 — live case NRCA: cross bullish 3
    # hari lalu, ABOVE_CENTERLINE, +5.5% sejak cross, TAPI tidak muncul di
    # lane manapun -- macd_approach_tier PASTI None utk kandidat post-cross
    # by design (lane cuma utk pre_cross), dan gain 5.5% < 6.0% jadi juga
    # belum masuk CONTINUATION. Ini celah nyata, bukan Danger Gate: NRCA
    # invisible di ketiga sinyal sekaligus). Dites thd stagnant metrik yg
    # sama (576 ISSI raw OHLC 2 tahun, cross<=5hr): bucket gain_since_cross
    # 3-6% hit6_d5=40.30%/hit10_d5=25.12% (n=2906) -- genuinely elevated vs
    # bucket 0-3% (31.90%/19.23%), meski masih di bawah bucket 6-10% yg
    # dipakai CONTINUATION (48.26%/33.95%) -- makanya ditampilkan section
    # TERPISAH dgn ekspektasi lebih rendah, bukan digabung ke CONTINUATION.
    validation_candidates = [
        r for r in scored.values()
        if r.get("ticker") not in excluded_tickers
        and r["ticker"] not in {c["ticker"] for c in continuation_candidates}
        and r.get("macd_cross_direction") == "bullish"
        and r.get("macd_cross_days_ago") is not None
        and r["macd_cross_days_ago"] <= MACD_CONTINUATION_MAX_CROSS_DAYS
        and r.get("macd_gain_since_cross_pct") is not None
        and 3.0 <= r["macd_gain_since_cross_pct"] < 6.0
        and r.get("price_vs_sma20_pct") is not None
        and r["price_vs_sma20_pct"] >= MACD_LANE_DIST_SMA20_MIN_PCT
    ]
    validation_candidates.sort(key=lambda r: r["macd_gain_since_cross_pct"], reverse=True)
    # MBSS v2 (user request 2026-08-27 -- suppress kalau confidence individual <50% di level terdekat.
    # BUGFIX: should_suppress, bukan compute_tp1_tp2(...) is None -- lihat catatan lane_confidence.py)
    validation_candidates = [
        r for r in validation_candidates
        if not lane_confidence.should_suppress(
            "VALIDATION", {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b")}, r.get("price")
        )
    ]
    validation_candidates = validation_candidates[:8]

    if validation_candidates:
        lines.append(
            f"\n📊 VALIDATION — {len(validation_candidates)} kandidat (sudah cross bullish, +3-6% sejak cross)\n"
            f"Confidence individual per ticker (dist_to_sma20/pct_b masing2, bukan rata-rata grup):\n"
        )
        for r in validation_candidates:
            t = r.get("targets", {})
            bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
            danger_note = ""
            if bb_info and bb_info.get("predicted_danger") is not None and bb_info["predicted_danger"] >= 50:
                danger_note = f" | ⚠️ Danger {bb_info['predicted_danger']:.0f}/100"
            tp_suffix = _lane_tp_suffix("VALIDATION", {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b")}, r.get("price"))
            lines.append(
                f"• {r['ticker']} — cross {r.get('macd_cross_days_ago', '-')} hari lalu, "
                f"+{r['macd_gain_since_cross_pct']:.1f}% sejak cross{tp_suffix}\n"
                f"   Harga {r.get('price')} | SL {t.get('cut_loss')}{danger_note}"
                f"{broker_engine.format_smart_money_tag(r['ticker'], broksum_data)}"
            )
        try:
            await asyncio.to_thread(
                core.lock_daily_daytrade_picks, validation_candidates, "hc_validation",
                (backbone_result or {}).get("all_scored", {})
            )
        except Exception as e:
            print(f"⚠️ Gagal mengunci picks /hc validation untuk /winrate: {e}")

    # MBSS v2 (user request 2026-08-31, live case SPTO/WIFI/MEDC/HRUM/BANK --
    # fresh cross genuine, gain_since_cross 2-3%, TAPI tidak masuk lane
    # manapun -- persis pola NRCA di atas, cuma di gain band lebih rendah).
    # Backtest 2thn/576 ISSI (chronological 70/30, validasi): populasi ini
    # py peluang TRANSISI ke gain_since_cross>=4% (zona VALIDATION) dlm 1-2
    # hari bursa = 33.4% (n=661), vs baseline pasar acak 13.4% (>2x lipat).
    # Pita 0-2% DITOLAK (10.7-18.8%, tak terselamatkan filter vol_ratio/RSI)
    # -- HANYA 2-3% yg lolos. TANPA gate dist_to_sma20 (SENGAJA, beda dari
    # CONTINUATION/VALIDATION -- backtest TIDAK mengkondisikan dist_to_sma20
    # sama sekali). TIDAK didukung lane_confidence (belum ada model utk lane
    # baru) -- _lane_tp_suffix return "" otomatis, tampil dgn WR statis spt
    # FAST_RECOVERY/EARLY_RECOVERY. Filosofi user: "kita punya mekanisme
    # sweep candidate all setup, maka lebih banyak kandidat sebenarnya lebih
    # baik" -- lane INI perannya jaring kandidat AWAL, Conviction Sweep yg
    # konfirmasi belakangan.
    early_validation_candidates = [
        r for r in scored.values()
        if r.get("ticker") not in excluded_tickers
        and r["ticker"] not in {c["ticker"] for c in continuation_candidates}
        and r["ticker"] not in {c["ticker"] for c in validation_candidates}
        and r.get("macd_cross_direction") == "bullish"
        and r.get("macd_cross_days_ago") is not None
        and r["macd_cross_days_ago"] <= MACD_CONTINUATION_MAX_CROSS_DAYS
        and r.get("macd_gain_since_cross_pct") is not None
        and 2.0 <= r["macd_gain_since_cross_pct"] < 3.0
    ]
    early_validation_candidates.sort(key=lambda r: r["macd_gain_since_cross_pct"], reverse=True)
    early_validation_candidates = early_validation_candidates[:8]

    if early_validation_candidates:
        lines.append(
            f"\n🌱 EARLY VALIDATION — {len(early_validation_candidates)} kandidat (sudah cross bullish, +2-3% sejak cross)\n"
            f"~33% lanjut ke zona VALIDATION dlm 1-2 hari bursa (vs baseline pasar 13.4%) -- jaring kandidat awal:\n"
        )
        for r in early_validation_candidates:
            t = r.get("targets", {})
            bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
            danger_note = ""
            if bb_info and bb_info.get("predicted_danger") is not None and bb_info["predicted_danger"] >= 50:
                danger_note = f" | ⚠️ Danger {bb_info['predicted_danger']:.0f}/100"
            lines.append(
                f"• {r['ticker']} — cross {r.get('macd_cross_days_ago', '-')} hari lalu, "
                f"+{r['macd_gain_since_cross_pct']:.1f}% sejak cross\n"
                f"   Harga {r.get('price')} | SL {t.get('cut_loss')}{danger_note}"
                f"{broker_engine.format_smart_money_tag(r['ticker'], broksum_data)}"
            )
        try:
            await asyncio.to_thread(
                core.lock_daily_daytrade_picks, early_validation_candidates, "hc_early_validation",
                (backbone_result or {}).get("all_scored", {})
            )
        except Exception as e:
            print(f"⚠️ Gagal mengunci picks /hc early_validation untuk /winrate: {e}")

    # MBSS v2 (user request 2026-08-31 -- lanjutan riset dead zone "since
    # cross 3-6d, kondisi tertentu"): gain_since_cross>=3% open-ended di
    # hari 3-6, TANPA gate dist_to_sma20, HANYA kalau belum tercover
    # CONTINUATION/VALIDATION (dist>=12% & gain 3-10%, hari <=5) MAUPUN
    # EARLY_VALIDATION (gain 2-3%, hari 1-5). Dead-zone map penuh (cross_
    # days_ago 1-6, chronological 70/30 validasi): gain>=3% merged (skip
    # yg SUDAH tercover) touch>=3%(d3)=61.3% (n=2346) vs baseline pasar
    # 49.5%. BEDA dari EARLY_VALIDATION -- DIDUKUNG lane_confidence (model
    # DILATIH, fitur gain_since_cross+dist_to_sma20+pct_b, AUC 0.558-0.572,
    # Brier konsisten lebih baik dari baseline -- gain_since_cross py
    # varians besar di pita open-ended ini, jadi fitur eksplisit yg
    # genuinely informatif, beda dari EARLY_VALIDATION yg pita gain-nya
    # sempit 2-3% shg gain tak berguna sbg fitur).
    MACD_LATE_VALIDATION_GAIN_MIN = 3.0
    MACD_LATE_VALIDATION_MIN_DAYS_AGO = 3
    MACD_LATE_VALIDATION_MAX_DAYS_AGO = 6
    late_validation_candidates = [
        r for r in scored.values()
        if r.get("ticker") not in excluded_tickers
        and r["ticker"] not in {c["ticker"] for c in continuation_candidates}
        and r["ticker"] not in {c["ticker"] for c in validation_candidates}
        and r["ticker"] not in {c["ticker"] for c in early_validation_candidates}
        and r.get("macd_cross_direction") == "bullish"
        and r.get("macd_cross_days_ago") is not None
        and MACD_LATE_VALIDATION_MIN_DAYS_AGO <= r["macd_cross_days_ago"] <= MACD_LATE_VALIDATION_MAX_DAYS_AGO
        and r.get("macd_gain_since_cross_pct") is not None
        and r["macd_gain_since_cross_pct"] >= MACD_LATE_VALIDATION_GAIN_MIN
    ]
    late_validation_candidates.sort(key=lambda r: r["macd_gain_since_cross_pct"], reverse=True)

    def _late_val_features(r):
        return {
            "gain_since_cross": r.get("macd_gain_since_cross_pct"),
            "dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b"),
        }

    late_validation_candidates = [
        r for r in late_validation_candidates
        if not lane_confidence.should_suppress("LATE_VALIDATION", _late_val_features(r), r.get("price"))
    ]
    late_validation_candidates = late_validation_candidates[:8]

    if late_validation_candidates:
        lines.append(
            f"\n🍂 LATE VALIDATION — {len(late_validation_candidates)} kandidat (sudah cross bullish, +3%+ sejak cross, hari 3-6)\n"
            f"Confidence individual per ticker (gain_since_cross/dist_to_sma20/pct_b masing2):\n"
        )
        for r in late_validation_candidates:
            t = r.get("targets", {})
            bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
            danger_note = ""
            if bb_info and bb_info.get("predicted_danger") is not None and bb_info["predicted_danger"] >= 50:
                danger_note = f" | ⚠️ Danger {bb_info['predicted_danger']:.0f}/100"
            tp_suffix = _lane_tp_suffix("LATE_VALIDATION", _late_val_features(r), r.get("price"))
            lines.append(
                f"• {r['ticker']} — cross {r.get('macd_cross_days_ago', '-')} hari lalu, "
                f"+{r['macd_gain_since_cross_pct']:.1f}% sejak cross{tp_suffix}\n"
                f"   Harga {r.get('price')} | SL {t.get('cut_loss')}{danger_note}"
                f"{broker_engine.format_smart_money_tag(r['ticker'], broksum_data)}"
            )
        try:
            await asyncio.to_thread(
                core.lock_daily_daytrade_picks, late_validation_candidates, "hc_late_validation",
                (backbone_result or {}).get("all_scored", {})
            )
        except Exception as e:
            print(f"⚠️ Gagal mengunci picks /hc late_validation untuk /winrate: {e}")

    # MBSS v2 (user request 2026-08-24 — riset "episode extended": bukan
    # fresh breakout (itu domain CONTINUATION/VALIDATION di atas, cross<=5
    # hari), tapi episode yg SUDAH lebih lama (cross 6-10 hari lalu) DAN
    # sudah pernah volume breakout>=3x (bukan yg tenang2 saja) DAN masih
    # above_centerline. Dua sub-pola, KEDUANYA post-cross by construction
    # jadi HANYA muncul di HC (tidak pernah di SDT, yg pre-cross-only) --
    # persis siklus yg didiskusikan: SDT (pre-cross) -> HC VALIDATION/
    # CONTINUATION (cross<=5hr) -> HC tier ini (cross 6-10hr).
    #
    # Dites 576 ISSI raw OHLC 2 tahun (n=7542 populasi dasar):
    #   MOMENTUM EXTENDED (prioritas #1): gap_slope_3d>=Q4(0.3106, laju
    #     pelebaran macd-signal tercepat) + ret_1d_today>2.5% -- hit6=67.9%/
    #     hit10=55.3% (n=514) di threshold 2.5%. hit6 naik ke 71.95%/59.76%
    #     di threshold 5% (n=410, "kualitas lebih baik") tapi user pilih
    #     2.5% utk volume kandidat lebih banyak -- makanya ditag kualitas
    #     di pesan (>=5% = kualitas tinggi, 2.5-5% = valid tapi lebih lemah)
    #     bukan disatukan tanpa keterangan. JANGAN filter RSI<70 (dites,
    #     backwards -- RSI tinggi di kombinasi ini justru lebih baik).
    #   PULLBACK EXTENDED (prioritas #2, tag higher risk): gap_slope_3d>=Q4
    #     + pullback dari peak episode>=15% -- hit6=62.85%/hit10=45.51%
    #     (n=646), stagnant_negative 44.27% (SEDIKIT lebih tinggi dari
    #     baseline ~39%, bukan makin aman -- upside conversion naik, bukan
    #     risiko turun, makanya WAJIB tag "risiko lebih tinggi").
    # MBSS v2 (user request 2026-08-27 -- riset generalisasi window, 576 ISSI/
    # 2thn): window 6-10 hari LAMA diperlebar ke 6-40 -- gate lengkap
    # [regime ABOVE + had_breakout_in_episode + gap_slope_3d>=Q4 + ret1d>2.5%]
    # dites tanpa batas hari dulu, breakdown per rentang TIDAK menunjukkan
    # dilusi kualitas sama sekali sampai 40 hari (n 6-10=431/hit6=69.4% vs
    # n 6-40=1260/hit6=70.2%, malah SEDIKIT naik + stagnant_neg SEDIKIT
    # turun 36.0%->32.6%) -- window lama membuang ~65% kandidat yg sama
    # bagusnya secara percuma. >40 hari mulai n kecil & kualitas turun,
    # tetap dijaga sbg batas atas.
    MACD_EXTENDED_MIN_CROSS_DAYS_AGO = 6   # (5,40] eksklusif thd CONTINUATION/VALIDATION yg <=5
    MACD_EXTENDED_MAX_CROSS_DAYS_AGO = 40
    MACD_GAP_SLOPE_Q4_THRESHOLD = 0.3106    # kuartil-75 gap_slope_3d dari populasi backtest, BUKAN cross-sectional live (sama konvensi lane MACD_LANE_FAST_SLOPE3_MIN dkk)
    MACD_MOMENTUM_RET1D_MIN = 2.5
    MACD_MOMENTUM_RET1D_HIGH_QUALITY = 5.0  # >= ini ditag kualitas tinggi di pesan
    MACD_PULLBACK_EXTENDED_DEPTH_MAX = -15.0

    def _in_extended_window(r):
        return (
            r.get("macd_regime") == "ABOVE_CENTERLINE"
            and r.get("macd_episode_had_volume_breakout") is True
            and r.get("macd_cross_days_ago") is not None
            and MACD_EXTENDED_MIN_CROSS_DAYS_AGO <= r["macd_cross_days_ago"] <= MACD_EXTENDED_MAX_CROSS_DAYS_AGO
            and r.get("macd_gap_slope_3d") is not None
            and r["macd_gap_slope_3d"] >= MACD_GAP_SLOPE_Q4_THRESHOLD
        )

    extended_excluded = excluded_tickers | {c["ticker"] for c in continuation_candidates} | {c["ticker"] for c in validation_candidates}

    momentum_extended_candidates = [
        r for r in scored.values()
        if r.get("ticker") not in extended_excluded
        and _in_extended_window(r)
        and r.get("ret_1d_pct") is not None and r["ret_1d_pct"] > MACD_MOMENTUM_RET1D_MIN
    ]
    momentum_extended_candidates.sort(key=lambda r: r["ret_1d_pct"], reverse=True)
    # MBSS v2 (user request 2026-08-27 -- suppress kalau confidence individual <50% di level terdekat.
    # BUGFIX: should_suppress, bukan compute_tp1_tp2(...) is None -- lihat catatan lane_confidence.py)
    momentum_extended_candidates = [
        r for r in momentum_extended_candidates
        if not lane_confidence.should_suppress(
            "MOMENTUM_EXTENDED",
            {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b"), "gap_slope_3d": r.get("macd_gap_slope_3d")},
            r.get("price"),
        )
    ]
    momentum_extended_candidates = momentum_extended_candidates[:8]

    if momentum_extended_candidates:
        lines.append(f"\n🚀 MOMENTUM EXTENDED — {len(momentum_extended_candidates)} kandidat (episode sudah 6-10 hari, MACD akselerasi + harga konfirmasi hari ini)\n")
        for r in momentum_extended_candidates:
            t = r.get("targets", {})
            bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
            danger_note = ""
            if bb_info and bb_info.get("predicted_danger") is not None and bb_info["predicted_danger"] >= 50:
                danger_note = f" | ⚠️ Danger {bb_info['predicted_danger']:.0f}/100"
            quality_tag = " 🥇kualitas tinggi" if r["ret_1d_pct"] >= MACD_MOMENTUM_RET1D_HIGH_QUALITY else ""
            tp_suffix = _lane_tp_suffix(
                "MOMENTUM_EXTENDED",
                {"dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b"), "gap_slope_3d": r.get("macd_gap_slope_3d")},
                r.get("price"),
            )
            lines.append(
                f"• {r['ticker']} — cross {r.get('macd_cross_days_ago', '-')} hari lalu, "
                f"+{r['ret_1d_pct']:.1f}% hari ini{quality_tag}{tp_suffix}\n"
                f"   Harga {r.get('price')} | SL {t.get('cut_loss')}{danger_note}"
                f"{broker_engine.format_smart_money_tag(r['ticker'], broksum_data)}"
            )
        try:
            await asyncio.to_thread(
                core.lock_daily_daytrade_picks, momentum_extended_candidates, "hc_momentum_extended",
                (backbone_result or {}).get("all_scored", {})
            )
        except Exception as e:
            print(f"⚠️ Gagal mengunci picks /hc momentum extended untuk /winrate: {e}")

    pullback_extended_candidates = [
        r for r in scored.values()
        if r.get("ticker") not in extended_excluded
        and r["ticker"] not in {c["ticker"] for c in momentum_extended_candidates}
        and _in_extended_window(r)
        and r.get("macd_pullback_from_episode_peak_pct") is not None
        and r["macd_pullback_from_episode_peak_pct"] <= MACD_PULLBACK_EXTENDED_DEPTH_MAX
    ]
    pullback_extended_candidates.sort(key=lambda r: r["macd_pullback_from_episode_peak_pct"])
    pullback_extended_candidates = pullback_extended_candidates[:8]

    if pullback_extended_candidates:
        lines.append(f"\n📉 PULLBACK EXTENDED — {len(pullback_extended_candidates)} kandidat (episode 6-10 hari, MACD masih akselerasi, TAPI harga sedang pullback ≥15% dari peak) ⚠️ risiko lebih tinggi dari MOMENTUM EXTENDED\n")
        for r in pullback_extended_candidates:
            t = r.get("targets", {})
            bb_info = (backbone_result or {}).get("all_scored", {}).get(r["ticker"]) if backbone_result else None
            danger_note = ""
            if bb_info and bb_info.get("predicted_danger") is not None and bb_info["predicted_danger"] >= 50:
                danger_note = f" | ⚠️ Danger {bb_info['predicted_danger']:.0f}/100"
            lines.append(
                f"• {r['ticker']} — cross {r.get('macd_cross_days_ago', '-')} hari lalu, "
                f"pullback {r['macd_pullback_from_episode_peak_pct']:.1f}% dari peak episode | "
                f"Harga {r.get('price')} | SL {t.get('cut_loss')}{danger_note}"
                f"{broker_engine.format_smart_money_tag(r['ticker'], broksum_data)}"
            )
        try:
            await asyncio.to_thread(
                core.lock_daily_daytrade_picks, pullback_extended_candidates, "hc_pullback_extended",
                (backbone_result or {}).get("all_scored", {})
            )
        except Exception as e:
            print(f"⚠️ Gagal mengunci picks /hc pullback extended untuk /winrate: {e}")

    all_tickers = (
        [r["ticker"] for r in top10] + [r["ticker"] for r in accumulation_candidates]
        + [r["ticker"] for r in continuation_candidates] + [r["ticker"] for r in validation_candidates]
        + [r["ticker"] for r in momentum_extended_candidates] + [r["ticker"] for r in pullback_extended_candidates]
    )
    buttons = core.build_check_buttons(all_tickers)
    await core.safe_reply(update.message, "\n\n".join(lines), reply_markup=buttons)




# ==========================================
# BSJP SCREENING (MBSS v2, user request 2026-08-29 -- REVISI TOTAL:
# unified 4-kriteria, blend ARA/second-wave/6-kriteria-lama/pullback jadi
# SATU sinyal "Beli Sore Jual Pagi". Full parameter sweep (162 kombinasi,
# close-based/exit-efficiency validation, daily_2y_issi_raw.pkl) -- lihat
# catatan lengkap di engine/scanalert.py run_bsjp_shortlist_scan/run_bsjp_
# recheck_once. Command ini = FASE 1 (scan penuh universe akhir sesi 1,
# simpan shortlist) -- FASE 2 (recheck live tiap 15 menit 09:30-15:50,
# kirim alert final) jalan otomatis via JobQueue, lihat engine/legacy_
# core.py run_bsjp_recheck_job.
# ==========================================


async def bsjp_screening_command(update, context):
    """
    /bsjp -- FASE 1 unified BSJP: scan SELURUH universe ISSI thd 4 kriteria
    wajib (AND, lihat engine/scanalert.py utk detail & sumber angka):
      1. ret_1d > 18%
      2. Volume hari ini > 1.5x volume kemarin
      3. High hari ini < 1.01x harga sekarang ("clean close")
      4. Volume hari ini > 1.0x rata-rata volume 200 hari
    Simpan yg lolos sbg shortlist (dipakai FASE 2 -- recheck live otomatis
    tiap 15 menit 09:30-15:50 WIB, lihat run_bsjp_recheck_job).

    MBSS v2 (user request 2026-08-31 -- "harusnya tetap bisa di running
    ketika istirahat, kan hanya untuk jaring kandidat awal?"): jendela
    DIPERLEBAR dari get_current_idx_session() (yg return None saat istirahat
    siang 12:00-13:30/11:30-14:00 Jumat) ke SELURUH hari bursa 09:00-16:00 --
    BEDA dgn command lain yg genuinely butuh sesi AKTIF (harga bergerak
    detik-ini). BSJP Fase 1 cuma butuh data HARI INI SEJAUH INI (ret_1d,
    volume-so-far, high-so-far via yf.download partial-day bar) -- data itu
    SUDAH final/beku begitu sesi 1 tutup, TIDAK berubah lagi selama istirahat
    (baru update lagi begitu sesi 2 buka), jadi genuinely valid dicek kapan
    pun 09:00-16:00, termasuk pas istirahat. Sebelum 09:00 TETAP ditolak
    (belum ada data hari ini SAMA SEKALI, bukan cuma beku).
    """
    import engine.scanalert as scanalert_engine  # import lokal -- hindari circular import di level modul

    # MBSS v2 (user request 2026-09-02): /bsjp tp -- panduan jual pre-open
    # esok pagi (TP1/TP2 dari entry ASLI Fase 2 hari bursa terakhir), TIDAK
    # dibatasi jendela 09:00-16:00 spt scan Fase 1 di bawah -- justru
    # dipakai MALAM hari yg sama atau PAGI besok SEBELUM market buka.
    if context.args and context.args[0].lower() == "tp":
        msg = scanalert_engine.build_bsjp_tp_plan_message()
        await core.safe_reply(update.message, msg)
        return

    now_wib = datetime.datetime.now(core.WIB)
    bsjp_window_start = datetime.time(9, 0)
    bsjp_window_end = datetime.time(16, 0)  # akhir pra-penutupan, sama batas atas semua sesi IDX
    is_holiday = await asyncio.to_thread(core.is_idx_market_holiday_today)
    if now_wib.weekday() >= 5 or is_holiday or not (bsjp_window_start <= now_wib.time() < bsjp_window_end):
        await core.safe_reply(
            update.message,
            "⚠️ /bsjp cuma berguna selama hari bursa berjalan (09:00-16:00 WIB) -- di luar itu belum/tidak ada data hari ini utk dicek."
        )
        return

    scored = nightly_engine.load_daily_scan_cache()
    if not scored:
        await core.safe_reply(update.message, "⚠️ Cache /eodscan belum ada/basi -- jalankan /eodscan dulu (dari kemarin sore, bukan hari ini).")
        return
    universe = sorted(scored.keys())

    await core.safe_reply(update.message, f"🌆 Scan BSJP (4 kriteria unified) dari {len(universe)} ticker universe, mengecek data live...")

    try:
        passed = await scanalert_engine.run_bsjp_shortlist_scan(universe)
    except Exception as e:
        await core.safe_reply(update.message, f"⚠️ Scan BSJP gagal: {e}")
        return

    if not passed:
        await core.safe_reply(
            update.message,
            "📋 Tidak ada kandidat yang lolos SEMUA 4 kriteria BSJP saat ini (formula ketat -- wajar kalau kosong, itu justru tujuannya). "
            "Kalau ada shortlist tersimpan dari /bsjp sebelumnya hari ini, itu TETAP dipantau (tidak dihapus)."
        )
        return

    passed.sort(key=lambda r: r["ret_1d_pct"], reverse=True)
    lines = [f"🌆 BSJP SHORTLIST — {len(passed)} kandidat lolos SEMUA 4 kriteria (akan di-recheck live tiap 15 menit 09:30-15:50)\n"]
    for i, r in enumerate(passed, 1):
        vol_vs_prev = r["volume_so_far"] / max(r["prev_volume"], 1.0)
        vol_vs_ma200 = r["volume_so_far"] / max(r["vol_ma200"], 1.0)
        lines.append(
            f"{i}. {r['ticker']} — {r['current_price']:,.0f} ({r['ret_1d_pct']:+.1f}%)\n"
            f"   Vol {vol_vs_prev:.1f}x kemarin | {vol_vs_ma200:.1f}x MA200"
        )
    lines.append("\n⚠️ Ini shortlist FASE 1, BUKAN alert entry -- alert final (dgn TP1) dikirim otomatis kalau kandidat MASIH lolos semua kriteria saat recheck 09:30-15:50 WIB.")

    buttons = core.build_check_buttons([r["ticker"] for r in passed])
    await core.safe_reply(update.message, "\n\n".join(lines), reply_markup=buttons)

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

        # MBSS v2 (user request 2026-08-30): is_high_conviction GANTI TOTAL ke
        # _daytrade_wr_tp1 (lihat docstring helper itu) -- tag "HIGH CONVICTION"
        # di sini sekarang = lolos floor likuiditas + floor WR 60% model.
        if _daytrade_wr_tp1(r) is not None:
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
        # MBSS v2 (user request 2026-08-30): is_high_conviction GANTI TOTAL ke
        # _daytrade_wr_tp1 -- Consensus Prime's HC leg sekarang = lolos floor
        # likuiditas + floor WR 60% model (bukan lagi Minervini 5-6 kriteria).
        if _daytrade_wr_tp1(r) is not None:
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

    # MBSS v2 (user request 2026-08-30): is_high_conviction GANTI TOTAL ke
    # _daytrade_wr_tp1 -- lihat docstring helper itu. Sort tetap by final
    # score (bukan WR) supaya union ini konsisten dgn "top HC by final
    # score" yg dijelaskan di docstring fungsi ini.
    hc_candidates = [r for r in pool if _daytrade_wr_tp1(r) is not None]
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
        "Kriteria PLACEHOLDER (threshold-nya), BELUM ada data forward sama sekali: volume_ratio>=3.0x (3 bar terakhir vs "
        "baseline 3 bar sebelumnya, divalidasi backtest 1m riil supaya cek pertama bisa mulai ~menit ke-6 sesi, bukan "
        "~menit ke-18) DAN price spike>=1.5% (3 bar terakhir). Paling efektif dijalankan manual "
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

    # MBSS v2 (user request — Explosive Lane DIHAPUS dari /consensus, cukup
    # muncul di /screendaytrade saja): dulu section "=== EXPLOSIVE LANE ==="
    # di sini, reuse _explosive_score/EXPLOSIVE_MIN_SCORE_BY_REGIME. Sekarang
    # Explosive murni section SDT (lane FAST_RECOVERY/EARLY_RECOVERY,
    # macd_approach_tier baru) -- lihat commands/scan.py screen_daytrade().
    # explosive_picks dipertahankan sbg list KOSONG (bukan dihapus variabelnya)
    # supaya referensi di bawah (SMART-MONEY WATCH exclusion, LONG-HORIZON
    # WATCH "juga di" tag, lock_candidates, all_tickers) tidak perlu diubah satu-satu.
    explosive_picks = []

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

    # MBSS v2 (user request — Explosive Lane diganti sistem baru, dipakai
    # KONSISTEN dengan SDT: lane FAST_RECOVERY/EARLY_RECOVERY, macd_approach_
    # tier baru — lihat screen_daytrade()). _explosive_score (formula lama)
    # sudah tidak dipakai di mana pun lagi.
    explosive_tickers = [
        r["ticker"] for r in pool
        if r["ticker"] not in prime_tickers and r.get("macd_approach_tier") in ("FAST_RECOVERY", "EARLY_RECOVERY")
    ][:EXPLOSIVE_MAX_NAMES]

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
