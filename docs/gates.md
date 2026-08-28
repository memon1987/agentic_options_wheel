# Gate inventory — every control on the live sell path

**Plans:** `docs/plans/fc-013.md` (this document is its §7 deliverable),
`docs/plans/fc-069.md` items 3 / 4 / 14
**Created:** 2026-08-03 (post-FC-068 / FC-065 / FC-071 / FC-075 P1 tree)

A *gate* here means: a check that can stop the bot from opening or rolling a
position. This file is the single place that says what they are, where they
live, what config drives them, what they emit when they fire, and which test
pins them.

**Why it exists.** This codebase's recurring defect is a control that reads as
live and is not: `STATE_STORAGE_BUCKET` was never set so wheel-state
persistence never worked; the 4h min-hold gate read an in-process dict that was
empty by the time it was read; the pre-FC-065 cost-basis floor resolved to zero
for every assigned position; the roller's earnings gate is structurally no-op'd
by a quote-key bug; and until FC-013 the `earnings.*` knobs read as live and
gated nothing that opens a position. A gate with no row in this table is a gate
nobody is watching.

**Reading rule.** "Fails closed" means the check blocks the trade when it
cannot evaluate. "Fails open" means it allows the trade. Both appear below;
which one is correct is a per-gate decision, and every fail-open is deliberate
and stated.

---

## The live pipeline

```
/scan   OptionsScanner  ──►  GCS opportunity blob  ──►  /run  ExecutionEngine  ──►  PutSeller / CallSeller
        (stage gates)                                    (selection gates)          (execution gates)

/roll   WheelEngine.run_rolling_cycle ──► CallRoller ──► RiskManager.validate_roll   (daily 15:30 ET)
/monitor  closes only — never gated (a close reduces risk)
```

`main.py --command scan`, Cloud Run `/scan`, and the backtest simulator all
construct `OptionsScanner` through the same constructor, so every scan-stage
gate applies identically in production, on the CLI, and in replays.

---

## Stage 1 — scan (`src/data/options_scanner.py`, `src/api/market_data.py`)

| # | Gate | Where | Applies to | Config | Fires as | Fail posture |
|---|---|---|---|---|---|---|
| 1 | Price / volume band | `market_data.filter_suitable_stocks` | puts | `strategy.min_stock_price`, `max_stock_price`, `min_avg_volume` | `stock_rejected_filter` (tally: "price/volume band (stage 1)") | n/a — a missing metric drops the symbol |
| 2 | **Earnings blackout, put leg** | `OptionsScanner._put_leg_blocked_by_earnings` | puts | `earnings.enabled` (+ `EARNINGS_ENABLED` env), `earnings.blackout_days: 2` | `put_scan_skipped_earnings_blackout` / `..._unknown` | **closed** on unknown |
| 3 | Existing position skip | `OptionsScanner._has_existing_position` | puts | — | `put_scan_skipped_existing_position` (`reason`: `stock_position` / `option_position`; tally: "already holds this underlying (scan, put)") — silent until FC-069 item 12; the API-error limb fires `position_check_failed` instead | **closed** — an exception returns `True`, as does an unparseable option symbol |
| 4 | **Earnings unknown, call leg** | `OptionsScanner.scan_for_call_opportunities` | calls | as row 2 | `call_scan_skipped_earnings_unknown` + row `blocked{earnings_unknown}` | **closed** |
| 5 | ≥100 shares | `scan_for_call_opportunities` | calls | — | row `not_eligible{insufficient_shares}` | n/a |
| 6 | Cost-basis floor — divergent | `CostBasisResolver.resolve_detailed` | calls | tolerance `max($0.10, 0.1%)` | `call_scan_skipped_cost_basis_divergent` + row `blocked{floor_divergent}` | **closed** |
| 7 | Cost-basis floor — unresolved | `scan_for_call_opportunities` | calls | — | `call_scan_skipped_cost_basis_unresolved` + row `blocked{floor_unresolved}` | **closed** |
| 8 | Chain criteria (DTE / premium / delta / liquidity / below-basis) | `market_data._check_call_criteria_detailed`, `_check_put_criteria_detailed` | both | `strategy.{put,call}_target_dte`, `min_{put,call}_premium`, `{put,call}_delta_range` | `stage_7/8_complete_not_found`, `reason_counts` on the row | n/a |
| 9 | **Earnings SPAN, per candidate** | `market_data.find_suitable_calls(exclude_expiry_on_or_after=…)` | calls | as row 2 (no numeric knob — see below) | rejection stat `expires_into_earnings`; if it empties the set, `call_scan_skipped_earnings_blackout` + row `blocked{earnings_blackout}` | **closed** (an unparseable expiry is rejected) |
| 10 | Stock quote unusable | `OptionsScanner._create_call_opportunity` | calls | — | `call_scan_skipped_quote_unavailable` + row `no_candidates{quote_unavailable}` | **closed** (FC-065 P1; it used to emit an opportunity priced against a `current_price` of 0) |

