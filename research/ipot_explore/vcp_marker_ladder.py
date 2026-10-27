"""
research/ipot_explore/vcp_marker_ladder.py — Path-1 test on a REBUILT, progressively loosened VCP signal: does the broker-footprint marker (cross-population score of mover_rule_walkforward)
add lift inside the VCP population, and does the lift survive when the VCP gates are relaxed to get more n?

Signal rebuilt from OHLCV exactly as engine/vcp_pillar._compute_latest_features (3-stage range contraction r15>r10>r5 & r5<0.7*r15, 5d vol < 15d vol, within -10% of the 20d high, RSI14 Wilder,
ATR%20, 20d value traded, MACD hist % of price). NOT included: the foreign-flow gate and the RS20d gate (no history on the IPOT universe) -> V0 is slightly looser than production.
Ladder (fixed before running): V0 prod-like (RSI>=65, ATR20<=10, liq 0.15-30B, MACD -0.5..2.75) | V1 RSI>=55 | V2 V1 without the MACD gate | V3 no RSI/MACD gate | V4 V3 without volume dry-up
| V5 only contraction (r15>r10>r5) & within -15% of the 20d high, ATR20<=10, liq range.
Mechanic: entry open(t+1); TP1 +2.5% or TP3 +7%, SL -10%, expire at the close of day 12 (a same-day touch of both counts as the stop). Marker = cross-population score >= train P80:
top150 rows scored by the rule trained on the outside universe and vice versa. Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.vcp_marker_ladder
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.ipot_explore import common
from research.ipot_explore.mover_footprint import panel, TOP, OUT, SKIP
from research.ipot_explore.mover_rule_walkforward import realised, SIZE
from research.ipot_explore.mover_rule_by_regime import selected


def vcp_flags(px: pd.DataFrame) -> pd.DataFrame:
    out = []
    for tk, g in px.sort_values(["ticker", "date"]).groupby("ticker"):
        if len(g) < 100:
            continue
        c, h, l, v = g["close"], g["high"], g["low"], g["volume"]
        dlt = c.diff(); up = dlt.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-dlt.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
        macd = c.rolling(12).mean() - c.rolling(26).mean(); hist = macd - macd.ewm(span=9, adjust=False).mean(); mp = hist / c * 100
        pc = c.shift(1); tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1); atr = (tr / c * 100).rolling(20).mean()
        liq = (c * v).rolling(20).mean()
        r1 = (h.rolling(15).max() - l.rolling(15).min()) / c; r2 = (h.rolling(10).max() - l.rolling(10).min()) / c; r3 = (h.rolling(5).max() - l.rolling(5).min()) / c
        dry = v.rolling(5).mean() < v.rolling(15).mean(); h20 = h.rolling(20).max(); dist = (c - h20) / h20
        contr = (r1 > r2) & (r2 > r3); tight = contr & (r3 < r1 * 0.7)
        base = atr <= 10; liqok = (liq >= 0.15e9) & (liq < 30e9)
        f = pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy()})
        f["V0"] = (tight & dry & (dist > -0.10) & (rsi >= 65) & base & liqok & (mp >= -0.5) & (mp < 2.75)).to_numpy()
        f["V1"] = (tight & dry & (dist > -0.10) & (rsi >= 55) & base & liqok & (mp >= -0.5) & (mp < 2.75)).to_numpy()
        f["V2"] = (tight & dry & (dist > -0.10) & (rsi >= 55) & base & liqok).to_numpy()
        f["V3"] = (tight & dry & (dist > -0.10) & base & liqok).to_numpy()
        f["V4"] = (tight & (dist > -0.10) & base & liqok).to_numpy()
        f["V5"] = (contr & (dist > -0.15) & base & liqok).to_numpy()
        out.append(f)
    return pd.concat(out, ignore_index=True)


def vcp_returns(px: pd.DataFrame, hor: int = 12, sl: float = -0.10) -> pd.DataFrame:
    rows = []
    for tk, g in px.sort_values(["ticker", "date"]).groupby("ticker"):
        o, h, l, c = g["open"].to_numpy(), g["high"].to_numpy(), g["low"].to_numpy(), g["close"].to_numpy(); n = len(g)
        r1 = np.full(n, np.nan); r3 = np.full(n, np.nan)
        for i in range(n - 1 - hor):
            e = o[i + 1]
            if not e > 0:
                continue
            for tp, arr in ((0.025, r1), (0.07, r3)):
                r = c[i + hor] / e - 1
                for k in range(i + 1, i + hor + 1):
                    if l[k] / e - 1 <= sl:
                        r = sl; break
                    if h[k] / e - 1 >= tp:
                        r = tp; break
                arr[i] = r
        rows.append(pd.DataFrame({"ticker": tk, "date": g["date"].to_numpy(), "r_tp1": r1, "r_tp3": r3}))
    return pd.concat(rows, ignore_index=True)


def diff_t(d, col):
    on, off = d[d.sel], d[~d.sel]
    x = d.sel.astype(float) - d.sel.astype(float).mean(); y = d[col]
    b = (x * y).sum() / (x * x).sum(); e = y - y.mean() - b * x
    cs = (x * e).groupby(d.ticker).sum(); t = b / (np.sqrt((cs ** 2).sum()) / (x * x).sum())
    return on[col].mean() - off[col].mean(), t


def main():
    px_all = common.load_ohlcv(start="2025-01-01")
    flags = vcp_flags(px_all); vr = vcp_returns(px_all)
    rl = realised(common.load_ohlcv(start="2025-06-01"))
    d1 = panel(TOP, 3, common.OUT_DIR + "/features.pkl").merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"]).reset_index(drop=True)
    d2 = panel(OUT, 2, None).merge(rl, on=["ticker", "date"], how="left").dropna(subset=["ret"]).reset_index(drop=True)
    feats = [c for c in d1.columns if c in d2.columns and c not in SKIP and c != "ret" and not c.endswith(SIZE) and c not in ("value", "volume")
             and (pd.api.types.is_numeric_dtype(d1[c]) or pd.api.types.is_bool_dtype(d1[c]))]
    pops = {"top150 (marker trained on outside)": selected(d2, d1, feats)[0], "outside (marker trained on top150)": selected(d1, d2, feats)[0]}
    for pn, d in pops.items():
        d = d.merge(flags, on=["ticker", "date"], how="left").merge(vr, on=["ticker", "date"], how="left")
        print(f"\n===== {pn}: {len(d)} rows, marker ON {100 * d.sel.mean():.0f}% =====")
        print("  step  | VCP signals (rows, tickers) | marker ON n / OFF n | TP1 mechanic mean ret ON vs OFF (diff, t) | TP3 mechanic ON vs OFF (diff, t) | all-VCP mean ret TP1/TP3")
        d["ALL"] = True
        for v in ("V0", "V1", "V2", "V3", "V4", "V5", "ALL"):
            s = d[d[v].fillna(False)].dropna(subset=["r_tp1", "r_tp3"])
            if len(s) < 60 or s.sel.sum() < 25 or (~s.sel).sum() < 25:
                print(f"  {v}    | n={len(s)} (ON {int(s.sel.sum())}) too few"); continue
                
            a1, t1 = diff_t(s, "r_tp1"); a3, t3 = diff_t(s, "r_tp3")
            print(f"  {v}    | {len(s):>5}, {s.ticker.nunique():>3} | {int(s.sel.sum()):>4} / {int((~s.sel).sum()):>4} | {100 * s[s.sel].r_tp1.mean():+.2f}% vs {100 * s[~s.sel].r_tp1.mean():+.2f}% ({100 * a1:+.2f}pp, t {t1:+.1f}) "
                  f"| {100 * s[s.sel].r_tp3.mean():+.2f}% vs {100 * s[~s.sel].r_tp3.mean():+.2f}% ({100 * a3:+.2f}pp, t {t3:+.1f}) | {100 * s.r_tp1.mean():+.2f}% / {100 * s.r_tp3.mean():+.2f}%")


if __name__ == "__main__":
    main()
