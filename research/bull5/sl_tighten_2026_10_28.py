"""Tighter stop-loss for OSB-v2 (now -20%, TP +10%, 20d) and BOW (now -18%, TP=BB upper, 5d). Entry = signal-day close (as the lane panel),
SL checked first within a bar; gap-aware option fills at min(open, stop). Open positions marked to the last close (2026-10-20)."""
import sqlite3,pickle,warnings;warnings.filterwarnings("ignore")
import numpy as np,pandas as pd
pd.set_option("display.width",220)
EP=pickle.load(open("research/bull5/lane_episodes_open.pkl","rb"));J=pd.read_pickle("research/bull5/J2.pkl")
px=pd.read_sql("select ticker,date,open,high,low,close from ohlcv_daily order by ticker,date",sqlite3.connect("mbss_ohlcv.db"),parse_dates=["date"]);px=px[px.date<="2026-10-20"]
P={t:(g.date.to_numpy(),*[g[x].to_numpy(float) for x in ("open","high","low","close")]) for t,g in px.groupby("ticker")}
pos={t:pd.Series(np.arange(len(v[0])),index=v[0]) for t,v in P.items()}
Jx=J[["d50","d5","above10low"]].rename(columns={"d50":"j50","d5":"j5","above10low":"jlow"})
j=pd.read_csv("research/bull5/glob/JKSE.csv",index_col=0,parse_dates=True).Close;j=j.reindex(J.index.union(j.index)).ffill().reindex(J.index);jr=j.pct_change()*100
fm5=pd.concat([jr.shift(-k) for k in range(1,6)],axis=1).min(axis=1)
def load(k):
    e=EP[k].copy();e["date"]=pd.to_datetime(e["date"]);e=e.merge(Jx,left_on="date",right_index=True,how="left")
    e["hyb"]=(e.j50>=-1.1)|((e.jlow>=4)&(e.j5>=0));e["fm5"]=fm5.reindex(e.date).values;return e
def run(tk,dt,sl,tp,H,gap=True,be=None,band=False):
    d,o,h,l,c=P[tk];i=pos[tk][pd.Timestamp(dt)];e=c[i]
    if band:
        s=pd.Series(c);ma=s.rolling(20).mean().to_numpy();sd=s.rolling(20).std().to_numpy();bb=ma+2*sd
    stop=e*(1-sl);last=min(i+H,len(c)-1);mx=e
    for j_ in range(i+1,last+1):
        tpp=bb[j_] if band else e*(1+tp)
        if l[j_]<=stop: return ((min(o[j_],stop) if gap else stop)/e-1)*100,j_-i,"SL"
        if (not band or (tpp==tpp and tpp>=e)) and h[j_]>=tpp: return (tpp/e-1)*100,j_-i,"TP"
        if be and h[j_]>=e*(1+be): stop=max(stop,e*1.002)
    j_=last;return (c[j_]/e-1)*100,j_-i,("OPEN" if i+H>len(c)-1 else "TIME")
def grid(name,k,SLs,tp,H,band=False):
    e=load(k);Tr=[(r.ticker,r.date,r.hyb,r.fm5) for r in e.itertuples()]
    wins={"full history":lambda d:True,"Jul-Okt 2026":lambda d:d>=pd.Timestamp("2026-07-01"),"Sep-Okt 2026":lambda d:d>=pd.Timestamp("2026-09-01")}
    rows=[]
    for sl in SLs:
        for be in (None,0.05):
            res=[(run(tk,dt,sl,tp,H,True,be,band),dt,hy,f) for tk,dt,hy,f in Tr]
            for wn,fn in wins.items():
                s=[(r[0],r[1],r[2]) for r,dt,hy,f in res if fn(dt)]
                if not s: continue
                a=np.array([x[0] for x in s]);dd=np.array([x[1] for x in s]);why=[x[2] for x in s]
                rows.append(dict(SL=int(sl*100),BE=("on" if be else "-"),window=wn,n=len(a),win=100*(a>0).mean(),mean=a.mean(),med=np.median(a),p5=np.percentile(a,5),worst=a.min(),sl_rate=100*np.mean([w=="SL" for w in why]),hold=dd.mean(),rpd=a.mean()/dd.mean()))
    R=pd.DataFrame(rows).round(2)
    print("\n==========",name)
    for wn in wins: print(f"--- {wn}");print(R[(R.window==wn)&(R.BE=='-')].drop(columns=["window","BE"]).to_string(index=False))
    print("--- break-even stop after +5% (BE on), Sep-Okt 2026 and full");print(R[(R.BE=='on')&(R.window.isin(["full history","Sep-Okt 2026"]))].drop(columns=["BE"]).to_string(index=False))
    return Tr
OS=grid("OSB-v2 (TP +10%, 20 hari)", "OSBv2",[.06,.08,.10,.12,.15,.20],0.10,20)
BW=grid("BOW (TP = BB upper, 5 hari)","BOW",[.06,.08,.10,.12,.15,.18],None,5,band=True)
# OSB-v2 by IHSG state + shock for chosen SL levels
print("\n==== OSB-v2: SL by IHSG state / shock-after (full history)")
e=load("OSBv2")
for sl in (.08,.10,.12,.20):
    r=pd.DataFrame([dict(hyb=x.hyb,sh=(x.fm5<=-2) if x.fm5==x.fm5 else np.nan,ret=run(x.ticker,x.date,sl,.10,20,True)[0]) for x in e.itertuples()])
    print(f"SL{int(sl*100):2d}: gate OPEN mean {r[r.hyb].ret.mean():+.2f} win {100*(r[r.hyb].ret>0).mean():.0f}% | gate CLOSED mean {r[~r.hyb].ret.mean():+.2f} win {100*(r[~r.hyb].ret>0).mean():.0f}% | SHOCK-after mean {r[r.sh==True].ret.mean():+.2f} win {100*(r[r.sh==True].ret>0).mean():.0f}% | no-shock mean {r[r.sh==False].ret.mean():+.2f}")
