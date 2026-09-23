"""
MBSS v2 (user request 2026-09-23) -- realistic execution-aware win-rate
for the MACD-confirm pillar's (day, lane, tag) matrix: instead of pure
close-only-at-D12 (the validated default exit, see
research/macd_confirm_d1d5_tp_formula_2026_09_23.py), walk the REMAINING
window day-by-day from the entry day's own close and simulate what
actually happens if SL_PCT=-10% and the SHIPPED TP1/TP2
(TP_TABLE_BY_DAY_LANE_TAG, already live in engine/macd_confirm_pillar.py)
were used as real intraday exit triggers.

Each day in the remaining window, in order:
  1) if low <= SL price -> exit LOSS (-10%), stop
  2) elif high >= TP2 price -> exit WIN (TP2), stop
  3) elif high >= TP1 price -> exit WIN (TP1), stop  [only tracked
     separately for touch-rate; for the "TP1-based" win-rate variant,
     TP1 alone is the win trigger, not TP2]
  4) else continue to next day
If nothing triggers by D12, fall back to close-D12 sign (matches the
close-only baseline).

Reports 3 win-rate variants per cell: SL-only (no TP shortcut, just a
real stop), TP1-aware (TP1 counts as an early win), TP2-aware (TP2
counts as an early win) -- so the difference vs the pure close-only
win_remain in the prior research is visible directly.
"""
import numpy as np
import pandas as pd

import sys
sys.path.insert(0, ".")
from engine.macd_confirm_pillar import TP_TABLE_BY_DAY_LANE_TAG, TAG_THRESHOLDS, SL_PCT  # noqa: E402

RAW = "research/ohlcv_backtest_raw.csv"
SPLIT_DATE = "2025-09-10"
CONSISTENCY_THRESHOLD = 0.90
N_LB = 10
HOLD = 12
ATR_PCTL_FINAL = 0.70
LANES = ["Quality(D2)", "Core/Neutral", "HighRisk/Reward"]
TAGS = ["VALID", "STRONG", "VERY STRONG"]


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
        g[f"pct_above_{N_LB}"] = macd_line.gt(0).rolling(N_LB).mean()
        g["macd_slope_5d"] = macd_line - macd_line.shift(5)
        down_day = (daily_ret < 0).astype(int)
        three_down = (down_day & down_day.shift(1).fillna(0).astype(int) & down_day.shift(2).fillna(0).astype(int))
        g[f"no_3down_streak_{N_LB}"] = three_down.rolling(N_LB).max().eq(0)
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


def build_stage1_test():
    df = pd.read_csv(RAW)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
    df = compute_fields(df)

    base_pop = df[
        (df[f"pct_above_{N_LB}"] >= CONSISTENCY_THRESHOLD)
        & (df["macd_slope_5d"] >= 0)
        & (df[f"no_3down_streak_{N_LB}"])
    ].copy()
    split = pd.Timestamp(SPLIT_DATE)
    train_base = base_pop[base_pop["date"] < split]
    test_base = base_pop[base_pop["date"] >= split]

    atr_cap = train_base["atr14_pct"].quantile(ATR_PCTL_FINAL)
    deciles_liq = [train_base[train_base["atr14_pct"] <= atr_cap]["value_20d_avg"].quantile(q) for q in np.linspace(0, 1, 11)]
    lo_liq, hi_liq = deciles_liq[1], deciles_liq[6]
    stage1_pre = train_base[(train_base["atr14_pct"] <= atr_cap) & (train_base["value_20d_avg"] >= lo_liq) & (train_base["value_20d_avg"] <= hi_liq)]
    std_cap = stage1_pre["std_ret_10d"].quantile(0.80)

    def apply_stage1(d):
        return d[(d["atr14_pct"] <= atr_cap) & (d["value_20d_avg"] >= lo_liq) & (d["value_20d_avg"] <= hi_liq) & (d["std_ret_10d"] <= std_cap)]

    stage1_train_raw = apply_stage1(train_base)
    stage1_test_raw = apply_stage1(test_base)

    rsi_th = stage1_train_raw["rsi14"].quantile(0.90)
    ret_th = stage1_train_raw["signal_day_ret"].quantile(0.90)
    gap_th = stage1_train_raw["signal_day_gap_pct"].quantile(0.90)
    range_th = stage1_train_raw["signal_day_range_pct"].quantile(0.10)
    deciles_liq_full = [stage1_train_raw["value_20d_avg"].quantile(q) for q in np.linspace(0, 1, 11)]
    lo_d2, hi_d2 = deciles_liq_full[1], deciles_liq_full[2]

    def lane_of(d):
        flag_d2 = (d["value_20d_avg"] >= lo_d2) & (d["value_20d_avg"] < hi_d2)
        flag_risk = (d["rsi14"] >= rsi_th) | (d["signal_day_ret"] >= ret_th) | (d["signal_day_gap_pct"] >= gap_th) | (d["signal_day_range_pct"] <= range_th)
        lane = pd.Series("Core/Neutral", index=d.index)
        lane[flag_risk] = "HighRisk/Reward"
        lane[flag_d2] = "Quality(D2)"
        return lane

    stage1_test_raw = stage1_test_raw.reset_index(drop=True)
    stage1_test_raw["lane"] = lane_of(stage1_test_raw)
    return df, stage1_test_raw


