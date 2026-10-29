"""
2026-10-07 round 9 (self-contained, no cross-script imports to avoid
fragile coupling): compare ALL 6 lanes by CAPITAL EFFICIENCY -- mean
return PER DAY the capital is actually tied up, not just mean return per
trade. A lane returning +2% in 2 days redeploys 6x faster than one
returning +2% in 12 days; raw mean-return comparisons (used in every
earlier round) hide this. Every lane's gate/exit logic is reproduced
EXACTLY as already validated in prior rounds, but this time days_held
(the actual day count to TP/SL/expiry) is captured per trade -- none of
the earlier episode CSVs recorded it.

Each lane shown BASELINE (no FF filter) and WITH its validated FF
booster/exclusion applied (from the round-7/round-5 memory), so the
comparison reflects the actual recommended production behavior.

return_per_day% = mean(ret_pct) / mean(days_held).
annualized_simple% = return_per_day% * 252 -- an intuitive NON-compounded
scale reference ONLY. Explicitly NOT a forward guarantee: ignores
compounding, slippage, and whether a fresh signal is actually available
every single day (capital sitting idle between signals earns 0%, which
this simple ratio does not penalize).
"""
import numpy as np
import pandas as pd

from ff_booster_validate_lanes_2026_10_07 import wilson_lb, dedup_episodes


def wstats(sub, ret_col="ret_pct", days_col="days_held"):
    n = len(sub)
    if n == 0:
        return None
    wins = (sub[ret_col] > 0).sum()
    mean_days = sub[days_col].mean()
    mean_ret = sub[ret_col].mean()
    rpd = mean_ret / mean_days if mean_days else float("nan")
    return {
        "n": n, "win%": round(100 * wins / n, 1), "wlb%": round(100 * wilson_lb(wins, n), 1),
        "mean_ret%": round(mean_ret, 2), "mean_days": round(mean_days, 2),
        "ret_per_day%": round(rpd, 3), "annualized_simple%": round(rpd * 252, 1),
    }


def print_stats(label, s):
    if s is None:
        print(f"  {label:42s} n=0")
        return
    print(f"  {label:42s} n={s['n']:5d} win%={s['win%']:5.1f} wlb%={s['wlb%']:5.1f} "
          f"mean_ret%={s['mean_ret%']:+6.2f} mean_days={s['mean_days']:5.2f} "
          f"ret/day%={s['ret_per_day%']:+6.3f}  annualized(simple)%={s['annualized_simple%']:+8.1f}")


