import sqlite3,numpy as np,pandas as pd
from sklearn.metrics import roc_auc_score
pd.set_option("display.width",220)
ds=pd.read_pickle("research/bull5/ds10.pkl")
uni=ds[(ds.liq>=1)&(ds.close>=100)&(~ds.hsc)][["ticker","date"]]
px=pd.read_sql("select ticker,date,high,low,close,volume from ohlcv_daily",sqlite3.connect("mbss_ohlcv.db"),parse_dates=["date"]);px=px[px.date<="2026-10-20"]
C=px.pivot(index="date",columns="ticker",values="close");V=px.pivot(index="date",columns="ticker",values="volume")
R=C.pct_change()
mem=uni.assign(x=1).pivot(index="date",columns="ticker",values="x").reindex(index=C.index,columns=C.columns)
idx=R.where(mem==1).median(axis=1).fillna(0)          # median-stock index return
sec=pd.read_csv("idx_ic_sector_map.csv",usecols=["ticker","sector"]).drop_duplicates("ticker").set_index("ticker").sector
dates=C.index
def feats(s):                       # features as of close of date index s (causal)
    f=pd.DataFrame(index=C.columns)
    def ret(k): return C.iloc[s]/C.iloc[s-k]-1
    for k in (5,20,60,120): f[f"r{k}"]=ret(k)
    f["rs20"]=f.r20-f.r20.median();f["rs60"]=f.r60-f.r60.median();f["rs120"]=f.r120-f.r120.median()
    w=R.iloc[s-119:s+1];ix=idx.iloc[s-119:s+1]
    cov=w.sub(w.mean()).mul(ix-ix.mean(),axis=0).mean();f["beta120"]=cov/ix.var()
    dn=ix<0;f["dnbeta120"]=(w[dn].mul(1)).mean()/ix[dn].mean()      # downside capture
    f["upcap120"]=w[~dn].mean()/ix[~dn].mean()
    f["vol60"]=R.iloc[s-59:s+1].std()*100
    f["upshare60"]=(R.iloc[s-59:s+1]>0).mean()
    f["maxdd60"]=(C.iloc[s-59:s+1]/C.iloc[s-59:s+1].cummax()-1).min()*100
    f["hi250"]=(C.iloc[s]/C.iloc[max(0,s-249):s+1].max()-1)*100
    f["d200"]=(C.iloc[s]/C.iloc[max(0,s-199):s+1].mean()-1)*100
    f["d50"]=(C.iloc[s]/C.iloc[s-49:s+1].mean()-1)*100
    f["liq"]=(C*V).iloc[s-19:s+1].median()/1e9
    f["lprice"]=np.log(C.iloc[s])
    f["volr"]=V.iloc[s-4:s+1].mean()/V.iloc[s-59:s+1].mean()
    f["corr120"]=w.corrwith(ix)
    return f
# crash windows: forward-H median return <= -12%, non-overlapping, plus the Sep window
H=35;res=[]
fw=pd.Series({dates[i]:(C.iloc[i+H]/C.iloc[i]-1)[mem.iloc[i]==1].median() for i in range(130,len(dates)-H)})
starts=[];i=0;ds_=list(fw.index)
while i<len(ds_):
    if fw.iloc[i]<=-0.12: starts.append(ds_[i]);i+=H+1
    else: i+=1
sep=dates.get_loc(pd.Timestamp("2026-08-31"))
print("crash windows (start, fwd35 median ret):",[(str(d)[:10],round(fw[d]*100,1)) for d in starts])
print("Sep-Oct window: start 2026-08-31 -> 10-20 (",len(dates)-1-sep,"days)")
rows=[]
def label(s,e,sel):
    r=(C.iloc[e]/C.iloc[s]-1)
    return r[sel]
allf=[]
def add(name,s,e,tag):
    sel=mem.iloc[s]==1
    sel&=C.iloc[s].notna()&C.iloc[e].notna()
    f=feats(s)[sel];r=(C.iloc[e]/C.iloc[s]-1)[sel]
    f["y_up"]=(r>0).astype(int);f["ret"]=r*100
    f["y_top"]=(r.rank(pct=True)>=0.85).astype(int);f["win"]=name;f["tag"]=tag
    f["sector"]=sec.reindex(f.index);allf.append(f)
    print(f"{name}: n={len(f)} market median={r.median()*100:.1f}% share_up={(r>0).mean():.1%}")
for d in starts:
    s=dates.get_loc(d);add(str(d)[:10],s,s+H,"A")
# Sept-Oct 2026 (exact window)
add("2026-08-31",sep,len(dates)-1,"A")
F=pd.concat(allf);F.to_pickle("research/bull5/resil_feats.pkl")
feat=[c for c in F.columns if c not in("y_up","ret","y_top","win","tag","sector")]
out=[]
for ft in feat:
    row={"feat":ft}
    for w,g in F.groupby("win"):
        g=g.dropna(subset=[ft]);row[w]=roc_auc_score(g.y_top,g[ft]) if g.y_top.nunique()>1 and len(g)>40 else np.nan
    out.append(row)
A=pd.DataFrame(out).set_index("feat");A["mean"]=A.mean(axis=1);A["sign_agree"]=((A.drop(columns="mean")-.5)*np.sign(A["mean"]-.5)>0).sum(axis=1)
print("AUC for 'top-15% relative performer in crash window' (0.5=null):")
print(A.sort_values("mean",key=lambda x:(x-.5).abs(),ascending=False).round(2).to_string())
