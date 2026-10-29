"""Does the exit (TP/SL/trailing/maxhold) need to be tighter in crash / rebound regimes?
Signals: research/bull5/T2.pkl (raw ungated lane signals). Populations by IHSG state at signal date."""
import sqlite3,itertools,warnings;warnings.filterwarnings("ignore")
import numpy as np,pandas as pd
pd.set_option("display.width",230)
COST=.4;H=9
T=pd.read_pickle("research/bull5/T2.pkl");J=pd.read_pickle("research/bull5/J2.pkl")
px=pd.read_sql("select ticker,date,open,high,low,close from ohlcv_daily order by ticker,date",sqlite3.connect("mbss_ohlcv.db"),parse_dates=["date"]);px=px[px.date<="2026-10-20"]
P={tk:(g.date.to_numpy(),*[g[x].to_numpy(float) for x in ("open","high","low","close")]) for tk,g in px.groupby("ticker")}
pos={tk:pd.Series(np.arange(len(v[0])),index=v[0]) for tk,v in P.items()}
j=pd.read_csv("research/bull5/glob/JKSE.csv",index_col=0,parse_dates=True).Close
j=j.reindex(J.index.union(j.index)).ffill().reindex(J.index);jr=j.pct_change()*100
T["ncr"]=(jr<=-1.5).rolling(20).sum().reindex(T.date).values
T=T.dropna(subset=["d50","above10low","d5"]).copy()
L=T.d50>=-1.1;RB=(T.above10low>=4)&(T.d5>=0)
POP={"BAD regime & IHSG rebounded (tradeable under hybrid gate)":(~L)&RB,
     "BAD regime, all (entering despite closed gate)":~L,
     "GOOD regime but crash day in last 20d (recovering market)":L&(T.ncr>=1),
     "GOOD regime, calm (no crash day in 20d)":L&(T.ncr==0)}
def build(df):
    out=[]
    for r in df.itertuples():
        d,o,h,l,c=P[r.ticker];i=pos[r.ticker][r.date]
        if i+H>=len(d) or not o[i+1]>0: continue
        out.append((o[i+1:i+H+1],h[i+1:i+H+1],l[i+1:i+H+1],c[i+H],r.per))
    return out
def sim(Tr,tp,sl,act,w,mh):
    rs=[];hd=[]
    for o,h,l,cl,per in Tr:
        e=o[0];stop=e*(1-sl);tpp=e*(1+tp);maxh=e;res=None
        for k in range(mh):
            if l[k]<=stop: res=(min(o[k],stop)/e-1,k+1);break
            if h[k]>=tpp: res=(tp,k+1);break
            if act:
                maxh=max(maxh,h[k])
                if maxh>=e*(1+act): stop=max(stop,maxh*(1-w))
        if res is None:
            cc=[cl] if mh==H else None
            res=((cl if mh==H else None)/e-1,mh) if mh==H else (None,mh)
        rs.append(res[0]);hd.append(res[1])
    return rs,hd
def sim2(Tr,tp,sl,act,w,mh):
    # mh<H : close of bar mh (needs closes) -> approximate with bar-mh high/low midpoint is wrong; use stored closes
    rs=[];hd=[]
    for o,h,l,cl,per,closes in Tr:
        e=o[0];stop=e*(1-sl);tpp=e*(1+tp);maxh=e;res=None
        for k in range(mh):
            if l[k]<=stop: res=(min(o[k],stop)/e-1,k+1);break
            if h[k]>=tpp: res=(tp,k+1);break
            if act:
                maxh=max(maxh,h[k])
                if maxh>=e*(1+act): stop=max(stop,maxh*(1-w))
        if res is None: res=(closes[mh-1]/e-1,mh)
        rs.append(res[0]*100-COST);hd.append(res[1])
    return np.array(rs),np.array(hd)
def build2(df):
    out=[]
    for r in df.itertuples():
        d,o,h,l,c=P[r.ticker];i=pos[r.ticker][r.date]
        if i+H>=len(d) or not o[i+1]>0: continue
        out.append((o[i+1:i+H+1],h[i+1:i+H+1],l[i+1:i+H+1],c[i],r.per,c[i+1:i+H+1]))
    return out
TPs=[.08,.10,.15,.20];SLs=[.05,.06,.08];TR=[(0,0)]+[(a/100,w/100) for a in (3,5,8) for w in (3,4,6)];MH=[5,9]
BASE=dict(tp=.20,sl=.08,act=.08,w=.06,mh=9);PLAIN=dict(tp=.10,sl=.08,act=0,w=0,mh=9)
for pn,m in POP.items():
    Tr=build2(T[m]);per=np.array([t[4] for t in Tr]);n_tr=(per=="train").sum();n_te=(per=="test").sum()
    print(f"\n=========== {pn}: n_train={n_tr} n_test={n_te}")
    if n_tr+n_te<60: print("too few");continue
    rows=[]
    for tp,sl,(a,w),mh in itertools.product(TPs,SLs,TR,MH):
        rs,hd=sim2(Tr,tp,sl,a,w,mh)
        d=dict(tp=int(tp*100),sl=int(sl*100),act=int(a*100),w=int(w*100),mh=mh,avg=rs.mean(),avg_tr=rs[per=="train"].mean() if n_tr else np.nan,avg_te=rs[per=="test"].mean() if n_te else np.nan,
               win=100*(rs>0).mean(),stop=100*(rs<=-sl*100).mean(),p5=np.percentile(rs,5),hold=hd.mean(),exTop5=np.sort(rs)[:-max(1,int(len(rs)*.05))].mean())
        rows.append(d)
    R=pd.DataFrame(rows);R["floor"]=R[["avg_tr","avg_te"]].min(axis=1)
    def get(**k):
        q=R
        for a_,b_ in k.items(): q=q[q[a_]==b_]
        return q
    b=get(tp=20,sl=8,act=8,w=6,mh=9).iloc[0];pl=get(tp=10,sl=8,act=0,w=0,mh=9).iloc[0]
    print(f"balanced (TP20/SL8/trail8-6/9d): avg {b.avg:+.2f} tr {b.avg_tr:+.2f} te {b.avg_te:+.2f} win {b.win:.0f}% stop {b.stop:.0f}% hold {b.hold:.1f}")
    print(f"plain    (TP10/SL8/9d)         : avg {pl.avg:+.2f} tr {pl.avg_tr:+.2f} te {pl.avg_te:+.2f} win {pl.win:.0f}% stop {pl.stop:.0f}% hold {pl.hold:.1f}")
    print("-- top 8 by min(train,test) avg");print(R.sort_values("floor",ascending=False).head(8)[["tp","sl","act","w","mh","avg","avg_tr","avg_te","win","stop","p5","hold","exTop5"]].round(2).to_string(index=False))
    print("-- marginal effects (mean avg over the rest of the grid): ")
    for k in ("tp","sl","mh"): print(f"   {k}:",R.groupby(k).avg.mean().round(2).to_dict())
    R["trail"]=np.where(R.act==0,"none",R.act.astype(str)+"/"+R.w.astype(str));print("   trail act/width:",R.groupby("trail").avg.mean().round(2).to_dict())
