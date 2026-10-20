"""
engine/winrate_monitor.py — MBSS v2 (2026-10-11).

Lightweight REALIZED-vs-ASSUMED win-rate drift check, run as part of
/eodscan's nightly cycle (NOT a full backtest re-validation like the
laptop-side research scripts) -- this reads only the already-existing
picks-history JSON files, no bulk OHLCV/CSV needed, so it's safe to run
on the VPS with nothing extra to deploy.

Why this exists: BOW's live LANE_STATS and MACD-confirm's WIN_RATE_TABLE
were BOTH found stale this session purely because nobody was checking
(see memory project_bow_rank_scoring_redesign_2026_10_10.md for the full
trail -- BOW's bug was a 5-day tracking cutoff, MACD-confirm's was 3
weeks of regime drift since its one-time 2026-09-23 validation). A full
CSV-based backtest re-run is possible but heavy and laptop-only (the
research CSV never leaves the laptop, confirmed 2026-10-11 -- not
tracked in git). This is the cheap, VPS-native alternative: compare
REALIZED win-rate from each lane's own resolved pick history against
whatever win-rate the system currently ASSUMES for that population, and
ALERT (never auto-correct) if the gap is large enough and the sample is
big enough to trust. A human (or Claude, when asked) reviews and decides
whether to actually update LANE_STATS/WIN_RATE_TABLE -- same principle
as every other number-correction this session, just automated for
noticing, not for acting.

Threshold choices (deliberately conservative -- tune later if this
proves too noisy or too quiet in practice):
  MIN_N = 20 resolved picks in the lookback window (below this, a gap
    could easily be sampling noise, not real drift).
  GAP_THRESHOLD_PP = 12.0 percentage points (BOW's real bug produced a
    ~25-30pt gap, MACD-confirm's regime drift produced 5-12pt on its
    worst cell -- 12pt sits just above "normal" cell-to-cell research
    noise seen throughout this session, so it should catch another
    MACD-confirm-sized drift without firing on every minor wobble).
  LOOKBACK_DAYS = 45 (resolved_date within this many days of today --
    long enough to accumulate a usable n for lanes that don't fire daily,
    short enough to reflect CURRENT conditions, not a year-old average).
"""
from __future__ import annotations

import datetime

MIN_N = 20
GAP_THRESHOLD_PP = 12.0
LOOKBACK_DAYS = 45


def _resolved_recent(picks: list, lookback_days: int = LOOKBACK_DAYS) -> list:
    cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
    out = []
    for p in picks:
        if p.get("resolved_ret_pct") is None:
            continue
        rd = p.get("resolved_date")
        if not rd:
            continue
        try:
            rd_date = datetime.date.fromisoformat(str(rd)[:10])
        except ValueError:
            continue
        if rd_date >= cutoff:
            out.append(p)
    return out


def _realized_win_pct(resolved: list) -> float:
    wins = sum(1 for p in resolved if p["resolved_ret_pct"] > 0)
    return 100.0 * wins / len(resolved)


def check_bow() -> list[str]:
    """Tier-specific (BOW now has tier-aware LANE_STATS, see
    engine/capital_rank.py) -- checked per tier since a drift in one
    tier shouldn't be masked by the other two averaging it out."""
    import engine.buy_on_weakness as bow_engine
    import engine.capital_rank as cr

    resolved = _resolved_recent(bow_engine.load_buy_on_weakness_picks())
    by_tier: dict[int, list] = {1: [], 2: [], 3: []}
    for p in resolved:
        t = p.get("tier")
        if t in by_tier:
            by_tier[t].append(p)

    warnings = []
    for tier, sub in by_tier.items():
        if len(sub) < MIN_N:
            continue
        realized = _realized_win_pct(sub)
        assumed = cr.LANE_STATS[("BOW", tier, False)]["win"]
        gap = assumed - realized
        if gap >= GAP_THRESHOLD_PP:
            warnings.append(
                f"⚠️ BOW Tier{tier}: realized win {realized:.0f}% (n={len(sub)}, {LOOKBACK_DAYS}d) "
                f"vs asumsi {assumed:.0f}% — selisih {gap:.0f}pt, pertimbangkan review."
            )
    return warnings


