"""
MBSS v2 (user request 2026-09-23, careful re-pass) -- derive a research-
based TP1/TP2 FORMULA (not a single flat number) for the MACD-confirm
pillar, keyed by (day, lane, tag), using the "remaining ceiling from
TODAY's (updated) price" methodology from
research/macd_confirm_d1d5_remaining_ceiling_2026_09_23.py, plus a
lane-specific "already ran up too far" cutoff.

Per user instruction: inspect every small (day x lane x tag) bucket
carefully (report n, don't hide thin cells), use QUANTILES of the
remaining max-high-touch distribution (not just fixed discrete %
levels) so TP1/TP2 are precise per cell, and recompute win-rate from
the UPDATED/current price (i.e. win = P(remaining_close_ret > 0) from
day k's own close forward to D12), not from the original entry_ref
like the CURRENT WIN_RATE_TABLE in engine/macd_confirm_pillar.py does.

Definitions (same Stage-1 gate / lane / tag-threshold logic as the
production module and the prior two research scripts in this family --
do not re-derive, see macd_confirm_d1d5_remaining_ceiling_2026_09_23.py
docstring for the full trail):
  - TP1 = quantile of remaining max-high touch return at the point where
    ~65% of episodes touch it (empirical 35th percentile of the
    remaining-ceiling distribution)
  - TP2 = ~40% touch point (60th percentile)
  - win_remain = P(remaining_close_ret > 0), current-price basis
  - MIN_N = 15 per cell; below that the cell is reported but flagged
    THIN and a same-lane cross-day fallback is shown alongside it
"""
import numpy as np
import pandas as pd

RAW = "research/ohlcv_backtest_raw.csv"
SPLIT_DATE = "2025-09-10"
CONSISTENCY_THRESHOLD = 0.90
N_LB = 10
HOLD = 12
ATR_PCTL_FINAL = 0.70
MIN_N = 15

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


def build_episodes():
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

                remaining_start = day_idx + 1
                remaining_end = fi + HOLD
                if remaining_start > remaining_end or remaining_end >= n:
                    continue
                remaining_close_ret = (close_a[remaining_end] - price_k) / price_k
                window_high = high_a[remaining_start: remaining_end + 1]
                if window_high.size == 0:
                    continue
                max_high_ret = (np.nanmax(window_high) - price_k) / price_k

                rows.append({
                    "ticker": tkr, "day": k, "lane": lane, "tag": tag,
                    "ret_so_far": ret_so_far, "remaining_close_ret": remaining_close_ret,
                    "max_high_ret": max_high_ret,
                })
    return pd.DataFrame(rows)


def cell_stats(sub):
    n = len(sub)
    tp1 = sub["max_high_ret"].quantile(0.35)  # ~65% touch
    tp2 = sub["max_high_ret"].quantile(0.60)  # ~40% touch
    touch_tp1 = (sub["max_high_ret"] >= tp1).mean()
    touch_tp2 = (sub["max_high_ret"] >= tp2).mean()
    win_remain = (sub["remaining_close_ret"] > 0).mean()
    return dict(n=n, tp1=tp1, tp2=tp2, touch_tp1=touch_tp1, touch_tp2=touch_tp2,
                win_remain=win_remain, mean_remain=sub["remaining_close_ret"].mean(),
                med_remain=sub["remaining_close_ret"].median())


