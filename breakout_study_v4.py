#!/usr/bin/env python3
"""
breakout_study.py

Purpose:
- Pull whitelist tickers (JII70/ISSI/etc) from the same public Gist style used by the bot.
- Download OHLCV from Yahoo Finance via yfinance.
- Learn which D-1/D-2/D-3 parameters tend to appear before daily Top Gainer / breakout moves.
- Build an empirical breakout formula, not a ticker whitelist.
- Produce:
  1) positive_breakout_samples.csv
  2) labelled_training_rows.csv
  3) breakout_threshold_formula.json
  4) top10_breakout_watch.csv

Termux setup:
  pkg update -y
  pkg install python -y
  pip install pandas numpy yfinance requests

Example:
  python breakout_study.py --start 2026-07-15 --index-key JII70 --future-window 3 --breakout-return 10

Notes:
- This is a research/screening helper, not an execution signal.
- Use /check TICKER the next morning to validate VWAP, Vol pace, and Active Breakout.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf

GIST_URL = "https://gist.githubusercontent.com/SeptiyanAndika/2941e872798cea3bfb2e550106b8ad28/raw/index-saham.json"


def fetch_whitelist(index_key: str = "JII70", fallback_key: str = "JII") -> List[str]:
    r = requests.get(GIST_URL, timeout=20)
    r.raise_for_status()
    data = r.json()
    tickers = data.get(index_key) or data.get(index_key.upper()) or []
    if not tickers and fallback_key:
        tickers = data.get(fallback_key) or []
    if not tickers:
        raise RuntimeError(f"No tickers found for index_key={index_key}. Available keys: {list(data.keys())[:20]}")
    tickers = sorted({str(t).upper().strip().replace('.JK', '') for t in tickers if str(t).strip()})
    return tickers


def download_ohlcv(tickers: List[str], start: str, end: str | None, batch_size: int = 40, pause_sec: float = 2.0) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    yf_symbols = [f"{t}.JK" for t in tickers]
    for i in range(0, len(yf_symbols), batch_size):
        batch_symbols = yf_symbols[i:i + batch_size]
        batch_tickers = tickers[i:i + batch_size]
        print(f"Downloading {i+1}-{min(i+batch_size, len(yf_symbols))}/{len(yf_symbols)}...")
        raw = yf.download(
            batch_symbols,
            start=start,
            end=end,
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        if raw is None or raw.empty:
            time.sleep(pause_sec)
            continue

        for t, sym in zip(batch_tickers, batch_symbols):
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if sym not in raw.columns.get_level_values(1):
                        continue
                    df = raw.xs(sym, axis=1, level=1).copy()
                else:
                    df = raw.copy()
                cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
                if len(cols) < 5:
                    continue
                df = df[cols].dropna()
                df.index = pd.to_datetime(df.index).tz_localize(None)
                if len(df) >= 30:
                    out[t] = df
            except Exception as e:
                print(f"  skip {t}: {e}")
        time.sleep(pause_sec)
    return out


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    line = ema_fast - ema_slow
    signal = line.ewm(span=9, adjust=False).mean()
    hist = line - signal
    return line, signal, hist


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = pd.Series(0.0, index=high.index)
    minus_dm = pd.Series(0.0, index=high.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move[(up_move > down_move) & (up_move > 0)]
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move[(down_move > up_move) & (down_move > 0)]
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


def cmf(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 20) -> pd.Series:
    mfm = ((close - low) - (high - close)) / (high - low + 1e-9)
    mfv = mfm * volume
    return mfv.rolling(period).sum() / (volume.rolling(period).sum() + 1e-9)


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = close.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    return (direction * volume).fillna(0).cumsum()


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ret_1d_pct"] = d["Close"].pct_change() * 100
    d["intraday_range_pct"] = (d["High"] - d["Low"]) / d["Close"].replace(0, np.nan) * 100
    d["close_pos_day"] = (d["Close"] - d["Low"]) / (d["High"] - d["Low"] + 1e-9)
    d["vol_ma20"] = d["Volume"].rolling(20).mean()
    d["vol_ratio_20"] = d["Volume"] / (d["vol_ma20"] + 1e-9)
    d["value_traded"] = d["Close"] * d["Volume"]
    d["high_10d"] = d["High"].rolling(10).max()
    d["high_20d"] = d["High"].rolling(20).max()
    d["dist_to_10d_high_pct"] = (d["high_10d"] - d["Close"]) / d["Close"].replace(0, np.nan) * 100
    d["dist_to_20d_high_pct"] = (d["high_20d"] - d["Close"]) / d["Close"].replace(0, np.nan) * 100
    d["range_10d_pct"] = (d["High"].rolling(10).max() - d["Low"].rolling(10).min()) / d["Close"].replace(0, np.nan) * 100
    d["sma20"] = d["Close"].rolling(20).mean()
    d["sma50"] = d["Close"].rolling(50).mean()
    d["price_vs_sma20_pct"] = (d["Close"] - d["sma20"]) / d["sma20"].replace(0, np.nan) * 100
    d["price_vs_sma50_pct"] = (d["Close"] - d["sma50"]) / d["sma50"].replace(0, np.nan) * 100
    d["rsi14"] = rsi(d["Close"])
    _, _, mh = macd(d["Close"])
    d["macd_hist"] = mh
    d["macd_bullish"] = (mh > 0).astype(int)
    d["adx14"] = adx(d["High"], d["Low"], d["Close"])
    d["cmf20"] = cmf(d["High"], d["Low"], d["Close"], d["Volume"])
    d["obv"] = obv(d["Close"], d["Volume"])
    d["obv_slope_5_pct"] = d["obv"].diff(5) / (d["obv"].abs().shift(5) + 1e-9) * 100
    d["consecutive_up"] = (d["Close"].diff() > 0).astype(int).groupby((d["Close"].diff() <= 0).cumsum()).cumsum()
    return d


FEATURES = [
    "vol_ratio_20", "value_traded", "dist_to_10d_high_pct", "dist_to_20d_high_pct",
    "range_10d_pct", "close_pos_day", "rsi14", "macd_hist", "macd_bullish",
    "adx14", "cmf20", "obv_slope_5_pct", "price_vs_sma20_pct", "price_vs_sma50_pct",
    "intraday_range_pct", "consecutive_up",
]

HIGHER_BETTER_DEFAULT = {
    "vol_ratio_20", "value_traded", "close_pos_day", "macd_hist", "macd_bullish",
    "adx14", "cmf20", "obv_slope_5_pct", "price_vs_sma20_pct", "price_vs_sma50_pct",
    "intraday_range_pct", "consecutive_up",
}
LOWER_BETTER_DEFAULT = {"dist_to_10d_high_pct", "dist_to_20d_high_pct"}
RANGE_FEATURES = {"rsi14", "range_10d_pct"}



def weighted_quantile(values, quantile, sample_weight=None):
    values = np.asarray(values, dtype=float)
    if sample_weight is None:
        return float(np.nanquantile(values, quantile))
    sample_weight = np.asarray(sample_weight, dtype=float)
    mask = ~np.isnan(values) & ~np.isnan(sample_weight)
    values = values[mask]
    sample_weight = sample_weight[mask]
    if len(values) == 0:
        return float("nan")
    sorter = np.argsort(values)
    values = values[sorter]
    sample_weight = sample_weight[sorter]
    weighted_cdf = np.cumsum(sample_weight) - 0.5 * sample_weight
    weighted_cdf /= np.sum(sample_weight)
    return float(np.interp(quantile, weighted_cdf, values))


def weighted_mean(values, weights):
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    mask = ~np.isnan(values) & ~np.isnan(weights)
    if mask.sum() == 0:
        return float("nan")
    return float(np.average(values[mask], weights=weights[mask]))


def make_labelled_rows(data: Dict[str, pd.DataFrame], start: str, future_window: int, breakout_return: float, min_price: float, min_value_traded: float) -> pd.DataFrame:
    rows = []
    start_dt = pd.Timestamp(start)
    for ticker, raw in data.items():
        df = add_features(raw)
        # future max return from D close to next N trading days close
        fut = pd.concat([(df["Close"].shift(-i) / df["Close"] - 1) * 100 for i in range(1, future_window + 1)], axis=1)
        df["future_max_ret_pct"] = fut.max(axis=1)
        df["future_breakout"] = (df["future_max_ret_pct"] >= breakout_return).astype(int)
        df["ticker"] = ticker
        df["date"] = df.index
        keep = df[(df["date"] >= start_dt) & (df["Close"] >= min_price) & (df["value_traded"] >= min_value_traded)].copy()
        rows.append(keep[["ticker", "date", "Open", "High", "Low", "Close", "Volume", "future_max_ret_pct", "future_breakout"] + FEATURES])
    if not rows:
        return pd.DataFrame()
    labelled = pd.concat(rows, ignore_index=True)
    labelled = labelled.replace([np.inf, -np.inf], np.nan).dropna(subset=FEATURES + ["future_max_ret_pct"])
    return labelled


def make_positive_samples_from_actual_breakout_days(data: Dict[str, pd.DataFrame], start: str, breakout_return: float, min_price: float, min_value_traded: float) -> pd.DataFrame:
    rows = []
    start_dt = pd.Timestamp(start)
    for ticker, raw in data.items():
        df = add_features(raw)
        df["breakout_day_ret_pct"] = df["Close"].pct_change() * 100
        df["value_traded"] = df["Close"] * df["Volume"]
        for idx in range(3, len(df)):
            date = df.index[idx]
            if date < start_dt:
                continue
            if df["breakout_day_ret_pct"].iloc[idx] < breakout_return:
                continue
            if df["Close"].iloc[idx] < min_price or df["value_traded"].iloc[idx] < min_value_traded:
                continue
            for lead in [1, 2, 3]:
                prior_idx = idx - lead
                if prior_idx < 0:
                    continue
                prior = df.iloc[prior_idx]
                row = {"ticker": ticker, "breakout_date": date, "lead_days": lead, "breakout_day_ret_pct": df["breakout_day_ret_pct"].iloc[idx]}
                row.update({f: prior.get(f) for f in FEATURES})
                row.update({"prior_close": prior["Close"], "prior_volume": prior["Volume"], "prior_date": df.index[prior_idx]})
                rows.append(row)
    out = pd.DataFrame(rows).replace([np.inf, -np.inf], np.nan)
    return out.dropna(subset=FEATURES) if not out.empty else out


def make_labelled_rows_topn(
    data: Dict[str, pd.DataFrame],
    start: str,
    top_n_per_day: int,
    lead_days: int,
    min_top_gainer_return: float,
    min_price: float,
    min_value_traded: float,
    post_break_risk_window: int = 3,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build labels from actual daily Top Gainers instead of an arbitrary future-return threshold.

    For each trading date >= start:
    - rank all eligible tickers by same-day return
    - take top N that also return at least min_top_gainer_return
    - label each ticker's D-1, D-2, ..., D-lead_days rows as positive samples

    This directly answers: "what did the best gainers look like 1-3 days before they ran?"
    """
    start_dt = pd.Timestamp(start)
    feats: Dict[str, pd.DataFrame] = {}
    daily_rows = []

    for ticker, raw in data.items():
        df = add_features(raw)
        df["ticker"] = ticker
        df["date"] = df.index
        df["value_traded"] = df["Close"] * df["Volume"]
        feats[ticker] = df
        tmp = df[(df["date"] >= start_dt) & (df["Close"] >= min_price) & (df["value_traded"] >= min_value_traded)].copy()
        if not tmp.empty:
            daily_rows.append(tmp[["ticker", "date", "Close", "Volume", "value_traded", "ret_1d_pct"]])

    if not daily_rows:
        return pd.DataFrame(), pd.DataFrame()

    daily = pd.concat(daily_rows, ignore_index=True).replace([np.inf, -np.inf], np.nan).dropna(subset=["ret_1d_pct"])

    top_events = []
    for date, g in daily.groupby("date"):
        top = g[g["ret_1d_pct"] >= min_top_gainer_return].sort_values("ret_1d_pct", ascending=False).head(top_n_per_day).copy()
        top["top_rank"] = range(1, len(top) + 1)
        for _, row in top.iterrows():
            # Weight top 1-10 more than top 11-30 while still keeping wider sample size.
            rank = int(row["top_rank"])
            rank_weight = 1.00 if rank <= 10 else (0.60 if rank <= 20 else 0.35)
            top_events.append({
                "ticker": row["ticker"],
                "breakout_date": row["date"],
                "top_rank": rank,
                "rank_weight": rank_weight,
                "breakout_day_ret_pct": float(row["ret_1d_pct"]),
                "breakout_close": float(row["Close"]),
                "breakout_value_traded": float(row["value_traded"]),
            })

    events = pd.DataFrame(top_events)
    if events.empty:
        return pd.DataFrame(), pd.DataFrame()

    positive_keys = set()
    positive_rows = []
    for _, ev in events.iterrows():
        ticker = ev["ticker"]
        df = feats.get(ticker)
        if df is None or df.empty:
            continue
        locs = np.where(df.index == pd.Timestamp(ev["breakout_date"]))[0]
        if len(locs) == 0:
            continue
        idx = int(locs[0])
        breakout_close = float(df["Close"].iloc[idx])
        breakout_high = float(df["High"].iloc[idx])
        future_slice = df.iloc[idx + 1: idx + 1 + post_break_risk_window]
        if future_slice.empty:
            post_break_close_drawdown_pct = 0.0
            post_break_high_entry_drawdown_pct = 0.0
        else:
            min_future_low = float(future_slice["Low"].min())
            post_break_close_drawdown_pct = (min_future_low / max(breakout_close, 1e-9) - 1) * 100
            post_break_high_entry_drawdown_pct = (min_future_low / max(breakout_high, 1e-9) - 1) * 100
        for lead in range(1, lead_days + 1):
            prior_idx = idx - lead
            if prior_idx < 0:
                continue
            prior = df.iloc[prior_idx]
            if prior["Close"] < min_price or prior["value_traded"] < min_value_traded:
                continue
            key = (ticker, pd.Timestamp(prior.name))
            positive_keys.add(key)
            row = {
                "ticker": ticker,
                "date": prior.name,
                "breakout_date": ev["breakout_date"],
                "lead_days": lead,
                "breakout_day_ret_pct": ev["breakout_day_ret_pct"],
                "top_rank": ev.get("top_rank"),
                "rank_weight": ev.get("rank_weight", 1.0),
                "post_break_close_drawdown_pct": post_break_close_drawdown_pct,
                "post_break_high_entry_drawdown_pct": post_break_high_entry_drawdown_pct,
                "safe_after_break": 1 if post_break_high_entry_drawdown_pct >= -6.0 else 0,
                "future_breakout": 1,
                "future_max_ret_pct": ev["breakout_day_ret_pct"],
                "Open": prior["Open"], "High": prior["High"], "Low": prior["Low"],
                "Close": prior["Close"], "Volume": prior["Volume"],
            }
            row.update({f: prior.get(f) for f in FEATURES})
            positive_rows.append(row)

    labelled_rows = []
    for ticker, df in feats.items():
        eligible = df[(df["date"] >= start_dt) & (df["Close"] >= min_price) & (df["value_traded"] >= min_value_traded)].copy()
        for idx, row in eligible.iterrows():
            out = {
                "ticker": ticker,
                "date": idx,
                "Open": row["Open"], "High": row["High"], "Low": row["Low"],
                "Close": row["Close"], "Volume": row["Volume"],
                "future_breakout": 1 if (ticker, pd.Timestamp(idx)) in positive_keys else 0,
                "future_max_ret_pct": np.nan,
                "sample_weight": 1.0,
            }
            out.update({f: row.get(f) for f in FEATURES})
            labelled_rows.append(out)

    labelled = pd.DataFrame(labelled_rows).replace([np.inf, -np.inf], np.nan).dropna(subset=FEATURES)
    positives = pd.DataFrame(positive_rows).replace([np.inf, -np.inf], np.nan).dropna(subset=FEATURES)
    if not positives.empty and not labelled.empty:
        pos_map = {(r["ticker"], pd.Timestamp(r["date"])): r for _, r in positives.iterrows()}
        def _weight_for(row):
            m = pos_map.get((row["ticker"], pd.Timestamp(row["date"])))
            return float(m.get("rank_weight", 1.0)) if m is not None else 1.0
        def _rank_for(row):
            m = pos_map.get((row["ticker"], pd.Timestamp(row["date"])))
            return int(m.get("top_rank")) if m is not None and not pd.isna(m.get("top_rank")) else None
        labelled["sample_weight"] = labelled.apply(_weight_for, axis=1)
        labelled["positive_top_rank"] = labelled.apply(_rank_for, axis=1)
    return labelled, positives


