import pandas as pd,numpy as np,itertools,pickle
ds=pd.read_pickle("research/bull5/ds10.pkl")
ds=ds[(ds.liq>=1)&(ds.close>=100)&(~ds.hsc)].dropna(subset=["t10_sl8","r_sl5","sma50","hi60","atr14","rsi","volr","pctb"]).reset_index(drop=True)
COST=0.4
feats=["ret1","ret3","ret5","ret10","ret20","d5","d20","d50","slope20","atr14","atrratio","rsi","pctb","bbw","cpos","body","gap","volr","volr5","liq","hi60","hi20","lo20","nup3","nup5","rng_pct","rng10"]
tr=ds.date<"2025-07-01";te=~tr
y=ds.t10_sl8.to_numpy()-COST
Xtr=ds[tr];ytr=y[tr.to_numpy()]
# candidate conditions as boolean masks on train (thresholds from train quantiles)
conds=[]
for f in feats:
    qs=np.unique(np.nanquantile(Xtr[f],[.05,.1,.2,.3,.4,.5,.6,.7,.8,.9,.95]))
    for q in qs:
        conds.append((f,">=",q));conds.append((f,"<=",q))
def mask(df,c):
    f,op,q=c;return (df[f]>=q).to_numpy() if op==">=" else (df[f]<=q).to_numpy()
M=[mask(Xtr,c) for c in conds]
MIN_N=300
fold=(Xtr.date.dt.year.astype(str)+(Xtr.date.dt.month>6).astype(int).astype(str)).to_numpy()
FU=np.unique(fold);FM=[fold==u for u in FU]
def score(m):
    n=m.sum()
    if n<MIN_N: return -9
    r=ytr[m];fm=[ytr[m&f].mean() if (m&f).sum()>=30 else -9 for f in FM]
    return r.mean()-1.0*r.std()/np.sqrt(n)+0.4*(sum(x>0 for x in fm)-2.5)
beam=[((),np.ones(len(Xtr),bool),-9)]
allres=[]
for depth in range(4):
    cand={}
    for combo,m,_ in beam:
        for i,c in enumerate(conds):
            if i in combo: continue
            nm=m&M[i];s=score(nm)
            if s>-8:
                key=tuple(sorted(combo+(i,)))
                if key not in cand or cand[key][2]<s: cand[key]=(key,nm,s)
    beam=sorted(cand.values(),key=lambda x:-x[2])[:60]
    allres+=beam
    print("depth",depth+1,"best",beam[0][2],[conds[i] for i in beam[0][0]],flush=True)
pickle.dump((conds,[(k,s) for k,_,s in allres]),open("research/bull5/beam10.pkl","wb"))
# evaluate
rows=[]
dtr=Xtr.date.nunique();
for key,_,s in allres:
    mt=np.ones(len(ds),bool)
    for i in key: mt&=mask(ds,conds[i])
    a=mt&tr.to_numpy();b=mt&te.to_numpy()
    if b.sum()<30: continue
    rows.append(dict(rule=" & ".join(f"{conds[i][0]}{conds[i][1]}{conds[i][2]:.3g}" for i in key),
      n_tr=a.sum(),ev_tr=y[a].mean(),hit_tr=ds.hit10[a].mean(),n_te=b.sum(),ev_te=y[b].mean(),hit_te=ds.hit10[b].mean(),
      perday_te=b.sum()/ds[te].date.nunique(),ev_26=y[mt&(ds.date>="2026-01-01").to_numpy()].mean(),n_26=(mt&(ds.date>="2026-01-01").to_numpy()).sum(),days_te=ds[b].date.nunique()))
R=pd.DataFrame(rows).drop_duplicates("rule").sort_values("ev_te",ascending=False)
R.to_csv("research/bull5/rules10.csv",index=False)
pd.set_option("display.width",250);pd.set_option("display.max_colwidth",120)
print("baseline tr/te",ytr.mean(),y[te.to_numpy()].mean());print(R.head(30).round(3).to_string())