### The earnings gate in detail (FC-013)

**Per-leg semantics, decided by the operator 2026-08-03 on the step-0 audit and
`docs/investigations/earnings-pop-callaway-2026-08-03.md`. The legs diverge
deliberately.**

- **Puts — N=2 days-until, symbol-level.** Block symbol S when
  `0 <= (next_earnings_date(S) − today) <= 2`, calendar days, inclusive at both
  ends, day-of blocked. Put-side realized-loss risk concentrates in immediate
  pre-event *entries* (the GOOGL 2026-04-28 incident, `days_until = 1`), while
  wide-window pre-earnings put income was the book's best bucket — 100% win
  rate, $301 average. A wide put window would forfeit the best trades to block
  a risk that lives in the last two days. Post-earnings is deliberately
  uncovered: the service looks forward only, and day-after IV crush is what the
  wheel should be selling into.
- **Calls — TRUE SPAN, per candidate.** Reject a candidate when
  `expiration_date >= next_earnings_date`. Candidates expiring *before* the
  event proceed. `>=` is inclusive because assignment on an expiry landing on
  the report date resolves after the report.
  - There is **no numeric knob** for the call leg, on purpose. Span is the risk
    predicate itself and is DTE-invariant by construction. A hardcoded N=7
    manufactures a silent disconnect the day trading parameters extend past 7
    DTE; deriving N from DTE over-blocks progressively at longer DTE, making a
    30-DTE call that expires before the event illegal. Both were explicitly
    rejected by the operator.
  - There is **no symbol-level blackout skip** on this leg: a symbol reporting
    in three days may legally sell a call expiring in two.
- **Unknown is symbol-level on both legs** and is a *different* state from
  blackout, with a different event, a different decision reason, and its own
  alert. A span test needs a date; no date means no candidate can be cleared.

**`earnings_hour` is fetched, cached and logged — and deliberately unused by
both predicates.** Finnhub returns `bmo` / `amc` / `dmh`, and the service
carries it through to the enrichment fields, but neither the N=2 window nor the
span test reads it. Consequences, stated so this is a recorded decision rather
than an oversight:

- A **BMO** reporter is blocked for the whole of day D even though the report
  lands before the open, so the post-report session is treated as pre-report.
  That is conservative, not dangerous.
- **Immaterial today:** every reporter that has actually filled on this book is
  AMC (AAPL, AMZN, GOOGL, MSFT, NVDA). It becomes real the day a tradeable BMO
  reporter joins the universe, at which point the fix is to let `earnings_hour`
  narrow the day-of case — the field is already plumbed for it.
- The `>=` span boundary was reviewed against this and **validated**: for an AMC
  reporter, a contract expiring on the report date settles that afternoon, and
  contrary exercise lands before the ~5:30pm ET cutoff — after the report. So
  expiry-day-equals-event-day genuinely carries the gap and the inclusive
  comparison is right.

**Fail-closed blast radius, and what bounds it.** A transient Finnhub outage is
bounded by the two-layer cache (L1 module scope, L2 a GCS blob — only symbols
whose cached dates are >24h stale block) and the 1h failure cache. A
*persistent* cause — key wiped by a `--set-env-vars` deploy, key revoked, plan
lapsed — blocks **all opens indefinitely**, and nothing self-heals it. Two
controls bound that: Alert 4 in `deploy/monitoring/drawdown_pause_alert.md`
(policy: `deploy/monitoring/earnings_gate_alert_policy.json`), and the
`EARNINGS_ENABLED` env lever below.

