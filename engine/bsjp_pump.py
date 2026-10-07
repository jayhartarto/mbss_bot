"""
BSJP-PUMP -- "Lane 2" of the merged BSJP production design (2026-10-05).
Same brand as BSJP (user decision: don't expose "DNA-match" to the user),
but a structurally different signal -- see memory
project_bsjp_dna_matching_production_design_2026_10_05.md and
project_sameday_momentum_strongclose_archetype_2026_10_05.md for the full
research trail and locked thresholds. Do not re-derive thresholds here.

Core difference from BSJP (engine/bsjp2.py): BSJP trades the OVERNIGHT GAP
(D0 close -> D1 open) confirmed by 2-day cumulative momentum. BSJP-PUMP
needs NO gap at all -- it trades same-day continuation of a stock that
already moved big and closed strong, re-confirmed the next trading day.

Day convention: D0 = the day the big move already happened (EOD gate
evaluated after D0's close). D1 = the next trading day -- session-1
break (~12:00 WIB) is a MANDATORY checkpoint (unlike BSJP, which has none),
session-2 is where the real entry (on a dip vs D1's own session-1 close)
happens. Exit: flat TP+5%/SL-5%, horizon 1-3 trading days.

Pipeline (mirrors engine/bsjp2.py's 3-phase shape, see engine/nightly.py):
  1. Nightly (same call site as BSJP v2): build_bsjp_pump_watchlist computes
     tonight's loose D0 gate (ret_1d>=8% & liquid>=0.15B) using `results`
     already fetched -- stores D1's tracking list, also appends to the
     occurrence signal log.
  2. Intraday D1, 09:00-12:05 WIB (JobQueue, run_bsjp_pump_session1_tick):
     tracks each watchlist ticker's session-1 open/high/low, and ONCE at
     ~12:00 evaluates the mandatory checkpoint (ret_s1>=8% &
     close_pos_s1>=0.8), sending a checkpoint message.
  3. Intraday D1, 13:30-15:50 WIB (JobQueue, run_bsjp_pump_session2_tick):
     tracks checkpoint survivors only, looking for a dip vs D1's own
     session-1 closing price (the locked checkpoint reference) -- this is
     the real entry opportunity, not a risk signal (see memory: dip
     presence doesn't predict worse outcomes, only dip SEVERITY does).
  4. Nightly, same call site as (1): finalize_bsjp_pump_confirmations uses
     TONIGHT's `results` as D1's full-day close, confirms whichever
     tickers passed today's checkpoint, and computes TP/SL off the best
     entry reference seen during session-2 -- this is what `/bsjp tp`
     appends after BSJP's own picks.

NOTE (deliberate v1 simplification, NOT the full "one shared job" ideal
from the production-design memory): session-2 tracking runs as its OWN
JobQueue job/message, reusing engine/bsjp2.py's live-bar fetch helper and
copying its self-correcting tick->re-evaluate->send-new-message pattern,
rather than editing run_bsjp2_intraday_tick itself to splice this lane's
block into the SAME message. bsjp2.py's live tick has a long history of
subtle bugfixes (entry-price locking, no-double-counting ticks, etc.) --
safer to keep it untouched and run Lane-2 as a parallel message at the
same cadence than to risk regressing it. Can be visually merged later.
"""

from __future__ import annotations

import datetime
import json
import os

import pandas as pd

# =====================================================================
# Locked thresholds (memory project_sameday_momentum_strongclose_archetype_
# 2026_10_05.md + project_bsjp_dna_matching_production_design_2026_10_05.md).
# Do not re-tune without new research.
# =====================================================================

D0_RET1D_MIN_PCT = 8.0       # loose nightly watchlist gate (loosened from the original 15% backtest gate -- confirmed cheap, see memory)
LIQ_MIN = 0.15e9             # same flat floor convention as BOW/VCP/BSJP
CHECKPOINT_RET_S1_MIN_PCT = 8.0
CHECKPOINT_CLOSE_POS_S1_MIN = 0.8
OCCURRENCE_WINDOW_CALENDAR_DAYS = 7  # proxy for "trailing 5 TRADING days" (log only has trading-day entries, so a 7-calendar-day window already excludes weekends naturally)
EXIT_TP_PCT = 5.0
EXIT_SL_PCT = 5.0

