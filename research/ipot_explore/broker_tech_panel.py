"""research/ipot_explore/broker_tech_panel.py — broker marker ON/OFF INSIDE lane-like technical setups, split by IHSG regime.
Setups are lane-LIKE approximations from OHLCV (not the production gates): OS (green candle, dist_sma20<=-8%, RSI<=40), VCPlike (V4 ladder),
BOSlike (within 3% of 60d high, above sma20, ret10>0), PB (RSI<=45 & close>sma50). Exit TP8/SL6/max10 from open(t+1), cost 0.4%.
Markers fixed before running: M1 foreign_inst 5d net>=4% vol | M2 >=3 distinct inst brokers (foreign_inst+gov_inst) with 5d net>=1% vol each
| M3 top-3 brokers' 5d net >=10% vol | M4 absorption: retail_app 5d net<=-4% & foreign_inst 5d net>=2%.
Verdict rule: pooled t>=3 AND same sign in >=2 of 3 blocks AND same sign in >=2 regimes with n>=100.
Run: PYTHONUTF8=1 JKSE=<full ^JKSE csv from yfinance> OUTP=<pickle> PYTHONPATH=. python research/ipot_explore/broker_tech_panel.py"""
import os, sys, warnings, numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
import research.ipot_explore.accum_episode as A
from engine.broker_roles import role_of
from engine.ihsg_regime import _build_regime
TP, SL, MH, COST = 8.0, 6.0, 10, 0.4
rng = np.random.default_rng(11)
d, o = A.load(); o = A.prep_price(o)
g = o.groupby('ticker', sort=False)
for k in range(1, MH + 1):
    for c in ('open', 'high', 'low', 'close'): o[f'{c}{k}'] = g[c].shift(-k)
o['gap'] = (o.open_n / o.close - 1) * 100
entry = o.open1.values; n = len(o); ret = np.full(n, np.nan); done = np.zeros(n, bool)
for k in range(1, MH + 1):
    op, hi, lo, cl = (o[f'{c}{k}'].values for c in ('open', 'high', 'low', 'close'))
    valid = ~np.isnan(hi) & ~done & ~np.isnan(entry); stop = entry * (1 - SL / 100); tp = entry * (1 + TP / 100)
    gd = valid & (op <= stop) & (k > 1); gu = valid & (op >= tp) & (k > 1)
    sh = valid & ~gd & ~gu & (lo <= stop); th = valid & ~gd & ~gu & ~sh & (hi >= tp)
    for m, px in ((gd, op), (gu, op), (sh, stop), (th, tp)):
        ret[m] = (px[m] / entry[m] - 1) * 100; done[m] = True
    if k == MH:
        m = valid & ~done; ret[m] = (cl[m] / entry[m] - 1) * 100; done[m] = True
o['trade'] = ret
# technicals
c, v = o.close, o.volume; gb = o.groupby('ticker')
o['sma20'] = gb.close.transform(lambda s: s.rolling(20).mean()); o['sma50'] = gb.close.transform(lambda s: s.rolling(50).mean())
dl = gb.close.diff()
up = dl.clip(lower=0).groupby(o.ticker).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
dn = (-dl.clip(upper=0)).groupby(o.ticker).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
o['rsi'] = 100 - 100 / (1 + up / dn.replace(0, np.nan)); o['dist20'] = (c / o.sma20 - 1) * 100
o['h60'] = gb.high.transform(lambda s: s.rolling(60).max()); o['h20'] = gb.high.transform(lambda s: s.rolling(20).max())
o['ret10'] = (c / gb.close.shift(10) - 1) * 100
def rng_(w): return gb.high.transform(lambda s: s.rolling(w).max()) - gb.low.transform(lambda s: s.rolling(w).min())
r15, r10, r5 = rng_(15) / c, rng_(10) / c, rng_(5) / c
liq = (c * v).groupby(o.ticker).transform(lambda s: s.rolling(20).mean()); atr20 = o.atr * 100
o['OS'] = (c > o.open) & (o.dist20 <= -8) & (o.rsi <= 40)
o['VCPlike'] = (r15 > r10) & (r10 > r5) & (r5 < r15 * 0.7) & (c / o.h20 - 1 > -0.10) & (atr20 <= 10) & (liq >= 0.15e9) & (liq < 30e9)
o['BOSlike'] = (c / o.h60 - 1 > -0.03) & (c > o.sma20) & (o.ret10 > 0) & (atr20 <= 10) & (liq >= 0.15e9)
o['PB'] = (o.rsi <= 45) & (c > o.sma50) & (liq >= 0.15e9)
# broker features (conservative net_lo)
ms = d.groupby(['ticker', 'date']).sell_lot.transform('min').fillna(0); sl_ = d.sell_lot.fillna(0); bl_ = d.buy_lot.fillna(0)
d['net_lo'] = np.where(d.buy_lot.isna(), -sl_, bl_ - np.where(d.sell_lot.isna(), ms, sl_))
rows = []
for t, dt in d.groupby('ticker'):
    cal = o.loc[o.ticker == t, 'date'].values
    if len(cal) < 80: continue
    vv = pd.Series(o.loc[o.ticker == t, 'vol_lot'].values).rolling(5).sum().values
    pv = dt.pivot_table(index='date', columns='broker', values='net_lo', aggfunc='sum').reindex(cal).fillna(0).rolling(5).sum() / vv[:, None]
    rl = pd.Series({b: role_of(b) for b in pv.columns})
    f = {'ticker': t, 'date': cal}
    for r_ in ('foreign_inst', 'gov_inst', 'retail_app', 'scalper', 'local_other'):
        cols = rl.index[rl == r_]; f['r_' + r_] = pv[cols].sum(axis=1).values if len(cols) else np.zeros(len(cal))
    ic = rl.index[rl.isin(['foreign_inst', 'gov_inst'])]
    f['n_inst'] = (pv[ic] >= 0.01).sum(axis=1).values if len(ic) else np.zeros(len(cal))
    f['top3'] = np.sort(pv.values, axis=1)[:, -3:].sum(axis=1) if pv.shape[1] >= 3 else np.nan
    rows.append(pd.DataFrame(f))
