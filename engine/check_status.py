"""
engine/check_status.py — MBSS v2 (2026-10-22).

Lane / regime / risk-tag block for /check, deliberately terse (one line per fact). Replaces the retired daytrade-era blocks
(MACD lane labels, backbone Entry Rank, tactical rank) with what the current build actually trades on:
  * is this ticker an ACTIVE pick in a swing lane (BOW, OSB-v2, VCP, MACD-confirm, BOS)? -> that lane's entry/SL/TP, read from
    the same pick files /swing reads (cache only, no live scan);
  * IHSG regime phase and whether the regime-gated lanes are armed;
  * display-only warnings: XL net-buy crowding, weird-stock OHLCV flags (tag-only per the 2026-10-20 backtest), thin liquidity.
Every function is best-effort: a failure returns "" / [] and never breaks /check (missing = neutral).
"""
from __future__ import annotations

import engine.legacy_core as core

# Weird-stock flag thresholds: research/weird_stock_flag_backtest_2026_10_20.py (20-day rolling window ending on the signal day).
ZERO_VOL_DAYS = 1
GAP_DAYS = 2          # open gap >= 5%
ARA_ARB_DAYS = 2
THIN_VALUE_BN = 1.0   # Rp bn/day, 20d mean; exit feasibility only (no tail penalty found)


def _picks(loader_module, loader_name: str) -> list:
    try:
        return getattr(loader_module, loader_name)() or []
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ check_status: gagal baca pick {loader_name}: {e}")
        return []


def lane_status(ticker: str) -> list[str]:
    """One line per swing lane where `ticker` has an ALIVE pick. Empty list when none."""
    import engine.buy_on_weakness as bow
    import engine.oversold_bounce_v2 as osb
    import engine.vcp_pillar as vcp
    import engine.macd_confirm_pillar as mc
    import engine.buy_on_strength as bos

    t = ticker.upper()
    out: list[str] = []

    for p in (x for x in _picks(bow, "load_buy_on_weakness_picks") if x.get("ticker") == t and x.get("status") == "ALIVE"):
        hidden = " (hidden di /swing)" if bow.should_hide_from_rank(p["tier"], p.get("drift_zone", "NETRAL")) else ""
        out.append(f"🪶 BOW T{p['tier']} Day {p['age_days']}/5{hidden} | Entry {p['entry_low']:,.0f}-{p['entry_high']:,.0f} | "
                   f"SL {p['sl_price']:,.0f} | TP {p['tp1_price']:,.0f}")

    for p in (x for x in _picks(osb, "load_oversold_bounce_v2_picks") if x.get("ticker") == t and x.get("status") == "ALIVE"):
        tier1 = " 🏆" if p.get("tier") == "TIER1" else ""
        out.append(f"🔄 OSB-v2{tier1} Day {p['age_days']}/{osb.ALERT_MAX_AGE_DAYS} | Entry {p['entry_ref_price']:,.0f} | "
                   f"SL {p['sl_price']:,.0f} | TP {p['tp1_price']:,.0f} / {p['tp2_price']:,.0f}")

    for p in (x for x in _picks(vcp, "load_vcp_picks") if x.get("ticker") == t and x.get("status") == "ALIVE"):
        hidden = " (hidden di /swing)" if vcp.should_hide_vcp_diskon(p) else ""
        out.append(f"📐 VCP Day {p['age_days']}/{vcp.ALERT_MAX_AGE_DAYS}{hidden} | Entry {p['entry_ref_price']:,.0f} | "
                   f"SL {p['sl_price']:,.0f} | TP {p['tp1_price']:,.0f} / {p['tp2_price']:,.0f}")

    for p in (x for x in _picks(mc, "load_macd_confirm_picks") if x.get("ticker") == t and x.get("status") == "ALIVE"):
        out.append(f"🌀 MACD-confirm Day {p.get('age_days', 1)}/{mc.ALERT_MAX_AGE_DAYS} | Entry {p['entry_ref_price']:,.0f} | "
                   f"SL {p['sl_price']:,.0f} | TP {p['tp_price']:,.0f}")

    for p in (x for x in _picks(bos, "load_buy_on_strength_picks") if x.get("ticker") == t and x.get("status") == "ALIVE"):
        sl_pct = p.get("sl_pct", bos.SL_PCT_NORMAL)
        sl_price = p.get("sl_price") or round(p["entry_ref_price"] * (1 + sl_pct / 100))
        out.append(f"💪 BOS Day {p['age_days']} | Entry {p['entry_ref_price']:,.0f} | SL {sl_price:,.0f} | "
                   f"TP {round(p['entry_ref_price'] * (1 + bos.TP_PCT / 100)):,.0f}")
    return out