SESSION1_CHECKPOINT_TIME = datetime.time(12, 0)
SESSION1_WINDOW_START = datetime.time(9, 0)
SESSION1_WINDOW_END = datetime.time(12, 10)   # a little slack past 12:00 so a late tick can still catch the checkpoint
SESSION2_WINDOW_START = datetime.time(13, 30)
SESSION2_WINDOW_END = datetime.time(15, 50)
SESSION2_LATE_WARNING_START = datetime.time(15, 35)  # memory: 5/6 worst retraces clustered 15:20-15:48, no time to recover


def _state_path(filename: str) -> str:
    import engine.legacy_core as core_engine
    return os.path.join(core_engine.PROJECT_ROOT, filename)


def _today_str() -> str:
    import engine.legacy_core as core_engine
    return datetime.datetime.now(core_engine.WIB).strftime("%Y-%m-%d")


def _load_json_state(filename: str) -> dict:
    path = _state_path(filename)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json_state(filename: str, state: dict):
    path = _state_path(filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


# =====================================================================
# Feature computation (called nightly from engine/scoring.py, same
# zero-extra-fetch convention as compute_bsjp2_features).
# =====================================================================

def compute_bsjp_pump_features(hist: pd.DataFrame) -> dict | None:
    """Causal same-day momentum + strong-close feature set."""
    if hist is None or len(hist) < 2 or "Close" not in hist.columns:
        return None
    closes = hist["Close"]
    highs = hist["High"]
    lows = hist["Low"]
    today_close = float(closes.iloc[-1])
    today_high = float(highs.iloc[-1])
    today_low = float(lows.iloc[-1])
    prior_close = float(closes.iloc[-2])
    if prior_close <= 0 or today_close <= 0:
        return None
    ret_1d = (today_close - prior_close) / prior_close * 100.0
    day_range = today_high - today_low
    close_pos = (today_close - today_low) / day_range if day_range > 0 else None
    return {
        "pump_ret_1d": round(ret_1d, 2),
        "pump_close_pos": round(close_pos, 3) if close_pos is not None else None,
        "pump_close": today_close,
        "pump_high": today_high,
        "pump_low": today_low,
    }


# =====================================================================
# Occurrence signal log (append-only, pruned nightly) -- how many times
# the loose D0 gate fired for this ticker within the trailing window.
# =====================================================================

def _load_signal_log() -> list[dict]:
    state = _load_json_state("bsjp_pump_signal_log.json")
    return state.get("entries") or []


def _save_signal_log(entries: list[dict]):
    _save_json_state("bsjp_pump_signal_log.json", {"entries": entries})


def _prune_and_append_signal_log(today_tickers: list[str], today: str) -> dict[str, int]:
    """Appends tonight's loose-gate passers to the log, prunes anything
    older than the occurrence window, and returns {ticker: occurrence}
    for tonight's passers (count includes tonight itself)."""
    entries = _load_signal_log()
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=OCCURRENCE_WINDOW_CALENDAR_DAYS * 3)).isoformat()
    entries = [e for e in entries if e.get("date", "") >= cutoff]  # generous prune window (3x), occurrence count below uses the real window
    entries.extend({"ticker": t, "date": today} for t in today_tickers)
    _save_signal_log(entries)

    occ_cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=OCCURRENCE_WINDOW_CALENDAR_DAYS)).isoformat()
    occurrence = {}
    for t in today_tickers:
        occurrence[t] = sum(1 for e in entries if e["ticker"] == t and e["date"] >= occ_cutoff)
    return occurrence


# =====================================================================
# Phase A -- nightly watchlist build (D0 loose gate)
# =====================================================================