def learn_thresholds(labelled: pd.DataFrame, min_lift: float = 1.05) -> Dict:
    positives = labelled[labelled["future_breakout"] == 1]
    negatives = labelled[labelled["future_breakout"] == 0]
    weights_all = labelled.get("sample_weight", pd.Series(1.0, index=labelled.index)).fillna(1.0).astype(float)
    weights_pos = positives.get("sample_weight", pd.Series(1.0, index=positives.index)).fillna(1.0).astype(float)
    base_rate = weighted_mean(labelled["future_breakout"].astype(float), weights_all)
    rules = []
    for f in FEATURES:
        pos = positives[f].dropna()
        neg = negatives[f].dropna()
        if len(pos) < 5 or len(neg) < 10:
            continue
        pos_weights = weights_pos.loc[pos.index] if len(weights_pos) else None
        if f in RANGE_FEATURES:
            low = weighted_quantile(pos.values, 0.25, pos_weights.values if pos_weights is not None else None)
            high = weighted_quantile(pos.values, 0.75, pos_weights.values if pos_weights is not None else None)
            mask = labelled[f].between(low, high)
            direction = "between"
            threshold = [round(low, 4), round(high, 4)]
        elif f in LOWER_BETTER_DEFAULT:
            th = weighted_quantile(pos.values, 0.75, pos_weights.values if pos_weights is not None else None)
            mask = labelled[f] <= th
            direction = "<="
            threshold = round(th, 4)
        else:
            th = weighted_quantile(pos.values, 0.25, pos_weights.values if pos_weights is not None else None)
            mask = labelled[f] >= th
            direction = ">="
            threshold = round(th, 4)
        if mask.any():
            hit_rate = weighted_mean(labelled.loc[mask, "future_breakout"].astype(float), weights_all.loc[mask])
        else:
            hit_rate = 0
        coverage = float(mask.mean())
        lift = hit_rate / base_rate if base_rate and base_rate > 0 else 0
        # Keep practical core features even if lift is marginal; top30 creates noisy sample by design.
        if lift >= min_lift or f in ["vol_ratio_20", "dist_to_20d_high_pct", "close_pos_day", "cmf20", "adx14", "intraday_range_pct", "macd_hist"]:
            rules.append({
                "feature": f,
                "direction": direction,
                "threshold": threshold,
                "positive_weighted_median": round(float(weighted_quantile(pos.values, 0.50, pos_weights.values if pos_weights is not None else None)), 4),
                "positive_median": round(float(pos.median()), 4),
                "negative_median": round(float(neg.median()), 4),
                "hit_rate_if_rule_true": round(float(hit_rate), 4),
                "coverage": round(coverage, 4),
                "lift": round(float(lift), 4),
            })
    rules = sorted(rules, key=lambda r: (r["lift"], r["coverage"]), reverse=True)
    return {"base_rate": round(float(base_rate), 4), "positive_rows": int(len(positives)), "weighted_positive_rows": round(float(weights_pos.sum()), 2), "total_rows": int(len(labelled)), "rules": rules}


