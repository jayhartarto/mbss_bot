"""
engine/macd_confirm_pillar.py — MBSS v2 (2026-09-22, 3rd swing-trade lane;
MECHANICALLY REDESIGNED 2026-10-14, see memory
project_swing_menu_trimming_2026_10_14.md for the full research trail --
this replaces the original D1-D5 tag/close-only-D12 design below).

Entry gate (Stage-1, UNCHANGED, all features causal, from bars up to and
including the trigger day):
  - macd_line (SMA12-SMA26, Brights convention) > 0 for >=90% of the
    trailing 10 trading days
  - macd_slope_5d (macd_line - macd_line 5d ago) >= 0 (not diving)
  - no 3-consecutive-down-day streak anywhere in the trailing 10d window
  - atr14_pct (Wilder) <= 5.78
  - value_20d_avg (liquidity) in [19,586,155 ; 1,259,645,238] IDR -- a
    HUMP-shaped band, not a ceiling
  - std_ret_10d (10d daily-return stdev) <= 4.29%
  - FF exclusion: net_value_z20<=-1.0 vs this ticker's own trailing
    20d baseline (see _ff_net_value_z20_excluded)

Lane assignment (Quality(D2)/HighRisk/Reward/Core-Neutral, UNCHANGED
formula) is kept for INFORMATIONAL display only as of 2026-10-14 -- see
"2026-10-14 REDESIGN" below for why it no longer gates or scores anything.

2026-10-14 REDESIGN (supersedes everything below this point in spirit;
old text kept further down for research-trail continuity, read the new
findings first):

  A full re-investigation this session found the ENTIRE D1-D5 tag system
  (FADING/VALID/STRONG/VERY STRONG) does NOT predict anything useful once
  honestly measured with a FRESH-ENTRY-ANCHORED (not D1-anchored) method:
  tag only distinguishes eventual PAYOUT SIZE, not win-rate (all tags sit
  near 45-47% win pooled) -- the original 70-90%-by-tag numbers in this
  docstring's older sections were a D1-entry-anchoring lookahead artifact,
  not real. No D1-knowable entry-gate feature (consistency magnitude,
  MACD-slope, ATR, liquidity, RSI, signal-day candle strength, FF
  z-score, lane) predicts win-rate OR loser/FADING status either (all
  spreads under ~7pp, same noise signature as a rejected OSB-v2 lead that
  session). The ONE real, large, actionable lever found: **regime_phase
  at the entry/trigger day**. BOUNCE_OFF_LOW is consistently and by far
  the best regime across BOW, VCP, OSB-v2 AND this lane (pooled baseline
  win=56.4% vs 36.8-44.0% for the other 4 phases, n=3166 of 14268) --
  PULLBACK_IN_BOUNCE is consistently the worst everywhere this session
  (third independent confirmation of that specific pattern, see
  buy_on_weakness.py / vcp_pillar.py for the other two).

  Exit redesign: also tested whether real SL/TP-touch mechanics (vs the
  old close-only-D12) and various maxhold caps improve ret/day, same
  methodology as buy_on_weakness.py/vcp_pillar.py's own maxhold sweeps
  this session. Tried: conditional continuation keyed off the D2-D5 tag
  (both "ride to day5 then exit" and "exit immediately on a FADING day"
  variants) -- BOTH underperform a plain fixed exit on ret/day (cutting
  early on a stalled/fading signal forfeits more comebacks than it saves,
  same lesson as a rejected BOW idea this session). Tried a shrinking-
  threshold "early-TP" idea (bank a small profit on day 1-4 if floating
  gain is still modest) -- works well with a near-zero floor but the gain
  from that comes almost entirely from banking sub-1% floating profits,
  which a real broker fee would eat; once floored at a fee-realistic 1%,
  the benefit evaporates back to baseline. User's final call: skip all
  the conditional machinery, use the plain fixed exit.

  **LOCKED DESIGN**: entry gated additionally to regime_phase==
  BOUNCE_OFF_LOW (checked ONCE per nightly run, same pattern as
  engine/oversold_bounce_v2.py's dd_100 gate -- produces zero candidates
  on nights it doesn't match, that's expected, not a bug). Exit: SL=-6%
  and TP=+8% as REAL intraday touch triggers (checked against the day's
  own Low/High, SL checked first same "conservative same-day-ambiguity"
  convention as every other lane here), maxhold=5 trading days (EXPIRED
  at close if neither touched by then). Backtested (BOUNCE_OFF_LOW
  population, n=3166): win=62.2%, mean=+1.23%/trade, ret/day=0.296 --
  this specific combo was chosen over several ret/day-superior but either
  thinner or practically-awkward alternatives (e.g. maxhold=1 TP20/SL10
  reached ret/day=0.508 but collapses the "swing" hold into an overnight
  flip, blurring into this bot's day-trade lanes -- user's explicit call
  to keep this pillar's swing character over maximizing ret/day).

  The D1-D5 tag/lane-keyed TP tables, win-rate tables, chase-cutoff, and
  deep-fade-from-peak logic below are RETIRED from decision-making --
  `is_chasing_too_high()` and `needs_hard_sl_warning()` now always return
  False (their premises no longer apply: there's no more lane-specific
  fade-risk once a REAL mechanical -6% SL caps every trade uniformly, and
  no D1-knowable chase signal survived re-testing). `win_rate_of()` now
  returns the single flat backtested rate (see WIN_RATE_FLAT) for any
  active pick rather than a per-(day,lane,tag) lookup -- kept as a
  function (not deleted) because engine/nightly.py's momentum-pillar
  adjustment calls it directly; this is the one deliberate blast-radius
  consideration from this redesign, see that call site before changing
  the function signature further.

  Alerts now show ONCE at entry (Day 1) and go silent until resolution
  (SL_HIT/TP_HIT/EXPIRED) -- there is no decision point in days 2-5
  anymore (confirmed by every conditional-exit test above), so repeating
  "AGING Day N/5" served no purpose. commands/scan.py's swing_command
  filters display to age_days<=1 for this lane; the persisted pick still
  tracks/resolves normally in the background regardless of what's shown
  (house convention: never filter in the engine/persistence layer).

--- ORIGINAL 2026-09-22/23 RESEARCH TRAIL (tag system, retired 2026-10-14
but kept for history -- do not revive without re-reading the redesign
section above first) ---

IMPORTANT framing, user-corrected 2026-09-22: the D1-D5 tag is an ENTRY-
TIMING / watchlist signal, NOT a live-position exit-management tool.

Stage-2 conviction tiering (checked DAILY from age_day 1 through 5, then
LOCKED): tertile cut of the historical confirmed-positive (ret>0)
distribution, thresholds re-derived per age_day:
  FADING:      ret_so_far <= 0
  VALID:       0 < ret_so_far <= t1(age_day)
  STRONG:      t1(age_day) < ret_so_far <= t2(age_day)
  VERY STRONG: ret_so_far > t2(age_day)
CAVEAT (confirmed and GENERALIZED by the 2026-10-14 redesign above): the
70-90%-by-tag figure was ALWAYS a D1-entry-anchoring illusion, not just
"needs the remaining-window table instead" as first thought in 2026-09-23
-- the remaining-window table itself turned out to carry no real win-rate
signal either once checked this session.
"""
from __future__ import annotations

