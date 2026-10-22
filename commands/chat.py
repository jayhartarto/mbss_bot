"""
commands/chat.py — Command Layer: /tanya freeform stock discussion chat (MBSS v2, user request)

/tanya <pertanyaan bebas> — user ngobrol bebas dengan Gemini soal saham
("saham apa yang lebih bagus sekarang", "bandingkan A dan B", "entry
sekarang harga X volume Y masih bagus atau telat?"), dengan konteks
otomatis dari data yang SUDAH ADA di sistem: konsensus lintas-tool
(reuse commands.scan.compute_consensus_candidates, logika sama dengan
/consensus), HIGH CONVICTION (/hc), dan portofolio (/myportfolio) — semua
dari cache /eodscan, instan. Kalau pertanyaan menyebut ticker spesifik
yang dikenali sistem, /tanya JUGA fetch data live buat ticker itu (mirip
blok pertama /check: compute_factor_scoring + intraday context).

Percakapan multi-turn: history disimpan per chat_id (lihat
core.load_tanya_history / core.save_tanya_turn / core.reset_tanya_history
di engine/legacy_core.py), dikirim ulang ke Gemini tiap pertanyaan baru
supaya bisa nyambung ("saham itu tadi", dst). Data pasar SENDIRI selalu
di-refresh tiap turn (bukan dari history) karena harga/skor bisa berubah
antar pesan dalam percakapan yang sama — lihat core.ask_gemini_chat.

/tanyareset — hapus history percakapan chat ini, mulai obrolan baru.

Same MODULE-import rule as the rest of the Command Layer (see
commands/scan.py docstring): `import engine.legacy_core as core` here,
`import commands.chat as commands_chat` in legacy_core.py's build_app() —
never `from module import name`, and never touch `core.xxx` /
`commands_scan.xxx` at module level, only inside function bodies.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import sqlite3

import engine.legacy_core as core
import engine.nightly as nightly_engine
import engine.scoring as scoring_engine
import commands.scan as commands_scan

TANYA_MAX_LIVE_TICKERS = 3  # bound latency/cost kalau pertanyaan nyebut banyak ticker sekaligus
TANYA_CONSENSUS_TOP_N = 15
TANYA_HC_TOP_N = 10

_TICKER_TOKEN_RE = re.compile(r"\b[A-Za-z]{2,5}\b")


def _extract_mentioned_tickers(question: str, known_tickers: set) -> list:
    """
    Token 2-5 huruf apa pun (case-insensitive — user boleh ketik "bbca" atau
    "BBCA") yang cocok dengan universe yang kita kenal (cache /eodscan +
    posisi/watchlist portofolio). Membership check ke known_tickers-lah yang
    menyaring kata umum ("apa", "yang", dst), bukan pola regex-nya sendiri.
    """
    seen = []
    for tok in _TICKER_TOKEN_RE.findall(question.upper()):
        if tok in known_tickers and tok not in seen:
            seen.append(tok)
    return seen


async def _fetch_live_ticker_context(ticker: str) -> dict | None:
    """
    Data "live-ish" untuk satu ticker — reuse pipeline yang sama dengan blok
    pertama /check (compute_factor_scoring + intraday context), TANPA bagian
    intraday_targets/position P&L yang lebih berat, karena di sini cuma buat
    konteks chat, bukan output utama.
    """
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(scoring_engine.compute_factor_scoring, ticker), timeout=60
        )
    except Exception as e:
        print(f"⚠️ /tanya: gagal compute_factor_scoring {ticker}: {e}")
        return None
    if not result:
        return None

    try:
        intraday_ctx = await asyncio.to_thread(core.fetch_intraday_market_context, ticker)
        if intraday_ctx.get("available"):
            result["intraday_momentum"] = intraday_ctx.get("momentum")
            result["intraday_breakout"] = intraday_ctx.get("breakout")
            result["active_breakout"] = intraday_ctx.get("active_breakout")
            if intraday_ctx.get("price") is not None:
                result["price"] = intraday_ctx["price"]
    except Exception as e:
        print(f"⚠️ /tanya: gagal fetch intraday context {ticker}: {e}")

    return result


WEB_TRIGGERS = (
    "berita", "news", "katalis", "kenapa", "mengapa", "kabar", "isu", "rumor", "dividen", "rups", "right issue", "rights", "ipo",
    "akuisisi", "merger", "buyback", "msci", "bi rate", "suku bunga", "fed ", "kebijakan", "ojk", "bei ", "regulasi",
    "laporan keuangan", "kuartal", "laba", "rugi", "ihsg", "rupiah", "batubara", "nikel", "emas", "minyak", "komoditas", "sektor",
    "outlook", "prospek", "analis", "target harga", "tarif", "pemerintah", "danantara", "asing jual", "net sell",
)


def _wants_web(question: str) -> tuple[bool, str]:
    """Explicit prefixes win: '!web ...' forces search, '!noweb ...' disables it. Otherwise search when the question looks
    news/macro/corporate-action shaped (Google Search grounding costs latency, so it is not on for pure data questions)."""
    q = question.strip()
    low = q.lower()
    if low.startswith("!noweb"):
        return False, q[6:].strip()
    if low.startswith("!web"):
        return True, q[4:].strip()
    return any(k in low for k in WEB_TRIGGERS), q


def _span_value(ticker: str, d_from: str, d_to: str):
    df = core.get_ohlcv_daily_from_db(ticker, 40)  # DatetimeIndex, columns Open/High/Low/Close/Volume
    if df is None or df.empty:
        return None
    idx = df.index.strftime("%Y-%m-%d")
    m = df[(idx >= d_from) & (idx <= d_to)]
    return float((m["Close"] * m["Volume"]).sum()) if not m.empty else None


def _build_extra_context(mentioned: list) -> dict:
    """Context added 2026-10-21: IHSG regime, active swing picks + /rank, BSJP picks, and for the tickers named in the question:
    which lanes hold them, recent broker flow (XL flag), foreign flow. Every block is independent and best-effort - a failure only
    drops that block (named in `unavailable`), never the chat."""
    out: dict = {"today_wib": datetime.datetime.now(core.WIB).strftime("%Y-%m-%d %H:%M WIB"), "unavailable": []}

    try:
        import engine.ihsg_regime as ihsg_regime
        r = ihsg_regime.get_ihsg_regime_today()
        out["ihsg_regime"] = r or "tidak tersedia"
        if not r:
            out["unavailable"].append("ihsg_regime")
    except Exception as e:  # noqa: BLE001
        out["unavailable"].append(f"ihsg_regime ({str(e)[:60]})")

    ranked = []
    try:
        import engine.capital_rank as capital_rank
        ranked = capital_rank.compute_rank()
        keep = ("tier", "age_days", "status", "trigger_date", "entry_ref_price", "entry_low", "entry_high", "sl_price", "tp1_price", "tp2_price")
        out["swing_rank_top10"] = [{
            "ticker": c["pick"].get("ticker"), "lane": c["lane_key"], "ff_priority": c["ff_priority"],
            "rank_score": round(c["score"], 1), "win_rate_pct": c["stats"].get("wlb"),
            "ret_per_day_pct": c["stats"].get("ret_per_day"), "tp_gain_pct": c["tp_gain_pct"],
            **{k: c["pick"].get(k) for k in keep if c["pick"].get(k) is not None},
        } for c in ranked[:10]]
        out["swing_active_pick_count"] = len(ranked)
    except Exception as e:  # noqa: BLE001
        out["unavailable"].append(f"swing_rank ({str(e)[:60]})")

    try:
        import engine.bsjp2 as bsjp2
        st = bsjp2._load_json_state("bsjp2_confirmed_picks.json")
        picks = [p for p in (st.get("picks") or []) if p.get("tier") in bsjp2.LADDER_TIERS or p.get("rocket")]
        out["bsjp_confirmed"] = {"as_of": st.get("trading_day_marker"), "picks": [
            {k: p.get(k) for k in ("ticker", "tier", "rocket", "rung", "gap_pct", "ret_2d_cum", "tp1", "sl") if p.get(k) is not None}
            for p in picks[:8]]}
    except Exception as e:  # noqa: BLE001
        out["unavailable"].append(f"bsjp ({str(e)[:60]})")

    if not mentioned:
        return out

    out["mentioned_ticker_context"] = {}
    for t in mentioned:
        out["mentioned_ticker_context"][t] = {"in_swing_lanes": [
            {"lane": c["lane_key"], "age_days": c["pick"].get("age_days"), "status": c["pick"].get("status"), "ff_priority": c["ff_priority"],
             "sl_price": c["pick"].get("sl_price"), "tp1_price": c["pick"].get("tp1_price")}
            for c in ranked if c["pick"].get("ticker") == t] or "tidak ada di lane swing aktif"}

    try:
        import engine.broker_flow_db as flow_db
        import engine.nightly as nightly
        conn = flow_db.connect(nightly.BROKER_FLOW_DB)
        try:
            first = flow_db.recent_flow_summary(conn, mentioned)
            values = {}
            for t, rec in first.items():
                v = _span_value(t, rec["from"], rec["as_of"])
                if v:
                    values[t] = v
            flows = flow_db.recent_flow_summary(conn, mentioned, values)
        finally:
            conn.close()
        for t, rec in flows.items():
            out["mentioned_ticker_context"][t]["broker_flow"] = rec
    except Exception as e:  # noqa: BLE001
        out["unavailable"].append(f"broker_flow ({str(e)[:60]})")

    try:
        import engine.buy_on_weakness as bow
        conn = sqlite3.connect(bow.FF_DB_PATH, timeout=30)
        try:
            for t in mentioned:
                rows = conn.execute(
                    "SELECT date, foreign_net FROM foreign_flow_daily WHERE ticker=? ORDER BY date DESC LIMIT 10", (t,)).fetchall()
                if rows:
                    out["mentioned_ticker_context"][t]["foreign_flow_last10d_shares_net"] = {
                        "net_last5": sum(r[1] or 0 for r in rows[:5]), "net_last10": sum(r[1] or 0 for r in rows),
                        "days_net_buy_of_last10": sum(1 for r in rows if (r[1] or 0) > 0), "latest_date": rows[0][0]}
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        out["unavailable"].append(f"foreign_flow ({str(e)[:60]})")
    return out


def _build_context_bundle(scored: dict, staleness_note, portfolio: dict, live_data: dict, extra: dict | None = None) -> dict:
    """Ringkasan konteks pasar buat Gemini — bukan seluruh field mentah scoring, biar fokus & hemat token."""
    broksum_data = nightly_engine.load_broksum_250()
    backbone_result, _ = nightly_engine.load_backbone_daily_allow_stale()
    market_regime = (backbone_result or {}).get("market_regime")
    qualifying, multi_broker_lines = commands_scan.compute_consensus_candidates(scored, broksum_data, market_regime)

    consensus_summary = [{
        "ticker": r["ticker"], "tools": r["_consensus_tools"],
        "final_score": r.get("scores", {}).get("final"),
        "action_id": r.get("action_id"),
        "risk_reward_at_max": r.get("targets", {}).get("risk_reward_at_max"),
    } for r in qualifying[:TANYA_CONSENSUS_TOP_N]]

    # MBSS v2 (user request 2026-08-30): is_high_conviction GANTI TOTAL ke
    # commands_scan._daytrade_wr_tp1 -- lihat docstring helper itu utk
    # alasan/riwayat audit Task #21. WR individual dari model dipakai
    # langsung (bukan lagi compute_daytrade_score momentum-murni).
    hc_wr_pairs = commands_scan._compute_daytrade_wr_candidates(list(scored.values()))
    hc_summary = [{
        "ticker": r["ticker"], "wr_pct": info["wr_pct"], "tp1_price": info["price"],
        "final_score": r.get("scores", {}).get("final"),
        "risk_reward_at_max": r.get("targets", {}).get("risk_reward_at_max"),
    } for r, info in hc_wr_pairs[:TANYA_HC_TOP_N]]

    positions = portfolio.get("positions", {})
    portfolio_summary = {
        "cash_idr": portfolio.get("cash", 0.0),
        "positions": [
            {"ticker": t, "lots": p["lots"], "avg_price": p["avg_price"]}
            for t, p in positions.items()
        ],
        "watchlist": portfolio.get("watchlist", []),
    }

    return {
        "cache_staleness_warning": staleness_note or None,
        "cross_tool_consensus_top_picks": consensus_summary,
        "high_conviction_top_picks": hc_summary,
        "multi_broker_accumulation": [line for _, line in multi_broker_lines[:5]],
        "user_portfolio": portfolio_summary,
        "live_data_for_tickers_mentioned_in_question": live_data,
        **({"bot_context_2026_10": extra} if extra else {}),
    }


async def tanya_command(update, context):
    question = " ".join(context.args).strip()
    if not question:
        await core.safe_reply(
            update.message,
            "Pakai: /tanya <pertanyaan bebas>\n"
            "Contoh:\n"
            "/tanya saham apa yang lebih bagus buat entry sekarang?\n"
            "/tanya bandingkan BBCA sama BBRI\n"
            "/tanya TLKM entry 3200 volume gede, masih bagus atau telat?\n\n"
            "Obrolannya nyambung antar pesan — pakai /tanyareset buat mulai obrolan baru.\n\n"
            "Tahu regime IHSG, pick /swing + /rank, BSJP, arus asing & broker (XL). Pertanyaan soal berita/makro/aksi korporasi "
            "otomatis dibantu pencarian web. Paksa: awali dengan !web, matikan: !noweb."
        )
        return

    use_search, question = _wants_web(question)
    if not question:
        await core.safe_reply(update.message, "Pertanyaannya kosong setelah !web/!noweb. Contoh: /tanya !web kenapa IHSG turun?")
        return
    await core.safe_reply(update.message, "🌐 Mikir + cari di web dulu..." if use_search else "🤔 Mikir dulu...")

    scored, staleness_note = nightly_engine.load_daily_scan_cache_allow_stale()
    scored = scored or {}
    portfolio = core.load_portfolio()

    known_tickers = (
        set(scored.keys())
        | set(portfolio.get("positions", {}).keys())
        | set(portfolio.get("watchlist", []))
    )
    mentioned = _extract_mentioned_tickers(question, known_tickers)[:TANYA_MAX_LIVE_TICKERS]

    live_data = {}
    for ticker in mentioned:
        live = await _fetch_live_ticker_context(ticker)
        if live:
            live_data[ticker] = live

    try:
        extra = await asyncio.wait_for(asyncio.to_thread(_build_extra_context, mentioned), timeout=90)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ /tanya: konteks tambahan gagal: {e}")
        extra = {"unavailable": ["seluruh konteks tambahan"]}
    context_bundle = _build_context_bundle(scored, staleness_note, portfolio, live_data, extra)
    history = core.load_tanya_history(update.effective_chat.id)

    try:
        answer = await asyncio.to_thread(
            core.ask_gemini_chat, history, question, json.dumps(context_bundle, default=str), core.TANYA_INSTRUCTION, 3, use_search
        )
    except Exception as e:
        msg = str(e)
        if "503" in msg or "429" in msg:
            await core.safe_reply(update.message, "⏳ Gemini sedang sibuk (server Google overload, HTTP 503/429). Itu sementara, bukan masalah bot — coba lagi 1-2 menit.")
        else:
            await core.safe_reply(update.message, f"⚠️ Gagal minta jawaban ke Gemini: {core._redact_secrets(msg)[:200]}")
        return

    await core.safe_reply(update.message, answer)
    core.save_tanya_turn(update.effective_chat.id, question, answer)


async def tanya_reset_command(update, context):
    core.reset_tanya_history(update.effective_chat.id)
    await core.safe_reply(update.message, "🔄 Obrolan /tanya direset — mulai dari awal.")
