import sys;sys.path.insert(0,"research/bull5")
import sqlite3,numpy as np,pandas as pd
src=open("research/bull5/confirm_detail_2026_10_28.py").read().split("def stats")[0].replace('ds=ds.dropna(subset=["t10_sl8"]).copy()','').replace('dropna(subset=["r_sl5","sma50"','dropna(subset=["sma50"')
exec(src)
con=sqlite3.connect("mbss_ohlcv.db")
px=pd.read_sql("select ticker,date,open,high,low,close from ohlcv_daily order by ticker,date",con,parse_dates=["date"]);px=px[px.date<="2026-10-20"]
P={tk:(g.date.to_numpy(),*[g[x].to_numpy(float) for x in ("open","high","low","close")]) for tk,g in px.groupby("ticker")}
pos={tk:pd.Series(np.arange(len(v[0])),index=v[0]) for tk,v in P.items()}
H=9
def trade(tk,dt,tp,sl,act,w):
    d,o,h,l,c=P[tk];i=pos[tk][dt]
    if i+1>=len(d): return None
    e=o[i+1];stop=e*(1-sl);tpp=e*(1+tp) if tp else 1e18;maxh=e
    last=min(i+H,len(d)-1)
    for j in range(i+1,last+1):
        if l[j]<=stop: return dict(e=e,ei=i+1,xi=j,x=min(o[j],stop),why="stop" if stop<=e*(1-sl)+1e-9 else "trail",st="closed")
        if h[j]>=tpp: return dict(e=e,ei=i+1,xi=j,x=tpp,why="TP",st="closed")
        if act:
            maxh=max(maxh,h[j])
            if maxh>=e*(1+act): stop=max(stop,maxh*(1-w))
    j=last
    if i+H<=len(d)-1: return dict(e=e,ei=i+1,xi=j,x=c[j],why="time9",st="closed")
    return dict(e=e,ei=i+1,xi=j,x=c[j],why="OPEN",st="open")
def portfolio(sig,ex,N=8,f=.125,lo="2023-01-01",hi="2026-12-31"):
    tr=[]
    for r in sig.itertuples():
        if not(lo<=str(r.date)[:10]<=hi): continue
        t=trade(r.ticker,r.date,*ex)
        if t is None: continue
        d=P[r.ticker][0];t.update(tk=r.ticker,sigd=r.date,ent=d[t["ei"]],ext=d[t["xi"]],liq=r.liq);tr.append(t)
    tr.sort(key=lambda x:(x["ent"],-x["liq"]))
    open_=[];taken=[];eq=100.
    cal=sorted(set(px.date[(px.date>=lo)&(px.date<=min(hi,"2026-10-20"))]))
    ti=0;curve={};realized=100.
    for day in cal:
        # enter today's trades
        while ti<len(tr) and tr[ti]["ent"]==day:
            t=tr[ti];ti+=1
            if len(open_)<N and not any(p["tk"]==t["tk"] for p in open_):
                t["size"]=f*realized;open_.append(t);taken.append(t)
        # mtm at close
        mtm=0
        for p in list(open_):
            d,o,h,l,c=P[p["tk"]];j=pos[p["tk"]].get(day)
            if day>=p["ext"] and p["st"]=="closed":
                p["ret"]=(p["x"]/p["e"]-1)*100-C;realized+=p["size"]*p["ret"]/100;open_.remove(p)
            elif j is not None: mtm+=p["size"]*((c[j]/p["e"]-1)*100-C/2)/100
        curve[day]=realized+mtm
    for p in open_: p["ret"]=(p["x"]/p["e"]-1)*100-C
    cv=pd.Series(curve);dd=(cv/cv.cummax()-1).min()*100
    return taken,cv,dd
def summ(taken,cv,dd,nm):
    cl=[t for t in taken if t["st"]=="closed"];r=np.array([t["ret"] for t in cl])
    print(f"{nm:34s} entries={len(taken)} closed={len(cl)} win={100*(r>0).mean():.0f}% avg={r.mean():+.2f}% med={np.median(r):+.2f}% total={cv.iloc[-1]/cv.iloc[0]*100-100:+.1f}% maxDD(MTM)={dd:.1f}% best={r.max():+.0f} worst={r.min():+.0f}")
sig=ds[ds["any"]&(ds.p3>=1)]
EX={"TP10/SL8":(.10,.08,0,0),"TP20/SL8+trail8/6 (balanced)":(.20,.08,.08,.06),"noTP/SL8+trail8/4":(None,.08,.08,.04),"TP20/SL8 plain":(.20,.08,0,0)}
print("signals by month 2026 (confirm3):",sig[sig.date>="2026-01-01"].groupby(sig.date.dt.to_period("M")).size().to_dict())
for nm,ex in EX.items():
    print("=====",nm)
    for per,(lo,hi) in (("train 23-01..25-06",("2023-01-01","2025-06-30")),("test 25-07..26-10",("2025-07-01","2026-10-20")),("full",("2023-01-01","2026-10-20"))):
        t,cv,dd=portfolio(sig,ex,lo=lo,hi=hi);summ(t,cv,dd,per)
    for m,(lo,hi) in (("2026-08",("2026-08-01","2026-08-31")),("2026-09",("2026-09-01","2026-09-30")),("2026-10 (to 10-20)",("2026-10-01","2026-10-20"))):
        t,cv,dd=portfolio(sig,ex,lo=lo,hi=hi);summ(t,cv,dd,"  "+m)
        if "balanced" in nm:
            for x in t: print(f"      {x['tk']:5s} sig {str(x['sigd'])[:10]} ent {str(x['ent'])[:10]} @{x['e']:.0f} -> {str(x['ext'])[:10]} {x['why']:5s} {x['ret']:+6.1f}%")
# market gate state in those months
g=ds.groupby("date").agg(mret5=("mret5","first"),brd20=("brd20","first"))
g["gate"]=g.mret5>=-0.5
print(g[g.index>="2026-07-01"].groupby(g[g.index>="2026-07-01"].index.to_period("M")).agg(gate_days=("gate","sum"),days=("gate","size"),mret5=("mret5","mean"),brd20=("brd20","mean")).round(2))
raw=ds[ds.raw&(ds.date>="2026-07-01")];print("raw alerts/month:",raw.groupby(raw.date.dt.to_period("M")).size().to_dict(),"gated:",ds[ds["any"]&(ds.date>="2026-07-01")].groupby(ds.date.dt.to_period("M")).size().to_dict())
