"""
research/ipot_explore/mm_ceiling.py — Engine 7 of the IPOT research framework: the market-maker (MM) "ceiling" module.

User hypothesis (2026-10-22): identify the market maker in a stock and its average price, and treat that average as the CEILING of the mark-up that can be expected
once retail has exited / reduced. Everything below only MEASURES; nothing is assumed to work.

Run (project root):  python -m research.ipot_explore.mm_ceiling [--db research/ipot_dev.sqlite]

Pieces
  broker_window_profile()  per (ticker, date, broker) over a trailing window W (default 10 trading days):
        gross_share   (buy + sell) / (2 * traded value)            -> size of the broker in this stock
        presence      share of window days the broker is in the top-10 of either side
        two_frac      share of its present days on BOTH sides
        sym           value-weighted lot symmetry min/max on the two-sided days
        net_ratio     |sum of net_mid| / (sum buy + sum sell)       -> ~0 for a churner, ~1 for a one-way accumulator
        buy_vwap / sell_vwap / spread   its volume-weighted average prices (buy, sell) and sell/buy - 1
  mm_flag                  gross_share, presence, two_frac, sym, net_ratio and |spread| all inside thresholds (FIRST-GUESS thresholds, to be set from the
                           distributions BEFORE looking at outcomes; two-sidedness alone tracks broker size, see audit).
  retail_exit              net selling by the retail-app class (XL, XC, YP, PD ...) over the window, as a share of traded value.
  ceiling_table()          one row per (ticker, date) with the dominant MM candidate, its reference prices and the forward highs:
        up_vs_mm_buy_h   = max(high[t+1..t+h]) / mm_buy_vwap  - 1     (mark-up over the MM's cost)
        up_vs_mm_sell_h  = max(high[t+1..t+h]) / mm_sell_vwap - 1     (<0 means price never reached the MM's average sell)
        ceiling_hit_h    = max high >= mm_sell_vwap ; stall_h = close[t+h] / mm_sell_vwap - 1
  Forward windows start at t+1 because the summary is published after the close.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from research.ipot_explore import common, features

WINDOW = 10
MIN_OBS = 5
CP_MIN_OBS = 7
RETAIL_CLASSES = {"retail_app"}
CP_THRESHOLDS = {"gross_share": 0.04, "presence": 0.70, "cp_corr": -0.30}   # counterparty rule: non-retail broker moving AGAINST the retail net flow
MM_THRESHOLDS = {"gross_share": 0.04, "presence": 0.70, "two_frac": 0.70, "sym": 0.55, "net_ratio": 0.25, "abs_spread": 0.015}


def broker_window_profile(raw: pd.DataFrame, px: pd.DataFrame, window: int = WINDOW) -> pd.DataFrame:
    a = features.broker_day_table(raw, "all", "all")
    if a.empty:
        return pd.DataFrame()
    a["sym_row"] = np.where(a["is_two"], np.minimum(a["buy_lot"], a["sell_lot"]) / np.maximum(a["buy_lot"], a["sell_lot"]), 0.0)
    pxv = px.set_index(["ticker", "date"])["value"]
    frames = []
    for tk, d in a.groupby("ticker"):
        idx = pd.DatetimeIndex(sorted(d["date"].unique()))
        if len(idx) < MIN_OBS or tk not in pxv.index.get_level_values(0):
            continue
        val = pxv.loc[tk].reindex(idx)

        def piv(col, agg="sum"):
            return d.pivot_table(index="date", columns="broker", values=col, aggfunc=agg).reindex(idx)

        bv, sv = piv("buy_val").fillna(0.0), piv("sell_val").fillna(0.0)
        bl, sl = piv("buy_lot").fillna(0.0), piv("sell_lot").fillna(0.0)
        net = piv("net_val_mid").fillna(0.0)
        two = piv("is_two", "max").fillna(0.0)
        symsum = piv("sym_row").fillna(0.0)
        present = ((bv > 0) | (sv > 0)).astype(float)

        def R(x):
            return x.rolling(window, min_periods=MIN_OBS).sum()

        n_obs = pd.Series(1.0, index=idx).rolling(window, min_periods=MIN_OBS).sum()
        gross_val = R(bv) + R(sv)
        retail_cols = [c for c in net.columns if common.BROKER_CLASS.get(c) in RETAIL_CLASSES]
        retail_series = (net[retail_cols].sum(axis=1) if retail_cols else pd.Series(0.0, index=idx)).div(val)
        net_share = net.div(val, axis=0)
        cp_corr = net_share.rolling(window, min_periods=CP_MIN_OBS).corr(retail_series)
        prof = {
            "gross_share": gross_val.div(2.0 * R(val.to_frame("v"))["v"].replace(0, np.nan), axis=0),
            "presence": R(present).div(n_obs, axis=0),
            "two_frac": R(two) / R(present).replace(0, np.nan),
            "sym": R(symsum) / R(two).replace(0, np.nan),
            "net_ratio": R(net).abs() / gross_val.replace(0, np.nan),
            "net_mid_sum": R(net),
            "cp_corr": cp_corr,
            "buy_vwap": R(bv) / (R(bl) * 100.0).replace(0, np.nan),
            "sell_vwap": R(sv) / (R(sl) * 100.0).replace(0, np.nan),
        }
        stacked = pd.concat({k: v.stack(future_stack=True) for k, v in prof.items()}, axis=1)
        stacked.index.names = ["date", "broker"]
        stacked["ticker"] = tk
        stacked["window_value"] = R(val.to_frame("v"))["v"].reindex(stacked.index.get_level_values(0)).to_numpy()
        frames.append(stacked.reset_index())
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out[out["gross_share"].notna() & (out["gross_share"] > 0)].copy()
    out["spread"] = out["sell_vwap"] / out["buy_vwap"] - 1
    out["cls"] = out["broker"].map(common.BROKER_CLASS).fillna("other")
    return out


def flag_mm(prof: pd.DataFrame, th: dict | None = None) -> pd.DataFrame:
    th = th or MM_THRESHOLDS
    prof = prof.copy()
    prof["mm_flag"] = ((prof["gross_share"] >= th["gross_share"]) & (prof["presence"] >= th["presence"]) & (prof["two_frac"] >= th["two_frac"])
                       & (prof["sym"] >= th["sym"]) & (prof["net_ratio"] <= th["net_ratio"]) & (prof["spread"].abs() <= th["abs_spread"]))
    prof["cp_flag"] = ((~prof["cls"].isin(RETAIL_CLASSES)) & (prof["gross_share"] >= CP_THRESHOLDS["gross_share"])
                       & (prof["presence"] >= CP_THRESHOLDS["presence"]) & (prof["cp_corr"] <= CP_THRESHOLDS["cp_corr"]))
    prof["cp_score"] = (-prof["cp_corr"]).clip(lower=0) * prof["gross_share"]
    return prof


def retail_exit(prof: pd.DataFrame) -> pd.DataFrame:
    """Per (ticker, date): net selling of the retail-app class over the window as a share of the window's traded value (negative = retail net SELLING)."""
    r = prof[prof["cls"].isin(RETAIL_CLASSES)]
    g = r.groupby(["ticker", "date"]).agg(retail_net=("net_mid_sum", "sum"), window_value=("window_value", "first")).reset_index()
    g["retail_net_share"] = g["retail_net"] / g["window_value"]
    return g[["ticker", "date", "retail_net_share"]]