def main():
    res = build_episodes()
    print(f"Total (episode x day) rows: {len(res)}\n")

    print("=" * 110)
    print("FULL MATRIX: every (Day, Lane, Tag) cell, n reported, THIN flagged if n<{}".format(MIN_N))
    print("=" * 110)
    cells = {}
    for day in range(1, 6):
        for lane in LANES:
            for tag in TAGS:
                sub = res[(res["day"] == day) & (res["lane"] == lane) & (res["tag"] == tag)]
                if len(sub) == 0:
                    continue
                stats = cell_stats(sub)
                cells[(day, lane, tag)] = stats
                flag = "" if stats["n"] >= MIN_N else "  <-- THIN"
                print(f"Day{day} {lane:16s} {tag:12s} n={stats['n']:4d}  "
                      f"TP1={stats['tp1']:+.1%}(touch~{stats['touch_tp1']:.0%})  "
                      f"TP2={stats['tp2']:+.1%}(touch~{stats['touch_tp2']:.0%})  "
                      f"win_remain={stats['win_remain']:.1%}  mean_remain={stats['mean_remain']:+.1%}  "
                      f"med_remain={stats['med_remain']:+.1%}{flag}")
        print()

    print("=" * 110)
    print("FALLBACK POOL A: (Lane, Tag) pooled across ALL days 2-5 (for thin per-day cells)")
    print("=" * 110)
    fallback_a = {}
    for lane in LANES:
        for tag in TAGS:
            sub = res[(res["day"].between(2, 5)) & (res["lane"] == lane) & (res["tag"] == tag)]
            if len(sub) == 0:
                continue
            stats = cell_stats(sub)
            fallback_a[(lane, tag)] = stats
            print(f"{lane:16s} {tag:12s} n={stats['n']:4d}  "
                  f"TP1={stats['tp1']:+.1%}(touch~{stats['touch_tp1']:.0%})  "
                  f"TP2={stats['tp2']:+.1%}(touch~{stats['touch_tp2']:.0%})  "
                  f"win_remain={stats['win_remain']:.1%}  mean_remain={stats['mean_remain']:+.1%}  med_remain={stats['med_remain']:+.1%}")

    print()
    print("=" * 110)
    print("CHASE-RISK CUTOFF per (Day, Lane): top-quintile ret_so_far lower bound + its win_remain")
    print("(only material where top quintile clearly underperforms the rest -- see prior research: Quality(D2) only)")
    print("=" * 110)
    chase_cutoffs = {}
    for day in range(2, 6):
        for lane in LANES:
            sub = res[(res["day"] == day) & (res["lane"] == lane) & (res["tag"] != "FADING")].copy()
            if len(sub) < 40:
                continue
            try:
                sub["q"] = pd.qcut(sub["ret_so_far"], 5, labels=False, duplicates="drop")
            except ValueError:
                continue
            q4 = sub[sub["q"] == sub["q"].max()]
            rest = sub[sub["q"] != sub["q"].max()]
            cutoff = q4["ret_so_far"].min()
            win_q4 = (q4["remaining_close_ret"] > 0).mean()
            win_rest = (rest["remaining_close_ret"] > 0).mean()
            med_q4 = q4["remaining_close_ret"].median()
            degrade = win_q4 < win_rest - 0.08  # material underperformance threshold
            chase_cutoffs[(day, lane)] = dict(cutoff=cutoff, win_q4=win_q4, win_rest=win_rest, degrade=degrade)
            marker = "  <-- CHASE-RISK (top quintile clearly worse)" if degrade else ""
            print(f"Day{day} {lane:16s} top-quintile cutoff ret_so_far>={cutoff:+.1%}  "
                  f"win_q4={win_q4:.1%} vs win_rest={win_rest:.1%}  med_remain_q4={med_q4:+.1%}{marker}")

    print()
    print("=" * 110)
    print("PROPOSED FORMULA (per user request) -- printed as Python dict literals, ready to paste")
    print("=" * 110)
    print("\n# TP1/TP2 by (day, lane, tag) in %, rounded to nearest 0.5%, using per-day cell when n>={},".format(MIN_N))
    print("# else falling back to the (lane,tag) pooled-days-2-5 estimate. Day 1 has no tag (always FADING).")
    print("TP_TABLE_BY_DAY_LANE_TAG = {")
    for day in range(2, 6):
        print(f"    {day}: {{")
        for lane in LANES:
            print(f'        "{lane}": {{')
            for tag in TAGS:
                c = cells.get((day, lane, tag))
                if c is not None and c["n"] >= MIN_N:
                    tp1, tp2, src = c["tp1"], c["tp2"], "own"
                else:
                    fb = fallback_a.get((lane, tag))
                    tp1, tp2, src = (fb["tp1"], fb["tp2"], "fallback") if fb else (None, None, "MISSING")
                if tp1 is None:
                    print(f'            "{tag}": None,  # MISSING DATA')
                else:
                    print(f'            "{tag}": ({round(tp1*100/0.5)*0.5:.1f}, {round(tp2*100/0.5)*0.5:.1f}),  # {src}, n={c["n"] if c else fallback_a[(lane,tag)]["n"]}')
            print("        },")
        print("    },")
    print("}")

    print("\n# Updated WIN_RATE (current-price / remaining basis), same key structure:")
    print("WIN_RATE_TABLE_UPDATED = {")
    for day in range(2, 6):
        print(f"    {day}: {{")
        for lane in LANES:
            print(f'        "{lane}": {{')
            for tag in TAGS:
                c = cells.get((day, lane, tag))
                if c is not None and c["n"] >= MIN_N:
                    wr, src = c["win_remain"], "own"
                else:
                    fb = fallback_a.get((lane, tag))
                    wr, src = (fb["win_remain"], "fallback") if fb else (None, "MISSING")
                if wr is None:
                    print(f'            "{tag}": None,  # MISSING DATA')
                else:
                    print(f'            "{tag}": {wr*100:.1f},  # {src}')
            print("        },")
        print("    },")
    print("}")

    print("\n# Chase-risk cutoffs (ret_so_far %, day-specific) -- only wire where degrade=True:")
    for (day, lane), c in chase_cutoffs.items():
        if c["degrade"]:
            print(f"  Day{day} {lane}: ret_so_far >= {c['cutoff']*100:+.1f}%  (win drops {c['win_rest']*100:.0f}%->{c['win_q4']*100:.0f}%)")


if __name__ == "__main__":
    main()
