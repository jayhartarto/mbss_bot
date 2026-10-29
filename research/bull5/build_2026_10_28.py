"""Bullish-episode sweep: features at close t, outcome = buy open t+1, TP+5% / SL / maxhold 9 bars.
Output research/bull5/ds.pkl"""
import sqlite3, json, numpy as np, pandas as pd
con=sqlite3.connect("mbss_ohlcv.db")
px=pd.read_sql("select ticker,date,open,high,low,close,volume from ohlcv_daily order by ticker,date",con,parse_dates=["date"])
px=px[px.date<="2026-10-20"]
hsc=set(json.load(open("hsc_list.json"))["tickers"])
H=9;TP=0.05
out=[]
for tk,g in px.groupby("ticker"):
    g=g.reset_index(drop=True)
    if len(g)<120: continue
    o,h,l,c,v=[g[x].to_numpy(float) for x in ("open","high","low","close","volume")]
    s=pd.Series(c);n=len(g)
    f=pd.DataFrame({"ticker":tk,"date":g.date})
    for k in (1,3,5,10,20): f[f"ret{k}"]=s.pct_change(k)*100
    f["sma5"]=s.rolling(5).mean();f["sma10"]=s.rolling(10).mean();f["sma20"]=s.rolling(20).mean();f["sma50"]=s.rolling(50).mean()
    f["d5"]=(c/f.sma5-1)*100;f["d20"]=(c/f.sma20-1)*100;f["d50"]=(c/f.sma50-1)*100
    f["slope20"]=(f.sma20/f.sma20.shift(5)-1)*100
    pc=s.shift(1)
    tr=pd.concat([pd.Series(h-l),(pd.Series(h)-pc).abs(),(pd.Series(l)-pc).abs()],axis=1).max(axis=1)
    f["atr14"]=tr.rolling(14).mean()/s*100;f["atr5"]=tr.rolling(5).mean()/s*100
    f["atrratio"]=f.atr5/f.atr14
    d=s.diff();up=d.clip(lower=0).rolling(14).mean();dn=(-d.clip(upper=0)).rolling(14).mean()
    f["rsi"]=100-100/(1+up/dn)
    sd=s.rolling(20).std();f["pctb"]=(c-(f.sma20-2*sd))/(4*sd)
    f["bbw"]=4*sd/f.sma20*100
    rng=np.where(h>l,h-l,np.nan);f["cpos"]=(c-l)/rng
    f["body"]=(c-o)/o*100;f["gap"]=(o/pc-1)*100
    vs=pd.Series(v);f["volr"]=vs/vs.rolling(20).mean();f["volr5"]=vs.rolling(5).mean()/vs.rolling(20).mean()
    f["liq"]=(s*vs).rolling(20).median()/1e9
    f["hi60"]=(c/pd.Series(h).rolling(60).max()-1)*100
    f["hi20"]=(c/pd.Series(h).rolling(20).max()-1)*100
    f["lo20"]=(c/pd.Series(l).rolling(20).min()-1)*100
    f["close"]=c
    f["nup3"]=(d>0).astype(int).rolling(3).sum();f["nup5"]=(d>0).astype(int).rolling(5).sum()
    f["rng_pct"]=(h-l)/c*100
    f["rng10"]=(pd.Series(h).rolling(10).max()/pd.Series(l).rolling(10).min()-1)*100
    # outcome: entry open t+1
    ent=np.full(n,np.nan);ent[:-1]=o[1:]
    res={}
    for slp in (0.05,0.07,1.0):
        ret=np.full(n,np.nan);dayhit=np.full(n,np.nan)
        for t in range(n-H-1):
            e=o[t+1]
            if not e>0: continue
            tp=e*(1+TP);sl=e*(1-slp);r=None
            for k in range(1,H+1):
                j=t+k
                if l[j]<=sl and slp<1: r=-slp;dh=k;break
                if h[j]>=tp: r=TP;dh=k;break
            if r is None: r=c[t+H]/e-1;dh=H+1
            ret[t]=r*100;dayhit[t]=dh if r==TP else np.nan
        f[f"r_sl{int(slp*100)}"]=ret
        if slp==0.05: f["hitday"]=dayhit
    mx=np.full(n,np.nan)
    for t in range(n-H-1): mx[t]=(h[t+1:t+H+1].max()/o[t+1]-1)*100
    f["mfe9"]=mx
    f["hit5"]=(f.mfe9>=5).astype(float).where(f.mfe9.notna())
    out.append(f)
ds=pd.concat(out,ignore_index=True)
# market breadth
b=ds.groupby("date").agg(brd20=("d20",lambda x:(x>0).mean()),mret1=("ret1","median"),mret5=("ret5","median"))
ds=ds.merge(b,left_on="date",right_index=True)
ds["hsc"]=ds.ticker.isin(hsc)
ds.to_pickle("research/bull5/ds.pkl")
print(ds.shape, ds.hit5.mean(), ds.date.max())
