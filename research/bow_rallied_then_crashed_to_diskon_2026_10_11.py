"""
2026-10-11 (audit follow-up): classify_path_condition() in
engine/buy_on_weakness.py has 3 validated path-dependence conditions
(rallied_then_faded -> ends NETRAL, calm_quiet, fading_naik_late -> still
NAIK) but NONE for a pick that rallied into NAIK territory and then kept
falling PAST netral into DISKON (drift <= -3%) -- even though the
module's own docstring cites "OKAS/CSIS/BEST/SMGA-style round-trips" as
the motivating observation, and OKAS/CSIS specifically round-tripped INTO
discount, not just back to neutral. This checks whether "rallied then
crashed to DISKON" is a distinct population from "sat in DISKON the whole
time, never rallied" -- same harness/panel/exit-mechanic as
bow_dynamic_conditional_stats_2026_10_10.py (reused, not reinvented) so
numbers are directly comparable to the already-validated conditions.

No look-ahead: peak_drift_so_far at obs_day only uses days up to and
including obs_day (days 1..obs_day since trigger); entry simulated at
obs_day+1's OPEN (same convention as the parent script).
"""
import numpy as np
import pandas as pd
import sys
sys.path.insert(0, r"C:\Users\asus\claude_jay\mbss_v2_project\research")
from ff_booster_validate_lanes_2026_10_07 import wilson_lb
from bow_dynamic_conditional_stats_2026_10_10 import (
    build_bow_episodes, assign_tier, simulate_from, RESOLUTION_WINDOW,
)

OBS_DAYS = [2, 3, 4, 5, 7, 10]
RALLIED_MIN_PEAK = 1.0     # matches RALLIED_THEN_FADED_MIN_PEAK (NAIK boundary)
DISKON_MAX = -3.0          # matches DRIFT_DISKON_MAX


def main():
    print("Building BOW episode population (reused from 2026-10-10 script)...")
    dfb, bow_ep_mask, n = build_bow_episodes()
    trigger_idxs = dfb.index[bow_ep_mask]
    print(f"Total BOW trigger episodes: {len(trigger_idxs)}")

    all_rows = []
    for obs_day in OBS_DAYS:
        for idx in trigger_idxs:
            ticker = dfb.at[idx, "ticker"]
            trigger_close = dfb.at[idx, "close"]
            tier = assign_tier(dfb.at[idx, "pctb"], dfb.at[idx, "cmf_20"])
            obs_idx = idx + obs_day
            if obs_idx >= n or dfb.at[obs_idx, "ticker"] != ticker or trigger_close <= 0:
                continue

            # peak drift reached at ANY day from trigger+1..obs_day (causal,
            # uses only what would genuinely be known by obs_day).
            closes_so_far = dfb["close"].to_numpy()[idx + 1: obs_idx + 1]
            if len(closes_so_far) == 0 or pd.isna(closes_so_far).all():
                continue
            drift_path = (closes_so_far - trigger_close) / trigger_close * 100
            peak_drift_so_far = float(np.nanmax(drift_path))

            obs_close = dfb.at[obs_idx, "close"]
            if pd.isna(obs_close):
                continue
            current_drift = (obs_close - trigger_close) / trigger_close * 100

            if current_drift > DISKON_MAX:
                continue  # only care about picks sitting in DISKON at obs_day

            rallied = peak_drift_so_far >= RALLIED_MIN_PEAK

            entry_idx = obs_idx + 1
            if entry_idx >= n or dfb.at[entry_idx, "ticker"] != ticker:
                continue
            entry_ref = dfb.at[entry_idx, "open"]
            res = simulate_from(dfb, n, entry_idx, ticker, entry_ref, max_days=RESOLUTION_WINDOW)
            if res is None:
                continue
            exit_kind, ret, days = res
            all_rows.append({
                "obs_day": obs_day, "tier": tier,
                "group": "rallied_to_diskon" if rallied else "diskon_never_rallied",
                "peak_drift_so_far": peak_drift_so_far, "current_drift": current_drift,
                "giveback": peak_drift_so_far - current_drift,
                "ret_pct": ret, "days_held": days, "exit_kind": exit_kind,
            })
        print(f"  obs_day={obs_day} done, rows so far={len(all_rows)}")

    grid = pd.DataFrame(all_rows)
    grid.to_pickle(r"C:\Users\asus\claude_jay\mbss_v2_project\research\.tmp_bow_rallied_to_diskon_2026_10_11.pkl")

    print("\n=== POOLED (all obs_days, all tiers): rallied_to_diskon vs diskon_never_rallied ===")
    print(f"{'group':22s}{'n':>6s}{'win%':>7s}{'wlb%':>7s}{'mean_ret%':>11s}{'mean_days':>10s}{'SL%':>6s}")
    for group in ["rallied_to_diskon", "diskon_never_rallied"]:
        sub = grid[grid.group == group]
        n_ = len(sub)
        if n_ == 0:
            print(f"{group:22s}{'(n=0)':>6s}")
            continue
        wins = (sub.ret_pct > 0).sum()
        sl_n = (sub.exit_kind == "SL").sum()
        print(f"{group:22s}{n_:>6d}{100*wins/n_:>7.1f}{100*wilson_lb(wins, n_):>7.1f}"
              f"{sub.ret_pct.mean():>11.2f}{sub.days_held.mean():>10.2f}{100*sl_n/n_:>6.1f}")

    print("\n=== BY obs_day ===")
    print(f"{'obs_day':8s}{'group':22s}{'n':>6s}{'win%':>7s}{'wlb%':>7s}{'mean_ret%':>11s}{'SL%':>6s}")
    for obs_day in OBS_DAYS:
        for group in ["rallied_to_diskon", "diskon_never_rallied"]:
            sub = grid[(grid.obs_day == obs_day) & (grid.group == group)]
            n_ = len(sub)
            if n_ == 0:
                continue
            wins = (sub.ret_pct > 0).sum()
            sl_n = (sub.exit_kind == "SL").sum()
            flag = "THIN" if n_ < 50 else ("BORDER" if n_ < 150 else "SOLID")
            print(f"{obs_day:<8d}{group:22s}{n_:>6d}{100*wins/n_:>7.1f}{100*wilson_lb(wins, n_):>7.1f}"
                  f"{sub.ret_pct.mean():>11.2f}{100*sl_n/n_:>6.1f}  {flag}")

    print("\n=== Sensitivity: does giveback MAGNITUDE matter within rallied_to_diskon? ===")
    rtd = grid[grid.group == "rallied_to_diskon"]
    for lo, hi, label in [(0, 5, "giveback 2-5pp"), (5, 10, "giveback 5-10pp"), (10, 1e9, "giveback >=10pp")]:
        sub = rtd[(rtd.giveback >= lo) & (rtd.giveback < hi)]
        n_ = len(sub)
        if n_ == 0:
            continue
        wins = (sub.ret_pct > 0).sum()
        print(f"  {label:18s} n={n_:5d} win={100*wins/n_:5.1f}% wlb={100*wilson_lb(wins, n_):5.1f}% mean_ret={sub.ret_pct.mean():+.2f}%")


if __name__ == "__main__":
    main()