def build_bsjp_pump_watchlist(results: dict) -> list[dict]:
    """Called right after tonight's EOD scoring, same call site/convention
    as build_bsjp2_watchlist. `results` = tonight's per-ticker dict
    (tonight = D0 for a fresh signal)."""
    import engine.broker as broker_engine

    have_features = [r for r in results.values() if r.get("pump_ret_1d") is not None]
    passers = [
        r for r in have_features
        if r["pump_ret_1d"] >= D0_RET1D_MIN_PCT and (r.get("value_traded_20d_avg") or 0.0) >= LIQ_MIN
    ]
    print(
        f"🌙 BSJP-PUMP watchlist funnel: {len(results)} ticker total, "
        f"{len(have_features)} punya pump features, {len(passers)} lolos gate "
        f"(ret_1d>={D0_RET1D_MIN_PCT}% & liquid>={LIQ_MIN:,.0f})."
    )
    today = _today_str()
    occurrence = _prune_and_append_signal_log([r["ticker"] for r in passers], today)

    ff_ratios = {}
    try:
        ff_ratios = broker_engine.load_pushed_foreign_flow_net_ratio() or {}
    except Exception as e:
        print(f"⚠️ BSJP-PUMP: gagal baca foreign flow pushed file: {e}")

    candidates = []
    for r in passers:
        ticker = r["ticker"]
        candidates.append({
            "ticker": ticker,
            "ret_1d_d0": r["pump_ret_1d"],
            "close_pos_d0": r.get("pump_close_pos"),
            "close_d0": r.get("pump_close"),
            "occurrence": occurrence.get(ticker, 1),
            "ff_net_ratio_1d": ff_ratios.get(ticker),
        })

    _save_json_state("bsjp_pump_watchlist_state.json", {
        "trading_day_marker": today,
        "candidates": {c["ticker"]: c for c in candidates},
    })
    # Session-1/session-2 state resets for the new trading day -- stale
    # checkpoint/live state from a previous day must never leak into
    # tonight's fresh watchlist's tracking.
    _save_json_state("bsjp_pump_session1_state.json", {"trading_day_marker": today, "tickers": {}, "checkpoint_done": False})
    _save_json_state("bsjp_pump_checkpoint_state.json", {"trading_day_marker": today, "passed": {}})
    _save_json_state("bsjp_pump_live_state.json", {"trading_day_marker": today, "tickers": {}})
    return candidates


def build_bsjp_pump_watchlist_message(candidates: list[dict]) -> str:
    if not candidates:
        return "📋 BSJP-PUMP watchlist malam ini kosong."
    lines = [f"🌙 BSJP-PUMP WATCHLIST (loose) — {len(candidates)} kandidat (cek checkpoint sesi-1 ~12:00 WIB besok)\n"]
    for c in sorted(candidates, key=lambda x: x["occurrence"], reverse=True):
        occ_tag = f" [{c['occurrence']}x dlm {OCCURRENCE_WINDOW_CALENDAR_DAYS}d]" if c["occurrence"] >= 2 else ""
        lines.append(f"{c['ticker']} — closing {c['close_d0']:,.0f}, ret_1d {c['ret_1d_d0']:+.1f}%{occ_tag}")
    lines.append("\n⚠️ Ini watchlist LONGGAR, belum sinyal final -- checkpoint besok siang yang menentukan.")
    return "\n\n".join(lines)


# =====================================================================
# Phase B1 -- intraday D1, session-1 checkpoint (09:00-12:05 WIB)
# =====================================================================

