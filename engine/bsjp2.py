"""
BSJP v2 -- "Beli Sore Jual Pagi" Stage-1/Stage-2/rocket system.

Replaces the old Fase1/Fase2/pyramid mechanism (retired 2026-09-20, see
archive/bsjp_legacy_fase1_fase2_pyramid_2026_09_20.py). Full research trail,
locked formulas, and design rationale are in memory
`project_bsjp_derisk_revisit_2026_09_17.md` (sections "STAGE-1 REVISED",
"STAGE-2 REBUILT on STAGE-1 REVISED", "ARA ROCKET TAG REBUILT + FIXED",
"Day-by-day TP1/TP2/TP3 timing ... hold/cut discipline", "Watchlist message
design", "CRITICAL BUG FOUND + FIXED: TP3 tier-labeling", and "TP
calibration"). This module is pure implementation of formulas already
validated there -- do not re-derive thresholds here.

Day convention (same throughout the research): D0 = signal day (EOD gate
evaluated after D0's close), D1 = entry day (position notionally opened at
D1's close; rocket-tagged entries are opened near D1's OPEN instead), D2-D4
= outcome/hold window.

Pipeline (see engine/nightly.py run_nightly_full_scan for call sites):
  1. Nightly, right after tonight's EOD scoring (`results` = tonight's
     per-ticker dict, tonight = D0 for a fresh signal): build_bsjp2_watchlist
     computes the D0-static Stage-1 union and stores it -- this is
     tomorrow's (D1's) tracking list.
  2. Intraday D1 (JobQueue, run_bsjp2_intraday_tick): tracks trigger/fading
     price live, tags 🚀/🚀🚀 right after D1's open, tracks rsi14_d1 live for
     the 3-api upgrade, and live-recomputes TP1/TP2/TP3(if valid)/SL.
  3. Nightly, same call site as (1) but using TONIGHT's `results` as D1's
     data for whatever was on YESTERDAY's watchlist:
     finalize_bsjp2_confirmations confirms Stage-1 (ret_2d_cum>=2%), assigns
     the Stage-2 tier/rocket tag, and computes the dynamic TP1/TP2/TP3+SL
     recommendation -- this is what `/bsjp tp` reads.
"""

from __future__ import annotations

import datetime
import json
import os

import pandas as pd

STATE_FILE_BSJP2_WATCHLIST = None  # set lazily, see _state_path()
STATE_FILE_BSJP2_CONFIRMED = None
STATE_FILE_BSJP2_LIVE = None


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
# Locked thresholds (memory `project_bsjp_derisk_revisit_2026_09_17.md`,
# sections "STAGE-1 REVISED", "STAGE-2 REBUILT", "ARA ROCKET TAG REBUILT +
# FIXED", "TP calibration"). Do not re-tune without new research.
# =====================================================================

# Stage-1 REVISED gate
STAGE1_GAP_MIN_PCT = 1.0
STAGE1_GAP_MAX_PCT = 12.0
STAGE1_LIQUIDITY_PERCENTILE = 0.25  # q25 of avg_trade_value_20d across tonight's universe
STAGE1_RET2D_MIN_PCT = 2.0

# Stage-2 tiers (all stacked on Stage-1 confirmation)
TIER1_RET2D_MIN_PCT = 4.0
TIER1_PCTB_MIN = 0.7
TIER1_GAP_MIN_PCT = 0.0
TIER2_ATR_MIN_PCT = 2.0
TIER3_ATR_MIN_PCT = 3.0  # ATR-ladder ceiling, don't extend further (memory: tail risk crosses 25% past this)
TIER_EXTREME_ATR_MAX_PCT = 6.0
TIER_EXTREME_RSI_MIN = 60.0

# Rocket tag (resolves at D1's OPEN, not at D0's close)
ROCKET_GAP_OPENING_MIN_PCT = 13.0
ROCKET_GAP_OPENING_MAX_PCT = 21.0
ROCKET_ATR_MAX_PCT = 4.7
ROCKET_PLUS_RSI_D0_MIN = 85.0

# Dynamic TP1/TP2 table (MFE-percentile based, replaces fixed 3%/5% -- see
# "TP calibration" section). TIER1 and TIER-EXTREME deliberately use the SAME
# numbers (memory's own conclusion: their MFE upside is ~equivalent --
# TIER-EXTREME's real edge is retaining more of the gain by D4, not a bigger
# TP target) -- using TIER1's own published figures (2.16/5.45), not an
# average, to stay closest to the source numbers. STAGE1_BASE (Stage-1
# confirmed but didn't clear TIER1's own conviction filter) has no dedicated
# calibration in the research -- TIER1's numbers are reused here as a
# deliberately conservative default, not a validated STAGE1_BASE-specific
# figure.
DYNAMIC_TP_TABLE = {
    "STAGE1_BASE": (2.16, 5.45),
    "TIER1": (2.16, 5.45),
    "TIER2": (2.16, 5.45),
    "TIER3": (2.16, 5.45),
    "TIER_EXTREME": (2.16, 5.45),
    "ROCKET": (2.13, 9.40),
    "ROCKET_PLUS": (3.68, 9.91),
}