**`enabled: false` and *broken* are different states.** Disabled means no gate
at all and byte-identical pre-FC-013 behaviour. Broken means every symbol
answers `unknown`, every symbol is skipped, and `earnings_gate_unusable` is
logged on every construction — loud, never a silent no-op (FC-069 item 8).

**Acceptance criterion, verified.** The shipped configuration blocks every
incident on record: GOOGL put 2026-04-28 (`days_until = 1 ≤ 2`); AAPL C347.5
2026-07-30 (`days_until = 0`, expiry 08-03 ≥ 07-30 → spanning); AMZN C262.5
2026-07-30 (`days_until = 0`, expiry 08-07 ≥ 07-30 → spanning, $2,308 of upside
surrendered against $222 collected). Encoded executable as
`test_the_incident_geometry_is_blocked`.

**Residual accepted, on the record.** Span also blocks the spanning calls that
*profit* on gap-downs (AAPL's C347.5 made $165; GOOGL's two July spanners made
$584 combined). Accepted because the asymmetry is roughly 10:1 the other way —
one gap-up pop costs ~$2,300 gross (~$1,500–2,100 re-arm-adjusted) against a
~$100–200 cushion per gap-down — and those cushion trades are exactly the
earnings-IV bait the scorer over-selects (cross-ref FC-073).

---

## Stage 2 — selection (`src/strategy/execution_engine.py`)

Runs on `/run`, against the opportunity blob the scan wrote.

| # | Gate | Where | Config | Fires as | Fail posture |
|---|---|---|---|---|---|
| 11 | Stale-blob age | `OpportunityStore` (blob read on `/run`) | `strategy.opportunity_max_age_minutes: 30` | stale opportunities not returned | closed |
| 12 | Already-positioned / previously-failed | `filter_duplicate_opportunities`, `filter_failed_opportunities` | — | rows `dropped{already_positioned, previously_failed}` | closed; `_failed_symbols` is process-local and resets on instance death (accepted amnesia, FC-069 item 15) |
| 13 | Duplicate underlying in batch | `select_batch` | — | `selection_dropped{duplicate_underlying}` | closed |
| 14 | Share ledger (calls) | `select_batch` | — | `selection_dropped{insufficient_available_shares}` | closed; `positions_unavailable` when the position read fails |
| 15 | Buying power (puts) | `select_batch` | — | `selection_dropped{insufficient_buying_power}` | closed |
| 16 | Sizing | `select_batch` | `risk.max_position_size` | `selection_dropped{sizing_failed}` | closed |

`DROP_REASONS` is FC-038's model for **batch-resource contention** — shares,
buying power, duplicate underlying, all evaluated against *this* batch. The
earnings gate is deliberately *not* here (FC-013 DD-1): earnings is a policy
fact known before ranking, `/run` has no calendar of its own, and gating at
both stages buys nothing — both predicates are date-granular and the ≤30 minutes
between `/scan` and `/run` sit inside one market session and never cross
midnight, so neither can change value in that window.

---

## Stage 3 — execution (`src/strategy/put_seller.py`, `src/strategy/call_seller.py`)

| # | Gate | Where | Config | Fires as | Fail posture |
|---|---|---|---|---|---|
| 17 | Wrong-seller routing | `PutSeller.execute_put_sale`, `CallSeller.execute_call_sale` | — | `call_rejected_by_put_seller`, `put_rejected_by_call_seller` | closed |
| 18 | Execute-time cost-basis floor | `CallSeller`, via `opportunity_floor_per_share` | — | rejection before order submit | closed (FC-050 restored this; it reads the floor off the opportunity so scan and execute enforce the same number) |
| 19 | Naked-call block | `ExecutionEngine.execute_batch` | — | `naked_call_blocked` | closed |
| 20 | Naked-call invariant (FC-041) | `ExecutionEngine.execute_batch`, immediately before `execute_call_sale` | — | `naked_call_invariant_blocked` + `selection_dropped{naked_call_invariant}` | closed |