def learn_drop_risk_thresholds(positives: pd.DataFrame, safe_drawdown_pct: float = -6.0) -> Dict:
    """Learn which pre-breakout features are associated with lower post-break drop risk."""
    if positives.empty or "post_break_high_entry_drawdown_pct" not in positives.columns:
        return {"safe_drawdown_pct": safe_drawdown_pct, "rules": []}
    p = positives.copy()
    p["safe_after_break"] = (p["post_break_high_entry_drawdown_pct"] >= safe_drawdown_pct).astype(int)
    safe = p[p["safe_after_break"] == 1]
    risky = p[p["safe_after_break"] == 0]
    if len(safe) < 5 or len(risky) < 5:
        return {"safe_drawdown_pct": safe_drawdown_pct, "safe_rows": int(len(safe)), "risky_rows": int(len(risky)), "rules": []}
    weights = p.get("rank_weight", pd.Series(1.0, index=p.index)).fillna(1.0).astype(float)
    base_safe = weighted_mean(p["safe_after_break"].astype(float), weights)
    rules = []
    for f in FEATURES:
        sv = safe[f].dropna()
        rv = risky[f].dropna()
        if len(sv) < 5 or len(rv) < 5:
            continue
        sw = safe.get("rank_weight", pd.Series(1.0, index=safe.index)).loc[sv.index].fillna(1.0).astype(float)
        # Direction is selected by whether safe median is higher or lower than risky median.
        safe_med = weighted_quantile(sv.values, 0.50, sw.values)
        risky_med = float(rv.median())
        if f in RANGE_FEATURES:
            lo = weighted_quantile(sv.values, 0.25, sw.values)
            hi = weighted_quantile(sv.values, 0.75, sw.values)
            mask = p[f].between(lo, hi)
            direction = "between"
            threshold = [round(lo, 4), round(hi, 4)]
        elif safe_med >= risky_med:
            th = weighted_quantile(sv.values, 0.25, sw.values)
            mask = p[f] >= th
            direction = ">="
            threshold = round(th, 4)
        else:
            th = weighted_quantile(sv.values, 0.75, sw.values)
            mask = p[f] <= th
            direction = "<="
            threshold = round(th, 4)
        hit = weighted_mean(p.loc[mask, "safe_after_break"].astype(float), weights.loc[mask]) if mask.any() else 0
        lift = hit / base_safe if base_safe > 0 else 0
        if lift >= 1.03 or f in ["cmf20", "vol_ratio_20", "close_pos_day", "rsi14", "range_10d_pct"]:
            rules.append({
                "feature": f,
                "direction": direction,
                "threshold": threshold,
                "safe_weighted_median": round(float(safe_med), 4),
                "risky_median": round(float(risky_med), 4),
                "safe_rate_if_rule_true": round(float(hit), 4),
                "lift": round(float(lift), 4),
            })
    rules = sorted(rules, key=lambda r: r["lift"], reverse=True)
    return {"safe_drawdown_pct": safe_drawdown_pct, "safe_rows": int(len(safe)), "risky_rows": int(len(risky)), "base_safe_rate": round(float(base_safe), 4), "rules": rules}