# Spike-then-faded D1 candle (backtest-validated 2026-09-25, see memory
# project_bsjp_d1_spike_fade_quality_downgrade_2026_09_25.md and
# research/bsjp_d1_wick_quality_2026_09_25.py +
# research/bsjp_d1_wick_by_tier_2026_09_25.py, n=4013 Stage-1 REVISED
# population, tier classification via _assign_conviction_tier below):
# a D1 candle whose own high ran well above its own close, AND closed in the
# bottom half of its own day range, degrades forward D2-D4 quality EVEN
# WITHIN the same conviction tier -- worst inside TIER_EXTREME (win
# 53.6%->39.5%, median +0.94%->-1.92%, tail MAE<-8% 17.8%->29.4%), same
# direction in STAGE1_BASE (win 46.0%->36.4%, median 0.00%->-1.47%). Treated
# the same as an ordinary FADING pick: tagged fading, dropped/hidden.
SPIKE_FADE_UPPER_WICK_MIN_PCT = 5.0   # D1 high at least this % above D1 close
SPIKE_FADE_CLOSE_POSITION_MAX = 0.5   # D1 close in the bottom half of its own day range


def _is_spike_fade_candle(day_high, day_low, day_close) -> bool:
    if day_high is None or day_low is None or day_close is None or day_close <= 0:
        return False
    upper_wick_pct = (day_high - day_close) / day_close * 100.0
    day_range = day_high - day_low
    close_position = (day_close - day_low) / day_range if day_range > 0 else 0.5
    return upper_wick_pct >= SPIKE_FADE_UPPER_WICK_MIN_PCT and close_position < SPIKE_FADE_CLOSE_POSITION_MAX


# Fire-icon display (memory "Tracking-message design decision": user
# collapsed the ladder to 3 states -- TIER2/TIER3 (the ATR-floor ladder)
# both display as "2 api", TIER-EXTREME alone is "3 api". STAGE1_BASE (Stage-1
# confirmed, didn't clear TIER1's own conviction filter) gets no fire icon --
# it's a real pick but has no Stage-2 conviction backtest behind it.
FIRE_ICON_BY_TIER = {
    "STAGE1_BASE": "", "TIER1": "🔥", "TIER2": "🔥🔥", "TIER3": "🔥🔥",
    "TIER_EXTREME": "🔥🔥🔥",
}


def _assign_conviction_tier(ret_2d_cum, pct_b_prior, gap_pct, atr_pct_prior, rsi_extreme=None) -> str:
    """Shared Stage-2 tier classifier, used identically by the intraday live
    tracker and the nightly finalize step so the two paths can never diverge.

    IMPORTANT (memory "STAGE-2 REBUILT on STAGE-1 REVISED" + the TIER-EXTREME
    finer-grid section): TIER2/TIER3/TIER_EXTREME were ALL backtested STACKED
    ON TOP of TIER1's own conviction filter (`.tmp_bsjp_stage2_v2_tier1base_
    with_rsi.parquet`, n=1,471) -- NOT on the raw Stage-1 population (n=4,013).
    A candidate that fails TIER1's own ret_2d_cum/pct_b_prior/gap_pct filter
    must never be promoted to TIER2/3/EXTREME just because its ATR or RSI
    alone would qualify -- that combination was never actually backtested.
    """
    is_tier1 = (
        (ret_2d_cum or 0) >= TIER1_RET2D_MIN_PCT
        and (pct_b_prior or 0) >= TIER1_PCTB_MIN
        and (gap_pct if gap_pct is not None else -999) >= TIER1_GAP_MIN_PCT
    )
    if not is_tier1:
        return "STAGE1_BASE"
    if atr_pct_prior is not None and atr_pct_prior <= TIER_EXTREME_ATR_MAX_PCT and rsi_extreme is not None and rsi_extreme >= TIER_EXTREME_RSI_MIN:
        return "TIER_EXTREME"
    if atr_pct_prior is not None and atr_pct_prior >= TIER3_ATR_MIN_PCT:
        return "TIER3"
    if atr_pct_prior is not None and atr_pct_prior >= TIER2_ATR_MIN_PCT:
        return "TIER2"
    return "TIER1"

HOLD_CUT_GUIDANCE = (
    "Disiplin hold/cut (dari riset day-by-day D2-D4): kalau TP1 SUDAH tersentuh "
    "di D2, wajar tahan sedikit lebih lama (trailing sisanya). Kalau MASIH "
    "floating (belum TP1) di akhir D2 dan sudah rugi ~-5% s.d -6%, disarankan "
    "CUT -- cuma ~36% yang akhirnya recover. Kalau floating tapi masih tipis, "
    "beri toleransi 1 hari lagi (s.d D3), tapi JANGAN tahan posisi belum jelas "
    "sampai D4 -- itu jendela tail-risk tertinggi tanpa upside yang sepadan."
)


# =====================================================================
# Feature computation (called nightly from engine/scoring.py, zero extra
# fetch -- reuses the same `hist` DataFrame compute_factor_scoring already
# has in memory). "Today" here = whatever day the nightly run treats as
# current (D0 for a fresh signal, D1 the following night when finalizing).
# =====================================================================

