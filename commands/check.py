"""
commands/check.py — Command Layer: check/screenshot group (MBSS v2 Sprint 1, Phase 5d)

Telegram handlers for /check (+ /cek), /brokersum, the Broker Sum screenshot
photo handler, and the skip/quick-check inline button callbacks.

All thin handlers — everything they call (safe_reply, compute_factor_scoring,
fetch_intraday_market_context, portfolio state, broker fetch/cache,
build_check_signal_summary, ask_gemini_to_analyze, ...) stays in
engine/legacy_core.py, accessed via `core.xxx`. Same circular-import rule
as every other Command Layer module in this refactor — see
engine/nightly.py's docstring for the full explanation.

BUGFIX (confirmed with user before changing, see conversation history):
`quick_check_callback` originally had 5 extra dangling lines appended to its
body in engine/legacy_core.py — an exact duplicate of `skip_brokersum_callback`'s
code with no `async def` of its own, so Python's indentation rules silently
made them execute as part of quick_check_callback on every tap (popping any
pending brokersum check, clearing the message's reply markup, and sending an
extra "👍 Dilewati." message — none of which has anything to do with
quick-check). Not reproduced here.
"""
from __future__ import annotations

import asyncio
import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import engine.legacy_core as core
import engine.broker as broker_engine
import engine.scoring as scoring_engine
import engine.market as market_engine
import engine.nightly as nightly_engine


