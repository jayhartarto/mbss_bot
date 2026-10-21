"""
engine/broker_flow_db.py — MBSS v2 (2026-10-21).

Single history store for per-broker x per-ticker NET flow from RapidAPI's
GET /api/market-detector/broker-activity/{code}. Same schema for the one-off
backtest backfill (weekly windows, built on the laptop) and the nightly sweep
(single-day windows, written on the VPS), so the backfill file can simply be
merged into the live DB (see merge_into()).

Design rules (why the tables look like this):
  * A row is identified by (broker, ticker, window_start, window_end, board) -> merging two DB files is
    INSERT OR IGNORE, and daily and weekly windows coexist. Daily rows can be summed into weeks;
    a weekly row can never be split back into days.
  * net_value is SIGNED IDR (+ = broker net bought, - = net sold). Lots are re-signed from the value
    because the API sometimes returns a positive lot with a negative value (seen on INDY/AUTO/KOCI-W).
    Analysis should use value, not lots.
  * Each call returns only the top-N tickers per side, so a ticker that is ABSENT means |net| is below that
    call's cutoff, not that the broker did nothing. The cutoffs (min_abs_buy / min_abs_sell) are kept in
    flow_fetch_log so analysis can treat "absent" as ~0 with a known error bound.
  * flow_fetch_log records every call incl. EMPTY answers (HTTP 200, success, zero rows - the broker-summary
    endpoint does this a lot) and the remaining RapidAPI quota after the call.
  * flow_raw keeps the gzipped response so the parser can be fixed later without spending quota again.
"""
from __future__ import annotations

import datetime
import gzip
import json
import re
import sqlite3

SOURCE = "rapidapi_broker_activity"
EQUITY_RE = re.compile(r"^[A-Z]{4}$")  # drops warrants/rights/bonds like KOCI-W, INKPYUCN6A

SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_flow (
    broker_code  TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    window_start TEXT NOT NULL,      -- YYYY-MM-DD, first calendar day requested
    window_end   TEXT NOT NULL,      -- YYYY-MM-DD, last calendar day requested
    board        TEXT NOT NULL DEFAULT 'ALL',
    net_value    REAL NOT NULL,      -- signed IDR, + net buy / - net sell
    net_lot      REAL,               -- signed by net_value, magnitude from the API
    avg_price    REAL,
    rank_in_side INTEGER,            -- 1 = largest within its side of this call
    PRIMARY KEY (broker_code, ticker, window_start, window_end, board)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_flow_ticker_end ON broker_flow (ticker, window_end);
CREATE INDEX IF NOT EXISTS ix_flow_end ON broker_flow (window_end, broker_code);

CREATE TABLE IF NOT EXISTS flow_fetch_log (
    broker_code  TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    board        TEXT NOT NULL DEFAULT 'ALL',
    limit_req    INTEGER,
    http_status  INTEGER,
    n_buy        INTEGER,
    n_sell       INTEGER,
    min_abs_buy  REAL,               -- smallest |net| in the buy list = coverage cutoff
    min_abs_sell REAL,
    source       TEXT,
    fetched_at   TEXT,
    quota_remaining INTEGER,
    PRIMARY KEY (broker_code, window_start, window_end, board)
);

-- Market-wide ranking of EVERY broker for one window (RapidAPI "top broker"): one call covers ~80 brokers, so it is the
-- cheap way to get market-level group flows (BROKER_GROUP_GOVERNMENT / FOREIGN / LOCAL) and each broker's market share.
CREATE TABLE IF NOT EXISTS broker_market_flow (
    broker_code  TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    grp          TEXT,               -- vendor group, e.g. BROKER_GROUP_GOVERNMENT / FOREIGN / LOCAL
    name         TEXT,
    total_value  REAL,               -- buy + sell
    buy_value    REAL,
    sell_value   REAL,
    net_value    REAL,
    total_volume REAL,
    total_freq   REAL,
    PRIMARY KEY (broker_code, window_start, window_end)
) WITHOUT ROWID;

-- Broker rows read from a user's Brights/Stockbit SCREENSHOT. Kept apart from broker_flow on purpose: only the visible top
-- rows per side, investor-type tabs (Domestic/Foreign) instead of broker totals, and values RESCALED from lots to match the
-- header net (the app's lot column is ~3x too large). Not comparable with API rows; good for forward evaluation of the
-- exact information the user had when deciding, and for filling gaps the API sweep never covered.
CREATE TABLE IF NOT EXISTS broker_flow_screen (
    ticker       TEXT NOT NULL,
    captured_at  TEXT NOT NULL,      -- ISO timestamp the bot received the image
    tab          TEXT NOT NULL,      -- Domestic / Foreign / All
    window_text  TEXT,               -- date range as printed in the app, e.g. '02 Jun, 2026 - 08 Jun, 2026'
    broker_code  TEXT NOT NULL,
    signed_lot   REAL,               -- as read, sign-corrected (+ net buy / - net sell), app units
    avg_price    REAL,
    value_scaled REAL,               -- signed IDR after rescaling so visible rows sum to the header net
    scale        REAL,
    scale_source TEXT,               -- header | fallback
    header_buy   REAL,
    header_sell  REAL,
    header_net   REAL,
    image_sha1   TEXT,
    PRIMARY KEY (ticker, captured_at, tab, broker_code)
) WITHOUT ROWID;

-- What the bot told the user about a pick, so the verdict can be scored against what happened next.
CREATE TABLE IF NOT EXISTS pick_assessment (
    ticker       TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    verdict      TEXT,
    n_xl         REAL,               -- XL net / window traded value
    ret5_pct     REAL,
    window_text  TEXT,
    has_foreign  INTEGER,
    detail_json  TEXT,
    PRIMARY KEY (ticker, captured_at)
);

CREATE TABLE IF NOT EXISTS flow_raw (
    broker_code  TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    board        TEXT NOT NULL DEFAULT 'ALL',
    json_gz      BLOB NOT NULL,
    PRIMARY KEY (broker_code, window_start, window_end, board)
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def parse_activity(payload: dict) -> list[dict]:
    """payload = response.json(). Returns [{ticker, net_value, net_lot, avg_price, rank}, ...] for equities."""
    d = ((payload or {}).get("data") or {}).get("data") or {}
    bs = d.get("broker_summary") or {}
    out = []
    for rank, r in enumerate(bs.get("brokers_buy") or [], 1):
        t = r.get("netbs_stock_code")
        v = abs(r.get("bval") or 0)
        if t and EQUITY_RE.match(t) and v > 0:
            out.append(dict(ticker=t, net_value=float(v), net_lot=float(abs(r.get("blot") or 0)),
                            avg_price=r.get("netbs_buy_avg_price"), rank=rank))
    for rank, r in enumerate(bs.get("brokers_sell") or [], 1):
        t = r.get("netbs_stock_code")
        v = abs(r.get("sval") or 0)
        if t and EQUITY_RE.match(t) and v > 0:
            out.append(dict(ticker=t, net_value=-float(v), net_lot=-float(abs(r.get("slot") or 0)),
                            avg_price=r.get("netbs_sell_avg_price"), rank=rank))
    return out


def _cutoffs(payload: dict) -> tuple[int, int, float | None, float | None]:
    bs = ((((payload or {}).get("data") or {}).get("data") or {}).get("broker_summary")) or {}
    buy = [abs(r.get("bval") or 0) for r in bs.get("brokers_buy") or []]
    sell = [abs(r.get("sval") or 0) for r in bs.get("brokers_sell") or []]
    return len(buy), len(sell), (min(buy) if buy else None), (min(sell) if sell else None)


def already_fetched(conn, broker: str, ws: str, we: str, board: str = "ALL") -> bool:
    """True when this window was fetched AND returned rows (empty answers are retryable)."""
    row = conn.execute(
        "SELECT n_buy + n_sell FROM flow_fetch_log WHERE broker_code=? AND window_start=? AND window_end=? AND board=?",
        (broker, ws, we, board)).fetchone()
    return bool(row and (row[0] or 0) > 0)


def save_call(conn, broker: str, ws: str, we: str, payload: dict, http_status: int, limit_req: int,
              quota_remaining: int | None, board: str = "ALL") -> int:
    """Store one API answer (rows + log + raw). Returns the number of equity rows written."""
    rows = parse_activity(payload) if http_status == 200 else []
    n_buy, n_sell, min_buy, min_sell = _cutoffs(payload) if http_status == 200 else (0, 0, None, None)
    conn.executemany(
        "INSERT OR REPLACE INTO broker_flow (broker_code,ticker,window_start,window_end,board,net_value,net_lot,avg_price,rank_in_side)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(broker, r["ticker"], ws, we, board, r["net_value"], r["net_lot"], r["avg_price"], r["rank"]) for r in rows])
    conn.execute(
        "INSERT OR REPLACE INTO flow_fetch_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (broker, ws, we, board, limit_req, http_status, n_buy, n_sell, min_buy, min_sell, SOURCE,
         datetime.datetime.now().isoformat(timespec="seconds"), quota_remaining))
    conn.execute("INSERT OR REPLACE INTO flow_raw VALUES (?,?,?,?,?)",
                 (broker, ws, we, board, gzip.compress(json.dumps(payload).encode("utf-8"))))
    conn.commit()
    return len(rows)


def merge_into(dest_path: str, src_path: str) -> dict:
    """Merge a seed DB (e.g. the laptop backfill) into the live DB. Idempotent; existing rows win."""
    conn = connect(dest_path)
    conn.execute("ATTACH DATABASE ? AS src", (src_path,))
    before = conn.execute("SELECT COUNT(*) FROM broker_flow").fetchone()[0]
    for tbl in ("broker_flow", "broker_market_flow", "broker_flow_screen", "pick_assessment", "flow_fetch_log", "flow_raw"):
        conn.execute(f"INSERT OR IGNORE INTO main.{tbl} SELECT * FROM src.{tbl}")
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM broker_flow").fetchone()[0]
    conn.execute("DETACH DATABASE src")
    return {"rows_before": before, "rows_after": after, "added": after - before}


TOP_KEY = "__TOP__"  # broker_code used in flow_fetch_log / flow_raw for market-wide top-broker calls


def parse_top_brokers(payload: dict) -> list[dict]:
    """payload = response.json() of the top-broker endpoint (numbers arrive as strings)."""
    lst = (((payload or {}).get("data") or {}).get("data") or {}).get("list") or []
    f = lambda v: float(v) if v not in (None, "") else None  # noqa: E731
    return [dict(code=r.get("code"), grp=r.get("group"), name=r.get("name"), total_value=f(r.get("total_value")),
                 buy_value=f(r.get("buy_value")), sell_value=f(r.get("sell_value")), net_value=f(r.get("net_value")),
                 total_volume=f(r.get("total_volume")), total_freq=f(r.get("total_frequency"))) for r in lst if r.get("code")]


def save_top_brokers(conn, ws: str, we: str, payload: dict, http_status: int, quota_remaining: int | None) -> int:
    rows = parse_top_brokers(payload) if http_status == 200 else []
    conn.executemany(
        "INSERT OR REPLACE INTO broker_market_flow VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(r["code"], ws, we, r["grp"], r["name"], r["total_value"], r["buy_value"], r["sell_value"], r["net_value"],
          r["total_volume"], r["total_freq"]) for r in rows])
    conn.execute("INSERT OR REPLACE INTO flow_fetch_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (TOP_KEY, ws, we, "ALL", None, http_status, len(rows), 0, None, None, SOURCE + ":top_broker",
                  datetime.datetime.now().isoformat(timespec="seconds"), quota_remaining))
    conn.execute("INSERT OR REPLACE INTO flow_raw VALUES (?,?,?,?,?)",
                 (TOP_KEY, ws, we, "ALL", gzip.compress(json.dumps(payload).encode("utf-8"))))
    conn.commit()
    return len(rows)


