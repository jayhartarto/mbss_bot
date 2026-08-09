from pathlib import Path
import re

path = Path("bot_dev.py")
src = path.read_text(encoding="utf-8")

def sub_once(pattern, replacement, text, name):
    new_text, n = re.subn(pattern, replacement, text, flags=re.DOTALL)
    if n != 1:
        raise SystemExit(f"{name} replacement failed, matched={n}")
    return new_text

# 1) build_executiongate_extra_candidates -> portfolio/watchlist + testbrief only
src = sub_once(
r"def build_executiongate_extra_candidates\(.*?\n(?=async def executiongate_command)",
r'''
def build_executiongate_extra_candidates(max_items=12):
    """
    Kandidat tambahan Execution Gate dari sumber yang relevan:
    - myportfolio + watchlist
    - testbrief / daily scan cache

    Tidak fetch top gainer seluruh universe.
    Return dict:
      {
        "portfolio_watchlist": [...],
        "testbrief": [...],
        "combined": [...],
      }
    """
    def _norm(ticker):
        t = str(ticker or "").upper().strip()
        return t if t else None

    def _dedupe(seq):
        out = []
        seen = set()
        for item in seq:
            t = _norm(item)
            if not t or t in seen:
                continue
            seen.add(t)
            out.append(t)
        return out

    portfolio_watchlist = []
    testbrief = []

    try:
        pf = load_portfolio()

        positions = pf.get("positions") or pf.get("holdings") or {}
        if isinstance(positions, dict):
            portfolio_watchlist.extend(positions.keys())
        elif isinstance(positions, list):
            for item in positions:
                if isinstance(item, str):
                    portfolio_watchlist.append(item)
                elif isinstance(item, dict):
                    portfolio_watchlist.append(item.get("ticker") or item.get("code"))

        portfolio_watchlist.extend(pf.get("watchlist", []) or [])
    except Exception as e:
        print(f"⚠️ executiongate portfolio/watchlist gagal: {str(e)[:80]}")

    try:
        daily = load_daily_scan_cache()
        rows = []
        if isinstance(daily, dict):
            rows = list(daily.values())
        elif isinstance(daily, list):
            rows = daily

        def get_score(r):
            if not isinstance(r, dict):
                return 0
            return float(
                r.get("final_score")
                or r.get("score")
                or r.get("radar_score")
                or r.get("v5_total")
                or 0
            )

        rows = sorted(rows, key=get_score, reverse=True)
        for r in rows[:max_items]:
            if isinstance(r, str):
                testbrief.append(r)
            elif isinstance(r, dict):
                testbrief.append(r.get("ticker") or r.get("code"))
    except Exception as e:
        print(f"⚠️ executiongate testbrief gagal: {str(e)[:80]}")

    portfolio_watchlist = _dedupe(portfolio_watchlist)[:max_items]
    testbrief = _dedupe(testbrief)[:max_items]
    combined = _dedupe(portfolio_watchlist + testbrief)[:max_items]

    print(
        f"📋 ExecutionGate sources: portfolio/watchlist={len(portfolio_watchlist)}, "
        f"testbrief={len(testbrief)}, combined={len(combined)}"
    )

    return {
        "portfolio_watchlist": portfolio_watchlist,
        "testbrief": testbrief,
        "combined": combined,
    }
''',
"build_executiongate_extra_candidates"
)

# 2) get_executiongate_screendaytrade_autopicks -> cache only, no fallback scan
src = sub_once(
r"def get_executiongate_screendaytrade_autopicks\(count: int = EXECUTION_GATE_AUTOPICKS\) -> list:\n.*?\n(?=def _executiongate_decision_original)",
r'''
def get_executiongate_screendaytrade_autopicks(count: int = EXECUTION_GATE_AUTOPICKS) -> list:
    """Execution Gate only uses today's daily scan cache. No full-universe fallback."""
    scored = load_daily_scan_cache()
    if not scored:
        print("⚠️ ExecutionGate: daily scan cache kosong. Jalankan /screendaytrade dulu.")
        return []

    records = list(scored.values()) if isinstance(scored, dict) else list(scored)
    if not records:
        print("⚠️ ExecutionGate: daily scan cache ada tapi kosong.")
        return []

    pre, _ = filter_and_rank_daytrade_candidates(records, count=max(count, 20))
    ranked = sorted(pre, key=lambda r: compute_scalaling_readiness(r)["score"], reverse=True)
    return ranked[:count]
''',
"get_executiongate_screendaytrade_autopicks"
)

# typo-safe fix if previous replacement spells compute_scaling_readiness accidentally
src = src.replace("compute_scalaling_readiness", "compute_scaling_readiness")

# 3) executiongate_command -> portfolio/watchlist first, then screendaytrade cache, then testbrief
src = sub_once(
r"async def executiongate_command\(update: Update, context: ContextTypes.DEFAULT_TYPE\):\n.*?\n(?=def split_message|# ── CLI mode:|if __name__ == '__main__')",
r'''
async def executiongate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = get_executiongate_session_status()
    if not status["allowed"]:
        await safe_reply(update.message, f"⛔ /executiongate hanya aktif saat market live atau break siang. Status sekarang: {status['label']}.")
        return

    await safe_reply(
        update.message,
        f"🧭 Execution Gate berjalan ({status['label']}). "
        f"Mengambil myportfolio/watchlist + cache screendaytrade/testbrief (tanpa fetch all stock)..."
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

        def add_ticker(ticker, source_label):
            t = str(ticker or "").upper().strip()
            if not t or t in seen:
                return
            base = compute_factor_scoring(t, include_quote_check=False)
            if not base:
                return
            base["executiongate_source"] = source_label
            watch.append(base)
            seen.add(t)

        # Prioritas: portfolio/watchlist dulu, lalu screendaytrade cache, lalu testbrief cache.
        for t in portfolio_watchlist:
            add_ticker(t, "myportfolio+watchlist")
            if len(watch) >= EXECUTION_GATE_MAX_WATCHLIST:
                break

        if len(watch) < EXECUTION_GATE_MAX_WATCHLIST:
            for item in autopicks:
                if isinstance(item, dict):
                    t = item.get("ticker")
                else:
                    t = item
                add_ticker(t, "screendaytrade")
                if len(watch) >= EXECUTION_GATE_MAX_WATCHLIST:
                    break

        if len(watch) < EXECUTION_GATE_MAX_WATCHLIST:
            for t in testbrief:
                add_ticker(t, "testbrief")
                if len(watch) >= EXECUTION_GATE_MAX_WATCHLIST:
                    break

        if not watch:
            await safe_reply(
                update.message,
                "⚠️ Tidak ada kandidat dari myportfolio/watchlist maupun cache hari ini. "
                "Isi portofolio / watchlist dulu atau jalankan /screendaytrade."
            )
            return

        evaluated = await asyncio.wait_for(
            asyncio.to_thread(evaluate_executiongate_watchlist, watch[:EXECUTION_GATE_MAX_WATCHLIST]),
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
"executiongate_command"
)

path.write_text(src, encoding="utf-8")
print("patched:", path)
