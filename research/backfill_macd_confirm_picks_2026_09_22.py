"""
MBSS v2 (user request 2026-09-22) -- one-time backfill for
macd_confirm_pillar_picks.json: simulate that the nightly job has been
running for the past 8 trading days, so the FIRST real /eodscan (whenever
the bot next runs live) shows a realistic mix of NEW and AGING (Day 1-5)
picks instead of starting from zero.

IMPORTANT: pulls historical OHLCV from the LIVE local DB (same
engine.legacy_core.get_ohlcv_daily_from_db every other module reads from),
NOT a static research CSV -- so this script gives a CORRECT backfill as
of whenever it's actually run (dev laptop or VPS, any date), not a stale
snapshot. The ticker UNIVERSE list comes from ticker_whitelist.json's
`eligible_tickers` (tracked in git, present on every environment) --
research/ohlcv_backtest_raw.csv is a dev-laptop-only research artifact
NEVER committed to git and will NOT exist on a fresh VPS checkout, don't
reintroduce a dependency on it here. Uses the identical Stage-1/lane/tag
logic as engine/macd_confirm_pillar.py (imported directly, not
reimplemented) applied to each historical row. Output schema matches
macd_confirm_pillar.py's pick dicts exactly. Run this ONCE per environment
right after deploying engine/macd_confirm_pillar.py -- re-running it later
would overwrite any real picks the nightly job has already tracked, so
guard against that (see the check near the bottom).
"""
import json
import os
import numpy as np
import pandas as pd

import sys
sys.path.insert(0, ".")
import engine.macd_confirm_pillar as pillar
import engine.legacy_core as core
import engine.scanalert as scanalert

TICKER_LIST_SOURCE = "ticker_whitelist.json"
BACKFILL_WINDOW_DAYS = 8  # look back this many trading days for D0 candidates
DB_FETCH_LIMIT = 200      # bars to pull per ticker from the live DB (needs >=60 for MIN_HISTORY + 10d lookback)


def calc_macd_line(close):
    return close.rolling(12).mean() - close.rolling(26).mean()


def calc_rsi(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))


def compute_fields(df):
    out = []
    for tkr, g in df.groupby("ticker", sort=False):
        g = g.sort_values("date").reset_index(drop=True).copy()
        close, high, low, vol, openp = g["close"], g["high"], g["low"], g["volume"], g["open"]
        daily_ret = close.pct_change()
        macd_line = calc_macd_line(close)
        g[f"pct_above_{pillar.N_LB}"] = macd_line.gt(0).rolling(pillar.N_LB).mean()
        g["macd_slope_5d"] = macd_line - macd_line.shift(5)
        down_day = (daily_ret < 0).astype(int)
        three_down = (down_day & down_day.shift(1).fillna(0).astype(int) & down_day.shift(2).fillna(0).astype(int))
        g[f"no_3down_streak_{pillar.N_LB}"] = three_down.rolling(pillar.N_LB).max().eq(0)
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        g["atr14_pct"] = tr.ewm(alpha=1 / 14, adjust=False).mean() / close * 100
        g["value_20d_avg"] = (close * vol).rolling(20).mean()
        g["std_ret_10d"] = daily_ret.rolling(10).std() * 100
        g["rsi14"] = calc_rsi(close)
        g["signal_day_ret"] = daily_ret * 100
        g["signal_day_gap_pct"] = (openp - prev_close) / prev_close * 100
        g["signal_day_range_pct"] = (high - low) / close * 100
        out.append(g)
    return pd.concat(out, ignore_index=True)


def assign_lane(row):
    if pillar.QUALITY_LIQ_MIN <= row["value_20d_avg"] < pillar.QUALITY_LIQ_MAX:
        return "Quality(D2)"
    is_highrisk = (
        (pd.notna(row["rsi14"]) and row["rsi14"] >= pillar.HIGHRISK_RSI_MIN)
        or (pd.notna(row["signal_day_ret"]) and row["signal_day_ret"] >= pillar.HIGHRISK_SIGNAL_RET_MIN)
        or (pd.notna(row["signal_day_gap_pct"]) and row["signal_day_gap_pct"] >= pillar.HIGHRISK_SIGNAL_GAP_MIN)
        or (pd.notna(row["signal_day_range_pct"]) and row["signal_day_range_pct"] <= pillar.HIGHRISK_SIGNAL_RANGE_MAX)
    )
    return "HighRisk/Reward" if is_highrisk else "Core/Neutral"


def _load_from_live_db(tickers):
    """Pull each ticker's historical OHLCV from the live local DB (same
    source engine/macd_confirm_pillar.py's evaluate_ticker() reads from),
    reshaped into the same long-format columns compute_fields() expects."""
    rows = []
    for t in tickers:
        try:
            hist = core.get_ohlcv_daily_from_db(t, limit=DB_FETCH_LIMIT)
        except Exception as e:
            print(f"  skip {t}: {e}")
            continue
        if hist is None or len(hist) < pillar.MIN_HISTORY:
            continue
        h = hist.reset_index()
        h.columns = [c.lower() if c.lower() != "index" else "date" for c in h.columns]
        h["ticker"] = t
        rows.append(h[["ticker", "date", "open", "high", "low", "close", "volume"]])
    if not rows:
        return pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close", "volume"])
    return pd.concat(rows, ignore_index=True)