async def check_stock(update, context):
    if not context.args:
        await core.safe_reply(update.message,
            "Cara pakai: /check TICKER [zapi]\nContoh: /check BBCA\nContoh (+ data broker Zapi): /check BBCA zapi"
        )
        return

    ticker = context.args[0].upper().strip()
    use_zapi = len(context.args) > 1 and context.args[1].lower() == "zapi"
    await core.safe_reply(update.message, f"🔎 Menganalisa {ticker}, mohon tunggu...")

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(scoring_engine.compute_factor_scoring, ticker), timeout=1800
        )
    except asyncio.TimeoutError:
        await core.safe_reply(update.message, f"⚠️ Timeout mengambil data untuk {ticker}. Coba lagi nanti.")
        return
    except Exception as e:
        await core.safe_reply(update.message, f"⚠️ Gagal mengambil data untuk {ticker}: {e}")
        return

    if not result:
        real_reason = scoring_engine.get_last_exclusion_reason(ticker)
        if real_reason:
            await core.safe_reply(update.message, f"⚠️ Gagal mengambil data untuk {ticker}: {real_reason}")
        else:
            # Fallback generik — dipakai kalau None terjadi di luar 7 titik yang
            # sudah ditandai di compute_factor_scoring() (mis. exception di luar
            # jalur normal), bukan lagi asumsi default seperti sebelumnya.
            await core.safe_reply(update.message,
                f"⚠️ Gagal mengambil data untuk {ticker}. Kemungkinan penyebab:\n"
                "- Kode saham salah atau belum listing cukup lama\n"
                "- Ada masalah jaringan/API sesaat\n\n"
                "Coba lagi setelah beberapa menit."
        )
        return

    sharia_universe = core.fetch_online_sharia_list()
    result["is_sharia"] = ticker in sharia_universe

    # MBSS v2 (user request — memperkaya backlog data untuk adaptive parameter
    # ke depan): kunci /check ke mekanisme winrate yang sama, source="check".
    # SENGAJA cuma untuk sinyal BELI yang genuinely yakin (STRONG_BUY/
    # BUY_ACCUMULATE) — bukan setiap /check. Banyak /check itu cuma rasa
    # penasaran atau cek posisi yang SUDAH dipegang (HOLD/AVOID_SELL/
    # MIXED_SIGNALS), bukan prediksi baru yang layak diuji — kalau semua
    # dikunci tanpa filter, datanya jadi kotor (bukan representasi "sistem
    # merekomendasikan ini", cuma "user pernah lihat ini").
    if result.get("action_id") in ("STRONG_BUY", "BUY_ACCUMULATE"):
        try:
            await asyncio.to_thread(core.lock_daily_daytrade_picks, [result], "check")
        except Exception as e:
            print(f"⚠️ Gagal mengunci pick /check {ticker} untuk /winrate: {e}")

    # Intraday live context (5m bars) for /check only — used to update live price,
    # high/low, momentum, and breakout probability while market is open.
    try:
        intraday_ctx = await asyncio.to_thread(core.fetch_intraday_market_context, ticker)
        result["intraday_momentum"] = intraday_ctx.get("momentum", {"available": False, "reason": "error teknis"})
        result["intraday_breakout"] = intraday_ctx.get("breakout", {"available": False, "reason": "error teknis"})
        result["active_breakout"] = intraday_ctx.get("active_breakout", {"available": False, "reason": "error teknis"})
        result["vwap_movement"] = intraday_ctx.get("vwap_movement", {"available": False, "reason": "error teknis"})
        if intraday_ctx.get("available"):
            if intraday_ctx.get("price") is not None:
                result["price"] = intraday_ctx["price"]
            if intraday_ctx.get("high") is not None:
                result["intraday_high"] = intraday_ctx["high"]
            if intraday_ctx.get("low") is not None:
                result["intraday_low"] = intraday_ctx["low"]
            if intraday_ctx.get("vwap_snapshot"):
                result["intraday_vwap"] = intraday_ctx.get("vwap_snapshot")
    except Exception as e:
        print(f"⚠️ Gagal fetch intraday context untuk {ticker}: {e}")
        result["intraday_momentum"] = {"available": False, "reason": "error teknis"}
        result["intraday_breakout"] = {"available": False, "reason": "error teknis"}
        result["active_breakout"] = {"available": False, "reason": "error teknis"}
        result["vwap_movement"] = {"available": False, "reason": "error teknis", "15m": {"available": False, "reason": "error teknis"}, "30m": {"available": False, "reason": "error teknis"}, "60m": {"available": False, "reason": "error teknis"}, "overall_signal": "N/A"}

    # Intraday targets — entry range/TP1/TP2/SL yang lebih presisi dari data live
    # + history SQLite, TERPISAH dari scoring['targets'] yang dipakai /myportfolio dan winrate
    hist = None
    try:
        hist = core.get_ohlcv_smart(ticker, limit=60)
        result["intraday_targets"] = await asyncio.to_thread(
            core.compute_intraday_targets, ticker, result, hist if not hist.empty else None
        )
    except Exception as e:
        print(f"⚠️ Gagal compute intraday targets untuk {ticker}: {e}")
        result["intraday_targets"] = {}

    # Position-awareness: if this ticker is a current holding, feed the actual
    # position (cost basis, lots, unrealized P&L) into the analysis so the response
    # is framed relative to what the person actually owns, not just generic signal.
    portfolio = core.load_portfolio()
    held_position = portfolio.get("positions", {}).get(ticker)
    if held_position:
        avg_price = held_position["avg_price"]
        lots = held_position["lots"]
        current_price = result["price"]
        unrealized_pnl_pct = ((current_price - avg_price) / avg_price) * 100
        unrealized_pnl_idr = (current_price - avg_price) * lots * core.BOARD_LOT_SIZE
        result["is_held_position"] = True
        result["held_avg_price"] = avg_price
        result["held_lots"] = lots
        result["held_unrealized_pnl_pct"] = round(unrealized_pnl_pct, 2)
        result["held_unrealized_pnl_idr"] = int(unrealized_pnl_idr)
    else:
        result["is_held_position"] = False

    # Free enrichment: if this ticker already has same-day real broker flow data
    # cached (from an earlier /myportfolio brokersum run), pick it up here at ZERO
    # extra API cost — /check never fetches this on its own, only reuses what's
    # already been paid for today. EXCEPT if "zapi" explicitly requested — that
    # always fetches fresh from Zapi (belum diverifikasi apakah update live atau
    # harian, jadi TIDAK dipakai default/otomatis, hanya kalau diminta eksplisit).
    if use_zapi:
        try:
            zapi_brokersum = await asyncio.to_thread(
                broker_engine.compute_brokersum_metrics_zapi, ticker, result.get("cmf"), result.get("obv_divergence")
            )
            if zapi_brokersum:
                result["brokersum"] = zapi_brokersum
                scoring_engine.apply_brokersum_adjustment(result, zapi_brokersum)
                print(f"📋 /check {ticker}: enriched with Zapi brokersum (fresh fetch)")
            else:
                await core.safe_reply(update.message, f"⚠️ Gagal mengambil data Zapi untuk {ticker}, lanjut tanpa data broker.")
        except Exception as e:
            print(f"⚠️ Zapi brokersum gagal untuk {ticker}: {e}")
    else:
        cached_brokersum = broker_engine.get_cached_brokersum(ticker)
        if cached_brokersum:
            result["brokersum"] = cached_brokersum
            print(f"📋 /check {ticker}: enriched with cached same-day brokersum data")

    # Company-specific news + real dividend/split history — this is what can
    # actually surface corporate actions like buybacks or earnings releases,
    # unlike the general market-wide news used in the daily briefs.
    company_news = await asyncio.to_thread(core.fetch_company_news, ticker, result.get("name", ticker))
    # MBSS v2 (user request — real case: NELY/GIAA acquisition headlines):
    # anggota "sudah di-react market atau belum" dari harga sejak tanggal
    # artikel, pakai hist yang sudah di-fetch di atas (zero extra API cost).
    company_news = core.enrich_news_with_price_reaction(company_news, hist, result["price"])
    corporate_actions = await asyncio.to_thread(core.fetch_recent_corporate_actions, ticker)
    result["recent_news"] = company_news  # [{"title", "published", "price_reaction"?}, ...] — bukan cuma title lagi
    result["recent_dividends"] = corporate_actions["recent_dividends"]
    result["recent_splits"] = corporate_actions["recent_splits"]

    # Second message: deterministic, short, and focused on the live signal at /check time.
    # This replaces the earlier long Gemini paragraph that often repeated dashboard fields.
    analysis_text = core.build_check_signal_summary(result)

    # Raw deterministic data — plain Python formatting, NOT AI-generated — so every
    # number Gemini referenced above can be independently cross-checked directly.
    freshness_line = f"⚠️ {result['data_freshness_warning']}\n" if result.get("data_freshness_warning") else ""

    intraday_line = (
        f"Intraday: High {result['intraday_high']} | Low {result['intraday_low']}\n"
        if result.get('intraday_high') else ""
    )

    # ── Icon helpers ──────────────────────────────────────────────
    def _icon_score(v):
        if v is None: return "➖"
        return "✅" if v >= 7.0 else "❗" if v < 5.5 else "➖"

    def _icon_rsi(v):
        if v is None: return "➖"
        if v > 70: return "⚠️"
        if v < 30: return "🔥"
        if v > 60: return "📈"
        if v < 40: return "📉"
        return "➖"

    def _icon_cmf(v):
        if v is None: return "➖"
        if v > 0.1:  return "✅"
        if v > 0.0:  return "📈"
        if v > -0.1: return "❗"
        return "🔴"

    def _icon_vol(v):
        if v is None: return "➖"
        if v >= 3.0: return "🔥"
        if v >= 1.5: return "✅"
        if v >= 1.2: return "📈"
        if v >= 0.8: return "➖"
        return "❗"

    def _icon_adx(v):
        if v is None: return "➖"
        if v >= 30: return "✅"
        if v >= 20: return "📈"
        return "➖"

    def _icon_macd(state):
        return "✅" if state == "bullish" else "❗" if state == "bearish" else "➖"

    def _icon_rs(v):
        if v is None: return "➖"
        if v > 1.0:  return "✅"
        if v < -1.0: return "❗"
        return "➖"

    def _fmt(n):
        """Format angka harga dengan titik ribuan."""
        try: return f"{int(n):,}".replace(",", ".")
        except: return str(n)

    # ── Data alias ─────────────────────────────────────────────────
    price  = result["price"]
    scores = result["scores"]
    it     = result.get("intraday_targets", {})
    im     = result.get("intraday_momentum", {})
    hc     = result.get("high_conviction", {})
    RISK_CHARACTER_LABEL_ID = {
        "BASE_DEFENSIF": "🛡️ BASE/DEFENSIF",
        "SWING_AGRESIF": "⚡ SWING/AGRESIF",
        "NETRAL": "➖ NETRAL",
    }
    risk_label = RISK_CHARACTER_LABEL_ID.get(result.get("risk_character"), "")

    # MBSS v2 (user request — diskusi Bollinger Bands): label human-readable
    # untuk bb_signal_note (lihat compute_factor_scoring) — band_walking_*
    # SENGAJA ditandai "BUKAN sinyal mantul/capek", supaya user tidak salah
    # baca band walking sebagai reversal (persis kekhawatiran user waktu
    # diskusi: "kekuatan arus bisa menabrak aturan ini").
    BB_SIGNAL_LABEL_ID = {
        "near_lower_band_bounce_candidate": "📉 Dekat lower Bollinger Band — kandidat mantul (mean-reversion)",
        "near_upper_band_caution": "📈 Dekat upper Bollinger Band — waspada potensi mantul turun",
        "band_walking_down": "⬇️ Band walking (downtrend kuat) — pelemahan berlanjut, BUKAN sinyal mantul",
        "band_walking_up": "⬆️ Band walking (uptrend kuat) — kelanjutan tren, BUKAN sinyal capek",
    }
    bb_line = ""
    bb_note_text = BB_SIGNAL_LABEL_ID.get(result.get("bb_signal_note"))
    if bb_note_text:
        bb_line += f"\n{bb_note_text}"
    if result.get("bollinger_squeeze"):
        bb_line += f"\n🎯 Bollinger squeeze (bandwidth persentil {result.get('bollinger_bandwidth_percentile', '-')}) — volatilitas terkompresi, potensi pra-breakout (arah belum pasti)"

    # MBSS v2 (RapidAPI integration, user request) — replaces the old
    # "💹 BROKER RIIL" block (which mixed Index Alpha/Zapi/screenshot sources,
    # each with a different calc basis, making day-to-day numbers
    # incomparable). Uniformly sourced from one place now
    # (compute_check_broker_info), so this is the single broker-data section
    # in the whole /check message — deliberately does NOT name the data
    # source in this text, reads as a natural extension of the bot's own
    # broker/bandar vocabulary (per this session's user-facing text
    # convention decision).
    broker_info = broker_engine.compute_check_broker_info(ticker, nightly_engine.load_broksum_250())
    brokersum_line = ""
    if broker_info:
        broker_parts = " | ".join(
            f"{b['code']} [{b['tag']}] {_fmt(b['buy_avg'])}" if b.get("buy_avg") else f"{b['code']} [{b['tag']}]"
            for b in broker_info["top_brokers"]
        )
        netbuy_lot = broker_info["net_buy_top3_volume"] / 100  # 1 lot = 100 lembar
        netbuy_str = f"{netbuy_lot:,.0f} lot (Rp{broker_info['net_buy_top3_value']/1e9:.1f}M)"
        dominance = broker_info.get("dominance_pct")
        dominance_str = f" — {dominance:.0f}% dari total transaksi {broker_info['lookback_days']} hari terakhir" if dominance is not None else ""
        trend = broker_info.get("dominance_trend")
        trend_str = f"\nTren dominasi: {trend}" if trend else ""

        ceiling_price_val = broker_info.get("ceiling_price")
        ceiling_code = broker_info.get("ceiling_code")
        ceiling_line = f"\nCeiling: {_fmt(ceiling_price_val)} (avg tertinggi {ceiling_code})" if ceiling_price_val else ""

        brokersum_line = (
            f"\n💹 BROKER INFO:\n"
            f"Top Broker: {broker_parts}\n"
            f"Net-buy Top-3: {netbuy_str}{dominance_str}"
            f"{trend_str}"
            f"{ceiling_line}"
        )

    # ── Skor baris ─────────────────────────────────────────────────
    sv, sm, ss, sf = scores["value"], scores["momentum"], scores["sentiment"], scores["final"]
    skor_line = (
        f"💯 SKOR FINAL: {sf}\n"
        f"{_icon_score(sv)} Nilai {sv}  |  "
        f"{_icon_score(sm)} Momentum {sm}  |  "
        f"{_icon_score(ss)} Sentimen {ss}"
        f"{bb_line}"
    )

    # ── Target intraday ────────────────────────────────────────────
    # MBSS v2 (RapidAPI integration, user request — "ganti total"): ceiling
    # sekarang SATU-SATUNYA sumbernya broker_info (avg tertinggi di antara
    # top-3 net buyer), bukan lagi avg beli broker net-buy terbesar dari
    # broksum_250 — biar cuma ada satu definisi ceiling di seluruh pesan
    # /check, bukan dua definisi berbeda yang membingungkan.
    ceiling_price = broker_info.get("ceiling_price") if broker_info else None
    ceiling_str = f" / {_fmt(ceiling_price)}*" if ceiling_price else ""

    if it:
        above = " ▲" if it.get("entry_atas_above_price") else ""
        target_lines = (
            f"📈 HARGA & TARGET\n"
            f"Harga  : {_fmt(price)}\n"
            f"Entry  : {_fmt(it['entry_bawah'])} — {_fmt(it['entry_atas'])}{above}{ceiling_str}\n"
            f"TP1    : {_fmt(it['tp1'])}  ({(it['tp1']/price-1)*100:+.1f}%)\n"
        )
        if it.get("tp2"):
            target_lines += f"TP2    : {_fmt(it['tp2'])}  ({(it['tp2']/price-1)*100:+.1f}%)\n"
        target_lines += (
            f"SL     : {_fmt(it['sl'])}  ({(it['sl']/price-1)*100:+.1f}%)\n"
            f"RR     : TP1=1:{it['rr_tp1']}"
            + (f"  |  TP2=1:{it['rr_tp2']}" if it.get("rr_tp2") else "") + "\n"
            f"↳ {it['entry_bawah_context']}"
            # Ceiling explanation moved to the "💹 BROKER INFO" block below —
            # no footnote here anymore, avoids explaining the same number twice.
        )
    else:
        tgt = result.get("targets", {})
        target_lines = (
            f"📈 HARGA & TARGET\n"
            f"Harga  : {_fmt(price)}\n"
            f"Entry  : {tgt.get('buy_range', '?')}{ceiling_str}\n"
            f"TP1    : {_fmt(tgt.get('tp_1', '?'))}\n"
            f"SL     : {_fmt(tgt.get('cut_loss', '?'))}"
        )

    # ── Posisi (kalau saham ini dipegang) ─────────────────────────
    posisi_lines = ""
    portfolio = core.load_portfolio()
    positions = portfolio.get("positions", {})
    ticker = result["ticker"]
    if ticker in positions:
        pos = positions[ticker]
        avg = pos.get("avg_price", 0)
        lots = pos.get("lots", 0)
        days = pos.get("days_held")
        pnl_pct = (price - avg) / avg * 100 if avg else 0
        pnl_rp  = (price - avg) * lots * 100
        pnl_icon = "✅" if pnl_pct >= 0 else ("⚠️" if pnl_pct >= -5 else "🔴")
        posisi_lines = (
            f"\n💼 POSISI SAYA\n"
            f"{pnl_icon} {lots} lot @avg {_fmt(avg)}  "
            f"({pnl_pct:+.1f}%)\n"
            f"P/L  : {'+' if pnl_rp >= 0 else '-'}Rp{abs(pnl_rp):,.0f}"
            + (f"  |  {days} hari" if days else "")
        )

    # ── Intraday status ────────────────────────────────────────────
    intraday_status = ""
    hi = result.get("intraday_high")
    lo = result.get("intraday_low")
    if hi and lo:
        intraday_status += f"\n⚡ INTRADAY\nHigh {_fmt(hi)}  |  Low {_fmt(lo)}\n"
    vwap_fb = result.get("intraday_vwap") or {}
    if vwap_fb.get("available"):
        vwap_ref = vwap_fb.get("vwap_raw", vwap_fb.get("vwap", price))
        vwap_sign = "di atas" if price >= vwap_ref else "di bawah"
        vp = vwap_fb.get("volume_pace_ratio")
        vp_txt = f" | Vol pace {vp}x" if vp is not None else ""
        intraday_status += f"VWAP {_fmt(vwap_fb.get('vwap'))} ({vwap_fb.get('vwap_distance_pct'):+.2f}%, {vwap_sign}){vp_txt}\n"
    if im.get("available"):
        sess = "Sesi 1" if im["session"] == "sesi_1" else "Sesi 2"
        intraday_status += f"Momentum {sess}: {im['reading']} ({im['change_pct']:+.2f}%)\n"
    elif hi and lo:
        intraday_status += "Momentum: di luar jam bursa\n"

    vwap_movement = result.get("vwap_movement") or {}
    intraday_status += core.format_vwap_movement_block(vwap_movement) + "\n"

    br = result.get("intraday_breakout", {})
    if br.get("available"):
        br_icon = "🔥" if br["label"] == "TINGGI" else "📈" if br["label"] == "SEDANG" else "⬇️"
        cluster_note = f" ({br['resistance_cluster_count']}x)" if br.get("resistance_cluster_count", 1) > 1 else ""
        status_note = f"  [{br['breakout_status_label']}]" if br.get("breakout_status_label") else ""
        intraday_status += (
            f"{br_icon} Breakout: {br['label']} ({br['score']}/100){status_note}"
            f"  Resist {_fmt(br['resistance'])}{cluster_note}"
            f"  Jarak {br['distance_pct']:+.1f}%"
        )
        if br.get("volume_warning"):
            intraday_status += f"\n{br['volume_warning']}"
    elif hi and lo:
        br_reason = br.get("reason") or "data belum cukup"
        intraday_status += f"Peluang Breakout: tidak tersedia ({br_reason})"

    ab = result.get("active_breakout", {})
    if ab.get("available"):
        intraday_status += (
            f"\n⚡ Active Breakout: {ab.get('label')} ({ab.get('score')}/100)"
            f"  Trigger {_fmt(ab.get('trigger_price'))}"
            f"  VWAP {_fmt(ab.get('vwap'))}"
            f"  Invalid {_fmt(ab.get('invalidation_level'))}"
        )
        if ab.get("volume_pace_ratio") is not None:
            intraday_status += f"\nVol pace {ab.get('volume_pace_ratio')}x | {ab.get('notes', '')}"

    # ── Conviction + karakter ──────────────────────────────────────
    meta_line = ""
    if hc: meta_line += f"{hc.get('conviction_label', '')}\n"
    if risk_label: meta_line += risk_label

    # ── Raw data dengan icon ───────────────────────────────────────
    rsi  = result.get("rsi")
    cmf  = result.get("cmf")
    vol  = result.get("vol_ratio")
    adx  = result.get("adx")
    macd = result.get("macd_state", "")
    rs   = result.get("relative_strength_vs_ihsg")
    macd_cross = ""
    if result.get("macd_cross_days_ago") is not None:
        macd_cross = f" (cross {result['macd_cross_direction']} {result['macd_cross_days_ago']}hr lalu)"

    raw_data_block = (
        f"─────────────────────\n"
        f"PE {result['pe']}  |  PB {result['pb']}  |  Div {result['dividend_yield_pct']}%\n"
        f"{_icon_rsi(rsi)} RSI {rsi}  |  "
        f"{_icon_cmf(cmf)} CMF {cmf}  |  "
        f"{_icon_vol(vol)} Vol {vol}x\n"
        f"{_icon_macd(macd)} MACD {macd}{macd_cross}  |  "
        f"{_icon_adx(adx)} ADX {adx} ({core.format_adx_label(adx)})\n"
        f"{_icon_rs(rs)} RS vs IHSG {rs}%  |  Range {result['day_range_pct_10d']}%"
        f"{market_engine.format_sector_tag(result.get('sector'), prefix=chr(10))}"
        f"{broker_engine.format_smart_money_tag(ticker, nightly_engine.load_broksum_250())}"
    )

    # MBSS v2 (user request — Bias Bandar di /check, studi kasus manual
    # TMPO/MDIA/JGLE/DOOH/ICON): tampilkan klasifikasi 5 kategori APA ADANYA
    # — di sini (beda dari tools screening lain yang langsung exclude),
    # DISTRIBUSI/TANPA DUKUNGAN justru paling berguna ditampilkan sebagai
    # peringatan eksplisit, karena user sudah punya niat spesifik ke saham
    # ini. Selalu sertakan catatan keterbatasan (histori s/d kemarin).
    bias_label = result.get("bias_bandar")
    bias_block = ""
    if bias_label and bias_label != "BELUM CUKUP DATA":
        BIAS_ICON = {
            "AKUMULASI SEGAR": "🟢", "PULLBACK DIDUKUNG": "🟢",
            "DISTRIBUSI": "🔴", "TANPA DUKUNGAN": "⚪", "AKUMULASI BASI": "🟡",
        }
        icon = BIAS_ICON.get(bias_label, "")
        bias_block = f"\n\n{icon} Bias Bandar: {bias_label} (histori s/d kemarin, belum termasuk aktivitas hari ini)"
        # Peringatan euforia — cuma relevan kalau TANPA DUKUNGAN DAN harga
        # sekarang sudah turun cukup jauh dari puncak intraday hari ini
        # (indikasi ditolak dari titik tertinggi, persis kasus ICON).
        hi = result.get("intraday_high")
        current = result.get("price")
        if bias_label == "TANPA DUKUNGAN" and hi and current and hi > current * 1.03:
            drop_pct = (hi - current) / hi * 100
            bias_block += (
                f"\n⚠️ Sempat capai {hi:.0f} hari ini, sekarang {current:.0f} (turun {drop_pct:.1f}% dari puncak) "
                f"TANPA dukungan broker whitelist — indikasi euforia, waspada entry di area atas."
            )

    # ── Tanggal & jam ──────────────────────────────────────────────
    now_wib = datetime.datetime.now(core.WIB)
    date_str = now_wib.strftime("%d %b %Y")
    session_info = core.get_current_idx_session()
    if session_info == "sesi_1":
        jam_str = "🟢 Sesi 1 berlangsung"
    elif session_info == "sesi_2":
        jam_str = "🟢 Sesi 2 berlangsung"
    else:
        jam_str = "🕐 Di luar jam bursa"

    # ── PESAN 1 — Dashboard ────────────────────────────────────────
    # NOTE (MBSS v2 refactor, Phase 5d bugfix, confirmed with user before
    # changing): freshness_line and brokersum_line used to be computed above
    # but never actually included here — a pre-existing bug in the original
    # code (not introduced by this refactor), meaning a successfully-fetched
    # "💹 BROKER RIIL" block and any data-freshness warning were silently
    # discarded and never reached the user. Wired in below. `intraday_line`
    # is deliberately NOT added — it duplicates what `intraday_status`
    # already shows (High/Low), so it was genuinely dead/superseded code,
    # not a bug; adding it would just print High/Low twice.
    # BUGFIX (ditemukan lewat pertanyaan user — persis pola company_name
    # sebelumnya): recent_news SUDAH di-fetch (fetch_company_news, 1 network
    # call tiap /check) tapi TIDAK PERNAH ditampilkan di manapun — user
    # bayar biaya fetch-nya tanpa pernah lihat hasilnya. Sekarang benar-benar
    # ditampilkan, maksimal 3 judul terbaru.
    # MBSS v2 (user request — real case: NELY/GIAA acquisition headlines):
    # tampilkan reaksi harga sejak tanggal artikel ini muncul (kalau
    # kehitung), supaya kelihatan langsung apakah beritanya kemungkinan
    # sudah "priced in" atau masih belum direspons pasar — bukan cuma
    # dugaan dari narasi Gemini.
    news_block = ""
    if result.get("recent_news"):
        news_lines = []
        for item in result["recent_news"][:3]:
            reaction = item.get("price_reaction")
            reaction_str = (
                f" ({reaction['days_ago']} hari lalu, harga {reaction['price_change_since_pct']:+.1f}% sejak saat itu)"
                if reaction else ""
            )
            news_lines.append(f"• {item['title']}{reaction_str}")
        news_block = f"📰 Berita Terkini\n" + "\n".join(news_lines)

    msg1 = "\n".join(filter(None, [
        f"{result.get('name', ticker)} ({ticker})",
        f"📅 {date_str}  |  {jam_str}",
        "",
        freshness_line,
        f"🎯 {result['action_label_id']}",
        meta_line,
        "",
        skor_line,
        "",
        target_lines,
        posisi_lines,
        intraday_status,
        "",
        raw_data_block + bias_block,
        brokersum_line,
        news_block,
    ]))
    await core.safe_reply(update.message, msg1)

    # Pesan 2 — sinyal ringkas pada waktu /check dijalankan
    await core.safe_reply(update.message, analysis_text)

    # Offer optional Broker Sum screenshot enrichment — SKIP entirely kalau sudah
    # ada data broker dari sumber lain (cache Index Alpha, atau baru saja fetch
    # Zapi via "/check TICKER zapi") — menawarkan lagi untuk data yang sama itu
    # redundant dan bisa membingungkan ("kenapa ditawari lagi, sudah ada datanya").
    if not result.get("brokersum"):
        core.PENDING_BROKERSUM_CHECKS[update.effective_chat.id] = {
            "ticker": ticker,
            "expires_at": datetime.datetime.now(core.WIB) + datetime.timedelta(minutes=core.PENDING_BROKERSUM_TIMEOUT_MINUTES),
        }
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Lewati (tidak ada screenshot)", callback_data="skip_brokersum")]])
        await update.message.reply_text(
            f"📸 Ada info Broker Sum untuk {ticker} hari ini dari app-mu? Kirim screenshot kalau ada, "
            f"atau lewati saja.",
            reply_markup=keyboard,
        )