import datetime
import json
import os

import numpy as np
import pandas as pd

from engine import legacy_core as core
from engine import scanalert as scanalert_engine  # IDX tick-size rounding (_idx_round_tick*)
import engine.buy_on_weakness as bow_engine  # reuse _load_ff_lookup, same FF archive
import engine.ihsg_regime as ihsg_regime_engine  # BOUNCE_OFF_LOW entry gate, 2026-10-14 redesign

# ---------------------------------------------------------------------------
# Constants (all validated in the research trail referenced above)
# ---------------------------------------------------------------------------
PICKS_FILE = os.path.join(core.PROJECT_ROOT, "macd_confirm_pillar_picks.json")

MIN_HISTORY = 60
N_LB = 10
CONSISTENCY_THRESHOLD = 0.90

ATR_MAX = 5.78
LIQ_MIN = 19_586_155.0
LIQ_MAX = 1_259_645_238.0
STD_RET_10D_MAX = 4.29

QUALITY_LIQ_MIN = 19_586_155.0
QUALITY_LIQ_MAX = 59_722_504.0
HIGHRISK_RSI_MIN = 81.01
HIGHRISK_SIGNAL_RET_MIN = 4.17
HIGHRISK_SIGNAL_GAP_MIN = 1.82
HIGHRISK_SIGNAL_RANGE_MAX = 0.51