def feature_score_percentile(row: pd.Series, feature: str, rule: Dict) -> Tuple[float, str | None]:
    """Continuous score 0-1 from learned rule instead of binary all-or-nothing."""
    val = row.get(feature)
    if pd.isna(val):
        return 0.0, None
    direction = rule["direction"]
    th = rule["threshold"]
    lift = float(rule.get("lift", 1.0))
    if direction == ">=":
        # good if above threshold; saturate at roughly 2x threshold where sensible
        denom = abs(float(th)) if abs(float(th)) > 1e-9 else 1.0
        raw = (float(val) - float(th)) / denom
        score = 0.45 + max(0.0, min(0.55, raw * 0.55)) if val >= th else max(0.0, 0.45 * (float(val) / max(float(th), 1e-9)))
        reason = f"{feature} {val:.2f} >= {th}" if val >= th else None
    elif direction == "<=":
        denom = abs(float(th)) if abs(float(th)) > 1e-9 else 1.0
        raw = (float(th) - float(val)) / denom
        score = 0.45 + max(0.0, min(0.55, raw * 0.55)) if val <= th else max(0.0, 0.45 * (float(th) / max(float(val), 1e-9)))
        reason = f"{feature} {val:.2f} <= {th}" if val <= th else None
    else:
        lo, hi = th
        mid = (lo + hi) / 2
        half = max((hi - lo) / 2, 1e-9)
        dist = abs(float(val) - mid) / half
        score = max(0.0, 1.0 - dist)
        reason = f"{feature} {val:.2f} in [{lo}, {hi}]" if lo <= val <= hi else None
    # cap lift effect so one noisy rule cannot dominate
    return max(0.0, min(1.0, score)) * min(1.35, max(0.75, lift / 1.5)), reason


