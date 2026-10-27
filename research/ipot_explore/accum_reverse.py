"""research/ipot_explore/accum_reverse.py — outcome-first: which broker NET accumulation (5d to t) precedes a big rise from open(t+1)?
Big rise = max high over t+1..t+10 >= +15% above open(t+1). Tradeable filter: no chase (ret_pre5<=5%), t+1 gap<=7% (no ARA open).
Per ticker-day features: net_lo share (cum 5d net / 5d volume) summed per role + max single broker. Compare P(win | feature>=4%) with the
same date x ATR-tercile cell base rate (lift, pp), ticker-clustered bootstrap, 3 blocks.
"""
import os, sys, numpy as np, pandas as pd
sys.path.insert(0, os.getcwd())
import research.ipot_explore.accum_episode as A
from engine.broker_roles import role_of
rng=np.random.default_rng(3)
d,o=A.load(); o=A.prep_price(o)
g=o.groupby('ticker',sort=False)
o['maxhi']=g['high'].transform(lambda s: s[::-1].rolling(10,min_periods=10).max()[::-1].shift(-1))
o['rise']=(o.maxhi/o.open_n-1)*100
o['gap']=(o.open_n/o.close-1)*100
o['win']=(o.rise>=15).astype(float)
min_sell=d.groupby(['ticker','date']).sell_lot.transform('min').fillna(0)
bl=d.buy_lot.fillna(0); sl=d.sell_lot.fillna(0)
d['net_lo']=np.where(d.buy_lot.isna(),-sl,bl-np.where(d.sell_lot.isna(),min_sell,sl))
d['role']=d.broker.map(role_of)
rows=[]
for t,dt in d.groupby('ticker'):
    cal=o.loc[o.ticker==t,'date'].values
    if len(cal)<60: continue
    v=pd.Series(o.loc[o.ticker==t,'vol_lot'].values).rolling(5).sum().values
    feats={}
    for r,gr in dt.groupby('role'):
        feats['role_'+r]=gr.pivot_table(index='date',columns='broker',values='net_lo',aggfunc='sum').reindex(cal).fillna(0).sum(axis=1).rolling(5).sum().values/v
    pv=dt.pivot_table(index='date',columns='broker',values='net_lo',aggfunc='sum').reindex(cal).fillna(0).rolling(5).sum()
    for r in ['foreign_inst','gov_inst','local_other','scalper','retail_app','foreign_retail_base']:
        cols=[c for c in pv.columns if role_of(c)==r]
        if cols: feats['max_'+r]=pv[cols].max(axis=1).values/v
    f=pd.DataFrame(feats); f['ticker']=t; f['date']=cal; rows.append(f)
F=pd.concat(rows)
X=o.merge(F,on=['ticker','date'],how='inner').dropna(subset=['rise','ret_pre5','gap','cell'])
X=X[(X.ret_pre5<=5)&(X.gap<=7)]
X['base']=X.groupby(['date','cell']).win.transform('mean')
X['block']=-1
for b,(a,z) in enumerate(A.BLOCKS): X.loc[(X.date>=a)&(X.date<=z),'block']=b
print('rows',len(X),'tickers',X.ticker.nunique(),'base win rate %.2f%%'%(100*X.win.mean()))
def lift(sub):
    s=(sub.win-sub.base); gs=s.groupby(sub.ticker).agg(['sum','count']); m=s.mean()
    bs=[gs['sum'].values[k].sum()/gs['count'].values[k].sum() for k in (rng.integers(0,len(gs),len(gs)) for _ in range(200))]
    return len(sub),100*sub.win.mean(),100*m,m/np.std(bs)
out=[]
for c in [c for c in X.columns if c.startswith(('role_','max_'))]:
    for thr in (0.04,0.08):
        sub=X[X[c]>=thr]
        if len(sub)<300: continue
        n,w,l,t=lift(sub)
        bl=[lift(sub[sub.block==b])[2] if (sub.block==b).sum()>100 else np.nan for b in range(3)]
        out.append((c,thr,n,sub.ticker.nunique(),w,l,t,*bl))
R=pd.DataFrame(out,columns=['feature','thr','n','tickers','P(win)%','lift_pp','t','blk0','blk1','blk2'])
print(R.round(2).to_string(index=False))
# reverse view: among big winners, how often did each role accumulate >=4% vs among matched controls
W=X[X.win==1]
print('\nshare of winners with role accumulation >=4%  vs  all rows:')
for c in [c for c in X.columns if c.startswith('role_')]:
    print(c, round(100*(W[c]>=0.04).mean(),1), round(100*(X[c]>=0.04).mean(),1))
# ELIT-like: price falling
Xf=X[X.ret_pre5<-5].copy()
Xf['pb']=pd.cut(Xf.ret_pre5,[-100,-20,-12,-8,-5])
Xf['base']=Xf.groupby(['pb','cell','block']).win.transform('mean')
print('\nfalling-price subset base %.2f%% n=%d'%(100*Xf.win.mean(),len(Xf)))
rf=[]
for c in [c for c in X.columns if c.startswith(('role_','max_'))]:
    sub=Xf[Xf[c]>=0.04]
    if len(sub)<200: continue
    n,w,l,t=lift(sub); rf.append((c,n,w,l,t))
print(pd.DataFrame(rf,columns=['feature','n','P(win)%','lift_pp','t']).round(2).to_string(index=False))