X = o.merge(pd.concat(rows), on=['ticker', 'date']).dropna(subset=['trade', 'rsi', 'dist20', 'cell'])
X = X[X.gap <= 7].copy()
X['M1'] = X.r_foreign_inst >= .04; X['M2'] = X.n_inst >= 3; X['M3'] = X.top3 >= .10
X['M4'] = (X.r_retail_app <= -.04) & (X.r_foreign_inst >= .02)
reg = pd.read_csv(os.environ['JKSE']); reg.index = pd.to_datetime(reg.iloc[:, 0], utc=True).dt.tz_convert('Asia/Jakarta').dt.tz_localize(None)
reg = _build_regime(reg)[['date', 'regime_phase']]; reg['date'] = reg.date.dt.strftime('%Y-%m-%d')
X = X.merge(reg, on='date', how='left'); X['block'] = -1
for b, (a, z) in enumerate(A.BLOCKS): X.loc[(X.date >= a) & (X.date <= z), 'block'] = b
X.to_pickle(os.environ['OUTP'])

def dedup(df):
    df = df.sort_values(['ticker', 'date']); kp = []; pt = None; pd_ = None
    for i, t, dd in zip(df.index, df.ticker, pd.to_datetime(df.date)):
        if t != pt or (dd - pd_).days > 14: kp.append(i); pt, pd_ = t, dd
    return df.loc[kp]

def lift(s, m):
    on, off = s[s[m]], s[~s[m]]
    if len(on) < 60 or len(off) < 60: return None
    base = off.groupby(['date', 'cell']).trade.mean().rename('b')  # OFF rows of the same setup, same date x ATR cell
    on = on.join(base, on=['date', 'cell']).dropna(subset=['b'])
    if len(on) < 60: return None
    ex = on.trade - on.b; ge = ex.groupby(on.ticker).agg(['sum', 'count'])
    bs = [ge['sum'].values[k].sum() / ge['count'].values[k].sum() for k in (rng.integers(0, len(ge), len(ge)) for _ in range(150))]
    return len(on), on.trade.mean(), ex.mean(), ex.mean() / np.std(bs), on.assign(ex=ex)

res = []
for st in ('OS', 'VCPlike', 'BOSlike', 'PB'):
    S = dedup(X[X[st]])
    for m in ('M1', 'M2', 'M3', 'M4'):
        L = lift(S, m)
        if L is None: continue
        n_, gross, ex, t, on = L
        bl = [on[on.block == b].ex.mean() if (on.block == b).sum() >= 40 else np.nan for b in range(3)]
        rg = on.groupby('regime_phase').ex.agg(['mean', 'count']); rg = rg[rg['count'] >= 100]
        res.append(dict(setup=st, marker=m, setup_n=len(S), n_on=n_, share=100 * n_ / len(S), gross_on=gross, net_on=gross - COST, excess=ex, t=t,
                        b0=bl[0], b1=bl[1], b2=bl[2], regimes=';'.join(f"{k[:5]}:{v['mean']:+.1f}({int(v['count'])})" for k, v in rg.iterrows())))
R = pd.DataFrame(res); pd.set_option('display.width', 300); pd.set_option('display.max_colwidth', 120)
print(R.round(2).to_string(index=False))
print('\nsetup baseline (dedup) mean trade, by regime:')
for st in ('OS', 'VCPlike', 'BOSlike', 'PB'):
    S = dedup(X[X[st]]); print(st, len(S), round(S.trade.mean(), 2), '|', S.groupby('regime_phase').trade.agg(['mean', 'count']).round(2).T.to_dict())
