"""
research/ipot_explore/broker_skill.py — Engine 4 of the IPOT research framework: is there broker-level SKILL (informativeness) beyond noise?

Run (project root):  python -m research.ipot_explore.broker_skill [--db research/ipot_dev.sqlite] [--B 400] [--top-n 1]

Event: broker b is the TOP net buyer (rank <= top_n by net_val_mid, net > 0) of a ticker-day (or the top net SELLER). Outcome: rel_{h}d (forward return from open(t+1)
minus the liquid-universe median of that date), TICKER-DEMEANED (y - mean y of that ticker) so a broker that simply trades a persistently strong or weak stock gets no credit.
Per broker: mean adjusted outcome on its events, standard error clustered by ticker.
  * Empirical Bayes: tau^2 = max(0, var_between(mu_hat) - mean(se^2)); posterior mean = m + tau^2/(tau^2 + se^2) * (mu_hat - m). tau^2 ~ 0 means the spread of broker
    estimates is no bigger than noise: nobody shows skill, and the posterior collapses to the mean.
  * Dispersion test: D = variance over brokers of z = mu_hat/se, compared with a null that circularly shifts each ticker's outcome series (keeps autocorrelation and drift,
    breaks the link to the brokers). Also max|z|.
  * Persistence (the real test of skill): split the dates in 2 blocks, Spearman correlation of broker estimates between blocks (anchor: a crypto-wallet study found 0.52 between
    adjacent 10-day windows), with a permutation p over broker labels.
A single broker that looks good in one block and bad in the next is luck. Only persistence + dispersion beyond the shift-null count as evidence.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from research.ipot_explore import common, features

HORIZONS = (1, 3, 5)


def build_events(raw: pd.DataFrame, outcomes: pd.DataFrame, side: str = "buy", top_n: int = 1) -> pd.DataFrame:
    """One row per (ticker, date, broker) event with the ticker-demeaned outcomes rel_1d / rel_3d / rel_5d (columns y1, y3, y5)."""
    a = features.broker_day_table(raw, "all", "all")
    sign = 1.0 if side == "buy" else -1.0
    a = a[(sign * a["net_val_mid"]) > 0].copy()
    a["rk"] = a.groupby(["ticker", "date"])["net_val_mid"].rank(ascending=(side == "sell"), method="first")
    ev = a[a["rk"] <= top_n][["ticker", "date", "broker", "net_val_mid"]].copy()
    o = outcomes.copy()
    for h in HORIZONS:
        col = f"rel_{h}d"
        o[f"y{h}"] = o[col] - o.groupby("ticker")[col].transform("mean")
    ev = ev.merge(o[["ticker", "date"] + [f"y{h}" for h in HORIZONS]], on=["ticker", "date"], how="left")
    return ev


def broker_estimates(ev: pd.DataFrame, h: int, n_min: int = 40) -> pd.DataFrame:
    ycol = f"y{h}"
    rows = []
    for b, g in ev.dropna(subset=[ycol]).groupby("broker"):
        if len(g) < n_min:
            continue
        mu = g[ycol].mean()
        cs = g.groupby("ticker")[ycol].apply(lambda s: float((s - mu).sum()))
        se = float(np.sqrt((cs ** 2).sum()) / len(g))
        rows.append({"broker": b, "n": len(g), "n_tickers": g["ticker"].nunique(), "mu": mu, "se": se})
    est = pd.DataFrame(rows)
    if est.empty:
        return est
    est["z"] = est["mu"] / est["se"]
    return est


def eb_shrink(est: pd.DataFrame) -> tuple[pd.DataFrame, float, float]:
    """Method-of-moments empirical Bayes. Returns (frame with posterior, tau2, grand mean)."""
    e = est.copy()
    w = 1.0 / e["se"] ** 2
    m = float((w * e["mu"]).sum() / w.sum())
    tau2 = max(0.0, float(e["mu"].var(ddof=1) - (e["se"] ** 2).mean())) if len(e) > 2 else 0.0
    shrink = tau2 / (tau2 + e["se"] ** 2) if tau2 > 0 else 0.0
    e["posterior"] = m + shrink * (e["mu"] - m)
    e["shrink"] = shrink if np.isscalar(shrink) else shrink
    return e, tau2, m


def _dispersion(est: pd.DataFrame) -> tuple[float, float]:
    return float(est["z"].var(ddof=1)), float(est["z"].abs().max())


def shift_null(ev: pd.DataFrame, outcomes: pd.DataFrame, h: int, n_min: int, B: int = 300, seed: int = 3):
    """Null for (D, max|z|): circularly shift each ticker's outcome series by a random offset, re-attach to the (unchanged) events, recompute."""
    o = outcomes.sort_values(["ticker", "date"]).reset_index(drop=True).copy()
    col = f"rel_{h}d"
    o["y"] = o[col] - o.groupby("ticker")[col].transform("mean")
    pos = o.reset_index().set_index(["ticker", "date"])["index"]
    idx = pos.reindex(pd.MultiIndex.from_frame(ev[["ticker", "date"]])).to_numpy()
    ok = ~np.isnan(idx)
    idx = idx[ok].astype(int)
    e = ev.loc[ok, ["ticker", "broker"]].reset_index(drop=True)
    groups = [np.where(o["ticker"].to_numpy() == t)[0] for t in o["ticker"].unique()]
    rng = np.random.default_rng(seed)
    y0 = o["y"].to_numpy()
    out = np.zeros((B, 2))
    for b in range(B):
        yp = y0.copy()
        for gidx in groups:
            if len(gidx) > 2:
                yp[gidx] = y0[gidx][np.roll(np.arange(len(gidx)), int(rng.integers(1, len(gidx))))]
        tmp = e.assign(**{f"y{h}": yp[idx]})
        est = broker_estimates(tmp, h, n_min)
        out[b] = _dispersion(est) if len(est) > 2 else (np.nan, np.nan)
    return out


