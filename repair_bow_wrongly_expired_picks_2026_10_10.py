"""
repair_bow_wrongly_expired_picks_2026_10_10.py — one-time data repair.

Fixes `buy_on_weakness_picks.json` entries that were wrongly marked
EXPIRED by the pre-2026-10-10 bug: `ALERT_MAX_AGE_DAYS` (5) was used as
BOTH the entry-freshness label AND the SL/TP tracking cutoff, when it was
only ever meant to be the former (user clarification, see
`engine/buy_on_weakness.py` docstring and memory
`project_wyckoff_methodology_exploration_2026_10_09.md` for the full
diagnosis/validation trail). Every entry hit by the old bug has:
  - status == "EXPIRED"
  - resolved_ret_pct is None (the old EXPIRED branch never set it --
    only the SL_HIT/TP2_HIT branches did)
  - age_days == 6 (exactly old ALERT_MAX_AGE_DAYS(5) + 1)
That signature is UNIQUE to the old bug. A genuine EXPIRED under the
corrected rule (RESOLUTION_MAX_AGE_DAYS=16) would show age_days == 17 --
this filter never touches those, so re-running this script after the
fix has been live for a while is a harmless no-op on new data.

For each flagged entry, this REPLAYS forward from entry_ref_price using
the ACTUAL historical OHLCV already in the DB (not a blind resume) --
walks day by day starting from the FIRST day the old code never looked
at (day 6 of 5 real trading days were already consumed reaching
age_days=6; the old code checked SL/TP each of those days and found
neither, which is WHY it's safe to skip straight to day 6 onward rather
than re-checking), applying the exact SL(-18%)/TP2(band-touch, validity-
fixed) rule, bounded by RESOLUTION_MAX_AGE_DAYS(16) total age from the
trigger day. This determines the TRUE outcome as of today's data:
SL_HIT, TP2_HIT, genuinely-still-ALIVE (within window, unresolved, data
just hasn't caught up to the window edge yet), or NOW-EXPIRED (window
has elapsed since under the corrected rule).

Run ONCE after deploying the 2026-10-10 fix (commits 743542e, b95d46b).
Idempotent -- safe to re-run; already-repaired entries no longer match
the flag signature so they're left untouched.
"""
from __future__ import annotations

import pandas as pd

from engine import legacy_core as core
import engine.buy_on_weakness as bow_engine

OLD_BUG_AGE_DAYS = 6  # == old ALERT_MAX_AGE_DAYS(5) + 1, exact signature
ALREADY_CHECKED_DAYS = OLD_BUG_AGE_DAYS - 1  # 5 real trading days the old code already consumed


def _is_wrongly_expired(pick: dict) -> bool:
    return (
        pick.get("status") == "EXPIRED"
        and pick.get("resolved_ret_pct") is None
        and pick.get("age_days") == OLD_BUG_AGE_DAYS
    )