def compute_bsjp2_features(hist: pd.DataFrame) -> dict | None:
    """Causal Stage-1/Stage-2/rocket feature set. Returns None only when
    there's not even enough history for the CHEAPEST field (gap_pct/
    pct_b_prior, ~20 rows).

    BUGFIX (2026-09-20, live case: first deploy night's watchlist funnel
    showed only 1/508 tickers with ANY bsjp2 feature): this used to gate the
    WHOLE dict on >=260 rows (mirroring the old system's BSJP_MIN_HISTORY_
    DAYS, needed for is_first_high252's 252d lookback) -- but that throws
    away gap_pct/pct_b_prior/atr_pct_prior (need ~20 rows) and
    macd_hist_prior (~35 rows) for every ticker whose local OHLCV history
    doesn't happen to reach 260 rows yet, even though those fields are
    individually computable with far less data. Each field below already
    has its own length check -- only `is_first_high252` needs 254+ rows and
    silently stays False when there isn't enough, same as intended."""
    if hist is None or len(hist) < 20 or "Close" not in hist.columns:
        return None

    closes = hist["Close"]
    highs = hist["High"]
    lows = hist["Low"]
    opens = hist["Open"] if "Open" in hist.columns else None

    today_close = float(closes.iloc[-1])
    today_high = float(highs.iloc[-1])
    today_low = float(lows.iloc[-1])
    prior_close = float(closes.iloc[-2])  # close_(D0-1) -- the trigger/fading anchor
    if prior_close <= 0 or today_close <= 0:
        return None
    today_open = float(opens.iloc[-1]) if opens is not None and pd.notna(opens.iloc[-1]) else None

    gap_pct = ((today_open - prior_close) / prior_close * 100.0) if today_open else None

    # is_first_high252: new 252d high TODAY that was NOT already true yesterday
    is_first_high252 = False
    if len(highs) >= 254:
        high252_asof_d1 = float(highs.iloc[:-1].tail(252).max())  # excludes today, "as of D-1"
        high252_asof_d2 = float(highs.iloc[:-2].tail(252).max())  # excludes today & D-1, "as of D-2"
        is_new_today = today_close >= high252_asof_d1
        was_new_yesterday = prior_close >= high252_asof_d2
        is_first_high252 = bool(is_new_today and not was_new_yesterday)

    # macd_hist_prior: MACD histogram computed WITHOUT today (as of D-1)
    macd_hist_prior = None
    prior_closes = closes.iloc[:-1]
    if len(prior_closes) >= 35:
        import engine.legacy_core as core_engine
        _, _, macd_hist_prior_series = core_engine.calculate_macd(prior_closes)
        macd_hist_prior = float(macd_hist_prior_series.iloc[-1])

    # atr_pct_prior: causal ATR%, excludes today (ported from the old BSJP
    # historical-base fetch, archive/bsjp_legacy_fase1_fase2_pyramid_2026_09_20.py)
    atr_pct_prior = None
    if len(prior_closes) >= 15:
        prior_highs = highs.iloc[:-1]
        prior_lows = lows.iloc[:-1]
        tr = pd.concat([
            prior_highs - prior_lows,
            (prior_highs - prior_closes.shift(1)).abs(),
            (prior_lows - prior_closes.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
        if pd.notna(atr):
            atr_pct_prior = float(atr / prior_close * 100.0)

    # pct_b_prior: Bollinger %B computed WITHOUT today (as of D-1)
    pct_b_prior = None
    if len(prior_closes) >= 20:
        sma20_prior = float(prior_closes.tail(20).mean())
        std20_prior = float(prior_closes.tail(20).std())
        bb_upper_prior = sma20_prior + 2 * std20_prior
        bb_lower_prior = sma20_prior - 2 * std20_prior
        bb_width_prior = bb_upper_prior - bb_lower_prior
        if bb_width_prior > 0:
            pct_b_prior = float((prior_close - bb_lower_prior) / bb_width_prior)

    # rsi14, AS OF TODAY's own close (today=D0 tonight, today=D1 the night
    # finalize_bsjp2_confirmations reads it back)
    rsi14_today = None
    if len(closes) >= 15:
        import engine.legacy_core as core_engine
        rsi_series = core_engine.calculate_rsi(closes)
        rsi14_today = float(rsi_series.iloc[-1])

    # BB upper INCLUDING today -- reused as "TP3 candidate #1" once this
    # same field is read back the night AFTER a watchlist ticker's D1 closes
    bb_upper_today = None
    if len(closes) >= 20:
        sma20 = float(closes.tail(20).mean())
        std20 = float(closes.tail(20).std())
        bb_upper_today = sma20 + 2 * std20

    # 60d swing high EXCLUDING today (prior to D0) -- "TP3 candidate #2"
    swing_high_60d_prior = float(highs.iloc[:-1].tail(60).max()) if len(highs) >= 61 else None

    # Trailing closes (INCLUDING today), just enough for a live RSI(14) calc
    # once intraday tracking appends today's live/partial price on top of
    # this. Stored small deliberately (state file stays JSON, not a pickle).
    close_history_20 = [round(float(x), 4) for x in closes.tail(20)]

    return {
        "bsjp2_gap_pct": round(gap_pct, 2) if gap_pct is not None else None,
        "bsjp2_is_first_high252": is_first_high252,
        "bsjp2_macd_hist_prior": round(macd_hist_prior, 4) if macd_hist_prior is not None else None,
        "bsjp2_atr_pct_prior": round(atr_pct_prior, 2) if atr_pct_prior is not None else None,
        "bsjp2_pct_b_prior": round(pct_b_prior, 4) if pct_b_prior is not None else None,
        "bsjp2_rsi14": round(rsi14_today, 1) if rsi14_today is not None else None,
        "bsjp2_close": today_close,
        "bsjp2_high": today_high,
        "bsjp2_low": today_low,
        "bsjp2_prev_close": prior_close,
        "bsjp2_bb_upper": round(bb_upper_today, 2) if bb_upper_today is not None else None,
        "bsjp2_swing_high_60d_prior": round(swing_high_60d_prior, 2) if swing_high_60d_prior is not None else None,
        "bsjp2_close_history_20": close_history_20,
    }


# =====================================================================
# Phase A -- nightly watchlist build (D0-static Stage-1 union)
# =====================================================================

def build_bsjp2_watchlist(results: dict) -> list[dict]:
    """Called right after tonight's EOD scoring. `results` = tonight's
    per-ticker dict from compute_factor_scoring (tonight = D0). Computes the
    D0-static half of Stage-1 REVISED and stores the passing tickers as
    tomorrow's (D1's) tracking list."""
    have_features = [r for r in results.values() if r.get("bsjp2_gap_pct") is not None]
    liquidity_values = sorted(
        float(r["value_traded_20d_avg"]) for r in have_features
        if r.get("value_traded_20d_avg") is not None
    )
    # Diagnostic (2026-09-20, live case: first deploy night's watchlist came
    # back empty with no way to tell "genuinely 0 candidates" from "the
    # feature computation broke for everyone" -- see the matching bugfix note
    # in engine/scoring.py compute_factor_scoring) -- always print the funnel
    # counts, this is one line/night, not per-ticker spam.
    print(
        f"🌙 BSJP v2 watchlist funnel: {len(results)} ticker total, "
        f"{len(have_features)} punya bsjp2 features (0 di sini = compute_bsjp2_features gagal sistemik, cek log di atas), "
        f"{len(liquidity_values)} punya liquidity juga."
    )
    if not liquidity_values:
        return []
    q25_idx = max(0, int(len(liquidity_values) * STAGE1_LIQUIDITY_PERCENTILE) - 1)
    liquidity_q25 = liquidity_values[q25_idx]

    union_pass, macd_reject = 0, 0
    candidates = []
    for ticker, r in results.items():
        gap_pct = r.get("bsjp2_gap_pct")
        is_first_high252 = r.get("bsjp2_is_first_high252")
        macd_hist_prior = r.get("bsjp2_macd_hist_prior")
        if gap_pct is None or macd_hist_prior is None:
            continue
        liquidity = r.get("value_traded_20d_avg") or 0.0
        gap_leg = (STAGE1_GAP_MIN_PCT <= gap_pct < STAGE1_GAP_MAX_PCT) and liquidity >= liquidity_q25
        union = gap_leg or bool(is_first_high252)
        if not union:
            continue
        union_pass += 1
        if not macd_hist_prior >= 0:
            macd_reject += 1
            continue
        candidates.append({
            "ticker": ticker,
            "gap_pct": gap_pct,
            "is_first_high252": bool(is_first_high252),
            "macd_hist_prior": macd_hist_prior,
            "atr_pct_prior": r.get("bsjp2_atr_pct_prior"),
            "pct_b_prior": r.get("bsjp2_pct_b_prior"),
            "rsi14_d0": r.get("bsjp2_rsi14"),
            "close_d0": r.get("bsjp2_close"),
            "prev_close": r.get("bsjp2_prev_close"),  # close_(D0-1), trigger/fading anchor
            "swing_high_60d_prior": r.get("bsjp2_swing_high_60d_prior"),
            "close_history_20": r.get("bsjp2_close_history_20") or [],
        })

    print(
        f"🌙 BSJP v2 watchlist funnel: liquidity_q25={liquidity_q25:,.0f}, "
        f"{union_pass} lolos union (gap+liq OR is_first_high252), {macd_reject} kena reject macd_hist_prior<0, "
        f"{len(candidates)} final candidate."
    )
    today = _today_str()
    _save_json_state("bsjp2_watchlist_state.json", {
        "trading_day_marker": today,
        "candidates": {c["ticker"]: c for c in candidates},
        # live tracking (rocket tag, fire-icon upgrades) writes into this
        # same ticker dict during D1's session -- see run_bsjp2_intraday_tick
    })
    return candidates


def build_bsjp2_watchlist_message(candidates: list[dict]) -> str:
    import engine.scanalert as scanalert_engine
    if not candidates:
        return (
            "📋 BSJP watchlist malam ini kosong (formula Stage-1 ketat -- wajar "
            "kalau kosong beberapa hari, itu justru tujuan derisk-nya)."
        )
    lines = [f"🌙 BSJP WATCHLIST — {len(candidates)} kandidat (akan dipantau live begitu D+1 buka)\n"]
    for c in sorted(candidates, key=lambda x: x["gap_pct"], reverse=True):
        trigger = scanalert_engine._idx_round_tick_ceil(c["prev_close"] * (1 + STAGE1_RET2D_MIN_PCT / 100.0))
        vs_close = "di bawah closing" if trigger <= c["close_d0"] else "di atas closing"
        tag = " (new 52w high)" if c["is_first_high252"] else ""
        lines.append(
            f"{c['ticker']} — closing {c['close_d0']:,.0f}, gap {c['gap_pct']:+.1f}%{tag}\n"
            f"   Trigger/fading: {trigger:,.0f} ({vs_close})"
        )
    lines.append(
        "\n⚠️ Ini WATCHLIST, bukan alert entry. Trigger = harga minimal yang harus "
        "DIPERTAHANKAN besok (di atas/sama = tetap valid, di bawah = FADING, "
        "tinggalkan). Rocket tag (🚀/🚀🚀) & status api baru muncul setelah D+1 buka."
    )
    return "\n\n".join(lines)


# =====================================================================
# Phase B -- intraday D1 live tracking
# =====================================================================

_BSJP2_FETCH_BATCH = 100


def _fetch_bsjp2_live_bar(tickers: list[str]) -> dict:
    """Live partial-day bar for D1, same batching pattern as the old
    _fetch_bsjp_live_bar (archive/bsjp_legacy_fase1_fase2_pyramid_2026_09_20.py)."""
    import time
    import yfinance as yf
    import engine.legacy_core as core_engine
    today_date = datetime.datetime.now(core_engine.WIB).date()
    live = {}
    for i in range(0, len(tickers), _BSJP2_FETCH_BATCH):
        batch = tickers[i:i + _BSJP2_FETCH_BATCH]
        symbols = [t + ".JK" for t in batch]
        data = yf.download(symbols, period="5d", interval="1d", group_by="ticker", threads=True, progress=False)
        for t in batch:
            sym = t + ".JK"
            try:
                d = data[sym].dropna(how="all")
            except Exception:
                continue
            today_rows = d[d.index.date == today_date]
            if today_rows.empty:
                continue
            row = today_rows.iloc[-1]
            live[t] = {
                "current_price": float(row["Close"]),
                "open_so_far": float(row["Open"]) if pd.notna(row["Open"]) else None,
                "high_so_far": float(row["High"]) if pd.notna(row["High"]) else None,
                "low_so_far": float(row["Low"]) if pd.notna(row["Low"]) else None,
            }
        if i + _BSJP2_FETCH_BATCH < len(tickers):
            time.sleep(0.5)
    return live


def _rocket_tag(gap_opening: float | None, atr_pct_prior: float | None, rsi14_d0: float | None) -> str | None:
    if gap_opening is None or atr_pct_prior is None:
        return None
    if not (ROCKET_GAP_OPENING_MIN_PCT <= gap_opening < ROCKET_GAP_OPENING_MAX_PCT and atr_pct_prior <= ROCKET_ATR_MAX_PCT):
        return None
    if rsi14_d0 is not None and rsi14_d0 >= ROCKET_PLUS_RSI_D0_MIN:
        return "🚀🚀"
    return "🚀"


INTRADAY_WINDOW_START = datetime.time(9, 0)
INTRADAY_WINDOW_END = datetime.time(15, 50)


async def run_bsjp2_intraday_tick() -> dict:
    """JobQueue callback (~300s cadence, mirrors the old Fase2 recheck
    window). Tracks each watchlist ticker's live trigger/fading status,
    tags 🚀/🚀🚀 right after D1 open, and (re)sends the tracking message."""
    import engine.legacy_core as core_engine
    import engine.scanalert as scanalert_engine

    summary = {"skipped_reason": None, "tracked": 0}
    now_wib = datetime.datetime.now(core_engine.WIB)
    if now_wib.weekday() >= 5:
        summary["skipped_reason"] = "weekend"
        return summary
    if not (INTRADAY_WINDOW_START <= now_wib.time() <= INTRADAY_WINDOW_END):
        summary["skipped_reason"] = "outside_window"
        return summary

    watchlist_state = _load_json_state("bsjp2_watchlist_state.json")
    candidates = watchlist_state.get("candidates") or {}
    if not candidates:
        summary["skipped_reason"] = "no_watchlist"
        return summary

    live_state = _load_json_state("bsjp2_live_state.json")
    if live_state.get("trading_day_marker") != _today_str():
        live_state = {"trading_day_marker": _today_str(), "message_id": None, "tickers": {}}

    tickers = list(candidates.keys())
    live_bars = await scanalert_engine._fetch_with_timeout(_fetch_bsjp2_live_bar, tickers, default={})

    active = []
    for ticker, c in candidates.items():
        bar = live_bars.get(ticker)
        t_state = live_state["tickers"].setdefault(ticker, {"tier": None, "fading": False, "rocket": None})
        if bar is None:
            active.append((ticker, c, t_state))
            continue
        price_now = bar["current_price"]
        prev_close = c["prev_close"]
        ret_2d_cum_live = (price_now - prev_close) / prev_close * 100.0 if prev_close else None

        if t_state["rocket"] is None and bar.get("open_so_far") and c.get("close_d0"):
            gap_opening = (bar["open_so_far"] - c["close_d0"]) / c["close_d0"] * 100.0
            t_state["rocket"] = _rocket_tag(gap_opening, c.get("atr_pct_prior"), c.get("rsi14_d0")) or "none"

        # high_so_far/low_so_far from yfinance's partial-day bar are already
        # the session's cumulative extremes as of this tick (not a per-tick
        # delta) -- track the running max/min across ticks.
        if bar.get("high_so_far") is not None:
            t_state["high_so_far"] = max(t_state.get("high_so_far") or 0.0, bar["high_so_far"])
        if bar.get("low_so_far") is not None:
            prev_low = t_state.get("low_so_far")
            t_state["low_so_far"] = bar["low_so_far"] if prev_low is None else min(prev_low, bar["low_so_far"])

        # Spike-then-faded guard (backtest-validated, see SPIKE_FADE_* above):
        # treated the same as ordinary Stage-1 FADING -- tagged fading and
        # hidden by _render_bsjp2_intraday_message. Uses price_now as the
        # best available live proxy for D1's eventual close (same convention
        # already used for entry_price_locked below) -- re-evaluated fresh
        # every tick, so it un-flags automatically if price recovers later
        # in the session, same self-correcting behavior as the ret_2d_cum
        # fading check.
        spike_fade_live = _is_spike_fade_candle(t_state.get("high_so_far"), t_state.get("low_so_far"), price_now)

        t_state["fading"] = bool(
            (ret_2d_cum_live is not None and ret_2d_cum_live < STAGE1_RET2D_MIN_PCT) or spike_fade_live
        )
        t_state["price_now"] = price_now
        t_state["ret_2d_cum_live"] = ret_2d_cum_live

        # BUGFIX (user report 2026-09-21): TP1/TP2/TP3 used to be recomputed
        # from the live, constantly-moving `price_now` on EVERY tick -- this
        # made displayed targets drift tick-to-tick (TP3 could silently
        # appear/disappear as TP1/TP2 floated past it) and meant the "target"
        # shown had no fixed relationship to the price the position was
        # actually opened at. Lock the entry-price reference ONCE, the first
        # tick this ticker is confirmed (not fading) -- rocket entries use
        # D1's own open (known as soon as the rocket tag fires), non-rocket
        # entries use price_now AT THAT FIRST CONFIRMED TICK as the working
        # entry estimate (true entry is D1's close per this module's own
        # convention, not known yet intraday -- this is the best available
        # proxy until then). All subsequent ticks reuse this locked price,
        # never recompute it.
        if not t_state["fading"] and t_state.get("entry_price_locked") is None:
            if t_state.get("rocket") and t_state["rocket"] != "none" and bar.get("open_so_far"):
                t_state["entry_price_locked"] = bar["open_so_far"]
            else:
                t_state["entry_price_locked"] = price_now

        if not t_state["fading"]:
            # Live RSI(14) approximation for the 3-api (TIER-EXTREME) upgrade
            # -- appends today's live price onto the trailing closes captured
            # at watchlist-build time, same causal-window convention as
            # everywhere else in this module.
            rsi14_live = None
            close_hist = c.get("close_history_20") or []
            if len(close_hist) >= 14:
                import engine.legacy_core as core_engine
                rsi_series_live = core_engine.calculate_rsi(pd.Series(close_hist + [price_now]))
                rsi14_live = float(rsi_series_live.iloc[-1])
            t_state["tier"] = _assign_conviction_tier(
                ret_2d_cum_live, c.get("pct_b_prior"), c.get("gap_pct"), c.get("atr_pct_prior"), rsi14_live,
            )
        active.append((ticker, c, t_state))

    live_state["tickers"] = {t: s for t, _, s in active}
    text = _render_bsjp2_intraday_message(active)
    bot = scanalert_engine._get_shared_bot()
    if bot is not None:
        try:
            # BUGFIX (user report 2026-09-21): this used to edit_message_text
            # the SAME message in place every ~5min tick, so the user had no
            # way to tell "just refreshed" from "been sitting here 20 minutes
            # unchanged". Always send a NEW message instead -- the timestamp
            # in the header (see _render_bsjp2_intraday_message) makes each
            # update visibly distinguishable in the chat history.
            sent = await bot.send_message(chat_id=core_engine.TELEGRAM_CHAT_ID, text=text)
            live_state["message_id"] = sent.message_id
        except Exception as e:
            print(f"⚠️ BSJP v2 intraday tick: gagal kirim pesan: {e}")

    _save_json_state("bsjp2_live_state.json", live_state)
    summary["tracked"] = len(active)
    return summary


def _dynamic_tp_sl(tier: str, price_now: float, trigger: float, swing_high_60d_prior: float | None, bb_upper_today: float | None) -> dict:
    """All returned prices are snapped to a valid IDX tick (engine/scanalert.py
    _idx_round_tick_ceil) -- raw MFE-percentage math almost never lands on a
    tradeable price, and `trigger`/SL from the caller is already tick-valid.
    TP1/TP2/TP3 round UP (never down) so the displayed target still implies
    AT LEAST the calibrated %, matching the convention already used for the
    trigger/fading price."""
    import engine.scanalert as scanalert_engine
    tp1_pct, tp2_pct = DYNAMIC_TP_TABLE.get(tier, DYNAMIC_TP_TABLE["TIER1"])
    tp1 = scanalert_engine._idx_round_tick_ceil(price_now * (1 + tp1_pct / 100.0))
    tp2 = scanalert_engine._idx_round_tick_ceil(price_now * (1 + tp2_pct / 100.0))
    tp3_candidates = [v for v in (swing_high_60d_prior, bb_upper_today) if v is not None]
    tp3 = scanalert_engine._idx_round_tick_ceil(max(tp3_candidates)) if tp3_candidates else None
    if tp3 is not None and tp3 <= tp2:
        tp3 = None  # TP3 tier-labeling bug fix -- never show a TP3 below TP2
    return {"tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": trigger}


def _tp_tier_and_display(tier: str, rocket: str | None) -> tuple[str, str]:
    """Returns (tp_calc_tier, display_tag). Rocket ALWAYS wins the display
    slot when present -- it already implies its own (bigger) TP calibration
    was used, so showing the underlying conviction tier alongside it would
    be misleading (a rocket pick can legitimately have no TIER1 conviction
    tier at all, since rocket and TIER1 are independent conditions)."""
    if rocket == "🚀🚀":
        return "ROCKET_PLUS", "🚀🚀 ROKET+"
    if rocket == "🚀":
        return "ROCKET", "🚀 ROKET"
    fire = FIRE_ICON_BY_TIER.get(tier, "")
    return tier, (fire if fire else "·")


def _render_bsjp2_intraday_message(active: list[tuple]) -> str:
    import engine.legacy_core as core_engine
    import engine.scanalert as scanalert_engine
    now_wib = datetime.datetime.now(core_engine.WIB)
    lines = [f"🔥 BSJP LIVE — status D+1 (update {now_wib.strftime('%H:%M:%S')} WIB)\n"]
    any_hidden_fading = False
    for ticker, c, t_state in active:
        price_now = t_state.get("price_now")
        trigger = scanalert_engine._idx_round_tick_ceil(c["prev_close"] * (1 + STAGE1_RET2D_MIN_PCT / 100.0))
        if price_now is None:
            lines.append(f"{ticker} — belum ada data live siklus ini")
            continue
        # FADING is hidden from display (user request 2026-09-25, same
        # convention as MACD-confirm/swing, commit c51b57a): it means "don't
        # enter today", not "gone" -- still tracked in live_state every tick
        # (t_state["fading"] above), so a recovery re-appears automatically
        # without extra logic. Don't filter in the tracking loop itself, only
        # here at display time.
        if t_state.get("fading"):
            any_hidden_fading = True
            continue
        rocket = t_state.get("rocket")
        rocket = rocket if rocket and rocket != "none" else None
        tier = t_state.get("tier") or "STAGE1_BASE"
        tp_tier, display_tag = _tp_tier_and_display(tier, rocket)
        # BUGFIX (user report 2026-09-21): TP1/TP2/TP3 now computed from the
        # LOCKED entry price (set once, see run_bsjp2_intraday_tick), NOT the
        # live price_now -- targets stay fixed for the rest of the session
        # instead of drifting (and TP3 silently appearing/disappearing) as
        # price moves tick-to-tick.
        entry_price = t_state.get("entry_price_locked") or price_now
        # Live BB-upper-in-progress isn't computed intraday (would need a full
        # OHLCV refetch, not just the cheap live bar) -- TP3 intraday uses
        # only the swing-high leg; finalize_bsjp2_confirmations adds the
        # BB-upper leg once tonight's full close is available.
        tp_sl = _dynamic_tp_sl(tp_tier, entry_price, trigger, c.get("swing_high_60d_prior"), None)
        block = [f"{ticker} Now {price_now:,.0f} (entry {entry_price:,.0f}) | {display_tag}"]
        # No-chasing guard (user request 2026-09-25, same logic already
        # validated/shipped for MACD-confirm: project_macd_confirm_sltp_
        # currentprice_and_nochase_2026_09_23). Once the live price has
        # already run past TP2 (the higher of the two calibrated targets),
        # buying now has objectively less room left to that same historical
        # ceiling than the tier's own backtest assumed -- not a claim the
        # stock is bad, just that today's price is no longer a fresh entry.
        if price_now > tp_sl["tp2"]:
            block.append(
                f"⛔ JANGAN DIKEJAR — harga sudah lewat TP2 ({tp_sl['tp2']:,.0f}), "
                "entry baru di sini sudah kehilangan sebagian besar target yang dihitung"
            )
        else:
            block.append(f"TP1 {tp_sl['tp1']:,.0f} (+{(tp_sl['tp1']/price_now-1)*100:.1f}% dari now)")
            block.append(f"TP2 {tp_sl['tp2']:,.0f} (+{(tp_sl['tp2']/price_now-1)*100:.1f}% dari now)")
            if tp_sl["tp3"] is not None:
                block.append(f"TP3 {tp_sl['tp3']:,.0f} (+{(tp_sl['tp3']/price_now-1)*100:.1f}% dari now)*")
        block.append(f"SL {tp_sl['sl']:,.0f} ({(tp_sl['sl']/price_now-1)*100:.1f}% dari now)")
        block.append(f"Trigger/fading: {trigger:,.0f}")
        lines.append("\n".join(block))
    footer = [
        "\n· = confirmed Stage-1 tapi belum capai TIER1 (konviksi lebih tinggi) -- tetap valid, "
        "TP pakai angka dasar.\n*TP3 cuma tampil kalau targetnya genuinely di atas TP2.\n"
        "TP1/TP2/TP3/SL dikunci di harga saat pertama terkonfirmasi -- TIDAK berubah lagi sepanjang sesi "
        "biar tidak jadi target bergerak; \"dari now\" cuma menunjukkan jarak dari harga saat ini."
    ]
    if any_hidden_fading:
        footer.append(
            "(Ada kandidat FADING (termasuk yang kena pola spike-lalu-fade: high jauh di atas "
            "close & closing di separuh bawah range hari itu) yang disembunyikan dari tampilan -- "
            "tetap dipantau, akan muncul lagi kalau recover.)"
        )
    lines.append("\n".join(footer))
    return "\n\n".join(lines)


# =====================================================================
# Phase C -- nightly finalize (Stage-1 confirmation + Stage-2 tier + TP/SL)
# =====================================================================

def finalize_bsjp2_confirmations(results: dict) -> list[dict]:
    """Called nightly, same call site as build_bsjp2_watchlist, using
    TONIGHT's `results` as D1's own data. Confirms Stage-1 for whatever was
    on the watchlist saved the PREVIOUS night, assigns the Stage-2 tier /
    rocket tag, and computes the dynic TP1/TP2/TP3+SL recommendation."""
    import engine.scanalert as scanalert_engine

    watchlist_state = _load_json_state("bsjp2_watchlist_state.json")
    candidates = watchlist_state.get("candidates") or {}
    if not candidates:
        return []
    live_state = _load_json_state("bsjp2_live_state.json")
    # Only trust today's live-tracking rocket tags -- a stale live_state (job
    # never ran today, or this is being re-run late) must not attribute a
    # leftover rocket tag from a previous day's tickers.
    live_tickers = live_state.get("tickers") or {} if live_state.get("trading_day_marker") == _today_str() else {}

    confirmed = []
    for ticker, c in candidates.items():
        r = results.get(ticker)
        if r is None or r.get("bsjp2_close") is None:
            continue
        close_d1 = r["bsjp2_close"]
        prev_close = c["prev_close"]
        if not prev_close:
            continue
        ret_2d_cum = (close_d1 - prev_close) / prev_close * 100.0
        if ret_2d_cum < STAGE1_RET2D_MIN_PCT:
            continue  # never confirmed -- faded, drop silently (state file gets replaced by tomorrow's watchlist anyway)
        if _is_spike_fade_candle(r.get("bsjp2_high"), r.get("bsjp2_low"), close_d1):
            continue  # spike-then-faded D1 candle -- backtest-validated quality downgrade, treated as fading (drop silently)

        atr_prior = c.get("atr_pct_prior")
        pct_b_prior = c.get("pct_b_prior")
        gap_pct = c.get("gap_pct")
        rsi14_d1 = r.get("bsjp2_rsi14")  # tonight's own rsi = D1's rsi, since tonight IS D1 for this ticker
        tier = _assign_conviction_tier(ret_2d_cum, pct_b_prior, gap_pct, atr_prior, rsi14_d1)

        rocket = (live_tickers.get(ticker) or {}).get("rocket")
        if rocket == "🚀🚀":
            tier_label = "ROCKET_PLUS"
        elif rocket == "🚀":
            tier_label = "ROCKET"
        else:
            tier_label = tier

        trigger = scanalert_engine._idx_round_tick_ceil(prev_close * (1 + STAGE1_RET2D_MIN_PCT / 100.0))
        tp_sl = _dynamic_tp_sl(tier_label, close_d1, trigger, c.get("swing_high_60d_prior"), r.get("bsjp2_bb_upper"))

        confirmed.append({
            "ticker": ticker,
            "tier": tier,
            "rocket": rocket if rocket and rocket != "none" else None,
            "close_d1": close_d1,
            "ret_2d_cum": round(ret_2d_cum, 2),
            "tp1": tp_sl["tp1"], "tp2": tp_sl["tp2"], "tp3": tp_sl["tp3"], "sl": tp_sl["sl"],
        })

    today = _today_str()
    _save_json_state("bsjp2_confirmed_picks.json", {"trading_day_marker": today, "picks": confirmed})
    return confirmed


def build_bsjp2_tp_message() -> str:
    """/bsjp tp -- reads tonight's finalized confirmations."""
    state = _load_json_state("bsjp2_confirmed_picks.json")
    picks = state.get("picks") or []
    if not picks:
        return "📋 Tidak ada BSJP pick yang terkonfirmasi Stage-1 hari ini (ret_2d_cum belum tembus 2%)."
    lines = [f"🎯 BSJP TP/SL REKOMENDASI — {len(picks)} pick terkonfirmasi (siap pasang sebelum open besok)\n"]
    for p in sorted(picks, key=lambda x: x["ret_2d_cum"], reverse=True):
        _, display_tag = _tp_tier_and_display(p["tier"], p.get("rocket"))
        tier_name = p["tier"].replace("_", "-") if p["tier"] != "STAGE1_BASE" else "belum TIER1"
        tag = display_tag if p.get("rocket") else f"{display_tag} {tier_name}".strip()
        lines.append(
            f"{p['ticker']} [{tag}] — closing {p['close_d1']:,.0f} ({p['ret_2d_cum']:+.1f}% 2d)\n"
            f"  TP1 {p['tp1']:,.0f} | TP2 {p['tp2']:,.0f}"
            + (f" | TP3 {p['tp3']:,.0f}*" if p.get("tp3") else "")
            + f"\n  SL {p['sl']:,.0f}"
        )
    lines.append(f"\n*TP3 cuma tampil kalau targetnya di atas TP2.\n\n{HOLD_CUT_GUIDANCE}")
    return "\n\n".join(lines)
