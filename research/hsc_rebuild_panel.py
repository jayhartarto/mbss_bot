"""Rebuild research/.tmp_lane_validate_panel_v2.pkl per HSC variant at UNIVERSE level
(filter ohlcv+foreign flow BEFORE feature build, so cross-sectional features such as
net_ratio_cs_pctrank are recomputed on the filtered universe). Trimmed to <=2026-10-07
to match the stale original panel. usage: python research/hsc_rebuild_panel.py <base|nohsc|hsconly>"""
import sys, os
import pandas as pd
sys.path.insert(0, "research"); sys.path.insert(0, ".")
import ff_booster_validate_lanes_2026_10_07 as fb
import ff_feature_enrich_2026_10_07 as fe
variant = sys.argv[1]
HSC = set(pd.read_csv("research/hsc_list_2026_10_13.csv").ticker)
def flt(df):
    if variant == "nohsc": return df[~df.ticker.isin(HSC)].reset_index(drop=True)
    if variant == "hsconly": return df[df.ticker.isin(HSC)].reset_index(drop=True)
    return df
ff, ohlcv, ihsg = fb.load_data()
cut = pd.Timestamp("2026-10-07")
ohlcv = flt(ohlcv); ohlcv = ohlcv[ohlcv.date <= cut]
ff = flt(ff); ff = ff[ff.date <= cut]
panel = fb.build_common_panel(ff, ohlcv, ihsg)
out = {}
pd.read_pickle = lambda p, *a, **k: panel.copy()
_orig_tp = pd.DataFrame.to_pickle
pd.DataFrame.to_pickle = lambda self, p, *a, **k: out.setdefault("df", self)
fe.main()
pd.DataFrame.to_pickle = _orig_tp
os.makedirs("research/.hsc_out", exist_ok=True)
d = out["df"]; d = d[d.date >= pd.Timestamp("2024-08-21")]; out["df"] = d
d.to_pickle(f"research/.hsc_out/panel_{variant}.pkl")
print(variant, out["df"].shape, out["df"].ticker.nunique(), out["df"].date.max())