def save_screen_capture(conn, ticker: str, captured_at: str, tabs: dict, assessment: dict | None = None) -> int:
    """tabs = {"Domestic": extracted, "Foreign": extracted}; rows rescaled with engine.broksum_read.scale_tab."""
    import engine.broksum_read as br
    n = 0
    for tab_name, tab in tabs.items():
        sc = br.scale_tab(tab)
        h = tab.get("header") or {}
        for r in sc["rows"]:
            conn.execute(
                "INSERT OR REPLACE INTO broker_flow_screen VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ticker, captured_at, tab_name, tab.get("date_range_text"), r["code"], r["signed_lot"], r["avg"], r["value"],
                 sc["scale"], sc["scale_source"], h.get("buy_value_idr"), h.get("sell_value_idr"), h.get("net_value_idr"),
                 tab.get("image_sha1")))
            n += 1
    if assessment and assessment.get("ok"):
        conn.execute(
            "INSERT OR REPLACE INTO pick_assessment VALUES (?,?,?,?,?,?,?,?)",
            (ticker, captured_at, assessment["verdict"], assessment["n_xl"], assessment.get("ret5"), assessment.get("window"),
             int(assessment.get("has_foreign_tab", False)),
             json.dumps({k: assessment[k] for k in ("role_pct", "retail_app_net_pct", "notes", "chasing")}, default=str)))
    conn.commit()
    return n


def save_inner(conn, broker: str, ws: str, we: str, inner: dict, limit_req: int, board: str = "ALL") -> int:
    """For callers that already unwrapped response['data']['data'] (engine.broker.fetch_rapidapi_broker_activity)."""
    return save_call(conn, broker, ws, we, {"success": True, "data": {"data": inner}}, 200, limit_req, None, board)