async def run_bsjp_pump_session1_tick() -> dict:
    """JobQueue callback, same ~300s cadence as BSJP's own intraday tick.
    No-op outside 09:00-12:10 WIB or once today's checkpoint is already
    evaluated."""
    import engine.legacy_core as core_engine
    import engine.scanalert as scanalert_engine
    import engine.bsjp2 as bsjp2_engine

    summary = {"skipped_reason": None, "checkpoint_fired": False}
    now_wib = datetime.datetime.now(core_engine.WIB)
    if now_wib.weekday() >= 5:
        summary["skipped_reason"] = "weekend"
        return summary
    if not (SESSION1_WINDOW_START <= now_wib.time() <= SESSION1_WINDOW_END):
        summary["skipped_reason"] = "outside_window"
        return summary

    watchlist_state = _load_json_state("bsjp_pump_watchlist_state.json")
    candidates = watchlist_state.get("candidates") or {}
    # BUGFIX (2026-10-07, live case: checkpoint silently never fired 2 days
    # running -- see memory project_bsjp_pump_session1_checkpoint_missed_
    # 2026_10_06.md). `trading_day_marker` here is stamped with D0's OWN
    # date (the night build_bsjp_pump_watchlist ran), but this tick runs
    # during D1 -- a DIFFERENT calendar date -- so the old exact-equality
    # check `!= _today_str()` could NEVER pass, causing "no_watchlist" to
    # fire every single day by construction, not just on a stale watchlist.
    # Use a generous staleness window instead (watchlist built in the last
    # 4 calendar days -- covers a weekend) -- genuinely stale (multi-night
    # EOD-scan failure) still gets caught, a normal D0->D1 overnight gap
    # does not.
    watchlist_marker = watchlist_state.get("trading_day_marker")
    if not watchlist_marker or not candidates:
        summary["skipped_reason"] = "no_watchlist"
        return summary
    try:
        if (now_wib.date() - datetime.date.fromisoformat(watchlist_marker)).days > 4:
            summary["skipped_reason"] = "stale_watchlist"
            return summary
    except ValueError:
        summary["skipped_reason"] = "no_watchlist"
        return summary

    s1_state = _load_json_state("bsjp_pump_session1_state.json")
    if s1_state.get("trading_day_marker") != _today_str():
        s1_state = {"trading_day_marker": _today_str(), "tickers": {}, "checkpoint_done": False}
    if s1_state.get("checkpoint_done"):
        summary["skipped_reason"] = "checkpoint_already_done"
        return summary

    tickers = list(candidates.keys())
    # Reuse BSJP's own live-bar fetch helper -- same batching/yfinance
    # pattern, no need to duplicate it for a second lane.
    live_bars = await scanalert_engine._fetch_with_timeout(bsjp2_engine._fetch_bsjp2_live_bar, tickers, default={})

    for ticker, bar in live_bars.items():
        t_state = s1_state["tickers"].setdefault(ticker, {})
        if bar.get("open_so_far") is not None and "open_s1" not in t_state:
            t_state["open_s1"] = bar["open_so_far"]
        if bar.get("high_so_far") is not None:
            t_state["high_s1"] = max(t_state.get("high_s1") or 0.0, bar["high_so_far"])
        if bar.get("low_so_far") is not None:
            prev_low = t_state.get("low_s1")
            t_state["low_s1"] = bar["low_so_far"] if prev_low is None else min(prev_low, bar["low_so_far"])
        t_state["price_now"] = bar["current_price"]

    if now_wib.time() >= SESSION1_CHECKPOINT_TIME:
        passed = {}
        for ticker, t_state in s1_state["tickers"].items():
            open_s1, high_s1, low_s1, price_now = t_state.get("open_s1"), t_state.get("high_s1"), t_state.get("low_s1"), t_state.get("price_now")
            if not open_s1 or high_s1 is None or low_s1 is None or price_now is None:
                continue
            ret_s1 = (price_now - open_s1) / open_s1 * 100.0
            rng = high_s1 - low_s1
            close_pos_s1 = (price_now - low_s1) / rng if rng > 0 else None
            if ret_s1 >= CHECKPOINT_RET_S1_MIN_PCT and close_pos_s1 is not None and close_pos_s1 >= CHECKPOINT_CLOSE_POS_S1_MIN:
                passed[ticker] = {
                    "ret_s1": round(ret_s1, 2), "close_pos_s1": round(close_pos_s1, 3),
                    "close_s1": price_now,  # locked reference for session-2 discount tracking
                }
        s1_state["checkpoint_done"] = True
        _save_json_state("bsjp_pump_checkpoint_state.json", {"trading_day_marker": _today_str(), "passed": passed})
        summary["checkpoint_fired"] = True
        summary["n_passed"] = len(passed)

        bot = scanalert_engine._get_shared_bot()
        if bot is not None:
            text = _render_checkpoint_message(passed, len(candidates))
            try:
                await bot.send_message(chat_id=core_engine.TELEGRAM_CHAT_ID, text=text)
            except Exception as e:
                print(f"⚠️ BSJP-PUMP checkpoint: gagal kirim pesan: {e}")

    _save_json_state("bsjp_pump_session1_state.json", s1_state)
    return summary


