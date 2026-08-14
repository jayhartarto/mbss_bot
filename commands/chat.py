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
import json
import re

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


def _build_context_bundle(scored: dict, staleness_note, portfolio: dict, live_data: dict) -> dict:
    """Ringkasan konteks pasar buat Gemini — bukan seluruh field mentah scoring, biar fokus & hemat token."""
    broksum_data = nightly_engine.load_broksum_250()
    qualifying, multi_broker_lines = commands_scan.compute_consensus_candidates(scored, broksum_data)

    consensus_summary = [{
        "ticker": r["ticker"], "tools": r["_consensus_tools"],
        "final_score": r.get("scores", {}).get("final"),
        "action_id": r.get("action_id"),
        "risk_reward_at_max": r.get("targets", {}).get("risk_reward_at_max"),
    } for r in qualifying[:TANYA_CONSENSUS_TOP_N]]

    hc_candidates = [r for r in scored.values() if r.get("high_conviction", {}).get("is_high_conviction")]
    for r in hc_candidates:
        r["_daytrade_score_hc"] = core.compute_daytrade_score(r)
    hc_candidates.sort(key=lambda r: r["_daytrade_score_hc"], reverse=True)
    hc_summary = [{
        "ticker": r["ticker"], "daytrade_score": round(r["_daytrade_score_hc"], 1),
        "final_score": r.get("scores", {}).get("final"),
        "risk_reward_at_max": r.get("targets", {}).get("risk_reward_at_max"),
    } for r in hc_candidates[:TANYA_HC_TOP_N]]

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
            "Obrolannya nyambung antar pesan — pakai /tanyareset buat mulai obrolan baru."
        )
        return

    await core.safe_reply(update.message, "🤔 Mikir dulu...")

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

    context_bundle = _build_context_bundle(scored, staleness_note, portfolio, live_data)
    history = core.load_tanya_history(update.effective_chat.id)

    try:
        answer = await asyncio.to_thread(
            core.ask_gemini_chat, history, question, json.dumps(context_bundle, default=str)
        )
    except Exception as e:
        await core.safe_reply(update.message, f"⚠️ Gagal minta jawaban ke Gemini: {str(e)[:200]}")
        return

    await core.safe_reply(update.message, answer)
    core.save_tanya_turn(update.effective_chat.id, question, answer)


async def tanya_reset_command(update, context):
    core.reset_tanya_history(update.effective_chat.id)
    await core.safe_reply(update.message, "🔄 Obrolan /tanya direset — mulai dari awal.")