LANE_ICONS = {"Quality(D2)": "⭐⭐", "Core/Neutral": "⚪⭐", "HighRisk/Reward": "⚠️⭐"}


# --- 2026-10-14 REDESIGN: fixed mechanical exit, replaces the retired
# D1-D5 tag/TP-table/close-only-D12 system above (see module docstring).
REQUIRED_REGIME_PHASE = "BOUNCE_OFF_LOW"  # entry gate, checked once per nightly run
SL_PCT = 6.0
TP_PCT = 8.0
ALERT_MAX_AGE_DAYS = 5  # real intraday SL/TP touch checked daily; EXPIRED at close if neither hit by day 5
WIN_RATE_FLAT = 62.2    # backtested win% for this exact combo (BOUNCE_OFF_LOW, n=3166) -- used by win_rate_of()


# ---------------------------------------------------------------------------
# Feature computation — single ticker, TODAY's row only.
# ---------------------------------------------------------------------------
def _compute_latest_features(df: pd.DataFrame) -> dict | None:
    """df: output of core.get_ohlcv_daily_from_db (index=date, columns
    Open/High/Low/Close/Volume, ascending). Returns None if not enough
    history for a reliable read."""
    if df is None or len(df) < MIN_HISTORY:
        return None

    close = df["Close"]; high = df["High"]; low = df["Low"]; volume = df["Volume"]
    daily_ret = close.pct_change()

    macd_line = close.rolling(12).mean() - close.rolling(26).mean()
    pct_above_10d = macd_line.gt(0).rolling(N_LB).mean()
    macd_slope_5d = macd_line - macd_line.shift(5)

    down_day = (daily_ret < 0).astype(int)
    three_down = down_day & down_day.shift(1).fillna(0).astype(int) & down_day.shift(2).fillna(0).astype(int)
    no_3down_streak = three_down.rolling(N_LB).max().eq(0)

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr14_pct = tr.ewm(alpha=1 / 14, adjust=False).mean() / close * 100

    value_20d_avg = (close * volume).rolling(20).mean()
    std_ret_10d = daily_ret.rolling(10).std() * 100
    rsi14 = core.calculate_rsi(close)

    signal_day_ret = daily_ret * 100
    signal_day_gap_pct = (df["Open"] - prev_close) / prev_close * 100
    signal_day_range_pct = (high - low) / close * 100

    def last(s):
        v = s.iloc[-1]
        return float(v) if pd.notna(v) else None

    return {
        "close": float(close.iloc[-1]),
        "pct_above_10d": last(pct_above_10d),
        "macd_slope_5d": last(macd_slope_5d),
        "no_3down_streak": bool(no_3down_streak.iloc[-1]) if pd.notna(no_3down_streak.iloc[-1]) else False,
        "atr14_pct": last(atr14_pct),
        "value_20d_avg": last(value_20d_avg),
        "std_ret_10d": last(std_ret_10d),
        "rsi14": last(rsi14),
        "signal_day_ret": last(signal_day_ret),
        "signal_day_gap_pct": last(signal_day_gap_pct),
        "signal_day_range_pct": last(signal_day_range_pct),
        "latest_date": df.index[-1].strftime("%Y-%m-%d"),
    }


def _passes_stage1(feats: dict) -> bool:
    if feats["pct_above_10d"] is None or feats["pct_above_10d"] < CONSISTENCY_THRESHOLD:
        return False
    if feats["macd_slope_5d"] is None or feats["macd_slope_5d"] < 0:
        return False
    if not feats["no_3down_streak"]:
        return False
    if feats["atr14_pct"] is None or feats["atr14_pct"] > ATR_MAX:
        return False
    if feats["value_20d_avg"] is None or not (LIQ_MIN <= feats["value_20d_avg"] <= LIQ_MAX):
        return False
    if feats["std_ret_10d"] is None or feats["std_ret_10d"] > STD_RET_10D_MAX:
        return False
    return True


