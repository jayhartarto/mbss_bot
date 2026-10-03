"""
research/entrypagi_streak_backtest_2026_10_01.py -- MBSS v2, user question
chain (2026-10-01):
  1. "kalau entrypagi muncul 2x berturut-turut atau lebih, ini pertanda
     bagus atau malah menjelang exhaustion?"
  2. "kalau dari regime bearish/sideways cenderung ihsg growth negatif,
     apakah tetap sama hasilnya?"
  3. "cek juga yang keluar >2x return high-nya berapa mean, median dan
     tailnya?"
  4. "ini terjadi sama di sinyal momentum maupun bounce?"

ENTRY PAGI itself doesn't log picks into a history file with outcomes
(entry_pagi_state.json is live-session state, not an accumulating record),
so this reconstructs BOTH its DNA gates DIRECTLY from mbss_ohlcv.db's 2+
year daily panel (2024-08-21 to 2026-09-29, 684 tickers) instead of waiting
weeks for live tracking:

  - MOMENTUM-PUMP gate (entry_pagi_dna_gate_pass, engine/scanalert.py):
    pct_b>=0.6, ret_5d_pct>0, atr_pct14>=p75 cross-sectional that day.
  - OVERSOLD-BOUNCE gate (entry_pagi_oversold_bounce_gate_pass, same file):
    rsi14<=p10 cross-sectional, price_vs_sma20_pct<=p10 cross-sectional,
    value_traded>=median cross-sectional. WATCHLIST-ONLY in production
    (no exit-horizon research existed for it before this script -- see its
    own docstring) -- this is also the FIRST outcome backtest for this
    archetype, not just the streak question.

All formulas reproduced EXACTLY from engine/scoring.py / engine/
legacy_core.py: ATR14 = Wilder EWM(alpha=1/14, min_periods=14) of true
range as %% of close; pct_b from SMA20+-2*std20; RSI14 = Wilder's (EWM
alpha=1/14, not a simple rolling mean, see legacy_core.calculate_rsi);
price_vs_sma20_pct = (close-sma20)/sma20*100; ret_5d_pct = 5-trading-day
close-to-close %% change; value_traded = close*volume. Cross-sectional
percentile = engine/scanalert.py's _cross_sectional_percentile exactly
(ascending sort, idx=min(int(n*p), n-1), needs n>=20). No lookahead: every
feature at day T only uses data up to and including T.

Universe: ticker_whitelist.json's eligible_tickers (515) as a proxy for the
production population -- the REAL nightly universe shifts over time, this
uses today's snapshot applied across the whole 2yr window, a known
approximation (same caveat as any single-pass research script here).

Streak = consecutive TRADING DAYS (per ticker's own row sequence, not
calendar days) a given gate has been passing, ending at day T. Forward
return = next trading day's close-to-close %% change from day T's close
(T+1), and separately T+3. Forward-HIGH return = max(High) over the next
N trading days (T+1..T+N), MFE-style, not just the close.

Purely descriptive -- no formula/gate change from this alone, per this
project's "no hunch-based tuning" discipline.
"""
import json
import sqlite3

import numpy as np
import pandas as pd
import yfinance as yf

DB_PATH = "mbss_ohlcv.db"
WHITELIST_PATH = "ticker_whitelist.json"

ATR_PERCENTILE = 0.75
PCT_B_MIN = 0.6
RSI_PERCENTILE = 0.10
DIST_SMA20_PERCENTILE = 0.10
LIQUIDITY_PERCENTILE = 0.50


def cross_sectional_percentile(day_df: pd.DataFrame, field: str, percentile: float):
    """EXACT replica of engine/scanalert.py's _cross_sectional_percentile."""
    vals = sorted(day_df[field].dropna().tolist())
    if len(vals) < 20:
        return None
    idx = min(int(len(vals) * percentile), len(vals) - 1)
    return vals[idx]