EP={}
def main():
    df = pd.read_pickle("research/.tmp_lane_validate_panel_v2.pkl")
    df = df.sort_values(["ticker", "date"]).reset_index(drop=True)
    n = len(df)
    g = df.groupby("ticker")
    close = df["close"]; high = df["high"]; low = df["low"]; open_ = df["open"]

    all_results = {}

    # ===================================================================
    # BOW -- SL-18%, TP2=band-touch, max 5 days
    # ===================================================================
    print("=== BOW ===")

    def compute_bow_peak_features(sub):
        hi = sub["high"].to_numpy(); vol = sub["volume"].to_numpy()
        vol_sma20 = sub["volume"].rolling(20).mean().to_numpy()
        vol_ratio = vol / vol_sma20
        nn = len(sub)
        pullback_depth = np.full(nn, np.nan); rally_pre40 = np.full(nn, np.nan)
        decline_vr = np.full(nn, np.nan); peak_price_arr = np.full(nn, np.nan)
        for i in range(20, nn):
            window = hi[i - 20:i]
            if np.all(np.isnan(window)):
                continue
            peak_rel = np.nanargmax(window); peak_loc = i - 20 + peak_rel
            peak_price = window[peak_rel]
            if peak_price <= 0 or np.isnan(peak_price):
                continue
            peak_price_arr[i] = peak_price
            pullback_depth[i] = (peak_price - sub["close"].to_numpy()[i]) / peak_price * 100
            pre_start = max(0, peak_loc - 40)
            pre_window = sub["close"].to_numpy()[pre_start:peak_loc]
            if len(pre_window) > 0:
                base_low = np.nanmin(pre_window)
                if base_low and base_low > 0:
                    rally_pre40[i] = (peak_price - base_low) / base_low * 100
            vr_window = vol_ratio[peak_loc:i + 1]
            if len(vr_window) > 0:
                decline_vr[i] = np.nanmean(vr_window)
        return pd.DataFrame({"pullback_depth_pct": pullback_depth, "rally_pct_pre40": rally_pre40,
                              "decline_formation_vr": decline_vr, "peak_price": peak_price_arr}, index=sub.index)

    pieces = [compute_bow_peak_features(sub) for _, sub in df.groupby("ticker", sort=False)]
    peak_feats = pd.concat(pieces).sort_index()
    dfb = df.join(peak_feats)
    sma100 = g["close"].transform(lambda s: s.rolling(100).mean())
    sma20 = g["close"].transform(lambda s: s.rolling(20).mean())
    std20 = g["close"].transform(lambda s: s.rolling(20).std())
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    pctb_b = (close - bb_lower) / (bb_upper - bb_lower)
    dfb["bb_upper"] = bb_upper
    sma12 = g["close"].transform(lambda s: s.rolling(12).mean())
    sma26 = g["close"].transform(lambda s: s.rolling(26).mean())
    macd_line_pct = (sma12 - sma26) / close * 100
    obv_dir = g["close"].transform(lambda s: np.sign(s.diff()).fillna(0))
    obv = (obv_dir * dfb["volume"]).groupby(dfb["ticker"]).cumsum()
    obv_slope = obv.groupby(dfb["ticker"]).transform(lambda s: s.pct_change(20) * 100)
    obv_bear = (dfb["stock_ret_20d"] > 0) & (obv_slope < 0)
    rs_20d = dfb["stock_ret_20d"] - dfb["ihsg_ret_20d"]
    bow_mask = (
        (close > sma100) & (pctb_b <= 0.35) & (dfb["decline_formation_vr"] < 0.6)
        & (dfb["rally_pct_pre40"] >= 5.0) & (dfb["pullback_depth_pct"] >= 8.0) & (dfb["pullback_depth_pct"] < 22.0)
        & (dfb["atr_pct_20d"] <= 6.0) & (dfb["cmf_20"] <= 0.20) & (~obv_bear)
        & (macd_line_pct <= 1.0)
        & (~((dfb["ff_pctvol_10d"].notna()) & (dfb["ff_pctvol_10d"] <= -10.0)))
        & (~((rs_20d.notna()) & (rs_20d > 10.0)))
    ).fillna(False)
    bow_ep_mask = dedup_episodes(bow_mask, dfb["ticker"])

    rows = []
    for idx in dfb.index[bow_ep_mask]:
        ticker = dfb.at[idx, "ticker"]; entry_ref = dfb.at[idx, "close"]
        sl_abs = entry_ref * 0.82
        exit_kind, ret, days = None, None, None
        for k in range(1, 6):
            j = idx + k
            if j >= n or dfb.at[j, "ticker"] != ticker:
                break
            band = dfb.at[j, "bb_upper"]
            if dfb.at[j, "low"] <= sl_abs:
                exit_kind, ret, days = "SL", (sl_abs - entry_ref) / entry_ref * 100, k; break
            if pd.notna(band) and dfb.at[j, "high"] >= band and band >= entry_ref:
                exit_kind, ret, days = "TP", (band - entry_ref) / entry_ref * 100, k; break
        if exit_kind is None:
            j5 = idx + 5
            if j5 < n and dfb.at[j5, "ticker"] == ticker:
                exit_kind, ret, days = "EXPIRED", (dfb.at[j5, "close"] - entry_ref) / entry_ref * 100, 5
        if exit_kind is None:
            continue
        rows.append({"ticker": ticker, "date": dfb.at[idx,"date"], "ret_pct": ret, "days_held": days, "net_ratio_z60": dfb.at[idx, "net_ratio_z60"]})
    bow_ep = pd.DataFrame(rows)
    all_results["BOW baseline"] = wstats(bow_ep)
    all_results["BOW +booster(z60>=1.0)"] = wstats(bow_ep[bow_ep["net_ratio_z60"] >= 1.0])
    print(f"n={len(bow_ep)}"); EP['BOW']=bow_ep

    # ===================================================================
    # OSB-v2 -- SL-20%, TP2=+10%, max 20 days
    # ===================================================================
    print("=== OSB-v2 ===")
    sma20_o = g["close"].transform(lambda s: s.rolling(20).mean())
    vs_sma20_pct = (close / sma20_o - 1) * 100
    ret_1d_pct = g["close"].transform(lambda s: s.pct_change() * 100)
    is_green = close > open_
    osb_mask = (
        (df["ihsg_dd100"] <= -10.0) & (df["rsi14"] >= 20.0) & (df["rsi14"] < 40.0)
        & (vs_sma20_pct <= -10.0) & (df["value_traded_20d"] >= 1e9) & is_green
        & (ret_1d_pct <= df["ihsg_ret_1d"])
    ).fillna(False)
    osb_ep_mask = dedup_episodes(osb_mask, df["ticker"])
    rows = []
    for idx in df.index[osb_ep_mask]:
        ticker = df.at[idx, "ticker"]; entry_ref = df.at[idx, "close"]
        sl_abs = entry_ref * 0.80; tp_abs = entry_ref * 1.10
        exit_kind, ret, days = None, None, None
        for k in range(1, 21):
            j = idx + k
            if j >= n or df.at[j, "ticker"] != ticker:
                break
            if df.at[j, "low"] <= sl_abs:
                exit_kind, ret, days = "SL", -20.0, k; break
            if df.at[j, "high"] >= tp_abs:
                exit_kind, ret, days = "TP", 10.0, k; break
        if exit_kind is None:
            j20 = idx + 20
            if j20 < n and df.at[j20, "ticker"] == ticker:
                exit_kind, ret, days = "EXPIRED", (df.at[j20, "close"] - entry_ref) / entry_ref * 100, 20
        if exit_kind is None:
            continue
        rows.append({"ticker": ticker, "date": df.at[idx,"date"], "ret_pct": ret, "days_held": days, "pos_days_5": df.at[idx, "pos_days_5"]})
    osb_ep = pd.DataFrame(rows)
    all_results["OSB-v2 baseline"] = wstats(osb_ep)
    all_results["OSB-v2 +booster(pos_days_5>=3)"] = wstats(osb_ep[osb_ep["pos_days_5"] >= 3])
    print(f"n={len(osb_ep)}"); EP['OSBv2']=osb_ep

    # ===================================================================
    # VCP -- SL-10%, TP1=+2.5%, max 12 days -- no FF filter recommended
    # ===================================================================
    print("=== VCP ===")
    sma_fast = g["close"].transform(lambda s: s.rolling(12).mean())
    sma_slow = g["close"].transform(lambda s: s.rolling(26).mean())
    macd_line = sma_fast - sma_slow
    tmp = df[["ticker"]].copy(); tmp["macd_line"] = macd_line
    signal_line = tmp.groupby("ticker")["macd_line"].transform(lambda s: s.ewm(span=9, adjust=False).mean())
    macd_hist_pct = (macd_line - signal_line) / close * 100
    r1 = g.apply(lambda x: (x["high"].rolling(15).max() - x["low"].rolling(15).min()) / x["close"]).reset_index(level=0, drop=True)
    r2 = g.apply(lambda x: (x["high"].rolling(10).max() - x["low"].rolling(10).min()) / x["close"]).reset_index(level=0, drop=True)
    r3 = g.apply(lambda x: (x["high"].rolling(5).max() - x["low"].rolling(5).min()) / x["close"]).reset_index(level=0, drop=True)
    vol5 = g["volume"].transform(lambda s: s.rolling(5).mean())
    vol15 = g["volume"].transform(lambda s: s.rolling(15).mean())
    high20 = g["high"].transform(lambda s: s.rolling(20).max())
    tightening = (r1 > r2) & (r2 > r3) & (r3 < r1 * 0.7)
    vol_dryup = vol5 < vol15
    near_high = (close - high20) / high20 > -0.10
    vcp_pass = tightening & vol_dryup & near_high
    vcp_mask = (
        vcp_pass & (df["rsi14"] >= 65.0) & (df["atr_pct_20d"] <= 10.0)
        & (df["value_traded_20d"] >= 0.15e9) & (df["value_traded_20d"] < 30e9)
        & (df["ff_pctvol_10d"] > 0) & (macd_hist_pct >= -0.5) & (macd_hist_pct < 2.75)
    ).fillna(False)
    vcp_ep_mask = dedup_episodes(vcp_mask, df["ticker"])
    rows = []
    for idx in df.index[vcp_ep_mask]:
        ticker = df.at[idx, "ticker"]; entry_ref = df.at[idx, "close"]
        sl_abs = entry_ref * 0.90; tp_abs = entry_ref * 1.025
        exit_kind, ret, days = None, None, None
        for k in range(1, 13):
            j = idx + k
            if j >= n or df.at[j, "ticker"] != ticker:
                break
            if df.at[j, "low"] <= sl_abs:
                exit_kind, ret, days = "SL", -10.0, k; break
            if df.at[j, "high"] >= tp_abs:
                exit_kind, ret, days = "TP", 2.5, k; break
        if exit_kind is None:
            j12 = idx + 12
            if j12 < n and df.at[j12, "ticker"] == ticker:
                exit_kind, ret, days = "EXPIRED", (df.at[j12, "close"] - entry_ref) / entry_ref * 100, 12
        if exit_kind is None:
            continue
        rows.append({"ticker": ticker, "date": df.at[idx,"date"], "ret_pct": ret, "days_held": days})
    vcp_ep = pd.DataFrame(rows)
    all_results["VCP baseline (no FF filter)"] = wstats(vcp_ep)
    print(f"n={len(vcp_ep)}"); EP['VCP']=vcp_ep

    # ===================================================================
    # MACD-confirm -- close-only exit at D12, FIXED 12-day hold always
    # ===================================================================
    print("=== MACD-confirm ===")
    daily_ret = g["close"].transform(lambda s: s.pct_change())
    sma12m = g["close"].transform(lambda s: s.rolling(12).mean())
    sma26m = g["close"].transform(lambda s: s.rolling(26).mean())
    macd_line_m = sma12m - sma26m
    tmp2 = df[["ticker"]].copy(); tmp2["macd_line"] = macd_line_m; tmp2["daily_ret"] = daily_ret
    pct_above_10d = tmp2.groupby("ticker")["macd_line"].transform(lambda s: s.gt(0).rolling(10).mean())
    macd_slope_5d = tmp2.groupby("ticker")["macd_line"].transform(lambda s: s - s.shift(5))
    down_day = (tmp2["daily_ret"] < 0).astype(int); tmp2["down_day"] = down_day
    three_down = tmp2.groupby("ticker")["down_day"].transform(lambda s: s & s.shift(1).fillna(0).astype(int) & s.shift(2).fillna(0).astype(int))
    tmp2["three_down"] = three_down
    no_3down = tmp2.groupby("ticker")["three_down"].transform(lambda s: s.rolling(10).max().eq(0))
    prev_close_m = g["close"].transform(lambda s: s.shift(1))
    tr_m = pd.concat([high - low, (high - prev_close_m).abs(), (low - prev_close_m).abs()], axis=1).max(axis=1)
    tmp2["tr"] = tr_m
    atr14_pct = tmp2.groupby("ticker")["tr"].transform(lambda s: s.ewm(alpha=1/14, adjust=False).mean()) / close * 100
    value_20d_avg = g.apply(lambda x: (x["close"] * x["volume"]).rolling(20, min_periods=20).mean()).reset_index(level=0, drop=True)
    tmp2["daily_ret_pct"] = tmp2["daily_ret"] * 100
    std_ret_10d = tmp2.groupby("ticker")["daily_ret_pct"].transform(lambda s: s.rolling(10).std())
    macd_gate = (
        (pct_above_10d >= 0.90) & (macd_slope_5d >= 0) & no_3down & (atr14_pct <= 5.78)
        & (value_20d_avg >= 19586155.0) & (value_20d_avg <= 1259645238.0) & (std_ret_10d <= 4.29)
    ).fillna(False)
    macd_ep_mask = dedup_episodes(macd_gate, df["ticker"])
    fwd_12d = g["close"].transform(lambda s: s.shift(-12) / s - 1) * 100
    macd_idx = df.index[macd_ep_mask]
    macd_df = df.loc[macd_idx, ["ticker", "date", "net_value_z20"]].copy()
    macd_df["ret_pct"] = fwd_12d.loc[macd_idx].values
    macd_df["days_held"] = 12
    macd_df = macd_df.dropna(subset=["ret_pct"])
    all_results["MACD-confirm baseline"] = wstats(macd_df)
    all_results["MACD-confirm (-exclusion z20<=-1.0, remainder)"] = wstats(macd_df[~(macd_df["net_value_z20"] <= -1.0).fillna(False)])
    print(f"n={len(macd_df)}"); EP['MACDc']=macd_df

    # ===================================================================
    # BSJP Lane1 -- TP1=2.16% cap, cut-if-red@D2, hold-to-D4
    # ===================================================================
    print("=== BSJP Lane1 ===")
    close_shift1 = g["close"].transform(lambda s: s.shift(1))
    gap_pct = (open_ - close_shift1) / close_shift1 * 100
    high252_d1 = g["high"].transform(lambda s: s.shift(1).rolling(252, min_periods=252).max())
    high252_d2 = g["high"].transform(lambda s: s.shift(2).rolling(252, min_periods=252).max())
    is_new_today = close >= high252_d1
    was_new_yday = close_shift1 >= high252_d2
    is_first_high252 = is_new_today & (~was_new_yday) & high252_d1.notna() & high252_d2.notna()
    sma12l = g["close"].transform(lambda s: s.rolling(12).mean())
    sma26l = g["close"].transform(lambda s: s.rolling(26).mean())
    macd_line_l = sma12l - sma26l
    tmp3 = df[["ticker"]].copy(); tmp3["macd_line"] = macd_line_l
    signal_line_l = tmp3.groupby("ticker")["macd_line"].transform(lambda s: s.ewm(span=9, adjust=False).mean())
    macd_hist_prior = (macd_line_l - signal_line_l).groupby(df["ticker"]).shift(1)
    liq_q25 = df.groupby("date")["value_traded_20d"].transform(lambda s: s.quantile(0.25))
    gap_leg = (gap_pct >= 1.0) & (gap_pct < 12.0) & (df["value_traded_20d"] >= liq_q25)
    union = gap_leg | is_first_high252
    watchlist_d0 = (union & macd_hist_prior.notna() & (macd_hist_prior >= 0) & gap_pct.notna()).fillna(False)
    watchlist_d0_shift = watchlist_d0.groupby(df["ticker"]).shift(1).fillna(False)
    ret_2d_cum = g["close"].transform(lambda s: s.pct_change(2)) * 100
    confirmed_d1 = (watchlist_d0_shift & (ret_2d_cum >= 2.0)).fillna(False)

    rows = []
    for idx in df.index[confirmed_d1]:
        ticker = df.at[idx, "ticker"]; entry_ref = df.at[idx, "close"]
        tp_level = entry_ref * 1.0216
        d2 = idx + 1
        if d2 >= n or df.at[d2, "ticker"] != ticker:
            continue
        exit_kind, ret, days = None, None, None
        if df.at[d2, "high"] >= tp_level:
            exit_kind, ret, days = "TP", 2.16, 1
        elif df.at[d2, "close"] < entry_ref:
            exit_kind, ret, days = "CUT", (df.at[d2, "close"] - entry_ref) / entry_ref * 100, 1
        else:
            for k in [2, 3]:
                j = idx + k
                if j >= n or df.at[j, "ticker"] != ticker:
                    break
                if df.at[j, "high"] >= tp_level:
                    exit_kind, ret, days = "TP", 2.16, k; break
            if exit_kind is None:
                j4 = idx + 3
                if j4 < n and df.at[j4, "ticker"] == ticker:
                    exit_kind, ret, days = "EXPIRED", (df.at[j4, "close"] - entry_ref) / entry_ref * 100, 3
        if exit_kind is None:
            continue
        rows.append({"ticker": ticker, "date": df.at[idx,"date"], "ret_pct": ret, "days_held": days, "net_value_z20": df.at[idx, "net_value_z20"]})
    lane1_ep = pd.DataFrame(rows)
    all_results["BSJP Lane1 baseline"] = wstats(lane1_ep)
    all_results["BSJP Lane1 +booster(z20>=0.5)"] = wstats(lane1_ep[lane1_ep["net_value_z20"] >= 0.5])
    print(f"n={len(lane1_ep)}"); EP['BSJP1']=lane1_ep

    # ===================================================================
    # BSJP Lane2 -- CLOSED, no filter, using daily-OHLCV-proxy population
    # ===================================================================
    print("=== BSJP Lane2 (daily-proxy, for reference -- CLOSED, no FF filter) ===")
    ret_1d_l2 = g["close"].transform(lambda s: s.pct_change()) * 100
    d0_gate = (ret_1d_l2 >= 8.0) & (df["value_traded_20d"] >= 0.15e9)
    d0_gate = d0_gate.fillna(False)
    close_pos_today = (close - low) / (high - low)
    rows = []
    for idx in df.index[d0_gate]:
        ticker = df.at[idx, "ticker"]; d1 = idx + 1
        if d1 >= n or df.at[d1, "ticker"] != ticker:
            continue
        ret1d_d1 = ret_1d_l2.at[d1]; cpos_d1 = close_pos_today.at[d1]
        if pd.isna(ret1d_d1) or pd.isna(cpos_d1) or not (ret1d_d1 >= 8.0 and cpos_d1 >= 0.8):
            continue
        entry_ref = df.at[d1, "close"]; sl_abs = entry_ref * 0.95; tp_abs = entry_ref * 1.05
        exit_kind, ret, days = None, None, None
        for k in [1, 2]:
            j = d1 + k
            if j >= n or df.at[j, "ticker"] != ticker:
                break
            if df.at[j, "low"] <= sl_abs:
                exit_kind, ret, days = "SL", -5.0, k; break
            if df.at[j, "high"] >= tp_abs:
                exit_kind, ret, days = "TP", 5.0, k; break
        if exit_kind is None:
            j3 = d1 + 2
            if j3 < n and df.at[j3, "ticker"] == ticker:
                exit_kind, ret, days = "EXPIRED", (df.at[j3, "close"] - entry_ref) / entry_ref * 100, 2
        if exit_kind is None:
            continue
        rows.append({"ticker": ticker, "date": df.at[idx,"date"], "ret_pct": ret, "days_held": days})
    lane2_ep = pd.DataFrame(rows)
    all_results["BSJP Lane2 (daily-proxy, no filter -- CLOSED)"] = wstats(lane2_ep)
    print(f"n={len(lane2_ep)}"); EP['BSJP2']=lane2_ep

    print("\n" + "=" * 130)
    print("CAPITAL EFFICIENCY COMPARISON -- return per day of capital tied up")
    print("=" * 130)
    for label, s in all_results.items():
        print_stats(label, s)


if __name__ == "__main__":
    main()
    import pickle;pickle.dump(EP,open("research/bull5/lane_episodes.pkl","wb"));print("saved",{k:len(v) for k,v in EP.items()})

if __name__=='__main__':
    pass