def simulate_touch(high_a, low_a, close_a, start_idx, end_idx, entry, sl_price, tp1_price, tp2_price):
    """Walk start_idx..end_idx inclusive day-by-day. Returns dict with
    sl_hit (bool), tp1_touch (bool, TP1 reached before SL), tp2_touch
    (bool, TP2 reached before SL), and close_ret (D12 close fallback)."""
    sl_hit = False
    tp1_touch = False
    tp2_touch = False
    for i in range(start_idx, end_idx + 1):
        if low_a[i] <= sl_price:
            sl_hit = True
            break
        if high_a[i] >= tp2_price:
            tp2_touch = True
        if high_a[i] >= tp1_price:
            tp1_touch = True
    close_ret = (close_a[end_idx] - entry) / entry
    return dict(sl_hit=sl_hit, tp1_touch=tp1_touch, tp2_touch=tp2_touch, close_ret=close_ret)


def main():
    df, stage1_test_raw = build_stage1_test()

    rows = []
    for tkr, g in stage1_test_raw.groupby("ticker", sort=False):
        full = df[df["ticker"] == tkr].reset_index(drop=True)
        idx_map = full.index[full["date"].isin(g["date"])].tolist()
        close_a = full["close"].to_numpy()
        high_a = full["high"].to_numpy()
        low_a = full["low"].to_numpy()
        n = len(full)
        for li, fi in zip(g.index.tolist(), idx_map):
            if fi + HOLD >= n:
                continue
            entry_ref = close_a[fi]
            if not np.isfinite(entry_ref) or entry_ref <= 0:
                continue
            lane = g.loc[li, "lane"]
            for k in range(1, 6):
                day_idx = fi + (k - 1)
                if day_idx >= n:
                    continue
                price_k = close_a[day_idx]
                if not np.isfinite(price_k) or price_k <= 0:
                    continue
                ret_so_far = (price_k - entry_ref) / entry_ref
                t1, t2 = TAG_THRESHOLDS[k]
                if ret_so_far <= 0:
                    tag = "FADING"
                elif ret_so_far <= t1:
                    tag = "VALID"
                elif ret_so_far <= t2:
                    tag = "STRONG"
                else:
                    tag = "VERY STRONG"
                if tag == "FADING":
                    continue  # no TP shown for FADING in production, skip

                cell = TP_TABLE_BY_DAY_LANE_TAG.get(min(k, 5), {}).get(lane, {}).get(tag)
                if cell is None:
                    continue
                tp1_pct, tp2_pct = cell

                remaining_start = day_idx + 1
                remaining_end = fi + HOLD
                if remaining_start > remaining_end or remaining_end >= n:
                    continue

                sl_price = price_k * (1 - SL_PCT / 100)
                tp1_price = price_k * (1 + tp1_pct / 100)
                tp2_price = price_k * (1 + tp2_pct / 100)

                r = simulate_touch(high_a, low_a, close_a, remaining_start, remaining_end,
                                    price_k, sl_price, tp1_price, tp2_price)
                rows.append({"ticker": tkr, "day": k, "lane": lane, "tag": tag,
                             "tp1_pct": tp1_pct, "tp2_pct": tp2_pct, **r})

    res = pd.DataFrame(rows)
    print(f"Total (episode x day, non-FADING) rows: {len(res)}\n")

    print("=" * 118)
    print("Execution-aware win-rate per (Day, Lane, Tag): SL-only vs TP1-aware vs TP2-aware vs close-only baseline")
    print("=" * 118)
    for day in range(2, 6):
        for lane in LANES:
            for tag in TAGS:
                sub = res[(res["day"] == day) & (res["lane"] == lane) & (res["tag"] == tag)]
                if len(sub) < 15:
                    continue
                n = len(sub)
                sl_rate = sub["sl_hit"].mean()
                sl_ret = -0.10
                tp1_ret = (sub["tp1_pct"] / 100).to_numpy()
                tp2_ret = (sub["tp2_pct"] / 100).to_numpy()
                sl_hit = sub["sl_hit"].to_numpy()
                tp1_touch = sub["tp1_touch"].to_numpy()
                tp2_touch = sub["tp2_touch"].to_numpy()
                close_ret = sub["close_ret"].to_numpy()

                # SL-only win-rate: loss if SL hit, else close-D12 sign (no TP shortcut)
                win_sl_only = np.where(sl_hit, False, close_ret > 0).mean()
                ret_sl_only = np.where(sl_hit, sl_ret, close_ret)

                # TP1-aware: exit at TP1 if touched before SL, else SL if hit, else close-D12
                win_tp1 = np.where(sl_hit, False, np.where(tp1_touch, True, close_ret > 0)).mean()
                ret_tp1 = np.where(sl_hit, sl_ret, np.where(tp1_touch, tp1_ret, close_ret))

                # TP2-aware: exit at TP2 if touched before SL, else SL if hit, else close-D12
                win_tp2 = np.where(sl_hit, False, np.where(tp2_touch, True, close_ret > 0)).mean()
                ret_tp2 = np.where(sl_hit, sl_ret, np.where(tp2_touch, tp2_ret, close_ret))

                # pure close-only baseline (ignore SL/TP entirely, for reference)
                win_close_only = (close_ret > 0).mean()

                print(f"Day{day} {lane:16s} {tag:12s} n={n:4d}  SL-hit={sl_rate:.1%}\n"
                      f"    win:  SL-only={win_sl_only:.1%}  TP1-aware={win_tp1:.1%}  "
                      f"TP2-aware={win_tp2:.1%}  close-only={win_close_only:.1%}\n"
                      f"    mean: SL-only={ret_sl_only.mean():+.2%}  TP1-aware={ret_tp1.mean():+.2%}  "
                      f"TP2-aware={ret_tp2.mean():+.2%}  close-only={close_ret.mean():+.2%}\n"
                      f"    med:  SL-only={np.median(ret_sl_only):+.2%}  TP1-aware={np.median(ret_tp1):+.2%}  "
                      f"TP2-aware={np.median(ret_tp2):+.2%}  close-only={np.median(close_ret):+.2%}")
        print()


if __name__ == "__main__":
    main()
