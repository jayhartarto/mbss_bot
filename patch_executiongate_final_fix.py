from pathlib import Path
import re

path = Path("bot_dev.py")
src = path.read_text(encoding="utf-8")

def sub_once(pattern, replacement, text, name="block"):
    new_text, n = re.subn(pattern, replacement, text, flags=re.DOTALL)
    if n != 1:
        raise SystemExit(f"{name} replacement failed, matched={n}")
    return new_text

# 1) Screendaytrade autopicks: cache only, no universe fallback.
src = sub_once(
    r"def get_executiongate_screendaytrade_autopicks\(count: int = EXECUTION_GATE_AUTOPICKS\) -> list:\n.*?\n(?=def _executiongate_decision_original)",
    r'''
def get_executiongate_screendaytrade_autopicks(count: int = EXECUTION_GATE_AUTOPICKS) -> list:
    """Use today's daily scan cache only; never fallback to a full-universe scan."""
    scored = load_daily_scan_cache()
    if not scored:
        print("⚠️ ExecutionGate: daily scan cache kosong. Jalankan /screendaytrade dulu.")
        return []

    records = list(scored.values()) if isinstance(scored, dict) else list(scored)
    if not records:
        print("⚠️ ExecutionGate: daily scan cache ada tapi kosong.")
        return []

    pre, _ = filter_and_rank_daytrade_candidates(records, count=max(count, 20))
    ranked = sorted(pre, key=compute_daytrade_score, reverse=True)
    return ranked[:count]
''',
    src,
    "get_executiongate_screendaytrade_autopicks",
)

# 2) Execution gate: portfolio/watchlist first, then screendaytrade cache, then testbrief.
src = sub_once(
    r"async def executiongate_command\(update: Update, context: ContextTypes.DEFAULT_TYPE\):\n.*?\n(?=DAYTRADE_FINAL_PICKS_COUNT = 12)",
    r'''
async def executiongate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = get_executiongate_session_status()
    if not status["allowed"]:
        await safe_reply(update.message, f"⛔ /executiongate hanya aktif saat market live atau break siang. Status sekarang: {status['label']}.")
        return

    await safe_reply(
        update.message,
        f"🧭 Execution Gate berjalan ({status['label']}). "
        f"Mengambil myportfolio/watchlist dulu, lalu cache screendaytrade/testbrief (tanpa fetch all stock)..."
    )

    try:
        extra_sources = await asyncio.to_thread(build_executiongate_extra_candidates, EXECUTION_GATE_MAX_WATCHLIST)
        portfolio_watchlist = extra_sources.get("portfolio_watchlist", [])
        testbrief = extra_sources.get("testbrief", [])

        autopicks = await asyncio.wait_for(
            asyncio.to_thread(get_executiongate_screendaytrade_autopicks, EXECUTION_GATE_AUTOPICKS),
            timeout=1800
        )

        watch = []
        seen = set()

        def append_ticker(ticker, source_label):
            t = str(ticker or "").upper().strip()
            if not t or t in seen:
                return
            base = compute_factor_scoring(t, include_quote_check=False)
            if not base:
                return
            base["executiongate_source"] = source_label
            watch.append(base)
            seen.add(t)

        for t in portfolio_watchlist:
            append_ticker(t, "myportfolio+watchlist")
            if len(watch) >= EXECUTION_GATE_MAX_WATCHLIST:
                break

        if len(watch) < EXECUTION_GATE_MAX_WATCHLIST:
            for item in autopicks:
                t = item.get("ticker") if isinstance(item, dict) else item
                append_ticker(t, "screendaytrade")
                if len(watch) >= EXECUTION_GATE_MAX_WATCHLIST:
                    break

        if len(watch) < EXECUTION_GATE_MAX_WATCHLIST:
            for t in testbrief:
                append_ticker(t, "testbrief")
                if len(watch) >= EXECUTION_GATE_MAX_WATCHLIST:
                    break

        if not watch:
            await safe_reply(
                update.message,
                "⚠️ Tidak ada kandidat dari myportfolio/watchlist maupun cache hari ini. "
                "Isi portofolio/watchlist dulu atau jalankan /screendaytrade."
            )
            return

        evaluated = await asyncio.wait_for(
            asyncio.to_thread(
                evaluate_executiongate_watchlist,
                watch[:EXECUTION_GATE_MAX_WATCHLIST],
            ),
            timeout=1800
        )
    except asyncio.TimeoutError:
        await safe_reply(update.message, "⏱️ Execution Gate timeout. Coba ulang beberapa menit lagi.")
        return
    except Exception as e:
        await safe_reply(update.message, f"⚠️ Execution Gate gagal: {str(e)[:200]}")
        return

    lines = [f"🧭 EXECUTION GATE — {status['label']}\n"]
    lines.append("ENTER sangat ketat: harga harus sehat vs VWAP, active breakout valid, vol pace hidup, risk/RR tidak buruk.\n")
    shown = 0
    for r in evaluated[:EXECUTION_GATE_MAX_WATCHLIST]:
        shown += 1
        icon = "🟢" if r["decision"] == "ENTER" else ("🟡" if r["decision"] == "WATCH" else "🔴")
        vp = r.get("vol_pace") if r.get("vol_pace") is not None else "-"
        lines.append(
            f"{shown}. {icon} {r['ticker']} — {r['decision']} ({r.get('gate_score',0)}/100) [{r.get('source','?')}]\n"
            f"   Breakout {r.get('breakout_score',0)}/100 | Risk {r.get('risk_score',0)}/100 | Active {r.get('active_score',0)}/100 {r.get('active_label','')}\n"
            f"   Harga {smart_round_price(r.get('price',0))} | VWAP dist {r.get('vwap_dist','-')}% | Vol pace {vp}x | Trigger {r.get('trigger') or '-'}\n"
            f"   Aksi: {r.get('action','-')} | Alasan: {', '.join(r.get('reasons',[])[:4])}\n"
        )
    lines.append("\nRule: ENTER boleh dipertimbangkan; WATCH tunggu /check membaik; FAIL no entry.")
    await send_long_message(update.message, "\n".join(lines))
''',
    src,
    "executiongate_command",
)

path.write_text(src, encoding="utf-8")
print("patched:", path)
