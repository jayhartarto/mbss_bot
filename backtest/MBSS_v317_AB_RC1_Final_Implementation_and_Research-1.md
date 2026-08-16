# MBSS v3.17 Development Specification

## Backbone Top-8, Consensus Brief, Explosive Lane, and RapidAPI Enrichment

**Status:** Development-ready specification  
**Baseline:** MBSS scoring formula v3.17.0  
**Primary universe:** seluruh saham syariah eligible setelah whitelist dan liquidity eligibility  
**Primary validation window:** 1 June–12 August 2026  
**Historical RapidAPI policy:** no backfill; forward enrichment only

---

## 1. Objective

Implement a shared EOD backbone that ranks the broad Sharia universe before results are delivered into the two primary scanners:

1. `/screendaytrade`
2. `/hc`

`/consensus` becomes the concise user-facing brief. It summarizes the highest-quality overlap between both scanners, adds a controlled Explosive Lane, and appends RapidAPI smart-money and multibagger context when available.

`/gptpick` remains available, but it is downstream from the same backbone and is not the primary user brief.

`/executiongate` is not part of the new workflow. `/check TICKER` is the live confirmation tool.

---

## 2. Target Architecture

```text
Nightly EOD cache, broad Sharia universe
        ↓
Feature Engine v3.17
RSI, ADX, MACD, CMF, OBV, Bollinger Bands,
V5 Breakout, Continuation, Activity, VolQ,
Room, Safety, RR, volatility, relative strength
        ↓
Global Market Regime, one regime per date
R1 / R2 / R3 / R4 / R5
        ↓
BACKBONE
Danger Gate → Probability Rank → Top-8 Moderate
        ↓
Primary scanners
/screendaytrade          /hc
        ↓                   ↓
        └────── /consensus brief ──────┐
                                       ├─ Consensus Prime
                                       ├─ Explosive Lane, 1–3 names
                                       ├─ Smart-Money Overlay
                                       └─ Long-Horizon Multibagger Info
        ↓
/check TICKER for live confirmation
```

---

## 3. Backbone Definition

### 3.1 Danger Gate

The strongest validated function of the research model is dangerous-loss control. The gate must be evaluated before any primary scanner ranks its candidates.

Inputs should include:

- `day_range_pct_10d`
- `risk_reward_at_max`
- V5 Safety / `risk_score`
- RSI
- Activity
- Breakout extension
- Room
- volume ratio
- market regime
- bearish OBV divergence
- MACD bearish cross
- below SMA50 / EMA21
- near-price-floor flag

The initial production cutoff should implement the **moderate gate frozen from discovery data**, not a threshold re-tuned daily. Store both:

```text
predicted_danger
passed_danger_gate
```

If the statistical model is not shipped in production, first implement an auditable deterministic approximation from the same factors and retain the fields above for later calibration.

### 3.2 Probability Ranking

After candidates pass the Danger Gate, rank using:

- Continuation quality
- CMF / money-flow direction
- ADX trend reliability
- relative strength versus IHSG
- MACD state and freshness
- Bollinger context
- Safety
- VolQ
- Room
- RR
- continuous volatility position, including P90 context
- market-regime compatibility

P90 is part of the backbone, not merely a display badge. Do not implement it as a universal hard AND-filter because that makes output too sparse and was not stable across all periods.

### 3.3 Output Size

```text
Target: Top 8
Acceptable normal range: 5–8
Never fill with bad candidates just to reach quota
```

When fewer than five candidates pass the gate, return fewer than five and state that market quality is limited.

---

## 4. Regime Policy

The regime is **market-wide**, normally based on IHSG/breadth context, and applies equally to all tickers for that signal date. Individual ticker behaviour remains represented by its own technical features.

Suggested posture:

- **R1 Bull Stable:** more offensive; preserve breakout, relative-strength and upside winners.
- **R2 Bull High Vol:** highly selective until sufficient forward sample exists.
- **R3 Sideways:** use the validated moderate gate; this was the strongest near-current regime.
- **R4 Risk Off:** lower confidence and reduced output, but not automatic no-trade.
- **R5 Stress:** strict risk control; prefer `/screendaytrade` opportunities over HC-only names when evidence conflicts.
- **R0/insufficient:** no high-confidence recommendation. Show data-quality warning.

---

