import sqlite3,numpy as np,pandas as pd
ds=pd.read_pickle("research/bull5/ds.pkl")
con=sqlite3.connect("mbss_ohlcv.db")
px=pd.read_sql("select ticker,date,open,high,low,close from ohlcv_daily order by ticker,date",con,parse_dates=["date"])
H=9;cols={}
res=[]
for tk,g in px.groupby("ticker"):
    g=g.reset_index(drop=True)
    if len(g)<120: continue
    g=g[g.date<="2026-10-20"].reset_index(drop=True)
    o,h,l,c=[g[x].to_numpy(float) for x in ("open","high","low","close")];n=len(g)
    r10=np.full(n,np.nan);r10n=np.full(n,np.nan)
    for t in range(n-H-1):
        e=o[t+1]
        if not e>0: continue
        for tag,sl,arr in (("sl8",.08,r10),("nosl",1.0,r10n)):
            r=None
            for k in range(1,H+1):
                j=t+k
                if sl<1 and l[j]<=e*(1-sl): r=-sl;break
                if h[j]>=e*1.10: r=.10;break
            if r is None: r=c[t+H]/e-1
            arr[t]=r*100
    res.append(pd.DataFrame({"ticker":tk,"date":g.date,"t10_sl8":r10,"t10_nosl":r10n}))
R=pd.concat(res)
ds=ds.merge(R,on=["ticker","date"],how="left")
ds["hit10"]=(ds.mfe9>=10).astype(float).where(ds.mfe9.notna())
ds.to_pickle("research/bull5/ds10.pkl");print(ds.hit10.mean())
