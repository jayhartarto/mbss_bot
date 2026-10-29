import sys,warnings;warnings.filterwarnings("ignore")
src=open("research/bull5/portfolio_final_2026_10_28.py").read().split('sig=ds[ds["any"]')[0]
exec(src)
EXb=(.20,.08,.08,.06)
cal=pd.DatetimeIndex(sorted(ds.date.unique()))
j=pd.read_csv("research/bull5/glob/JKSE.csv",index_col=0,parse_dates=True).Close.dropna()
j=j.reindex(cal.union(j.index)).ffill().reindex(cal);jr=j.pct_change()*100
J=pd.DataFrame(index=cal)
for k in (1,2,3,5,10,20): J[f"r{k}"]=j.pct_change(k)*100
for k in (3,5,10,20,50): J[f"d{k}"]=(j/j.rolling(k).mean()-1)*100
J["above5low"]=(j/j.rolling(5).min()-1)*100
J["above10low"]=(j/j.rolling(10).min()-1)*100
J["dd10"]=(j/j.rolling(10).max()-1)*100
J["dd20"]=(j/j.rolling(20).max()-1)*100
J["sma5_10"]=(j.rolling(5).mean()/j.rolling(10).mean()-1)*100
J["green"]=(jr>0).astype(int);J["green3"]=J.green.rolling(3).sum();J["green5"]=J.green.rolling(5).sum()
J["min3"]=jr.rolling(3).min();J["min5"]=jr.rolling(5).min();J["min10"]=jr.rolling(10).min()
# vectorised stock features vs IHSG
Cm=px.pivot(index="date",columns="ticker",values="close").reindex(cal).ffill(limit=3);Rm=Cm.pct_change()*100
dn=(jr<0).astype(float)
F={}
cov=Rm.rolling(120).cov(jr);F["beta120"]=cov.div(jr.rolling(120).var(),axis=0)
def dcap(w):
    num=Rm.mul(dn,axis=0).rolling(w).sum();den=(jr*dn).rolling(w).sum();return num.div(den,axis=0)
F["dncap120"]=dcap(120);F["dncap20"]=dcap(20);F["dncap10"]=dcap(10)
for k in (5,10,20): F[f"rs{k}"]=(Cm.pct_change(k)*100).sub(J[f"r{k}"],axis=0)
F["hi250"]=(Cm/Cm.rolling(250,min_periods=120).max()-1)*100
F["d50s"]=(Cm/Cm.rolling(50).mean()-1)*100
F["up60"]=(Rm>0).rolling(60).mean()
red=(jr<0)
F["held10"]=((Rm.sub(jr,axis=0)>0)&red.values[:,None]).rolling(10).sum().div(red.rolling(10).sum(),axis=0)   # share of IHSG red days stock outperformed
F["posred10"]=((Rm>=0)&red.values[:,None]).rolling(10).sum().div(red.rolling(10).sum(),axis=0)
F["dd10s"]=(Cm/Cm.rolling(10).max()-1)*100
F["corr60"]=Rm.rolling(60).corr(jr)
sig=ds[ds.raw]
rows=[]
for r in sig.itertuples():
    t=trade(r.ticker,r.date,*EXb)
    if t is None or t["st"]=="open": continue
    d={"date":r.date,"ticker":r.ticker,"ret":(t["x"]/t["e"]-1)*100-C,"p3":r.p3,"mret5":r.mret5,"brd20":r.brd20,"fS":r.fS,"fP":r.fP,"liq":r.liq,"why":t["why"]}
    for k,v in F.items(): d[k]=v.at[r.date,r.ticker]
    rows.append(d)
T=pd.DataFrame(rows).merge(J,left_on="date",right_index=True,how="left")
T["per"]=np.where(T.date<"2025-07-01","train","test");T["sl"]=(T.ret<=-8).astype(int)
T.to_pickle("research/bull5/T2.pkl");J.to_pickle("research/bull5/J2.pkl")
print(len(T),T.p3.ge(1).sum(),T.ret.mean().round(2),T.groupby("per").size().to_dict())
