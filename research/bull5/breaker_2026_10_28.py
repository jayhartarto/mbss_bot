import sys,warnings;warnings.filterwarnings("ignore")
sys.path.insert(0,"research/bull5")
src=open("research/bull5/portfolio_final_2026_10_28.py").read().split('sig=ds[ds["any"]')[0]
exec(src)
EXb=(.20,.08,.08,.06)
def gl(n):
    s=pd.read_csv(f"research/bull5/glob/{n}.csv",index_col=0,parse_dates=True).Close.dropna();s.index=s.index.tz_localize(None) if s.index.tz else s.index;return s
G={}
for n in ("JKSE","USDIDR","TNX","SPX","VIX","OIL","GOLD","COPPER","DXY","EEM","N225","HSI"): G[n]=gl(n)
cal=pd.DatetimeIndex(sorted(ds.date.unique()))
M=pd.DataFrame(index=cal)
for n,s in G.items():
    s=s.reindex(cal.union(s.index)).ffill().reindex(cal)       # value known at that close (US lags: use prior-day)
    if n in("SPX","VIX","OIL","GOLD","COPPER","DXY","TNX","EEM"): s=s.shift(1)   # US close happens after IDX close -> only previous day known
    M[n+"_5d"]=s.pct_change(5)*100;M[n+"_lvl"]=s
M["JKSE_dd100"]=(G["JKSE"].reindex(cal).ffill()/G["JKSE"].reindex(cal).ffill().rolling(100,min_periods=20).max()-1)*100
M["JKSE_20d"]=G["JKSE"].reindex(cal).ffill().pct_change(20)*100
M["TNX_chg5"]=M["TNX_lvl"].diff(5)
d1=ds.groupby("date").agg(mret5=("mret5","first"),brd20=("brd20","first"),mret1=("mret1","first"))
M=M.join(d1)
sig=ds[ds["any"]&(ds.p3>=1)]
rows=[]
for r in sig.itertuples():
    t=trade(r.ticker,r.date,*EXb)
    if t is None or t["st"]=="open": continue
    rows.append(dict(date=r.date,ticker=r.ticker,ret=(t["x"]/t["e"]-1)*100-C))
T=pd.DataFrame(rows).merge(M,left_on="date",right_index=True,how="left")
T["per"]=np.where(T.date<"2025-07-01","train","test")
print("signals",len(T),"avg",T.ret.mean().round(2))
feats=["mret5","brd20","mret1","JKSE_5d","JKSE_20d","JKSE_dd100","USDIDR_5d","TNX_chg5","VIX_lvl","VIX_5d","SPX_5d","DXY_5d","EEM_5d","OIL_5d","GOLD_5d","COPPER_5d","N225_5d","HSI_5d"]
print("== tercile analysis of per-signal net return (balanced exit). Q1=lowest feature value; cutoffs from full sample")
out=[]
for f in feats:
    x=T.dropna(subset=[f]).copy()
    if len(x)<200: continue
    x["q"]=pd.qcut(x[f].rank(method="first"),3,labels=["Q1","Q2","Q3"])
    row={"feat":f}
    for per in ("train","test"):
        g=x[x.per==per].groupby("q").ret.agg(["mean","size"])
        for q in ("Q1","Q2","Q3"):
            if q in g.index: row[f"{per[:2]}_{q}"]=round(g.loc[q,"mean"],2)
    out.append(row)
print(pd.DataFrame(out).to_string(index=False))
# daily-level correlation (signal-date avg return vs feature)
dd=T.groupby("date").agg(ret=("ret","mean"),n=("ret","size")).join(M)
print("== spearman corr of day-avg signal return with feature (train/test)")
for f in feats:
    a=dd[dd.index<"2025-07-01"][["ret",f]].dropna();b=dd[dd.index>="2025-07-01"][["ret",f]].dropna()
    if len(a)>30 and len(b)>30: print(f"{f:12s} train {a.ret.corr(a[f],method='spearman'):+.2f} test {b.ret.corr(b[f],method='spearman'):+.2f}")
# candidate breakers (skip entry if condition true)
CAND={"mret5<-1":T.mret5<-1,"mret5<-2":T.mret5<-2,"brd20<0.30":T.brd20<0.30,"brd20<0.40":T.brd20<0.40,"JKSE_5d<-2":T.JKSE_5d<-2,"JKSE_5d<-3":T.JKSE_5d<-3,"JKSE_dd100<-15":T.JKSE_dd100<-15,"JKSE_20d<-8":T.JKSE_20d<-8,"VIX>25":T.VIX_lvl>25,"USDIDR_5d>1":T.USDIDR_5d>1,"USDIDR_5d>2":T.USDIDR_5d>2,"SPX_5d<-3":T.SPX_5d<-3,"EEM_5d<-3":T.EEM_5d<-3}
print("== breaker = skip signal when condition true  (kept = signals still taken)")
res=[]
for nm,c in CAND.items():
    c=c.fillna(False)
    for per in ("train","test"):
        a=T[T.per==per];ca=c[T.per==per]
        res.append(dict(rule=nm,per=per,skipped=int(ca.sum()),skip_avg=round(a[ca].ret.mean(),2) if ca.sum()>5 else np.nan,kept=int((~ca).sum()),kept_avg=round(a[~ca].ret.mean(),2),all_avg=round(a.ret.mean(),2),kept_win=round(100*(a[~ca].ret>0).mean(),1)))
R2=pd.DataFrame(res);print(R2.to_string(index=False))
# months Aug-Oct 2026: which signals would breakers have skipped
m=T[T.date>="2026-08-01"].copy()
for nm in ("mret5<-2","brd20<0.40","JKSE_dd100<-15","JKSE_20d<-8","USDIDR_5d>1"):
    c=CAND[nm].fillna(False)[T.date>="2026-08-01"]
    print(nm,"| Aug-Oct signals",len(m),"skipped",int(c.sum()),"avg skipped",round(m[c].ret.mean(),2) if c.sum() else None,"avg kept",round(m[~c].ret.mean(),2))
M.loc["2026-09-01":,["JKSE_5d","JKSE_dd100","JKSE_20d","USDIDR_5d","VIX_lvl","SPX_5d","mret5","brd20"]].iloc[::4].round(1).to_csv(sys.stdout)