def _render_checkpoint_message(passed: dict, n_watchlist: int) -> str:
    if not passed:
        return (
            f"✅ BSJP-PUMP CHECKPOINT — session-1 break\n\n"
            f"0 dari {n_watchlist} kandidat lolos (ret_s1≥{CHECKPOINT_RET_S1_MIN_PCT:.0f}% & "
            f"close_pos_s1≥{CHECKPOINT_CLOSE_POS_S1_MIN:.1f}). Tidak ada yang dipantau sesi-2 hari ini."
        )
    lines = [f"✅ BSJP-PUMP CHECKPOINT — session-1 break\n{len(passed)} dari {n_watchlist} kandidat LOLOS → masuk tracking sesi-2\n"]
    for ticker, p in sorted(passed.items(), key=lambda x: x[1]["ret_s1"], reverse=True):
        lines.append(f"{ticker} — ret_s1 {p['ret_s1']:+.1f}%, close_pos_s1 {p['close_pos_s1']:.2f}")
    return "\n\n".join(lines)


# =====================================================================
# Phase B2 -- intraday D1, session-2 tracking (13:30-15:50 WIB)
# =====================================================================

async def run_bsjp_pump_session2_tick() -> dict:
    """JobQueue callback, same ~300s cadence. Tracks checkpoint survivors
    only, looking for a dip vs D1's own session-1 closing price (NOT
    session-2's own opening reference -- catches the lunch-break-gap fade,
    see memory BTEK case)."""
    import engine.legacy_core as core_engine
    import engine.scanalert as scanalert_engine
    import engine.bsjp2 as bsjp2_engine

    summary = {"skipped_reason": None, "tracked": 0}
    now_wib = datetime.datetime.now(core_engine.WIB)
    if now_wib.weekday() >= 5:
        summary["skipped_reason"] = "weekend"
        return summary
    if not (SESSION2_WINDOW_START <= now_wib.time() <= SESSION2_WINDOW_END):
        summary["skipped_reason"] = "outside_window"
        return summary

    checkpoint_state = _load_json_state("bsjp_pump_checkpoint_state.json")
    passed = checkpoint_state.get("passed") or {}
    if checkpoint_state.get("trading_day_marker") != _today_str() or not passed:
        summary["skipped_reason"] = "no_checkpoint_survivors"
        return summary

    live_state = _load_json_state("bsjp_pump_live_state.json")
    if live_state.get("trading_day_marker") != _today_str():
        live_state = {"trading_day_marker": _today_str(), "tickers": {}}

    tickers = list(passed.keys())
    live_bars = await scanalert_engine._fetch_with_timeout(bsjp2_engine._fetch_bsjp2_live_bar, tickers, default={})

    active = []
    for ticker, p in passed.items():
        bar = live_bars.get(ticker)
        t_state = live_state["tickers"].setdefault(ticker, {"best_discount_price": None})
        if bar is None:
            active.append((ticker, p, t_state))
            continue
        price_now = bar["current_price"]
        close_s1 = p["close_s1"]
        if price_now < close_s1 and (t_state["best_discount_price"] is None or price_now < t_state["best_discount_price"]):
            t_state["best_discount_price"] = price_now
        t_state["price_now"] = price_now
        active.append((ticker, p, t_state))

    live_state["tickers"] = {t: s for t, _, s in active}
    _save_json_state("bsjp_pump_live_state.json", live_state)

    text = _render_session2_message(active, now_wib)
    bot = scanalert_engine._get_shared_bot()
    if bot is not None:
        try:
            await bot.send_message(chat_id=core_engine.TELEGRAM_CHAT_ID, text=text)
        except Exception as e:
            print(f"⚠️ BSJP-PUMP session-2 tick: gagal kirim pesan: {e}")

    summary["tracked"] = len(active)
    return summary


