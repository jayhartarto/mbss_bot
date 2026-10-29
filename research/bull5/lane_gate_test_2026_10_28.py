"""Does the IHSG gate (long d50>=-1.1 / hybrid long-OR-rebound / breadth) work equally in the swing lanes and BSJP?
Episodes from research/bull5/lane_episodes.pkl (lane_capital_efficiency replay panel, lane-native exits)."""
import pickle,warnings;warnings.filterwarnings("ignore")
import numpy as np,pandas as pd
pd.set_option("display.width",230)
EP=pickle.load(open("research/bull5/lane_episodes.pkl","rb"))
J=pd.read_pickle("research/bull5/J2.pkl")
ds=pd.read_pickle("research/bull5/ds10.pkl");ds=ds[(ds.liq>=1)&(ds.close>=100)&(~ds.hsc)]
B=ds.groupby("date").agg(mret5=("mret5","first"),brd20=("brd20","first"))
Jx=J[["d50","d5","above10low","r1","r5"]].rename(columns={"d50":"j50","d5":"j5","above10low":"jlow","r1":"jr1","r5":"jr5"})
LANE_NAME={"BOW":"BOW (pullback)","OSBv2":"OSB-v2 (oversold, crash-native)","VCP":"VCP","MACDc":"MACD-confirm","BSJP1":"BSJP Lane1","BSJP2":"BSJP Lane2 (closed)"}
rows=[]
for k,e in EP.items():
    e=e.copy();e["date"]=pd.to_datetime(e["date"]);e=e.merge(Jx,left_on="date",right_index=True,how="left").merge(B,left_on="date",right_index=True,how="left")
    e=e.dropna(subset=["j50","ret_pct"]);e["per"]=np.where(e.date<"2025-07-01","train","test");e["yr"]=e.date.dt.year
    e["long"]=e.j50>=-1.1;e["reb"]=(e.jlow>=4)&(e.j5>=0);e["hyb"]=e.long|e.reb;e["br"]=e.mret5>=-0.5
    EP[k]=e
    for gn,m in (("no gate",e.ret_pct==e.ret_pct),("breadth mret5>=-0.5",e.br),("IHSG long d50>=-1.1",e.long),("IHSG hybrid",e.hyb),("SKIPPED by hybrid",~e.hyb),("IHSG bad & rebounded",(~e.long)&e.reb)):
        for per in ("train","test","all"):
            a=e[m&((e.per==per) if per!="all" else (e.ret_pct==e.ret_pct))]
            if len(a)<15: continue
            rows.append(dict(lane=LANE_NAME[k],gate=gn,per=per,n=len(a),win=100*(a.ret_pct>0).mean(),mean=a.ret_pct.mean(),med=a.ret_pct.median(),rpd=a.ret_pct.mean()/a.days_held.mean(),pct_kept=100*len(a)/max(1,(e.per==per).sum() if per!='all' else len(e))))
R=pd.DataFrame(rows).round(2)
for lane in LANE_NAME.values():
    print("=====",lane,"| date range",EP[[k for k,v in LANE_NAME.items() if v==lane][0]].date.min().date(),"->",EP[[k for k,v in LANE_NAME.items() if v==lane][0]].date.max().date())
    r=R[R.lane==lane].drop(columns="lane");print(r.pivot_table(index="gate",columns="per",values=["n","win","mean","rpd"],aggfunc="first").reindex(["no gate","breadth mret5>=-0.5","IHSG long d50>=-1.1","IHSG hybrid","SKIPPED by hybrid","IHSG bad & rebounded"]).round(2).to_string())
pickle.dump(EP,open("research/bull5/lane_episodes_gated.pkl","wb"))
# by year skipped vs kept (hybrid) mean ret
print("\n== by year: mean ret kept(hybrid) / skipped / share skipped")
out=[]
for k,e in EP.items():
    for y,g in e.groupby("yr"):
        kp=g[g.hyb];sk=g[~g.hyb]
        out.append(dict(lane=k,yr=y,n=len(g),kept=kp.ret_pct.mean(),skip=sk.ret_pct.mean() if len(sk)>=5 else np.nan,skip_share=100*len(sk)/len(g)))
O=pd.DataFrame(out).round(2);print(O.pivot(index="lane",columns="yr",values="kept").round(2).to_string());print(O.pivot(index="lane",columns="yr",values="skip").round(2).to_string());print(O.pivot(index="lane",columns="yr",values="skip_share").round(0).to_string())

# ---------------- crash resilience of BOW / OSB-v2 (and the others for comparison)
j=pd.read_csv("research/bull5/glob/JKSE.csv",index_col=0,parse_dates=True).Close
j=j.reindex(J.index.union(j.index)).ffill().reindex(J.index);jr=j.pct_change()*100
dd100=(j/j.rolling(100,min_periods=20).max()-1)*100
fwdmin5=pd.concat([jr.shift(-k) for k in range(1,6)],axis=1).min(axis=1)
ncr=(jr<=-1.5).rolling(20).sum()
print("\n================ CRASH RESILIENCE ================")
for k in ("BOW","OSBv2","VCP","MACDc","BSJP1"):
    e=EP[k].copy();e["dd100"]=dd100.reindex(e.date).values;e["fmin5"]=fwdmin5.reindex(e.date).values;e["ncr"]=ncr.reindex(e.date).values
    e["bucket"]=pd.cut(e.dd100,[-100,-15,-10,-5,0.1],labels=["dd<-15 (deep)","-15..-10","-10..-5","> -5 (normal)"])
    nm=LANE_NAME[k];sl={"BOW":-17.9,"OSBv2":-19.9}.get(k,None)
    def st(g):
        return pd.Series(dict(n=len(g),win=100*(g.ret_pct>0).mean(),mean=g.ret_pct.mean(),med=g.ret_pct.median(),p5=g.ret_pct.quantile(.05),worst=g.ret_pct.min(),rpd=g.ret_pct.mean()/g.days_held.mean()))
    print("\n-----",nm,"| overall",st(e).round(2).to_dict())
    print("by IHSG drawdown-from-100d-high at signal:");print(e.groupby("bucket",observed=True).apply(st).round(2).to_string())
    print("by whether IHSG had a day <=-2% in the NEXT 5 days:");print(e.dropna(subset=["fmin5"]).groupby(e.fmin5<=-2).apply(st).round(2).rename(index={False:"no shock",True:"SHOCK after"}).to_string())
    print("by IHSG crash days (<=-1.5%) in last 20d at signal:");print(e.dropna(subset=["ncr"]).groupby(pd.cut(e.ncr,[-1,0,2,50],labels=["0","1-2","3+"]),observed=True).apply(st).round(2).to_string())
    m26=e[e.date>="2026-08-01"];print("Aug-Oct 2026 signals:",len(m26),"mean",round(m26.ret_pct.mean(),2) if len(m26) else None,"win",round(100*(m26.ret_pct>0).mean(),0) if len(m26) else None,"| by month:",m26.groupby(m26.date.dt.to_period("M")).ret_pct.agg(["size","mean"]).round(1).to_dict("index") if len(m26) else None)
