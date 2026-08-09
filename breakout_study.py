#!/usr/bin/env python3
"""
breakout_study.py

Purpose:
- Pull whitelist tickers (JII70/ISSI/etc) from the same public Gist style used by the bot.
- Download OHLCV from Yahoo Finance via yfinance.
- Learn which D-1/D-2/D-3 parameters tend to appear before breakout moves.
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


def learn_thresholds(labelled: pd.DataFrame, min_lift: float = 1.15) -> Dict:
    positives = labelled[labelled["future_breakout"] == 1]
    negatives = labelled[labelled["future_breakout"] == 0]
    base_rate = len(positives) / max(len(labelled), 1)
    rules = []
    for f in FEATURES:
        pos = positives[f].dropna()
        neg = negatives[f].dropna()
        if len(pos) < 5 or len(neg) < 10:
            continue
        if f in RANGE_FEATURES:
            low = float(pos.quantile(0.25))
            high = float(pos.quantile(0.75))
            mask = labelled[f].between(low, high)
            direction = "between"
            threshold = [round(low, 4), round(high, 4)]
        elif f in LOWER_BETTER_DEFAULT:
            th = float(pos.quantile(0.75))
            mask = labelled[f] <= th
            direction = "<="
            threshold = round(th, 4)
        else:
            th = float(pos.quantile(0.25))
            mask = labelled[f] >= th
            direction = ">="
            threshold = round(th, 4)
        hit_rate = labelled.loc[mask, "future_breakout"].mean() if mask.any() else 0
        coverage = float(mask.mean())
        lift = hit_rate / base_rate if base_rate > 0 else 0
        if lift >= min_lift or f in ["vol_ratio_20", "dist_to_20d_high_pct", "close_pos_day", "cmf20", "adx14"]:
            rules.append({
                "feature": f,
                "direction": direction,
                "threshold": threshold,
                "positive_median": round(float(pos.median()), 4),
                "negative_median": round(float(neg.median()), 4),
                "hit_rate_if_rule_true": round(float(hit_rate), 4),
                "coverage": round(coverage, 4),
                "lift": round(float(lift), 4),
            })
    # sort by lift, but keep practical features visible
    rules = sorted(rules, key=lambda r: (r["lift"], r["coverage"]), reverse=True)
    return {"base_rate": round(float(base_rate), 4), "positive_rows": int(len(positives)), "total_rows": int(len(labelled)), "rules": rules}


def score_row(row: pd.Series, rules: List[Dict]) -> Tuple[int, List[str]]:
    score = 0
    reasons = []
    # Use top 10 rules, max 100 points.
    for rule in rules[:10]:
        f = rule["feature"]
        val = row.get(f)
        if pd.isna(val):
            continue
        ok = False
        if rule["direction"] == ">=":
            ok = val >= rule["threshold"]
        elif rule["direction"] == "<=":
            ok = val <= rule["threshold"]
        elif rule["direction"] == "between":
            lo, hi = rule["threshold"]
            ok = lo <= val <= hi
        if ok:
            pts = max(5, min(15, int(8 + rule.get("lift", 1) * 2)))
            score += pts
            reasons.append(f"{f} {rule['direction']} {rule['threshold']}")
    return int(min(score, 100)), reasons[:5]


def build_top10(data: Dict[str, pd.DataFrame], formula: Dict, min_price: float, min_value_traded: float) -> pd.DataFrame:
    latest_rows = []
    rules = formula.get("rules", [])
    for ticker, raw in data.items():
        df = add_features(raw).dropna(subset=FEATURES)
        if df.empty:
            continue
        last = df.iloc[-1].copy()
        if last["Close"] < min_price or last["Close"] * last["Volume"] < min_value_traded:
            continue
        score, reasons = score_row(last, rules)
        latest_rows.append({
            "ticker": ticker,
            "date": df.index[-1].strftime("%Y-%m-%d"),
            "close": round(float(last["Close"]), 2),
            "breakout_similarity": score,
            "reasons": "; ".join(reasons),
            "vol_ratio_20": round(float(last["vol_ratio_20"]), 2),
            "dist_to_20d_high_pct": round(float(last["dist_to_20d_high_pct"]), 2),
            "close_pos_day": round(float(last["close_pos_day"]), 2),
            "rsi14": round(float(last["rsi14"]), 2),
            "adx14": round(float(last["adx14"]), 2),
            "cmf20": round(float(last["cmf20"]), 3),
            "range_10d_pct": round(float(last["range_10d_pct"]), 2),
            "macd_hist": round(float(last["macd_hist"]), 4),
        })
    out = pd.DataFrame(latest_rows)
    if out.empty:
        return out
    return out.sort_values(["breakout_similarity", "vol_ratio_20"], ascending=False).head(10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-07-15", help="Start date for breakout labels, e.g. 2026-07-15")
    ap.add_argument("--end", default=None, help="End date exclusive for yfinance. Default: today/latest available")
    ap.add_argument("--warmup-days", type=int, default=70, help="Download extra days before start for indicators")
    ap.add_argument("--index-key", default="JII70", help="Whitelist key in Gist, e.g. JII70, JII, ISSI")
    ap.add_argument("--future-window", type=int, default=3, help="Lookahead days to classify future breakout")
    ap.add_argument("--breakout-return", type=float, default=10.0, help="Future return threshold in percent")
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

    labelled = make_labelled_rows(data, args.start, args.future_window, args.breakout_return, args.min_price, args.min_value_traded)
    if labelled.empty:
        raise RuntimeError("No labelled rows. Try lowering --min-value-traded or checking date range.")
    labelled.to_csv(outdir / "labelled_training_rows.csv", index=False)

    positives = make_positive_samples_from_actual_breakout_days(data, args.start, args.breakout_return, args.min_price, args.min_value_traded)
    positives.to_csv(outdir / "positive_breakout_samples.csv", index=False)

    formula = learn_thresholds(labelled)
    formula.update({
        "start": args.start,
        "future_window": args.future_window,
        "breakout_return_pct": args.breakout_return,
        "index_key": args.index_key,
        "min_price": args.min_price,
        "min_value_traded": args.min_value_traded,
        "generated_from_tickers": len(data),
    })
    (outdir / "breakout_threshold_formula.json").write_text(json.dumps(formula, indent=2, default=str), encoding="utf-8")

    top10 = build_top10(data, formula, args.min_price, args.min_value_traded)
    top10.to_csv(outdir / "top10_breakout_watch.csv", index=False)

    print("\n=== BREAKOUT STUDY SUMMARY ===")
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