def _render_session2_message(active: list[tuple], now_wib: datetime.datetime) -> str:
    import engine.scanalert as scanalert_engine
    lines = [f"🔥 BSJP-PUMP LIVE — sesi-2 (update {now_wib.strftime('%H:%M:%S')} WIB)\n"]
    for ticker, p, t_state in active:
        price_now = t_state.get("price_now")
        close_s1 = p["close_s1"]
        if price_now is None:
            lines.append(f"{ticker} — belum ada data live siklus ini")
            continue
        diskon_pct = (price_now / close_s1 - 1) * 100.0
        entry_ref = t_state.get("best_discount_price") or close_s1
        tp = scanalert_engine._idx_round_tick_ceil(entry_ref * (1 + EXIT_TP_PCT / 100.0))
        sl = scanalert_engine._idx_round_tick_ceil(entry_ref * (1 - EXIT_SL_PCT / 100.0))
        block = [f"{ticker} Now {price_now:,.0f} (close S1 {close_s1:,.0f}, {diskon_pct:+.1f}%)"]
        if diskon_pct < 0:
            block.append(f"💰 diskon {diskon_pct:+.1f}% dari close S1 — entry layak dipertimbangkan")
        else:
            block.append("belum ada diskon dari close S1 — tunggu dip")
        block.append(f"TP {tp:,.0f} (+{EXIT_TP_PCT:.0f}% dari entry {entry_ref:,.0f}) | SL {sl:,.0f} (-{EXIT_SL_PCT:.0f}%)")
        lines.append("\n".join(block))
    if now_wib.time() >= SESSION2_LATE_WARNING_START:
        lines.append("\n⚠️ 15:35-15:50 WIB: retrace di jam ini jarang pulih sampai close — waspada ekstra.")
    return "\n\n".join(lines)


# =====================================================================
# Phase C -- nightly finalize
# =====================================================================

def finalize_bsjp_pump_confirmations(results: dict) -> list[dict]:
    """Called nightly, same call site as build_bsjp_pump_watchlist, using
    TONIGHT's `results` as D1's full-day close. Confirms whichever
    tickers passed TODAY's session-1 checkpoint, using the best entry
    reference seen during session-2 (a dip if one occurred, else the
    checkpoint's own session-1 closing price) for TP/SL."""
    import engine.scanalert as scanalert_engine

    checkpoint_state = _load_json_state("bsjp_pump_checkpoint_state.json")
    passed = checkpoint_state.get("passed") or {}
    if checkpoint_state.get("trading_day_marker") != _today_str() or not passed:
        _save_json_state("bsjp_pump_confirmed_picks.json", {"trading_day_marker": _today_str(), "picks": []})
        _save_json_state("bsjp_pump_d2_checkpoint_watch.json", {"positions": {}})
        return []

    live_state = _load_json_state("bsjp_pump_live_state.json")
    live_tickers = live_state.get("tickers") or {} if live_state.get("trading_day_marker") == _today_str() else {}

    # occurrence (2026-10-06 re-validation, memory project_spike5_nonneg_
    # continuation_dna_2026_10_06.md follow-up on Lane2's own data,
    # corrected-entry version): confirmed WIN/SL-rate improves monotonically
    # with occurrence (win 70.1%->82.2%->89.5% from occ1->occ>=2->occ4+;
    # SL-hit 29.3%->17.8%->10.5%) -- same direction as Lane1, informational
    # context here (NOT a rung-based exit split like Lane1 -- TP+5/SL-5
    # clearly beats pasrah at EVERY occurrence level on Lane2's own data,
    # unlike Lane1, so the existing exit stays unchanged). Reused from the
    # watchlist state's own D0-loose-gate occurrence (already computed in
    # build_bsjp_pump_watchlist) -- still THIS trading day's D0 candidates
    # at this point in the nightly call order (finalize runs before build
    # overwrites it), no new log needed.
    watchlist_state = _load_json_state("bsjp_pump_watchlist_state.json")
    watchlist_candidates = watchlist_state.get("candidates") or {}

    confirmed = []
    for ticker, p in passed.items():
        r = results.get(ticker)
        close_s1 = p["close_s1"]
        best_discount = (live_tickers.get(ticker) or {}).get("best_discount_price")
        entry_ref = best_discount or close_s1
        tp = scanalert_engine._idx_round_tick_ceil(entry_ref * (1 + EXIT_TP_PCT / 100.0))
        sl = scanalert_engine._idx_round_tick_ceil(entry_ref * (1 - EXIT_SL_PCT / 100.0))
        confirmed.append({
            "ticker": ticker,
            "ret_s1": p["ret_s1"],
            "close_s1": close_s1,
            "entry_ref": entry_ref,
            "had_discount": best_discount is not None,
            "ret_1d_full": r.get("pump_ret_1d") if r else None,
            "close_pos_full": r.get("pump_close_pos") if r else None,
            "occurrence": (watchlist_candidates.get(ticker) or {}).get("occurrence", 1),
            "tp": tp, "sl": sl,
        })

    _save_json_state("bsjp_pump_confirmed_picks.json", {"trading_day_marker": _today_str(), "picks": confirmed})

    # Seed D2 checkpoint (cut-if-red, same research as Lane1's -- on Lane2's
    # OWN corrected-entry data: still-red-at-D2's-close has 91.3% chance of
    # falling further by D3, mean/median ~-10%, only 13.8% ever recover).
    checkpoint_positions = {
        p["ticker"]: {"entry": p["entry_ref"], "occurrence": p["occurrence"]} for p in confirmed
    }
    _save_json_state("bsjp_pump_d2_checkpoint_watch.json", {"positions": checkpoint_positions})

    return confirmed


