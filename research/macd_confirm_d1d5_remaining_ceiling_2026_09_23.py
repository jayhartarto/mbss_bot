"""
MBSS v2 (user request 2026-09-23) -- MACD-confirm pillar follow-up.

Question: if a trader enters FRESH on day k (k=1..5) of the D1-D5
conviction window (not on day 1), what remaining ceiling gain (from
THAT day's price forward to the D12 horizon) can realistically be
expected, per (day k, lane, tag_k) cell? This is what should drive a
truly DYNAMIC TP1/TP2 (current TP1_PCT_BY_TAG/TP2_PCT_BY_TAG in
engine/macd_confirm_pillar.py is a single flat number per tag, blended
across all entry days -- calibrated from the Day-5-tag x full-D1-D12-
window touch rate, which overstates remaining room for someone entering
late in the window).

Also: derive a research-backed "already run up too much, don't chase"
cutoff -- bin ret_so_far_k into quantiles within each (day, lane) cell
and check whether remaining forward return / remaining touch-rate
actually degrades as ret_so_far_k rises (vs. the CURRENT is_chasing_too_high
guard, which just reuses the flat TP2_PCT_BY_TAG number as a proxy without
directly testing whether remaining ceiling shrinks there).

Reuses the exact Stage-1 gate / lane assignment / tag-threshold logic from
research/macd_n10_sl_fairness_and_tp_targets_2026_09_22.py (same TEST
split, same thresholds) -- do not re-derive, see that file + memory
project_macd_n10_stage1_locked_final_2026_09_22.md for why these numbers.
"""
import numpy as np
import pandas as pd

RAW = "research/ohlcv_backtest_raw.csv"
SPLIT_DATE = "2025-09-10"
CONSISTENCY_THRESHOLD = 0.90
N_LB = 10
HOLD = 12
ATR_PCTL_FINAL = 0.70

TAG_THRESHOLDS = {
    1: (0.009, 0.022),
    2: (0.014, 0.036),
    3: (0.017, 0.045),
    4: (0.018, 0.052),
    5: (0.020, 0.057),
}
TP_LEVELS = [0.03, 0.05, 0.08, 0.10, 0.15, 0.20]


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


def main():
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

    # --- per-episode simulation: entry_ref = TRIGGER DAY close (matches
    # production evaluate_ticker: entry_ref_price = feats["close"] on the
    # Stage-1 trigger day itself = "Day 1"). For each k=1..5 compute
    # ret_so_far_k, tag_k, then the REMAINING forward window (day k+1
    # through day HOLD, all relative to trigger day) for touch-rates and
    # close-to-close remaining return. ---
    rows = []
    for tkr, g in stage1_test_raw.groupby("ticker", sort=False):
        full = df[df["ticker"] == tkr].reset_index(drop=True)
        idx_map = full.index[full["date"].isin(g["date"])].tolist()
        close_a = full["close"].to_numpy()
        high_a = full["high"].to_numpy()
        n = len(full)
        for li, fi in zip(g.index.tolist(), idx_map):
            if fi + HOLD >= n:
                continue
            entry_ref = close_a[fi]
            if not np.isfinite(entry_ref) or entry_ref <= 0:
                continue
            lane = g.loc[li, "lane"]
            for k in range(1, 6):
                day_idx = fi + (k - 1)  # k=1 -> trigger day itself
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

                remaining_start = day_idx + 1
                remaining_end = fi + HOLD  # inclusive close index, matches production ALERT_MAX_AGE_DAYS=12 from trigger day
                if remaining_start > remaining_end or remaining_end >= n:
                    continue
                remaining_close_ret = (close_a[remaining_end] - price_k) / price_k
                window_high = high_a[remaining_start: remaining_end + 1]
                if window_high.size == 0:
                    continue
                max_high_ret = (np.nanmax(window_high) - price_k) / price_k
                touch = {tp: bool(max_high_ret >= tp) for tp in TP_LEVELS}

                rows.append({
                    "ticker": tkr, "day": k, "lane": lane, "tag": tag,
                    "ret_so_far": ret_so_far, "remaining_close_ret": remaining_close_ret,
                    "max_high_ret": max_high_ret, **{f"touch_{tp}": touch[tp] for tp in TP_LEVELS},
                })

    res = pd.DataFrame(rows)
    print(f"Total (episode x day) rows: {len(res)}\n")

    print("=" * 100)
    print("PART 1: Remaining-ceiling matrix by (Day, Lane, Tag) -- forward from THAT day's close to D12")
    print("=" * 100)
    for day in range(1, 6):
        for lane in ["Quality(D2)", "Core/Neutral", "HighRisk/Reward"]:
            for tag in ["VALID", "STRONG", "VERY STRONG"]:
                sub = res[(res["day"] == day) & (res["lane"] == lane) & (res["tag"] == tag)]
                if len(sub) < 15:
                    continue
                win = (sub["remaining_close_ret"] > 0).mean()
                mean_r = sub["remaining_close_ret"].mean()
                med_r = sub["remaining_close_ret"].median()
                med_ceiling = sub["max_high_ret"].median()
                touch_str = "  ".join(f"{tp:.0%}:{sub[f'touch_{tp}'].mean():.0%}" for tp in TP_LEVELS)
                print(f"Day{day} {lane:16s} {tag:12s} n={len(sub):4d}  win(remain)={win:.1%}  "
                      f"mean_remain={mean_r:+.1%}  med_remain={med_r:+.1%}  med_ceiling={med_ceiling:+.1%}  touch: {touch_str}")
        print()

    print("=" * 100)
    print("PART 2: Does remaining ceiling degrade with ret_so_far (already-run-up) within (Day, Lane)?")
    print("Quintile of ret_so_far_k -> median remaining close-return / median remaining max-high ceiling")
    print("=" * 100)
    for day in [2, 3, 5]:
        for lane in ["Quality(D2)", "Core/Neutral", "HighRisk/Reward"]:
            sub = res[(res["day"] == day) & (res["lane"] == lane) & (res["tag"] != "FADING")].copy()
            if len(sub) < 40:
                continue
            try:
                sub["q"] = pd.qcut(sub["ret_so_far"], 5, labels=False, duplicates="drop")
            except ValueError:
                continue
            print(f"\nDay{day} {lane}:")
            for q, grp in sub.groupby("q"):
                lo, hi = grp["ret_so_far"].min(), grp["ret_so_far"].max()
                print(f"  Q{q} ret_so_far[{lo:+.1%},{hi:+.1%}] n={len(grp):4d}  "
                      f"med_remain_close={grp['remaining_close_ret'].median():+.1%}  "
                      f"med_remain_ceiling={grp['max_high_ret'].median():+.1%}  "
                      f"win(remain)={( grp['remaining_close_ret']>0).mean():.1%}")


if __name__ == "__main__":
    main()