def score_row(row: pd.Series, rules: List[Dict]) -> Tuple[int, List[str]]:
    """
    Empirical breakout score focused on formula discovery.
    Improvements vs v1:
    - continuous scoring, not identical binary 88/100 clusters
    - anti-late penalty for stocks that already broke out today
    - explicit guardrails for volume, proximity to high, and overheated RSI
    """
    # Core practical features first. These are the most interpretable for breakout detection.
    priority = [
        "vol_ratio_20", "dist_to_20d_high_pct", "dist_to_10d_high_pct", "close_pos_day",
        "cmf20", "adx14", "range_10d_pct", "macd_hist", "price_vs_sma20_pct",
        "intraday_range_pct", "obv_slope_5_pct", "rsi14",
    ]
    rules_by_feature = {r["feature"]: r for r in rules}
    used = [rules_by_feature[f] for f in priority if f in rules_by_feature]
    used += [r for r in rules if r["feature"] not in {u["feature"] for u in used}]
    used = used[:12]

    total_weight = 0.0
    total_score = 0.0
    reasons = []
    for rule in used:
        f = rule["feature"]
        weight = min(2.0, max(0.7, float(rule.get("lift", 1.0))))
        sc, reason = feature_score_percentile(row, f, rule)
        total_score += sc * weight
        total_weight += weight
        if reason:
            reasons.append(reason)
    base = (total_score / max(total_weight, 1e-9)) * 100

    # Guardrails: good formula should find pre-breakout, not chase post-breakout.
    ret_1d = row.get("ret_1d_pct", 0)
    rsi_val = row.get("rsi14", 0)
    dist20 = row.get("dist_to_20d_high_pct", 999)
    vol_ratio = row.get("vol_ratio_20", 0)
    cmf_val = row.get("cmf20", 0)

    penalty = 0
    if ret_1d >= 12:
        penalty += 25; reasons.append("penalty: sudah naik ekstrem hari ini")
    elif ret_1d >= 8:
        penalty += 15; reasons.append("penalty: sudah breakout hari ini")
    if rsi_val >= 82 and dist20 <= 1:
        penalty += 12; reasons.append("penalty: overheat dekat high")
    if vol_ratio < 0.8:
        penalty += 8; reasons.append("penalty: volume belum hidup")
    if cmf_val < -0.15:
        penalty += 10; reasons.append("penalty: CMF distribusi")

    final = int(max(0, min(100, base - penalty)))
    return final, reasons[:7]


