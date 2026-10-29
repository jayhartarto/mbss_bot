"""Can IPOT broker flow boost the bull5 (S1/S2/A/B) signals?  Features at signal date t from fd='all',board='all' top-10/side data.
net_lo convention (as engine/newbuyer_tag): broker missing on buy side -> buys 0; missing on sell side -> sells the day's smallest listed sell."""
import sqlite3,warnings,sys;warnings.filterwarnings("ignore")
import numpy as np,pandas as pd
sys.path.insert(0,".")
from engine.broker_roles import role_of
pd.set_option("display.width",230)
T=pd.read_pickle("research/bull5/T2.pkl")
T=T.dropna(subset=["d50","above10low","d5"]).copy()
T["gated"]=(T.d50>=-1.1)|((T.above10low>=4)&(T.d5>=0))
tick=sorted(T.ticker.unique())
con=sqlite3.connect("file:research/ipot_merged.sqlite?mode=ro",uri=True)
d=pd.read_sql_query("SELECT ticker,trade_date date,broker_code broker,buy_lot,sell_lot FROM ipot_broker_daily WHERE fd='all' AND board='all'",con)
d=d[d.ticker.isin(tick)].copy()
ms=d.groupby(["ticker","date"]).sell_lot.transform("min").fillna(0);sl=d.sell_lot.fillna(0);bl=d.buy_lot.fillna(0)
d["net"]=np.where(d.buy_lot.isna(),-sl,bl-np.where(d.sell_lot.isna(),ms,sl))
dg={t:x for t,x in d.groupby("ticker")}
o=pd.read_sql_query("SELECT ticker,date,volume,close FROM ohlcv_daily WHERE date>='2022-10-01' ORDER BY ticker,date",sqlite3.connect("mbss_ohlcv.db"))
ROLES=["foreign_inst","foreign_retail_base","local_other","gov_inst","retail_app","scalper"]
REF=("foreign_inst","local_other","foreign_retail_base")
need=T.groupby("ticker").date.apply(lambda s:set(pd.to_datetime(s).dt.strftime("%Y-%m-%d")))
rows=[]
for t,g in o.groupby("ticker"):
    if t not in dg or t not in need.index: continue
    cal=g.date.values;vol=g.volume.values/100.0;v5=pd.Series(vol).rolling(5).sum().values;v20=pd.Series(vol).rolling(20).sum().values;v1=vol
    pv=dg[t].pivot_table(index="date",columns="broker",values="net",aggfunc="sum").reindex(cal).fillna(0.0)
    hasd=pd.Series(dg[t].date.unique()).isin(cal).sum()
    roll5=pv.rolling(5).sum().div(v5,axis=0);roll1=pv.div(v1,axis=0)
    pa=pv.abs().rolling(20).sum().shift(5).div(v20,axis=0)
    rl=pd.Series({b:role_of(b) for b in pv.columns})
    f=pd.DataFrame({"ticker":t,"date":cal})
    for r in ROLES:
        cols=rl.index[rl==r]
        f[f"n5_{r}"]=roll5[cols].sum(axis=1).values if len(cols) else 0.0
        f[f"n1_{r}"]=roll1[cols].sum(axis=1).values if len(cols) else 0.0
    ref=rl.index[rl.isin(REF)];new=(pa[ref]<0.0025)
    f["nb_ref"]=(roll5[ref].clip(lower=0).where(new,0)).sum(axis=1).values if len(ref) else 0.0
    f["n_new_buyers"]=((roll5[ref]>0.01)&new).sum(axis=1).values if len(ref) else 0
    pos=roll5.clip(lower=0).values;srt=-np.sort(-pos,axis=1)
    f["top3_buy5"]=srt[:,:3].sum(axis=1) if srt.shape[1]>=3 else srt.sum(axis=1)
    f["n_buyers5"]=(roll5>0.01).sum(axis=1).values;f["tot5"]=roll5.sum(axis=1).values
    f["bdays"]=pd.Series(cal).isin(set(dg[t].date)).rolling(25).sum().values
    f["liq20"]=(g.close.values*g.volume.values)
    f["liq20"]=pd.Series(f.liq20.values).rolling(20).mean().values
    f["date"]=pd.to_datetime(f.date)
    f=f[f.date.dt.strftime("%Y-%m-%d").isin(need[t])]
    rows.append(f)
F=pd.concat(rows,ignore_index=True)
X=T.merge(F,on=["ticker","date"],how="left")
X["ok"]=(X.bdays>=15)
X["tag"]=(X.nb_ref>=0.057)&(X.liq20>=3e8)&X.ok
print("signals",len(X),"| with broker data ok:",int(X.ok.sum()),f"({100*X.ok.mean():.0f}%) | by period ok-share:",X.groupby("per").ok.mean().round(2).to_dict())
X.to_pickle("research/bull5/broker_X.pkl")
X["win"]=X.ret>0
def row(df,m,nm):
    o=[]
    for p in ("train","test"):
        a=df[(df.per==p)&m[(df.per==p)]]
        o.append(f"{p}: n={len(a):4d} avg={a.ret.mean():+.2f} win={100*a.win.mean():.0f}% SL={100*a.sl.mean():.0f}%")
    print(f"{nm:52s}"+" | ".join(o))
from sklearn.metrics import roc_auc_score
for sn,df in (("hybrid-gated CONFIRM3",X[X.gated&(X.p3>=1)&X.ok]),("hybrid-gated ALL raw",X[X.gated&X.ok]),("ALL ungated raw",X[X.ok])):
    print("\n=====",sn,len(df))
    row(df,df.ret==df.ret,"baseline");row(df,df.tag,"PEMBELI BARU tag (nb_ref>=.057 & liq)");row(df,~df.tag,"  not tagged")
    row(df,df.fS.astype(bool),"  S-family (momentum) all");row(df,df.fS.astype(bool)&df.tag,"  S-family & tag");row(df,df.fP.astype(bool),"  P-family (A/B pullback) all");row(df,df.fP.astype(bool)&df.tag,"  P-family & tag")
    out=[]
    feats=[c for c in df.columns if c.startswith(("n5_","n1_"))]+["nb_ref","n_new_buyers","top3_buy5","n_buyers5","tot5"]
    for f in feats:
        r={"feat":f}
        for lab,y in (("win",df.win),("nosl",1-df.sl)):
            for p in ("train","test"):
                g=df[df.per==p];yy=y[df.per==p]
                r[f"{lab}_{p[:2]}"]=roc_auc_score(yy,g[f].fillna(0)) if yy.nunique()>1 and g[f].nunique()>3 else np.nan
        out.append(r)
    A=pd.DataFrame(out).set_index("feat").round(2);A["dev"]=(A.drop(columns=[]).sub(.5).abs().mean(axis=1));print(A.sort_values("dev",ascending=False).drop(columns="dev").head(9).to_string())
    print("-- top tertile vs bottom tertile by feature (avg ret): ",end="")
    for f in ("nb_ref","n5_foreign_inst","n5_local_other","n5_retail_app","n5_scalper","top3_buy5","tot5"):
        res=[]
        for p in ("train","test"):
            g=df[(df.per==p)&df[f].notna()];hi=g[g[f]>=g[f].quantile(2/3)].ret.mean();lo=g[g[f]<=g[f].quantile(1/3)].ret.mean();res.append(f"{hi:+.1f}/{lo:+.1f}")
        print(f"\n   {f:18s} top/bottom tertile  train {res[0]}  test {res[1]}",end="")
    print()
