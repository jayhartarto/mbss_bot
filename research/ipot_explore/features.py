"""
research/ipot_explore/features.py — Phase 1 feature atlas for the IPOT broker-summary research (see ../IPOT_BROKER_RESEARCH_FRAMEWORK.md, groups A-G).

Run (from the project root):  python -m research.ipot_explore.features [--db research/ipot_dev.sqlite] [--out research/ipot_explore/out]

Pipeline
  broker_day_table()       one row per (ticker, date, broker) of a variant, with net BOUNDS (net is only exact when the broker is on both
                           sides of the day's top-10; one-sided brokers get lo/hi bounds from the other side's 10th-place cutoff).
  ticker_day_features()    group A-F: coverage, concentration, churn, class shares, price relation of the visible buyers/sellers, foreign / nego share.
  rolling_pattern_features() group G: routine (stealth) score, burst ratio, dominant-accumulator inventory + VWAP + ramp, per ticker over trading-day windows.
  outcomes()               forward returns from open(t+1) (IPOT is published after the close), relative to the liquid-universe median that day, MFE/MAE.
Everything is deterministic and re-runnable; no hypothesis is baked in (features only describe).
"""
from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd

from research.ipot_explore import common

ROUTINE_WINDOW = 10      # trading days
BURST_BASE_WINDOW = 20
MIN_OBS = 5              # min trading days with data inside a window before a rolling feature is reported
HORIZONS = (1, 3, 5, 10)


# ---------------------------------------------------------------------------------------------- broker-day table
def broker_day_table(raw: pd.DataFrame, fd: str = "all", board: str = "all") -> pd.DataFrame:
    """Rows of one variant with: is_two, cut_*_val/lot (the 10th place on each side; 0 if the side listed < 10 brokers), and net_{val,lot}_{lo,hi,mid}.
    two-sided -> exact net (lo=hi=mid). buy-only -> net in [buy - cut_sell, buy]. sell-only -> net in [-sell, -sell + cut_buy]."""
    d = raw[(raw["fd"] == fd) & (raw["board"] == board)].copy()
    if d.empty:
        return d
    key = ["ticker", "date"]
    g = d.groupby(key)
    n_buy = g["buy_val"].transform("count")
    n_sell = g["sell_val"].transform("count")
    for side, n in (("buy", n_buy), ("sell", n_sell)):
        for unit in ("val", "lot"):
            col = f"{side}_{unit}"
            cut = g[col].transform("min")
            d[f"cut_{side}_{unit}"] = np.where(n >= 10, cut, 0.0)
    d["is_two"] = d["buy_val"].notna() & d["sell_val"].notna()
    for unit in ("val", "lot"):
        b, s = d[f"buy_{unit}"], d[f"sell_{unit}"]
        cb, cs = d[f"cut_buy_{unit}"], d[f"cut_sell_{unit}"]
        lo = np.where(d["is_two"], b - s, np.where(b.notna(), b - cs, -s))
        hi = np.where(d["is_two"], b - s, np.where(b.notna(), b, -s + cb))
        d[f"net_{unit}_lo"], d[f"net_{unit}_hi"] = lo, hi
        d[f"net_{unit}_mid"] = (lo + hi) / 2.0
    return d


# ---------------------------------------------------------------------------------------------- ticker-day features (groups A-F)
def _side_stats(d: pd.DataFrame, side: str) -> pd.DataFrame:
    """Top-1/3/5 share, HHI, n brokers >=5% share, and the avg price of the #1 and of the top-3 (value weighted) for one side."""
    col, lot, avg = f"{side}_val", f"{side}_lot", f"{side}_avg"
    s = d.dropna(subset=[col]).sort_values(["ticker", "date", col], ascending=[True, True, False]).copy()
    s["rk"] = s.groupby(["ticker", "date"]).cumcount() + 1
    s["share"] = s[col] / s.groupby(["ticker", "date"])[col].transform("sum")
    s["val3"] = np.where(s["rk"] <= 3, s[col], 0.0)
    s["lot3"] = np.where(s["rk"] <= 3, s[lot], 0.0)
    g = s.groupby(["ticker", "date"])
    out = pd.DataFrame({
        f"{side}_vis": g[col].sum(),
        f"{side}_top1": g["share"].apply(lambda x: x.iloc[0]),
        f"{side}_top3": g["share"].apply(lambda x: x.iloc[:3].sum()),
        f"{side}_top5": g["share"].apply(lambda x: x.iloc[:5].sum()),
        f"{side}_hhi": g["share"].apply(lambda x: float((x ** 2).sum())),
        f"{side}_n5": g["share"].apply(lambda x: int((x >= 0.05).sum())),
        f"{side}_top1_avg": s[s["rk"] == 1].set_index(["ticker", "date"])[avg],
        f"{side}_top3_avg": g["val3"].sum() / (g["lot3"].sum() * 100.0).replace(0, np.nan),
    })
    return out


