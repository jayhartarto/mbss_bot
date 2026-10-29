# bull5 research session 2026-10-28 — summary, decisions, next steps

Scope: find >5% / >10% (<10 day) entry formulas, confirmation, exits, IHSG gate, crash-resilient "tangguh" stocks, broker booster, gate on other lanes.
All scripts in this folder are `*_2026_10_28.py`. Data caches (`*.pkl`, `glob/*.csv` from yfinance up to 2026-10-22) are regenerable — do NOT commit them (folder is ~414 MB).

## Findings (net of 0.4% cost unless noted; train Jan23–Jun25, test Jul25–Oct26)
1. **Formulas.** S1/S2 (momentum): slope20 >= 7.1% and ATR14 <= 5.8% (+ atrratio <= .97 & cpos >= .25 for S1, or body >= .7% for S2). A/B (pullback): A = ret3 >= 5.7 & gap <= -.93 & cpos <= .94; B = ret10 >= 12 & d5 >= -1.15 & d50 <= 4.58 & rng <= 3.85. Confirmation = same ticker alerted again within 3 days -> test EV +2.0% (win 59%). Persistence (>= 3 alerts / 10d) +3.4%.
2. **Exit.** TP20 / SL8 / trailing (activate +8%, width 6%) / 9 days. Tighter or earlier trailing hurts in every regime; "no TP" is outlier-driven (RMKE, SSIA episodes).
3. **IHSG gate.** Hybrid = (IHSG >= SMA50 - 1.1%) OR (IHSG >= +4% above its 10d low AND >= SMA5). Beats the breadth gate; shorter (3–20d) gates add nothing. Open ~64% of days.
4. **8-slot portfolio**, confirm3 + hybrid gate: +244% / MDD -5.6% (full), test +87%. Booster size tilt 1.5x (stock UP on >= 2 IHSG crash days in last 20d): +272%. Booster as priority = no change; as filter = hurts. Aug–Oct 2026: about -1.8%, flat equity through the 10-16 crash because the gate was closed.
5. **Tangguh.** Structural resilience (beta, hi250, downside capture) is NOT a booster. "Closed UP on IHSG crash days (<= -1.5%) in the last 20 sessions" IS (+4.6/+3.6% vs +2.3% for >= 1 day; +6.5% for >= 2). Being up on the crash day is NOT a buy signal by itself (fwd 5d negative).
6. **Broker.** PEMBELI BARU tag (nb_ref >= .057 & liq >= Rp0.3bn): +3.75 / +4.88% vs +2.47 / +2.62% untagged; pullback A/B strongest (+8.4 / +5.5%); ~0.2 tagged picks/day. Avoid flags: scalper (MG, CP) net5 >= +3% of volume; retail_app (XL, XC, PD) net5 >= +5%.
7. **Gate on other lanes** (panel replay 2024-09..2026-10, lane-native exits): BSJP1 kept +0.22 vs skipped -0.52 (test only); MACD-confirm +5.2 vs +1.3 (test only); BOW +1.9 vs +0.4; VCP +0.2 vs -0.9; OSB-v2 is crash-native (gate cuts 62% of picks). **CAUTION:** in Jul–Oct 2026 the gate showed NO benefit (OSB-v2 dropped +5.9% vs kept +2.0%; BOW kept -0.04% vs dropped +1.07%).
8. **OSB-v2.** Hard hybrid gate = 0.36 picks/day on average but only 7% of days have any pick (max 66 in one day) and 65% come from one rebound leg (Apr–Jun 2026). Top-3 per day lowers full-history win 75% -> 68%. SL -20% is too loose in Sep–Oct (-1.36% mean, worst -25.8%); -8% + break-even after +5% gives +1.06% (win 68%, worst -8%); -6% + BE has best capital efficiency. BOW SL -18% is rarely hit; -12% only trims the tail.

## Decisions by the user (2026-10-28)
- APPROVED: tag booster (PEMBELI BARU) placed higher in /rank and /swing, especially pullback; HIDE picks with avoid flags (scalper / XL crowd).
- APPROVED: implement hybrid IHSG gate for BSJP Lane1, MACD-confirm, BOW, VCP.
- OSB-v2: user wanted a hard gate if n/day is enough -> it is NOT (see 8). Awaiting choice: A (hard gate) vs B (tiered Top-3/5 + risk label when gate closed; recommended Top-5).
- SL tightening (OSB-v2 -> -8% + BE@+5%, BOW -> -12%): proposed, awaiting explicit approval.

## NEXT WORK (in order) — nothing in engine/ has been changed yet
A. Answer open decisions: OSB-v2 plan A vs B (+ Top-N), adopt SL changes or not.
B. Implement:
   1. `engine/ihsg_gate.py` — shared hybrid gate (fresh ^JKSE, cached per day, fail-OPEN on data error with a log line, returns state + reason). Wire into `compute_buy_on_weakness_candidates`, `compute_vcp_candidates`, `compute_macd_confirm_candidates`, BSJP lane1. Show "gate IHSG tertutup" notice in /swing and BSJP output. Keep the Jul–Oct 2026 caveat in mind: log dropped picks' outcomes so the gate can be audited after 4–6 weeks.
   2. `engine/newbuyer_tag.py` — extend to BOW (production evidence earlier said "unclear": first re-run a lane_newbuyer-style replay on production BOW, because the bull5 evidence is for A/B signals only). Ordering: tagged picks first in /swing and /rank (`capital_rank.NEWBUYER_RANK_BONUS`, pullback lanes first). Do not touch OSB-v2 (opposite sign in earlier research).
   3. Hide flags: new function (reuse newbuyer_tag's net_lo convention) for 5d net/volume of scalper (MG, CP) and retail_app (XL, XC, PD); hide when scalper >= +3% or retail_app >= +5%; fail-open when IPOT data is missing; print "N pick disembunyikan (crowd)" in the footer.
   4. OSB-v2: tier / Top-N display, risk label when gate closed, SL/BE change if approved; BOW SL if approved.
   5. Tests: synthetic unit tests for gate + flags; replay check that kept/dropped counts match research numbers; dry-run nightly pipeline; IPOT top-up via /ffupdate must be fresh (VPS DB).
C. Research follow-ups (not blocking):
   - Portfolio sim with new-buyer size tilt and crash-day booster together.
   - OSB-v2 / BOW open positions marked at the 2026-10-19 low (true crash exposure).
   - Verify the gate on the production-engine replay (`research/ipot_explore/lane_newbuyer.py`) instead of the older panel.
   - Frequency check: confirm3 gives ~0.35 new entries/day vs the goal of >= 1/day or 2 per 5 days.
   - Refresh yfinance JKSE nightly (`research/bull5/glob` is static to 10-22); forward-test 4–6 weeks.
   - Watchlist of names that held up on >= 3 crash days: PKPK, GPSO, RSCH, ITMG, PGAS.
D. Housekeeping: do not mix the commit with the user's uncommitted broker work (`engine/broker.py`, `broker_roles.py`, `broksum_read.py`, `research/ipot_backfill.py`); add `research/bull5/*.pkl` and `research/bull5/glob/` to `.gitignore`; MEMORY.md index is over its size limit — compress.
