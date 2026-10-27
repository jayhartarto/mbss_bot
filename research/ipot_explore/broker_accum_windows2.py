"""
research/ipot_explore/broker_accum_windows2.py — experiment family EXP-BRKACC-2 (registered before running): the same per-broker accumulation test as EXP-BRKACC-1 with LOOSER definitions.
Definitions of 'accumulating' (share = net buying over the window / traded value over the same days):
  P80   share >= P80 of the broker's positive shares and >= 0.5%      P90   share >= P90 of them and >= 1%      CONT  continuous: share clipped at the broker's P1/P99, in standard deviations (dose-response)
  POOL  any non-retail broker (AK BK ZP YU KZ RX CC OD NI DX MG CP) at >= P80 of its own positive shares (one event type pooled over brokers), and the max over those brokers of the standardised share (continuous)
Everything else as before: windows 20/40 days, horizons 5/10/20 days (rel return in pp), ticker AND date fixed effects, controls, two-way clustered t.  Family: 16 brokers x 3 defs x 2 windows x 3 horizons = 288, plus POOL 2 x 2 x 3 = 12; BH over all 300.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.broker_accum_windows2
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common
from research.ipot_explore import gorengan_trade as gt
from research.ipot_explore.breakout_backing import norm_p, bh
from research.ipot_explore.broker_accum_windows import window_shares, BROKERS, WINDOWS, HS, CTRL
from research.ipot_explore.lead_1d import demean2, twoway_t
from research.ipot_explore.mover_footprint import TOP, OUT

NONRETAIL = ["AK", "BK", "ZP", "YU", "KZ", "RX", "CC", "OD", "NI", "DX", "MG", "CP"]


def run_reg(d0: pd.DataFrame, evcol: str, h: int):
    ycol = f"rel_{h}d"
    d = d0.dropna(subset=[ycol] + CTRL + [evcol]).copy()
    d["y"] = d[ycol] * 100
    if evcol != "E" and d[evcol].nunique() < 3:
        return None
    M = demean2(d[[evcol] + CTRL + ["y"]].to_numpy(float), d["ticker"].to_numpy(), d["date"].to_numpy())
    coef, t = twoway_t(M[:, :-1], M[:, -1], d["ticker"].to_numpy(), d["date"].to_numpy(), 0)
    u = np.sort(d["date"].unique())
    half = d["date"] <= u[len(u) // 2]
    sg = []
    for m in (half, ~half):
        x = d[m]
        hi = x[evcol] > x[evcol].quantile(0.8) if evcol.startswith("C_") else x[evcol] == 1
        sg.append(float(np.sign(x[hi]["y"].mean() - x[~hi]["y"].mean())) if hi.sum() >= 10 else 0.0)
    return coef, t, sg, int((d[evcol] == 1).sum()) if not evcol.startswith("C_") else len(d), d[evcol].std()


def main():
    f, pxs, px_all, tdi = gt.build()
    raw = pd.concat([common.load_ipot(TOP), common.load_ipot(OUT)], ignore_index=True)
    sub = px_all[px_all.ticker.isin(f.ticker.unique())]
    S = window_shares(raw, sub)
    p = px_all.sort_values(["ticker", "date"]).copy()
    p["val20"] = (p["close"] * p["volume"]).groupby(p["ticker"]).transform(lambda s: s.rolling(20, min_periods=10).mean())
    entry = p.groupby("ticker")["open"].shift(-1)
    p["ret_20d"] = p.groupby("ticker")["close"].shift(-20) / entry - 1
    p = p.merge(p[p["val20"] >= 1e9].groupby("date")["ret_20d"].median().rename("med20"), on="date", how="left")
    p["rel_20d"] = p["ret_20d"] - p["med20"]
    f = f.merge(p[["ticker", "date", "rel_20d"]], on=["ticker", "date"], how="left")
    rows = []
    for W in WINDOWS:
        poolE, poolC = None, None
        for b in BROKERS:
            g = S[(S.broker == b) & (S.W == W)]
            pos = g.loc[g.share > 0, "share"]
            if len(pos) < 300:
                continue
            p80, p90 = max(0.005, float(pos.quantile(0.80))), max(0.01, float(pos.quantile(0.90)))
            lo, hi = g["share"].quantile([0.01, 0.99])
            sc = g["share"].clip(lo, hi)
            z = ((sc - sc.mean()) / sc.std()).rename("zshare")
            gg = g.assign(P80=(g.share >= p80), P90=(g.share >= p90), C_=z)
            if b in NONRETAIL:
                e80 = gg[gg.P80][["ticker", "date"]].assign(Eb=1.0)
                poolE = e80 if poolE is None else pd.concat([poolE, e80])
                cz = gg[["ticker", "date", "C_"]].rename(columns={"C_": "z"})
                poolC = cz if poolC is None else pd.concat([poolC, cz])
            for name, col, thr in (("P80", "P80", p80), ("P90", "P90", p90), ("CONT", "C_", None)):
                if name == "CONT":
                    d0 = f.merge(gg[["ticker", "date", "C_"]], on=["ticker", "date"], how="left")
                    d0["C_"] = d0["C_"].fillna(0.0)
                    evc = "C_"
                else:
                    d0 = f.merge(gg[gg[col]][["ticker", "date"]].assign(E=1.0), on=["ticker", "date"], how="left")
                    d0["E"] = d0["E"].fillna(0.0)
                    evc = "E"
                for h in HS:
                    r = run_reg(d0, evc, h)
                    if r is None:
                        continue
                    coef, t, sg, n_ev, sd = r
                    rows.append(dict(broker=b, W=W, defn=name, h=h, n_ev=n_ev, coef=coef, t=t, halves=sg))
        # pooled over non-retail brokers
        if poolE is not None:
            ev = poolE.drop_duplicates(["ticker", "date"])[["ticker", "date"]].assign(E=1.0)
            d0 = f.merge(ev, on=["ticker", "date"], how="left")
            d0["E"] = d0["E"].fillna(0.0)
            mz = poolC.groupby(["ticker", "date"])["z"].max().rename("C_").reset_index()
            d1 = f.merge(mz, on=["ticker", "date"], how="left")
            d1["C_"] = d1["C_"].fillna(0.0)
            for h in HS:
                for name, d, evc in (("POOL_P80any", d0, "E"), ("POOL_maxZ", d1, "C_")):
                    r = run_reg(d, evc, h)
                    if r is not None:
                        coef, t, sg, n_ev, sd = r
                        rows.append(dict(broker="NONRETAIL", W=W, defn=name, h=h, n_ev=n_ev, coef=coef, t=t, halves=sg))
    G = pd.DataFrame(rows)
    G["p"] = G["t"].map(norm_p)
    G["bh"] = bh(G["p"].to_numpy())
    print(f"FAMILY EXP-BRKACC-2: {len(G)} tests | nominal |t|>=2: {int((G.t.abs() >= 2).sum())} (chance ~{0.05 * len(G):.0f}) | |t|>=3: {int((G.t.abs() >= 3).sum())} | BH q<0.05 survivors: {int(G.bh.sum())}")
    pd.set_option("display.width", 200)
    print("\nstrongest cells:")
    print(G.assign(a=G.t.abs()).sort_values("a", ascending=False).head(18)[["broker", "W", "defn", "h", "n_ev", "coef", "t", "halves", "bh"]].round(3).to_string(index=False))
    print("\nPOSITIVE side only (does ANY non-retail broker show a positive accumulation effect?): top 12 by t")
    print(G[G.broker != "XL"].sort_values("t", ascending=False).head(12)[["broker", "W", "defn", "h", "n_ev", "coef", "t", "halves", "bh"]].round(3).to_string(index=False))
    print("\nper broker, CONT definition (dose-response, pp per +1 sd of accumulation share), averaged over W and h:")
    c = G[G.defn == "CONT"].groupby("broker").agg(coef=("coef", "mean"), best_t=("t", "max"), worst_t=("t", "min")).round(2)
    print(c.to_string())
    print("\nPOOLED non-retail:")
    print(G[G.broker == "NONRETAIL"].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
