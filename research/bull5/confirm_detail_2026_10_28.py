import pandas as pd,numpy as np
ds=pd.read_pickle("research/bull5/ds10.pkl")
ds=ds[(ds.liq>=1)&(ds.close>=100)&(~ds.hsc)].dropna(subset=["r_sl5","sma50","hi60","atr14","rsi","volr","pctb"]).sort_values(["ticker","date"]).reset_index(drop=True)
C=.4
A=(ds.ret3>=5.7)&(ds.gap<=-0.93)&(ds.cpos<=0.941);B=(ds.ret10>=12)&(ds.d5>=-1.15)&(ds.d50<=4.58)&(ds.rng_pct<=3.85);G=ds.mret5>=-0.5
T=(ds.slope20>=7.09)&(ds.atr14<=5.83);S1=T&(ds.atrratio<=0.971)&(ds.cpos>=0.25);S2=T&(ds.body>=0.704)
ds["fS"]=(S1|S2);ds["fP"]=(A|B);ds["raw"]=ds.fS|ds.fP;ds["any"]=ds.raw&G
g=ds.groupby("ticker")
def prior(col,W): return g[col].transform(lambda s:s.astype(int).shift(1).rolling(W,min_periods=1).sum()).fillna(0)
for W in (3,5,10,15): ds[f"p{W}"]=prior("raw",W)
ds["pS5"]=prior("fS",5);ds["pP5"]=prior("fP",5)
# close at most recent prior alert within 10d, and days since
ds["ac"]=g.apply(lambda x:x.close.where(x.raw).shift(1).ffill(limit=9),include_groups=False).reset_index(level=0,drop=True)
idx=np.arange(len(ds));ds["ai"]=pd.Series(np.where(ds.raw,idx,np.nan),index=ds.index)
ds["ai"]=g["ai"].transform(lambda s:s.shift(1).ffill(limit=9))
ds["since"]=idx-ds.ai
ds["chg"]=(ds.close/ds.ac-1)*100
ds=ds.dropna(subset=["t10_sl8"]).copy()
def stats(nm,m):
    s=[]
    for per,sel in (("tr",ds.date<"2025-07-01"),("te",ds.date>="2025-07-01")):
        d=ds[m&sel]
        s.append(f"{per} n={len(d):4d} /d={len(d)/ds[sel].date.nunique():.2f} win10={(d.t10_sl8>C).mean():.0%} hit10={d.hit10.mean():.0%} EV={d.t10_sl8.mean()-C:+.2f}" if len(d)>=20 else f"{per} n={len(d)}")
    print(f"{nm:44s}"+" | ".join(s))
base=ds["any"]
stats("all gated alerts",base)
print("-- window W (prior alert within W days)")
for W in (3,5,10,15): stats(f"confirm W={W}",base&(ds[f"p{W}"]>=1))
print("-- number of prior alerts in 10d")
for lo,hi in ((0,0),(1,1),(2,2),(3,99)): stats(f"prior10 in [{lo},{hi}]",base&(ds.p10>=lo)&(ds.p10<=hi) if False else base&(ds.p10.between(lo,hi)))
print("-- prior-type x today-type (5d)")
for pn,pm in (("prior S",ds.pS5>=1),("prior P(A/B)",ds.pP5>=1)):
    for tn,tm in (("today S",ds.fS),("today P",ds.fP)): stats(f"{pn} -> {tn}",base&pm&tm)
print("-- days since last alert (<=10d)")
for lo,hi in ((1,1),(2,3),(4,5),(6,10)): stats(f"since {lo}-{hi}d",base&ds.since.between(lo,hi))
print("-- price change since last alert close (W<=10)")
for lo,hi in ((-99,-5),(-5,0),(0,5),(5,10),(10,99)): stats(f"chg [{lo},{hi})%",base&ds.since.notna()&(ds.chg>=lo)&(ds.chg<hi))