def build_bsjp_pump_status_message() -> str:
    """/bsjp (no args) -- on-demand status, reads whichever state file is
    current WITHOUT a fresh live fetch (same convention as BSJP's own
    /bsjp: the JobQueue ticks are the only thing that fetches live data,
    this command just renders the last tick's cached state)."""
    import engine.legacy_core as core_engine

    today = _today_str()
    checkpoint_state = _load_json_state("bsjp_pump_checkpoint_state.json")
    if checkpoint_state.get("trading_day_marker") == today and checkpoint_state.get("passed"):
        passed = checkpoint_state["passed"]
        live_state = _load_json_state("bsjp_pump_live_state.json")
        live_tickers = live_state.get("tickers") or {} if live_state.get("trading_day_marker") == today else {}
        active = [(ticker, p, live_tickers.get(ticker, {})) for ticker, p in passed.items()]
        now_wib = datetime.datetime.now(core_engine.WIB)
        return _render_session2_message(active, now_wib)

    s1_state = _load_json_state("bsjp_pump_session1_state.json")
    if s1_state.get("trading_day_marker") == today and s1_state.get("tickers"):
        return (
            f"⏳ BSJP-PUMP — checkpoint sesi-1 belum dievaluasi (nunggu jam "
            f"{SESSION1_CHECKPOINT_TIME.strftime('%H:%M')} WIB), {len(s1_state['tickers'])} "
            f"kandidat sedang dipantau."
        )

    watchlist_state = _load_json_state("bsjp_pump_watchlist_state.json")
    candidates = list((watchlist_state.get("candidates") or {}).values())
    return build_bsjp_pump_watchlist_message(candidates)


def build_bsjp_pump_tp_message() -> str:
    """/bsjp tp -- appended after BSJP's own picks (see commands/scan.py).

    occurrence shown as context only (2026-10-06 re-validation confirms it's
    a real, monotonic signal here too -- win 70.1%->89.5% from occ1->occ4+ --
    but UNLIKE Lane1, TP+5/SL-5 beats pasrah at every occurrence level on
    Lane2's own data, so there's no rung-based exit split to apply here)."""
    state = _load_json_state("bsjp_pump_confirmed_picks.json")
    picks = state.get("picks") or []
    if not picks:
        return "📋 Tidak ada BSJP-PUMP pick yang lolos checkpoint hari ini."
    lines = [f"🎯 BSJP-PUMP TP/SL REKOMENDASI — {len(picks)} pick\n"]
    for p in sorted(picks, key=lambda x: x["ret_s1"], reverse=True):
        discount_tag = " [entry diskon]" if p["had_discount"] else " [entry = close S1, tanpa diskon]"
        occ = p.get("occurrence", 1)
        occ_tag = f" [occurrence {occ}x/5d]" if occ >= 2 else ""
        lines.append(
            f"{p['ticker']}{discount_tag}{occ_tag} — ret_s1 {p['ret_s1']:+.1f}%, entry {p['entry_ref']:,.0f}\n"
            f"  TP {p['tp']:,.0f} | SL {p['sl']:,.0f}"
        )
    lines.append(
        f"\nHorizon 1-3 hari, exit flat di TP/SL -- tidak ada tier/trailing seperti BSJP.\n"
        "⚠️ Cek /bsjp checkpoint besok malam: masih merah di closing besok (belum TP) "
        "punya ~91% chance makin dalam di hari berikutnya -- CUT, jangan tahan."
    )
    return "\n\n".join(lines)


