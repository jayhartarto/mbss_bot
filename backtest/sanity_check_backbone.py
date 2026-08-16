"""
backtest/sanity_check_backbone.py — quick sanity check for engine/backbone.py
(MBSS v2, AB-RC1). Run ONCE on the server after /eodscan has produced a
fresh daily_scan_cache for today:

    python backtest/sanity_check_backbone.py

NOT a historical backtest — this runs the new Danger Gate / Probability
Rank against TONIGHT's real EOD cache and just prints the distribution +
Top-8 so obvious bugs (inverted scores, degenerate distribution, a
financial-distress ticker slipping through the gate) can be caught by eye
before wiring this into production commands. Genuine forward validation
starts once this is live and picks are tracked day over day.
"""
import sys

import engine.nightly as nightly_engine
import engine.market as market_engine
import engine.backbone as backbone_engine


def main():
    scored_dict, staleness_note = nightly_engine.load_daily_scan_cache_allow_stale()
    if not scored_dict:
        print("❌ Tidak ada daily_scan_cache sama sekali — jalankan /eodscan dulu.")
        sys.exit(1)
    if staleness_note:
        print(f"⚠️ {staleness_note}")
    results = list(scored_dict.values())
    print(f"📋 {len(results)} ticker di cache /eodscan.\n")

    market_context = market_engine.load_market_context()
    if not market_context:
        print("⚠️ Tidak ada market context hari ini (breadth/regime) — pakai R0_UNKNOWN sebagai fallback.")
        market_regime = "R0_UNKNOWN"
    else:
        market_regime = market_context.get("regime", "R0_UNKNOWN")
    print(f"📊 Market regime: {market_regime}\n")

    result = backbone_engine.compute_backbone(results, market_regime)

    danger_values = sorted(v["predicted_danger"] for v in result["all_scored"].values())
    n = len(danger_values)
    print("=== Distribusi predicted_danger (0-100, makin tinggi makin bahaya) ===")
    print(f"min={danger_values[0]:.1f}  p25={danger_values[n//4]:.1f}  "
          f"median={danger_values[n//2]:.1f}  p75={danger_values[3*n//4]:.1f}  max={danger_values[-1]:.1f}")
    print(f"Gate quantile: {result['danger_gate_quantile']}  ->  cutoff value: {result['danger_cutoff_value']:.1f}")
    passed = sum(1 for v in result["all_scored"].values() if v["passed_danger_gate"])
    print(f"Lolos gate: {passed}/{n} ({passed/n*100:.0f}%)\n")

    print(f"=== TOP-{len(result['top8'])} (backbone_rank, ticker, danger, probability) ===")
    distress_leak = []
    for r in result["top8"]:
        distress = r.get("is_financial_distress_flag")
        floor = r.get("is_near_price_floor")
        flag_str = " ⚠️ DISTRESS" if distress else (" ⚠️ NEAR_FLOOR" if floor else "")
        print(
            f"{r['backbone_rank']}. {r['ticker']:6s} danger={r['predicted_danger']:5.1f} "
            f"prob={r['probability_score']:5.1f}  RSI={r.get('rsi', '-')}  ADX={r.get('adx', '-')}  "
            f"RR={ (r.get('targets') or {}).get('risk_reward_at_max', '-') }{flag_str}"
        )
        if distress:
            distress_leak.append(r["ticker"])

    if distress_leak:
        print(f"\n❌ BUG CANDIDATE: ticker distress finansial lolos ke Top-8: {distress_leak} — Danger Gate seharusnya menolak ini.")
    else:
        print("\n✅ Tidak ada ticker distress finansial di Top-8.")

    if len(set(round(v["predicted_danger"], 0) for v in result["all_scored"].values())) <= 2:
        print("❌ BUG CANDIDATE: predicted_danger nyaris konstan di seluruh universe — cek apakah input fields kosong/None semua.")


if __name__ == "__main__":
    main()