def _assign_lane(feats: dict) -> str:
    if QUALITY_LIQ_MIN <= feats["value_20d_avg"] < QUALITY_LIQ_MAX:
        return "Quality(D2)"
    is_highrisk = (
        (feats["rsi14"] is not None and feats["rsi14"] >= HIGHRISK_RSI_MIN)
        or (feats["signal_day_ret"] is not None and feats["signal_day_ret"] >= HIGHRISK_SIGNAL_RET_MIN)
        or (feats["signal_day_gap_pct"] is not None and feats["signal_day_gap_pct"] >= HIGHRISK_SIGNAL_GAP_MIN)
        or (feats["signal_day_range_pct"] is not None and feats["signal_day_range_pct"] <= HIGHRISK_SIGNAL_RANGE_MAX)
    )
    return "HighRisk/Reward" if is_highrisk else "Core/Neutral"


def _ff_net_value_z20_excluded(ticker: str, df: pd.DataFrame, ff_lookup: pd.DataFrame) -> bool:
    """EXCLUSION (2026-10-07, validated -- memory project_ff_netsell_
    exclusion_sweep_2026_10_07.md + project_ff_final_revalidation_
    2026_10_07.md): z-score of today's RAW foreign net-SELL value (not
    volume-normalized ratio) vs this ticker's own trailing 20-day
    baseline (excluding today). `net_value_z20<=-1.0` degrades this
    lane's own Stage-1 win rate 48.6%->43.7% (wlb 47.6->40.4) and nearly
    halves the mean forward return (+4.46%->+1.91%), on a big sample
    (n=851 of 8464). NOTE: this is a MEAN/win-rate improvement only --
    it does NOT tame this lane's fat tail (worst/best single trade are
    unchanged either way, since the exit is close-only at D12 with no
    SL/TP trigger at all). Missing FF data NEVER excludes (returns
    False) -- house convention, missing = neutral."""
    if len(df) < 21:
        return False
    last21_dates = df.index[-21:]
    ffg = ff_lookup[(ff_lookup.ticker == ticker) & (ff_lookup.date.isin(last21_dates))]
    if len(ffg) < 21:
        return False
    net_by_date = ffg.drop_duplicates("date").set_index("date")["foreign_net"].reindex(last21_dates)
    if net_by_date.isna().any():
        return False
    baseline, today = net_by_date.iloc[:-1], net_by_date.iloc[-1]
    std = baseline.std()
    if not std or pd.isna(std):
        return False
    z = float(np.clip((today - baseline.mean()) / std, -10, 10))
    return z <= -1.0


def evaluate_ticker(ticker: str, ff_lookup: pd.DataFrame | None = None) -> dict | None:
    """Returns a candidate dict if `ticker` qualifies for Stage-1 TODAY,
    else None. entry_ref_price = today's close; SL/TP are now FIXED at
    entry (2026-10-14 redesign, see module docstring) -- both anchored to
    this entry_ref_price and never recomputed on later days, matching the
    validated backtest exactly (SL_PCT/TP_PCT checked against real daily
    Low/High in _resolve_active_pick, not close-only).

    `ff_lookup` optional (defaults to None -> exclusion check simply
    skipped, never crashes) so existing callers/tests that don't pass it
    keep working unchanged. Does NOT check the regime_phase gate itself --
    see compute_macd_confirm_candidates, which checks it ONCE for the
    whole nightly run rather than per-ticker."""
    df = core.get_ohlcv_daily_from_db(ticker, limit=150)
    feats = _compute_latest_features(df)
    if feats is None or not _passes_stage1(feats):
        return None

    # EXCLUSION (2026-10-07, validated, see _ff_net_value_z20_excluded
    # docstring) -- the ONLY FF filter for this lane (no booster found).
    if ff_lookup is not None and _ff_net_value_z20_excluded(ticker, df, ff_lookup):
        return None

    lane = _assign_lane(feats)  # informational only since 2026-10-14, see docstring
    entry_ref = feats["close"]

    return {
        "ticker": ticker,
        "trigger_date": feats["latest_date"],
        "status": "ALIVE",
        "lane": lane,
        "age_days": 1,
        "entry_ref_price": entry_ref,
        "current_price": entry_ref,  # informational "price now" display, updated nightly
        "sl_price": scanalert_engine._idx_round_tick_floor(entry_ref * (1 - SL_PCT / 100)),
        "tp_price": scanalert_engine._idx_round_tick_ceil(entry_ref * (1 + TP_PCT / 100)),
        "ret_so_far_pct": None,
        "resolved_date": None,
        "resolved_ret_pct": None,
    }


