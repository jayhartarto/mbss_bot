"""
research/ipot_explore/bigwinner.py — EXP-BIGWIN-1 (registered before running): a signal for the RIGHT TAIL only. Can we pick, at the close of t, the stock-days that become big winners (net return >= +20% with a 12% stop and a 20-day hold,
bought at the open of t+1), instead of the average winner?
Population: every 3rd eligible stock-day of every ticker (close >= 50, 20-day value >= Rp0.1bn, >= 130 days of history), 2023-2026, 684 tickers. Outcome: market buy at open(t+1), stop -12% (gap through the stop exits at the open),
time exit at the close of day 20, no take-profit; net of 0.4% cost.  Label BIG = net >= +20%  (BIG15 / BIG30 shown too).
Features (all known at the close of t): trend (dist to MA20/MA50/MA100, MA20 and MA50 slopes, 60-day drawdown, 60-day range position), momentum (ret 1/5/10/20/60 days), RSI14, ATR%, Bollinger bandwidth, volume ratios, log value and log price,
relative strength vs IHSG (20/60 days), IHSG 20-day return, IHSG regime dummies, market breadth (share of eligible stocks above MA20 / MA50, share up on the day), and the per-date cross-sectional ranks of ret20 / RSI / volume ratio.
Learners: gradient boosting (HistGradientBoosting, 250 iterations, depth 4, lr 0.04) and a logistic regression; baselines: random and highest-ATR. Walk-forward by calendar quarter (train on all earlier quarters with a 25-trading-day purge;
test quarters from 2024Q3 on). Reported: AUC per quarter, and the precision / mean net return / median of (a) the top 1% and (b) the top 3 per day (no repeat of a ticker within 20 days) by predicted probability, vs the base rate;
the same without the market-context features (ablation).
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.bigwinner
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from research.ipot_explore.entry_scenarios import load, regimes, simulate, COST

PURGE = 25
MIN_PRICE = 30       # IDX removed the Rp50 floor; Rp30 is the executable minimum used here (was 50 in the first runs)
REGS = ["BEARISH_GRIND", "BOUNCE_OFF_LOW", "CRASH_FAST", "NEUTRAL_BULLISH", "PULLBACK_IN_BOUNCE"]


def ihsg() -> pd.DataFrame:
    import yfinance as yf
    h = yf.Ticker("^JKSE").history(period="6y")
    h.index = h.index.tz_localize(None).normalize()
    d = pd.DataFrame({"date": h.index.astype("datetime64[ns]"), "ix": h["Close"].to_numpy()})
    d["ix_ret5"], d["ix_ret20"], d["ix_ret60"] = d.ix.pct_change(5), d.ix.pct_change(20), d.ix.pct_change(60)
    d["ix_dd100"] = d.ix / d.ix.rolling(100, min_periods=30).max() - 1
    return d[["date", "ix_ret5", "ix_ret20", "ix_ret60", "ix_dd100"]]


def build():
    px = load()
    px["ticker"] = px["ticker"].astype("category")
    px = px.merge(regimes(), on="date", how="left").merge(ihsg(), on="date", how="left")
    px["regime_phase"] = px["regime_phase"].fillna("NA")
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    g = px.groupby("ticker", observed=True)
    c = px["close"]
    pc = g["close"].shift(1)
    tr = pd.concat([px["high"] - px["low"], (px["high"] - pc).abs(), (px["low"] - pc).abs()], axis=1).max(axis=1)
    px["atr"] = tr.groupby(px["ticker"], observed=True).transform(lambda s: s.rolling(14).mean()) / c
    for n in (5, 10, 20, 60):
        px[f"r{n}"] = g["close"].pct_change(n)
    px["d20"], px["d50"], px["d100"] = c / px["ma20"] - 1, c / px["ma50"] - 1, c / px["ma100"] - 1
    px["s20"] = px["ma20"] / g["ma20"].shift(5) - 1
    px["s50"] = px["ma50"] / g["ma50"].shift(10) - 1
    hi60, lo60 = g["close"].transform(lambda s: s.rolling(60).max()), g["close"].transform(lambda s: s.rolling(60).min())
    px["dd60"], px["pos60"] = c / hi60 - 1, (c - lo60) / (hi60 - lo60).replace(0, np.nan)
    sd20 = g["close"].transform(lambda s: s.rolling(20).std())
    px["bw"] = 4 * sd20 / px["ma20"]
    px["vr5"] = g["volume"].transform(lambda s: s.rolling(5).mean()) / g["volume"].transform(lambda s: s.rolling(20).mean())
    px["lval"], px["lclose"] = np.log(px["val20"].clip(lower=1)), np.log(c.clip(lower=1))
    px["rs20"], px["rs60"] = px["r20"] - px["ix_ret20"], px["r60"] - px["ix_ret60"]
    elig = (px["close"] >= MIN_PRICE) & (px["val20"] >= 0.1e9) & (px["nobs"] >= 130) & (px["date"] >= "2023-01-01")
    px["elig"] = elig
    e = px[elig]
    br = pd.DataFrame({"b20": (e.close > e.ma20).groupby(e.date).mean(), "b50": (e.close > e.ma50).groupby(e.date).mean(), "adv": (e.ret1 > 0).groupby(e.date).mean()}).reset_index()
    px = px.merge(br, on="date", how="left")
    for col in ("r20", "rsi", "vr"):
        px[f"rk_{col}"] = px[elig.reindex(px.index).fillna(False)].groupby("date")[col].rank(pct=True) if False else np.nan
    sub = px.loc[px.elig]
    for col in ("r20", "rsi", "vr"):
        px.loc[px.elig, f"rk_{col}"] = sub.groupby("date")[col].rank(pct=True).to_numpy()
    for r in REGS:
        px[f"reg_{r}"] = (px["regime_phase"] == r).astype(float)
    return px


PRICE_F = ["ret1", "r5", "r10", "r20", "r60", "rsi", "atr", "d20", "d50", "d100", "s20", "s50", "dd60", "pos60", "bw", "vr", "vr5", "lval", "lclose", "rk_r20", "rk_rsi", "rk_vr"]
MARKET_F = ["rs20", "rs60", "ix_ret5", "ix_ret20", "ix_ret60", "ix_dd100", "b20", "b50", "adv"] + [f"reg_{r}" for r in REGS]


def main():
    px = build()
    rows_all = np.where((px["elig"] & (px["nobs"] % 3 == 0)).to_numpy())[0]
    filled, ret = simulate(px, rows_all, None, 9.99, 0.12, 20, hmax=20)
    keep = filled
    rows = rows_all[keep]
    net = ret[keep] - COST
    D = px.iloc[rows].copy()
    D["net"] = net
    D["ticker_s"] = D["ticker"].astype(str)
    for thr in (0.15, 0.20, 0.30):
        D[f"BIG{int(thr * 100)}"] = (D["net"] >= thr).astype(int)
    D["q"] = D["date"].dt.to_period("Q")
    D["t"] = D["date"].rank(method="dense")
    D[PRICE_F + MARKET_F] = D[PRICE_F + MARKET_F].replace([np.inf, -np.inf], np.nan)
    D = D.dropna(subset=PRICE_F + MARKET_F).reset_index(drop=True)
    for col in PRICE_F + MARKET_F:
        lo, hi = D[col].quantile([0.005, 0.995])
        D[col] = D[col].clip(lo, hi)
    print(f"population {len(D)} stock-days ({D.ticker_s.nunique()} tickers, {D.date.min().date()}..{D.date.max().date()}) | net mean {100 * D.net.mean():+.2f}%, median {100 * D.net.median():+.2f}% | BIG15 {100 * D.BIG15.mean():.1f}% BIG20 {100 * D.BIG20.mean():.1f}% BIG30 {100 * D.BIG30.mean():.1f}%")
    print("net return of the base population by year:", {int(y): round(100 * v, 2) for y, v in D.groupby(D.date.dt.year)["net"].mean().items()})
    rng = np.random.default_rng(7)
    quarters = sorted(D["q"].unique())
    tdates = np.sort(D["date"].unique())
    results = {}
    for label, feats in (("PRICE+MARKET", PRICE_F + MARKET_F), ("PRICE only (ablation)", PRICE_F)):
        for lname in ("GBM", "LOGIT"):
            parts = []
            for q in quarters:
                if q < pd.Period("2024Q3"):
                    continue
                te = D["q"] == q
                start = D.loc[te, "date"].min()
                cut = tdates[max(np.searchsorted(tdates, np.datetime64(start)) - PURGE, 0)]
                tr = D["date"] < cut
                if tr.sum() < 15000 or te.sum() < 500 or D.loc[tr, "BIG20"].sum() < 200:
                    continue
                X, Xt = D.loc[tr, feats].to_numpy(float), D.loc[te, feats].to_numpy(float)
                if lname == "GBM":
                    m = HistGradientBoostingClassifier(max_iter=250, max_depth=4, learning_rate=0.04, l2_regularization=1.0, random_state=7).fit(X, D.loc[tr, "BIG20"])
                    p = m.predict_proba(Xt)[:, 1]
                else:
                    sc = StandardScaler().fit(X)
                    m = LogisticRegression(C=0.05, max_iter=400).fit(sc.transform(X), D.loc[tr, "BIG20"])
                    p = m.predict_proba(sc.transform(Xt))[:, 1]
                T = D.loc[te, ["date", "ticker_s", "net", "BIG20", "atr", "q"]].copy()
                T["p"] = p
                parts.append(T)
            results[(label, lname)] = pd.concat(parts)
    T0 = results[("PRICE+MARKET", "GBM")].copy()
    results[("baseline", "random")] = T0.assign(p=rng.random(len(T0)))
    results[("baseline", "ATR top")] = T0.assign(p=T0["atr"].to_numpy())

    def top_stats(T, label):
        out = {}
        thr = T.groupby("q")["p"].transform(lambda s: s.quantile(0.99))
        s1 = T[T["p"] >= thr]
        out["top1%"] = s1
        recent, rows_ = {}, []
        for dt, g in T.sort_values("date").groupby("date"):
            g = g.reset_index(drop=True)
            order = np.argsort(-g["p"].to_numpy())
            ch = 0
            for j in order:
                tk = g["ticker_s"].iloc[j]
                if (dt - recent.get(tk, pd.Timestamp("1900-01-01"))).days < 28:
                    continue
                recent[tk] = dt
                rows_.append(g.iloc[j])
                ch += 1
                if ch == 3:
                    break
        out["top3/day"] = pd.DataFrame(rows_)
        return out

    base = T0.BIG20.mean()
    print(f"\ntest period {T0.date.min().date()}..{T0.date.max().date()} | test rows {len(T0)} | base BIG20 {100 * base:.1f}% | base net mean {100 * T0.net.mean():+.2f}%")
    print(f"\n{'model':<26}{'AUC':>7}  {'selection':<9}{'n':>6}{'BIG20':>8}{'lift':>6}{'net mean':>10}{'median':>8}{'win':>6}")
    for (label, lname), T in results.items():
        try:
            auc = roc_auc_score(T.BIG20, T.p)
        except ValueError:
            auc = np.nan
        for sel, S in top_stats(T, label).items():
            print(f"{label + ' ' + lname:<26}{auc:>7.3f}  {sel:<9}{len(S):>6}{100 * S.BIG20.mean():>7.1f}%{S.BIG20.mean() / base:>6.1f}{100 * S.net.mean():>9.2f}%{100 * S.net.median():>7.1f}%{100 * (S.net > 0).mean():>5.0f}%")
    print("\nGBM PRICE+MARKET: per-quarter AUC, top-1% precision and mean net:")
    for q, g in T0.groupby("q"):
        s = g[g.p >= g.p.quantile(0.99)]
        try:
            a = roc_auc_score(g.BIG20, g.p)
        except ValueError:
            a = np.nan
        print(f"   {q}: n {len(g):>5} base {100 * g.BIG20.mean():4.1f}% | AUC {a:.3f} | top1% n {len(s):>3} precision {100 * s.BIG20.mean():4.1f}% net {100 * s.net.mean():+.2f}% median {100 * s.net.median():+.1f}%")


if __name__ == "__main__":
    main()
