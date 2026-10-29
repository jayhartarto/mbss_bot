import sys;sys.path.insert(0,"research/bull5")
import numpy as np,pandas as pd
import sim_2026_10_28 as S
from sim_2026_10_28 import P,pos,ds,C,H
def path2(tk,di,mode,sl=.08,hard=.15,tp=.10):
    d,o,h,l,c=P[tk];i=pos[tk][di]
    if i+H>=len(d): return None
    e=o[i+1]
    if not e>0: return None
    stop=e*(1-sl);hs=e*(1-hard);touched=False
    for j in range(i+1,i+H+1):
        if mode=="intra":
            if l[j]<=stop: return (min(o[j],stop)/e-1)*100-C,j,d[j],touched
        else:
            if l[j]<=hs and mode in("close_hard","close_hard12"): return (min(o[j],hs)/e-1)*100-C,j,d[j],touched
            if mode=="close_hard12" : pass
            if l[j]<=stop: touched=True
        if h[j]>=e*(1+tp): return tp*100-C,j,d[j],touched
        if mode in("close","close_hard") and c[j]<=stop: return (c[j]/e-1)*100-C,j,d[j],touched
        if mode=="close_nextopen" and c[j]<=stop and j+1<=i+H: return (o[j+1]/e-1)*100-C,j+1,d[j+1],touched
        if mode=="nosl_hard" : pass
    j=i+H;return (c[j]/e-1)*100-C,j,d[j],touched
def run(sig,mode,**kw):
    rows=[]
    for r in sig.itertuples():
        p=path2(r.ticker,r.date,mode,**kw)
        if p: rows.append((r.date,p[0],p[3]))
    return pd.DataFrame(rows,columns=["date","ret","touched"])
sig=ds[ds["any"]&(ds.p3>=1)]
sigall=ds[ds["any"]]
modes=[("intraday SL-8 (sekarang)","intra",{}),("close-based SL-8","close",{}),("close-based SL-8 + hard -12","close_hard",{"hard":.12}),("close-based SL-8 + hard -15","close_hard",{"hard":.15}),("close-based SL-8, exit next open","close_nextopen",{}),("no SL, hard -15 only","close_hard",{"sl":.99,"hard":.15}),("no SL at all (9d)","close",{"sl":.99})]
for nm,s in (("konfirmasi 3d",sig),("semua alert gated",sigall)):
    print("==",nm)
    out=[]
    for lab,m,kw in modes:
        for per,(lo,hi) in (("train",("2023-01-01","2025-06-30")),("test",("2025-07-01","2026-10-20"))):
            r=run(s[(s.date>=lo)&(s.date<=hi)],m,**kw)
            out.append(dict(rule=lab,per=per,n=len(r),win=round((r.ret>0).mean()*100,1),avg=round(r.ret.mean(),2),worst=round(r.ret.min(),1),p5=round(r.ret.quantile(.05),1),loss_gt10=round((r.ret<-10.4).mean()*100,1)))
    print(pd.DataFrame(out).to_string(index=False))
# what happens after intraday touch of -8 that did NOT stop out on close
r=run(sig,"close");t=r[r.touched]
print("trades touching -8 intraday (close-based run):",len(t),"of",len(r),"| final avg ret",round(t.ret.mean(),2),"win",round((t.ret>0).mean()*100,1),"| vs those that exited intraday-style avg",round(run(sig,'intra').query('ret<-7').ret.mean(),2))