def regime_line() -> str:
    """One line: IHSG regime phase + which regime-gated swing lanes are armed. '' when the regime can't be computed."""
    try:
        import engine.ihsg_regime as ihsg_regime
        r = ihsg_regime.get_ihsg_regime_today()
        if not r:
            return ""
        phase = r["regime_phase"]
        dd = r.get("dd_100")
        dd_txt = f" ({dd * 100:+.1f}%)" if dd is not None else ""
        osb_on = dd is not None and dd <= -0.10                        # OSB-v2 gate: dd_100 <= -10%
        bounce = r["bear_episode_id"] > 0 and phase == "BOUNCE_OFF_LOW"  # MACD-confirm / BOS gate
        return f"🌐 IHSG {phase}{dd_txt} | OSB-v2 {'ON' if osb_on else 'OFF'} | MACD-confirm/BOS {'ON' if bounce else 'OFF'}"
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ check_status: regime gagal (diabaikan): {e}")
        return ""


def risk_tags(ticker: str) -> list[str]:
    """Display-only warnings, one short line each. Never hides anything, never changes a score."""
    tags: list[str] = []
    t = ticker.upper()

    try:
        import engine.broker_flags as broker_flags
        flag = broker_flags.xl_flags([t]).get(t)
        if flag:
            tags.append(f"⚠️ XL ramai beli {flag['pct'] * 100:+.1f}% ({flag['as_of'][5:]}), sinyal kontrarian")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ check_status: xl flag gagal (diabaikan): {e}")

    try:
        import numpy as np
        df = core.get_ohlcv_daily_from_db(t, 60)
        if df is not None and len(df) >= 21:
            g = df.tail(21)
            c, o, v = g["Close"], g["Open"], g["Volume"]
            pc = c.shift(1)
            ret = (c / pc - 1).iloc[1:]
            gap = (o / pc - 1).abs().iloc[1:]
            pcp = pc.iloc[1:]
            lim = np.where(pcp <= 200, 0.35, np.where(pcp <= 5000, 0.25, 0.20))
            arb_lim = np.where(g.index[1:] >= np.datetime64("2025-04-08"), np.minimum(lim, 0.15), lim)
            ara = int(((ret >= lim * 0.92) | (ret <= -arb_lim * 0.92)).sum())
            zv = int((v.iloc[1:] == 0).sum())
            gp = int((gap >= 0.05).sum())
            val_bn = float((c.iloc[1:] * v.iloc[1:]).mean() / 1e9)
            parts = []
            if zv >= ZERO_VOL_DAYS:
                parts.append(f"{zv}x volume nol")
            if gp >= GAP_DAYS:
                parts.append(f"{gp}x gap buka ≥5%")
            if ara >= ARA_ARB_DAYS:
                parts.append(f"{ara}x ARA/ARB")
            if parts:
                tags.append("⚠️ 20 hari: " + ", ".join(parts) + " (risiko gap)")
            thin = " ⚠️ tipis" if val_bn < THIN_VALUE_BN else ""
            tags.append(f"💧 Likuiditas Rp{val_bn:,.1f} M/hari{thin}")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ check_status: flag OHLCV gagal (diabaikan): {e}")
    return tags