## 5. Primary Scanner Behaviour

### 5.1 `/screendaytrade`

- Receives the backbone candidate pool.
- Retains its Fresh Breakout, Continuation and Accumulation lane logic.
- Main source of large-upside opportunities.
- Must not fetch RapidAPI data live for the full universe.

### 5.2 `/hc`

- Receives the same backbone candidate pool.
- Applies High Conviction structural checks.
- Primarily acts as quality/structure confirmation.

### 5.3 `/gptpick`

- May re-rank the backbone pool using its existing formula.
- Must not independently re-open the entire universe.
- Preserve source tracking for later evaluation.

---

## 6. `/consensus` User Brief

### 6.1 Consensus Prime

Definition:

```text
passed Backbone Top-8
AND selected by /screendaytrade
AND selected by /hc
on the same signal date
```

Display all qualifying names, usually 0–2. Do not weaken the definition to force output.

Suggested label:

```text
🏆 CONSENSUS PRIME
```

Near-current validation result:

```text
n = 19
win rate = 68.42%
average return = +1.13%
median return = +1.67%
dangerous loss <= -10% = 0%
worst loss = -9.01%
```

### 6.2 Explosive Lane

Add **1–3 names**, potentially from outside final `/screendaytrade` and `/hc` output, provided they are still from the Sharia eligible universe and pass a looser but meaningful safety gate.

Primary factors:

1. Room high
2. RR high
3. Controlled but meaningful volatility
4. Activity strong
5. Not heavily extended or chased

Breakout and Continuation are confirmations, not dominant explosive predictors.

Hard-reject examples:

- near-price-floor risk
- very poor RR
- strong bearish OBV divergence
- bearish MACD cross combined with below SMA50
- extreme volatility beyond allowed tail
- heavy chase/extension
- missing/insufficient market regime

Soft penalties only:

- moderately high RSI
- above-median volatility
- isolated volume spike
- not selected by HC
- Safety below Probability Core but not structurally dangerous

Suggested label:

```text
🚀 EXPLOSIVE LANE
```

### 6.3 Smart-Money Overlay

Do not backfill RapidAPI. The nightly job sweeps the configured broker whitelist, captures their top net-buy activity, and intersects it with scanner candidates.

Required behaviour:

```text
RapidAPI unavailable → bonus 0, penalty 0
```

Suggested tiers:

- Consensus Prime + smart-money net buy → `💎 TRIPLE CONFIRMATION`
- Explosive candidate + smart-money net buy → `🚀 EXPLOSIVE + SMART MONEY`
- Strong smart-money accumulation without scanner confirmation → `💰 SMART-MONEY WATCH`, informational only
- Strong technical setup + whitelist net sell → `⚠️ SMART-MONEY DIVERGENCE`

Bonus inputs:

- number of whitelist brokers net buying
- aggregate whitelist net-buy percentage/value
- concentration
- persistence across available snapshots
- proximity to broker average entry
- whether price is still unextended

All bonus values must be bounded so broker data cannot rescue a fundamentally poor or danger-rejected ticker.

### 6.4 Long-Horizon Multibagger

RapidAPI multibagger results are displayed separately because the horizon differs from the 1–5 day scanner outcome.

Suggested label:

```text
🔭 LONG-HORIZON WATCH
```

Show at most 1–2 names with:

- multibagger score
- potential-return label
- horizon
- whether also present in Backbone, SDT, HC or Consensus

Do not materially blend this score into the short-horizon decision.

---

## 7. Suggested `/consensus` Message Format

```text
📊 MARKET REGIME
R3 SIDEWAYS
Posture: selective; prioritize risk control and healthy room.

🏆 CONSENSUS PRIME
1. ABCD
   Backbone: 82
   SDT: PRIORITY FRESH
   HC: 6/7
   Smart Money: ZP, BK net buy
   Status: TRIPLE CONFIRMATION

🚀 EXPLOSIVE LANE
1. EFGH
   Explosive: 78
   Room: 84 | RR: 2.4 | Activity: 81
   Risk: aggressive but passed explosive safety gate
   Action: run /check EFGH before entry

💰 SMART-MONEY WATCH
1. MNOP
   Whitelist net buyers: YU, AK
   Technical confirmation: not yet
   Status: pre-breakout watch, not an entry call

🔭 LONG-HORIZON WATCH
1. QRST
   Multibagger: 82/100
   Horizon: 6–12 months
   Also in HC: yes
```