async def skip_brokersum_callback(update, context):
    query = update.callback_query
    await query.answer()
    core.PENDING_BROKERSUM_CHECKS.pop(query.message.chat_id, None)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("👍 Dilewati.")


async def quick_check_callback(update, context):
    """
    Tombol shortcut ticker diklik — hasil RINGKAS (bukan replika penuh /check,
    yang punya opsi zapi/brokersum yang butuh interaksi tambahan) supaya cocok
    untuk use case "lihat cepat" dari tombol, bukan analisis mendalam. Kalau
    butuh detail lengkap (brokersum, dll), tetap arahkan ke /check manual.
    Sengaja TIDAK menyentuh/reuse check_stock() — dibangun terpisah dari fungsi
    inti yang sudah stabil (compute_factor_scoring, fetch_intraday_momentum)
    untuk menghindari risiko mengubah command /check yang sudah established.
    """
    query = update.callback_query
    await query.answer("Mengambil data...")
    ticker = query.data.replace("qchk_", "")

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(scoring_engine.compute_factor_scoring, ticker), timeout=1800
        )
    except Exception as e:
        await query.message.reply_text(f"⚠️ Gagal mengambil data {ticker}: {e}")
        return
    if not result:
        await query.message.reply_text(f"⚠️ Gagal mengambil data {ticker}.")
        return

    try:
        momentum = await asyncio.to_thread(core.fetch_intraday_momentum, ticker)
    except Exception:
        momentum = {"available": False}

    momentum_line = ""
    if momentum.get("available"):
        session_label = "Sesi 1" if momentum["session"] == "sesi_1" else "Sesi 2"
        momentum_line = f"\nMomentum {session_label}: {momentum['reading']} ({momentum['change_pct']:+.2f}%)"

    targets = result.get("targets", {})
    rr_max = targets.get("risk_reward_at_max")
    rr_line = f" | RR: 1:{rr_max}" if rr_max is not None else ""

    text = (
        f"⚡ {ticker} — {result.get('name', '')}\n"
        f"Harga: {result['price']} | RSI: {result['rsi']} | ADX: {result['adx']}\n"
        f"{result['action_label_id']}{momentum_line}\n"
        f"Entry: {targets.get('buy_range', '?')} | SL: {targets.get('cut_loss', '?')} | "
        f"TP1: {targets.get('tp_1', '?')}{rr_line}\n\n"
        f"Detail lengkap: /check {ticker}"
    )
    await query.message.reply_text(text)


