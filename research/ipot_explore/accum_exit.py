"""research/ipot_explore/accum_exit.py — falling-price (ret_pre5<-5%) + agregat net institusi >=4% volume, exit TP8/SL6/max10 dari open(t+1).
Pembanding: seluruh baris 'harga turun' yang sama (pb x ATR-tercile x blok), kontrol negatif ritel app. Biaya 0.4% roundtrip. Regime IHSG dari engine.ihsg_regime."""
import os, sys, warnings, numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
import research.ipot_explore.accum_episode as A
from engine.broker_roles import role_of
from engine.ihsg_regime import _build_regime
TP,SL,MH,COST=8.0,6.0,10,0.4
rng=np.random.default_rng(5)
d,o=A.load(); o=A.prep_price(o)
g=o.groupby('ticker',sort=False)
for k in range(1,MH+1):
    for c in ('open','high','low','close'): o[f'{c}{k}']=g[c].shift(-k)
o['gap']=(o.open_n/o.close-1)*100
# exit sim
entry=o.open1.values; n=len(o); ret=np.full(n,np.nan); done=np.zeros(n,bool); days=np.zeros(n)
for k in range(1,MH+1):
    op,hi,lo,cl=(o[f'{c}{k}'].values for c in ('open','high','low','close'))
    valid=~np.isnan(hi)&~done&~np.isnan(entry)
    stop=entry*(1-SL/100); tp=entry*(1+TP/100)
    gap_dn=valid&(op<=stop)&(k>1); gap_up=valid&(op>=tp)&(k>1)
    sl_hit=valid&~gap_dn&~gap_up&(lo<=stop); tp_hit=valid&~gap_dn&~gap_up&~sl_hit&(hi>=tp)
    for m,px in ((gap_dn,op),(gap_up,op),(sl_hit,stop),(tp_hit,tp)):
        ret[m]=(px[m]/entry[m]-1)*100; done[m]=True; days[m]=k
    if k==MH:
        m=valid&~done; ret[m]=(cl[m]/entry[m]-1)*100; days[m]=k; done[m]=True
o['trade']=ret; o['hold']=days
min_sell=d.groupby(['ticker','date']).sell_lot.transform('min').fillna(0)
sl_=d.sell_lot.fillna(0); bl_=d.buy_lot.fillna(0)
d['net_lo']=np.where(d.buy_lot.isna(),-sl_,bl_-np.where(d.sell_lot.isna(),min_sell,sl_)); d['role']=d.broker.map(role_of)
rows=[]
for t,dt in d.groupby('ticker'):
    cal=o.loc[o.ticker==t,'date'].values
    if len(cal)<60: continue
    v=pd.Series(o.loc[o.ticker==t,'vol_lot'].values).rolling(5).sum().values
    f={'ticker':t,'date':cal}
    for r in ('foreign_inst','gov_inst','retail_app'):
        gr=dt[dt.role==r]
        f['r_'+r]=(gr.pivot_table(index='date',columns='broker',values='net_lo',aggfunc='sum').reindex(cal).fillna(0).sum(axis=1).rolling(5).sum().values/v) if len(gr) else np.nan
    rows.append(pd.DataFrame(f))
X=o.merge(pd.concat(rows),on=['ticker','date'])
X=X.dropna(subset=['trade','ret_pre5','cell'])
X=X[(X.ret_pre5<-5)&(X.gap<=7)].copy()
X['net']=X.trade-COST
X['pb']=pd.cut(X.ret_pre5,[-100,-20,-12,-8,-5]); X['block']=-1
for b,(a,z) in enumerate(A.BLOCKS): X.loc[(X.date>=a)&(X.date<=z),'block']=b
X['base']=X.groupby(['pb','cell','block']).trade.transform('mean')
reg=_build_regime(pd.read_csv(os.environ['JKSE'],index_col=0).assign(Close=lambda x:x['Close']).set_axis(pd.to_datetime(pd.read_csv(os.environ['JKSE']).iloc[:,0],utc=True).dt.tz_convert('Asia/Jakarta').dt.tz_localize(None))) [['date','regime_phase']]
reg['date']=reg.date.dt.strftime('%Y-%m-%d'); X=X.merge(reg,on='date',how='left')
X['inst']=X.r_foreign_inst.fillna(0)+X.r_gov_inst.fillna(0)
V={'ALL falling (pembanding)':X.ticker.notna(),'foreign_inst>=4%':X.r_foreign_inst>=.04,'foreign_inst>=8%':X.r_foreign_inst>=.08,
   'foreign+gov sum>=4%':X.inst>=.04,'foreign>0 & gov>0 (both>=2%)':(X.r_foreign_inst>=.02)&(X.r_gov_inst>=.02),'retail_app>=4% (kontrol)':X.r_retail_app>=.04}
def dedup(df): 
    keep=[];last={}
    for i,(t,dt) in zip(df.index,df[['ticker','date']].values):
        pass
    df=df.sort_values(['ticker','date']); dts=pd.to_datetime(df.date); last=None; kp=[]
    prev_t=None;prev_d=None
    for i,t,dd in zip(df.index,df.ticker,dts):
        if t!=prev_t or (dd-prev_d).days>14: kp.append(i); prev_t,prev_d=t,dd
    return df.loc[kp]
def stat(s,col='trade'):
    x=s[col]; gs=x.groupby(s.ticker).agg(['sum','count'])
    ex=s[col]-s.base
    ge=ex.groupby(s.ticker).agg(['sum','count'])
    bs=[ge['sum'].values[k].sum()/ge['count'].values[k].sum() for k in (rng.integers(0,len(ge),len(ge)) for _ in range(200))]
    return dict(n=len(s),tick=s.ticker.nunique(),gross=x.mean(),net=x.mean()-COST,win=(x>0).mean()*100,tp=(s.trade>=TP-.01).mean()*100,sl=(s.trade<=-SL+.01).mean()*100,excess=ex.mean(),t=ex.mean()/np.std(bs))
out=[]
for k,m in V.items():
    s=dedup(X[m]); r=stat(s); r['set']=k
    for b in range(3):
        sb=s[s.block==b]; r[f'ex_b{b}']=(sb.trade-sb.base).mean() if len(sb)>50 else np.nan; r[f'n_b{b}']=len(sb)
    out.append(r)
R=pd.DataFrame(out).set_index('set'); pd.set_option('display.width',250)
print(R.round(2).to_string())
print('\nper regime (excess vs base, n) for foreign_inst>=4% vs ALL falling:')
for k in ('ALL falling (pembanding)','foreign_inst>=4%','foreign+gov sum>=4%'):
    s=dedup(X[V[k]]); print(k); print(s.assign(ex=s.trade-s.base).groupby('regime_phase').agg(n=('ex','size'),trade=('trade','mean'),excess=('ex','mean')).round(2).to_string())
X.to_pickle(os.environ.get('OUTP','x.pkl'))