---

## 8. Tracking Schema

Persist these fields when the brief is generated:

```text
signal_date
ticker
market_regime
backbone_score
probability_score
predicted_danger
passed_danger_gate
backbone_rank
sdt_selected
sdt_lane
hc_selected
hc_score
consensus_prime
explosive_score
explosive_selected
smart_money_bonus
smart_money_brokers
smart_money_net_pct
smart_money_divergence
multibagger_score
final_display_tier
entry_plan
tp1
cut_loss
formula_version
```

Outcome horizons:

- Consensus and Explosive: Day 1–5
- Smart-money overlay: compare enriched versus unenriched forward cohorts
- Multibagger: Day 30, 60, 90 and 180

---

## 9. Implementation Plan for Claude Agent

### Phase 1: Shared backbone module

Create a dedicated module, preferably:

```text
engine/backbone.py
```

Responsibilities:

- compute danger score
- apply gate
- compute probability score
- apply regime adjustment
- rank Top-8
- compute Explosive Score
- produce serializable explanations

Keep all calculations deterministic and unit-testable.

### Phase 2: Nightly integration

In `engine/nightly.py`:

1. Run scoring for the full eligible Sharia universe.
2. Compute and persist market regime.
3. Run backbone against the complete EOD results.
4. Save backbone pool and metadata in a dedicated cache partition.
5. Run RapidAPI sweeps only through existing quota-protected nightly functions.
6. Save smart-money and multibagger context separately.

Suggested cache partitions:

```text
backbone_daily
rapidapi_smartmoney
rapidapi_market_intelligence
```

### Phase 3: Primary scanners

In `commands/scan.py`:

- `/screendaytrade` reads `backbone_daily` instead of independently selecting from the full cache.
- `/hc` reads the same backbone pool.
- `/gptpick` reads the same backbone pool.
- Preserve existing scanner-specific scoring after the backbone.

### Phase 4: Consensus brief

Refactor `/consensus` so it:

1. Loads Backbone Top-8.
2. Computes SDT and HC results on the same pool.
3. Finds exact same-date intersection.
4. Selects 1–3 Explosive Lane candidates.
5. Applies RapidAPI smart-money overlay.
6. Appends multibagger information.
7. Outputs concise sections in the required order.

### Phase 5: `/check` integration

`/check` remains the live validation tool and should show:

- VWAP movement 15/30/60
- active breakout
- volume pace
- trigger/invalidation
- Bollinger context
- smart-money context if cached
- clear action: enter/watch/wait/avoid, without invoking full executiongate

### Phase 6: Tests

Required tests:

```text
test_backbone_danger_gate.py
test_backbone_ranking.py
test_explosive_lane.py
test_consensus_intersection.py
test_smartmoney_bonus_bounds.py
test_rapidapi_missing_is_neutral.py
test_multibagger_separate_horizon.py
test_scanners_use_same_backbone_pool.py
```

Acceptance criteria:

- no live RapidAPI fetch from bulk scanner command
- no candidate outside Sharia eligible universe
- missing RapidAPI never penalizes candidate
- Consensus Prime is exact SDT ∩ HC intersection
- Explosive Lane max 3 and passes minimum safety rules
- Top-8 is derived before scanner-specific ranking
- source and formula version are tracked
- existing Telegram commands still compile and register

---

## 10. Three-Day Simulation

The simulation uses a Top-8 moderate gate trained/frozen from the 2025 discovery period and evaluated on clean historical outcomes. Entry is the next trading-day open. TP/SL outcomes are monitored for up to five trading days.

### 10.1 Signal date: 31 July 2026

**Regime:** R1 Bull Stable  
**Consensus Prime:** SIMP

```text
SIMP
Entry: 3 Aug 2026 @ 610
TP1: 645
SL: 540
Outcome: time-based loss
Exit: 10 Aug 2026 @ 595
Return: -2.46%
Holding period: 5 trading days
```

Top-8 day summary:

```text
Wins: UCID, ERAA, BIRD
Losses: KPIG, MPMX, SIMP, DMAS, BUDI
Win rate: 37.5%
Consensus-only win rate: 0% (n=1)
```

