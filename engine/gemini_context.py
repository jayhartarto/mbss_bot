"""
engine/gemini_context.py — MBSS v2 (2026-10-22).

Structured, validated context for Gemini (/tanya and /testbrief): IHSG regime + which regime-gated lanes are armed, the lane x regime
scorecard cells for TODAY's regime, and every active swing pick with its tag (BELI-A / BELI-B / TAHAN / KELUAR / NETRAL), its reason, its
warnings and its cache-only corporate-action window; HINDARI picks are listed separately as hidden. Replaces the retired lenses
(consensus intersections, /hc high conviction, final-score "Strong Buy", manual portfolio) that /tanya and the morning brief used to feed.

Every block is best-effort and independent: a failure drops that block (named in `unavailable`), never the caller.
"""
from __future__ import annotations

import datetime

import engine.legacy_core as core

TAG_NAME = {"A": "BELI-A", "B": "BELI-B", "TAHAN": "TAHAN", "KELUAR": "KELUAR", "NETRAL": "NETRAL", "HINDARI": "HINDARI"}
_TAG_ORDER = {"A": 0, "B": 1, "KELUAR": 2, "TAHAN": 3, "NETRAL": 4, "HINDARI": 5}


def regime_block() -> dict:
    try:
        import engine.ihsg_regime as ihsg_regime
        r = ihsg_regime.get_ihsg_regime_today()
        if not r:
            return {"available": False}
        dd = r.get("dd_100")
        return {
            "available": True, "date": r.get("date"), "phase": r["regime_phase"],
            "ihsg_dd_from_100d_high_pct": None if dd is None else round(dd * 100, 1),
            "ihsg_ret_20d_pct": None if r.get("ret_20") is None else round(r["ret_20"], 1),   # already in percent units
            "osb_v2_armed": bool(dd is not None and dd <= -0.10),
            "macd_confirm_and_bos_armed": bool(r["bear_episode_id"] > 0 and r["regime_phase"] == "BOUNCE_OFF_LOW"),
        }
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": str(e)[:80]}


def scorecard_for_phase(phase: str | None) -> list[dict]:
    """The lane cells the tags are decided from, for today's regime phase only (win/mean in %, n, resulting level)."""
    import engine.lane_scorecard as lane_scorecard
    out = []
    if not phase:
        return out
    for lane, cells in lane_scorecard._table().items():
        c = cells.get(phase)
        if not c:
            continue
        lvl, _ = lane_scorecard.cell_level(lane, phase)
        out.append({"lane": lane, "n": c["n"], "win_pct": c["win"], "mean_pct": c["mean"], "ci95_mean_low": c.get("mean_lo"),
                    "level": {"A": "BELI-A", "B": "BELI-B", "NETRAL": "NETRAL", "NA": "bukti kurang (n<30)"}[lvl]})
    return out


def _active_picks() -> list[tuple[str, dict]]:
    import engine.buy_on_weakness as bow
    import engine.oversold_bounce_v2 as osb
    import engine.vcp_pillar as vcp
    import engine.macd_confirm_pillar as mc
    import engine.buy_on_strength as bos
    out = []
    for lane, mod, fn in (("BOW", bow, "load_buy_on_weakness_picks"), ("OSB-v2", osb, "load_oversold_bounce_v2_picks"),
                          ("VCP", vcp, "load_vcp_picks"), ("MACD-confirm", mc, "load_macd_confirm_picks"),
                          ("BOS", bos, "load_buy_on_strength_picks")):
        try:
            for p in getattr(mod, fn)() or []:
                if p.get("status") == "ALIVE":
                    out.append((lane, p))
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ gemini_context: pick {lane} gagal dibaca (diabaikan): {e}")
    return out


def _levels(p: dict) -> dict:
    keys = {"entry_ref_price": "entry", "entry_low": "entry_low", "entry_high": "entry_high", "sl_price": "sl", "tp_price": "tp",
            "tp1_price": "tp1", "tp2_price": "tp2", "age_days": "age_days"}
    return {v: p[k] for k, v in keys.items() if p.get(k) is not None}


def lane_tag_bundle(max_picks: int = 40) -> dict:
    """Regime + today's scorecard cells + every active pick with tag/reason/warnings/corporate-action window (cache only)."""
    import engine.lane_scorecard as lane_scorecard
    out: dict = {"as_of": datetime.datetime.now(core.WIB).strftime("%Y-%m-%d %H:%M WIB"), "unavailable": []}
    reg = regime_block()
    out["regime"] = reg
    try:
        out["scorecard_cells_today_regime"] = scorecard_for_phase(reg.get("phase"))
    except Exception as e:  # noqa: BLE001
        out["unavailable"].append(f"scorecard ({str(e)[:60]})")

    picks_out, hidden = [], []
    try:
        by_lane: dict = {}
        for lane, p in _active_picks():
            by_lane.setdefault(lane, []).append(p)
        for lane, plist in by_lane.items():
            kept, hid = lane_scorecard.partition(lane, plist)
            hidden += [{"ticker": t, "lane": ln, "reasons": r} for ln, t, r in hid]
            for p in kept:
                d = lane_scorecard.classify(lane, p)
                ev = lane_scorecard.evaluate(lane, p["ticker"], p.get("trigger_date"))
                ca = None
                try:
                    import engine.corporate_actions as corporate_actions
                    w = corporate_actions.window_lines(p["ticker"], allow_fetch=False, max_lines=1)
                    ca = w[0][2:].strip() if w else None
                except Exception:  # noqa: BLE001
                    pass
                picks_out.append({"ticker": p["ticker"], "lane": lane, "tag": TAG_NAME[d["tag"]], "tag_reason": d["reason"],
                                  "warnings": ev["warn"], "corporate_action_window": ca, "ff_priority": p.get("ff_priority"),
                                  **_levels(p), "_o": _TAG_ORDER[d["tag"]]})
        picks_out.sort(key=lambda x: (x["_o"], x["lane"], x["ticker"]))
        for x in picks_out:
            x.pop("_o", None)
        counts: dict = {}
        for x in picks_out:
            counts[x["tag"]] = counts.get(x["tag"], 0) + 1
        out["tag_counts"] = counts
        out["active_picks"] = picks_out[:max_picks]
        if len(picks_out) > max_picks:
            out["active_picks_truncated"] = f"{len(picks_out) - max_picks} pick lain tidak ditampilkan"
        out["hidden_hindari"] = hidden
    except Exception as e:  # noqa: BLE001
        out["unavailable"].append(f"picks ({str(e)[:60]})")
    return out