def main():
    if os.path.exists(pillar.PICKS_FILE):
        existing = pillar.load_macd_confirm_picks()
        if existing:
            print(f"REFUSING to backfill: {pillar.PICKS_FILE} already has {len(existing)} pick(s). "
                  f"This script is for a ONE-TIME initial seed only -- delete the file first if you "
                  f"really want to re-backfill (this will discard real tracked picks).")
            return

    with open(TICKER_LIST_SOURCE, encoding="utf-8") as f:
        ticker_universe = sorted(json.load(f)["eligible_tickers"])
    print(f"Loading {len(ticker_universe)} tickers from the LIVE local DB...")
    df = _load_from_live_db(ticker_universe)
    if df.empty:
        print("No data loaded from live DB -- aborting.")
        return
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
    df = compute_fields(df)

    stage1 = df[
        (df[f"pct_above_{pillar.N_LB}"] >= pillar.CONSISTENCY_THRESHOLD)
        & (df["macd_slope_5d"] >= 0)
        & (df[f"no_3down_streak_{pillar.N_LB}"])
        & (df["atr14_pct"] <= pillar.ATR_MAX)
        & (df["value_20d_avg"] >= pillar.LIQ_MIN)
        & (df["value_20d_avg"] <= pillar.LIQ_MAX)
        & (df["std_ret_10d"] <= pillar.STD_RET_10D_MAX)
    ].copy()

    all_dates = sorted(df["date"].unique())
    tonight = all_dates[-1]
    tonight_idx = len(all_dates) - 1
    print(f"Backfilling as of: {tonight.date()} (last available bar in the live local DB)")

    recent_dates = set(all_dates[-BACKFILL_WINDOW_DAYS:])
    candidates = stage1[stage1["date"].isin(recent_dates)]

    picks = []
    for _, row in candidates.iterrows():
        tkr = row["ticker"]
        d0_date = row["date"]
        d0_idx = all_dates.index(d0_date)
        days_since_d0 = tonight_idx - d0_idx
        if days_since_d0 < 0:
            continue

        full = df[df["ticker"] == tkr].reset_index(drop=True)
        full_dates = full["date"].tolist()
        if d0_date not in full_dates:
            continue
        fi = full_dates.index(d0_date)

        lane = assign_lane(row)
        entry_ref_price = float(row["close"])  # matches production's close-as-entry-ref convention
        age_days = days_since_d0 + 1  # D0 itself = age_days 1 (matches evaluate_ticker's convention)

        if age_days == 1:
            tag = "VALID"  # placeholder, resolved on the NEXT nightly run -- matches evaluate_ticker exactly
            ret_so_far_pct = None
        else:
            check_idx = fi + (age_days - 1)
            if check_idx >= len(full) or check_idx > tonight_idx:
                continue
            current_close = full["close"].iloc[check_idx]
            if not np.isfinite(current_close) or entry_ref_price <= 0:
                continue
            ret_so_far = (current_close - entry_ref_price) / entry_ref_price
            tag = pillar._tag_of(ret_so_far, age_days)
            ret_so_far_pct = round(ret_so_far * 100, 2)

        status = "EXPIRED" if age_days > pillar.ALERT_MAX_AGE_DAYS else "ALIVE"
        picks.append({
            "ticker": tkr,
            "trigger_date": d0_date.strftime("%Y-%m-%d"),
            "status": status,
            "lane": lane,
            "age_days": age_days,
            "entry_ref_price": entry_ref_price,
            "sl_price": scanalert._idx_round_tick_floor(entry_ref_price * (1 - pillar.SL_PCT / 100)),
            "tag": tag,
            "ret_so_far_pct": ret_so_far_pct,
            "resolved_date": None,
            "resolved_ret_pct": None,
            "_last_checked_date": tonight.strftime("%Y-%m-%d"),
        })

    # dedup: keep only the OLDEST active pick per ticker (matches update_and_save_picks'
    # "don't create a new pick if one is already active" rule)
    by_ticker = {}
    for p in picks:
        if p["ticker"] not in by_ticker or p["age_days"] > by_ticker[p["ticker"]]["age_days"]:
            by_ticker[p["ticker"]] = p
    final_picks = list(by_ticker.values())

    tag_counts = {}
    for p in final_picks:
        key = (p["status"], p.get("tag"))
        tag_counts[key] = tag_counts.get(key, 0) + 1
    print(f"\nTotal backfilled picks: {len(final_picks)}")
    for key, n in sorted(tag_counts.items(), key=lambda x: -x[1]):
        print(f"  {key}: {n}")

    with open(pillar.PICKS_FILE, "w", encoding="utf-8") as f:
        json.dump(final_picks, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {len(final_picks)} picks to {pillar.PICKS_FILE}")


if __name__ == "__main__":
    main()