def _class_shares(d: pd.DataFrame, side: str) -> pd.DataFrame:
    col = f"{side}_val"
    s = d.dropna(subset=[col]).copy()
    s["cls"] = s["broker"].map(common.BROKER_CLASS).fillna("other")
    tot = s.groupby(["ticker", "date"])[col].sum()
    p = s.pivot_table(index=["ticker", "date"], columns="cls", values=col, aggfunc="sum", fill_value=0.0)
    p = p.div(tot, axis=0)
    p.columns = [f"{side}_cls_{c}" for c in p.columns]
    return p


def ticker_day_features(raw: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    """One row per (ticker, date) of the all/all variant. `px` = common.load_ohlcv() (needs close/value)."""
    a = broker_day_table(raw, "all", "all")
    if a.empty:
        return pd.DataFrame()
    feat = _side_stats(a, "buy").join(_side_stats(a, "sell"), how="outer")
    feat = feat.join(_class_shares(a, "buy"), how="left").join(_class_shares(a, "sell"), how="left")
    g = a.groupby(["ticker", "date"])
    two = a[a["is_two"]]
    gt = two.groupby(["ticker", "date"])
    feat["n_two"] = gt.size()
    feat["n_two"] = feat["n_two"].fillna(0)
    feat["churn_share"] = 2.0 * gt.apply(lambda x: np.minimum(x["buy_val"], x["sell_val"]).sum()) / (feat["buy_vis"] + feat["sell_vis"])
    feat["two_sym"] = gt.apply(lambda x: float(np.average(np.minimum(x["buy_lot"], x["sell_lot"]) / np.maximum(x["buy_lot"], x["sell_lot"]),
                                                              weights=x["buy_val"] + x["sell_val"])))
    feat["top_net_buyer_mid"] = g["net_val_mid"].max()
    feat["top_net_seller_mid"] = g["net_val_mid"].min()
    feat = feat.reset_index().merge(px[["ticker", "date", "close", "value", "volume"]], on=["ticker", "date"], how="left")
    feat["buy_cov"] = feat["buy_vis"] / feat["value"]
    feat["sell_cov"] = feat["sell_vis"] / feat["value"]
    feat["top_net_buyer_share"] = feat["top_net_buyer_mid"] / feat["value"]
    feat["top_net_seller_share"] = feat["top_net_seller_mid"] / feat["value"]
    feat["buy_top1_avg_vs_close"] = feat["buy_top1_avg"] / feat["close"] - 1
    feat["sell_top1_avg_vs_close"] = feat["sell_top1_avg"] / feat["close"] - 1
    feat["buy_top3_avg_vs_close"] = feat["buy_top3_avg"] / feat["close"] - 1
    feat["buyer_seller_avg_spread"] = feat["buy_top1_avg"] / feat["sell_top1_avg"] - 1

    # foreign / domestic / negotiated-board views (other variants), as ratios to the all/all visible value or to the day's traded value
    for fd, board, tag in (("F", "all", "F"), ("D", "all", "D"), ("all", "NG", "NG")):
        v = broker_day_table(raw, fd, board)
        if v.empty:
            continue
        gv = v.groupby(["ticker", "date"])
        part = pd.DataFrame({f"{tag}_buy_vis": gv["buy_val"].sum(), f"{tag}_net_mid": gv["net_val_mid"].sum()}).reset_index()
        feat = feat.merge(part, on=["ticker", "date"], how="left")
        feat[f"{tag}_buy_share_of_vis"] = feat[f"{tag}_buy_vis"] / feat["buy_vis"]
        feat[f"{tag}_net_share"] = feat[f"{tag}_net_mid"] / feat["value"]
    return feat


# ---------------------------------------------------------------------------------------------- rolling pattern features (group G)
def _argmax_col(df: pd.DataFrame) -> pd.Series:
    """Column label of each row's maximum; NaN for rows that are entirely NaN (idxmax raises on those)."""
    return df.fillna(-np.inf).idxmax(axis=1).where(df.notna().any(axis=1))


def _lookup(pivot: pd.DataFrame, cols: pd.Series) -> np.ndarray:
    """pivot value at (row i, column cols[i]); NaN when cols[i] is missing/unknown."""
    idx = pivot.columns.get_indexer(cols.fillna("__none__"))
    vals = pivot.to_numpy(dtype=float)
    out = np.full(len(pivot), np.nan)
    ok = idx >= 0
    out[ok] = vals[np.arange(len(pivot))[ok], idx[ok]]
    return out


def rolling_pattern_features(raw: pd.DataFrame, px: pd.DataFrame, window: int = ROUTINE_WINDOW,
                             burst_w: int = BURST_BASE_WINDOW) -> pd.DataFrame:
    a = broker_day_table(raw, "all", "all")
    if a.empty:
        return pd.DataFrame()
    pxi = px.set_index(["ticker", "date"])
    rows = []
    for tk, d in a.groupby("ticker"):
        dates = sorted(d["date"].unique())
        if len(dates) < MIN_OBS:
            continue
        idx = pd.DatetimeIndex(dates)
        val = pxi.loc[tk]["value"].reindex(idx) if tk in pxi.index.get_level_values(0) else pd.Series(np.nan, index=idx)
        vol = pxi.loc[tk]["volume"].reindex(idx) if tk in pxi.index.get_level_values(0) else pd.Series(np.nan, index=idx)
        buy_sorted = d.dropna(subset=["buy_val"]).sort_values(["date", "buy_val"], ascending=[True, False]).copy()
        buy_sorted["rk"] = buy_sorted.groupby("date").cumcount() + 1
        top3 = buy_sorted[buy_sorted["rk"] <= 3].assign(f=1.0).pivot_table(index="date", columns="broker", values="f", aggfunc="max").reindex(idx).fillna(0.0)
        net = d.pivot_table(index="date", columns="broker", values="net_val_mid", aggfunc="sum").reindex(idx).fillna(0.0)
        bval = d.pivot_table(index="date", columns="broker", values="buy_val", aggfunc="sum").reindex(idx)
        blot = d.pivot_table(index="date", columns="broker", values="buy_lot", aggfunc="sum").reindex(idx)
        bavg = d.pivot_table(index="date", columns="broker", values="buy_avg", aggfunc="mean").reindex(idx)

        n_obs = pd.Series(1.0, index=idx).rolling(window, min_periods=MIN_OBS).sum()
        cnt = top3.rolling(window, min_periods=MIN_OBS).sum()
        out = pd.DataFrame(index=idx)
        out["routine_gross_broker"] = _argmax_col(cnt)
        out["routine_gross_score"] = cnt.max(axis=1) / n_obs      # SATURATED on real data (big brokers are always top-3 by gross buy): keep only as a size control
        # NET-based routine: how often a broker is among the top-3 NET buyers of the day (net_mid > 0). Big two-sided brokers drop out because their net
        # is near zero or negative on many days, so this separates steady accumulation from mere size.
        npos = net.where(net > 0)
        netrank = npos.rank(axis=1, ascending=False, method="first")
        topnet = (netrank <= 3).astype(float)
        ncnt = topnet.rolling(window, min_periods=MIN_OBS).sum()
        out["routine_net_broker"] = _argmax_col(ncnt)
        out["routine_net_score"] = ncnt.max(axis=1) / n_obs
        cv = bavg.rolling(window, min_periods=3).std() / bavg.rolling(window, min_periods=3).mean()
        out["routine_buyavg_cv"] = _lookup(cv, out["routine_net_broker"])

        cum = net.rolling(window, min_periods=MIN_OBS).sum()
        out["dom_broker"] = _argmax_col(cum)
        wval = val.rolling(window, min_periods=MIN_OBS).sum()
        out["dom_inventory_share"] = cum.max(axis=1) / wval
        posdays = (net > 0).astype(float).rolling(window, min_periods=MIN_OBS).sum() / n_obs.values[:, None]
        out["dom_pos_days"] = _lookup(posdays, out["dom_broker"])      # share of window days the dominant accumulator was a net buyer
        sv = bval.fillna(0.0).rolling(window, min_periods=MIN_OBS).sum()
        sl = blot.fillna(0.0).rolling(window, min_periods=MIN_OBS).sum()
        vwap = sv / (sl * 100.0).replace(0, np.nan)
        out["dom_vwap"] = _lookup(vwap, out["dom_broker"])
        share = net.div(val, axis=0)
        r3 = share.rolling(3, min_periods=3).sum()
        r10 = share.rolling(window, min_periods=window).sum()
        ramp = r3 / 3.0 - (r10 - r3) / (window - 3.0)
        out["dom_ramp"] = _lookup(ramp, out["dom_broker"])

        tb = net.max(axis=1) / val
        base = tb.shift(1).rolling(burst_w, min_periods=MIN_OBS).median()
        out["burst_ratio"] = (tb / base.where(base > 0)).replace([np.inf, -np.inf], np.nan).clip(upper=20.0)
        out["top_net_buyer_share_roll"] = tb
        vbase = vol.shift(1).rolling(burst_w, min_periods=MIN_OBS).mean()
        out["vol_ratio"] = vol / vbase
        out["ticker"] = tk
        out.index.name = "date"
        rows.append(out.reset_index())
    if not rows:
        return pd.DataFrame()
    res = pd.concat(rows, ignore_index=True)
    res = res.merge(px[["ticker", "date", "close"]], on=["ticker", "date"], how="left")
    res["dom_vwap_dist"] = res["close"] / res["dom_vwap"] - 1
    # price context over the same window (flat price while inventory builds = the 'quiet accumulation' shape)
    px = px.sort_values(["ticker", "date"]).copy()
    px["past_ret_10d"] = px.groupby("ticker")["close"].pct_change(window)      # BACKWARD (known at t); forward outcomes are ret_Nd in outcomes()
    res = res.merge(px[["ticker", "date", "past_ret_10d"]], on=["ticker", "date"], how="left")
    res["quiet_accum"] = (res["routine_net_score"] >= 0.6) & (res["past_ret_10d"].abs() <= 0.05) & (res["dom_inventory_share"] > 0.03)
    return res.drop(columns=["close"])


# ---------------------------------------------------------------------------------------------- outcomes
def outcomes(px_all: pd.DataFrame, min_value_20d: float = 1e9) -> pd.DataFrame:
    """Forward returns from open(t+1): ret_h = close(t+h)/open(t+1)-1 (h=1,3,5,10), relative to the liquid-universe median of the same date,
    plus 5-day MFE/MAE from the same entry."""
    p = px_all.sort_values(["ticker", "date"]).copy()
    g = p.groupby("ticker")
    entry = g["open"].shift(-1)
    p["val20"] = g["value"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    for h in HORIZONS:
        p[f"ret_{h}d"] = g["close"].shift(-h) / entry - 1
    mfe = pd.concat([g["high"].shift(-k) for k in range(1, 6)], axis=1).max(axis=1)
    mae = pd.concat([g["low"].shift(-k) for k in range(1, 6)], axis=1).min(axis=1)
    p["mfe_5d"] = mfe / entry - 1
    p["mae_5d"] = mae / entry - 1
    liquid = p[p["val20"] >= min_value_20d]
    for h in HORIZONS:
        med = liquid.groupby("date")[f"ret_{h}d"].median().rename(f"med_{h}d")
        p = p.merge(med, on="date", how="left")
        p[f"rel_{h}d"] = p[f"ret_{h}d"] - p[f"med_{h}d"]
        p = p.drop(columns=[f"med_{h}d"])
    keep = ["ticker", "date", "val20"] + [f"ret_{h}d" for h in HORIZONS] + [f"rel_{h}d" for h in HORIZONS] + ["mfe_5d", "mae_5d"]
    return p[keep]


# ---------------------------------------------------------------------------------------------- orchestration
def build(db_path: str = common.IPOT_DB, out_dir: str = common.OUT_DIR, ohlcv_start: str = "2025-01-01") -> pd.DataFrame:
    raw = common.load_ipot(db_path)
    tickers = sorted(raw["ticker"].unique())
    px_all = common.load_ohlcv(start=ohlcv_start)
    px = px_all[px_all["ticker"].isin(tickers)]
    base = ticker_day_features(raw, px)
    pat = rolling_pattern_features(raw, px)
    out = outcomes(px_all)
    feats = base.merge(pat, on=["ticker", "date"], how="left").merge(out, on=["ticker", "date"], how="left")
    os.makedirs(out_dir, exist_ok=True)
    feats.to_pickle(os.path.join(out_dir, "features.pkl"))
    return feats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=common.IPOT_DB)
    ap.add_argument("--out", default=common.OUT_DIR)
    a = ap.parse_args()
    f = build(a.db, a.out)
    print(f"features: {f.shape[0]} ticker-days x {f.shape[1]} columns, {f['ticker'].nunique()} tickers, {f['date'].min().date()} .. {f['date'].max().date()}")
    print("non-null share of key features:")
    for c in ("buy_cov", "buy_top1", "churn_share", "buy_top1_avg_vs_close", "F_net_share", "routine_score", "dom_inventory_share", "dom_ramp",
              "burst_ratio", "ret_5d", "rel_5d"):
        if c in f:
            print(f"  {c:26s} {f[c].notna().mean() * 100:5.1f}%")
