"""research/ipot_explore/flow_surprise_build.py — NEW POV (2026-10-27): broker flow as SURPRISE vs the stock's/broker's own history (step 1) and flow-vs-price ABSORPTION (step 2),
plus continuous stock-type descriptors (step 3 as modifiers, never hard segments). All point-in-time (baselines use data strictly before the 5-day window).
Per ticker-day t (known after close):
  z_*      : broker's 5d net share (net_lo / volume) minus its own mean over the prior 60 windows, / std (floor 0.5% of volume)   [zmax_* = most positive, zmin_* = most negative]
  zr_<role>: same but for the role sum
  nz2      : number of brokers with z>=2 ;  new_buy : share of volume bought (5d net>0) by brokers whose |net| over the prior 20d was < 0.25% of volume
  top1_chg : top net buyer of last 5d differs from top net buyer of prior 20d
  inst5    : foreign_inst+gov_inst 5d net share ; ret5_past ; cb_gap_inst/retail : close vs 5d volume-weighted buy price of that group (listed buys)
  type     : atr60, liq60, gorengan120 (share of days |ret|>7%), foreign_share60, retail_share60, top3conc60
Outcomes joined from dna_panel2: ret1, ret5, hi5, regime, atr_cell.
Run: PYTHONUTF8=1 PANEL=<dna_panel2.pkl> OUT=<pkl> PYTHONPATH=. python research/ipot_explore/flow_surprise_build.py"""
import os, sys, sqlite3, warnings, numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
from engine.broker_roles import role_of
ROOT = os.getcwd(); ROLES = ('foreign_inst', 'gov_inst', 'foreign_retail_base', 'retail_app', 'scalper', 'local_other')
c = sqlite3.connect(os.path.join(ROOT, 'research', 'ipot_merged.sqlite'))
d = pd.read_sql_query("SELECT ticker, trade_date date, broker_code broker, buy_lot, buy_avg, sell_lot FROM ipot_broker_daily WHERE fd='all' AND board='all'", c); c.close()
c = sqlite3.connect(os.path.join(ROOT, 'mbss_ohlcv.db'))
o = pd.read_sql_query("SELECT ticker, date, open, high, low, close, volume FROM ohlcv_daily WHERE date>='2022-10-01' ORDER BY ticker, date", c); c.close()
ms = d.groupby(['ticker', 'date']).sell_lot.transform('min').fillna(0); sl_ = d.sell_lot.fillna(0); bl_ = d.buy_lot.fillna(0)
d['net_lo'] = np.where(d.buy_lot.isna(), -sl_, bl_ - np.where(d.sell_lot.isna(), ms, sl_))
d['bval'] = bl_ * d.buy_avg.fillna(0); d['bl'] = bl_
dg = {t: x for t, x in d.groupby('ticker')}
rows = []
for t, g in o.groupby('ticker'):
    if t not in dg or len(g) < 160: continue
    g = g.reset_index(drop=True); cal = g.date.values; cl = g.close; vol = (g.volume / 100.0)
    bd = dg[t]
    pv = bd.pivot_table(index='date', columns='broker', values='net_lo', aggfunc='sum').reindex(cal).fillna(0.0)
    BL = bd.pivot_table(index='date', columns='broker', values='bl', aggfunc='sum').reindex(cal).fillna(0.0)
    BV = bd.pivot_table(index='date', columns='broker', values='bval', aggfunc='sum').reindex(cal).fillna(0.0)
    has = pd.Series(cal).isin(set(bd.date.unique())).values
    rl = pd.Series({b: role_of(b) for b in pv.columns}); vs5 = vol.rolling(5).sum().values; vs20 = vol.rolling(20).sum().values
    roll5 = pv.rolling(5).sum().div(vs5, axis=0)
    mu = roll5.shift(5).rolling(60, min_periods=30).mean(); sd = roll5.shift(5).rolling(60, min_periods=30).std().clip(lower=0.005)
    z = (roll5 - mu) / sd
    f = {'ticker': [t] * len(cal), 'date': cal}
    cols = {r: rl.index[rl == r] for r in ROLES}; inst = rl.index[rl.isin(['foreign_inst', 'gov_inst'])]
    f['zmax_all'] = z.max(axis=1).values; f['zmin_all'] = z.min(axis=1).values
    for nm, cc in (('inst', inst), ('retail', cols['retail_app']), ('scalper', cols['scalper']), ('frb', cols['foreign_retail_base']), ('local', cols['local_other'])):
        f['zmax_' + nm] = z[cc].max(axis=1).values if len(cc) else np.nan; f['zmin_' + nm] = z[cc].min(axis=1).values if len(cc) else np.nan
    f['nz2'] = (z >= 2).sum(axis=1).values; f['nz2_inst'] = (z[inst] >= 2).sum(axis=1).values if len(inst) else 0
    for r_ in ROLES:
        s = pv[cols[r_]].sum(axis=1) if len(cols[r_]) else pd.Series(0.0, index=pv.index)
        r5 = s.rolling(5).sum() / vs5; m_ = r5.shift(5).rolling(60, min_periods=30).mean(); s_ = r5.shift(5).rolling(60, min_periods=30).std().clip(lower=0.005)
        f['zr_' + r_] = ((r5 - m_) / s_).values
    pa = pv.abs().rolling(20).sum().shift(5).div(vs20, axis=0)   # prior-20d activity per broker (days -24..-5 relative ... ends 5 days before t)
    newm = (pa < 0.0025)
    f['new_buy'] = (roll5.clip(lower=0).where(newm, 0)).sum(axis=1).values
    f['new_buy_inst'] = (roll5[inst].clip(lower=0).where(newm[inst], 0)).sum(axis=1).values if len(inst) else 0
    top_now = roll5.values.argmax(axis=1); top_prior = pv.rolling(20).sum().shift(5).values.argmax(axis=1)
    f['top1_chg'] = (top_now != top_prior).astype(float)
    f['inst5'] = (roll5[inst].sum(axis=1).values if len(inst) else np.zeros(len(cal)))
    f['ret5_past'] = (cl.pct_change(5) * 100).values
    for nm, cc in (('inst', inst), ('retail', cols['retail_app'])):
        if len(cc):
            lots = BL[cc].sum(axis=1).rolling(5).sum().values; val = BV[cc].sum(axis=1).rolling(5).sum().values
            f['cb_gap_' + nm] = (cl.values / (val / np.where(lots == 0, np.nan, lots)) - 1) * 100
        else: f['cb_gap_' + nm] = np.nan
    # type descriptors (point-in-time)
    atr = (pd.concat([g.high - g.low, (g.high - cl.shift(1)).abs(), (g.low - cl.shift(1)).abs()], axis=1).max(axis=1) / cl * 100)
    f['atr60'] = atr.rolling(60).mean().values; f['liq60'] = np.log10((cl * g.volume).rolling(60).mean().clip(lower=1)).values
    f['gorengan120'] = ((cl.pct_change().abs() > 0.07).astype(float).rolling(120).mean()).values
    tot = BL.sum(axis=1).replace(0, np.nan)
    fsh = (BL[list(cols['foreign_inst']) + list(cols['foreign_retail_base'])].sum(axis=1) / tot); rsh = (BL[cols['retail_app']].sum(axis=1) / tot) if len(cols['retail_app']) else pd.Series(np.nan, index=tot.index)
    f['foreign_share60'] = fsh.rolling(60, min_periods=30).mean().values; f['retail_share60'] = rsh.rolling(60, min_periods=30).mean().values
    top3 = pd.Series(np.sort(BL.values, axis=1)[:, -3:].sum(axis=1), index=BL.index) / tot
    f['top3conc60'] = top3.rolling(60, min_periods=30).mean().values
    bad = {k: (len(v) if hasattr(v, '__len__') else 'scalar') for k, v in f.items() if hasattr(v, '__len__') and not isinstance(v, str) and len(v) != len(cal)}
    if bad: raise SystemExit(f'{t} len(cal)={len(cal)} bad={bad}')
    F = pd.DataFrame(f); F.loc[~has, [x for x in F.columns if x not in ('ticker', 'date')]] = np.nan
    rows.append(F)
F = pd.concat(rows, ignore_index=True)
P = pd.read_pickle(os.environ['PANEL'])[['ticker', 'date', 'regime_phase', 'ret1', 'ret5', 'hi5', 'gap0', 'atr_cell', 'atr_0', 'liq', 'close']]
X = P.merge(F, on=['ticker', 'date'], how='inner').dropna(subset=['ret5'])
X.to_pickle(os.environ['OUT'])
print('rows', len(X), 'tickers', X.ticker.nunique(), 'features', X.shape[1])
print(X.groupby('regime_phase').size().to_dict()); print(X[['zmax_all', 'zmax_inst', 'nz2', 'new_buy', 'top1_chg', 'cb_gap_inst', 'atr60', 'foreign_share60']].describe().round(2).T[['count', 'mean', '50%', 'std']])
