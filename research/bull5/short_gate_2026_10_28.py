import pandas as pd,numpy as np,itertools,warnings;warnings.filterwarnings("ignore")
pd.set_option("display.width",230)
T=pd.read_pickle("research/bull5/T2.pkl");J=pd.read_pickle("research/bull5/J2.pkl")
T=T.dropna(subset=["d50","d20","d10","d5","r3","r5"]).copy()
ND={p:T[T.per==p].date.nunique() for p in ("train","test")}
def ev(df,m,nm=None):
    o={}
    for p in ("train","test"):
        a=df[(df.per==p)&m.reindex(df.index).fillna(False).values] if False else df[(df.per==p)&m[df.per==p]]
        g=a.groupby("date").ret.mean()
        o[p]=(len(a),a.ret.mean() if len(a) else np.nan,100*(a.ret>0).mean() if len(a) else np.nan,100*a.sl.mean() if len(a) else np.nan,g.mean()/(g.std()/np.sqrt(max(len(g),2))) if len(g)>2 else np.nan)
    return o
def line(nm,df,m):
    o=ev(df,m);a,b=o["train"],o["test"]
    return dict(rule=nm,n_tr=a[0],avg_tr=a[1],win_tr=a[2],sl_tr=a[3],n_te=b[0],avg_te=b[1],win_te=b[2],sl_te=b[3],t_te=b[4],perday_te=b[0]/ND["test"])
for setname,df in (("CONFIRM3 (p3>=1)",T[T.p3>=1].copy()),("ALL raw lane signals",T.copy())):
    print("=========",setname,"n",len(df))
    base=[line("no gate",df,df.ret==df.ret),line("breadth mret5>=-0.5",df,df.mret5>=-0.5),line("IHSG d50>=-1.1 (long gate)",df,df.d50>=-1.1)]
    # candidate short gates
    G={}
    for f,ths in {"d3":(-1,-.5,0,.5),"d5":(-2,-1.5,-1,-.5,0,.5,1),"d10":(-3,-2,-1.5,-1,-.5,0,.5,1),"d20":(-4,-3,-2,-1.5,-1,-.5,0),"r1":(-1,-.5,0),"r2":(-1.5,-1,-.5,0),"r3":(-3,-2,-1.5,-1,-.5,0),"r5":(-4,-3,-2,-1.5,-1,0),"r10":(-4,-3,-2,-1,0),
                   "above5low":(0.5,1,1.5,2,3),"above10low":(1,2,3,4,5),"dd10":(-8,-6,-5,-4,-3,-2),"dd20":(-12,-10,-8,-6,-4),"sma5_10":(-1,-.5,-.25,0),"green3":(1,2),"green5":(2,3),"min3":(-3,-2,-1.5,-1),"min5":(-4,-3,-2,-1.5),"min10":(-5,-3,-2,-1.5)}.items():
        for t in ths: G[f"IHSG {f}>={t}"]=(df[f]>=t)
    res=[]
    for nm,m in G.items():
        r=line(nm,df,m)
        if r["n_tr"]>=60 and r["n_te"]>=60: res.append(r)
    R=pd.DataFrame(res);R["floor"]=R[["avg_tr","avg_te"]].min(axis=1)
    print("-- reference");print(pd.DataFrame(base).round(2).to_string(index=False))
    print("-- best single short gates by min(avg_tr,avg_te)");print(R.sort_values("floor",ascending=False).head(12).drop(columns="floor").round(2).to_string(index=False))
    # two-feature combos among the best 12 singles + long gate hysteresis
    top=list(R.sort_values("floor",ascending=False).head(14).rule)
    res2=[]
    for a,b in itertools.combinations(top,2):
        if a.split()[1].split(">=")[0]==b.split()[1].split(">=")[0]: continue
        r=line(a+" & "+b[5:],df,G[a]&G[b])
        if r["n_tr"]>=60 and r["n_te"]>=60: res2.append(r)
    R2=pd.DataFrame(res2);R2["floor"]=R2[["avg_tr","avg_te"]].min(axis=1)
    print("-- best 2-feature short gates");print(R2.sort_values("floor",ascending=False).head(8).drop(columns="floor").round(2).to_string(index=False))
    out=[line("d50>=-1.1 OR (short combo A)",df,(df.d50>=-1.1)|((df.d5>=0)&(df.r3>=0)))]
    print("-- hysteresis ideas");print(pd.DataFrame(out+[line("d50>=-1.1 OR (d10>=0 & r5>=0)",df,(df.d50>=-1.1)|((df.d10>=0)&(df.r5>=0))),line("d10>=0 & r5>=0",df,(df.d10>=0)&(df.r5>=0)),line("d20>=-1 & r3>=0",df,(df.d20>=-1)&(df.r3>=0)),line("d5>=0 & d10>=-0.5",df,(df.d5>=0)&(df.d10>=-.5))]).round(2).to_string(index=False))
# share of days gate open by year for chosen candidates
Jv=J.dropna(subset=["d50","d10","d5"])
for nm,m in {"d50>=-1.1":Jv.d50>=-1.1,"d10>=0 & r5>=0":(Jv.d10>=0)&(Jv.r5>=0),"d5>=0 & r3>=0":(Jv.d5>=0)&(Jv.r3>=0),"d20>=-1 & r3>=0":(Jv.d20>=-1)&(Jv.r3>=0)}.items():
    print(nm,"open share by year:",m.groupby(Jv.index.year).mean().round(2).to_dict())