# =====================================================================
# Phase D -- D2 checkpoint (cut-if-red, 2026-10-06 re-validation on Lane2's
# own corrected-entry data: memory project_spike5_nonneg_continuation_dna_
# 2026_10_06.md). Called nightly, SAME call site/results as
# finalize_bsjp_pump_confirmations, one cycle later -- tonight's `results`
# is D2's close for whatever finalize seeded into bsjp_pump_d2_checkpoint_
# watch.json last night. Still red at D2's close -> cut (~91.3% chance of
# falling further by D3, mean/median close ~-10%, only 13.8% ever recover).
# =====================================================================

def run_bsjp_pump_d2_checkpoint(results: dict) -> list[dict]:
    """Call BEFORE finalize_bsjp_pump_confirmations, which overwrites
    bsjp_pump_d2_checkpoint_watch.json with tonight's own fresh seed."""
    watch = _load_json_state("bsjp_pump_d2_checkpoint_watch.json")
    positions = watch.get("positions") or {}
    checked = []
    if positions:
        for ticker, pos in positions.items():
            r = results.get(ticker)
            if r is None or r.get("pump_close") is None:
                continue
            entry = pos.get("entry")
            close_d2 = r["pump_close"]
            if not entry:
                continue
            ret_pct = (close_d2 - entry) / entry * 100.0
            checked.append({
                "ticker": ticker, "entry": entry, "close_d2": close_d2,
                "ret_pct": round(ret_pct, 2), "occurrence": pos.get("occurrence", 1),
                "status": "CUT" if ret_pct < 0 else "HOLD",
            })
    _save_json_state("bsjp_pump_d2_checkpoint_last_result.json", {"trading_day_marker": _today_str(), "checked": checked})
    return checked


def read_last_bsjp_pump_d2_checkpoint() -> list[dict]:
    state = _load_json_state("bsjp_pump_d2_checkpoint_last_result.json")
    return state.get("checked") or []


def build_bsjp_pump_d2_checkpoint_message(checked: list[dict]) -> str:
    """/bsjp checkpoint -- appended after BSJP's own checkpoint message."""
    if not checked:
        return "📋 Tidak ada posisi BSJP-PUMP yang perlu di-checkpoint malam ini."
    cuts = sorted((c for c in checked if c["status"] == "CUT"), key=lambda x: x["ret_pct"])
    holds = sorted((c for c in checked if c["status"] == "HOLD"), key=lambda x: x["ret_pct"], reverse=True)
    lines = [f"🔎 BSJP-PUMP D2 CHECKPOINT — {len(checked)} posisi (entry vs closing hari ini)\n"]
    if cuts:
        block = ["🔴 CUT disarankan (closing masih di bawah entry):"]
        for c in cuts:
            block.append(f"  {c['ticker']}: entry {c['entry']:,.0f} → now {c['close_d2']:,.0f} ({c['ret_pct']:+.1f}%)")
        lines.append("\n".join(block))
    if holds:
        block = ["🟢 HOLD (masih di atas entry, aman ditahan 1 hari lagi):"]
        for c in holds:
            block.append(f"  {c['ticker']}: entry {c['entry']:,.0f} → now {c['close_d2']:,.0f} ({c['ret_pct']:+.1f}%)")
        lines.append("\n".join(block))
    lines.append(
        "\n⚠️ Riset (n=2,487, 2024-2026): closing MERAH di titik ini punya ~91.3% chance "
        "makin dalam ke hari berikutnya (mean/median jatuh ke ~-10%, cuma 13.8% akhirnya "
        "recover) -- CUT di sini, jangan tahan berharap recovery."
    )
    return "\n\n".join(lines)