def persistence(ev: pd.DataFrame, h: int, n_min: int, blocks: int = 2, B: int = 2000, seed: int = 9):
    dates = np.sort(ev["date"].unique())
    cuts = np.array_split(dates, blocks)
    ests = []
    for c in cuts:
        sub = ev[ev["date"].isin(set(c))]
        ests.append(broker_estimates(sub, h, max(10, n_min // blocks)).set_index("broker")["mu"])
    both = pd.concat(ests, axis=1, join="inner").dropna()
    if len(both) < 6:
        return float("nan"), float("nan"), len(both)
    rho = float(both.iloc[:, 0].rank().corr(both.iloc[:, 1].rank()))
    rng = np.random.default_rng(seed)
    r2 = both.iloc[:, 1].rank().to_numpy()
    r1 = both.iloc[:, 0].rank().to_numpy()
    perm = np.array([np.corrcoef(r1, rng.permutation(r2))[0, 1] for _ in range(B)])
    return rho, float((1 + np.sum(perm >= rho)) / (B + 1.0)), len(both)


def run(raw: pd.DataFrame, outcomes: pd.DataFrame, side: str = "buy", top_n: int = 1, n_min: int = 40, B: int = 300, verbose: bool = True) -> dict:
    ev = build_events(raw, outcomes, side, top_n)
    res = {}
    for h in HORIZONS:
        est = broker_estimates(ev, h, n_min)
        if len(est) < 4:
            continue
        post, tau2, m = eb_shrink(est)
        D, mz = _dispersion(est)
        nul = shift_null(ev, outcomes, h, n_min, B=B)
        p_D = float((1 + np.sum(nul[:, 0] >= D)) / (B + 1.0))
        p_m = float((1 + np.sum(nul[:, 1] >= mz)) / (B + 1.0))
        rho, p_rho, nb = persistence(ev, h, n_min)
        res[h] = {"est": post, "tau2": tau2, "D": D, "p_D": p_D, "max_z": mz, "p_maxz": p_m, "rho": rho, "p_rho": p_rho, "n_rho": nb}
        if verbose:
            print(f"\n--- side={side} top_n={top_n} horizon={h}d | brokers={len(est)} events={int(est['n'].sum())} | tau = {np.sqrt(tau2) * 100:.2f}% "
                  f"(EB spread of true effects; 0 = pure noise)")
            print(f"    dispersion of z: D={D:.2f} (shift-null P95 {np.nanpercentile(nul[:, 0], 95):.2f}) p={p_D:.3f} | max|z|={mz:.2f} (null P95 "
                  f"{np.nanpercentile(nul[:, 1], 95):.2f}) p={p_m:.3f} | persistence rho={rho:.2f} (n={nb} brokers, perm p={p_rho:.3f})")
            show = post.sort_values("posterior", ascending=False)
            print("    top 4 by posterior:", ", ".join(f"{r.broker} {r.posterior * 100:+.2f}% (raw {r.mu * 100:+.2f}%, z {r.z:+.1f}, n={r.n})" for r in show.head(4).itertuples()))
            print("    bottom 3        :", ", ".join(f"{r.broker} {r.posterior * 100:+.2f}% (raw {r.mu * 100:+.2f}%, z {r.z:+.1f}, n={r.n})" for r in show.tail(3).itertuples()))
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=common.IPOT_DB)
    ap.add_argument("--B", type=int, default=300)
    ap.add_argument("--top-n", type=int, default=1)
    ap.add_argument("--n-min", type=int, default=40)
    a = ap.parse_args()
    raw = common.load_ipot(a.db)
    px_all = common.load_ohlcv(start="2025-01-01")
    out = features.outcomes(px_all)
    out = out[out["ticker"].isin(set(raw["ticker"].unique()))]
    for side in ("buy", "sell"):
        run(raw, out, side=side, top_n=a.top_n, n_min=a.n_min, B=a.B)
    print("\n(Dev data: 30 tickers x ~3 weeks. 'No skill detected' here is not evidence of no skill; always read the detectable size via the positive-control test.)")
