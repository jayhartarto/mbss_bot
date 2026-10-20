"""2026-10-20: HSC-exclusion reference backtest for live /swing + /bsjp lanes.
Method: (a) runner `research/hsc_runner.py <base|nohsc|hsconly> <lane script>` re-runs each existing validated
lane script with the OHLCV universe (and 1m DB) filtered by research/hsc_list_2026_10_13.csv (80 tickers, static,
whole history); BASE = same script unfiltered, so only HSC differs. Lane scripts used: BOW v3 =
.tmp_bow_v3_tp_overshoot_sweep_2026_10_14 (TP%B>=1.0 row), BOS = .tmp_bos_bounceoffloW_variant (copy of
.tmp_ret10d_regime_robustness_check with regime=BOUNCE_OFF_LOW), VCP = vcp_drift_since_entry_2026_10_11,
BSJP Lane1 = bsjp_lane1_absolute_gate_confirm_2026_10_07, BSJP Lane2 = bsjp_lane2_absolute_gate_confirm_2026_10_07.
(b) this file: ticker-slice of cached trade populations (MACD-confirm TP8/SL6/mh5 in BOUNCE_OFF_LOW; Lane2 daily proxy)."""
import numpy as np, pandas as pd
HSC = set(pd.read_csv("research/hsc_list_2026_10_13.csv").ticker)

def st(rets, days=None):
    r = np.asarray(rets, float)
    if len(r) == 0: return "n=0"
    s = f"n={len(r):5d} win={np.mean(r>0)*100:5.1f}% mean={r.mean():+.2f}% med={np.median(r):+.2f}%"
    if days is not None: s += f" ret/day={r.sum()/np.sum(days):+.3f}"
    return s

def sim(row, sl=6, tp=8, mh=5):
    ep = row["entry_ref"]; sl_abs = ep*(1-sl/100); tp_abs = ep*(1+tp/100)
    hi, lo, cl = row["fwd_high"], row["fwd_low"], row["fwd_close"]
    nd = min(len(cl), mh-1)
    for k in range(nd):
        if lo[k] <= sl_abs: return -sl, k+2
        if hi[k] >= tp_abs: return tp, k+2
    if len(cl) == 0: return None
    i = max(nd-1, 0); return (cl[i]-ep)/ep*100, i+2

d = pd.read_pickle("research/.tmp_macd_d1_featured_population.pkl")
d = d[d.regime_phase == "BOUNCE_OFF_LOW"].copy()
res = [sim(r) for _, r in d.iterrows()]
d["ret"] = [x[0] if x else np.nan for x in res]; d["days"] = [x[1] if x else np.nan for x in res]; d = d.dropna(subset=["ret"])
d["hsc"] = d.ticker.isin(HSC)
print("MACD-confirm BOUNCE_OFF_LOW TP8/SL6/mh5 (note: pickle population stage-1 episodes, entry=trigger close)")
for lab, s in (("BASE", d), ("NO-HSC", d[~d.hsc]), ("HSC-only", d[d.hsc])): print(f"  {lab:9s}", st(s.ret, s.days))
print("  HSC tickers in population:", d[d.hsc].ticker.nunique(), "share of trades %.1f%%" % (d.hsc.mean()*100))

p = pd.read_csv("research/.tmp_bsjp_lane2_daily_proxy_episodes.csv"); p["hsc"] = p.ticker.isin(HSC)
print("BSJP Lane2 daily proxy (TP5/SL5)")
for lab, s in (("BASE", p), ("NO-HSC", p[~p.hsc]), ("HSC-only", p[p.hsc])): print(f"  {lab:9s}", st(s.ret_pct))

# BSJP Lane1 by tier / VCP (episode files written by the lane scripts; ticker slice)
l = pd.read_csv("research/.tmp_bsjp_lane1_episodes_with_tier.csv"); l["hsc"] = l.ticker.isin(HSC)
print("BSJP Lane1 (Stage-1 confirmed) by tier")
for t in ["ALL", "STAGE1_BASE", "TIER3", "TIER_EXTREME"]:
    s = l if t == "ALL" else l[l.tier == t]
    for lab, x in (("BASE", s), ("NO-HSC", s[~s.hsc]), ("HSC-only", s[s.hsc])): print(f"  {t:13s}{lab:9s}", st(x.ret_pct))
v = pd.read_pickle("research/.tmp_vcp_drift_episodes_2026_10_11.pkl"); v["hsc"] = v.ticker.isin(HSC)
print("VCP (TP1+2.5/SL-10/12d)")
for lab, x in (("BASE", v), ("NO-HSC", v[~v.hsc]), ("HSC-only", v[v.hsc])):
    print(f"  {lab:9s}", st(x.ret_pct), f"TP1={np.mean(x.exit_kind=='TP1')*100:.1f}% SL={np.mean(x.exit_kind=='SL')*100:.1f}%")
for nm, df_ in (("MACD", d), ("Lane1", l), ("VCP", v)):
    print(f"Top HSC tickers in {nm}:", df_[df_.hsc].ticker.value_counts().head(8).to_dict())
