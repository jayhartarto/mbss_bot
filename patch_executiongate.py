from pathlib import Path
import re

path = Path("bot_dev.py")
src = path.read_text(encoding="utf-8")

# 1) Ganti helper kandidat ekstra supaya ngasih list per-sumber, bukan top gainer.
helper_pattern = r"def build_executiongate_extra_candidates\(.*?\n(?=async def executiongate_command)"
helper_replacement = r'''
def build_executiongate_extra_candidates(max_items=12):
    """
    Kandidat tambahan Execution Gate dari sumber yang relevan:
    - myportfolio + watchlist
    - testbrief / daily scan cache

    Tidak fetch top gainer seluruh universe.
    Return:
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

    # 1. Portfolio: holdings + watchlist
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

    # 2. Testbrief / daily scan cache
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
'''
src, n = re.subn(helper_pattern, helper_replacement, src, flags=re.DOTALL)
if n != 1:
    raise SystemExit(f"helper replacement failed, matched={n}")

# 2) Ganti kalimat status awal.
old_msg = 'await safe_reply(update.message, f"🧭 Execution Gate berjalan ({status[\'label\']}). Mengambil 12 autopick screendaytrade + 8 top gainer 30 menit pertama...")'
new_msg = 'await safe_reply(update.message, f"🧭 Execution Gate berjalan ({status[\'label\']}). Mengambil screendaytrade + testbrief + myportfolio/watchlist (tanpa top gainer)...")'
if old_msg not in src:
    raise SystemExit("status message not found")
src = src.replace(old_msg, new_msg, 1)

# 3) Ganti block top gainer dengan flow baru.
old_block = '''
        topg = await asyncio.to_thread(build_executiongate_extra_candidates, EXECUTION_GATE_TOP_GAINERS)  # portfolio/watchlist/testbrief whitelist

        scored_by_ticker = {r.get("ticker"): r for r in autopicks if r.get("ticker")}
        watch = list(scored_by_ticker.values())
        for tg in topg:
            t = tg.get("ticker")
            if not t or t in scored_by_ticker:
                continue
            base = compute_factor_scoring(t, include_quote_check=False)
            if not base:
                continue
            base["executiongate_source"] = f"top30m +{tg.get('first30_change_pct')}%"
            base["first30_change_pct"] = tg.get("first30_change_pct")
            watch.append(base)
            if len(watch) >= EXECUTION_GATE_MAX_WATCHLIST:
                break

        evaluated = await asyncio.wait_for(asyncio.to_thread(evaluate_executiongate_watchlist, watch[:EXECUTION_GATE_MAX_WATCHLIST]), timeout=1800)
'''
new_block = '''
        extra_sources = await asyncio.to_thread(build_executiongate_extra_candidates, EXECUTION_GATE_MAX_WATCHLIST)
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
            for t in testbrief:
                append_ticker(t, "testbrief")
                if len(watch) >= EXECUTION_GATE_MAX_WATCHLIST:
                    break

        evaluated = await asyncio.wait_for(
            asyncio.to_thread(
                evaluate_executiongate_watchlist,
                watch[:EXECUTION_GATE_MAX_WATCHLIST],
            ),
            timeout=1800
        )
'''
if old_block not in src:
    raise SystemExit("executiongate block not found")
src = src.replace(old_block, new_block, 1)

# 4) Hapus konstanta top gainer kalau mau, tapi biarkan kalau masih dipakai tempat lain.
#    Tidak perlu diubah supaya patch kecil dan aman.

path.write_text(src, encoding="utf-8")
print("patched bot_dev.py")