def _forward_prices(px: pd.DataFrame, horizons=(5, 10)) -> pd.DataFrame:
    p = px.sort_values(["ticker", "date"]).copy()
    g = p.groupby("ticker")
    for h in horizons:
        p[f"maxhigh_{h}d"] = pd.concat([g["high"].shift(-k) for k in range(1, h + 1)], axis=1).max(axis=1)
        p[f"close_{h}d"] = g["close"].shift(-h)
    return p


def ceiling_table(prof: pd.DataFrame, px: pd.DataFrame, horizons=(5, 10), mode: str = "symmetric") -> pd.DataFrame:
    """mode='symmetric': the dominant broker passing the two-sided / small-net / small-spread rule (mm_flag).
    mode='counterparty': the non-retail broker with the highest cp_score among cp_flag (moves against the retail net flow)."""
    col, key = ("mm_flag", "gross_share") if mode == "symmetric" else ("cp_flag", "cp_score")
    flagged = prof[prof[col]].sort_values(["ticker", "date", key], ascending=[True, True, False])
    top = flagged.groupby(["ticker", "date"]).head(1)[["ticker", "date", "broker", "gross_share", "buy_vwap", "sell_vwap", "spread", "sym", "net_ratio"]]
    top = top.rename(columns={"broker": "mm_broker", "buy_vwap": "mm_buy_vwap", "sell_vwap": "mm_sell_vwap", "spread": "mm_spread",
                              "gross_share": "mm_gross_share"})
    fwd = _forward_prices(px, horizons)
    t = top.merge(fwd[["ticker", "date", "close"] + [c for c in fwd.columns if c.startswith(("maxhigh_", "close_"))]], on=["ticker", "date"], how="left")
    for h in horizons:
        t[f"up_vs_close_{h}d"] = t[f"maxhigh_{h}d"] / t["close"] - 1
        t[f"up_vs_mm_buy_{h}d"] = t[f"maxhigh_{h}d"] / t["mm_buy_vwap"] - 1
        t[f"up_vs_mm_sell_{h}d"] = t[f"maxhigh_{h}d"] / t["mm_sell_vwap"] - 1
        t[f"ceiling_hit_{h}d"] = (t[f"maxhigh_{h}d"] >= t["mm_sell_vwap"]).where(t[f"maxhigh_{h}d"].notna())
        t[f"stall_{h}d"] = t[f"close_{h}d"] / t["mm_sell_vwap"] - 1
    t["ref_mode"] = mode
    t["close_vs_mm_sell"] = t["close"] / t["mm_sell_vwap"] - 1
    t["close_vs_mm_buy"] = t["close"] / t["mm_buy_vwap"] - 1
    t = t.merge(retail_exit(prof), on=["ticker", "date"], how="left")
    return t


