"""
engine/broker_roles.py — MBSS v2 (2026-10-21).

Reference classification of IDX broker codes for the bandarmology reading (screenshot reader + sweep).
Built from RapidAPI's `top-broker` groups (BROKER_GROUP_GOVERNMENT / FOREIGN / LOCAL, 1 month ending
2026-10-21, ~89 brokers) plus observed behaviour in this project's samples. The vendor group is about
OWNERSHIP; `role` is about WHO TRADES THROUGH the broker (what matters for reading flows):

  gov_inst            CC, OD, NI, DX             state-owned houses, large institutional/block flow
  foreign_inst        AK, BK, KZ, RX, DR, HD,    foreign investment banks / institutional desks
                      TP, BQ, DP, AI, AG, FS, AH
  foreign_retail_base ZP, YU, YP, XA, KK         foreign-OWNED but with a large retail/wealth client base
                                                 (do NOT read them as "foreign institutions")
  retail_app          XL, XC, PD, GI, RO         mass-retail trading apps (Stockbit, Ajaib, IPOT, Webull, Pluang)
  scalper             MG, CP                     very high turnover, near-zero multi-day net; treat as noise
                                                 over windows longer than a day (untested hypothesis)
  local_other         everything else listed     local private houses (GR is LOCAL, not government)

Evidence status (research/broker_flow_history.sqlite, 18 weeks, ~270 stocks/week; see memory
project_broker_flow_weekly_backtest_xl_contrarian_2026_10_21): only XL has a measured, contrarian effect.
Institutional net (CC/AK/YU) showed none. Roles are a reading aid, not a validated signal by themselves.
"""
from __future__ import annotations

ROLE_OF = {
    **{c: "gov_inst" for c in ("CC", "OD", "NI", "DX")},
    **{c: "foreign_inst" for c in ("AK", "BK", "KZ", "RX", "DR", "HD", "TP", "BQ", "DP", "AI", "AG", "FS", "AH")},
    **{c: "foreign_retail_base" for c in ("ZP", "YU", "YP", "XA", "KK")},
    **{c: "retail_app" for c in ("XL", "XC", "PD", "GI", "RO")},
    **{c: "scalper" for c in ("MG", "CP")},
    **{c: "local_other" for c in (
        "DH", "SQ", "YB", "LG", "AZ", "BB", "GR", "KI", "IF", "AP", "EP", "YJ", "SS", "HP", "AO", "II", "BR", "RB",
        "RF", "CD", "IN", "AT", "FZ", "LS", "GA", "PO", "SH", "PC", "PG", "IU", "IH", "MU", "DU", "AR", "ES", "TS", "RS",
        "OK", "EL", "ZR", "ID", "PP", "SA", "SF", "MI", "QA", "RG", "PI", "DD", "JB", "BS", "AF", "TF", "PF", "IC", "IT",
        "AD", "YO", "BF", "FO")},
}

ROLE_LABEL_ID = {
    "gov_inst": "🏛️ Institusi pemerintah",
    "foreign_inst": "🏦 Asing institusional",
    "foreign_retail_base": "🌐 Asing (basis ritel/wealth besar)",
    "retail_app": "🛒 Aplikasi ritel massal",
    "scalper": "⚡ Scalper/trader cepat",
    "local_other": "🏢 Lokal swasta",
    "unknown": "❔ Tidak diketahui",
}

# Thresholds for the reading, as a fraction of the window's traded value (net buy of XL / market value).
# From the weekly backtest: XL net >= +1% of value -> -1.02%/week vs rest (t -3.9, 13/18 weeks);
# XL net <= -1% -> +0.56%/week (t +1.5, NOT significant); >= +5% -> -0.76% (t -2.3).
XL_CROWD_BUY = 0.01
XL_CROWD_BUY_STRONG = 0.05
XL_SELL = -0.01
RETAIL_APP_CODES = ("XL", "XC", "PD")


def role_of(code: str) -> str:
    return ROLE_OF.get((code or "").upper(), "unknown")


def role_label(code: str) -> str:
    return ROLE_LABEL_ID[role_of(code)]


def prompt_reference() -> str:
    """Compact classification text to embed in an LLM prompt that reads a broker-summary screenshot."""
    by_role: dict[str, list[str]] = {}
    for code, role in ROLE_OF.items():
        if role != "local_other":
            by_role.setdefault(role, []).append(code)
    lines = [f"- {role}: {', '.join(sorted(codes))}" for role, codes in by_role.items()]
    return "Broker role reference (ownership vs who actually trades):\n" + "\n".join(lines) + "\n- everything else: local_other"
