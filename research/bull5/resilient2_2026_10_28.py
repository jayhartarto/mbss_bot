import sys,warnings;warnings.filterwarnings("ignore")
exec(open("research/bull5/resilient_2026_10_28.py").read().split("# crash windows")[0])
from sklearn.linear_model import LogisticRegression
C=C.ffill(limit=3);R=C.pct_change()
print('bad calendar days:',[str(d.date()) for d in C.index[C.notna().sum(axis=1)<300]][-8:])
H=35
W=[("2024-10-30",H),("2025-01-23",H),("2026-01-09",H),("2026-04-01",H),("2026-08-31",None)]
def build(shift):
    out=[]
    for d,h in W:
        s0=dates.get_loc(pd.Timestamp(d)); e=s0+h if h else len(dates)-1
        s=s0+shift; sel=(mem.iloc[s]==1)&C.iloc[s].notna()&C.iloc[e].notna()
        f=feats(s)[sel];r=(C.iloc[e]/C.iloc[s]-1)[sel]
        f["ret"]=r*100;f["y_up"]=(r>0).astype(int);f["win"]=d;f["sector"]=sec.reindex(f.index)
        out.append(f)
    return pd.concat(out)
def score(g,cols):
    s=0
    for c,sgn in cols: s=s+sgn*g[c].rank(pct=True)
    return s/len(cols)
COMBOS={"A: -dnbeta120 + hi250 + r20":[("dnbeta120",-1),("hi250",1),("r20",1)],
        "B: -beta120 + hi250 + r120":[("beta120",-1),("hi250",1),("r120",1)],
        "C: -corr120 + -dnbeta120 + hi250":[("corr120",-1),("dnbeta120",-1),("hi250",1)],
        "D: -dnbeta120 + hi250":[("dnbeta120",-1),("hi250",1)],
        "E: hi250 only":[("hi250",1)],"F: -dnbeta120 only":[("dnbeta120",-1)],"G: r20 + r5 (in-crash RS)":[("r20",1),("r5",1)]}
for shift,label in ((0,"AS OF WINDOW START (pre-crash indicators)"),(10,"AS OF DAY 10 of crash (predict remaining days)")):
    F=build(shift)
    print("=====",label)
    rows=[]
    for nm,cols in COMBOS.items():
        for d,g in F.groupby("win"):
            g=g.dropna(subset=[c for c,_ in cols]).copy();g["sc"]=score(g,cols)
            top=g[g.sc.rank(pct=True)>=0.85]
            rows.append(dict(combo=nm,win=d,n_top=len(top),up_top=(top.y_up.mean()),up_all=g.y_up.mean(),med_top=top.ret.median(),med_all=g.ret.median(),auc=roc_auc_score(g.y_up,g.sc)))
    RR=pd.DataFrame(rows)
    sm=RR.groupby("combo").agg(auc_mean=("auc","mean"),auc_min=("auc","min"),up_top=("up_top","mean"),up_all=("up_all","mean"),med_top=("med_top","mean"),med_all=("med_all","mean"))
    print(sm.round(2).sort_values("auc_mean",ascending=False).to_string())
    if shift==0:
        print("-- per window for combo A and D");print(RR[RR.combo.str.startswith(("A:","D:"))].round(2).to_string(index=False))
    # leave-one-window-out logistic on standardised features
    cols=["dnbeta120","hi250","r20","beta120","r120","corr120","vol60","upshare60"]
    aucs=[]
    for d in F.win.unique():
        tr=F[F.win!=d].dropna(subset=cols);te=F[F.win==d].dropna(subset=cols)
        mu,sd=tr[cols].mean(),tr[cols].std()
        m=LogisticRegression(C=0.3,max_iter=500).fit((tr[cols]-mu)/sd,tr.y_up)
        p=m.predict_proba((te[cols]-mu)/sd)[:,1];te=te.assign(p=p);top=te[te.p.rank(pct=True)>=0.85]
        aucs.append((d,round(roc_auc_score(te.y_up,p),2),round(top.y_up.mean(),2),round(top.ret.median(),1),round(te.ret.median(),1)))
    print("LOWO logistic (win, auc, share_up_in_top15%, median ret top, median ret all):",aucs)
    if shift==0:
        print("coef (full):",dict(zip(cols,LogisticRegression(C=0.3,max_iter=500).fit((F.dropna(subset=cols)[cols]-F[cols].mean())/F[cols].std(),F.dropna(subset=cols).y_up).coef_[0].round(2))))
# who survived in Sep-Oct and what they looked like at 08-31
F0=build(0);S=F0[F0.win=="2026-08-31"].copy();S["sc"]=score(S,COMBOS["A: -dnbeta120 + hi250 + r20"])
S["rank_sc"]=S.sc.rank(pct=True)
cols=["ret","dnbeta120","beta120","hi250","r20","r120","vol60","rank_sc","sector"]
print("== Sep-Oct winners (ret>0) indicator profile vs rest (median):")
print(S.groupby("y_up")[["dnbeta120","beta120","corr120","hi250","d200","r20","r120","vol60","upshare60","liq"]].median().round(2).to_string())
print(S[S.y_up==1].sort_values("ret",ascending=False)[cols].round(2).to_string())
print("top15% by score A:",S[S.rank_sc>=.85].sort_values("ret",ascending=False)[["ret","rank_sc","sector"]].round(2).T.to_string())
# current candidates as of last date
s=len(dates)-1;sel=(mem.iloc[s]==1)&C.iloc[s].notna();f=feats(s)[sel]
f["sc"]=score(f.dropna(subset=["dnbeta120","hi250","r20"]),COMBOS["A: -dnbeta120 + hi250 + r20"])
f["sector"]=sec.reindex(f.index);print("== Candidates as of 2026-10-20 (score A top 15):");print(f.sort_values("sc",ascending=False).head(15)[["sc","dnbeta120","beta120","hi250","r20","r120","vol60","liq","sector"]].round(2).to_string())