def check_vcp() -> list[str]:
    import engine.vcp_pillar as vcp_engine
    import engine.capital_rank as cr

    resolved = _resolved_recent(vcp_engine.load_vcp_picks())
    if len(resolved) < MIN_N:
        return []
    realized = _realized_win_pct(resolved)
    assumed = cr.LANE_STATS[("VCP", None)]["win"]
    gap = assumed - realized
    if gap < GAP_THRESHOLD_PP:
        return []
    return [
        f"⚠️ VCP: realized win {realized:.0f}% (n={len(resolved)}, {LOOKBACK_DAYS}d) vs asumsi "
        f"{assumed:.0f}% — selisih {gap:.0f}pt. Catatan: TP-vs-SL VCP asymmetric (lihat docstring "
        "modul) — win% saja bisa menyesatkan, cek mean_ret juga sebelum ambil kesimpulan."
    ]


def check_macd() -> list[str]:
    """Expected win% = WIN_RATE_FLAT, the single backtested win-rate for
    the 2026-10-14 regime-gated fixed-exit redesign (BOUNCE_OFF_LOW gate,
    SL-6%/TP8%/maxhold5 -- see engine/macd_confirm_pillar.py module
    docstring). No more per-(day,lane,tag) cells to average."""
    import engine.macd_confirm_pillar as macd_engine

    # Only picks from the redesigned fixed-exit lane (have tp_price); pre-2026-10-14 tag-system
    # picks resolved under different exit rules and would read as a false 7% win-rate alarm.
    resolved = _resolved_recent([p for p in macd_engine.load_macd_confirm_picks() if "tp_price" in p])
    if len(resolved) < MIN_N:
        return []
    realized = _realized_win_pct(resolved)
    expected = macd_engine.WIN_RATE_FLAT
    gap = expected - realized
    if gap < GAP_THRESHOLD_PP:
        return []
    return [
        f"⚠️ MACD-confirm: realized win {realized:.0f}% (n={len(resolved)}, {LOOKBACK_DAYS}d) vs "
        f"ekspektasi {expected:.0f}% — selisih {gap:.0f}pt, pertimbangkan review desain exit."
    ]


def check_osb() -> list[str]:
    import engine.oversold_bounce_v2 as osb_engine
    import engine.capital_rank as cr

    resolved = _resolved_recent(osb_engine.load_oversold_bounce_v2_picks())
    warnings = []
    for ff in (True, False):
        sub = [p for p in resolved if bool(p.get("ff_priority")) == ff]
        if len(sub) < MIN_N:
            continue
        realized = _realized_win_pct(sub)
        assumed = cr.LANE_STATS[("OSB", ff)]["win"]
        gap = assumed - realized
        if gap >= GAP_THRESHOLD_PP:
            warnings.append(
                f"⚠️ OSB-v2{' +FF' if ff else ''}: realized win {realized:.0f}% (n={len(sub)}, "
                f"{LOOKBACK_DAYS}d) vs asumsi {assumed:.0f}% — selisih {gap:.0f}pt. Catatan: OSB-v2 "
                "episode cluster di sedikit periode drawdown (lihat memory) — n besar belum tentu "
                "independen, baca sebagai sinyal awal bukan kepastian."
            )
    return warnings


def check_all_lanes() -> list[str]:
    """Returns a flat list of warning strings (empty if nothing to
    flag). Each check is individually try/except-guarded so one lane's
    data quirk (e.g. a missing tier field) never blocks the others."""
    warnings: list[str] = []
    for fn in (check_bow, check_vcp, check_macd, check_osb):
        try:
            warnings.extend(fn())
        except Exception as e:
            print(f"⚠️ winrate_monitor: gagal cek {fn.__name__}: {e}")
    return warnings


def build_alert_message(warnings: list[str]) -> str:
    lines = ["📉 WIN-RATE DRIFT CHECK — selisih realized vs asumsi cukup besar:\n"]
    lines.extend(warnings)
    lines.append(
        "\nℹ️ Ini cek ringan dari history pick yang sudah resolve (bukan backtest ulang dari OHLCV "
        "besar). Alert saja — TIDAK ada angka yang otomatis diubah, review manual dulu sebelum "
        "update LANE_STATS/WIN_RATE_TABLE."
    )
    return "\n".join(lines)