def compute_macd_confirm_candidates(tickers: list[str]) -> list[dict]:
    """Nightly entry point: evaluate every ticker in `tickers`, return the
    list that qualifies today (does NOT touch the picks-history file --
    call update_and_save_picks() with the result to persist/merge).

    REGIME GATE (2026-10-14 redesign, see module docstring): checked ONCE
    here, not per-ticker -- produces an empty list on any night regime_phase
    isn't BOUNCE_OFF_LOW, same "zero candidates is expected, not a bug"
    convention as engine/oversold_bounce_v2.py's dd_100 gate. Fails CLOSED
    (no candidates) on a regime-fetch failure, since the gate can't be
    confirmed -- different from the "missing=neutral" convention used for
    boosters/scoring adjustments elsewhere, this is a hard entry condition."""
    regime = ihsg_regime_engine.get_ihsg_regime_today()
    if regime is None or regime.get("regime_phase") != REQUIRED_REGIME_PHASE:
        return []

    ff_lookup = bow_engine._load_ff_lookup()
    candidates = []
    for t in tickers:
        try:
            c = evaluate_ticker(t, ff_lookup)
        except Exception as e:
            print(f"⚠️ MACD-confirm pillar: gagal evaluasi {t}: {e}")
            continue
        if c is not None:
            candidates.append(c)
    return candidates


