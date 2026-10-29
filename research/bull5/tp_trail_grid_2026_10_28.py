import sys;sys.path.insert(0,"research/bull5")
import numpy as np,pandas as pd,itertools
from sim_2026_10_28 import P,pos,ds,C,H
def build(sig):
    T=[]
    for r in sig.itertuples():
        d,o,h,l,c=P[r.ticker];i=pos[r.ticker][r.date]
        if i+H>=len(d) or not o[i+1]>0: continue
        T.append((r.date,o[i+1:i+H+1],h[i+1:i+H+1],l[i+1:i+H+1],c[i+H]))
    return T
def run(T,tp,sl,act,w):
    rs=[];hd=[]
    for dt,o,h,l,cl in T:
        e=o[0];stop=e*(1-sl);tpp=e*(1+tp) if tp else 1e18;maxh=e;res=None
        for k in range(H):
            if l[k]<=stop: res=(min(o[k],stop)/e-1,k+1);break
            if h[k]>=tpp: res=(tp,k+1);break
            if act:
                maxh=max(maxh,h[k])
                if maxh>=e*(1+act): stop=max(stop,maxh*(1-w))
        if res is None: res=(cl/e-1,H)
        rs.append(res[0]*100-C);hd.append(res[1])
    rs=np.array(rs);return rs.mean(),(rs>0).mean()*100,np.mean(hd),np.percentile(rs,5)
sets={"confirm3":ds[ds["any"]&(ds.p3>=1)],"all gated":ds[ds["any"]]}
TPs=[.08,.10,.12,.15,.20,.30,None];SLs=[.06,.08,.10]
TR=[(0,0)]+[(a/100,w/100) for a in (5,8,10) for w in (4,6,8)]
rows=[]
for sn,s in sets.items():
    tr=build(s[s.date<"2025-07-01"]);te=build(s[s.date>="2025-07-01"])
    for tp,sl,(a,w) in itertools.product(TPs,SLs,TR):
        x=run(tr,tp,sl,a,w);y=run(te,tp,sl,a,w)
        rows.append(dict(set=sn,tp=(int(tp*100) if tp else 0),sl=int(sl*100),act=int(a*100),w=int(w*100),n_tr=len(tr),avg_tr=x[0],win_tr=x[1],avg_te=y[0],win_te=y[1],hold_te=y[2],p5_te=y[3],hold_tr=x[2]))
R=pd.DataFrame(rows);R["minavg"]=R[["avg_tr","avg_te"]].min(axis=1);R["perday_te"]=R.avg_te/R.hold_te
R.to_csv("research/bull5/tp_trail_grid.csv",index=False)
pd.set_option("display.width",230)
for sn in sets:
    d=R[R.set==sn]
    print("=====",sn)
    print("-- TP sweet spot (no trail), SL8"); print(d[(d.sl==8)&(d.act==0)][["tp","avg_tr","win_tr","avg_te","win_te","hold_te","p5_te","perday_te"]].round(2).to_string(index=False))
    print("-- SL effect (TP10, no trail)"); print(d[(d.tp==10)&(d.act==0)][["sl","avg_tr","win_tr","avg_te","win_te","p5_te"]].round(2).to_string(index=False))
    print("-- top 12 by min(train,test) avg"); print(d.sort_values("minavg",ascending=False).head(12)[["tp","sl","act","w","avg_tr","win_tr","avg_te","win_te","hold_te","p5_te","perday_te"]].round(2).to_string(index=False))
    print("-- trail only effect at TP none/SL8 (act,w)"); print(d[(d.tp==0)&(d.sl==8)][["act","w","avg_tr","win_tr","avg_te","win_te","hold_te"]].round(2).to_string(index=False))
