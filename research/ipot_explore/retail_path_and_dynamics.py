"""
research/ipot_explore/retail_path_and_dynamics.py — experiment family EXP-DYN-1 (registered before running).
Part B  retail-FOMO timing. Event = a retail-app broker (XL, XC, YP, PD; each alone and pooled) is the #1 net buyer of a ticker-day, first day of its streak (not #1 in the previous 3 trading days).
        Event-time profile of the RELATIVE daily return (stock minus the liquid-universe median of the same date) for event days -5..+10, with day +1 split into the overnight gap (close t -> open t+1) and the intraday part
        (open t+1 -> close t+1). Compared with matched non-event stock-days (same time-tercile x ATR-tercile cell). Shows WHERE the move happens relative to the retail entry and whether a day-1/2 gain exists that an
        open(t+1) entry could still catch.
Part A  broker dynamics (non-retail brokers AK BK ZP YU KZ RX CC OD NI DX MG CP GA): recent = net buying share over the last 10 days, prior = share over days -40..-11 (both need >= 70% data coverage).
        TURN_UP   prior <= -1% and recent >= +1% (a seller turns into a buyer) | TURN_DOWN  prior >= +1% and recent <= -1% (a buyer turns into a seller) | DELTA  recent - prior, standardised (continuous).
        Outcome rel_h (h = 5, 10, 20) in pp with ticker AND date fixed effects, controls, two-way clustered t. Family: 13 brokers x 3 defs x 3 horizons = 117, plus pooled TURN_UP / TURN_DOWN 2 x 3 = 6; BH over 123.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.retail_path_and_dynamics
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common, features
from research.ipot_explore import gorengan_trade as gt
from research.ipot_explore.breakout_backing import norm_p, bh
from research.ipot_explore.lead_1d import demean2, twoway_t
from research.ipot_explore.mover_footprint import TOP, OUT

RETAIL = ["XL", "XC", "YP", "PD"]
NONRETAIL = ["AK", "BK", "ZP", "YU", "KZ", "RX", "CC", "OD", "NI", "DX", "MG", "CP", "GA"]
CTRL = ["log_val20", "atr_pct", "past_ret_10d", "pret_1d", "pret_5d", "vol_ratio", "range60_pos", "log_close"]


def broker_top_buyer(raw: pd.DataFrame, brokers: list[str]) -> pd.DataFrame:
    a = features.broker_day_table(raw, "all", "all")[["ticker", "date", "broker", "net_val_mid"]].dropna()
    a = a[a["net_val_mid"] > 0].copy()
    a["rk"] = a.groupby(["ticker", "date"])["net_val_mid"].rank(ascending=False, method="first")
    return a[(a.rk == 1) & a.broker.isin(brokers)][["ticker", "date", "broker"]].drop_duplicates(["ticker", "date"])


def part_b(f, px_all, raw):
    p = px_all.sort_values(["ticker", "date"]).copy()
    g = p.groupby("ticker")
    p["c2c"] = g["close"].pct_change()
    p["gap"] = p["open"] / g["close"].shift(1) - 1
    p["intra"] = p["close"] / p["open"] - 1
    p["val20"] = (p["close"] * p["volume"]).groupby(p["ticker"]).transform(lambda s: s.rolling(20, min_periods=10).mean())
    liq = p[p["val20"] >= 1e9]
    for c in ("c2c", "gap", "intra"):
        p = p.merge(liq.groupby("date")[c].median().rename("m_" + c), on="date", how="left")
        p["r_" + c] = p[c] - p["m_" + c]
    byt = {tk: x.reset_index(drop=True) for tk, x in p.groupby("ticker")}
    tdi_ok = set(f.ticker.unique())
    ev_all = broker_top_buyer(raw, RETAIL)
    u = np.sort(f["date"].unique())
    k = len(u) // 3
    f = f.copy()
    f["tcl"] = np.where(f["date"] <= u[k], 1, np.where(f["date"] <= u[2 * k], 2, 3))
    f["atrq"] = pd.qcut(f["atr_pct"], 3, labels=False, duplicates="drop")
    cell = f.set_index(["ticker", "date"])[["tcl", "atrq"]]
    days = list(range(-5, 11))

    def profile(events: pd.DataFrame):
        P, GAP, INT, keys = [], [], [], []
        for r in events.itertuples():
            x = byt.get(r.ticker)
            if x is None or r.ticker not in tdi_ok:
                continue
            i = int(np.searchsorted(x["date"].to_numpy(), np.datetime64(r.date)))
            if i < 5 or i + 10 >= len(x) or x["date"].iloc[i] != r.date:
                continue
            rr = x["r_c2c"].to_numpy()[i - 5:i + 11]
            if np.isnan(rr).sum() > 3:
                continue
            P.append(rr)
            GAP.append(x["r_gap"].iloc[i + 1])
            INT.append(x["r_intra"].iloc[i + 1])
            keys.append((r.ticker, r.date))
        return np.array(P), np.array(GAP), np.array(INT), keys

    print("PART B  event-time profile of the relative daily return (bp); day +1 split into gap / intraday; control = same time-tercile x ATR-tercile cell, all non-event stock-days")
    for label, brokers in (("XL", ["XL"]), ("XC", ["XC"]), ("YP", ["YP"]), ("PD", ["PD"]), ("RETAIL pooled", RETAIL)):
        ev = ev_all[ev_all.broker.isin(brokers)].sort_values(["ticker", "date"]).copy()
        ev["prev"] = ev.groupby("ticker")["date"].shift(1)
        # first day of the streak: not an event within the previous 3 trading days (approximated by 5 calendar days)
        ev = ev[ev["prev"].isna() | ((ev["date"] - ev["prev"]).dt.days > 5)][["ticker", "date"]]
        P, GAP, INT, keys = profile(ev)
        if len(P) < 40:
            print(f"   {label}: only {len(P)} events")
            continue
        # control: draw the same number of random non-event stock-days with the same cells, same days window
        rng = np.random.default_rng(7)
        evset = set(keys)
        cells = [cell.loc[k_] for k_ in keys if k_ in cell.index]
        pool = f[["ticker", "date", "tcl", "atrq"]].copy()
        ctrl_keys = []
        for c in cells:
            cand = pool[(pool.tcl == c.tcl) & (pool.atrq == c.atrq)]
            j = rng.integers(0, len(cand))
            ctrl_keys.append((cand.ticker.iloc[j], cand.date.iloc[j]))
        Pc, GAPc, INTc, _ = profile(pd.DataFrame(ctrl_keys, columns=["ticker", "date"]))
        mean = np.nanmean(P, axis=0) * 1e4
        meanc = np.nanmean(Pc, axis=0) * 1e4
        se = np.nanstd(P, axis=0) / np.sqrt(len(P)) * 1e4
        print(f"\n   {label}: events {len(P)} (controls {len(Pc)})")
        print("     day:        " + " ".join(f"{d:>6}" for d in days))
        print("     event bp:   " + " ".join(f"{v:>6.0f}" for v in mean))
        print("     control bp: " + " ".join(f"{v:>6.0f}" for v in meanc))
        print("     t vs 0:     " + " ".join(f"{(v / s):>6.1f}" if s > 0 else "     -" for v, s in zip(mean, se)))
        cum = np.cumsum(np.nan_to_num(mean[days.index(1):]))
        print(f"     cumulative from day +1: d1 {cum[0]:.0f} d2 {cum[1]:.0f} d3 {cum[2]:.0f} d5 {cum[4]:.0f} d10 {cum[9]:.0f} bp | pre-event cumulative days -5..0: {np.nansum(mean[:6]):.0f} bp")
        print(f"     day+1 gap {np.nanmean(GAP) * 1e4:+.0f} bp (control {np.nanmean(GAPc) * 1e4:+.0f}) | day+1 intraday {np.nanmean(INT) * 1e4:+.0f} bp (control {np.nanmean(INTc) * 1e4:+.0f})")


def dyn_shares(raw: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    a = features.broker_day_table(raw, "all", "all")[["ticker", "date", "broker", "net_val_mid"]].dropna()
    a = a[a.broker.isin(NONRETAIL)].groupby(["ticker", "date", "broker"], as_index=False)["net_val_mid"].sum()
    val = px.assign(value=px["close"] * px["volume"]).set_index(["ticker", "date"])["value"]
    have = a[["ticker", "date"]].drop_duplicates()
    out = []
    for tk, g in have.groupby("ticker"):
        dts = pd.DatetimeIndex(sorted(px.loc[px.ticker == tk, "date"].unique()))
        if len(dts) < 60:
            continue
        cov = pd.Series(dts.isin(g["date"]), index=dts).astype(float)
        v = val.xs(tk, level="ticker").reindex(dts).fillna(0.0) * cov
        nt = a[a.ticker == tk].pivot_table(index="date", columns="broker", values="net_val_mid", aggfunc="sum").reindex(dts).fillna(0.0)
        den_r, cov_r = v.rolling(10).sum(), cov.rolling(10).sum()
        den_p, cov_p = v.shift(10).rolling(30).sum(), cov.shift(10).rolling(30).sum()
        ok = (cov_r >= 7) & (cov_p >= 21)
        rec = nt.rolling(10).sum().div(den_r.where(den_r > 0), axis=0)
        pri = nt.shift(10).rolling(30).sum().div(den_p.where(den_p > 0), axis=0)
        for b in nt.columns:
            r_, p_ = rec[b].where(ok), pri[b].where(ok)
            m = r_.notna() & p_.notna()
            if m.any():
                out.append(pd.DataFrame({"ticker": tk, "date": dts[m.to_numpy()], "broker": b, "recent": r_[m].to_numpy(), "prior": p_[m].to_numpy()}))
    return pd.concat(out, ignore_index=True)


def reg(d0, evcol, h):
    ycol = f"rel_{h}d"
    d = d0.dropna(subset=[ycol] + CTRL).copy()
    d["y"] = d[ycol] * 100
    M = demean2(d[[evcol] + CTRL + ["y"]].to_numpy(float), d["ticker"].to_numpy(), d["date"].to_numpy())
    coef, t = twoway_t(M[:, :-1], M[:, -1], d["ticker"].to_numpy(), d["date"].to_numpy(), 0)
    u = np.sort(d["date"].unique())
    half = d["date"] <= u[len(u) // 2]
    sg = []
    for m in (half, ~half):
        x = d[m]
        on = x[evcol] > (0.5 if x[evcol].max() <= 1 else x[evcol].quantile(0.8))
        sg.append(float(np.sign(x[on]["y"].mean() - x[~on]["y"].mean())) if on.sum() >= 10 else 0.0)
    return coef, t, sg


def part_a(f, px_all, raw):
    sub = px_all[px_all.ticker.isin(f.ticker.unique())]
    S = dyn_shares(raw, sub)
    print(f"\nPART A  broker dynamics: rows {len(S)}")
    p = px_all.sort_values(["ticker", "date"]).copy()
    p["val20"] = (p["close"] * p["volume"]).groupby(p["ticker"]).transform(lambda s: s.rolling(20, min_periods=10).mean())
    entry = p.groupby("ticker")["open"].shift(-1)
    p["ret_20d"] = p.groupby("ticker")["close"].shift(-20) / entry - 1
    p = p.merge(p[p["val20"] >= 1e9].groupby("date")["ret_20d"].median().rename("med20"), on="date", how="left")
    p["rel_20d"] = p["ret_20d"] - p["med20"]
    f = f.merge(p[["ticker", "date", "rel_20d"]], on=["ticker", "date"], how="left")
    rows = []
    poolU, poolD = [], []
    for b in NONRETAIL:
        g = S[S.broker == b].copy()
        if len(g) < 500:
            continue
        g["TURN_UP"] = ((g.prior <= -0.01) & (g.recent >= 0.01)).astype(float)
        g["TURN_DOWN"] = ((g.prior >= 0.01) & (g.recent <= -0.01)).astype(float)
        dl = (g.recent - g.prior)
        lo, hi = dl.quantile([0.01, 0.99])
        dl = dl.clip(lo, hi)
        g["DELTA"] = (dl - dl.mean()) / dl.std()
        poolU.append(g[g.TURN_UP == 1][["ticker", "date"]])
        poolD.append(g[g.TURN_DOWN == 1][["ticker", "date"]])
        for defn in ("TURN_UP", "TURN_DOWN", "DELTA"):
            d0 = f.merge(g[["ticker", "date", defn]], on=["ticker", "date"], how="left")
            d0[defn] = d0[defn].fillna(0.0)
            if defn != "DELTA" and d0[defn].sum() < 40:
                continue
            for h in (5, 10, 20):
                coef, t, sg = reg(d0, defn, h)
                rows.append(dict(broker=b, defn=defn, h=h, n_ev=int(d0[defn].sum()) if defn != "DELTA" else len(d0), coef=coef, t=t, halves=sg))
    for name, lst in (("TURN_UP", poolU), ("TURN_DOWN", poolD)):
        ev = pd.concat(lst).drop_duplicates().assign(E=1.0)
        d0 = f.merge(ev, on=["ticker", "date"], how="left")
        d0["E"] = d0["E"].fillna(0.0)
        for h in (5, 10, 20):
            coef, t, sg = reg(d0, "E", h)
            rows.append(dict(broker="NONRETAIL", defn="POOL_" + name, h=h, n_ev=int(d0.E.sum()), coef=coef, t=t, halves=sg))
    G = pd.DataFrame(rows)
    G["p"] = G["t"].map(norm_p)
    G["bh"] = bh(G["p"].to_numpy())
    print(f"FAMILY EXP-DYN-1 Part A: {len(G)} tests | nominal |t|>=2: {int((G.t.abs() >= 2).sum())} (chance ~{0.05 * len(G):.0f}) | |t|>=3: {int((G.t.abs() >= 3).sum())} | BH survivors: {int(G.bh.sum())}")
    pd.set_option("display.width", 200)
    print(G.assign(a=G.t.abs()).sort_values("a", ascending=False).head(14)[["broker", "defn", "h", "n_ev", "coef", "t", "halves", "bh"]].round(3).to_string(index=False))
    print("\npooled non-retail:")
    print(G[G.broker == "NONRETAIL"].round(3).to_string(index=False))


def main():
    f, pxs, px_all, tdi = gt.build()
    raw = pd.concat([common.load_ipot(TOP), common.load_ipot(OUT)], ignore_index=True)
    part_b(f, px_all, raw)
    part_a(f, px_all, raw)


if __name__ == "__main__":
    main()