# ---------------------------------------------------------------------------
# Pick-history persistence -- same 3-layer safety pattern as
# buy_on_weakness.py / vcp_pillar.py (REUSE core's existing generic helpers).
# ---------------------------------------------------------------------------
def load_macd_confirm_picks() -> list:
    if not os.path.exists(PICKS_FILE):
        return []
    try:
        with open(PICKS_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca macd_confirm_pillar_picks.json: {e}")
        return []


def save_macd_confirm_picks(picks: list):
    tmp_path = PICKS_FILE + ".tmp"
    try:
        core._backup_json_file_daily(PICKS_FILE)
        with open(tmp_path, "w") as f:
            json.dump(picks, f, indent=2, default=core._json_default_numpy_safe)
        os.replace(tmp_path, PICKS_FILE)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan macd_confirm_pillar_picks.json: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _resolve_active_pick(pick: dict) -> dict:
    """Re-check one still-ALIVE pick against fresh OHLCV (2026-10-14
    redesign, see module docstring): real intraday SL/TP touch against
    TODAY's Low/High, SL checked first (conservative same-day-ambiguity
    convention used throughout this codebase), then TP; else bump age and
    expire at ALERT_MAX_AGE_DAYS if neither has fired. sl_price/tp_price
    are FIXED at entry_ref_price (set once in evaluate_ticker, never
    recomputed here) -- this matches the validated backtest exactly,
    unlike the old design which recomputed SL off a moving current price."""
    if "tp_price" not in pick:
        # Pre-2026-10-14 pick (old D1-D5 tag system: no tp_price, SL off a moving
        # price). Its exit rules no longer exist, so it can't be resolved under the
        # fixed TP8/SL6/maxhold5 mechanic -- close it WITHOUT a resolved_ret_pct so
        # winrate_monitor (which only counts picks with a resolved_ret_pct) and
        # capital_rank never mix legacy outcomes into the redesigned lane's stats.
        pick["status"] = "LEGACY_CLOSED"
        pick["resolved_date"] = datetime.datetime.now(core.WIB).strftime("%Y-%m-%d")
        return pick

    df = core.get_ohlcv_daily_from_db(pick["ticker"], limit=150)
    feats = _compute_latest_features(df)
    if feats is None:
        return pick  # can't evaluate today (e.g. delisted/no data) -- leave as-is

    today_date = feats["latest_date"]
    if today_date == pick.get("_last_checked_date"):
        return pick  # already resolved for today, avoid double-incrementing age

    today_low = float(df["Low"].iloc[-1])
    today_high = float(df["High"].iloc[-1])
    entry_ref = pick["entry_ref_price"]

    if today_low <= pick["sl_price"]:
        pick["status"] = "SL_HIT"
        pick["resolved_date"] = today_date
        pick["resolved_ret_pct"] = round((pick["sl_price"] - entry_ref) / entry_ref * 100, 2)
        pick["_last_checked_date"] = today_date
        return pick
    if today_high >= pick["tp_price"]:
        pick["status"] = "TP_HIT"
        pick["resolved_date"] = today_date
        pick["resolved_ret_pct"] = round((pick["tp_price"] - entry_ref) / entry_ref * 100, 2)
        pick["_last_checked_date"] = today_date
        return pick

    pick["age_days"] = pick.get("age_days", 1) + 1
    pick["ret_so_far_pct"] = round((feats["close"] - entry_ref) / entry_ref * 100, 2)
    pick["current_price"] = feats["close"]

    if pick["age_days"] > ALERT_MAX_AGE_DAYS:
        pick["status"] = "EXPIRED"
        pick["resolved_date"] = today_date
        pick["resolved_ret_pct"] = pick["ret_so_far_pct"]

    pick["_last_checked_date"] = today_date
    return pick


def update_and_save_picks(new_candidates: list[dict]) -> list:
    """Merge today's qualifying candidates into the persisted pick history:
    re-evaluate every still-active existing pick (age/tag/expiry), then
    append a fresh entry ONLY for tickers that don't already have an
    active pick. Call once per nightly cycle."""
    history = load_macd_confirm_picks()

    active_tickers = set()
    for pick in history:
        if pick.get("status") == "ALIVE":
            try:
                pick = _resolve_active_pick(pick)
            except Exception as e:
                print(f"⚠️ MACD-confirm pillar: gagal resolve pick {pick.get('ticker')}: {e}")
            active_tickers.add(pick["ticker"])

    for cand in new_candidates:
        if cand["ticker"] in active_tickers:
            continue
        history.append(cand)
        active_tickers.add(cand["ticker"])

    from engine import newbuyer_tag  # booster tag 2026-10-27, display-only; fail-safe (never raises)
    newbuyer_tag.annotate(history, "MACD-confirm")
    save_macd_confirm_picks(history)
    return history


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
def format_age_label(pick: dict) -> str:
    """'NEW (Day 1/5)' or 'AGING (Day N/5) -- ALIVE'. Display-layer only
    (commands/scan.py's swing_command filters to age_days<=1 for this lane
    as of 2026-10-14, so AGING never actually shows there -- kept generic
    in case another caller wants the fuller label.)"""
    age = pick.get("age_days", 1)
    return f"NEW (Day {age}/5)" if age <= 1 else f"AGING (Day {age}/5) -- ALIVE"


def is_chasing_too_high(pick: dict) -> bool:
    """RETIRED 2026-10-14 (see module docstring) -- always False now.
    The Quality(D2)-only chase-cutoff this guarded was keyed to the D1-D5
    tag system, which this session's fresh-entry-anchored re-test showed
    carries no real predictive signal; kept as a function (not deleted)
    because engine/nightly.py's momentum-pillar adjustment calls it
    directly."""
    return False


def needs_hard_sl_warning(pick: dict) -> bool:
    """RETIRED 2026-10-14 (see module docstring) -- always False now.
    The HighRisk/Reward+VERY STRONG fat-tail risk this flagged was a
    consequence of the OLD close-only-D12 exit (no real stop at all); the
    new design's REAL mechanical SL=-6% already caps every trade's
    downside uniformly, so no lane/tag needs a separate warning anymore.
    Kept as a function (not deleted) for engine/nightly.py compatibility."""
    return False


def win_rate_of(pick: dict) -> float | None:
    """Returns the single flat backtested win% for this lane's current
    design (WIN_RATE_FLAT) for any ALIVE/resolved pick, or None if pick is
    falsy/malformed. Replaces the old per-(age_day,lane,tag) lookup table
    (2026-10-14 redesign, see module docstring) -- every active pick now
    passed the SAME regime+entry gate, so there is no more per-pick
    granularity to look up. Kept as a function, not a bare constant,
    because engine/nightly.py's momentum-pillar adjustment calls it
    directly as win_rate_of(pick)."""
    if not pick:
        return None
    return WIN_RATE_FLAT