def load_panel() -> pd.DataFrame:
    with open(WHITELIST_PATH) as f:
        universe = set(json.load(f).get("eligible_tickers", []))
    print(f"Universe: {len(universe)} ticker (ticker_whitelist.json)")

    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql(
        "SELECT ticker, date, open, high, low, close, volume FROM ohlcv_daily ORDER BY ticker, date",
        conn,
    )
    conn.close()
    df = df[df["ticker"].isin(universe)].copy()
    print(f"Rows loaded (universe-filtered): {len(df):,}, tickers: {df['ticker'].nunique()}, "
          f"date range: {df['date'].min()} to {df['date'].max()}")
    return df


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Explicit per-ticker loop (not groupby().apply()) -- pandas 3.0 always
    excludes the grouping column from what's passed to apply(), which
    silently drops 'ticker' from the whole frame; a plain loop sidesteps
    that entirely."""
    pieces = []
    for ticker, g in df.groupby("ticker", sort=False):
        g = g.sort_values("date").reset_index(drop=True).copy()
        close = g["close"]
        high = g["high"]
        low = g["low"]
        volume = g["volume"]
        prev_close = close.shift(1)

        tr = pd.concat([
            high - low, (high - prev_close).abs(), (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
        g["atr_pct14"] = atr / close.replace(0, pd.NA) * 100

        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        upper = sma20 + 2 * std20
        lower = sma20 - 2 * std20
        g["pct_b"] = (close - lower) / (upper - lower)
        g["price_vs_sma20_pct"] = (close - sma20) / sma20 * 100

        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
        rs = avg_gain / (avg_loss + 1e-9)
        g["rsi14"] = 100 - (100 / (1 + rs))

        g["value_traded"] = close * volume

        g["ret_5d_pct"] = close.pct_change(5) * 100
        g["fwd_ret_1d_pct"] = close.pct_change(1).shift(-1) * 100
        g["fwd_ret_3d_pct"] = close.pct_change(3).shift(-3) * 100

        def forward_max_high(n):
            return high.shift(-1).iloc[::-1].rolling(n, min_periods=1).max().iloc[::-1]

        for n in (3, 5, 10):
            fwd_high = forward_max_high(n)
            g[f"fwd_high_ret_{n}d_pct"] = (fwd_high - close) / close * 100
        pieces.append(g)

    df = pd.concat(pieces, ignore_index=True)
    print("Features computed (atr_pct14/pct_b/rsi14/price_vs_sma20_pct/value_traded/"
          "ret_5d_pct/fwd_ret_1d_pct/fwd_ret_3d_pct/fwd_high_ret_{3,5,10}d_pct).")
    return df


def add_gates(df: pd.DataFrame) -> pd.DataFrame:
    atr_p75 = df.groupby("date").apply(lambda d: cross_sectional_percentile(d, "atr_pct14", ATR_PERCENTILE))
    rsi_p10 = df.groupby("date").apply(lambda d: cross_sectional_percentile(d, "rsi14", RSI_PERCENTILE))
    dist_p10 = df.groupby("date").apply(lambda d: cross_sectional_percentile(d, "price_vs_sma20_pct", DIST_SMA20_PERCENTILE))
    liq_med = df.groupby("date").apply(lambda d: cross_sectional_percentile(d, "value_traded", LIQUIDITY_PERCENTILE))

    df["atr_p75_today"] = df["date"].map(atr_p75)
    df["rsi_p10_today"] = df["date"].map(rsi_p10)
    df["dist_p10_today"] = df["date"].map(dist_p10)
    df["liq_med_today"] = df["date"].map(liq_med)

    df["gate_momentum"] = (
        (df["pct_b"] >= PCT_B_MIN) & (df["ret_5d_pct"] > 0)
        & df["atr_pct14"].notna() & df["atr_p75_today"].notna()
        & (df["atr_pct14"] >= df["atr_p75_today"])
    )
    df["gate_bounce"] = (
        df["rsi14"].notna() & df["rsi_p10_today"].notna() & (df["rsi14"] <= df["rsi_p10_today"])
        & df["price_vs_sma20_pct"].notna() & df["dist_p10_today"].notna() & (df["price_vs_sma20_pct"] <= df["dist_p10_today"])
        & df["value_traded"].notna() & df["liq_med_today"].notna() & (df["value_traded"] >= df["liq_med_today"])
    )
    print(f"\nMOMENTUM gate-pass ticker-days: {df['gate_momentum'].sum():,}")
    print(f"BOUNCE gate-pass ticker-days:   {df['gate_bounce'].sum():,}")
    return df


def add_streak(df: pd.DataFrame, gate_col: str, out_col: str) -> pd.DataFrame:
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
    pieces = []
    for ticker, g in df.groupby("ticker", sort=False):
        pas = g[gate_col]
        grp = (pas != pas.shift()).cumsum()
        streak = pas.groupby(grp).cumcount() + 1
        streak = streak.where(pas, 0)
        pieces.append(streak)
    df[out_col] = pd.concat(pieces).reindex(df.index)
    return df


def load_ihsg_series() -> pd.DataFrame:
    ihsg = yf.Ticker("^JKSE").history(start="2024-07-01", end="2026-10-01")
    ihsg = ihsg.reset_index()
    ihsg["date"] = ihsg["Date"].dt.strftime("%Y-%m-%d")
    ihsg = ihsg.sort_values("date").reset_index(drop=True)
    close = ihsg["Close"]
    ihsg["sma20"] = close.rolling(20).mean()
    ihsg["ret_5d"] = close.pct_change(5) * 100
    ihsg["bearish_sideways_neg"] = (close < ihsg["sma20"]) & (ihsg["ret_5d"] < 0)
    # Same forward-return convention as the ticker panel -- lets us compare
    # a gate-pass ticker's forward move against the MARKET's own forward
    # move over the identical window (user request 2026-10-02: "cek
    # korelasi dengan return ihsg").
    ihsg["ihsg_fwd_ret_1d_pct"] = close.pct_change(1).shift(-1) * 100
    ihsg["ihsg_fwd_ret_3d_pct"] = close.pct_change(3).shift(-3) * 100
    return ihsg.set_index("date")


def add_cum_ret_during_streak(df: pd.DataFrame, streak_col: str, out_col: str) -> pd.DataFrame:
    """% change from the FIRST day of the current streak's close to today's
    close -- i.e. how far the ticker has already moved WHILE repeatedly
    re-triggering the gate (user request 2026-10-02: "kombinasikan dengan
    pola return-nya dia ketika ada repeating entrypagi"). streak==1 -> 0.0
    (start of streak IS today) by construction."""
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
    pieces = []
    for ticker, g in df.groupby("ticker", sort=False):
        close = g["close"].to_numpy()
        streak = g[streak_col].to_numpy()
        n = len(g)
        cum = np.full(n, np.nan)
        for i in range(n):
            s = streak[i]
            if s >= 1:
                start_idx = i - (int(s) - 1)
                if start_idx >= 0 and close[start_idx]:
                    cum[i] = (close[i] - close[start_idx]) / close[start_idx] * 100
        pieces.append(pd.Series(cum, index=g.index))
    df[out_col] = pd.concat(pieces).reindex(df.index)
    return df


def bucket_stats(label: str, sub: pd.DataFrame, ret_col: str):
    sub = sub.dropna(subset=[ret_col])
    n = len(sub)
    if n == 0:
        print(f"  {label:<14} n=0")
        return
    win = (sub[ret_col] > 0).mean() * 100
    mean = sub[ret_col].mean()
    median = sub[ret_col].median()
    print(f"  {label:<14} n={n:5d}  win%={win:5.1f}  mean={mean:+.2f}%  median={median:+.2f}%")


def streak_report(hits_subset: pd.DataFrame, streak_col: str, header: str):
    for ret_col, ret_label in [("fwd_ret_1d_pct", "FORWARD +1 TRADING DAY"), ("fwd_ret_3d_pct", "FORWARD +3 TRADING DAYS")]:
        print(f"\n{'=' * 70}\n{header} -- {ret_label} return by streak bucket\n{'=' * 70}")
        bucket_stats("Streak 1x (baru)", hits_subset[hits_subset[streak_col] == 1], ret_col)
        bucket_stats("Streak 2x", hits_subset[hits_subset[streak_col] == 2], ret_col)
        bucket_stats("Streak 3x", hits_subset[hits_subset[streak_col] == 3], ret_col)
        bucket_stats("Streak 4x", hits_subset[hits_subset[streak_col] == 4], ret_col)
        bucket_stats("Streak >=5x", hits_subset[hits_subset[streak_col] >= 5], ret_col)
        bucket_stats("ALL gate-pass (baseline)", hits_subset, ret_col)


def high_tail_report(sub: pd.DataFrame, header: str):
    print(f"\n{'=' * 70}\n{header}\n{'=' * 70}")
    for col, label in [
        ("fwd_high_ret_3d_pct", "Max HIGH reached, next 3 trading days"),
        ("fwd_high_ret_5d_pct", "Max HIGH reached, next 5 trading days"),
        ("fwd_high_ret_10d_pct", "Max HIGH reached, next 10 trading days"),
    ]:
        s = sub[col].dropna()
        if len(s) == 0:
            print(f"  {label:<40} n=0")
            continue
        q = s.quantile([0.10, 0.25, 0.50, 0.75, 0.90, 0.95])
        print(
            f"  {label:<40} n={len(s):5d}  mean={s.mean():+6.2f}%  median={q[0.50]:+6.2f}%  "
            f"p10={q[0.10]:+6.2f}%  p25={q[0.25]:+6.2f}%  p75={q[0.75]:+6.2f}%  "
            f"p90={q[0.90]:+6.2f}%  p95={q[0.95]:+6.2f}%  max={s.max():+6.2f}%  min={s.min():+6.2f}%"
        )


def reversal_report(hits_subset: pd.DataFrame, cum_col: str, header: str):
    """Does how far price ALREADY moved during the repeat-streak predict a
    flip (reversal) in forward direction, vs just continuing? Quartile-
    bucket cum_ret_during_streak (Q1=most decline/least-extended end of the
    distribution, Q4=most extension/least-decline end) and report forward
    return per bucket, plus the raw Pearson correlation as a single number
    (negative = mean-reversion signature, positive = momentum/continuation)."""
    print(f"\n{'=' * 70}\n{header}\n{'=' * 70}")
    for ret_col, ret_label in [("fwd_ret_1d_pct", "+1d"), ("fwd_ret_3d_pct", "+3d")]:
        sub = hits_subset.dropna(subset=[cum_col, ret_col])
        if len(sub) < 40:
            print(f"  [{ret_label}] n too small ({len(sub)}), skip.")
            continue
        corr = sub[cum_col].corr(sub[ret_col])
        print(f"  [{ret_label}] Pearson corr(cum_ret_during_streak, {ret_col}) = {corr:+.3f}  (n={len(sub)})")
        try:
            sub = sub.copy()
            sub["cum_q"] = pd.qcut(sub[cum_col], 4, labels=["Q1 lowest", "Q2", "Q3", "Q4 highest"], duplicates="drop")
            for q in sub["cum_q"].cat.categories:
                bucket_stats(f"  {q}", sub[sub["cum_q"] == q], ret_col)
        except ValueError as e:
            print(f"    (qcut failed: {e})")


def ihsg_correlation_report(hits_subset: pd.DataFrame, streak_col: str, header: str):
    """Pearson corr between the gate-pass ticker's forward return and
    IHSG's own forward return over the IDENTICAL window (how much of the
    signal is just market beta), plus market-EXCESS return (ticker fwd ret
    minus IHSG fwd ret) by streak bucket -- the earlier streak pattern,
    stripped of whatever the whole market was doing that window."""
    print(f"\n{'=' * 70}\n{header}\n{'=' * 70}")
    for ret_col, ihsg_col, label in [
        ("fwd_ret_1d_pct", "ihsg_fwd_ret_1d_pct", "+1d"),
        ("fwd_ret_3d_pct", "ihsg_fwd_ret_3d_pct", "+3d"),
    ]:
        sub = hits_subset.dropna(subset=[ret_col, ihsg_col])
        if len(sub) < 40:
            print(f"  [{label}] n too small ({len(sub)}), skip.")
            continue
        corr = sub[ret_col].corr(sub[ihsg_col])
        print(f"  [{label}] Pearson corr(ticker_fwd_ret, ihsg_fwd_ret) = {corr:+.3f}  (n={len(sub)})")

    hits_subset = hits_subset.copy()
    hits_subset["excess_ret_3d"] = hits_subset["fwd_ret_3d_pct"] - hits_subset["ihsg_fwd_ret_3d_pct"]
    print("\n  Market-EXCESS +3d return (ticker fwd ret minus IHSG fwd ret) by streak bucket:")
    bucket_stats("  Streak 1x", hits_subset[hits_subset[streak_col] == 1], "excess_ret_3d")
    bucket_stats("  Streak 2x", hits_subset[hits_subset[streak_col] == 2], "excess_ret_3d")
    bucket_stats("  Streak 3x", hits_subset[hits_subset[streak_col] == 3], "excess_ret_3d")
    bucket_stats("  Streak 4x", hits_subset[hits_subset[streak_col] == 4], "excess_ret_3d")
    bucket_stats("  Streak >=5x", hits_subset[hits_subset[streak_col] >= 5], "excess_ret_3d")
    bucket_stats("  ALL gate-pass", hits_subset, "excess_ret_3d")


def run_archetype(df: pd.DataFrame, gate_col: str, streak_col: str, regime_by_date: pd.Series, archetype_label: str, ihsg: pd.DataFrame = None, cum_col: str = None):
    print(f"\n\n{'#' * 78}\n# ARCHETYPE: {archetype_label}\n{'#' * 78}")
    hits = df[df[gate_col]].copy()
    print(f"\nStreak distribution among gate-pass days ({archetype_label}):")
    print(hits[streak_col].value_counts().sort_index().to_string())

    streak_report(hits, streak_col, f"[{archetype_label}] ALL REGIMES (2024-08-21 s/d 2026-09-29)")

    hits["ihsg_bearish_sideways_neg"] = hits["date"].map(regime_by_date)
    n_mapped = hits["ihsg_bearish_sideways_neg"].notna().sum()
    print(f"\nIHSG regime mapped for {n_mapped}/{len(hits)} gate-pass rows.")
    bearish_hits = hits[hits["ihsg_bearish_sideways_neg"] == True]
    bullish_hits = hits[hits["ihsg_bearish_sideways_neg"] == False]
    print(f"Split: {len(bearish_hits)} rows bearish/sideways-negative IHSG, {len(bullish_hits)} rows bullish/positive.")

    streak_report(bearish_hits, streak_col, f"[{archetype_label}] BEARISH/SIDEWAYS-NEGATIVE IHSG DAYS ONLY")
    streak_report(bullish_hits, streak_col, f"[{archetype_label}] BULLISH/POSITIVE IHSG DAYS ONLY (for comparison)")

    streak_gt2 = hits[hits[streak_col] > 2]
    streak_le2 = hits[hits[streak_col] <= 2]
    print(f"\nStreak>2x subset ({archetype_label}): n={len(streak_gt2)} gate-pass rows total.")
    high_tail_report(streak_gt2, f"[{archetype_label}] STREAK >2x -- ALL REGIMES -- forward-HIGH return tail")
    high_tail_report(bearish_hits[bearish_hits[streak_col] > 2], f"[{archetype_label}] STREAK >2x -- BEARISH/SIDEWAYS-NEGATIVE ONLY")
    high_tail_report(bullish_hits[bullish_hits[streak_col] > 2], f"[{archetype_label}] STREAK >2x -- BULLISH/POSITIVE ONLY")
    high_tail_report(streak_le2, f"[{archetype_label}] STREAK <=2x -- ALL REGIMES (baseline)")

    # --- user follow-up 2026-10-02: "cek gejala sebaliknya ... kombinasikan
    # dengan pola return-nya dia ketika ada repeating entrypagi, apakah
    # menunjukkan probability turun/naik. juga cek korelasi dengan return
    # ihsg." -- reversal-vs-continuation signature + market-beta check,
    # for the REPEATING (streak>=2) population specifically.
    repeat_hits = hits[hits[streak_col] >= 2].copy()
    repeat_hits["ihsg_fwd_ret_1d_pct"] = repeat_hits["date"].map(ihsg["ihsg_fwd_ret_1d_pct"])
    repeat_hits["ihsg_fwd_ret_3d_pct"] = repeat_hits["date"].map(ihsg["ihsg_fwd_ret_3d_pct"])

    reversal_report(repeat_hits, cum_col, f"[{archetype_label}] REPEATING (streak>=2x) -- cum-return-during-streak vs forward return (reversal check)")
    ihsg_correlation_report(repeat_hits, streak_col, f"[{archetype_label}] REPEATING (streak>=2x) -- correlation with IHSG forward return")


def main():
    df = load_panel()
    df = compute_features(df)
    df = add_gates(df)
    df = add_streak(df, "gate_momentum", "streak_momentum")
    df = add_streak(df, "gate_bounce", "streak_bounce")
    df = add_cum_ret_during_streak(df, "streak_momentum", "cum_ret_during_streak_momentum")
    df = add_cum_ret_during_streak(df, "streak_bounce", "cum_ret_during_streak_bounce")

    ihsg = load_ihsg_series()
    regime_by_date = ihsg["bearish_sideways_neg"]

    run_archetype(df, "gate_momentum", "streak_momentum", regime_by_date, "MOMENTUM-PUMP", ihsg, "cum_ret_during_streak_momentum")
    run_archetype(df, "gate_bounce", "streak_bounce", regime_by_date, "OVERSOLD-BOUNCE", ihsg, "cum_ret_during_streak_bounce")


if __name__ == "__main__":
    main()
