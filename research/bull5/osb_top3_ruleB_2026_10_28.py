"""Rule B for OSB-v2: show only the top-N picks per day (ranked by booster), whether the IHSG hybrid gate is open or closed."""
import sqlite3,sys,warnings,pickle;warnings.filterwarnings("ignore")
import numpy as np,pandas as pd
sys.path.insert(0,".")
from engine.broker_roles import role_of
pd.set_option("display.width",220)
EP=pickle.load(open("research/bull5/lane_episodes_open.pkl","rb"));J=pd.read_pickle("research/bull5/J2.pkl")
e=EP["OSBv2"].copy();e["date"]=pd.to_datetime(e["date"])
Jx=J[["d50","d5","above10low"]].rename(columns={"d50":"j50","d5":"j5","above10low":"jlow"})
e=e.merge(Jx,left_on="date",right_index=True,how="left");e["hyb"]=(e.j50>=-1.1)|((e.jlow>=4)&(e.j5>=0))
# liquidity + new-buyer (nb_ref) at signal date
o=pd.read_sql_query("SELECT ticker,date,volume,close FROM ohlcv_daily WHERE date>='2022-10-01' ORDER BY ticker,date",sqlite3.connect("mbss_ohlcv.db"))
con=sqlite3.connect("file:research/ipot_merged.sqlite?mode=ro",uri=True)
d=pd.read_sql_query("SELECT ticker,trade_date date,broker_code broker,buy_lot,sell_lot FROM ipot_broker_daily WHERE fd='all' AND board='all'",con)
d=d[d.ticker.isin(e.ticker.unique())].copy()
ms=d.groupby(["ticker","date"]).sell_lot.transform("min").fillna(0);sl=d.sell_lot.fillna(0);bl=d.buy_lot.fillna(0)
d["net"]=np.where(d.buy_lot.isna(),-sl,bl-np.where(d.sell_lot.isna(),ms,sl))
dg={t:x for t,x in d.groupby("ticker")};REF=("foreign_inst","local_other","foreign_retail_base")
rows=[]
for t,g in o.groupby("ticker"):
    if t not in dg: continue
    cal=g.date.values;vol=g.volume.values/100.0;v5=pd.Series(vol).rolling(5).sum().values;v20=pd.Series(vol).rolling(20).sum().values
    pv=dg[t].pivot_table(index="date",columns="broker",values="net",aggfunc="sum").reindex(cal).fillna(0.0)
    roll5=pv.rolling(5).sum().div(v5,axis=0);pa=pv.abs().rolling(20).sum().shift(5).div(v20,axis=0)
    ref=[b for b in pv.columns if role_of(b) in REF];new=pa[ref]<0.0025
    nb=(roll5[ref].clip(lower=0).where(new,0)).sum(axis=1).values if ref else np.zeros(len(cal))
    liq=pd.Series(g.close.values*g.volume.values).rolling(20).mean().values
    rows.append(pd.DataFrame({"ticker":t,"date":pd.to_datetime(cal),"nb_ref":nb,"liq20":liq}))
F=pd.concat(rows);e=e.merge(F,on=["ticker","date"],how="left")
e["tag"]=(e.nb_ref>=0.057)&(e.liq20>=3e8)
e["score"]=e.pos_days_5.fillna(0)+2*e.tag.astype(int)         # booster rank: FF pos days (0-5) + 2 if new-buyer tag
e.to_pickle("research/bull5/osb_ruleB.pkl")
def pick(df,N,key):
    out=[]
    for dt,g in df.groupby("date"):
        if key=="booster": g=g.sort_values(["score","liq20"],ascending=False)
        elif key=="liq": g=g.sort_values("liq20",ascending=False)
        elif key=="ff": g=g.sort_values(["pos_days_5","liq20"],ascending=False)
        elif key=="worst": g=g.sort_values(["score","liq20"],ascending=True)
        out.append(g.head(N))
    return pd.concat(out)
def st(g):
    if len(g)==0: return "n=0"
    c=g[~g.is_open]
    return f"n={len(g):3d} win={100*(g.ret_pct>0).mean():3.0f}% mean={g.ret_pct.mean():+5.2f} stop20={100*(g.ret_pct<=-19.9).mean():2.0f}% | closed-only n={len(c):3d} win={100*(c.ret_pct>0).mean() if len(c) else float('nan'):3.0f}% mean={c.ret_pct.mean() if len(c) else float('nan'):+5.2f}"
for lbl,lo in (("JUL - OKT 2026 (signals 1 Jul..5 Okt)","2026-07-01"),("FULL HISTORY (Nov 2024 - Okt 2026)","2000-01-01")):
    w=e[e.date>=lo]
    print("=====",lbl,"| days with signals:",w.date.nunique(),"| signals:",len(w))
    print(f"  all signals (no top-N)                  {st(w)}")
    for N in (3,5):
        for key,nm in (("booster","booster (FF pos_days_5 + new-buyer tag)"),("liq","liquidity only"),("worst","WORST-ranked (sanity)")):
            print(f"  top{N} by {nm:40s} {st(pick(w,N,key))}")
    t3=pick(w,3,"booster")
    print("  top3 booster split by IHSG gate: OPEN",st(t3[t3.hyb]),"\n"+" "*58+"CLOSED",st(t3[~t3.hyb]))
t3=pick(e[e.date>="2026-07-01"],3,"booster")
print("\ntop3 picks Jul-Oct 2026:");print(t3.sort_values("date")[["date","ticker","pos_days_5","tag","score","hyb","ret_pct","is_open"]].round(1).to_string(index=False))
