"""Run a research script with the OHLCV universe filtered by HSC membership.
usage: python research/hsc_runner.py <base|nohsc|hsconly> <script.py>
Patches pandas.read_csv (and sqlite3-agnostic) so ANY module the script imports that
reads research/ohlcv_backtest_raw.csv gets the filtered universe. Production code untouched."""
import sys, runpy, os
import pandas as pd
variant, script = sys.argv[1], sys.argv[2]
HSC = set(pd.read_csv("research/hsc_list_2026_10_13.csv").ticker)
_orig = pd.read_csv
def patched(path, *a, **k):
    df = _orig(path, *a, **k)
    if isinstance(path, (str, os.PathLike)) and str(path).replace("\\", "/").endswith("ohlcv_backtest_raw.csv") and "ticker" in getattr(df, "columns", []):
        if variant == "nohsc": df = df[~df.ticker.isin(HSC)].reset_index(drop=True)
        elif variant == "hsconly": df = df[df.ticker.isin(HSC)].reset_index(drop=True)
    return df
pd.read_csv = patched
_osq = pd.read_sql_query
def patched_sql(sql, *a, **k):
    df = _osq(sql, *a, **k)
    if "ohlcv_1m" in str(sql) and "ticker" in df.columns:
        if variant == "nohsc": df = df[~df.ticker.isin(HSC)].reset_index(drop=True)
        elif variant == "hsconly": df = df[df.ticker.isin(HSC)].reset_index(drop=True)
    return df
pd.read_sql_query = patched_sql
_orp = pd.read_pickle
def patched_pkl(path, *a, **k):
    if "lane_validate_panel_v2" in str(path) and os.environ.get("HSC_PANEL"):
        return _orp(f"research/.hsc_out/panel_{variant}.pkl", *a, **k)  # universe-level rebuilt panel
    df = _orp(path, *a, **k)
    if "lane_validate_panel" in str(path) and "ticker" in getattr(df, "columns", []):
        if variant == "nohsc": df = df[~df.ticker.isin(HSC)].reset_index(drop=True)
        elif variant == "hsconly": df = df[df.ticker.isin(HSC)].reset_index(drop=True)
    return df
pd.read_pickle = patched_pkl
sys.argv = [script]
sys.path.insert(0, "research"); sys.path.insert(0, ".")
g = runpy.run_path(script, run_name="__main__")
if os.environ.get("HSC_DUMP"):
    os.makedirs(os.environ["HSC_DUMP"], exist_ok=True)
    for k, v in g.items():
        if isinstance(v, pd.DataFrame) and "ticker" in v.columns and len(v) > 30:
            v.to_pickle(os.path.join(os.environ["HSC_DUMP"], k + ".pkl")); print("DUMPED", k, v.shape, list(v.columns)[:14])
