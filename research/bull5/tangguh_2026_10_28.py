import sys,warnings;warnings.filterwarnings("ignore")
exec(open("research/bull5/resilient_2026_10_28.py").read().split("# crash windows")[0])
from sklearn.metrics import roc_auc_score
from sklearn.linear_model import LogisticRegression
C=C.ffill(limit=3);R=C.pct_change()
px2=pd.read_sql("select ticker,date,open,high,low from ohlcv_daily",sqlite3.connect("mbss_ohlcv.db"),parse_dates=["date"]);px2=px2[px2.date<="2026-10-20"]
O=px2.pivot(index="date",columns="ticker",values="open").reindex(index=C.index,columns=C.columns)
Hh=px2.pivot(index="date",columns="ticker",values="high").reindex(index=C.index,columns=C.columns)
Ll=px2.pivot(index="date",columns="ticker",values="low").reindex(index=C.index,columns=C.columns)
j=pd.read_csv("research/bull5/glob/JKSE.csv",index_col=0,parse_dates=True).Close.dropna()
j=j.reindex(C.index.union(j.index)).ffill().reindex(C.index);jr=j.pct_change()*100
THR=float(sys.argv[1]) if len(sys.argv)>1 else -1.5
crash=(jr<=THR);first=crash&(~crash.shift(1).rolling(5,min_periods=1).max().astype(bool))
S=[C.index.get_loc(d) for d in C.index[first] if C.index.get_loc(d)>=130 and C.index.get_loc(d)+5<len(C)]
print(f"IHSG crash days (<= {THR}%):",int(crash.sum()),"| episode first-days:",len(S))
print([ (str(C.index[s].date()),round(jr.iloc[s],1)) for s in S])
rows=[]
for s0 in S:
    s=s0-1
    sel=(mem.iloc[s]==1)&C.iloc[s0].notna()&C.iloc[s0+5].notna()&C.iloc[s].notna()
    f=feats(s)[sel]
    pc=C.iloc[s][sel]
    f["r0"]=(C.iloc[s0][sel]/pc-1)*100;f["r0rel"]=f.r0-jr.iloc[s0]
    rng=(Hh.iloc[s0]-Ll.iloc[s0])[sel];f["cpos0"]=((C.iloc[s0]-Ll.iloc[s0])[sel]/rng.where(rng>0))
    f["gap0"]=(O.iloc[s0][sel]/pc-1)*100;f["body0"]=(C.iloc[s0][sel]/O.iloc[s0][sel]-1)*100
    f["vol0"]=V.iloc[s0][sel]/V.iloc[s0-20:s0].mean()[sel]
    f["rs5pre"]=f.r5-(j.iloc[s]/j.iloc[s-5]-1)*100/100
    f["low0"]=(Ll.iloc[s0][sel]/pc-1)*100                       # intraday depth on crash day
    f["up0"]=(f.r0>0).astype(int)
    w=C.iloc[s0+1:s0+6].loc[:,sel]
    f["rec5"]=(w.max()>=pc).astype(int)                          # recovers pre-crash close within 5d
    f["hi5"]=(C.iloc[s0+5][sel]>C.iloc[s0][sel]).astype(int)      # day-5 close higher than first-crash-day close
    f["fwd5"]=(C.iloc[s0+5][sel]/C.iloc[s0][sel]-1)*100         # return from crash-day close
    f["ep"]=str(C.index[s0].date());f["jr0"]=jr.iloc[s0]
    rows.append(f.reset_index().rename(columns={"index":"ticker"}))
D=pd.concat(rows,ignore_index=True);D.to_pickle("research/bull5/tangguh_D.pkl")
print("pooled n",len(D),"| base rates: up0 %.1f%%  rec5 %.1f%%  hi5 %.1f%%  | mean fwd5 all %.2f  up0 %.2f  rec5 %.2f"%(100*D.up0.mean(),100*D.rec5.mean(),100*D.hi5.mean(),D.fwd5.mean(),D[D.up0==1].fwd5.mean(),D[D.rec5==1].fwd5.mean()))
print("per-episode base rate of up0:",D.groupby("ep").up0.mean().round(2).describe()[["min","50%","max"]].to_dict())
pre=["r5","r20","r60","r120","rs20","rs60","beta120","dnbeta120","upcap120","corr120","vol60","upshare60","maxdd60","hi250","d200","d50","liq","lprice","volr","rs5pre"]
day0=["r0","r0rel","cpos0","gap0","body0","vol0","low0"]
for lab in ("up0","rec5","hi5"):
    out=[]
    for f_ in pre+(day0 if lab!="up0" else []):
        a=[]
        for ep,g in D.groupby("ep"):
            g=g.dropna(subset=[f_])
            if g[lab].nunique()>1 and g[lab].sum()>=5 and len(g)>60: a.append(roc_auc_score(g[lab],g[f_]))
        if len(a)>=8: out.append((f_,np.mean(a),np.mean(np.array(a)>0.5),len(a)))
    o=pd.DataFrame(out,columns=["feat","auc","share_eps_auc>.5","n_eps"]).assign(dev=lambda x:(x.auc-.5).abs()).sort_values("dev",ascending=False).drop(columns="dev")
    print(f"== label {lab}: AUC averaged over crash episodes");print(o.head(10).round(2).to_string(index=False))
