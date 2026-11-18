"""engine/pick_ledger.py -- buku besar (ledger) hasil pick: SETIAP sinyal lane dicatat sekali dan dinilai otomatis terhadap beberapa aturan exit.

Kenapa: backtest (research/*_2026_11_18.py) menunjukkan hasil sangat tergantung periode/regime (rebound vs crash lanjut), n kecil, dan filter
dipilih di sampel yg sama. Ledger memberi bukti LIVE/forward yang tidak bisa di-overfit: apa yg bot sebut sinyal, lalu apa yg terjadi.

Tabel (pick_ledger.sqlite, tidak masuk git):
  pick_ledger(id, lane, ticker, signal_date, entry_ref, tier, ff_priority, phase, source, recorded_at)  UNIQUE(lane,ticker,signal_date)
     source: live | replay_oos | replay_in_sample | legacy_file   (live menang atas replay bila sinyal sama)
  pick_outcome(pick_id, status, entry_date, entry_open, bars, last_close, mfe, mae, dd60, rsi14, liq20, filt, <rule>_ret/_days/_closed ...)

Konvensi hasil (sama dgn riset): sinyal di close T, entry di OPEN T+1, biaya 0.4% round-trip, hit pakai high/low, stop dicek SEBELUM TP dalam bar
yg sama (konservatif), fill gap-aware. Posisi yg belum selesai dinilai di close terakhir (MTM) dan ditandai belum closed.

Aturan exit yang dinilai (key):
  osb_exist      SL -8%, TP +10%, BE +0.2% setelah high >= +5% (aktif bar berikut), maks 8 bar lalu close   (OSB-v2 produksi)
  bow_exist      SL -12%, TP = Bollinger upper (SMA20+2sd, band close bar sebelumnya), maks 16 bar lalu close (BOW produksi)
  new_tp10_sl20  TP +10%, SL -20%, maks 90 bar
  new_tp12_sl20  TP +12%, SL -20%, maks 90 bar
  tp10_nosl      TP +10%, tanpa SL, maks 90 bar (MTM kalau belum kena)
'filt' = filter riset 2026-11-18: turun >=20% dari high 60 bar DAN RSI14 >= 31 pada bar sinyal.
"""
from __future__ import annotations

import datetime
import os
import sqlite3

import numpy as np
import pandas as pd

import engine.legacy_core as core

DB_PATH = os.path.join(core.PROJECT_ROOT, "pick_ledger.sqlite")
COST_PCT = 0.4
RULES = ("osb_exist", "bow_exist", "new_tp10_sl20", "new_tp12_sl20", "tp10_nosl")
RULE_LABEL = {
    "osb_exist": "OSB-v2 existing (SL8/BE/TP10/8h)",
    "bow_exist": "BOW existing (SL12/BB/16h)",
    "new_tp10_sl20": "NEW TP10 SL20",
    "new_tp12_sl20": "NEW TP12 SL20",
    "tp10_nosl": "TP10 tanpa SL (90h)",
}
SOURCE_RANK = {"live": 0, "replay_oos": 1, "replay_in_sample": 2, "legacy_file": 3}
EPISODE_GAP_DAYS = 20  # sinyal lane+ticker yg sama dalam 20 hari kalender = 1 episode (dihitung hanya yg pertama)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute(
        "CREATE TABLE IF NOT EXISTS pick_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, lane TEXT NOT NULL, ticker TEXT NOT NULL, "
        "signal_date TEXT NOT NULL, entry_ref REAL, tier TEXT, ff_priority INTEGER, phase TEXT, source TEXT NOT NULL, recorded_at TEXT, "
        "UNIQUE(lane, ticker, signal_date))"
    )
    cols = ", ".join(f"{r}_ret REAL, {r}_days INTEGER, {r}_closed INTEGER" for r in RULES)
    c.execute(
        "CREATE TABLE IF NOT EXISTS pick_outcome (pick_id INTEGER PRIMARY KEY, status TEXT, entry_date TEXT, entry_open REAL, bars INTEGER, "
        "last_close REAL, mfe REAL, mae REAL, dd60 REAL, rsi14 REAL, liq20 REAL, filt INTEGER, updated_at TEXT, " + cols + ")"
    )
    return c