def score_drop_risk(row: pd.Series, risk_rules: List[Dict]) -> Tuple[int, List[str]]:
    if not risk_rules:
        return 50, ["risk formula unavailable"]
    total = 0.0
    weight_sum = 0.0
    reasons = []
    for rule in risk_rules[:10]:
        f = rule["feature"]
        val = row.get(f)
        if pd.isna(val):
            continue
        ok = False
        th = rule["threshold"]
        if rule["direction"] == ">=": ok = val >= th
        elif rule["direction"] == "<=": ok = val <= th
        else:
            lo, hi = th; ok = lo <= val <= hi
        w = min(1.5, max(0.7, float(rule.get("lift", 1.0))))
        total += (1.0 if ok else 0.35) * w
        weight_sum += w
        if ok:
            reasons.append(f"{f} {rule['direction']} {th}")
    score = int(max(0, min(100, total / max(weight_sum, 1e-9) * 100)))
    return score, reasons[:4]


def build_top10(data: Dict[str, pd.DataFrame], formula: Dict, min_price: float, min_value_traded: float, top_n: int = 30) -> pd.DataFrame:
    latest_rows = []
    rules = formula.get("rules", [])
    risk_rules = formula.get("drop_risk_formula", {}).get("rules", [])
    for ticker, raw in data.items():
        df = add_features(raw).dropna(subset=FEATURES)
        if df.empty:
            continue
        last = df.iloc[-1].copy()
        if last["Close"] < min_price or last["Close"] * last["Volume"] < min_value_traded:
            continue
        score, reasons = score_row(last, rules)
        risk_score, risk_reasons = score_drop_risk(last, risk_rules)
        combined_score = int(round(score * 0.70 + risk_score * 0.30))
        latest_rows.append({
            "ticker": ticker,
            "date": df.index[-1].strftime("%Y-%m-%d"),
            "close": round(float(last["Close"]), 2),
            "breakout_similarity": score,
            "drop_risk_score": risk_score,
            "combined_breakout_score": combined_score,
            "reasons": "; ".join(reasons),
            "drop_risk_reasons": "; ".join(risk_reasons),
            "vol_ratio_20": round(float(last["vol_ratio_20"]), 2),
            "dist_to_20d_high_pct": round(float(last["dist_to_20d_high_pct"]), 2),
            "close_pos_day": round(float(last["close_pos_day"]), 2),
            "rsi14": round(float(last["rsi14"]), 2),
            "adx14": round(float(last["adx14"]), 2),
            "cmf20": round(float(last["cmf20"]), 3),
            "range_10d_pct": round(float(last["range_10d_pct"]), 2),
            "macd_hist": round(float(last["macd_hist"]), 4),
            "ret_1d_pct": round(float(last["ret_1d_pct"]), 2),
            "status_note": "ALREADY_EXTENDED" if (last["ret_1d_pct"] >= 8 or (last["dist_to_20d_high_pct"] <= 0.5 and last["rsi14"] >= 78)) else "SETUP_CANDIDATE",
        })
    out = pd.DataFrame(latest_rows)
    if out.empty:
        return out
    return out.sort_values(["combined_breakout_score", "breakout_similarity", "drop_risk_score"], ascending=False).head(top_n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-07-15", help="Start date for breakout labels, e.g. 2026-07-15")
    ap.add_argument("--end", default=None, help="End date exclusive for yfinance. Default: today/latest available")
    ap.add_argument("--warmup-days", type=int, default=70, help="Download extra days before start for indicators")
    ap.add_argument("--index-key", default="JII70", help="Whitelist key in Gist, e.g. JII70, JII, ISSI")
    ap.add_argument("--positive-mode", choices=["topn", "future"], default="topn", help="topn = use daily top gainers as positives; future = use future return threshold")
    ap.add_argument("--top-gainers-per-day", type=int, default=30, help="How many daily top gainers to use as breakout reference cases")
    ap.add_argument("--lead-days", type=int, default=3, help="Use D-1..D-N rows before each top gainer as positive samples")
    ap.add_argument("--min-top-gainer-return", type=float, default=4.0, help="Minimum same-day return for a stock to count as top gainer reference")
    ap.add_argument("--top-output", type=int, default=30, help="Number of candidates to output in top breakout watch")
    ap.add_argument("--post-break-risk-window", type=int, default=3, help="Days after breakout to measure worst drawdown risk")
    ap.add_argument("--safe-drawdown", type=float, default=-6.0, help="Safe max drop threshold from breakout-day high during risk window")
    ap.add_argument("--future-window", type=int, default=3, help="Lookahead days to classify future breakout, used only in --positive-mode future")
    ap.add_argument("--breakout-return", type=float, default=10.0, help="Future return threshold in percent, used for future mode and actual breakout samples")
    ap.add_argument("--min-price", type=float, default=55.0)
    ap.add_argument("--min-value-traded", type=float, default=1_000_000_000, help="Close*Volume minimum IDR-like value")
    ap.add_argument("--outdir", default="breakout_study_out")
    args = ap.parse_args()

    start_dt = pd.Timestamp(args.start)
    download_start = (start_dt - pd.Timedelta(days=args.warmup_days)).strftime("%Y-%m-%d")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching whitelist {args.index_key}...")
    tickers = fetch_whitelist(args.index_key)
    (outdir / "whitelist_tickers.txt").write_text("\n".join(tickers), encoding="utf-8")
    print(f"Whitelist tickers: {len(tickers)}")

    data = download_ohlcv(tickers, download_start, args.end)
    print(f"Downloaded usable tickers: {len(data)}")

    if args.positive_mode == "topn":
        labelled, positives = make_labelled_rows_topn(
            data=data,
            start=args.start,
            top_n_per_day=args.top_gainers_per_day,
            lead_days=args.lead_days,
            min_top_gainer_return=args.min_top_gainer_return,
            min_price=args.min_price,
            min_value_traded=args.min_value_traded,
            post_break_risk_window=args.post_break_risk_window,
        )
    else:
        labelled = make_labelled_rows(data, args.start, args.future_window, args.breakout_return, args.min_price, args.min_value_traded)
        positives = make_positive_samples_from_actual_breakout_days(data, args.start, args.breakout_return, args.min_price, args.min_value_traded)

    if labelled.empty:
        raise RuntimeError("No labelled rows. Try lowering --min-value-traded, lowering --min-top-gainer-return, or checking date range.")
    labelled.to_csv(outdir / "labelled_training_rows.csv", index=False)
    positives.to_csv(outdir / "positive_breakout_samples.csv", index=False)

    formula = learn_thresholds(labelled)
    formula["drop_risk_formula"] = learn_drop_risk_thresholds(positives, safe_drawdown_pct=args.safe_drawdown)
    formula.update({
        "start": args.start,
        "positive_mode": args.positive_mode,
        "top_gainers_per_day": args.top_gainers_per_day,
        "lead_days": args.lead_days,
        "min_top_gainer_return_pct": args.min_top_gainer_return,
        "top_output": args.top_output,
        "post_break_risk_window": args.post_break_risk_window,
        "safe_drawdown_pct": args.safe_drawdown,
        "future_window": args.future_window,
        "breakout_return_pct": args.breakout_return,
        "index_key": args.index_key,
        "min_price": args.min_price,
        "min_value_traded": args.min_value_traded,
        "generated_from_tickers": len(data),
    })
    (outdir / "breakout_threshold_formula.json").write_text(json.dumps(formula, indent=2, default=str), encoding="utf-8")

    top10 = build_top10(data, formula, args.min_price, args.min_value_traded, top_n=args.top_output)
    top10.to_csv(outdir / "top30_breakout_watch.csv", index=False)
    top10.to_csv(outdir / "top10_breakout_watch.csv", index=False)  # backward-compatible filename; may contain top_output rows

    print("\n=== BREAKOUT STUDY SUMMARY ===")
    print(f"Mode: {args.positive_mode} | TopN/day: {args.top_gainers_per_day} | Lead days: {args.lead_days} | Min gainer return: {args.min_top_gainer_return}%")
    print(f"Rows: {formula['total_rows']} | Positives: {formula['positive_rows']} | Base rate: {formula['base_rate']}")
    print("Top learned rules:")
    for r in formula["rules"][:12]:
        print(f"- {r['feature']} {r['direction']} {r['threshold']} | lift={r['lift']} | hit={r['hit_rate_if_rule_true']} | pos_med={r['positive_median']} neg_med={r['negative_median']}")
    print("\nTop 10 breakout watch:")
    if top10.empty:
        print("No candidates after filters.")
    else:
        print(top10.to_string(index=False))
    print(f"\nSaved outputs to: {outdir.resolve()}")


if __name__ == "__main__":
    main()