async def handle_brokersum_photo(update, context):
    chat_id = update.effective_chat.id
    pending = core.PENDING_BROKERSUM_CHECKS.get(chat_id)

    if not pending:
        # No recent /check waiting for a photo — don't guess which ticker this is for.
        return
    if datetime.datetime.now(core.WIB) > pending["expires_at"]:
        core.PENDING_BROKERSUM_CHECKS.pop(chat_id, None)
        await core.safe_reply(update.message, "⏱️ Waktu untuk kirim screenshot sudah lewat. Jalankan /check lagi kalau masih ingin menambahkan.")
        return

    ticker = pending["ticker"]
    core.PENDING_BROKERSUM_CHECKS.pop(chat_id, None)  # consume the pending state either way

    await core.safe_reply(update.message, f"🔍 Membaca screenshot untuk {ticker}...")

    try:
        photo = update.message.photo[-1]  # highest resolution available
        photo_file = await context.bot.get_file(photo.file_id)
        image_bytes = await photo_file.download_as_bytearray()

        extracted = await asyncio.to_thread(
            broker_engine.extract_brokersum_from_screenshot, bytes(image_bytes), "image/jpeg", ticker
        )
    except Exception as e:
        await core.safe_reply(update.message, f"⚠️ Gagal memproses gambar: {e}\nHasil /check {ticker} sebelumnya tidak berubah.")
        return

    if not extracted.get("success"):
        reason = extracted.get("reason_if_failed", "tidak jelas")
        await core.safe_reply(
            update.message,
            f"⚠️ Tidak bisa membaca data Broker Sum dari gambar ini ({reason}).\n"
            f"Hasil /check {ticker} sebelumnya tidak berubah — silakan coba screenshot lain kalau mau."
        )
        return

    brokersum = broker_engine.compute_brokersum_from_screenshot_data(extracted)
    if brokersum is None:
        await core.safe_reply(
            update.message,
            f"⚠️ Gemini menandai ekstraksi berhasil, tapi angka inti (buy/sell/net asing) "
            f"ternyata tidak terbaca sama sekali.\n"
            f"Hasil /check {ticker} sebelumnya tidak berubah — silakan coba screenshot lain kalau mau."
        )
        return
    await core.safe_reply(
        update.message,
        f"✅ Berhasil dibaca dari screenshot:\n"
        f"Broker Flow ALL 3D: {brokersum['net_foreign_flow_pct']}% (Rp{brokersum['net_foreign_flow_idr']:,})\n"
        f"Konsentrasi: {brokersum['broker_concentration_pct']}%\n"
        f"Smart Money: {brokersum.get('smart_money_flow_label', 'NEUTRAL')} "
        f"({brokersum.get('smart_money_confirmation_score', 0):+})\n"
        f"Top3 Buyer: {brokersum.get('top3_buy_concentration_pct', brokersum.get('broker_concentration_pct'))}% | "
        f"Top3 Seller: {brokersum.get('top3_sell_concentration_pct', '-')}%\n"
        f"Buyer Avg: {brokersum.get('dominant_buyer_avg') or '-'}\n"
        f"Reason: {'; '.join((brokersum.get('smart_money_reasons') or [])[:3]) or '-'}\n\n"
        f"Menerapkan konfirmasi smart money ke analisis {ticker}..."
    )

    try:
        scoring = await asyncio.wait_for(asyncio.to_thread(scoring_engine.compute_factor_scoring, ticker), timeout=1800)
    except Exception as e:
        await core.safe_reply(update.message, f"⚠️ Gagal mengambil ulang data {ticker} untuk menerapkan brokersum: {e}")
        return
    if not scoring:
        await core.safe_reply(update.message, f"⚠️ Gagal mengambil ulang data {ticker}.")
        return

    brokersum["proxy_agreement"] = "not_available"
    cmf = scoring.get("cmf")
    obv_divergence = scoring.get("obv_divergence")
    if cmf is not None and isinstance(cmf, (int, float)):
        proxy_bullish = cmf > 0
        real_bullish = brokersum["net_foreign_flow_pct"] > 0
        if obv_divergence == "bearish_divergence" and brokersum["net_foreign_flow_pct"] > 5:
            brokersum["proxy_agreement"] = "CONTRADICTION: proxy showed bearish OBV divergence but real broker flow (screenshot) is net positive"
        elif obv_divergence == "bullish_divergence" and brokersum["net_foreign_flow_pct"] < -5:
            brokersum["proxy_agreement"] = "CONTRADICTION: proxy showed bullish OBV divergence but real broker flow (screenshot) is net negative"
        elif proxy_bullish == real_bullish:
            brokersum["proxy_agreement"] = "confirms_proxy"
        else:
            brokersum["proxy_agreement"] = "diverges_from_proxy"

    scoring["brokersum"] = brokersum
    scoring_engine.apply_brokersum_adjustment(scoring, brokersum)

    # Cache under the same trading-day-aware key Index Alpha uses, so this doesn't
    # get needlessly re-fetched from Index Alpha for the same ticker/day, and vice versa.
    brokersum["trend"] = broker_engine.compute_brokersum_trend(ticker, brokersum["net_foreign_flow_idr"])
    cache = broker_engine._load_brokersum_cache()
    cache[ticker] = {"date": broker_engine.get_last_published_trading_day(), "data": brokersum}
    broker_engine._save_brokersum_cache(cache)
    broker_engine.append_brokersum_history(ticker, brokersum)

    # MBSS v2 (user request — kerangka bandarmology 4-langkah): checklist
    # transparan, TERPISAH dari narasi Gemini di bawah, supaya angka/fakta
    # mentahnya selalu terlihat apa adanya sebelum interpretasi apa pun.
    try:
        acc = broker_engine.assess_smart_accumulation(scoring, brokersum)
        if acc["checklist"]:
            lines = [f"🔎 CHECKLIST AKUMULASI — {ticker}", acc["summary_label"], ""]
            for c in acc["checklist"]:
                icon = "✅" if c["terpenuhi"] else "➖"
                lines.append(f"{icon} {c['kriteria']}\n   {c['detail']}")
            ceiling = broker_engine.get_broker_entry_ceiling(brokersum)
            if ceiling:
                lines.append(
                    f"\n📌 Referensi entry ceiling: Rp{ceiling['avg_price']:,.0f} — avg beli "
                    f"{ceiling['broker_label']} {ceiling['code']} ({ceiling['share_pct']}% dari net-buy "
                    f"teridentifikasi). Stopper chase, bukan level teknikal resmi — belum kelihatan di "
                    f"pesan Entry sebelumnya karena screenshot ini baru masuk sesudahnya."
                )
            lines.append(
                "\n⚠️ Ini konteks tambahan, BUKAN sinyal beli/jual pasti — data broker "
                "(terutama jenis broker) dikumpulkan manual & bisa basi/keliru, selalu "
                "silang-cek dengan chart/RR sebelum putuskan."
            )
            await core.safe_reply(update.message, "\n\n".join(lines))
    except Exception as e:
        print(f"⚠️ Gagal menghitung smart accumulation checklist: {e}")

    analysis_text = core.ask_gemini_to_analyze([scoring], core.SINGLE_CHECK_INSTRUCTION)
    await core.safe_reply(update.message, analysis_text)