# ---------------------------------------------------------------- pencatatan
def record_picks(lane: str, picks: list[dict], source: str = "live") -> int:
    """Catat kandidat lane (dict dgn ticker + trigger_date/signal_date + entry_ref_price). Idempoten; sumber 'live' menimpa label replay."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    n_new = 0
    with _conn() as c:
        for p in picks or []:
            t = p.get("ticker")
            d = str(p.get("trigger_date") or p.get("signal_date") or "")[:10]
            if not t or not d:
                continue
            row = c.execute("SELECT id, source FROM pick_ledger WHERE lane=? AND ticker=? AND signal_date=?", (lane, t, d)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO pick_ledger(lane,ticker,signal_date,entry_ref,tier,ff_priority,phase,source,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (lane, t, d, p.get("entry_ref_price") or p.get("entry_ref"), p.get("tier"), int(bool(p.get("ff_priority", p.get("ff")))),
                     p.get("phase"), source, now),
                )
                n_new += 1
            elif SOURCE_RANK.get(source, 9) < SOURCE_RANK.get(row[1], 9):
                c.execute("UPDATE pick_ledger SET source=?, recorded_at=? WHERE id=?", (source, now, row[0]))
    return n_new


def import_csv(path: str, source_oos_from: str = "2026-09-01") -> int:
    """Import sinyal hasil replay (kolom: lane,ticker,trigger_date,tier,entry_ref,ff,phase). Tanggal >= source_oos_from diberi label
    replay_oos (filter riset dipilih dgn data sebelum tanggal itu), selebihnya replay_in_sample. Baris live yg sudah ada tidak ditimpa."""
    df = pd.read_csv(path)
    n = 0
    for src, sub in (("replay_in_sample", df[df.trigger_date < source_oos_from]), ("replay_oos", df[df.trigger_date >= source_oos_from])):
        n += record_picks_df(sub, src)
    return n


def record_picks_df(df: pd.DataFrame, source: str) -> int:
    n = 0
    for lane, g in df.groupby("lane"):
        n += record_picks(lane, g.to_dict("records"), source)
    return n


# ---------------------------------------------------------------- penilaian
def _features(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"].astype(float)
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    m, sd = c.rolling(20).mean(), c.rolling(20).std()
    return pd.DataFrame({
        "rsi": 100 - 100 / (1 + up / dn.replace(0, np.nan)),
        "dd60": (c / c.rolling(60).max() - 1) * 100,
        "liq": (c * df["Volume"].astype(float)).rolling(20).mean() / 1e9,
        "bb": m + 2 * sd,
    }, index=df.index)


def _rule_tp_sl(o, h, l, c, ep, tp, sl, T):
    N = len(c)
    lv = ep * (1 + tp / 100)
    st = ep * (1 - sl / 100) if sl else None
    for k in range(min(T, N)):
        if st is not None and l[k] <= st:
            return min(o[k], st), k, True
        if h[k] >= lv:
            return max(o[k], lv), k, True
    k = min(T, N) - 1
    return c[k], k, N >= T


def _rule_osb(o, h, l, c, ep):
    sl, tp, be_on, be_lv, armed = ep * 0.92, ep * 1.10, ep * 1.05, ep * 1.002, False
    N = len(c)
    for k in range(min(8, N)):
        st = max(sl, be_lv) if armed else sl
        if l[k] <= st:
            return min(o[k], st), k, True
        if h[k] >= tp:
            return max(o[k], tp), k, True
        if h[k] >= be_on:
            armed = True
    k = min(8, N) - 1
    return c[k], k, N >= 8


def _rule_bow(o, h, l, c, bb, ep):
    sl = ep * 0.88
    N = len(c)
    for k in range(min(16, N)):
        tp = bb[k]  # band pada close bar sebelumnya (bb sudah digeser 1 bar oleh pemanggil)
        if l[k] <= sl:
            return min(o[k], sl), k, True
        if tp == tp and h[k] >= tp:
            return max(o[k], tp), k, True
    k = min(16, N) - 1
    return c[k], k, N >= 16


def compute_outcome(df: pd.DataFrame, signal_date: str) -> dict | None:
    """df: OHLCV harian (kolom Open/High/Low/Close/Volume, index tanggal naik). None kalau tanggal sinyal tak ada di data."""
    sd = pd.Timestamp(signal_date)
    if df is None or df.empty or sd not in df.index:
        return None
    i = df.index.get_loc(sd)
    f = _features(df)
    out = dict(dd60=float(f["dd60"].iloc[i]) if pd.notna(f["dd60"].iloc[i]) else None, rsi14=float(f["rsi"].iloc[i]) if pd.notna(f["rsi"].iloc[i]) else None,
               liq20=float(f["liq"].iloc[i]) if pd.notna(f["liq"].iloc[i]) else None)
    out["filt"] = int(out["dd60"] is not None and out["rsi14"] is not None and out["dd60"] <= -20 and out["rsi14"] >= 31)
    if i + 1 >= len(df):
        out.update(status="PENDING", bars=0)
        return out
    e = i + 1
    ep = float(df["Open"].iloc[e])
    if not ep > 0:
        out.update(status="PENDING", bars=0)
        return out
    o, h, l, c = (df[k].to_numpy(float)[e:] for k in ("Open", "High", "Low", "Close"))
    bb = f["bb"].shift(1).to_numpy()[e:]
    N = len(c)
    out.update(entry_date=str(df.index[e].date()), entry_open=ep, bars=N, last_close=float(c[-1]),
               mfe=float((h[:90].max() / ep - 1) * 100), mae=float((l[:90].min() / ep - 1) * 100))
    res = {
        "osb_exist": _rule_osb(o, h, l, c, ep),
        "bow_exist": _rule_bow(o, h, l, c, bb, ep),
        "new_tp10_sl20": _rule_tp_sl(o, h, l, c, ep, 10, 20, 90),
        "new_tp12_sl20": _rule_tp_sl(o, h, l, c, ep, 12, 20, 90),
        "tp10_nosl": _rule_tp_sl(o, h, l, c, ep, 10, None, 90),
    }
    all_closed = True
    for r, (px, k, closed) in res.items():
        out[f"{r}_ret"] = (px / ep - 1) * 100 - COST_PCT
        out[f"{r}_days"] = int(k + 1)
        out[f"{r}_closed"] = int(closed)
        all_closed &= closed
    out["status"] = "DONE" if all_closed else "ACTIVE"
    return out


def filter_info(ticker: str, signal_date: str | None = None) -> dict | None:
    """Filter riset (dd60<=-20 & RSI14>=31) pada bar sinyal -- dipakai /swing utk tag. None kalau data tidak cukup."""
    try:
        df = core.get_ohlcv_daily_from_db(ticker, limit=250)
        if df is None or df.empty:
            return None
        if signal_date:
            df = df[df.index <= pd.Timestamp(str(signal_date)[:10])]
        if len(df) < 70:
            return None
        f = _features(df)
        dd, rsi = f["dd60"].iloc[-1], f["rsi"].iloc[-1]
        if pd.isna(dd) or pd.isna(rsi):
            return None
        return dict(dd60=float(dd), rsi14=float(rsi), filt=bool(dd <= -20 and rsi >= 31))
    except Exception:
        return None


def resolve_all(verbose: bool = False) -> dict:
    """Hitung ulang outcome semua pick yg belum DONE (idempoten, aman dijalankan tiap malam). Return ringkasan jumlah."""
    cnt = {"checked": 0, "done": 0, "active": 0, "pending": 0, "missing": 0}
    now = datetime.datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        todo = c.execute(
            "SELECT p.id, p.ticker, p.signal_date FROM pick_ledger p LEFT JOIN pick_outcome o ON o.pick_id=p.id WHERE o.status IS NULL OR o.status != 'DONE'"
        ).fetchall()
        cache: dict[str, pd.DataFrame] = {}
        for pid, t, d in todo:
            cnt["checked"] += 1
            if t not in cache:
                cache[t] = core.get_ohlcv_daily_from_db(t, limit=500)
            out = compute_outcome(cache[t], d)
            if out is None:
                cnt["missing"] += 1
                continue
            cols = ["pick_id", "updated_at"] + list(out.keys())
            vals = [pid, now] + list(out.values())
            c.execute(f"INSERT OR REPLACE INTO pick_outcome({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
            cnt[out["status"].lower()] += 1
    if verbose:
        print(f"📒 Ledger resolve: {cnt}")
    return cnt


# ---------------------------------------------------------------- ringkasan
def load_ledger(lane: str | None = None, since: str | None = None, source: str | None = None, first_only: bool = True) -> pd.DataFrame:
    with _conn() as c:
        df = pd.read_sql_query("SELECT p.*, o.* FROM pick_ledger p JOIN pick_outcome o ON o.pick_id=p.id WHERE o.status IN ('ACTIVE','DONE')", c)
    if df.empty:
        return df
    df = df.loc[:, ~df.columns.duplicated()]
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    if lane:
        df = df[df.lane == lane]
    if since:
        df = df[df.signal_date >= pd.Timestamp(since)]
    if source:
        df = df[df.source == source] if source != "replay" else df[df.source.str.startswith("replay")]
    if first_only and len(df):
        keep, last = [], {}
        for r in df.sort_values("signal_date").itertuples():
            k = (r.lane, r.ticker)
            if k in last and (r.signal_date - last[k]).days < EPISODE_GAP_DAYS:
                continue
            last[k] = r.signal_date
            keep.append(r.Index)
        df = df.loc[keep]
    return df


def rule_stats(df: pd.DataFrame, rule: str) -> dict | None:
    if df.empty:
        return None
    x = df[f"{rule}_ret"].astype(float)
    d = df[f"{rule}_days"].astype(float)
    closed = df[f"{rule}_closed"].astype(int)
    return dict(n=len(df), open=int((closed == 0).sum()), win=100 * (x > 0).mean(), mean=x.mean(), days=d.mean(), rpd=x.mean() / d.mean() if d.mean() else float("nan"),
                p10=x.quantile(0.1), worst=x.min())


def build_ledger_message(lane: str | None = None, since: str | None = None, source: str | None = None) -> str:
    df = load_ledger(lane, since, source)
    if df.empty:
        return "📒 LEDGER kosong untuk filter ini (belum ada sinyal yg dinilai)."
    head = f"📒 LEDGER PICK — {len(df)} episode sinyal ({df.signal_date.min().date()}..{df.signal_date.max().date()})"
    if lane or since or source:
        head += f" | filter: {lane or 'semua lane'}{' ≥' + since if since else ''}{' src=' + source if source else ''}"
    lines = [head, "Entry open T+1, biaya 0.4%, posisi belum selesai dinilai di close terakhir (MTM). ret/hr = mean ÷ hari tahan."]
    srcs = df.groupby("source").size().to_dict()
    lines.append("Sumber: " + ", ".join(f"{k} {v}" for k, v in srcs.items()))
    for ln, g in df.groupby("lane"):
        for tag, sub in (("semua", g), ("lolos filter dd60/RSI", g[g.filt == 1])):
            if len(sub) < 3:
                continue
            lines.append(f"\n▶ {ln} — {tag} (n={len(sub)})")
            for r in RULES:
                s = rule_stats(sub, r)
                lines.append(f"  {RULE_LABEL[r]:32s} win {s['win']:4.0f}% mean {s['mean']:+5.1f}% {s['days']:4.1f}hr ret/hr {s['rpd']:+.2f}% p10 {s['p10']:+5.0f}% open {s['open']}")
    lines.append("\nCatatan: replay_in_sample = rekonstruksi engine produksi pada data yg dipakai memilih filter (bias optimis); replay_oos & live = bukti forward.")
    return "\n".join(lines)
