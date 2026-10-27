"""research/ipot_explore/broker_tech_by_regime.py — each IHSG regime phase is its OWN segment (own baseline, own time-stability), not a stratum
that must agree with the others. Input: panel pickle from broker_tech_panel.py. For every regime x lane-like setup x broker feature:
ON = feature in the top quartile of that regime x setup population (flow features) vs the rest; benchmark = OFF rows of the same setup,
same regime, same date x ATR cell; stability = chronological halves WITHIN the regime (10-day embargo); BH-FDR over all cells.
Run: PYTHONUTF8=1 PANEL=<pkl> python research/ipot_explore/broker_tech_by_regime.py"""
import os, warnings, numpy as np, pandas as pd
warnings.filterwarnings('ignore')
rng = np.random.default_rng(21)
X = pd.read_pickle(os.environ['PANEL'])
COST = 0.4
FEATS = ['r_foreign_inst', 'r_gov_inst', 'r_retail_app', 'r_scalper', 'r_local_other', 'n_inst', 'top3']

def dedup(df):
    df = df.sort_values(['ticker', 'date']); kp = []; pt = None; pd_ = None
    for i, t, dd in zip(df.index, df.ticker, pd.to_datetime(df.date)):
        if t != pt or (dd - pd_).days > 14: kp.append(i); pt, pd_ = t, dd
    return df.loc[kp]

def tstat(ex, tick):
    ge = ex.groupby(tick).agg(['sum', 'count'])
    if len(ge) < 5: return np.nan
    bs = [ge['sum'].values[k].sum() / ge['count'].values[k].sum() for k in (rng.integers(0, len(ge), len(ge)) for _ in range(150))]
    return ex.mean() / (np.std(bs) + 1e-9)

rows = []
for rg, XR in X.groupby('regime_phase'):
    dates = np.sort(XR.date.unique()); mid = dates[len(dates) // 2]
    for st in ('OS', 'VCPlike', 'BOSlike', 'PB'):
        S = dedup(XR[XR[st]])
        if len(S) < 150: continue
        for f in FEATS:
            thr = S[f].quantile(0.75)
            if f == 'n_inst': thr = max(thr, 2)
            on_m = S[f] >= thr
            if f in ('r_retail_app',):  # retail_app: test the SELL side (absorption) -> bottom quartile
                thr = S[f].quantile(0.25); on_m = S[f] <= thr
            on, off = S[on_m], S[~on_m]
            if len(on) < 50 or len(off) < 50: continue
            base = off.groupby(['date', 'cell']).trade.mean().rename('b')
            on = on.join(base, on=['date', 'cell']).dropna(subset=['b'])
            if len(on) < 50: continue
            ex = on.trade - on.b
            h1 = ex[on.date < mid]; h2 = ex[on.date > pd.Timestamp(mid).__add__(pd.Timedelta(days=14)).strftime('%Y-%m-%d')]
            rows.append(dict(regime=rg, setup=st, feat=f, thr=round(float(thr), 3), n_on=len(on), n_set=len(S), gross_on=on.trade.mean(), setup_mean=S.trade.mean(),
                             excess=ex.mean(), t=tstat(ex, on.ticker), h1=h1.mean() if len(h1) >= 25 else np.nan, h2=h2.mean() if len(h2) >= 25 else np.nan))
R = pd.DataFrame(rows)
from math import erf, sqrt
R['p'] = R.t.apply(lambda z: 2 * (1 - 0.5 * (1 + erf(abs(z) / sqrt(2)))) if pd.notna(z) else np.nan)
R = R.sort_values('p'); R['bh'] = R.p * len(R) / (np.arange(len(R)) + 1)
pd.set_option('display.width', 250)
print('cells tested:', len(R), ' expected false hits at p<.01:', round(0.01 * len(R), 1), ' at p<.05:', round(0.05 * len(R), 1))
print(R.round(3).head(30).to_string(index=False))
print('\nBH<0.10:'); print(R[R.bh < 0.10].round(3).to_string(index=False))
R.to_csv(os.path.join(os.path.dirname(os.environ['PANEL']), 'broker_tech_by_regime.csv'), index=False)