def _replay_pick(pick: dict) -> str:
    """Mutates `pick` in place with the corrected outcome. Returns a short
    status string for the summary report: 'sl'/'tp'/'alive'/'expired'/'skip'."""
    ticker = pick["ticker"]
    entry_ref = pick["entry_ref_price"]
    sl_abs = entry_ref * (1 - bow_engine.SL_PCT / 100)
    trigger_date = pd.Timestamp(pick["trigger_date"])

    df = core.get_ohlcv_daily_from_db(ticker, limit=400)
    if df.empty:
        return "skip"  # delisted / no data -- leave untouched

    close = df["Close"]
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    bb_upper = sma20 + 2 * std20

    bars_after = df[df.index > trigger_date]
    if len(bars_after) < ALREADY_CHECKED_DAYS:
        return "skip"  # shouldn't happen -- the old code already saw this many bars once

    new_bars = bars_after.iloc[ALREADY_CHECKED_DAYS:]  # the old code never looked at these
    max_k = bow_engine.RESOLUTION_MAX_AGE_DAYS - OLD_BUG_AGE_DAYS  # remaining checks allowed (10 at 16d cap)

    if len(new_bars) == 0:
        return "skip"  # no new trading day since the old expiry yet -- nothing to replay

    last_close, last_band = None, None
    for i, (date, row) in enumerate(new_bars.iloc[:max_k].iterrows(), start=1):
        age_now = OLD_BUG_AGE_DAYS + i
        band = bb_upper.loc[date]
        if row["Low"] <= sl_abs:
            pick["status"] = "SL_HIT"
            pick["resolved_date"] = date.strftime("%Y-%m-%d")
            pick["resolved_ret_pct"] = round((sl_abs - entry_ref) / entry_ref * 100, 2)
            pick["age_days"] = age_now
            return "sl"
        if pd.notna(band) and row["High"] >= band and band >= entry_ref:
            pick["status"] = "TP2_HIT"
            pick["resolved_date"] = date.strftime("%Y-%m-%d")
            pick["resolved_ret_pct"] = round((band - entry_ref) / entry_ref * 100, 2)
            pick["tp2_price_latest"] = round(band)
            pick["age_days"] = age_now
            return "tp"
        last_close, last_band = row["Close"], band

    checked_count = min(len(new_bars), max_k)
    final_age = OLD_BUG_AGE_DAYS + checked_count
    if checked_count >= max_k:
        # consumed the full remaining budget with no SL/TP -- genuinely
        # expired under the corrected rule, not just out of fresh data
        pick["status"] = "EXPIRED"
        pick["age_days"] = bow_engine.RESOLUTION_MAX_AGE_DAYS + 1
        return "expired"

    # still within the corrected window, data just hasn't caught up yet --
    # revive as ALIVE so tonight's regular nightly job continues tracking it
    pick["status"] = "ALIVE"
    pick["age_days"] = final_age
    drift_pct = (last_close - entry_ref) / entry_ref * 100
    zone_key, zone_label = bow_engine.classify_drift_zone(drift_pct)
    pick["price_drift_pct"] = round(drift_pct, 2)
    pick["drift_zone"] = zone_key
    pick["drift_label"] = zone_label
    if pd.notna(last_band):
        pick["tp2_price_latest"] = round(last_band)
    pick.pop("_last_checked_date", None)
    return "alive"


def main():
    picks = bow_engine.load_buy_on_weakness_picks()
    flagged = [p for p in picks if _is_wrongly_expired(p)]
    print(f"Found {len(flagged)} wrongly-EXPIRED picks (old 5-day-cutoff bug signature) "
          f"out of {len(picks)} total entries.")

    counts = {"sl": 0, "tp": 0, "alive": 0, "expired": 0, "skip": 0}
    for p in flagged:
        try:
            outcome = _replay_pick(p)
        except Exception as e:
            print(f"  WARN: failed to replay {p.get('ticker')} (trigger {p.get('trigger_date')}): {e}")
            outcome = "skip"
        counts[outcome] += 1
        if outcome != "skip":
            extra = f"ret={p.get('resolved_ret_pct')}%" if p.get("resolved_ret_pct") is not None else f"age_days={p['age_days']}"
            print(f"  {outcome.upper():8s} {p['ticker']:8s} (trigger {p['trigger_date']}) -> {p['status']}, {extra}")

    print(f"\nSummary: revived_alive={counts['alive']}, resolved_sl={counts['sl']}, "
          f"resolved_tp={counts['tp']}, now_expired_under_corrected_window={counts['expired']}, "
          f"skipped_no_data={counts['skip']}")

    if any(counts[k] for k in ("sl", "tp", "alive", "expired")):
        bow_engine.save_buy_on_weakness_picks(picks)
        print("Saved (backup created automatically via core._backup_json_file_daily).")
    else:
        print("Nothing changed -- not re-saving.")


if __name__ == "__main__":
    main()