def build(db_path: str = common.IPOT_DB, th: dict | None = None):
    raw = common.load_ipot(db_path)
    px = common.load_ohlcv(tickers=sorted(raw["ticker"].unique()), start="2025-01-01")
    prof = flag_mm(broker_window_profile(raw, px), th)
    return prof, ceiling_table(prof, px)


def placebo_other_brokers(prof: pd.DataFrame, ct: pd.DataFrame, px: pd.DataFrame, horizon: int = 5, max_dist: float = 0.10,
                          min_share: float = 0.02, B: int = 200, seed: int = 11) -> pd.DataFrame:
    """Is the reference broker's average SELL price special, or does ANY sizeable broker's average sell behave the same on that stock-day?
    Candidates = every broker with gross_share >= min_share whose window sell VWAP sits above the close by 0 < d0 <= max_dist (so a price move could reach it).
    Real = the reference broker's own row. Placebo = one candidate drawn at random among the SAME ticker-date's candidates (same stock, same volatility, same window),
    repeated B times. Compare P(max high of the next `horizon` days >= that sell VWAP), overall and by distance bucket. A real ceiling => the reference broker's
    hit rate is clearly LOWER than the random-candidate rate (price stalls at its average sell). (An earlier placebo that shuffled distances across tickers was
    WRONG: it broke the link between distance and the stock's volatility and manufactured a 'ceiling'.)"""
    fwd = _forward_prices(px, (horizon,))[["ticker", "date", "close", f"maxhigh_{horizon}d"]]
    c = prof[(prof["gross_share"] >= min_share) & prof["sell_vwap"].notna()].merge(fwd, on=["ticker", "date"], how="inner")
    c = c.dropna(subset=[f"maxhigh_{horizon}d"])
    c["d0"] = c["sell_vwap"] / c["close"] - 1
    c = c[(c["d0"] > 0) & (c["d0"] <= max_dist)].copy()
    c["hit"] = (c[f"maxhigh_{horizon}d"] >= c["sell_vwap"]).astype(float)
    ref_keys = ct[["ticker", "date", "mm_broker"]].rename(columns={"mm_broker": "broker"}).assign(is_ref=True)
    c = c.merge(ref_keys, on=["ticker", "date", "broker"], how="left")
    c["is_ref"] = c["is_ref"].fillna(False).astype(bool)
    grp_n = c.groupby(["ticker", "date"])["hit"].transform("size")
    c = c[(grp_n >= 2) & c.groupby(["ticker", "date"])["is_ref"].transform("any")].reset_index(drop=True)
    real_all = c[c["is_ref"]]
    if len(real_all) < 30:
        return pd.DataFrame()
    c["bucket"] = pd.cut(c["d0"], [0, 0.02, 0.05, max_dist])
    real = c[c["is_ref"]]
    rng = np.random.default_rng(seed)
    keys = c.groupby(["ticker", "date"]).ngroup().to_numpy()
    cats = list(c["bucket"].cat.categories)
    null_all = np.zeros(B)
    null_b = np.full((B, len(cats)), np.nan)
    hit = c["hit"].to_numpy()
    bk = c["bucket"].to_numpy()
    for b in range(B):
        order = np.lexsort((rng.random(len(c)), keys))
        first = order[np.r_[True, keys[order][1:] != keys[order][:-1]]]
        chosen_hit, chosen_bk = hit[first], bk[first]
        null_all[b] = chosen_hit.mean()
        for k, cat in enumerate(cats):
            m = chosen_bk == cat
            if m.sum():
                null_b[b, k] = chosen_hit[m].mean()
    rows = [{"bucket": "ALL", "n_ref": len(real), "ref_hit": real["hit"].mean(), "random_candidate_hit": null_all.mean(),
             "diff": real["hit"].mean() - null_all.mean(), "p_ref_lower": (1 + np.sum(null_all <= real["hit"].mean())) / (B + 1.0)}]
    for k, cat in enumerate(cats):
        r = real[real["bucket"] == cat]
        if len(r) < 10:
            continue
        rows.append({"bucket": str(cat), "n_ref": len(r), "ref_hit": r["hit"].mean(), "random_candidate_hit": np.nanmean(null_b[:, k]),
                     "diff": r["hit"].mean() - np.nanmean(null_b[:, k]),
                     "p_ref_lower": (1 + np.sum(null_b[:, k] <= r["hit"].mean())) / (B + 1.0)})
    return pd.DataFrame(rows)


