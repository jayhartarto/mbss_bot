import sys,warnings;warnings.filterwarnings("ignore")
src=open("research/bull5/portfolio_final_2026_10_28.py").read().split('sig=ds[ds["any"]')[0]
exec(src)
EXb=(.20,.08,.08,.06)
j=pd.read_csv("research/bull5/glob/JKSE.csv",index_col=0,parse_dates=True).Close.dropna()
cal=pd.DatetimeIndex(sorted(ds.date.unique()))
j=j.reindex(cal.union(j.index)).ffill().reindex(cal)
J=pd.DataFrame(index=cal)
for k in (1,3,5,10,20): J[f"r{k}"]=j.pct_change(k)*100
J["dd100"]=(j/j.rolling(100,min_periods=20).max()-1)*100
J["d20"]=(j/j.rolling(20).mean()-1)*100;J["d50"]=(j/j.rolling(50).mean()-1)*100
J["slope20"]=(j.rolling(20).mean()/j.rolling(20).mean().shift(5)-1)*100
J["rng10"]=(j.rolling(10).max()/j.rolling(10).min()-1)*100
J["vol10"]=j.pct_change().rolling(10).std()*100
J["lo20"]=(j/j.rolling(20).min()-1)*100
sig=ds[ds.raw&(ds.p3>=1)]            # ungated confirm3, so IHSG can be compared with the breadth gate
rows=[]
for r in sig.itertuples():
    t=trade(r.ticker,r.date,*EXb)
    if t is None or t["st"]=="open": continue
    rows.append(dict(date=r.date,ticker=r.ticker,ret=(t["x"]/t["e"]-1)*100-C,mret5=r.mret5,brd20=r.brd20))
T=pd.DataFrame(rows).merge(J,left_on="date",right_index=True,how="left").dropna(subset=["r5","dd100"])
T["per"]=np.where(T.date<"2025-07-01","train","test")
print("ungated confirm3 signals",len(T),"avg",T.ret.mean().round(2),"| with breadth gate mret5>=-0.5:",(T.mret5>=-0.5).sum(),T[T.mret5>=-0.5].ret.mean().round(2))
feats=["r1","r3","r5","r10","r20","dd100","d20","d50","slope20","rng10","vol10","lo20"]
print("== IHSG feature quintiles on signal date (avg net ret per signal)  tr | te")
for f in feats:
    x=T.copy();x["q"]=pd.qcut(x[f].rank(method="first"),5,labels=False)
    lo=x.groupby("q")[f].agg(["min","max"]).round(1)
    a=x[x.per=="train"].groupby("q").ret.mean().round(1);b=x[x.per=="test"].groupby("q").ret.mean().round(1)
    print(f"{f:8s}"," ".join(f"[{lo.loc[q,'min']:.1f},{lo.loc[q,'max']:.1f}]:{a.get(q,np.nan):+.1f}|{b.get(q,np.nan):+.1f}" for q in range(5)))
# threshold search: skip signals when feat < t (low) or > t (high); need skip n>=40 in both periods & skip avg < kept avg in both
res=[]
for f in feats:
    for side in ("low","high"):
        for qt in (.05,.1,.15,.2,.25,.3,.4):
            t=T[f].quantile(qt if side=="low" else 1-qt)
            c=(T[f]<t) if side=="low" else (T[f]>t)
            r={"feat":f,"skip":f"{f}{'<' if side=='low' else '>'}{t:.2f}"}
            ok=True
            for per in ("train","test"):
                a=T[T.per==per];ca=c[T.per==per]
                if ca.sum()<30: ok=False;break
                r[f"sk_{per[:2]}"]=a[ca].ret.mean();r[f"kp_{per[:2]}"]=a[~ca].ret.mean();r[f"n_{per[:2]}"]=int(ca.sum());r[f"all_{per[:2]}"]=a.ret.mean()
            if ok and r["sk_tr"]<r["kp_tr"] and r["sk_te"]<r["kp_te"]: r["gain"]=(r["kp_tr"]-r["all_tr"])+(r["kp_te"]-r["all_te"]);res.append(r)
Rs=pd.DataFrame(res).sort_values("gain",ascending=False)
pd.set_option("display.width",220)
print("== consistent skip-rules (skipped worse than kept in BOTH periods)");print(Rs.head(15).round(2).to_string(index=False))
T.to_pickle("research/bull5/ihsg_gate_T.pkl");J.to_pickle("research/bull5/ihsg_J.pkl")