Important interpretation: Consensus is high quality in aggregate, not guaranteed on each date. SIMP did not hit TP or SL within five days and closed at the time limit.

### 10.2 Signal date: 30 July 2026

**Regime:** R1 Bull Stable  
**Consensus Prime:** AALI and SMSM

```text
AALI
Entry: 31 Jul 2026 @ 6,875
TP1: 7,220
SL: 6,250
Outcome: TP win
Exit: 4 Aug 2026 @ 7,220
Return: +5.02%
Time to TP: 2 trading days

SMSM
Entry: 31 Jul 2026 @ ~1,740
TP1: 1,825
SL: 1,620
Outcome: time-based loss
Exit: 7 Aug 2026 @ 1,715
Return: -1.44%
Holding period: 5 trading days
```

Consensus day result:

```text
Wins: 1
Losses: 1
Win rate: 50%
Average return: approximately +1.79%
No dangerous loss
```

### 10.3 Signal date: 4 August 2026

**Regime:** R1 Bull Stable  
**Consensus Prime:** DKFT and INTP

```text
INTP
Entry: 5 Aug 2026 @ 5,000
TP1: 5,275
SL: 4,440
Outcome: TP win
Exit: 6 Aug 2026 @ 5,275
Return: +5.50%
Time to TP: 1 trading day

DKFT
Entry: 5 Aug 2026 @ 710
TP1: 745
SL: 610
Outcome: time-based loss
Exit: 12 Aug 2026 @ 670
Return: -5.63%
Holding period: 5 trading days
```

Consensus day result:

```text
Wins: 1
Losses: 1
Win rate: 50%
Average return: approximately -0.07%
No dangerous loss
```

---

## 11. Holding-Period Guidance

Based on the three-day examples:

- Winning TP events occurred in **1–2 trading days**.
- Non-TP/non-SL trades were resolved at the existing **5 trading-day time limit**.
- A losing hard SL example in the broader Top-8 occurred after 4 days, while several weak trades drifted until Day 5.

Operational guidance:

```text
Day 1–2:
Best window for fast TP realization. Run /check before entry and monitor live confirmation.

Day 3:
If momentum has not developed, downgrade confidence and avoid adding size.

Day 4–5:
Treat as evaluation/exit window. Do not indefinitely hold a short-horizon signal merely because SL has not been hit.

Maximum default holding period:
5 trading days for Consensus and Explosive short-horizon picks.
```

This is not a universal sell rule. `/check` may justify earlier exit, continued hold, or no entry based on live VWAP, active breakout, volume pace, and invalidation.

---

## 12. Research Caveats

- Historical candidate logs are derived from previous scanner outputs and do not fully reconstruct all ~389 EOD universe members before selection.
- RapidAPI was not backfilled and must be evaluated prospectively.
- Bollinger integration in v3.17 is not fully represented in older trade logs.
- Consensus sample is small despite strong aggregate quality.
- January–May 2026 remains a crash stress test; June–August 2026 is the near-current validation period.
- Retraining or threshold changes must use a documented discovery period and locked validation, never same-period tuning.


---

# Research Addendum: Adaptive Tool-Specific Formulation

## 13. Research Decision Summary

The research does **not** support one identical ranking formula for `/screendaytrade`, `/hc`, and the Explosive Lane. Each lane has a different objective:

- `/screendaytrade`: momentum, asymmetric upside, and fast realization.
- `/hc`: structural quality, probability, and lower tail risk.
- Explosive Lane: large upside or fast momentum, accepting a lower hit rate if expectancy stays positive and danger remains controlled.
- `/consensus`: state-aware agreement between final SDT and final HC, not a separate primary scanner.

The best implementation candidate therefore uses a shared natural backbone and adaptive downstream ranking.

## 14. Valid Research Design

### Discovery and training

```text
Full 2025
+ June–July 2026
```

January–May 2026 is retained as a crash/stress reference and is not used in this specific walk-forward fit.

### Out-of-sample test

```text
3–12 August 2026
```

All available August test dates were R1 Bull Stable. R3 findings are based on the separate 2025-trained validation and July R3 simulations. Future R3 forward validation remains required.

### Weighting conclusion

- Applying a large aggregate weight to June–July in both Probability and Danger models destabilized SDT and Consensus.
- Natural per-observation weighting was strongest for Probability ranking.
- A 60:40 recent-risk model is useful only as a soft risk overlay or limited HC safety check.
- It should not be a universal hard rejection gate.