def _report(ct: pd.DataFrame, label: str, prof_all: pd.DataFrame | None = None, px_all: pd.DataFrame | None = None) -> None:
    print(f"=== reference broker mode: {label} | ticker-days with a reference broker: {len(ct):,} | tickers: {ct['ticker'].nunique() if len(ct) else 0}")
    if ct.empty:
        return
    print("   who:", ct["mm_broker"].value_counts().head(8).to_dict())
    for h in (5, 10):
        col = f"up_vs_mm_sell_{h}d"
        x = ct.dropna(subset=[col])
        if not len(x):
            continue
        print(f"   h={h}d (n={len(x)}): max high vs reference SELL price: P25 {x[col].quantile(.25) * 100:+.1f}%  median {x[col].median() * 100:+.1f}%  P75 {x[col].quantile(.75) * 100:+.1f}%"
              f" | ceiling hit {x[f'ceiling_hit_{h}d'].mean() * 100:.0f}% | close(t+h) vs reference sell: median {x[f'stall_{h}d'].median() * 100:+.1f}%")
        r = x.dropna(subset=["retail_net_share"])
        if len(r) >= 30:
            lo, hi = r["retail_net_share"].quantile(.33), r["retail_net_share"].quantile(.67)
            ex, nx = r[r["retail_net_share"] <= lo], r[r["retail_net_share"] >= hi]
            print(f"      retail net SELLING (bottom tercile, n={len(ex)}): median up_vs_ref_buy {ex[f'up_vs_mm_buy_{h}d'].median() * 100:+.1f}%, hit {ex[f'ceiling_hit_{h}d'].mean() * 100:.0f}%"
                  f"  |  retail net BUYING (top tercile, n={len(nx)}): median {nx[f'up_vs_mm_buy_{h}d'].median() * 100:+.1f}%, hit {nx[f'ceiling_hit_{h}d'].mean() * 100:.0f}%")
    if prof_all is not None and px_all is not None:
        for h in (5, 10):
            pc = placebo_other_brokers(prof_all, ct, px_all, horizon=h)
            if not pc.empty:
                print(f"   PLACEBO h={h}d (reference broker's sell VWAP vs a random sizeable broker's sell VWAP on the SAME stock-day):")
                print(pc.round(3).to_string(index=False).replace(chr(10), chr(10) + "     "))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=common.IPOT_DB)
    a = ap.parse_args()
    raw = common.load_ipot(a.db)
    px = common.load_ohlcv(tickers=sorted(raw["ticker"].unique()), start="2025-01-01")
    prof = flag_mm(broker_window_profile(raw, px))
    full = prof[prof["presence"].notna()]
    print(f"broker-window rows: {len(full):,} | symmetric-rule flags: {int(prof['mm_flag'].sum()):,} | counterparty-rule flags: {int(prof['cp_flag'].sum()):,}")
    print("symmetric flags by class:", prof[prof["mm_flag"]]["cls"].value_counts().to_dict(), "(retail apps + big institutions dominate: the rule does NOT isolate market makers)")
    print("counterparty flags by class:", prof[prof["cp_flag"]]["cls"].value_counts().to_dict())
    for mode in ("symmetric", "counterparty"):
        _report(ceiling_table(prof, px, mode=mode), mode, prof, px)
    print("(Descriptive only. The dev sample is 30 tickers x ~3 weeks; nothing here is evidence.)")