Gate 20 is **belt to gate 19's braces, not a duplicate of it.** Gate 19 asks
`_available_shares` (which uses `parse_option_symbol`, whose leading-letters
fallback is a heuristic); gate 20 recounts the same positions snapshot with an
independent implementation that uses only `strict_option_type` and `occ_root`,
and requires `committed + about-to-sell <= owned`. Two implementations that must
agree; when they disagree we do not know which is right, so the order is refused
and the disagreement is logged with both answers. A non-zero
`dropped_naked_call_invariant` in `batch_selection_completed` — or any
`naked_call_invariant_blocked` error event — is a **defect report**, not a normal
batch outcome. Neither gate marks the contract non-retryable: share ownership is
transient.

---

## The roll path (daily, `src/strategy/call_roller.py`)

Rewritten by FC-078. The roller runs every trading day at 15:30 ET and is
**credit-only**: the invariant `STO_limit − BTC_limit >= min_net_credit` is
enforced on the limit prices actually placed, so a filled roll can never net a
debit.

| # | Gate | Config | Notes |
|---|---|---|---|
| 20 | Open-order conflict | — | a live open order on the option symbol → skip; `/monitor`'s DAY buy-to-close limits outlive its 14:55 slot and the profit-taker has precedence |
| 21 | ITM trigger ratio | `rolling.itm_trigger_ratio: 0.98` | OTM calls are the profit-taker's territory |
| 22 | Stock-quote quality | — | two-sided **and** `ask/bid <= 1.05`; **fails closed** (`stock_quote_unusable`) |
| 23 | Cost-basis floor | — | shared `CostBasisResolver`, **fails closed** (FC-065 P2), alert-wired since FC-078 |
| 24 | Earnings span on the **replacement** | `earnings.enabled` | `next_earnings_info` tri-state → `exclude_expiry_on_or_after`; `unknown` (or a missing calendar) skips the whole roll. **Fails closed** |
| 25 | `RiskManager.validate_roll` | `rolling.max_replacement_delta: 0.60`, `rolling.max_extension_days: 14` | strike > old; strike >= basis; delta upper rail only; expiry <= **old expiry** + N |
| 26 | Credit invariant | `rolling.min_net_credit_per_contract: 0.00` | checked at the candidate screen, pre-BTC re-check, order construction, and every ladder rung |
| 27 | Cycle time budget | — | a position is not *started* without 600 s of remaining budget → `cycle_budget_exhausted` |

