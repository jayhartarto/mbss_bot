"""
research/ipot_explore/common.py — loaders and constants shared by the IPOT broker-summary research (see ../IPOT_BROKER_RESEARCH_FRAMEWORK.md).

Conventions:
  * date = trading day the IPOT summary describes. The summary is published AFTER the close, so a feature of day t can be acted on at the
    open of t+1 at the earliest -> outcomes in features.py are measured from open(t+1).
  * IPOT values are IDR, lots are 100 shares: avg price ~= val / (lot * 100).
  * A broker missing from a side of a ticker-day is BELOW that side's 10th place, not zero (see features.broker_day_table for the net bounds).
"""
from __future__ import annotations

import os
import sqlite3

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IPOT_DB = os.path.join(ROOT, "research", "ipot_broker_daily.sqlite")
OHLCV_DB = os.path.join(ROOT, "mbss_ohlcv.db")
OUT_DIR = os.path.join(ROOT, "research", "ipot_explore", "out")

# PROVISIONAL static broker classes (from the notes in engine/tanya_knowledge.py / memory). The research replaces them with behaviour-based
# clusters (engine 3 of the framework); until then every code not listed here is "other".
BROKER_CLASS = {
    "CC": "gov", "OD": "gov", "NI": "gov", "DX": "gov",
    "CP": "scalper", "MG": "scalper",
    "XL": "retail_app", "XC": "retail_app", "YP": "retail_app", "PD": "retail_app",
    "ZP": "foreign_retail", "YU": "foreign_retail",
    "AK": "foreign_inst", "BK": "foreign_inst", "KZ": "foreign_inst", "RX": "foreign_inst", "GA": "foreign_inst",
}


def load_ipot(db_path: str = IPOT_DB) -> pd.DataFrame:
    """Raw broker rows (status 'ok' calls only), columns: ticker, date, fd, board, broker + buy/sell lot/val/avg/rank."""
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query("SELECT * FROM ipot_broker_daily", conn)
    finally:
        conn.close()
    df = df.rename(columns={"trade_date": "date", "broker_code": "broker"})
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    return df


def load_fetch_log(db_path: str = IPOT_DB) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        log = pd.read_sql_query("SELECT * FROM ipot_fetch_log", conn)
    finally:
        conn.close()
    log = log.rename(columns={"trade_date": "date"})
    log["date"] = pd.to_datetime(log["date"]).astype("datetime64[ns]")
    return log


def load_ohlcv(tickers: list[str] | None = None, start: str | None = None, db_path: str = OHLCV_DB) -> pd.DataFrame:
    """Daily bars with the .JK suffix stripped if present. Columns: ticker, date, open, high, low, close, volume, value (close*volume)."""
    q = "SELECT ticker, date, open, high, low, close, volume FROM ohlcv_daily"
    cond, params = [], []
    if start:
        cond.append("date >= ?")
        params.append(start)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        px = pd.read_sql_query(q + " ORDER BY ticker, date", conn, params=params)
    finally:
        conn.close()
    px["ticker"] = px["ticker"].str.replace(".JK", "", regex=False)
    if tickers is not None:
        px = px[px["ticker"].isin(set(tickers))]
    px["date"] = pd.to_datetime(px["date"]).astype("datetime64[ns]")
    px["value"] = px["close"] * px["volume"]
    return px.reset_index(drop=True)