## 15. Proposed Production Candidate Formula

### 15.1 Shared backbone

```text
Natural Probability Model
+ Natural Danger Gate
+ regime routing
+ eligible Sharia universe
```

R1 initial gate:

```text
Natural Danger quantile: Q55
```

R3 initial gate:

```text
Natural Danger quantile: Q35
```

The model must save raw scores and cutoff versions. Never train or modify thresholds inside Telegram command execution.

### 15.2 SDT adaptive ranking

Conceptual score:

```text
SDT Rank =
    Natural Probability
  + Relative Strength
  + ADX reliability
  + CMF
  + Activity, bounded
  + Room, bounded
  + existing SDT lane strength
  - chase and extension penalties
  - soft 60:40 Recent Risk penalty
```

Policies:

- Maximum three priority names in the Consensus brief, but `/screendaytrade` may retain its fuller output.
- No mandatory minimum count.
- Recent Risk is caution, not hard rejection.
- Strong whitelist broker net sell should apply a bounded divergence penalty.
- An unresolved ticker cannot generate a new entry signal.

### 15.3 HC adaptive ranking

Conceptual score:

```text
HC Rank =
    Natural Probability
  + HC structural criteria
  + Safety
  + Continuation quality
  + CMF
  + regime compatibility
  - Natural Danger
  - stronger 60:40 Recent Risk penalty
```

Initial quality control candidate:

```text
HC Rank minimum: 55
```

This threshold is a development candidate, not a permanently validated constant. It must be versioned and forward-tested.

Policies:

- Maximum three names in the brief.
- Do not fill quota if fewer names pass quality control.
- HC is allowed to be slower than SDT, but Day 2–3 confidence must still be updated.

### 15.4 Explosive Lane

Base score:

```text
Explosive Score =
    32% Room percentile
  + 30% RR percentile
  + 23% Activity percentile
  + 15% Controlled Volatility percentile
```

Penalties:

- volatility beyond upper tail
- Natural Danger above regime cutoff
- Recent Risk caution
- near-price-floor
- bearish OBV divergence
- bearish MACD cross combined with below SMA50
- excessive extension/chase

Initial production candidate:

```text
R1 minimum Explosive Score: 50
Maximum names: 3
No forced fill
```

R3 policy:

```text
Use stricter volatility ceiling.
Search inside final SDT first.
An outside-SDT candidate requires at least one:
- CMF confirmation
- strong trend confirmation
- smart-money accumulation
```

Labels:

- `FAST MOMENTUM`: expected short-horizon realization around Day 1–3.
- `TRUE EXPLOSIVE`: upside realization at or above 10%, or sufficiently large modeled room with confirmation.

## 16. State-Aware Consensus

A static same-day intersection is insufficient once duplicate entries are blocked. Implement three states:

```text
NEW CONSENSUS
Fresh ticker selected by final SDT and final HC today.

ACTIVE CONSENSUS
Existing active position/pick that remains confirmed by both lanes.

UPGRADED TO CONSENSUS
Ticker entered from one lane previously and receives confirmation from the other lane later.
```

No new entry should be generated for `ACTIVE` or `UPGRADED` if a position already exists. The brief should show the upgraded confidence and monitoring action instead.

## 17. Position-State and Cooldown Rules

```text
If ticker has unresolved active pick:
    block duplicate entry
    keep it under ACTIVE PICKS

If ticker just hit SL:
    cooldown 2–3 trading days
    unless /check confirms a valid reclaim
```

State tracking requires:

```text
first_signal_date
last_confirmation_date
active_lane_set
entry_date
planned_tp
planned_sl
position_status
cooldown_until
```

## 18. Dynamic Confidence Day 1–5

The historical outcome logs do not yet store full daily paths, so Day 2 negativity is an early warning, not a proven automatic failure rule.

Operational state machine:

```text
Day 1:
DEVELOPING / FAST CONFIRMATION

Day 2:
CONFIDENCE UP / NEUTRAL / EARLY EXIT WATCH

Day 3:
VALIDATED / STALLED / FAILED SETUP

Day 4–5:
HOLD RUNNER / TIME EXIT / CUT
```

Rules:

- Day 2 negative alone does not force exit.
- Day 2 negative plus failed VWAP, weaker volume, or broker distribution triggers `EARLY EXIT WATCH`.
- Day 3 still negative or stalled with weak structure triggers `FAILED SETUP` consideration.
- Default short-horizon maximum remains five trading days.

Required shadow-path fields:

```text
return_day_1 ... return_day_5
high_return_day_1 ... high_return_day_5
low_return_day_1 ... low_return_day_5
positive_day_2
recovered_after_negative_day_2
day_of_tp
day_of_sl
max_gain_5d
max_drawdown_5d
post_tp_max_gain_5d
```

## 19. Integrated August Replay Results

### Initial integrated state-aware replay, W1 August

```text
SDT:        n=6,  WR=66.7%, avg=+2.14%, danger=0%
HC:         n=12, WR=41.7%, avg=+0.08%, danger=0%
Explosive:  n=4,  WR=50.0%, avg=+2.37%, danger=0%
```

This demonstrated that tool-specific ranking improved SDT and Explosive, while HC needed a quality threshold.

### Threshold exploration candidate

A grid explored HC quality threshold, SDT rank threshold, and Explosive minimum score. The balanced candidate selected under minimum sample requirements was:

```text
HC Rank minimum: 55
Explosive minimum: 50
SDT rank minimum: none
HC pwin minimum: none
```

Across available August signals with unresolved-position blocking:

```text
SDT:        n=7,  WR=57.1%, avg=+0.89%, danger=0%
HC:         n=10, WR=70.0%, avg=+2.53%, danger=0%
Explosive:  n=4,  WR=50.0%, avg=+1.77%, danger=0%
```

Important limitation: thresholds were compared on August outcomes, so these values are **research candidates**, not fresh out-of-sample proof. Freeze them before forward deployment.

## 20. W1 August Candidate Picks Under the Balanced Formula

### 3 August 2026

```text
SDT:
AALI  +1.77%, Day 5
ICBP  +5.00%, TP Day 3
TLKM  -4.74%, Day 5

HC:
UCID  +6.10%, TP Day 1
ARNA  +0.81%, Day 5
SMSM  -2.53%, Day 5

Explosive:
LPCK +11.11%, TRUE EXPLOSIVE, TP Day 3
INTP  +3.92%, FAST MOMENTUM, TP Day 1
MAPA  -5.19%, Day 5
```

### 4 August 2026

```text
SDT:
ICON  +6.84%, TP Day 3
DKFT  -5.63%, Day 5

HC:
BAYU  +1.71%, Day 5
KBLI  -0.61%, Day 5
```

### 5 August 2026

```text
SDT:
BKSL  +9.59%, TP Day 1

Explosive:
ACES  -2.75%, Day 5
```

### 6 August 2026

```text
HC:
EKAD  +6.32%, TP Day 3
IKBI  +5.24%, TP Day 1
BISI  -1.44%, Day 5
```

### 10–11 August 2026 continuation sample

```text
SDT:
TPIA  -6.57%, SL Day 3

HC:
POWR  +5.23%, TP Day 1
WAPO  +4.48%, TP Day 1
```

TPIA should receive additional foreign-broker distribution caution when RapidAPI confirms persistent net sell.

## 21. Core Research Findings

### Probability and danger

- Winner prediction is moderate, while dangerous-loss prediction is materially stronger.
- Danger Gate is the most defensible shared backbone component.
- A universal P90 hard AND-filter is not stable enough.
- P90/volatility position should remain a continuous input.

### R1 Bull Stable

Prioritize:

- relative strength
- ADX reliability
- CMF
- bullish MACD context
- controlled activity and volatility

Avoid:

- activity/volume extremes without confirmation
- breakout extension
- overbought danger

### R3 Sideways

Prioritize:

- strict Danger Gate
- CMF
- Safety
- relative strength
- moderate breakout/activity

Avoid:

- volatility upper tail
- volume extremes
- explosive candidates with room but no directional confirmation

Validated R3 candidate result from 2025-trained regime model:

```text
Top-8, Q35 Danger Gate
WR 60.5%
Average return +1.03%
Danger 3.4%
Around 5.17 candidates per day
```

### Explosive finding

The strongest historical explosive associations were:

- Room
- RR
- day-range/volatility, controlled
- Activity

