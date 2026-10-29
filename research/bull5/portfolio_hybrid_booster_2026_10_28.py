"""8-slot portfolio sim: gate variants (breadth / IHSG long / IHSG hybrid) x signal set (confirm3 / all raw) x booster use
(priority, size tilt, filter). Balanced exit TP20/SL8/trail act8 width6. Daily mark-to-market equity, cost .4 incl, entry open t+1."""
import sys,warnings;warnings.filterwarnings("ignore")
src=open("research/bull5/portfolio_final_2026_10_28.py").read().split('sig=ds[ds["any"]')[0]
exec(src)                       # ds (incl. latest days), P,pos,trade, C=.4
EXb=(.20,.08,.08,.06)
J=pd.read_pickle("research/bull5/J2.pkl");cal=J.index
Cm=px.pivot(index="date",columns="ticker",values="close").reindex(cal).ffill(limit=3);Rm=Cm.pct_change()*100
j=pd.read_csv("research/bull5/glob/JKSE.csv",index_col=0,parse_dates=True).Close
j=j.reindex(cal.union(j.index)).ffill().reindex(cal);jr=j.pct_change()*100
cr=(jr<=-1.5);crm=pd.DataFrame(np.broadcast_to(cr.values[:,None],Rm.shape),index=Rm.index,columns=Rm.columns)
UP=((Rm>0)&crm).astype(float).rolling(20).sum();NC=cr.rolling(20).sum()
S=ds[ds.raw].merge(J[["d50","d5","above10low"]].rename(columns={"d50":"j50","d5":"j5","above10low":"jlow"}),left_on="date",right_index=True,how="left").dropna(subset=["j50"]).copy()
S["held"]=[UP.at[d,t] if t in UP.columns else np.nan for d,t in zip(S.date,S.ticker)]
S["ncr"]=NC.reindex(S.date).values
S["long"]=S.j50>=-1.1;S["reb"]=(S.jlow>=4)&(S.j5>=0);S["hyb"]=S.long|S.reb;S["br"]=S.mret5>=-0.5
S["boost"]=np.where(S.ncr>=1,np.where(S.held>=2,2,np.where(S.held>=1,1,0)),0)
def run(sig,lo,hi,N=8,f=.125,prio=False,tilt=False,drop_never=False):
    tr=[]
    for r in sig.itertuples():
        if not(lo<=str(r.date)[:10]<=hi): continue
        if drop_never and r.ncr>=1 and r.held==0: continue
        t=trade(r.ticker,r.date,*EXb)
        if t is None: continue
        d=P[r.ticker][0];t.update(tk=r.ticker,sigd=r.date,ent=d[t["ei"]],ext=d[t["xi"]],liq=r.liq,boost=r.boost);tr.append(t)
    tr.sort(key=lambda x:(x["ent"],-(x["boost"] if prio else 0),-x["liq"]))
    open_=[];taken=[];realized=100.;ti=0;curve={}
    days=[d for d in cal if lo<=str(d)[:10]<=min(hi,"2026-10-20")]
    for day in days:
        while ti<len(tr) and tr[ti]["ent"]==day:
            t=tr[ti];ti+=1
            mult=1.5 if (tilt and t["boost"]>=2) else 1.0
            used=sum(p["size"] for p in open_)
            if len(open_)<N and not any(p["tk"]==t["tk"] for p in open_) and used+f*mult*realized<=realized*1.0001:
                t["size"]=f*mult*realized;open_.append(t);taken.append(t)
        mtm=0
        for p in list(open_):
            dd,o,h,l,c=P[p["tk"]];jx=pos[p["tk"]].get(day)
            if day>=p["ext"] and p["st"]=="closed":
                p["ret"]=(p["x"]/p["e"]-1)*100-C;realized+=p["size"]*p["ret"]/100;open_.remove(p)
            elif jx is not None: mtm+=p["size"]*((c[jx]/p["e"]-1)*100-C/2)/100
        curve[day]=realized+mtm
    for p in open_: p["ret"]=(p["x"]/p["e"]-1)*100-C
    cv=pd.Series(curve)
    if len(cv)==0 or not taken: return None
    cl=[t for t in taken if t["st"]=="closed"];r=np.array([t["ret"] for t in cl])
    yrs=max((pd.Timestamp(min(hi,"2026-10-20"))-pd.Timestamp(lo)).days/365.25,0.05)
    tot=cv.iloc[-1]/100-1
    return dict(entries=len(taken),win=100*(r>0).mean(),avg=r.mean(),med=np.median(r),total=tot*100,ann=((1+tot)**(1/yrs)-1)*100 if yrs>=0.5 else np.nan,mdd=((cv/cv.cummax()-1).min()*100),
                boosted=sum(1 for t in taken if t["boost"]>=1),avg_boost=np.mean([t["ret"] for t in cl if t["boost"]>=1]) if any(t["boost"]>=1 for t in cl) else np.nan)
VAR=[("A confirm3 + breadth gate (old)",S[S.p3.ge(1)&S.br],{}),
     ("B confirm3 + IHSG long gate",S[S.p3.ge(1)&S.long],{}),
     ("C confirm3 + IHSG hybrid gate",S[S.p3.ge(1)&S.hyb],{}),
     ("D C + booster priority",S[S.p3.ge(1)&S.hyb],dict(prio=True)),
     ("E C + booster priority + 1.5x size (held>=2)",S[S.p3.ge(1)&S.hyb],dict(prio=True,tilt=True)),
     ("F C + drop never-up when crash in last 20d",S[S.p3.ge(1)&S.hyb],dict(drop_never=True)),
     ("G ALL raw + hybrid gate",S[S.hyb],{}),
     ("H ALL raw + hybrid + booster priority",S[S.hyb],dict(prio=True)),
     ("I ALL raw + hybrid + priority + 1.5x tilt",S[S.hyb],dict(prio=True,tilt=True)),
     ("J ALL raw + hybrid + drop never-up",S[S.hyb],dict(drop_never=True))]
PER=[("train 23-01..25-06","2023-01-01","2025-06-30"),("test 25-07..26-10","2025-07-01","2026-10-20"),("full","2023-01-01","2026-10-20"),("2026-08..10","2026-08-01","2026-10-20")]
rows=[]
for nm,sg,kw in VAR:
    for pn,lo,hi in PER:
        r=run(sg,lo,hi,**kw)
        if r: rows.append(dict(var=nm,per=pn,**{k:round(v,2) for k,v in r.items()}))
R=pd.DataFrame(rows);R.to_csv("research/bull5/portfolio_hybrid_booster.csv",index=False)
pd.set_option("display.width",230);pd.set_option("display.max_colwidth",60)
for pn,_,_ in PER:
    print("=====",pn);print(R[R.per==pn].drop(columns="per").to_string(index=False))
