"""
MBSS v2 (user request 2026-09-23) -- for the CLEAN population that will
actually appear in /swing after today's two hide rules (FADING hidden
since commit c51b57a, Quality(D2) VERY STRONG hidden this session, see
project_macd_confirm_d1d5_tp_formula_rework_2026_09_23.md), compute:
  1) % kena SL (-10% from that day's own price, touched any day in the
     remaining window before D12)
  2) drawdown MAE (max adverse excursion -- the worst intraday dip
     within the remaining window, as % from that day's price, regardless
     of whether SL was actually hit)
  3) mean loss for LOSERS only at D12 close (close-only basis, matches
     the validated close-only exit -- among trades that closed negative
     at D12, what's the average size of that loss)
Reuses the same Stage-1/lane/tag logic as the sibling scripts in this
family (macd_confirm_d1d5_tp_formula_2026_09_23.py etc).
"""
import numpy as np
import pandas as pd

RAW = "research/ohlcv_backtest_raw.csv"
SPLIT_DATE = "2025-09-10"
CONSISTENCY_THRESHOLD = 0.90
N_LB = 10
HOLD = 12
ATR_PCTL_FINAL = 0.70
SL_PCT = 10.0

TAG_THRESHOLDS = {
    1: (0.009, 0.022),
    2: (0.014, 0.036),
    3: (0.017, 0.045),
    4: (0.018, 0.052),
    5: (0.020, 0.057),
}
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

                # CLEAN population only: drop FADING and Quality(D2) VERY STRONG
                # (the two hide rules now live in /swing's display filter)
                if tag == "FADING":
                    continue
                if lane == "Quality(D2)" and tag == "VERY STRONG":
                    continue

                remaining_start = day_idx + 1
                remaining_end = fi + HOLD
                if remaining_start > remaining_end or remaining_end >= n:
                    continue

                sl_price = price_k * (1 - SL_PCT / 100)
                window_low = low_a[remaining_start: remaining_end + 1]
                sl_hit = bool(np.any(window_low <= sl_price))
                mae_pct = (np.nanmin(window_low) - price_k) / price_k  # most negative = worst drawdown
                remaining_close_ret = (close_a[remaining_end] - price_k) / price_k

                rows.append({
                    "ticker": tkr, "day": k, "lane": lane, "tag": tag,
                    "sl_hit": sl_hit, "mae_pct": mae_pct, "remaining_close_ret": remaining_close_ret,
                })

    res = pd.DataFrame(rows)
    print(f"CLEAN population (non-FADING, excl. Quality(D2) VERY STRONG): n={len(res)}\n")

    print("=== OVERALL (all days, all lanes/tags in the clean population) ===")
    sl_rate = res["sl_hit"].mean()
    mae_mean = res["mae_pct"].mean()
    mae_median = res["mae_pct"].median()
    losers = res[res["remaining_close_ret"] < 0]
    win_rate = (res["remaining_close_ret"] > 0).mean()
    print(f"n={len(res)}  win(D12 close)={win_rate:.1%}  SL-hit rate={sl_rate:.1%}  "
          f"MAE mean={mae_mean:+.2%}  MAE median={mae_median:+.2%}")
    print(f"Losers only (close_ret<0): n={len(losers)} ({len(losers)/len(res):.1%} of population)  "
          f"mean loss={losers['remaining_close_ret'].mean():+.2%}  median loss={losers['remaining_close_ret'].median():+.2%}")

    print("\n=== Per Day ===")
    for day in range(1, 6):
        sub = res[res["day"] == day]
        if len(sub) == 0:
            continue
        losers_d = sub[sub["remaining_close_ret"] < 0]
        print(f"Day{day}: n={len(sub):5d}  win={( sub['remaining_close_ret']>0).mean():.1%}  "
              f"SL-hit={sub['sl_hit'].mean():.1%}  MAE mean={sub['mae_pct'].mean():+.2%}  "
              f"MAE median={sub['mae_pct'].median():+.2%}  "
              f"losers n={len(losers_d):4d} ({len(losers_d)/len(sub):.1%})  "
              f"mean loss={losers_d['remaining_close_ret'].mean():+.2%}  med loss={losers_d['remaining_close_ret'].median():+.2%}")

    print("\n=== Per Lane (all days blended) ===")
    for lane in LANES:
        sub = res[res["lane"] == lane]
        if len(sub) == 0:
            continue
        losers_l = sub[sub["remaining_close_ret"] < 0]
        print(f"{lane:16s}: n={len(sub):5d}  win={( sub['remaining_close_ret']>0).mean():.1%}  "
              f"SL-hit={sub['sl_hit'].mean():.1%}  MAE mean={sub['mae_pct'].mean():+.2%}  "
              f"MAE median={sub['mae_pct'].median():+.2%}  "
              f"losers n={len(losers_l):4d} ({len(losers_l)/len(sub):.1%})  "
              f"mean loss={losers_l['remaining_close_ret'].mean():+.2%}  med loss={losers_l['remaining_close_ret'].median():+.2%}")

    print("\n=== Per (Day, Lane, Tag) -- full clean-population breakdown ===")
    for day in range(1, 6):
        for lane in LANES:
            for tag in TAGS:
                sub = res[(res["day"] == day) & (res["lane"] == lane) & (res["tag"] == tag)]
                if len(sub) < 15:
                    continue
                losers_c = sub[sub["remaining_close_ret"] < 0]
                print(f"Day{day} {lane:16s} {tag:12s} n={len(sub):4d}  win={( sub['remaining_close_ret']>0).mean():.1%}  "
                      f"SL-hit={sub['sl_hit'].mean():.1%}  MAE mean={sub['mae_pct'].mean():+.2%}  "
                      f"losers n={len(losers_c):3d} mean loss={losers_c['remaining_close_ret'].mean():+.2%}")
        print()


if __name__ == "__main__":
    main()