async def brokersum_upload_command(update, context):
    """
    Manual Broker Summary upload:
    /brokersum TICKER
    Lalu kirim screenshot Broker Summary tab ALL, periode 3 hari, Net aktif.
    """
    if not context.args:
        await core.safe_reply(
            update.message,
            "Cara pakai: /brokersum TICKER\n"
            "Contoh: /brokersum BULL\n\n"
            "Lalu kirim screenshot Broker Summary:\n"
            "• Tab: ALL\n"
            "• Periode: 3 hari bursa\n"
            "• Mode: Net aktif\n"
            "• Pastikan kode broker, volume/lot, average, dan total terlihat."
        )
        return

    ticker = context.args[0].upper().strip()
    chat_id = update.effective_chat.id

    core.PENDING_BROKERSUM_CHECKS[chat_id] = {
        "ticker": ticker,
        "expires_at": datetime.datetime.now(core.WIB) + datetime.timedelta(minutes=core.PENDING_BROKERSUM_TIMEOUT_MINUTES),
        "source": "manual_all_3d",
    }

    await core.safe_reply(
        update.message,
        f"📸 Silakan kirim screenshot Broker Summary {ticker}.\n\n"
        "Format wajib:\n"
        "• Tab: ALL\n"
        "• Periode: 3 hari bursa\n"
        "• Mode: Net aktif\n"
        "• Kode broker Buy/Sell, volume/lot, average, dan total harus terlihat.\n\n"
        "Bot akan membaca ini sebagai konfirmasi bandarmology/smart money, bukan foreign-only."
    )