def pick_lanes(ticker: str, include_recent_resolved: bool = True) -> list[tuple[str, dict]]:
    """[(lane, pick)] for every lane where `ticker` has an ALIVE pick, plus (optionally) picks resolved within the last few days so
    /check can say KELUAR. Lane keys are the ones engine.lane_scorecard uses."""
    import datetime
    import engine.buy_on_weakness as bow
    import engine.oversold_bounce_v2 as osb
    import engine.vcp_pillar as vcp
    import engine.macd_confirm_pillar as mc
    import engine.buy_on_strength as bos
    import engine.lane_scorecard as lane_scorecard

    t = ticker.upper()
    out: list[tuple[str, dict]] = []
    for lane, mod, fn in (("BOW", bow, "load_buy_on_weakness_picks"), ("OSB-v2", osb, "load_oversold_bounce_v2_picks"),
                          ("VCP", vcp, "load_vcp_picks"), ("MACD-confirm", mc, "load_macd_confirm_picks"),
                          ("BOS", bos, "load_buy_on_strength_picks")):
        for p in _picks(mod, fn):
            if p.get("ticker") != t:
                continue
            if p.get("status") == "ALIVE":
                out.append((lane, p))
            elif include_recent_resolved and p.get("status") not in (None, "LEGACY_CLOSED") and p.get("resolved_date"):
                try:
                    days = (datetime.date.today() - datetime.date.fromisoformat(str(p["resolved_date"])[:10])).days
                except Exception:  # noqa: BLE001
                    continue
                if days <= lane_scorecard.EXIT_RECENT_DAYS:
                    out.append((lane, p))
    return out


_TAG_PRIORITY = {"KELUAR": 0, "HINDARI": 1, "TAHAN": 2, "A": 3, "B": 4, "NETRAL": 5}


def tag_lines(ticker: str) -> tuple[list[str], list[str]]:
    """(tag lines, warning reasons). Tag lines are the new BELI-A / BELI-B / TAHAN / KELUAR / NETRAL / HINDARI verdicts (most urgent
    first); warnings are the lane-specific cautions (EXT, spike). Best-effort: a failure returns a plain NETRAL line."""
    try:
        import engine.lane_scorecard as lane_scorecard
        picks = pick_lanes(ticker)
        if not picks:
            d = lane_scorecard.classify_lookup(ticker)
            ev = lane_scorecard.evaluate(None, ticker.upper(), None)
            return [lane_scorecard.tag_text(d)], list(ev["warn"])
        decided = sorted(((lane_scorecard.classify(lane, p), lane, p) for lane, p in picks), key=lambda x: _TAG_PRIORITY[x[0]["tag"]])
        warn: list[str] = []
        for lane, p in picks:
            if p.get("status") == "ALIVE":
                warn += [w for w in lane_scorecard.evaluate(lane, ticker.upper(), p.get("trigger_date"))["warn"] if w not in warn]
        return [lane_scorecard.tag_text(d) for d, _, _ in decided[:3]], warn
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ check_status: tag gagal (diabaikan): {e}")
        return ["⚪ NETRAL — tag tidak bisa dihitung"], []


def corporate_action_lines(ticker: str) -> list[str]:
    """Ex-dividend / rights issue / split inside the next ~25 days (one live IPOT fetch for this ticker, 12h cache)."""
    try:
        import engine.corporate_actions as corporate_actions
        return corporate_actions.window_lines(ticker, allow_fetch=True)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ check_status: corporate action gagal (diabaikan): {e}")
        return []


def build_status_block(ticker: str) -> str:
    """The block shown at the top of /check message 1: tag verdict, lane lines, one regime line, warnings, corporate-action windows,
    one line per risk tag."""
    tags, warn = tag_lines(ticker)
    lines = list(tags)
    lines += lane_status(ticker)
    reg = regime_line()
    if reg:
        lines.append(reg)
    lines += [f"⚠️ {w}" for w in warn]
    lines += corporate_action_lines(ticker)
    lines += risk_tags(ticker)
    return chr(10).join(lines)