Very high Breakout or Continuation scores alone did not produce the highest explosive realization rate.

### Holding period

- Strong winners frequently realized TP during Day 1–3.
- Stalled and failed picks often remained unresolved until Day 5.
- This supports Day 2 warning and Day 3 validation/failure checkpoints, subject to future shadow-path proof.

## 22. RapidAPI Integration

### Smart-money whitelist

The existing whitelist broker sweep remains an enrichment layer. It must:

- collect top net-buy activity from whitelisted brokers
- intersect results with SDT, HC, Consensus, and Explosive candidates
- add a bounded score bonus
- apply divergence caution for strong whitelist net sell
- remain neutral when data is unavailable

Suggested display priority:

```text
Consensus + smart money = TRIPLE CONFIRMATION
Explosive + smart money = EXPLOSIVE + SMART MONEY
Smart money without technical confirmation = WATCH ONLY
Technical strength + smart-money net sell = DIVERGENCE WARNING
```

### Multibagger

RapidAPI multibagger remains a separate long-horizon information block. Do not materially blend it into Day 1–5 scoring.

## 23. Implementation Acceptance Criteria

- Shared natural backbone runs before SDT, HC and GPTPick.
- SDT, HC and Explosive use separate ranking functions.
- HC Rank threshold and Explosive minimum score are configuration constants with version metadata.
- Recent 60:40 Risk is an overlay, not a universal gate.
- Consensus is state-aware: NEW, ACTIVE, UPGRADED.
- Unresolved ticker cannot generate duplicate entry.
- RapidAPI missing data is neutral.
- Smart-money bonus is bounded.
- Multibagger remains separate horizon.
- `/check` produces Day 1–5 confidence state.
- Shadow path is persisted for future Day 2 failure/recovery research.
- Every released formula has a version and freeze date.

## 24. Formula Freeze Recommendation

Candidate release name:

```text
MBSS Adaptive Backbone Research Candidate 1
AB-RC1
```

Freeze configuration:

```text
Backbone: Natural Probability + Natural Danger
R1 Danger Gate: Q55
R3 Danger Gate: Q35
SDT: adaptive momentum rank, no hard Recent Risk gate
HC: adaptive structure rank, candidate minimum 55
Explosive: minimum score 50, max 3, no forced fill
Recent Risk: 60:40 soft overlay
Consensus: state-aware NEW/ACTIVE/UPGRADED
Default hold: max 5 trading days
```

Do not re-tune AB-RC1 until at least 20–30 new trading days are collected, except for critical code defects or safety failures.

---

# Final Practical Simulation Summary

## W1 August 2026, R1 Bull Stable

Period: signal dates 3–7 August 2026.

```text
Fresh unique picks: 18
Win rate: approximately 61.1%
Average return: approximately +1.97%
Dangerous loss <= -10%: 0%
```

Fast winners realized in Day 1–3 included UCID, INTP, BKSL, IKBI, ICBP, ICON, LPCK, and EKAD. LPCK realized a true explosive gain of +11.11% by Day 3. Slow winners generally produced smaller gains by Day 5. Failed or stalled picks generally remained weak until the Day 5 time exit.

## W2 August 2026, R1 Bull Stable

Period: signal dates 10–12 August 2026, with position monitoring through 14 August.

```text
Fresh unique entries: 3
Winners: POWR and WAPO
Loser: TPIA
Win rate: 66.7%
Average return: approximately +1.05%
Dangerous loss <= -10%: 0%
```

Timeline:

```text
10 August:
- POWR, HC Priority, +5.23%, TP Day 1
- TPIA, SDT Priority with caution, -6.57%, SL Day 3

11 August:
- WAPO, HC Priority, +4.48%, TP Day 1

12 August:
- No new entry
- TPIA remained an active risk-monitoring position
- POWR closed at TP
- WAPO remained active toward TP
```

This demonstrates the intended production behavior: issue several picks when quality is broad, issue only a few when opportunity is limited, and explicitly return `NO NEW ENTRY` when no fresh setup satisfies the formula.

## Final Operating Principle

```text
Quality over quota.
No forced picks.
Winner confidence should normally strengthen by Day 1–3.
Day 2 weakness is an early-warning state, not an automatic exit.
Day 3 weakness plus failed live structure is a failed-setup candidate.
Default maximum holding period is five trading days.
```

