"""Saved version of two inline tests (2026-10-28):
 (1) IHSG hybrid gate: d50>=-1.1 OR (IHSG>=4% above its 10d low AND IHSG>=SMA5) vs long gate only.
 (2) Booster = stock closed UP on >=1 IHSG crash day (IHSG<=-1.5%) in the last 20 sessions.
Input: research/bull5/T2.pkl + J2.pkl (build_T2_2026_10_28.py). Output: console tables."""
import sqlite3,warnings;warnings.filterwarnings("ignore")
import numpy as np,pandas as pd
T=pd.read_pickle("research/bull5/T2.pkl");J=pd.read_pickle("research/bull5/J2.pkl")
cal=J.index
px=pd.read_sql("select ticker,date,close from ohlcv_daily",sqlite3.connect("mbss_ohlcv.db"),parse_dates=["date"]);px=px[px.date<="2026-10-20"]
Cm=px.pivot(index="date",columns="ticker",values="close").reindex(cal).ffill(limit=3);Rm=Cm.pct_change()*100
j=pd.read_csv("research/bull5/glob/JKSE.csv",index_col=0,parse_dates=True).Close
j=j.reindex(cal.union(j.index)).ffill().reindex(cal);jr=j.pct_change()*100
cr=(jr<=-1.5);crm=pd.DataFrame(np.broadcast_to(cr.values[:,None],Rm.shape),index=Rm.index,columns=Rm.columns)
up=((Rm>0)&crm).astype(float).rolling(20).sum();avg=Rm.where(crm).rolling(20,min_periods=1).mean();nc=cr.rolling(20).sum()
T["heldcr15"]=[up.at[d,t] for d,t in zip(T.date,T.ticker)]
T["avgcr15"]=[avg.at[d,t] for d,t in zip(T.date,T.ticker)]
T["ncr15"]=nc.reindex(T.date).values
T=T.dropna(subset=["d50","above10low","d5"]).copy();T["win"]=T.ret>0
L=T.d50>=-1.1;RB=(T.above10low>=4)&(T.d5>=0);T["gated"]=L|RB
def row(df,m,nm):
    o=[]
    for p in ("train","test"):
        a=df[(df.per==p)&m[(df.per==p)]]
        o.append(f"{p}: n={len(a):4d} avg={a.ret.mean():+.2f} win={100*a.win.mean():.0f}% SL={100*a.sl.mean():.0f}%")
    print(f"{nm:56s}"+" | ".join(o))
print("== (1) hybrid gate")
for sn,df in (("ALL raw signals",T),("CONFIRM3 (p3>=1)",T[T.p3>=1])):
    print("-----",sn);Ld=df.d50>=-1.1;Rb=(df.above10low>=4)&(df.d5>=0)
    row(df,df.ret==df.ret,"no gate");row(df,Ld,"long gate d50>=-1.1");row(df,Ld|Rb,"hybrid: long OR rebound")
    row(df,(~Ld)&Rb,"bad regime & IHSG rebounded");row(df,(~Ld)&(~Rb),"bad regime & not rebounded")
print("== (2) booster: stock UP on IHSG crash days (<=-1.5%) within last 20 sessions")
for sn,df in (("hybrid-gated",T[T.gated]),("BAD regime (d50<-1.1)",T[~L]),("ALL",T)):
    print("-----",sn);ok=df.ncr15>=1
    row(df,ok,"signals with >=1 IHSG crash day in last 20d")
    row(df,ok&(df.heldcr15>=1),"  stock UP on >=1 crash days");row(df,ok&(df.heldcr15==0),"  stock never up")
    row(df,ok&(df.heldcr15>=2),"  stock UP on >=2 crash days");row(df,ok&(df.avgcr15>=-1),"  avg crash-day return >= -1%")
    row(df,ok&(df.avgcr15<-4),"  avg crash-day return < -4%")
