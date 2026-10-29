import sqlite3,numpy as np,pandas as pd
exec(open("research/bull5/confirm_detail_2026_10_28.py").read().split("def stats")[0])  # reuse ds + flags
con=sqlite3.connect("mbss_ohlcv.db")
px=pd.read_sql("select ticker,date,open,high,low,close from ohlcv_daily order by ticker,date",con,parse_dates=["date"])
px=px[px.date<="2026-10-20"]
P={tk:(g.date.to_numpy(),*[g[x].to_numpy(float) for x in ("open","high","low","close")]) for tk,g in px.groupby("ticker")}
pos={tk:pd.Series(np.arange(len(v[0])),index=v[0]) for tk,v in P.items()}
H=9
def path(tk,di,variant,entry):
    d,o,h,l,c=P[tk];i=pos[tk][di]
    if i+H>=len(d): return None
    e=o[i+1];start=i+1
    if entry=="gap3" and (o[i+1]/c[i]-1)>0.03: return None
    if entry=="limit":
        if l[i+1]>c[i]: return None
        e=min(o[i+1],c[i]);start=i+2   # conservative: no same-bar exit on fill day
    if not e>0: return None
    tp,sl,mode=variant
    stop=e*(1-sl);half=False;ret_half=0;maxh=e;trail=None
    for j in range(start,i+H+1):
        k=j-i
        # stop first (gap-aware fill)
        if l[j]<=stop:
            fill=min(o[j],stop);r=(fill/e-1)*100
            if half: r=(r+5)/2
            return r,j,d[j]
        if mode=="scale" and not half and h[j]>=e*1.05: half=True;stop=max(stop,e*1.005)
        if mode=="scale" and half and h[j]>=e*(1+tp): return ((tp*100)+5)/2,j,d[j]
        if mode in("base","be") and tp and h[j]>=e*(1+tp): return tp*100,j,d[j]
        if mode=="be" and h[j]>=e*1.05: stop=max(stop,e*1.005)
        if mode=="trail":
            maxh=max(maxh,h[j])
            if maxh>=e*1.05: stop=max(stop,maxh*0.93)
    j=i+H;r=(c[j]/e-1)*100
    if half: r=(r+5)/2
    return r,j,d[j]
def sim(sig,variant,entry,N=8,f=.125,lo="2023-01-01",hi="2026-12-31",ret_trades=False):
    tr=[]
    for r in sig.itertuples():
        if not(lo<=str(r.date)[:10]<=hi): continue
        p=path(r.ticker,r.date,variant,entry)
        if p is None: continue
        d,o,h,l,c=P[r.ticker];i=pos[r.ticker][r.date]
        tr.append((d[i+1],p[2],p[0]-C,r.liq,r.ticker))
    tr.sort(key=lambda x:(x[0],-x[3]))
    eq=100.;open_=[];done=[];curve={}
    taken=[]
    for ent,ex,ret,liq,tk in tr:
        # realize exits before entry
        still=[]
        for p in open_:
            if p[1]<ent: eq+=p[3]*p[2]/100;curve[p[1]]=eq
            else: still.append(p)
        open_=still
        if len(open_)>=N or any(p[4]==tk for p in open_): continue
        open_.append([ent,ex,ret,f*eq,tk]);taken.append((ent,ex,ret,tk))
    for p in open_: eq+=p[3]*p[2]/100;curve[p[1]]=eq
    cv=pd.Series(curve).sort_index();cv=pd.concat([pd.Series([100.]),cv]) if len(cv) else cv
    dd=(cv/cv.cummax()-1).min()*100
    rets=np.array([t[2] for t in taken])
    if len(rets)==0: return None
    days=(pd.Timestamp(hi if hi<"2026-10-21" else "2026-10-20")-pd.Timestamp(lo)).days
    return dict(n=len(rets),n_sig=len(tr),win=(rets>0).mean()*100,avg=rets.mean(),med=np.median(rets),tot=eq-100,dd=dd,hold=np.mean([(pd.Timestamp(t[1])-pd.Timestamp(t[0])).days for t in taken]))
if __name__=="__main__":
    ds["liq"]=ds.liq
    sets={"X1 confirm3":ds[ds["any"]&(ds.p3>=1)],"X1b confirm5":ds[ds["any"]&(ds.p5>=1)],"X2 persist>=3in10":ds[ds["any"]&(ds.p10>=3)],"ALL gated":ds[ds["any"]]}
    V={"base TP10/SL8":(0.10,0.08,"base"),"TP5/SL7":(0.05,0.07,"base"),"TP10/SL6":(0.10,0.06,"base"),"TP10/SL8+BE@5":(0.10,0.08,"be"),"scale half@5,rest TP10":(0.10,0.08,"scale"),"trail 7% after +5 (no TP)":(0,0.08,"trail")}
    pd.set_option("display.width",230)
    print("== exit variants (entry=open t+1, N=8 slots x12.5%, cost .4 incl, SL gap-aware)")
    for sn in ("X1 confirm3","ALL gated"):
        rows=[]
        for vn,v in V.items():
            for per,(lo,hi) in (("train","2023-01-01","2025-06-30"),("test",("2025-07-01","2026-10-20"))) if False else (("train",("2023-01-01","2025-06-30")),("test",("2025-07-01","2026-10-20"))):
                r=sim(sets[sn],v,"open",lo=lo,hi=hi)
                if r: rows.append(dict(set=sn,exit=vn,per=per,**{k:round(x,2) for k,x in r.items()}))
        print(pd.DataFrame(rows).to_string(index=False))
    print("== entry variants (X1 confirm3, exit TP10/SL8)")
    rows=[]
    for en in ("open","gap3","limit"):
        for per,(lo,hi) in (("train",("2023-01-01","2025-06-30")),("test",("2025-07-01","2026-10-20"))):
            r=sim(sets["X1 confirm3"],V["base TP10/SL8"],en,lo=lo,hi=hi)
            if r: rows.append(dict(entry=en,per=per,**{k:round(x,2) for k,x in r.items()}))
    print(pd.DataFrame(rows).to_string(index=False))
    print("== other sets (exit TP10/SL8, open)")
    rows=[]
    for sn in ("X1b confirm5","X2 persist>=3in10"):
        for per,(lo,hi) in (("train",("2023-01-01","2025-06-30")),("test",("2025-07-01","2026-10-20"))):
            r=sim(sets[sn],V["base TP10/SL8"],"open",lo=lo,hi=hi)
            if r: rows.append(dict(set=sn,per=per,**{k:round(x,2) for k,x in r.items()}))
    print(pd.DataFrame(rows).to_string(index=False))