**Deleted by FC-078** (see the plan's DD-6 table for each fate):
`rolling.max_current_dte` (the DTE ≤ 1 eligibility trap),
`rolling.max_rolls_per_position` (counted wheel-state rolls that never
persisted — always read 0, never bound), `rolling.earnings_blackout_days` (the
fail-OPEN blackout, subsumed by the fail-CLOSED span predicate above),
`rolling.max_debit_pct_of_premium` / `_of_notional` (no debit path exists to
tolerate), `rolling.btc_limit_over_ask_pct` / `stc_limit_under_bid_pct` (pads
that broke or wasted the credit invariant), `rolling.trigger_time_et` (never
consumed; the scheduler owns timing).

**The roller's earnings posture is now the scanner's.** FC-069 item 4's
"roller keeps its own fail-open gate" sign-off is superseded: the roll path
consumes the same tri-state `next_earnings_info` surface the scanner does and
fails closed on `unknown`. The buyback/replacement distinction is what makes
this correct — the gate exists to stop *opening* gap exposure, so it filters
**candidates**; when it empties the candidate set the roller places no order at
all rather than closing and staying uncovered.

**The injected-calendar quirk is gone.** It used to be that an injected calendar
bypassed the `enabled` check, because `run_rolling_cycle` consulted
`config.earnings_enabled` only when constructing its own service. FC-078's
roller gates the span check on `config.earnings_enabled` directly, so config
wins either way — the same posture as the scanner. A calendar that is injected
while earnings are disabled is used for log enrichment only, never as a gate;
and a *missing* calendar while earnings are enabled fails the roll closed
(`earnings_unknown`) rather than open.

`/monitor` closes positions and is **never gated** — a close reduces risk.

---

## Config keys and their env overrides

| Key | Default | Consumer | Env override |
|---|---|---|---|
| `earnings.enabled` | `true` | scanner gate construction (both legs) | **`EARNINGS_ENABLED`** — set wins, unset falls back to yaml, unparseable is ignored with a warning |
| `earnings.blackout_days` | `2` | put leg only | — |
| `earnings.cache_ttl_hours` | `24` | L1 + L2 entry validity | — |
| `earnings.lookahead_days` | `90` | Finnhub query window; validated `>= call_target_dte + 7` | — |
| `rolling.enabled` | `true` | roller master switch | **`ROLLER_ENABLED`** — same semantics as `EARNINGS_ENABLED` (FC-078) |
| `rolling.max_extension_days` | `14` | roll horizon, measured from the **old expiry** | — |
| `rolling.max_replacement_delta` | `0.60` | replacement delta upper rail | — |
| `rolling.min_net_credit_per_contract` | `0.00` | the credit invariant | — |
| `rolling.imminence_extrinsic_threshold` | `0.20` | assignment-imminence pricing override | — |
| — | `false` | roller evaluates but places neither leg | **`ROLLER_DRY_RUN`** — env-only, no yaml key |
| ~~`risk.gap_risk_controls.earnings_avoidance_days`~~ | — | **deleted 2026-08-04 by FC-069 S1**, with the whole `gap_risk_controls` block and `GapDetector`. It never gated anything and is unrelated to `earnings.blackout_days`, which is this gate's live knob. | — |

`EARNINGS_ENABLED` exists because the yaml value is baked into the image, so a
config rollback rides Cloud Build — the same pipeline that once sat silently
red for 11 days (FC-031). Use `--update-env-vars`, **never** `--set-env-vars`
(the latter wipes the whole env set, which is itself one of this gate's
persistent-failure scenarios).

**Kill-switch blast radius — wider than the scanner.** `earnings_enabled` is
read in two places, not one: the scanner's gate construction (rows 2/4/9 above)
**and** `WheelEngine.run_rolling_cycle`, which consults it before constructing
its own `EarningsCalendarService`. So `EARNINGS_ENABLED=false` also takes the
**roller's** earnings span gate dark (`call_roller.py:357` gates the span check
on `config.earnings_enabled` directly). This is **not** moot: FC-078 revived the
roller and it executes real rolls daily, so throwing the earnings kill switch
disarms two gates, not one. (The pre-FC-078 note that an *injected* calendar
bypassed the `enabled` check is obsolete — see "The injected-calendar quirk is
gone" below.)

---

## The emergent one-position invariant

Carried verbatim from FC-069 item 3. `max_positions_per_stock` was a dead knob
and FC-069 S1 deleted it (2026-08-04); this is what actually enforces the
behaviour:

> **One option position per underlying** emerges from (i) the scanner's
> put-side skip of any symbol with existing positions, (ii) `select_batch`'s
> `duplicate_underlying` drop (one per batch, both pools), (iii) the calls share
> ledger (no double-covering). The invariant is per-*position*, not
> per-*contract* — a single position may hold up to 10 contracts.
>
> **All three enforcing legs are positions-based and blind to resting unfilled
> orders** — a submitted-but-unfilled put is invisible to the scanner skip, the
> batch dedup (across cycles), and the share ledger alike; the invariant is
> "one *position* per underlying, modulo the open-order window" (FC-009's
> standing territory).

---

## Decision-record outcomes written by these gates

`src/data/decision_record.py`, closed enum, covered-call leg only (the put leg's
legibility is events + replay tally + the committed audit script):

| Outcome | Reason | Written by |
|---|---|---|
| `not_eligible` | `insufficient_shares` | gate 5 |
| `blocked` | `floor_unresolved`, `floor_divergent` | gates 7, 6 |
| `blocked` | **`earnings_unknown`** | gate 4 |
| `blocked` | **`earnings_blackout`** | gate 9, when span empties the candidate set |
| `no_candidates` | `no_qualifying_strikes`, `quote_unavailable`, `opportunity_build_failed` | gates 8, 10 |
| `dropped` | `DROP_REASONS` + `execution_failed`, `already_positioned`, `previously_failed`, `not_selected` | stage 2/3 |
| `sold` | `""` | stage 3 |

Two earnings reasons, not one: a row saying "earnings blackout" when Finnhub was
down is a mislabel, and the closed vocabulary exists precisely so rows cannot
misfile.

---

## Alert coverage

| Gate | Alerted? | Policy |
|---|---|---|
| Cost-basis floor (6, 7) | yes | `deploy/monitoring/cost_basis_alert_policy.json` |
| Earnings **unknown** (2, 4) + `earnings_gate_unusable` | yes | `deploy/monitoring/earnings_gate_alert_policy.json` |
| Earnings **blackout** (2, 9) | no, by design — a blackout skip is the gate working |
| Everything else | no | — |

Runbook for all of them: `deploy/monitoring/drawdown_pause_alert.md`.

---

## Replay parity

The backtest replays the same scanner, so every stage-1 gate applies in
replays. The earnings gate is served there by `HistoricalEarningsCalendar`
(`src/backtesting/engine/historical_earnings.py`) reading the committed
point-in-time table `src/backtesting/data/earnings_dates.json` — **yfinance-
sourced, deliberately a different provider from the live service's Finnhub**,
which is what makes the audit script's cross-check meaningful.

Two things to know before reading a replay's earnings behaviour:

- **Config wins over injection** (DD-8): the scanner consults
  `config.earnings_enabled` regardless of what was injected, so
  `earnings.enabled: false` disables the gate in production and in replays
  alike. A what-if replay with the gate forced on or off is a config override,
  like every other knob.
- **The replay fails OPEN on table gaps, and reports them.** Returning
  "unknown" (block) for a symbol missing from the table would silently zero out
  that symbol's entire replay — a pessimistic measurement bias that corrupts
  the verdict. Instead the run report's data-quality block carries
  `earnings_symbols_missing_from_table` (absent symbols) and
  `earnings_symbols_past_table_horizon` (present symbols whose dates have all
  gone by — otherwise indistinguishable from genuinely clear). Non-empty means
  refresh the table before trusting the run's earnings behaviour.

Replay tally mappings (`src/backtesting/engine/rejections.py`) exist for both
`*_scan_skipped_earnings_blackout` events. The `*_earnings_unknown` pair is
deliberately **unmapped**: the historical calendar cannot emit it, and an entry
with no replay emitter reads as coverage while counting nothing.

---

## Detective layer

Preventive gates are not the only control. `tools/diagnostics/fc013_earnings_exposure_audit.py`
is the retrospective check: it joins every option sell-to-open fill in
`options_wheel.trades_from_activities` against the point-in-time table and
reports, per leg, `days_to_next_earnings` and `spans_earnings` for each fill —
one column each for the two shipped predicates. Re-run it after a trading week
to confirm zero fills violated either. It matters because the BigQuery log sink
is dead (FC-046) and structured events age out of Cloud Logging in 30 days.

It also detects the one accepted fail-open in the live service: a Finnhub 200
with an empty `earningsCalendar` is cached as known-clear, so a coverage
regression on Finnhub's side would read as *permanently clear* for that symbol.
Any fill inside a *table*-known window that Finnhub called clear shows up in the
audit. (Environment note from step 0: BigQuery→dataframe reads need the
`db-dtypes` package installed or they fail at result materialization.)

---

## Test anchors

| Gate | Test |
|---|---|
| Earnings, put leg | `tests/test_options_scanner.py::TestPutLegEarningsGate` |
| Earnings, call span | `tests/test_market_data.py::TestCallSpanFilter`, `tests/test_options_scanner.py::TestCallLegEarningsSpanGate` |
| Incident geometry | `tests/test_options_scanner.py::TestCallLegEarningsSpanGate::test_the_incident_geometry_is_blocked` |
| Fail-closed on unknown | `tests/test_options_scanner.py::TestEarningsGateFailureSemantics` |
| Env kill switch | `tests/test_options_scanner.py::TestEarningsGateFailureSemantics::test_env_override_disables_the_gate` |
| Tri-state service + cache | `tests/test_earnings_calendar.py` |
| Roller unchanged | `tests/test_call_roller.py::TestRollerEarningsGateUnchanged` |
| Replay parity + horizon | `tests/test_backtest_earnings.py` |
| Closed enum | `tests/test_decision_record.py` |
| Tally mappings | `tests/test_backtest_rejections.py` |
| Hermeticity | `tests/conftest.py::_no_finnhub`, `tests/test_earnings_calendar.py::TestHermeticity` |
