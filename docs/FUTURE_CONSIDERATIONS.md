# Future Considerations

A running list of things we want to **research and write a plan for** before coding. This file is a **precursor to todos**, not a todo list itself. Entries here are ideas and open questions — they become actionable only after a plan file is published in `docs/plans/`.

## Lifecycle

```
1. Consideration  →  added here, loosely scoped, questions open
2. Research        →  investigation, data gathering, comments, alternatives
3. Plan published  →  a file in docs/plans/<slug>.md with the agreed approach
4. Execution       →  code changes are made against the plan
5. Archived        →  moved to "Completed" below with a link to the plan + PR
```

**Rule of thumb:** nothing graduates from this file into code until step 3 is done. See `docs/CLAUDE.md` ("Plan-First Development") for the enforcement rule.

---

## Entry template

Copy this when adding a new consideration. Keep it short — detail belongs in the eventual plan file, not here.

```markdown
### FC-NNN: <short title>

**Status:** Consideration | Researching | Plan drafted | Plan published | Executing | Done
**Scope:** wheel | covered_call | shared  (REQUIRED since 2026-08-27 — which strategy the change serves; `shared` = reusable machinery both consume, e.g. writers, OCC parsers, ExecutionEngine, deploy/CI. Multi-scope work: name the primary, list the rest.)
**Size estimate:** S | M | L  (M/L require a plan file before code changes)
**Owner:** <who is thinking about this>
**Plan file:** `docs/plans/<slug>.md` (once published)

**Problem / opportunity:** 1–3 sentences on what prompted this.

**Open questions:**
- ...
- ...

**Links:** related evals (`PERFORMANCE_EVAL_CATALOG.md#EVAL-XXX`), issues, PRs, logs.
```

---

## Active Considerations

### FC-001: Symbol universe optimization

**Status:** Consideration
**Size estimate:** M
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** Several configured symbols never trade — they burn ~6k API calls/month and slow scans. Before removing them or adding replacements, we need a plan covering rollout, monitoring, and reversion.

> **Premise corrected 2026-07-18.** The original entry listed 8 never-traded symbols including AAPL and MSFT. **Both now trade**: AAPL 11 trades since 2026-04-28 (assigned at $305 on 6/13), MSFT 6 trades since 2026-06-16 (assigned at $382.50 on 6/23) — they were below the price filter when this entry was written and have since come into range. The actual dead weight is **6 symbols: QQQ, SPY, F, PFE, KMI, VZ** (zero trades ever). FC-032's coverage work separately found F/PFE/KMI/VZ **structurally** untradeable (0 usable days — the $0.50 premium floor, not a data gap), which is a stronger argument for removal than "hasn't traded yet". Note SPY now appears in `stock_history_from_alpaca` as the FC-031 benchmark/trading-calendar symbol — that is ingest-only and does not make it a trading candidate.

**Open questions:**
- Remove the 6 dead-weight symbols in one change or stage it? (F/PFE/KMI/VZ have a structural justification; QQQ/SPY are price-filter exclusions.)
- Which replacement candidates (META, TSLA, COIN, PLTR) clear our filters in a backtest?
- Do we raise `max_stock_price` from $400 to bring QQQ/SPY into range? (AAPL/MSFT resolved themselves without a config change.)
- How do we validate the change hasn't reduced premium throughput?
- Should this merge into FC-032's wheel-fitness evaluation rather than stand alone? The backtesting overhaul is building exactly the machinery to answer "which symbols deserve capital".

**Links:** `PERFORMANCE_EVAL_CATALOG.md` EVAL-010; FC-032 (wheel-fitness evaluation — overlapping scope).

---

### FC-002: AMD gap-risk filter re-tuning

**Status:** Consideration
**Size estimate:** M
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** AMD is our top premium generator but is gap-filtered on 47% of scans (262 of 553). Filter thresholds may be over-conservative for this symbol. A plan needs to weigh actual realized overnight gaps against the threshold and consider per-symbol tuning vs. a global change.

**Updated 2026-07-18 (FC-032 engine walkthrough):** the binding constraint is broader than AMD and broader than gap *frequency*. NVDA was blocked on 18 of 20 decision days in Jun–Jul 2026 by the **40% realized-vol cap** (`max_historical_vol`) — its 30-day vol sat at 40–42%, so trading was noise around one hard line; the single day vol dipped to ~40% it traded. Validated against live: production also sold zero NVDA puts that window. Structural critique: the filter double-counts risk that delta-band strike selection already prices (IV rises → strikes auto-walk OTM → richer premium), is a binary cliff with no graduated response, and embargoes the top premium generators exactly when compensation is highest. **The A/B study to answer this empirically is Track B1 of `docs/plans/fc-042.md`** (filter off / vol-cap sweep / vol-relative percentile / half-size-instead-of-ban); any threshold change then gates on this FC's own plan + two reviewers. Note stage-4's execution gap check is dead (FC-036), so this filter is currently the *only* functioning gap control — do not simply disable it.

**Updated 2026-07-29 — B1 study published, and it reframes this entry. See
`docs/investigations/fc-002-gap-filter-ab.md`.** Four corrections, in order of consequence:

1. **The filter is not wired into the live trading path and has never gated a live trade**
   (filed as **FC-048**). Every block rate quoted above — this entry's and FC-036's —
   describes the *backtest engine*. Whether to gate at all is now the prior question;
   tuning a threshold nothing reads is a no-op.
2. **"47% of scans" is stale.** Reconstructed over the same bars, AMD is blocked on **94.4%**
   of sessions 2024-02→2026-07 and **100% of the 201 sessions in the live-fills window** —
   all by the gap-**frequency** leg, not the vol cap. AMD's median 34-session vol there is
   0.735.
3. **The vol-cap diagnosis is half right.** Confirmed for NVDA in Jun–Jul 2026 (35 of 38
   sessions blocked, all by the vol cap, vol 0.387–0.459 against a 0.400 line). But over the
   full window NVDA's two legs bind almost equally (52.9% vol-alone vs 51.3% frequency-alone),
   and on AMD frequency is the binding leg. A vol-only sweep would leave AMD untradeable.
4. **`vol_lookback_days: 252` is dead config** — nothing reads it. Both legs are computed
   over `gap_lookback_days + 20` = 50 **calendar** days ≈ 34 sessions.

**On the evidence, the filter is anti-selective in this window:** of 327 real entries, the
123 it would have blocked earned **$70.66/entry** against **$55.94** for the 204 it would
have allowed ($8,691 forgone, 43% of net realized); on 2,329 synthetic daily entries blocked
days out-earn allowed days on 4 of 4 symbols, both IV models, ±20% premium. Caveats stated
prominently in the study: one bull-market regime, no vol shock, and — per **FC-048** — the
study's *engine* arms are put-only and are therefore supporting evidence at most. The real-fills
and overlay layers, which carry the conclusion, are unaffected by FC-048. **Recommendation:
do not re-tune thresholds; resolve FC-049 first.** The one arm worth carrying forward is
graduated response (delta band → [0.10, 0.15] instead of a ban), **contingent on a re-run
after FC-048 is fixed** — its support is engine-only, and its "half size" variant is
impossible at `put_seller.py`'s hard-coded `contracts = 1`.

**Open questions:**
- ~~What's the distribution of AMD's actual overnight gaps vs the current filter?~~ Answered.
- Do we support per-symbol gap thresholds, or keep one global? *Still open — and now
  secondary to FC-048.*
- How do we avoid over-fitting to AMD's recent history? *Partly addressed: the study's
  pre-registration requires any proposal to hold its sign on 3 of 4 symbols.*
- **New:** should the gap-frequency leg exist at all? A >2% overnight move on a 40-vol name
  is a typical day, not a tail, and that leg is what makes AMD permanently untradeable.

**Links:** `docs/investigations/fc-002-gap-filter-ab.md`, `tools/diagnostics/fc002_gap_filter_ab.py`,
FC-048, FC-036, `PERFORMANCE_EVAL_CATALOG.md` EVAL-010, `docs/analysis/AMD_GAP_RISK_ANALYSIS_2025.md`.

---

### FC-003: DTE target optimization (7 → 2–3?)

**Status:** Consideration
**Size estimate:** L
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** Current 7 DTE target may be suboptimal. Shorter DTE has higher per-trade ROI but potentially higher assignment risk. A change here touches strategy config, risk thresholds, profit-target DTE bands, and scan cadence.

**Open questions:**
- Does 2–3 DTE net ROI (after assignment losses) actually beat 7 DTE?
- Do profit-target bands need to be re-tuned for the new DTE?
- Do we ramp via A/B (some symbols at 3 DTE, others at 7) or flip the whole universe?

**Links:** `PERFORMANCE_EVAL_CATALOG.md` EVAL-004, EVAL-002.

---

### FC-004: Autonomous eval-driven parameter tuning

**Status:** Consideration
**Size estimate:** L
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** `PERFORMANCE_EVAL_CATALOG.md` describes a scheduled runner + results storage + threshold checker + config proposer + human-in-the-loop gate. This is a multi-component build that needs a plan before any code lands.

**Open questions:**
- What's the minimum viable first eval to automate (EVAL-011 share-commitment verification is the highest-safety candidate)?
- Where does the scheduled runner live (Cloud Scheduler + Cloud Run job, or inline with the strategy?)
- What schema for the `eval_results` audit table?
- What are the hard per-parameter safety bounds?

**Links:** `PERFORMANCE_EVAL_CATALOG.md` "Future Automation Architecture" section.

---

### FC-005: Per-symbol strategy parameters

**Status:** Consideration
**Size estimate:** L
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** Low-priced symbols (F, PFE, KMI, VZ) fail the global `min_put_premium` because their option premiums are structurally smaller. Either remove them or support per-symbol DTE/premium overrides. Touches config schema, filter code, docs, tests.

**Open questions:**
- Do we actually want these low-priced symbols, or is it simpler to drop them (see FC-001)?
- Config shape: per-symbol overrides map, or a tier system?
- How does this interact with the gap-risk controls (FC-002)?

**Links:** `PERFORMANCE_EVAL_CATALOG.md` EVAL-007, EVAL-010.

---

### FC-009: Duplicate early_close_executed (revised 2026-04-24 post-FC-012)

**Status:** Consideration (scope clarified, not narrowed)
**Size estimate:** M
**Owner:** unassigned
**Plan file:** not yet

**Original problem:** The bot logs `early_close_executed` multiple times (4-10 duplicates) for the same position when the close order doesn't fill quickly. The `_closed_today` dedup set in `cloud_run_server.py` is in-memory and resets on Cloud Run cold starts. Each monitor invocation on a fresh instance re-evaluates the position, finds it still meets the close criteria, and places another close order.

**What FC-012 changed:** the dashboard no longer reads from the structlog-sourced `options_wheel.trades` table. Dashboard counts are from `trades_with_outcomes` (Alpaca FILLs, deduped on `activity_id`). So duplicate log entries no longer corrupt dashboard numbers. **But the bug is about duplicate *orders*, not logs — the dashboard migration doesn't fix the underlying mechanism.**

**What FC-010 did NOT change:** FC-010 only disabled the call **stop-loss** branch. Call profit-target early-closes and put profit-target early-closes still flow through the same shared code path (`deploy/cloud_run_server.py:694-776`) with the same in-memory dedup. FC-010 reduced the *frequency* of vulnerable events but not the *mechanism*.

**Paths currently exposed to the bug:**
- Put profit-target early-closes (never touched by prior fixes)
- Call profit-target early-closes (stop-loss portion was silenced by FC-010, profit-target portion unchanged)

**Why duplicate orders can actually happen:**
1. Monitor fires → `should_close_*_early()` returns True → check `_closed_today` (empty on cold start) → place buy-to-close order → add symbol to `_closed_today`.
2. Before the order fills, Cloud Run scales to zero.
3. Next monitor fires on a new instance → `_closed_today` is empty again → position still exists at Alpaca (short option) → `should_close_*_early()` still returns True (position still meets profit-target threshold based on mark) → second close order placed.
4. Both orders sit in Alpaca's queue. Depending on timing, one or both may fill.

**Remaining work:**
1. **Verify** via BQ query against `trades_from_activities`:
   ```sql
   SELECT symbol, COUNT(*) AS close_fills, COUNT(DISTINCT order_id) AS close_orders
   FROM `options_wheel.trades_from_activities`
   WHERE side = 'buy_to_close'
     AND transaction_time >= '2026-04-01'
   GROUP BY symbol
   HAVING COUNT(DISTINCT order_id) > 1
   ```
   If the query returns rows where `close_orders > 1` on the same symbol on the same day → real duplicate orders confirmed.
2. **Fix** the dedup so it survives cold starts. Two viable options:
   - Persist `_closed_today` to GCS alongside the existing wheel state.
   - Before placing a close order, check Alpaca for open buy-to-close orders on the same option symbol. Skip if one is already pending.

The second option is more robust — it handles the "Cloud Run instance crashed mid-cycle" case that even GCS persistence wouldn't catch cleanly. Small M-sized change.

**Links:** FC-010, FC-012. Relevant code: `deploy/cloud_run_server.py:694-776`, `src/strategy/put_seller.py:523` (`should_close_put_early`), `src/strategy/call_seller.py:536` (`should_close_call_early`).

---

### FC-013: Gate health audit & earnings blackout symmetry

**Status:** Plan published
**Size estimate:** M
**Owner:** Claude
**Plan file:** `docs/plans/fc-013.md`

**Problem / opportunity:** A GOOG put executed 2026-04-28 the day before Google's 2026-04-29 earnings. The FC-007 `EarningsCalendarService` exists and works, but is only wired into `CallRoller.should_roll()` — never into `PutSeller.find_put_opportunity()` or `CallSeller.evaluate_covered_call_opportunity()`. The FC-007 execution note acknowledged this deferral. A holistic gate audit also surfaced that `config.earnings_blackout_days`, `config.earnings_avoidance_days`, and `RiskManager.validate_new_position()` are all defined but never read/invoked. FC-013 wires earnings into both sellers, flips fail-open → fail-closed, removes the dead `earnings_avoidance_days` knob, and publishes `docs/gates.md` as a single source of truth for all gates.

**Open questions:** see plan file.

**Links:** FC-006, FC-007, `docs/CLAUDE.md` Wheel Strategy Symmetry Principle. Sibling FCs spun off from the audit: FC-014, FC-015, FC-016.

---

### FC-016: Test coverage for orchestration & account-level gates

**Status:** Consideration
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** The Phase-1 gate audit for FC-013 found ~0 unit-test coverage for orchestration STAGES 3/4/5/6/9 in `wheel_engine._find_new_opportunities` (max stocks evaluated per cycle, execution gap check, wheel state phase guard, existing position blocking, max new positions per cycle) and account-level gates (`_can_open_new_positions`: max total positions, minimum buying power). These gates run on every cycle but no test asserts they fire when their conditions are met. Add unit-test coverage for each gate's positive and negative paths.

**Open questions:**
- Test directly against `WheelEngine` with mocked Alpaca, or extract the gates into pure helpers first?
- Does coverage of the Cloud-Run-server market-open / strategy-lock gates belong here too, or in a separate FC?

**Links:** FC-013 Phase-1 audit, `wheel_engine.py:220-400`.

---

### FC-020: FIFO cycle pairing in wheel_cycles_from_activities

**Status:** Plan published
**Size estimate:** S-M
**Owner:** Claude
**Plan file:** `docs/plans/fc-020.md`

**Problem / opportunity:** After FC-019 landed, the per-symbol scorecard reconciles correctly to actual cash flow (sum of Total P&L ~= account growth, modulo small Alpaca-side data anomalies). But the per-cycle drilldown still has a pairing bug: when multiple put assignments happen on the same underlying before any are called away (overlapping share lots), the view pairs each assigned put to the earliest subsequent called_away, so two puts can both pair to the same called_away. The result: OPTRD events get summed into the wrong cycle window, inflating one cycle's `capital_gain` and treating another cycle as still open.

**Concrete example (AMD):**
- 2025-11-22: put assigned at $230 (Lot A starts)
- 2025-11-29: called away at $192.50 (Lot A ends)
- 2026-01-10: put assigned at $212.50 (Lot B starts)
- 2026-01-31: put assigned at $245 (Lot C starts — concurrent with Lot B)
- 2026-04-17: called away at $252.50 (one lot ends)

The view shows:
- Cycle 1 (correct): put $230 → call $192.50, cap_gain -$3,750
- Cycle 2 (WRONG): put $212.50 → call $252.50, cap_gain -$20,500 — sums OPTRDs from the second assignment too
- Cycle 3 (WRONG): put $245 → call $252.50 — pairs to same called_away as Cycle 2

**Fix:** FIFO pairing. For each underlying:
1. Sort all OPTRD-buy events by `transaction_time` ascending → assigned-put queue.
2. Sort all OPTRD-sell events by `transaction_time` ascending → called-away queue.
3. Walk the events in time order. Each OPTRD-buy opens a lot; each OPTRD-sell closes the oldest open lot.
4. Each lot pair = one cycle. `capital_gain = sell_price − buy_price` × shares (using actual OPTRD prices, not put_strike/call_strike).
5. Lots without a matching sell remain open.

This requires a stateful walk over events, which BigQuery can express via `ARRAY_AGG` + `OFFSET` tricks or a JavaScript UDF. Alternative: do the walk in Python in the backend's `BigQueryService.get_wheel_cycles_for_symbol` method.

**Open questions:**
- SQL-only or Python-side walk? Python is simpler to write but harder to test against the view abstraction; SQL keeps the view as source of truth.
- How to handle the AMD-style data anomaly where OPTRDs net to non-zero shares but Alpaca reports zero? Surface as an "unaccounted_shares_loss" column in the per-symbol scorecard, computed as `current_shares (from OPTRD net) − live_alpaca_shares`?

**Links:** FC-018 (dashboard), FC-019 (the OPTRD ingest that exposed this).

---

### FC-017: Option chain snapshots at decision points (for retrospective decision-quality analysis)

**Status:** Consideration
**Size estimate:** M
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** The dashboard rebuild (proposed FC-018) would benefit from retrospective decision-quality analysis: "could I have rolled to a higher-strike call instead of closing?", "was there a same-DTE put at a similar delta with better premium I should have picked instead?", "how does my close-time strike compare to the strike chain that existed at that moment?". These questions are not retrospectively computable from current data — they require a snapshot of the option chain at decision time. EOD prices are not enough; we need the strikes/premiums that existed *when the close or open decision was made*.

**Scope:** capture option chain snapshots at three decision points:
1. **Close decision** — when `should_close_*_early()` returns True. Snapshot the chain near the position being closed (same expiry, ±5 strikes), plus next-week's chain (±5 strikes near current price) so a "should I have rolled?" counterfactual is possible.
2. **Open decision** — when an opportunity is selected for execution. Snapshot the chain wider for the selected symbol (±10 strikes, all available expirations within target DTE band) so a "was there a better strike?" counterfactual is possible.
3. **Skip decision** — when the scanner had a candidate but skipped it for a gate reason. Just the candidate's chain row (so we can later validate "was the gate right?").

**Storage:** new BQ table `options_wheel.option_chain_snapshots` partitioned by `snapshot_date`, clustered by `underlying`. Schema captures `snapshot_id`, `decision_type`, `decision_id` (FK to the trade/scan/close event), `underlying`, `snapshot_time`, `chain` (REPEATED RECORD with strike, expiration, type, bid, ask, mid, delta, theta, iv, volume, oi). Append-only, idempotent by `snapshot_id`.

**Open questions:**
- Storage cost — option chains can be 50-200 rows each. If we snapshot every scan + every close, that's potentially 5-10k chain rows/day. At BQ pricing this is trivial ($X/month) but worth a back-of-envelope check.
- Alpaca rate limits — pulling chains adds API load. Are we already pulling them for these decisions and just not persisting? (Yes for opens — `find_suitable_*` already calls the chain. Closes likely don't pull a wider chain — would be net-new API calls.)
- Decision-id schema — how does a close-decision row in this table link back to the actual close FILL? Use `order_id` of the buy-to-close as the natural FK.
- Retention — do we want this forever or roll off after 1 year?

**Why deferred from FC-018 (dashboard rebuild):** counterfactual analysis is high-value but expensive to build correctly. FC-018 ships v1 of the new dashboard with retrospective views that *don't* require chain snapshots (closed-trade % of max profit, vs-buy-and-hold per symbol, ACB walk). FC-017 unlocks a follow-up dashboard iteration that adds the harder counterfactual surfaces.

**Links:** FC-018 (dashboard rebuild — depends on this for full decision-quality views); `src/strategy/put_seller.py:should_close_put_early`, `src/strategy/call_seller.py:should_close_call_early`, `src/api/market_data.py:find_suitable_puts/find_suitable_calls`.

---

### FC-033: Drawdown-pause escalation — permit a below-cost-basis call after an extended pause

**Status:** **Deferred** (operator decision 2026-07-31 — deliberately *not* Closed, for lineage)
**Size estimate:** M
**Owner:** zeshan (revisit is an operator decision only, never automatic)
**Plan file:** not yet

> **Deferral record (2026-07-31, FC-065 plan review OQ-2):** policy is now **hold uncovered below `avg_entry_price` until recovery — mechanical below-floor call writing is banned permanently** (see `docs/plans/fc-065.md`, "The floor definition, decided"). This FC's escalation idea is *deferred, not rejected*: the trader plan-reviewer argued the supporting evidence for "never, ever" is n=3 in a single V-shaped regime where every name later recovered, hold-until-recovery has no time bound, and a structurally impaired name could freeze $20–40k indefinitely. Reopen trigger: `uncovered_days` data from FC-065 Phase 4's decision record showing a pause long/costly enough that the operator wants to reconsider. Note the current arithmetic favors holding: expected loss per below-floor write ≈ 0.22 × $964 ≈ −$212 vs ~$130/week premium foregone. Also note the *pause mechanism itself* was never built (FC-065 OQ-3: floor-only gating; "paused" exists only as a decision-record label), so any revival here would gate on `uncovered_days`, not on a pause state.

**Problem / opportunity:** Split out of FC-030 (2026-07-18). When a symbol sits paused for a long stretch, the shares are dead capital — AMZN's 62-day pause cost an estimated $1,500–3,000 in foregone premium. One candidate response: after N days paused (14? 21?), allow a single far-OTM call whose strike is *below* the assignment-strike floor, harvesting some premium while accepting a capped share loss if called away.

**Why this is not FC-030:** it deliberately reverses part of FC-029 R2's hard cost-basis floor — the guard built specifically because the eroding floor caused the $9k of loss cycles. That makes it a strategy change (two-reviewer, high-stakes calibration per `~/CLAUDE.md`), not observability.

**Prerequisite:** empirical pause-duration data. FC-030's alerting starts collecting it; AMZN and GOOGL entered pauses 2026-07-17. Decide only after seeing whether pauses typically resolve in days (escalation unnecessary) or drag for weeks (escalation valuable).

**Open questions:**
- Day threshold to permit escalation, and how far OTM must the strike be?
- Operator-approval-in-the-loop, or automatic once configured?
- Does a called-away-below-cost outcome here beat continued waiting, measured over the observed pause distribution?
- Interaction with the FC-006 rolling engine (which has fired 0 times)?

**Links:** FC-029 R2/R3 (hard floor + pause), FC-030 (alerting; source of the duration data), `docs/investigations/strategy-review-2026-05-07.md` §R3.

---

### FC-011: Support non-Friday option expirations (daily/weekly rolling expirations)

**Status:** Consideration
**Size estimate:** L
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** Some high-volume symbols (e.g., GOOGL, AMZN, SPY, QQQ) now have options expiring every trading day, not just Fridays. The current system assumes Friday-only expirations in multiple places:

1. **FC-006 rolling engine** — hardcoded Friday guard (`weekday()==4`) in both the `/roll` endpoint and Cloud Scheduler (`30 15 * * 5`). Positions expiring on a Wednesday won't be evaluated for rolling.
2. **DTE bands** — the 7→0 bands assume a Monday-sell, Friday-expire cadence. A position sold Monday with a Wednesday expiry has DTE=2 at open, hitting different (later) bands than intended.
3. **`call_target_dte: 7` / `put_target_dte: 7`** — assumes next-Friday expiry. With daily expirations available, shorter DTE targets (2-3 days) become viable, potentially improving theta capture per calendar day.
4. **Strike selection** — `find_suitable_calls/puts` filters by `dte <= target_dte` which works, but may miss better opportunities at non-Friday expirations.

**Open questions:**
- Which symbols in our universe have daily expirations vs Friday-only? Need to audit Alpaca's option chain data.
- Should the rolling engine run daily (not just Fridays) for symbols with daily expirations?
- Do DTE bands need to be reparameterized for shorter-DTE strategies (see FC-003)?
- Should we support mixed strategies — daily expirations for some symbols, weekly for others?
- How does this interact with FC-003 (DTE target optimization from 7 to 2-3)?

**Links:** FC-003 (DTE target optimization), FC-006 (rolling engine)

---

### FC-032: Backtesting engine overhaul — symbol wheel-fitness evaluation

**Status:** Phases 0–4 **Done** (PR #35, merged 2026-07-18) — evaluate mode ships; **Phase 5 (screen mode) still open**, tracked in the plan's Rollout section
**Size estimate:** L
**Owner:** zeshan
**Plan file:** `docs/plans/fc-032.md`

**Problem / opportunity:** The original backtesting engine has never produced a single trade in any saved run (all `backtest_results/` artifacts show 0 trades). Root causes: it requests Alpaca option bars with `feed='iex'` (options come from OPRA) so chains are always empty; the covered-call scan crashes on a nonexistent `portfolio.positions` attribute; `_calculate_summary_metrics` is called but never defined; and it reimplements — and diverges from — the live strategy rules instead of replaying them. Separately, `tools/backtesting/scheduled_backtest.py` emits fabricated performance numbers when its (nonexistent-module) imports fail. We want a rebuilt engine whose purpose is **symbol wheel-fitness evaluation**: given a symbol, replay our live strategy rules over a 1–2 year lookback with real historical options data and report how the full wheel (CSP → assignment → covered calls → called away) would have performed — both on-demand for candidate symbols and on a cadence to flag risky symbols for demotion.

**Open questions:**
- Data budget: free Alpaca bars (Feb 2024+, no bid/ask/greeks) vs a paid vendor (ThetaData/ORATS) — decide after the Phase 1 data-quality report?
- Reuse live strategy components via an adapter vs standalone reimplementation with parity tests?
- One decision point per simulated day (EOD) vs mimicking the live 9/12/3 ET schedule?
- Is the scheduled screening/demotion job part of this FC or a follow-up?

**Links:** FC-017 (chain snapshots at decision points — same storage need, forward-looking), FC-001 (symbol universe optimization — this engine answers its backtest question), `docs/plans/fc-032.md`.

---

### FC-037: Synthetic pre-2024 premium extension for backtests

**Status:** Consideration — explicitly deferred out of FC-032 v1
**Size estimate:** M
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** Alpaca's historical options data starts 2024-02-01, so FC-032 can only backtest ~2.4 years — a single, calm vol regime. Fitness verdicts risk overfitting to it. Extending backward requires *synthesising* option premiums from underlying price history, which is dangerous: Black-Scholes with trailing realized vol badly underprices seller premiums (IV exceeds RV ~85% of days; VIX/RV ≈ 1.18; the variance risk premium concentrates at short maturities — worst exactly for our weeklies; OTM put skew adds 3–5 vol points). Naive BS-with-RV understates a 30-delta weekly put premium by ~50–60%.

If ever built, the recipe and its error bars are already written down in the FC-032 plan appendix: Yang-Zhang 21d realized vol × a per-symbol IV/HV factor calibrated from our real 2024+ chains, plus a skew bump; binomial with discrete dividends for ITM/dividend cases. Synthetic-regime results must be **labeled as such and never silently mixed** with real-data results.

**Open questions:**
- Is a wider, lower-fidelity window actually more informative than a narrow, high-fidelity one for a *fitness* verdict (which is comparative vs buy-and-hold, not absolute)?
- Cheaper alternative: buy ThetaData/ORATS history (2012+/2007+, real quotes) — $25–99/mo or $599 one-time. That buys real data instead of modeled data for less engineering risk.
- Would a multi-start-date ensemble on the real 2024+ window capture most of the timing-luck benefit at a fraction of the cost?

**Links:** `docs/plans/fc-032.md` (non-goals + research appendix), FC-032.

---

### FC-034: Premium floor is not scale-free — four universe symbols can never trade

**Status:** Consideration
**Size estimate:** M (changes live trading behavior → requires a plan file)
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** `min_put_premium: 0.50` is a **fixed dollar** floor applied identically to a $12 stock and a $600 one. The FC-032 coverage gate measured every decision day from 2024-02-01 to 2026-07-09 and found **F, PFE and KMI have zero usable decision days, and VZ has two** — a 0.10–0.20 delta weekly put on a low-priced underlying is worth pennies and can never clear $0.50. Independently confirmed against production: across 588 live Alpaca `FILL` activities and `options_wheel.trades_from_activities`, the bot has **never sold a put on any of the four**. They occupy 4 of 14 universe slots, consume screening API budget, and are structurally incapable of generating a trade.

This is not a data problem (bar coverage was 122/122 for all 14 symbols) and not a bug — the filter is doing exactly what it says. The question is whether a dollar-denominated floor is the right *threshold shape*.

**Open questions:**
- Re-express the floor as a fraction of strike (e.g. ≥ 0.5% of strike), as annualized return on collateral, or keep a dollar floor with a per-symbol override?
- A return-on-collateral floor is the economically meaningful one (it's what the wheel actually earns) — but it changes which contracts pass on *every* symbol, not just the cheap ones. Needs a backtest before it ships. FC-032's engine is exactly the tool; sequence this after FC-032 Phase 4.
- Or simply demote F/PFE/KMI/VZ from the universe and leave the floor alone. Cheapest fix, but leaves the threshold mis-shaped for any future low-priced candidate.
- Does `min_call_premium: 0.30` have the same defect on the call side? (Almost certainly — same shape, and calls are only sold post-assignment so the blast radius differs.)

**A/B study complete (2026-07-29) — recommendation is DEMOTE.** `docs/investigations/fc-034-premium-floor-ab.md`. Three arms (flat $0.50 / 0.40%-of-strike / 8% annualized return-on-collateral) over 275 decision days plus a read-only join against the 330 real sell-to-open fills. The pre-registered rules return DEMOTE: the cohort's *richest* in-band put pays a median $0.03–$0.08 a share (controls: $0.51–$0.53), so no floor admits them without admitting $3-per-contract trades; a 0.40%-of-strike floor would have retroactively blocked **47 of 330 real fills and $2,235 of realized option P&L**; and an 8%-annualized floor only helps the cohort by taking AAPL from 50% to 94% of usable days. The threshold shape is not the defect — the premium is not there. **Open question 4 (the call-side floor) is still open**: no covered call executed in any replay (see the study's New findings §1). Change still requires its own plan + two reviewers.

**Links:** `docs/investigations/fc-034-premium-floor-ab.md` (the A/B study), `docs/investigations/fc-032-coverage-gate.md` (the measurement + production validation), FC-032, FC-001 (symbol universe optimization). **A/B study = Track B2 of `docs/plans/fc-042.md`.**

---

### FC-035: delete the dead `poll_order_statuses` path and the `order_statuses` table

**Status:** Done — code merged (PR #54, `ceaae16`); **`bq rm` still outstanding (owner)**
**Size estimate:** M (multi-file deletion in live paths + a BigQuery table drop)
**Owner:** zeshan + Claude
**Plan file:** `docs/plans/fc-035.md`

**Re-scoped 2026-07-29:** originally "fix a one-line `NameError`." Investigation showed the whole path should be **deleted, not revived** — it has never executed, nothing reads its output, and FC-012 already scheduled the exact `bq rm` this performs. Step 0 gate verified live: **0 rows**, **no views** in `options_wheel` or `options_wheel_logs` reference the table, **no scheduled queries**. PR #47 (which revived it) is closed unmerged. Original problem statement retained below for history.

**Problem / opportunity:** `src/strategy/wheel_engine.py` (~line 698, in `poll_order_statuses`) calls:

```python
closed_orders = self.alpaca.trading_client.get_orders(
    filter=alpaca.trading.requests.GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=100))
```

The module `alpaca` is **never imported** in that file — only `QueryOrderStatus`, in a local import on the line above. So this raises `NameError: name 'alpaca' is not defined` on every invocation. The surrounding `except Exception` swallows it and logs a debug line, "Could not fetch closed orders directly, using all_orders". Found by pyflakes while seaming the clock for FC-032; confirmed pre-existing (predates the FC-032 branch).

**Consequence:** the closed-orders path has never once executed. Order-status polling sees only whatever `self.alpaca.get_orders()` returns (the SDK default), so `order_filled` / `order_expired` events may be under-reported. Worth checking whether the dashboard's order-status accuracy has been quietly leaning on the activities ingestor instead.

**Open questions:**
- Fix is one import (`from alpaca.trading.requests import GetOrdersRequest`). But this *enables* a code path that has never run in production — what does it start doing, and does it double-log `order_filled` events against the activities-derived ones?
- Does `get_orders()` (no filter) already return closed orders, making the whole block dead code worth deleting instead?
- Add a test that would have caught this: pyflakes/flake8 `undefined name` in CI. There are other pre-existing lint findings; a clean-then-gate pass is probably warranted.

**Links:** FC-032 (found during the Phase 3 clock seam), `src/strategy/wheel_engine.py`.

---

### FC-036: Stage-4 execution gap check is dead in production

**Status:** Done — fix merged unarmed (PR #52, `44159d5`); **arming rejected on evidence**, see `docs/investigations/fc-036-gap-gate-study.md`
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not needed (single-file fix) — but it changes runtime behavior, so branch + PR

**Problem / opportunity:** Found by the FC-032 backtest, which reproduced it faithfully — the replay is behaving correctly; **production is not**.

`GapDetector._get_previous_close` (`src/risk/gap_detector.py`) does `df[df.index < current_time]` and takes `.iloc[-1]`. Alpaca stamps daily stock bars at **midnight ET** (04:00 UTC under EDT, 05:00 under EST), far before any of our decision times (9:35am ET = 13:35 UTC). So today's own bar satisfies the filter and "previous close" returns **today's close**.

Verified against the live client on 2026-07-17: at a simulated 9:35am ET decision, `_get_previous_close` returned **202.81** while the latest bar in the frame was also **202.81** (dated the same day).

**Consequence — note the precise failure, it is not "the gap reads zero".** Alpaca returns a *partial* bar for the current session, and `can_execute_trade` compares a real-time IEX quote against it, so what the gate actually measures is the **~20-minute pre-market drift** (the partial bar's last print ~9:15 ET vs the 9:35 quote), not the overnight gap. Observed drifts ran −0.967% to +1.212% — **nonzero**, so anyone spot-checking `gap_percent` in the logs would wrongly conclude the gate is working.

Today's bar was returned in **10/10 live trials** across NVDA and AAPL. The real overnight gaps on five sampled NVDA days were −2.295%, −1.096%, +0.076%, +2.295%, −1.147%; against `execution_gap_threshold = 1.5`, **3 of 5 should have blocked execution and none did.** The gate can also fire *spuriously* on a violent 9:15→9:35 move unrelated to any overnight gap.

Note this is *not* true of the Stage-2 gap-risk analysis (`_detect_current_gap`, gap_detector.py:214), which compares `df_dates < target_date` — a **date** comparison rather than a timestamp one. That is the fix pattern, and it means one of our two gap controls works while the other does not, with nothing surfacing the disagreement.

**Open questions:**
- Fix by excluding the current session's bar (`df.index.date < current_time.date()`), or by requesting bars with an explicit end of yesterday?
- Enabling a gate that has never fired changes live behavior. How much would it have blocked historically? The FC-032 engine can now answer this directly — run it with the gate fixed and compare.
- Does the same 04:00-stamp assumption leak into any other windowed statistic?

**Links:** FC-032 (`docs/plans/fc-032.md`), found during its two-reviewer pass; `docs/investigations/fc-032-parity-check.md`. **Fix + would-have-blocked study = Track E1 of `docs/plans/fc-042.md`.**

---

### FC-040: Unit tests make live BigQuery calls against production data — ALREADY FIXED, entry withdrawn

**Status:** Withdrawn 2026-07-18 — the bug was real but had already been fixed on `main` before this entry was filed.

**What happened.** This entry was filed on 2026-07-18 during the FC-038 review, claiming the bug existed on `main`. It did not. `tests/conftest.py` on `main` already carries an autouse `_no_production_bigquery` fixture that stubs `CallSeller._lookup_last_opasn_put_strike`, with a `@pytest.mark.real_bq_lookup` escape hatch for the one test that genuinely exercises the fallback.

That fixture's own docstring documents the identical finding — same mechanism, same AAPL `$305` value, same drawdown-pause symptom — so the diagnosis here was a rediscovery, not a new bug.

**Why the false positive.** The verification was run in the FC-038 worktree, which is based on `main` from before PR #35 merged and therefore predates the fixture. The lesson is procedural and worth keeping: **verify a claimed `main` bug against `main`, not against a feature branch's base.** Every other finding in that review pass was re-checked against `main` afterward; FC-039 and FC-041 survived, this one did not.

**Links:** `tests/conftest.py` (`_no_production_bigquery`); `docs/investigations/fc-038-plan-review-2026-07-18.md`.

---

### FC-041: Naked-call share guard misparses OCC symbols and can fail open

**Status:** Consideration
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not needed (single-file fix), but it changes runtime behavior of a risk control, so branch + PR

**Problem / opportunity:** `ExecutionEngine`'s committed-share accounting (`src/strategy/execution_engine.py:333` (on `main`)) identifies short calls with a hand-rolled parser:

```python
for ch in opt_sym:
    if ch.isdigit(): break
    opt_underlying += ch
if opt_underlying == underlying and 'C' in opt_sym:
    committed_shares += abs(int(float(pos.get('qty', 0)))) * 100
```

Two defects:

1. **`'C' in opt_sym` is a substring test over the whole OCC symbol, including the ticker.** `CRWD250718P00150000` contains a `C`, so a short *put* on any C-containing ticker is counted as committing 100 shares to calls — over-blocking legitimate call sales. The current wheel universe (AAPL, MSFT, GOOGL, AMZN, NVDA, AMD, QQQ, SPY, IWM, UNH, F, PFE, KMI, VZ) contains no `C` ticker, so the wheel is safe **by luck, not by design**. Adding CSCO, CVX, KO-adjacent names, or C itself would trigger it.
2. **The digit-break underlying parser breaks on class shares.** `BRK.B` has position symbol `BRK.B` but OCC symbol `BRKB250718C...`, which parses to `BRKB` ≠ `BRK.B`. No match → `committed_shares = 0` → `available_shares = owned` → **the guard fails open and the bot writes calls against already-committed shares. That is a naked call.**

`src/utils/option_symbols.py` already exists and should be used instead; the option type is at a fixed offset in the OCC layout, not a substring.

**Consequence.** Defect (1) is currently latent and costs premium when triggered. Defect (2) is a genuine naked-call path — an uncovered short call has unbounded upside risk. Both become materially more likely under the covered-call **extensibility** proposal (a concurrent 07-18 project formerly numbered FC-038; must refile — see the FC-038 numbering note), which introduces a covered-call account with **no configured symbol universe by design**, where the operator buys arbitrary tickers through the Alpaca UI. That proposal's Phase 2 explicitly relies on this guard as the primitive for committed-share accounting. (Today's FC-038 — two-pool selection — also leans on committed-share accounting, but only for the configured universe, and reuses the FC-052-fixed canonical parsers.)

**Open questions:**
- Replace the parser with `src/utils/option_symbols.py`, or is that module's coverage incomplete for class-share tickers too? Check before assuming.
- Add a hard pre-submit assertion that `short_calls × 100 ≤ shares_owned` per underlying, independent of the parser, so a parsing bug cannot produce a naked call?
- Are there other places that infer option type or underlying by substring? Sweep for `'C' in` / `'P' in` over option symbols.
- Regression tests must include a short put on a C-containing ticker and a `BRK.B`-style class-share position.

**Links:** found during the covered-call-**extensibility** two-reviewer plan pass (project formerly numbered FC-038; review doc `fc-038-plan-review-2026-07-18.md` never landed on `main`), HIGH H1; flagged independently by both reviewers. Related: the extensibility refile (Phase 2 depends on this guard).

---

### FC-042: Backtest engine follow-on — performance, fidelity, and the filter studies

**Status:** Done — all tracks closed (PRs #49/#50/#48/#52/#54/#55/#56). **Surfaced FC-048 + FC-049, both blocking.**
**Size estimate:** L (tracks are individually S/M)
**Owner:** zeshan + Claude
**Plan file:** `docs/plans/fc-042.md`

**Problem / opportunity:** FC-032 shipped a working engine; three things separate it from answering the money questions. (1) Runs cost ~25 min/symbol — `ChainStore` is never constructed and contract discovery fetches ~70% unusable strikes. (2) Dividends/early assignment are unmodeled, blocking any verdict on income names. (3) The Jun–Jul 2026 walkthrough found NVDA blocked ~90% of the month by the gap filter's **40% vol cap** (not gap frequency), with NVDA's 30-day vol oscillating 40–42% against the hard line — trading was threshold noise on the symbol that is 155/241 lifetime put legs. The plan wires the cache + strike window (Track A), models dividends/early assignment (Track C), runs the gap-filter and premium-floor A/B studies feeding FC-002/FC-034 (Track B), and fixes FC-036/FC-035 behind quantifying studies (Track E). Includes a parallel-agent execution map. **No live thresholds change under this FC** — studies produce evidence; changes gate on FC-002/FC-034 plans.

**Open questions:**
- Vol-relative gate percentile (80th?) and graduated-response shape for B1.
- Dividend source: yfinance vs Alpaca corporate-actions endpoint (both validated reachable).

**Links:** `docs/plans/fc-042.md`, FC-002, FC-034, FC-035, FC-036, `docs/investigations/fc-032-parity-check.md`, `docs/investigations/fc-032-coverage-gate.md`.

---

### FC-043: `AlpacaClient.get_orders` status filter has never worked

**Status:** Done — merged (PR #51, `7e71f69`)
**Size estimate:** S (one wrapper function; live-behavior change)
**Owner:** zeshan + Claude
**Plan file:** `docs/plans/fc-043.md`

**Problem:** `AlpacaClient.get_orders(status=...)` (`src/api/alpaca_client.py:632`) has two independent defects: it never passes a `GetOrdersRequest`, so Alpaca's `status=open` REST default always applies (closed orders never fetched); and it value-filters `order.status.value == status`, but callers pass *query* tokens (`'open'`) that are never status *values* (`'open' not in [s.value for s in OrderStatus]`, verified). Net: the `status` argument has never selected the orders the caller asked for. **Three live callers broken:** the Stage-6 duplicate-order guard's Check 2 (`wheel_engine.py:496`, `status='open'`) returns `[]` unconditionally — and Check 2 is the *only* duplicate backstop that survives a Cloud Run cold start, i.e. the exact FC-009 window is currently unguarded; `wheel_engine.py:497` (`'pending_new'`); and `portfolio_tracker.py:315` (`'filled'`, "last 10 filled orders" — also always empty). Verified against live paper (500 closed orders; all four wrapper calls return 0).

**Fix:** map the `status` string to the correct `QueryOrderStatus` bucket, fetch with an explicit `GetOrdersRequest(limit=500)`, value-filter only for specific status values. Fixes all three callers at the root. Live trading logic → two reviewers.

**Links:** `docs/plans/fc-043.md`, FC-009 (confirmed duplicate-order bug this contributes to), FC-035/PR #47 (adjacent poll path, does not overlap — calls `trading_client` directly).

---

### FC-044: Daily execution grid — per-run decision telemetry + at-a-glance day view

**Status:** Consideration
**Size estimate:** L (two phases: telemetry backbone, then dashboard view)
**Owner:** zeshan
**Plan file:** not yet

**Problem / opportunity:** Day-to-day troubleshooting of the hourly engine is currently archaeology: to answer "what did the bot do today and was it the desired behavior?" you have to grep Cloud Run logs across three endpoints. We want a dashboard view for a single day — a grid with symbols as rows and hourly executions as columns — where each cell shows at a glance what happened to that symbol in that run: which gate stopped it (and why), whether an opportunity was found, whether a trade was placed, or whether the run never happened at all. The goal is visual deviation-detection: a normal day has a recognizable shape, and an abnormal one (a gate suddenly blocking everything, a missing scheduler run, a symbol silently skipped for weeks) should be visible without reading a single log line.

**The hard prerequisite — the data doesn't exist yet (surveyed 2026-07-25):** the grid cannot be built from BigQuery today; the finest live granularity is one `executions` row per endpoint invocation with **no symbol column**. Specifically:
- All per-symbol gate/skip reasons (stage 1 stock filter, stage 7/8 chain criteria with rejection breakdowns, batch dedup, naked-call block, drawdown pause) are `logger.info` → Cloud Run logs only. The `options_wheel_logs` sink dataset technically retains them, but nothing normalized reads it and the dashboard was deliberately repointed off it in FC-012.
- Two skips produce **zero telemetry anywhere**: insufficient-collateral drop in `select_batch` (`src/strategy/execution_engine.py:226` has no `else`) and the scanner's existing-position skip (`src/data/options_scanner.py:53`, bare `continue`).
- **No run identifier joins one hour together.** `request_id` is bound per HTTP request (so a `/scan` at :00 and its `/run` at :15 get different ids) and — despite `executions`/`errors` having a `request_id` column — is never passed to `write_execution`/`write_error`, so the column is always `""`. The only scan→run correlator is the 20-min-TTL GCS opportunity blob.
- **`options_wheel.scans` is a dead table with live readers.** Its writer was deleted in FC-012, yet the dashboard's Bot Health gate heatmap (`dashboard/backend/services/bigquery.py:303`) and the `gate_full_block_streak` anomaly flag (`:1393`) still query it — both presumably render empty today. No FC covered this until now.
- The documented 9-stage funnel (`docs/logging/FILTERING_STAGES_LOGGING.md`) describes the CLI/backtest path, not production: stages 2–6 and 9 never execute in the live `/scan`→`/run` path, while the gates that *do* fire live (scanner position skip, batch collateral fit, batch dedup) have no stage number at all. A grid must be built on the **live** gate sequence, not the documented one.
- `log_filtering_event()` (`src/utils/logging_events.py:421`) — the one helper defining a normalized `stage`/`status`/`symbol` contract — is dead code with zero call sites; it's a natural starting point for Phase 1.

**Rough shape (detail belongs in the plan):**
- *Phase 1 — decision telemetry:* a `run_id` minted at `/scan` and threaded through the GCS opportunity blob into `/run`; a new BQ `decision_events` table (run_id, run_ts, endpoint, symbol, gate, outcome, reason, metrics JSON) written at every gate verdict including the currently-silent skips; retire or repoint the dead `scans` readers.
- *Phase 2 — the grid:* symbols × hourly runs for a selected day; cell encodes furthest-stage-reached / terminal outcome, click-through to the full decision trail for that symbol-run; a column that never happened (scheduler miss, endpoint 500) must render as visibly distinct from "ran, nothing tradeable" — silent non-execution is exactly the failure mode that has bitten before (FC-031 sat undeployed 11 days; roller never fired).

**Open questions:**
- Write-time events to a new table vs. a normalized view over the existing `options_wheel_logs` sink? (Sink is free and already flowing, but wildcard-table parsing per event type is brittle and the two zero-telemetry skips still need code changes either way.)
- Cell semantics: terminal outcome only, or furthest-stage + reason? How to encode multi-contract evaluation (stage 7 rejects 40 contracts, accepts 1) without drowning the at-a-glance read?
- What defines "expected behavior" for deviation highlighting — static schedule (6 runs/day × universe), or a learned baseline?
- Volume/cost: ~14 symbols × ~4 gates × 6 runs/day is trivial (<500 rows/day), but per-contract stage-7 detail could be 100×; keep contract-level breakdown as aggregate counts in the metrics JSON?
- Does Phase 1 subsume FC-030's pause-observability metric (drawdown pause becomes just another gate event)?
- Retention: decision events are diagnostic, not canonical — partition + expire after N months?

**Links:** FC-030 (pause alerting — overlapping telemetry), FC-036 (dead stage-4 gate — grid would have made this visible immediately), FC-014 (RiskManager never invoked live — same class of "documented gate doesn't fire"), FC-002 (gate-hit-rate analysis wants the same data), FC-012 (`scans` writer removal), `docs/investigations/dashboard-metrics-audit-2026-07-07.md` §Bot Health.

---

### FC-045: `/monitor` misroutes covered calls to put-close logic (`'P' in symbol`)

**Status:** Consideration
**Size estimate:** S (one expression + test; live behavior path)
**Owner:** unassigned
**Plan file:** not yet

**Problem:** `deploy/cloud_run_server.py:688-689` classifies an option position by substring:

```python
is_put = 'P' in symbol      # evaluated FIRST
is_call = 'C' in symbol
```

`symbol` is a full OCC symbol (e.g. `AAPL250815C00185000`), so the ticker's own letters are tested. Any underlying containing `P` matches `is_put`, and because `is_put` is checked first, **every covered call on those symbols is evaluated by `should_close_put_early()` instead of `should_close_call_early()`** and tagged `position_type='PUT'`.

**Affected: 3 of 14 universe symbols — `AAPL`, `SPY`, `PFE`** (two are core holdings). Verified against `config/settings.yaml` 2026-07-28.

**Actual impact today is telemetry, not money — verified, not assumed.** `should_close_put_early` and `should_close_call_early` currently converge: both call `_get_profit_target_for_dte`, which reads the *same* shared config (`profit_taking_dte_bands` / `profit_taking_static_target`) in both classes, and both stop-loss branches are gated off (`use_put_stop_loss: false`, `use_call_stop_loss: false` — FC-010). So the close *decision* is presently identical; what differs is the emitted event (`put_profit_target_reached`) and `position_type`, which mislabels call closes as put closes in the analytics/dashboard attribution for those symbols.

**Latent severity is higher.** The moment `use_call_stop_loss` is re-enabled, or the two profit-target implementations diverge, this becomes a real behavioral bug — misrouted calls would silently consult the *put* stop-loss flag and never trigger call protection. It is a landmine, not just a label.

**Fix:** use the canonical parser — `parse_option_symbol(symbol)['option_type']` (`src/utils/option_symbols.py`, fully-anchored OCC regex). Do not re-implement OCC parsing. Lower-stakes copies of the same substring pattern in `tools/testing/*` can ride along.

**Why filed now:** surfaced during FC-035's plan verification. PR #47 fixed this same defect class inside the (now deleted) poll path via `_classify_order_strategy()`; that fix dies with the branch, but the live `/monitor` instance remains. Same bug family as FC-041 (naked-call guard OCC misparse) and the FC-043 Stage-6 substring over-block — **the third occurrence of substring-matching an OCC symbol**, which argues for a lint/grep guard rather than another one-off fix.

**Links:** `docs/plans/fc-035.md` (where it was found), FC-041, FC-043, `src/utils/option_symbols.py`.

---

### FC-046: `options_wheel_logs.trades_executed` view is unqueryable

**Status:** Consideration
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not yet

**Problem:** every query against the live view fails:

```
Cannot read field of type FLOAT64 as STRING  Field: jsonPayload.symbols
Cannot read repeated field of type STRING as optional STRING  Field: jsonPayload.symbols
```

The view selects across the `run_googleapis_com_stderr_*` day-sharded log tables and does `TO_JSON_STRING(jsonPayload)`. `jsonPayload.symbols` has been emitted with **three different shapes** over time (FLOAT64, STRING, REPEATED STRING), so the wildcard union cannot resolve a single type. Reproduced live 2026-07-29: `SELECT COUNT(*) FROM options_wheel_logs.trades_executed` errors.

**Compounding defect found 2026-07-18 — the sink itself is dead, so fixing the view yields an empty view.** The `options-wheel-logs` export sink stopped writing on **2025-11-22**: the newest day-sharded table is `run_googleapis_com_stderr_20251122` and the partitioned table's `MAX(timestamp)` is null. So there is no log data in BigQuery at all for the last eight months, independent of the schema collision. Any investigation using BigQuery for bot decisions must use `gcloud logging read` instead — which carries only **30-day** retention, meaning FC-038's new `selection_dropped` reasons and FC-050's `cost_basis_resolved_via_fallback` events are queryable for 30 days and then gone. Restoring the sink into the same dataset without addressing the repeated-field collision below will simply re-break it.

**Why it matters:** this view is treated in plan docs as a live ad-hoc analysis surface (FC-035's behavior contract reasoned carefully about not polluting it). It has in fact been broken — since 2025-11-22 for the sink, and unknown-but-longer for the schema collision. Any analysis that assumed it works has been silently unavailable, and the schema-collision class is the same one recorded in the 2026-04-07 session memory ("never string-ify arrays").

**Fix direction:** pin the wildcard union to a consistent projection (extract `symbols` with `JSON_VALUE`/`JSON_QUERY` rather than `TO_JSON_STRING` over the whole payload), or exclude the offending shards. Decide first whether the view is still wanted at all, given the dashboard reads `trades_from_activities`.

**Found:** during FC-035's two-reviewer pass, by a reviewer verifying the deletion had no consumers.

**Links:** `docs/plans/fc-035.md`, FC-012.

---

### FC-047: `log_system_event` never sets `event_type`, so system events are unqueryable by it

**Status:** Consideration
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not yet

**Problem:** `src/utils/logging_events.py` `log_system_event()` does:

```python
logger.info(event_type, event_category="system", status=status, ...)
```

`event_type` is passed as structlog's **positional message**, so it lands in `jsonPayload.event` and `jsonPayload.event_type` is **never set** for any system event. The function's own docstring advertises the opposite, showing `SELECT event_type ... GROUP BY event_type` — a query that returns NULL for every row it produces.

**Confirmed live (2026-07-29):** `pre_trade_reconciliation_completed` has 492 rows, all under `jsonPayload.event`. Two independent FC-035 reviewers each hit this and had to correct their queries mid-review; one nearly concluded the pre-trade housekeeping block had never run.

**Why it matters:** every consumer that filters system events on `event_type` silently matches nothing. This is a monitoring/observability trap of exactly the kind that hid FC-030's alerting problem (an alert filtering `severity>=WARNING` that matched nothing). Any future post-deploy verification keyed on `event_type` will look like a regression.

**Fix direction:** pass `event_type=event_type` as a kwarg (and keep the message for readability). **Check for downstream breakage first** — some views may already compensate by reading `jsonPayload.event`, and setting both could double-count or change existing query results. Contrast with `log_risk_event` / `log_order_status_update`, which set the kwarg correctly; the inconsistency is the real defect.

**Links:** `docs/plans/fc-035.md`, FC-030.

---

### FC-048: every backtest runs only half a wheel — covered calls are misrouted to the put path

**Status:** Consideration — **high priority, affects the credibility of every backtest verdict**
**Size estimate:** S to fix, M to re-validate downstream conclusions
**Owner:** unassigned
**Plan file:** not yet

**Problem:** `ExecutionEngine.execute_batch` routes on `opp.get('type', 'put')` (`src/strategy/execution_engine.py:286`) — defaulting to **put**. There are two producers of opportunities and only one of them sets that key:

| producer | sets `type`? | routes to |
|---|---|---|
| `options_scanner.py:340` (production `/scan` path) | **yes** (`'type': 'call'`) | call path — correct |
| `call_seller.evaluate_covered_call_opportunity` (used by `wheel_engine._find_new_opportunities` and `_manage_existing_positions`) | **no** — emits `'strategy': 'sell_call'` instead | **put path — misrouted** |

The backtest replays `run_strategy_cycle()`, which uses the second producer. So **every covered-call opportunity in every backtest is handed to `put_seller`**, which rejects it. Verified three ways: the code trace above; a direct routing simulation (engine-shaped dict → `'put'`, scanner-shaped dict → `'call'`); and empirically — a 2-year NVDA replay reports **30 puts sold, 0 calls**, and a B2 study run found **293 call opportunities on AAPL, 0 sold**.

**Production is NOT affected.** It executes from the scanner path, which types calls correctly; 84 real covered calls appear in `trades_from_activities`. This is a backtest-fidelity defect, not a live trading bug.

**Why it went unnoticed:** the golden test is named `test_the_wheel_actually_turns`, but it only asserts put-sold → assignment → shares-held. **No test has ever asserted that a covered call is sold in replay.** The suite covers exactly the half that works.

**What it means for existing conclusions:**
- **Every backtest to date models a put-only strategy.** Assigned shares are never called away, so cycles never complete, call premium is never earned, and returns are understated on any symbol that gets assigned.
- This plausibly explains an FC-032 Phase 5 finding previously blamed on window length: **7 of 14 symbols — including SPY and QQQ — were flagged for demotion purely for not closing a cycle.** A cycle cannot close without the call leg.
- Affects the engine-A/B layers of `fc-036-gap-gate-study.md` and the Track B studies. It does **not** overturn their headline conclusions, both of which rest on real-fills layers rather than the engine (FC-036's DO-NOT-ARM; FC-034's DEMOTE) — but the engine tables in those docs should be read as put-only.

**Fix direction:** set `'type': 'call'` in `evaluate_covered_call_opportunity` (and audit the put side for symmetry, per the repo's wheel-symmetry rule). Then add the test that was missing: a replay assertion that a covered call is sold after assignment. Re-run the affected verdicts afterwards — the fix is small, but re-validating what it changes is the real work.

**Found:** by the FC-034 (B2) premium-floor study while investigating why enabled symbols showed no call activity.

**Links:** `docs/plans/fc-042.md`, `docs/plans/fc-032.md`, `docs/investigations/fc-034-premium-floor-ab.md`, FC-015 (same family: a gate that has never fired while looking healthy).

---

### FC-049: The stage-2 gap-risk filter is not wired into the live trading path

*(Renumbered from FC-048 at merge: FC-048 was concurrently allocated on main to the covered-call misroute found by the B2 study. Independent findings — but the same species: a control that looks active and is not.)*

**Status:** Consideration — **code deleted from the tree 2026-08-04 by FC-069 item 5**
**Size estimate:** M
**Owner:** unassigned
**Plan file:** not yet

> **⚠ The code this entry describes is no longer in the repository.** FC-069 S1
> (operator decision, option ii) deleted `src/risk/gap_detector.py` (645 lines),
> `tests/test_gap_detector.py`, the entire `risk.gap_risk_controls:` yaml block
> (12 keys), every `Config` accessor for them, `/config`'s `gap_thresholds`
> block, and the three gap keys in `bq_writer.config_hash`. **It all lives at
> pre-S1 `main` SHA `afb6698`** — `git show afb6698:src/risk/gap_detector.py`.
>
> The operator's reasoning: "revive only with evidence" is better served by a
> git SHA than by 645 orphaned lines the next inventory has to re-litigate,
> and reviving *with* evidence means reviving code with the evidence in hand.
> The post-sweep control matrix reads **"gap risk: absent by decision"**, which
> ratifies FC-036's study (don't arm the execution gate) alongside this entry's
> finding. **This FC still owns any evidence-based revival** — nothing about
> the deletion forecloses it; it starts from git history rather than from a
> corpse in the tree. The two study harnesses are kept and carry SHA pointers:
> `tools/diagnostics/fc002_gap_filter_ab.py` and `fc036_gap_gate_study.py`.

**Problem:** `GapDetector.filter_stocks_by_gap_risk` is called from exactly one place —
`WheelEngine._find_new_opportunities`. The deployed Cloud Run trading path is
`/scan` → `OptionsScanner` → `OpportunityStore` → `/run`, and `src/data/options_scanner.py`
never constructs a `GapDetector`. Commit `842dcce` (2025-10-03, "Implement Cloud Storage
scan-to-execution architecture") removed `wheel_engine.run_strategy_cycle()` from `/run`;
the live account's first fill is **2025-10-06**. **No live trade has ever been evaluated by
stage 2.** The server does build a `WheelEngine`, but only for `reconcile_positions()` and
`run_rolling_cycle()` — neither reaches `_find_new_opportunities`.

**Confirmed four ways (2026-07-29)**, all reproducible via
`tools/diagnostics/fc002_gap_filter_ab.py verify`:
1. Source inspection: no module on the live scan path references `GapDetector`.
2. `git log -S "run_strategy_cycle" -- deploy/cloud_run_server.py` → `842dcce`, three days
   before the first fill.
3. Cloud Logging (40d): all 18 request_ids emitting `stage_2_complete` /
   `stock_passed_gap_filter` / `stock_filtered_by_gap_risk` are **backtest** requests.
4. On 2025-10-06, `docs/analysis/AMD_GAP_RISK_ANALYSIS_2025.md` records the filter returning
   "Suitable for Trading: NO" for AMD; BigQuery shows `AMD251010P00192500` sold short that
   same day for $223.

**Why it matters:** this is the mirror of FC-036 (a control that ran but measured the wrong
thing) — a control that measures correctly and never runs. It also invalidates the framing
of **FC-002**, whose block rates describe the backtest engine, not production. Reconstructed
over the same bars, the filter would have refused **123 of 327** real entries.

**Decision required before any threshold work:** wire it in, or delete it. What it must not
remain is a control that exists in `config/settings.yaml`, in the backtest, and in the FC
index, but not in the thing that trades. **Wiring the current rule in unchanged would be the
largest behaviour change in the project's history and, on the evidence, a costly one** — see
the study. Same class of latent defect as FC-035 (dead poll) and FC-039 (state persistence
never worked); worth a sweep for others.

**Links:** `docs/investigations/fc-002-gap-filter-ab.md`, FC-002, FC-036, FC-039.

---

### FC-051: the spread model needs per-symbol calibration, not a pooled fit

**Status:** Consideration
**Size estimate:** M
**Owner:** unassigned
**Plan file:** not yet

**Context — FC-042 Track C3 is now closed on its measurement half.** The RTH sample that C3 was blocked on has been taken (2026-07-29 11:39 ET, market confirmed open via Alpaca's clock, `--require-rth`): **n=524 OTM puts across AAPL/AMD/IWM/NVDA, median real half-spread $0.0250 vs $0.0614 modeled — 2.46x wider, wider on 86% of contracts.** That **retires** the long-standing caveat: the earlier after-hours sample (2.12x) warned the gap might close intraday, leaving "the model is conservative" unproven. It does not close — intraday the model is *more* conservative, not less. The report footer now states the RTH figure.

**What remains — the model's shape, not its level.** Fitting `SpreadModel` on that same RTH sample yields:

| | value |
|---|---|
| R² | **0.027** |
| samples used | 256 of 524 (121 excluded cheap, 147 at the $0.02 floor) |
| pooled `base_frac` | 0.0251 (default 0.05) |
| per-symbol `base_frac` | **AMD 0.0050 · IWM 0.0232 · NVDA 0.0159 · AAPL 0.0773** |

That is a **15x spread across four symbols**, and moneyness explains ~3% of the variance. The tool's own verdict is `NOT USABLE — DO NOT COMMIT`, and no parameters were committed.

**Why a pooled fit cannot work here:** a single `base_frac` averages a $290 ETF (IWM) against a $440 single name (AMD). Half-spread as a *fraction of mark* is not the same quantity across those. Two structural facts the fit surfaces: 28% of contracts sit at the **$0.02 exchange floor** — a constant, not a fraction — and 23% are "cheap" contracts where `cheap_widening` is already applied at eval, so fitting them double-counts it.

**Fix direction:** calibrate per symbol (or per liquidity tier / price bucket), and model the $0.02 floor explicitly rather than letting it distort a proportional fit. Re-check R² per symbol; NVDA already scores `[OK]` alone while the other three do not.

**Why it matters:** every backtest premium is a modeled bid/ask. The *level* is now known to be conservative, so verdicts are not being flattered — this is about narrowing an honest but coarse error bar, not correcting a bias. Lower priority than the FC-048 re-validation.

**Links:** `docs/plans/fc-042.md` (Track C3), `tools/diagnostics/spread_model_check.py`, PR #48.

---

### FC-052: oversell guard counted short PUTs as committed calls (5th OCC-substring instance)

**Status:** Done — fixed in the same PR that filed it
**Size estimate:** S
**Owner:** zeshan + Claude
**Plan file:** not needed (single-file, latent bug, no behavior change for the current universe)

**Problem:** `ExecutionEngine.execute_batch`'s covered-call oversell guard decided "is this short option a call on my underlying?" with two heuristics stacked:

```python
opt_underlying = ''
for ch in opt_sym:              # hand-rolled: chars up to the first digit
    if ch.isdigit(): break
    opt_underlying += ch
if opt_underlying == underlying and 'C' in opt_sym:   # substring
```

A short **put** on any ticker containing a `C` matches `'C' in opt_sym`, so it was counted as a committed call. That over-reports committed shares, under-reports available shares, and **silently starves the call side** — refusing legitimate covered calls with no error, the same signature as the covered-call starvation investigated on 2026-07-18.

**Latent, not firing:** no configured symbol contains a `C` (AAPL, MSFT, GOOGL, AMZN, NVDA, AMD, QQQ, SPY, IWM, UNH, F, PFE, KMI, VZ). It fires the moment one is added — and the failure mode is silence, not an error.

The hand-rolled underlying parser is separately wrong on adjusted roots: `1AAPL250815C00190000` yields `''` (first char is a digit).

**Fifth instance of the OCC-substring family** — FC-041 (naked-call guard), FC-043 (Stage-6 over-block), FC-045 (`/monitor` misroute), FC-048 (execution routing), and now this. FC-048 added the shared `strict_option_type()` primitive precisely so these stop recurring; this converts the last known site.

**Fix:** parse the contract once with the canonical parsers — `parse_option_symbol` for the underlying, `strict_option_type` for the side.

**Tests:** a short put on a C-containing ticker no longer consumes call capacity; a real short call still does (guard not over-corrected into overselling). Mutation-verified — reverting to the substring form fails the first test.

**Links:** FC-041, FC-043, FC-045, FC-048, `docs/investigations/covered-call-starvation-2026-07-18.md`.

### FC-053: the `/monitor` unknown-option-type skip is silent in production

**Status:** Consideration
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not yet

**Problem:** FC-045 made `/monitor` classify positions by a strict OCC parse. A position the parser rejects now takes the pre-existing `else: continue` branch — **skipped every cycle, never profit-taken**. With both stop-losses disabled (FC-010) it simply rides to expiry or assignment.

Skipping beats guessing (a mis-classified position would run the wrong seller's logic), so the design is right. The problem is that **nobody would notice.** The branch logs `logger.warning("Unknown option type", event_type="unknown_option_type")` and:

- Only two alert policies exist in `gen-lang-client-0607444019` — Cloud Build failure and drawdown pause. Neither matches this event.
- Cloud Run plain-text logs land at severity `DEFAULT` (recorded in the 2026-07-18 ops session), so even a `severity>=WARNING` filter would match nothing.

**When it fires:** adjusted contracts after a corporate action (`AAPL1250117C…`), which Alpaca does produce, and any symbol form the anchored regex rejects. **Zero live positions are affected today** — verified: the account's two open option positions both parse.

**Fix direction:** escalate to `log_error_event` (which sets `event_type`, unlike `log_system_event` — see FC-047) and/or return a `skipped` count in the `/monitor` response so a scheduler check can see it. Consider whether an adjusted contract should be *closeable* rather than merely skipped — an unmanaged short option is a real risk, just a rarer one than a mis-managed one.

**Found:** by the FC-045 reviewer, tracing what the newly-reachable branch does in production.

**Links:** FC-045, FC-047, FC-010.

---

### FC-054: `wheel_engine` state reconciliation counts calls as puts (6th OCC-substring site)

**Status:** Consideration
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not yet

**Problem:** `src/strategy/wheel_engine.py:923-925` uses the same defect the whole family is named for:

```python
if 'P' in option_symbol: ...      # counts a PUT
elif 'C' in option_symbol: ...    # counts a CALL
```

`option_symbol` is a full OCC symbol, so the ticker's own letters decide. **AAPL, SPY and PFE calls are counted as puts** in state reconciliation today — the same three symbols FC-045 fixed in `/monitor`, in a different file.

**Sixth known instance:** FC-041 (naked-call guard) · FC-043 (Stage-6 over-block) · FC-045 (`/monitor`) · FC-048 (execution routing) · FC-052 (oversell guard) · this. The shared `strict_option_type()` primitive exists (added in FC-048); this site has not been converted.

**Impact:** reconciliation miscounts, so wheel-state phase decisions and any telemetry derived from those counts are wrong for the three affected symbols. Needs tracing to say whether it changes a trading decision or only a count — do that before sizing the fix.

**Do the sweep, not another one-off.** Six occurrences argue for a repo-wide grep gate (a test asserting no `'P' in`/`'C' in` over an OCC symbol variable outside the canonical parser) rather than a seventh individual fix. FC-041's open question already calls for this sweep; this entry is the concrete evidence it is still needed.

**Found:** by the FC-045 reviewer, checking whether the family survived elsewhere.

**Links:** FC-041, FC-043, FC-045, FC-048, FC-052, `src/utils/option_symbols.py`.

---

### FC-055: the $400 `max_stock_price` ceiling silently excludes 3 of 14 symbols (4 with MSFT)

**Status:** Consideration — **high value, cheap to test**
**Size estimate:** S to change, M to validate
**Owner:** unassigned
**Plan file:** not yet

**Finding:** a full post-FC-048 screen shows **6 of 14 symbols with 0% days in position** — they never opened *any* position. Three of them are blocked at **stage 1** by `max_stock_price: 400.00` (`config/settings.yaml:49`), verified against live quotes on 2026-07-29:

| symbol | spot | over ceiling by |
|---|---:|---:|
| SPY | $736.47 | +84% |
| QQQ | $670.92 | +68% |
| AMD | $441.82 | +10% |
| *MSFT* | *$395.39* | *within $5 — oscillating across the line* |

**Why this matters more than it looks:**

- **SPY and QQQ are the two most liquid options markets in existence.** They are excluded by a price constant, not by any risk judgement.
- **AMD is the second-largest premium generator in the account's history** (FC-002 studied its gap-filtering at length — while it was also being excluded at stage 1 on price).
- **MSFT's `unfit` verdict is a boundary artifact.** At $395 it drifts across the ceiling, producing 2% days-in-position and a "cannot consistently trade this symbol" BLOCK. That reads as a strategy failure and is a config threshold.

**Combined with FC-034** (premium floor excludes F/PFE/KMI/VZ), the effective universe is **6 symbols, not 14**: AAPL, AMZN, GOOGL, IWM, NVDA, UNH. Every one of those six scored `marginal` with 61–93% days in position.

**The likely root cause is drift, not design.** The ceiling has not moved as the market rose; `min_stock_price` was lowered to 10.00 specifically to admit Ford, but the upper bound was never revisited. A cash-secured put on a $700 SPY needs $70k of collateral against a $40k `max_exposure_per_ticker` — so a ceiling *is* needed; the question is whether $400 is the right expression of it, or whether **collateral-per-contract** is the quantity actually being constrained.

**Test before changing:** the backtest engine can answer this directly now — raise the ceiling in a config override and re-screen SPY/QQQ/AMD. Do not change the live threshold on this entry alone; it needs its own plan + two reviewers, and the interaction with `max_exposure_per_ticker` and `max_position_size` must be worked out (a $736 SPY put is $73.6k of collateral against a $40k per-ticker cap — it may be excluded twice over, in which case raising the price ceiling alone changes nothing).

**Found:** during FC-048 re-validation (`docs/investigations/fc-048-revalidation.md`), while checking whether the `insufficient` verdicts were a put-only artifact. They were not — this is.

**Links:** `docs/investigations/fc-048-revalidation.md`, FC-034 (the low-price half of the same story), FC-032, FC-002.

---

### FC-056: the engine prices covered calls ~32% below live on identical contracts

**Status:** Consideration
**Size estimate:** M (investigation first — cause unknown)
**Owner:** unassigned
**Plan file:** not yet

**Finding:** the first call-side parity measurement (`docs/investigations/fc-032-call-parity.md`, 80 real decisions) shows the replay marks a covered call at **67.6% of the premium live actually received on the *identical* contract**. The put leg's equivalent figure is ~93% (a ~7% shortfall). **The call leg's pricing error is roughly 5x the put leg's.**

**A hypothesis already tested and DISCONFIRMED.** The obvious cause was DTE mix — 28% of live call fills are at DTE 8 while the sim caps at `call_target_dte: 7`, so it would pick shorter, cheaper contracts. Splitting exact-strike matches:

| | n | median sim/live |
|---|---:|---:|
| live DTE ≤ 7 (sim can match the expiry) | 8 | **0.644** |
| live DTE > 7 (sim capped shorter) | 6 | 0.723 |

The shortfall is **slightly worse where the sim can match the expiry**. Do not re-propose DTE as the cause without new evidence.

**Candidate directions, none verified:**
- **Thin call bars.** Daily option bars are trade aggregates; the close is the last print. Covered calls at 0.15–0.25 delta may trade far less than the equivalent puts, making the close staler and more likely to be an early-session print.
- **Intraday drift with a sign.** Live sold these at ~9:35/12:00/15:00; the bar close reflects a different underlying level. For calls, a systematic intraday drift would bias one way — measurable against intraday bars.
- **Spread model interaction.** Modeled half-spread is 2.46x real (FC-051 measurement); the 0.25 fill haircut then over-charges. But on a $3.10 median call this is pennies, not 32% — it cannot be the main term.

**Also unexplained: selection.** Strike reproduction is **55.2%** on calls vs 81% on puts, and is concentrated — GOOGL 15/18 and NVDA 9/19 reproduce well, while **AMD 0/9 and AMZN 1/17 barely reproduce at all**. Worth checking whether those two share a chain-liquidity or strike-spacing property.

**Why it matters, and why it is not urgent:** the error is **conservative** — the engine understates call premium, so it understates wheel returns. Every known bias now points the same way (put leg ~7% low, spread model 2.46x wide, call leg ~32% low), which makes reported returns a **floor**. That is the safe direction for a screening tool. But an absolute call-leg return should not be quoted as accurate, and a `marginal` verdict driven by the call leg is probably better in reality than reported.

**Links:** `docs/investigations/fc-032-call-parity.md`, `docs/investigations/fc-032-parity-check.md` (put side), FC-048 (made the call leg measurable at all), FC-051 (spread calibration).

### FC-057: stage-1 rejections were invisible in every backtest report

**Status:** Done — fixed in the same PR that filed it
**Size estimate:** S
**Owner:** zeshan + Claude
**Plan file:** not needed (one map entry + tests; no behavior change)

**Problem:** `RejectionTally._REASONS` mapped stages 2, 4, 5, 6, 7, 8 and the drawdown pause — but **not stage 1**. A symbol blocked on the price band or volume floor produced **no tally entry at all**, so a screen reported it as having simply done nothing: `insufficient`, 0% days in position, **no reason given**.

**This is how the $400 `max_stock_price` hid.** SPY ($736), QQQ ($671) and AMD ($442) have been excluded at stage 1 for months. Their verdicts were read as *strategy* results — "the strategy can't trade this symbol" — when the truth was "a config constant excludes it before the chain is ever examined". FC-055 only surfaced because the FC-048 re-validation cross-checked spot prices by hand against `config/settings.yaml`; the engine's own report could not say it.

The event was never missing. `market_data.py:148` emits `stock_rejected_filter` with the price and the band it missed. **Nothing counted it.**

**Fix:** one entry in `_REASONS`. Verified end to end — a SPY replay now reports `The binding constraint was **price/volume band (stage 1)**` with 20 of 20 decision days attributed, where before it reported an unexplained `insufficient`.

**Tests:** a parametrised contract asserting **every** stage that can block a day has a nameable reason — so the next stage added to the pipeline cannot be silently unreportable — plus a direct tally test and a guard that unmapped events are still ignored (the tally names known stages, it must not invent reasons). Mutation-verified.

**The general lesson:** the engine's *observability* map is a correctness surface. A stage missing from it does not produce a wrong number — it produces a **missing explanation**, which is worse, because the reader supplies their own.

**Links:** FC-055 (the exclusion this made invisible), `docs/investigations/fc-048-revalidation.md`, FC-015 (same family: a control that looks healthy while doing nothing).

---

### FC-058: Track D blockers — wrong image registry, and the Secret Manager fallback is probably dead

**Status:** Blockers RESOLVED (Track D shipped 2026-07-30); **the code fix remains open**. The deploy was unblocked by reading the live service rather than by changing code: credentials are wired as **`secretKeyRef`** to Secret Manager, so Cloud Run injects the env var directly and the application-level fallback is never exercised. That means the `GOOGLE_CLOUD_PROJECT` / `GCP_PROJECT_ID` vs `GCP_PROJECT` mismatch is **dead code, not a live outage** — but it is still wrong, and the bare `except Exception: pass` around credential resolution still means a silently empty credential. Fix both when convenient.
**Size estimate:** S once verified; verification needs `gcloud auth`
**Owner:** unassigned
**Plan file:** not yet

**Found while attempting Track D.** Two things would each independently break a Cloud Run Job created from the runbook as originally written.

**1. Wrong image registry (my error, now corrected in the runbook).** The handoff doc said `gcr.io/gen-lang-client-0607444019/options-wheel-strategy`. The service is actually built and pushed to **Artifact Registry**: `us-central1-docker.pkg.dev/$PROJECT_ID/options-wheel/options-wheel-strategy:$COMMIT_SHA` (`cloudbuild.yaml:68`). ~~There is also no `:latest` tag published~~ — **CORRECTED 2026-07-30**: a `latest` tag *is* published (verified via `gcloud artifacts docker images list`, which showed `<sha>,latest`). SHA-pinning is still the right choice for a Job — reproducibility, and `latest` moves under you — but the original claim was false.

**2. The Secret Manager fallback almost certainly never resolves.** `src/utils/config.py:33`:

```python
project_id = os.getenv('GOOGLE_CLOUD_PROJECT', os.getenv('GCP_PROJECT_ID'))
```

`cloudbuild.yaml:75` sets **`GCP_PROJECT`** — neither name the fallback reads. Cloud Run does not inject `GOOGLE_CLOUD_PROJECT` by default. So `project_id` is `None`, the request name becomes `projects/None/secrets/...`, the call throws, and `except Exception: pass` swallows it and returns `""`.

Counted across `src/` + `deploy/` + `cloudbuild.yaml`: **`GCP_PROJECT` appears 11 times, `GOOGLE_CLOUD_PROJECT` 9, `GCP_PROJECT_ID` 4** — but the Secret Manager path is the *only* place reading the latter two. The naming is inconsistent and this is where it bites.

**Implication:** since the live bot does trade, `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` are almost certainly present on the service as env vars **applied out of band** (consistent with the 2026-07-18 ops gotcha that `--set-env-vars` wipes the whole env set — a symptom of exactly this pattern). **A fresh Cloud Run Job inherits none of it** and would fail to authenticate.

**Verify before acting (needs `gcloud auth login`):**
1. `gcloud run services describe options-wheel-strategy --region us-central1 --format="yaml(spec.template.spec.containers[0].env)"` — is `ALPACA_API_KEY` a literal env var, a `secretKeyRef`, or absent?
2. If absent, the Secret Manager path *is* working somehow — find out how, because the code path above says it should not.
3. Check whether any secret is reachable: `gcloud secrets versions access latest --secret=alpaca-api-key`.

**Fix direction:** settle on **one** project env-var name and use it everywhere (`GCP_PROJECT` is the majority and what cloudbuild sets — the fallback should read it). Then wire the Job with `--set-secrets` rather than copying literal keys, so credentials are not duplicated across two deploy surfaces. Also consider whether that bare `except Exception: pass` should log — a silently empty credential is the worst possible failure mode, and it is why this went unnoticed.

**Links:** `docs/BACKTEST_ENGINE.md` (Track D runbook, corrected), `cloudbuild.yaml:68,75`, `src/utils/config.py:20-40`, FC-031 ops session (the `--set-env-vars` wipe gotcha).

---

### FC-059: Cloud Run **Job** logs never reach Cloud Logging (the screen is unobservable)

**Status:** Done — fixed, and **verified in production**: the same Job emitted 0 log entries in 64 minutes on the old image and 14 in the first 3 minutes on the fixed one. Per-symbol progress is now visible, which is how the 1h47m runtime and the ~5.5 min/symbol pacing were measured at all.
**Size estimate:** S (one predicate; the consequence is large)
**Owner:** zeshan + Claude
**Plan file:** not needed (one function + tests)

**Found by watching the first real Cloud Run Job execution produce ZERO application logs in 19 minutes.** The job was healthy; it was invisible.

`src/utils/logger.py` decided where to write with:

```python
def _is_cloud_run() -> bool:
    return os.environ.get("K_SERVICE") is not None
```

Cloud Run **Services** set `K_SERVICE`. Cloud Run **Jobs** set **`CLOUD_RUN_JOB`** and **`CLOUD_RUN_EXECUTION`** — and *not* `K_SERVICE`. So inside a Job:

1. `_is_cloud_run()` → `False`
2. `log_to_file` → `True` (the local-dev default)
3. `logging.FileHandler("logs/options_wheel.log")` — a file inside the container
4. Cloud Run's filesystem is **ephemeral**; the file dies with the task

**Consequence:** the monthly backtest screen — the thing Track D exists to schedule — produces **no observable output at all**. A failure surfaces to the operator as "task failed" with no reason, no traceback, and no indication of which symbol it died on. For a job that runs 12 times a year and writes to a canonical BigQuery table, that is close to the worst possible observability posture.

**This would have shipped invisibly.** The Job was created, started, pulled its image and ran. Every signal said healthy. Only the *absence* of logs revealed it, and only because the run was being watched live — a scheduled 03:00 execution would have failed silently forever.

**Fix:** check all three variables. Verified across all four shapes (local, Service, Job via `CLOUD_RUN_JOB`, Job via `CLOUD_RUN_EXECUTION`); local dev still logs to a file, which is wanted. Mutation-verified: reverting to `K_SERVICE`-only fails 3 tests.

**Same family as this session's recurring theme** — FC-015, FC-036, FC-048, FC-057, FC-058: code that looks healthy while doing nothing, and failure modes that are silent rather than loud. Here the silence *was* the symptom.

**Note for Track D:** the currently-running execution predates this fix (image SHA `2c1773c`), so it will stay silent. The scheduler must not be re-pointed until a Job runs on an image containing this fix — otherwise the monthly schedule is undiagnosable from day one.

**Links:** `docs/BACKTEST_ENGINE.md` (Track D), FC-058, FC-047 (the other logging-visibility defect), the 2026-07-18 ops session (Cloud Run plain-text logs land at severity DEFAULT).

---

### FC-060: scenario-analysis platform — parameter exploration, an owned option-data history, and persisted scenarios

**Status:** Consideration — **deliberately deferred until the monthly screen is live and verified**
**Size estimate:** XL (four separable layers; each is L on its own)
**Owner:** zeshan
**Plan file:** not yet — needs its own plan, and probably one plan per layer

---

## The vision (owner-stated)

1. **Evaluate how different parameters perform across a selected universe** — either the current live wheel symbols or **new candidate symbols** being considered for inclusion.
2. **A web UI for ad-hoc exploration** of return curves and scenario analysis.
3. **Programmatic regression tests** that search optimal permutations of inputs across the symbol universe, so **production parameters are grounded in real data rather than intuition alone**.
4. **Persist the underlying option data over time** — query once, persist forever — so we accumulate our own history and stop depending on a vendor for data we have already paid to query.
5. **Persist backtest results** so a past scenario can be revisited rather than recomputed.

Goal 3 is the point of the whole thing. Every threshold in `config/settings.yaml` today was set by judgement, and this session demonstrated repeatedly what that costs: the `$400` price ceiling silently excluded SPY/QQQ/AMD (FC-055), the `$0.50` premium floor made four symbols structurally untradeable (FC-034), and the stage-4 gap threshold of 1.5 turned out to **lose $5,021** when finally measured (FC-036). None of those were wrong on purpose — they were unmeasured.

---

## The architectural insight this should be built around

**Most config parameters do not change the option chains. They only change which contracts get selected from them.**

`simulator.py:313` already builds chains as a discrete phase (`chains = self._build_chains(...)`) before the day loop, and `ChainStore` is already keyed by `(symbol, date, model_fingerprint)`. That splits the parameter space in two:

| class | parameters | invalidates cached chains? |
|---|---|---|
| **Selection-only** | `put/call_delta_range`, `min_put/call_premium`, `min/max_stock_price`, `min_avg_volume`, gap thresholds, profit-target DTE bands, `max_position_size`, `max_exposure_per_ticker`, `put/call_target_dte` | **No** |
| **Chain-invalidating** | risk-free rate, dividend yield, spread-model params | **Yes** — already captured by the model fingerprint |

**Nearly every scenario worth running is selection-only.** So chain building — the expensive, API-throttled, un-parallelisable part — happens **once per (symbol, date)**, and every additional scenario is a replay against data already on disk.

Order-of-magnitude, using this session's measurements: a 50-scenario sweep over 14 symbols is **~70 hours** if each scenario rebuilds chains, versus **minutes** if it replays a warm lake. That ratio is the entire business case.

---

## Proposed architecture — four separable layers

### Layer 1 — the chain lake (goal 4)

GCS-backed, partitioned by symbol/date, keyed by model fingerprint. Immutable by nature: a chain for a settled past session never changes, which is why `ChainStore` correctly has **no TTL**.

**This layer is a data asset, not a cache** — and that reframing matters for priority. Alpaca's option history begins **2024-02-01**. Every month we persist is a month of history that no longer depends on the vendor retaining it, on the subscription tier covering it, or on rate limits at query time. Three years from now this is the difference between a 2-year and a 5-year backtestable window, and **it cannot be retroactively created** — data not captured is gone.

That argues for starting persistence **early and cheaply**, ahead of the rest of the platform. Current local cache: **5,382 files / 137 MB** for ~2 years × 14 symbols → GCS storage cost is roughly **$0.003/month**. The asset is essentially free to accumulate; the cost is only in not starting.

Constraints, measured:
- **Must stay sequential.** The B2 study measured that Alpaca's trading-API contract-discovery endpoint **throttles independently and does not respond to sharding — 18 parallel processes ran *slower* than 6.** Parallelising this layer makes it worse.
- **Incremental by construction.** Consecutive monthly windows overlap ~335 of 365 days, so a refresh fetches ~30 new days per symbol, not 365.
- **New candidate symbols are a cold fetch** (goal 1) — the lake grows along both axes, symbol and date. Onboarding a candidate costs one full backfill, then it is warm forever.

### Layer 2 — the scenario runner (goals 1, 3)

Takes `(window, symbols, config-delta)` and replays against the lake with **zero API calls**. Parallelise **here** — it is pure CPU, so Cloud Run Jobs `--tasks N` or multiprocessing both work and neither hits a rate limit.

The mechanism already exists: the B1, B2 and FC-036 studies all injected config via `config._config["risk"][...]` to run threshold arms. This layer is the productisation of what those one-off harnesses did by hand (`tools/diagnostics/fc002_gap_filter_ab.py`, `fc034_premium_floor_study.py`, `fc036_gap_gate_study.py` are three worked examples of the intended shape).

### Layer 3 — the scenario store (goal 5)

`bq_writer.config_hash()` **already exists** and already hashes `put_delta_range`, `call_delta_range`, `min_put_premium`, `min_call_premium`, `max_position_size`, `max_gap_frequency`, `execution_gap_threshold`, the DTE targets and a `_scoring` block. So:

- Key results on `(config_hash, window, symbol)`
- **Dedup on re-request** — a scenario already computed returns instantly
- **Return curves are a `GROUP BY`**, not a new subsystem
- Revisiting a past scenario (goal 5) is a query, and `config_hash` makes it interpretable — the column exists precisely because *"a verdict is uninterpretable without the thresholds that produced it"*

### Layer 4 — the UI (goal 2)

**Submit-and-poll, never synchronous.** Even warm, a multi-symbol scenario is seconds-to-minutes; the existing `/backtest/screen` endpoint is disabled precisely because no synchronous HTTP request survives a real run. If the requested `config_hash` is already in the store, return immediately — which makes exploring *adjacent* parameters feel instant, and that is most of the perceived quality of such a tool.

---

## The dominant risk: this is a multiple-comparisons machine

**Goal 3 is the most valuable and the most dangerous.** Alpaca's history starts 2024-02 — **one vol regime, predominantly a bull market**. A tool that evaluates dozens of permutations and surfaces the best return curve will reliably find a configuration that looks excellent and is **fitted to that regime**. Deploying it to live trading would then be worse than the intuition it replaced, because it would carry the authority of a number.

Every study this session produced carried a single-regime caveat, and — critically — **each one's honest verdict came from a real-fills layer, not from the engine**. FC-036's engine A/B called threshold 5.0 "free"; the real-fills join proved it was a $1,900 tax that prevented nothing.

So the following are **requirements, not disclaimers**:

- **Out-of-sample by default.** Report fit-window and holdout separately; never a single blended number.
- **Show the per-symbol distribution, not just the aggregate.** FC-034's entire value was noticing that KMI's flattering +138% annualised came from **one 8-day trade in 273 days**. An aggregate hides that; a distribution cannot.
- **Keep the real-fills reality check reachable.** For any candidate config, quantify what it would have done to the **330 actual sell-to-open fills** on record — the layer that has survived every engine change this session.
- **Require a minimum activity threshold** before a config's return is displayed at all. The engine already has an `insufficient` verdict and a `days_in_position_fraction` for exactly this.

### A second, subtler bias that a UI will hide

Reported returns are a **floor**: put leg ~7% low, **call leg ~32% low** (FC-056), spread model 2.46× too wide. Comparing two scenarios is safer than reading absolute levels — **except** when the two differ in call-leg activity, because then the ~32% call understatement **biases the comparison itself**. A configuration that writes more covered calls will look **worse than it is**.

Any scenario-comparison UI must surface call-leg activity alongside return, or it will systematically mislead in a consistent direction. This should be resolved by fixing FC-056 rather than by annotating around it.

---

## Sequencing, and why this is deferred

**Prerequisite: the monthly screen must be live and verified generating real data.** That is the owner's stated gate and it is the right one — a scenario platform built on an engine whose production behaviour is unproven inherits every unknown.

Suggested order once unblocked:

1. **Start persisting the chain lake immediately** (Layer 1, standalone). It is nearly free, and it is the only component whose value is **lost by waiting** — unqueried history cannot be recovered later. This does not require the rest of the platform to exist.
2. **Split chain-materialisation from replay** behind a clean interface, and prove a warm 10-scenario sweep runs in minutes. **This is the load-bearing bet** — if it does not hold, the rest of the design does not work. It also pays for itself immediately by making FC-055 and FC-002's remaining arm cheap to answer.
3. **Layer 2 + 3** (runner + store) — the programmatic regression tests (goal 3) become possible here, with the guardrails above.
4. **Layer 4** (UI) last. It is the most visible and the least load-bearing.

Fix **FC-056** (call-leg pricing) before goal 3 drives any production parameter change, for the asymmetric-bias reason above.

---

## Open questions

- **GCS FUSE vs. explicit object access for the lake.** A mount needs no code change, but pays per-object overhead on thousands of ~25 KB files, and **GCS has no atomic rename** — which silently weakens the temp-file-plus-`os.replace` atomicity Track A deliberately added to stop a torn parquet wedging future runs. Measure the FUSE read path before adopting; consider write-once naming so there is nothing to rename.
- **Where does the search live for goal 3?** Grid, random, or Bayesian over the parameter space — grid explodes combinatorially across ~10 parameters.
- **How is a candidate symbol admitted?** Goal 1 implies an onboarding path: backfill its chains, screen it, compare against incumbents. Interacts with FC-055/FC-034, since today's filters would reject most candidates before evaluation.
- **Does the scenario store extend `backtest_runs` or get its own table?** `backtest_runs` is one row per symbol per run and already carries `config_hash` and `run_kind` — extending may be enough.

## Explicitly out of scope

- **Do not build a UI backend that reimplements strategy logic.** The engine's entire value is that it replays the *real* code (the FC-032 premise). A parallel implementation would drift, and an invisible drift is worse than no backtest.
- **Not programmatic demotion.** Separately deferred by the owner; scenario analysis informs parameters, it does not act.

**Links:** `docs/BACKTEST_ENGINE.md`, `docs/plans/fc-032.md`, `docs/plans/fc-042.md`, FC-034, FC-051, FC-055, FC-056; worked prototypes of Layer 2 in `tools/diagnostics/fc002_gap_filter_ab.py`, `fc034_premium_floor_study.py`, `fc036_gap_gate_study.py`.

---

### FC-061: share ledger is blind to open (unfilled) short-call orders — `qty_available` is the broker-truth alternative

**Status:** Consideration
**Size estimate:** S–M
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** FC-038's `_available_shares` ledger counts **filled** short-call positions only. A covered-call sell that rests unfilled across cycles (DAY limit at 5% under mid; resting-order examples in the 07-18 investigation) doesn't commit shares in the ledger, so the next cycle can select and submit a second call against the same 100 shares. The scanner is equally blind, and the deterministic `client_order_id` only dedupes identical (symbol, qty, side, price) same-day — premium drift or a different strike defeats it. **Verified interim control (2026-07-30, live probe):** Alpaca locks shares backing short calls *and* open orders — AMZN showed `qty=100, qty_available=0` — so the second order is rejected at placement; the failure mode today is a wasted selection slot plus failure-loop noise (possible same-day `_failed_symbols` blacklisting of the best contract), **not** a naked call. Raised independently by both FC-038 PR reviewers (trader R2 / reliability #3).

**The candidate fix and why it wasn't rushed into FC-038:** Alpaca's equity `qty_available` is broker-authoritative and already nets out both filled short calls and open-order holds — one field closes the gap. But a naive swap has real hazards: it double-counts if combined with the existing committed-shares subtraction (use it as a *clamp* or a *replacement*, not an addition); `AlpacaClient.get_positions` currently strips the field; the backtest adapter (`BacktestAlpacaClient.get_positions`) doesn't emit it, so the engine needs a fallback or the adapter needs to model holds; and its semantics also reflect pending equity sell orders (probably desirable, but a behavior change to reason through). Note FC-043 is why order-list-based guards were previously killed — `qty_available` sidesteps the broken `get_orders` filter entirely.

**Open questions:**
- Clamp (`min(parsed_available, qty_available)`) vs replacement? Clamp preserves offline testability and backtest parity.
- Backfill `qty_available` into `get_positions`' normalized dict for all consumers, or fetch point-wise in the helper?
- Should the scanner also consult it and stop emitting call opportunities for fully-held lots?

**Links:** FC-038 (PR #73 review disposition), FC-043 (`get_orders` filter — why order-based guards died), FC-052 (parser primitives the current ledger uses), `src/strategy/execution_engine.py` (`_available_shares`), `src/api/alpaca_client.py:244-259`.

---

### FC-066: the call roller has never executed a roll — quote-key bug, eligibility gap, stateless premium

> **CLOSED 2026-08-04 — superseded by FC-078** (`docs/plans/fc-078.md`, PR #82, merged `c379e81`): quote keys fixed, cadence daily, premium dependency deleted (credit-only makes it irrelevant), pre-revival checklist delivered (alert wiring, structured skip events, replay-BQ gate pinned). **The roller executed its first-ever roll the same day** (GOOGL C370 8/07 → C375 8/21, +$235). Entry below preserved for history.

**Status:** Consideration — **the fatal defect (1) has no other entry; FC-039 covers only cause (3)**
**Size estimate:** M
**Owner:** unassigned
**Plan file:** not yet

> **Filing note (2026-07-31):** this was investigated on 2026-07-18 and its original entry was lost in the three-session index collision (same event that lost FC-038). Refiled here. FC-062 and FC-039 both refer to "the FC-039 family" for the roller — FC-039 is *wheel state persistence*, which is only cause (3) below; causes (1) and (2) had no entry anywhere until now.

**Problem:** `CallRoller` has **never placed a single order** in production. Proven two ways on 2026-07-18: every `roll_cycle_completed` event in log retention (5 Fridays, 6/19 → 7/17) reports `rolls_evaluated=1, rolls_executed=0`, and a scan of the full Alpaca order history (459 orders) finds no option order in any Friday roll window, ever. Four stacked causes:

1. **Quote-key mismatch — fatal and silent.** `call_roller.py:102` reads `quote.get('last_price')/quote.get('ask_price')`, but `AlpacaClient.get_stock_quote` returns keys `bid`/`ask`. Price resolves to 0 and the method returns None **before any gate can log a skip reason** — which is why five Fridays produced zero diagnostic output. (`get_stock_quote` also *raises* on failure rather than returning falsy, so the `if not quote` branch is unreachable as written.)
2. **Eligibility gap.** Monitor profit-taking churn buys calls back within days and `/run` immediately re-sells a fresh ~7-DTE contract, so the call open on any given Friday is 5–7 DTE — never ≤ `rolling_max_current_dte: 1`. Separately, 52% of call sales expire mid-week, which a Friday-only job can never see. (Note the corrected mechanism: 48% of call expiries *are* Fridays, so a Friday-expiry ITM call at 15:30 would be eligible — churn, not the calendar, is the main idler.)
3. **Stateless premium needed.** `STATE_STORAGE_BUCKET` is unset, so `original_premium` resolves 0 → `debit_pct_of_premium` 999 → any debit roll rejected; `roll_count` never increments. Setting the bucket is **insufficient**: `/run` builds `CallSeller` with no wheel_state, so `set_active_call_details` never fires regardless. (This cause, and only this one, is FC-039.)
4. **No observability** on the fatal path.

**Why it matters more now:** covered calls are being written again (FC-038) on a book where positions run underwater, so the assignment-rescue case the roller exists for is live rather than theoretical.

**Fix direction:** fix the quote keys (bid/ask mid); derive `original_premium` **statelessly** from the opening sell's `filled_avg_price` in Alpaca order history rather than depending on state persistence production has never had; move eligibility (DTE ≤ 1 + ITM ratio) into the daily monitor cycle sequenced *before* profit-taking (near-disjoint states: profit-taking fires on OTM winners, rolling on ITM losers), while retaining a ~15:30 ET expiry-day check since the last monitor runs 14:55 ET; log every skip with a reason.

**Must land with FC-062** — `execute_roll` bypasses FC-050's execute-time guard entirely. Reviving the roller without that fix ships a guaranteed-loss path on the leg most likely to be near basis. *(Update 2026-08-01: the roller's floor half of FC-062 is now fixed — FC-065 Phase 2, PR #76 — so what remains of FC-062 here is the `execute_roll` gate routing.)*

**Pre-revival checklist — added 2026-08-01 from the FC-065 Phase 2 two-reviewer pass (PR #76).** Phase 2 fixed the roller's floor (shared `CostBasisResolver`, fail-closed) while the roller is still dead, so these three items are **latent today and become load-bearing the moment cause (1) is fixed**. Do them *before* the first live roll, not after:

1. **Wire both roll-path cost-basis skip events into the deployed alert policy.** `deploy/monitoring/cost_basis_alert_policy.json` matches `call_scan_skipped_cost_basis_unresolved` / `_divergent` **only** — the scan path. Phase 2 added `call_roll_skipped_cost_basis_unresolved` and `call_roll_skipped_cost_basis_divergent`, and nothing alerts on either. A roller that fails closed on every position would be silent, which is the exact FC-030 failure shape (a control nobody watches). Add both to the filter's `jsonPayload.event_type` **and** `textPayload` clauses — Cloud Run emits plain-text logs with empty severity, so the payload-shape duplication is required, not belt-and-braces.
2. **Give the pre-guard silent skips structured events.** `call_roller.py:126` (falsy quote), `:129` (zero/missing price) and `:210` (zero BTC ask) all `return None` with **no log line at all**. Friday 2026-07-31's live run is the proof this matters: `rolls_evaluated=3` with **zero per-position events emitted** — the quote-key bug (cause 1) kills every evaluation at `:127-129`, upstream of the floor guard. The operational trap: after Phase 2, "no floor-skip events in the logs" reads as "every floor resolved cleanly" when it actually means "nothing ever reached the floor." Every early return on this path needs a reason code before the roller is trusted.
3. **The roller's replay BigQuery-gate forwarding is now load-bearing.** `WheelEngine` forwards `allow_bigquery_cost_basis` to `CallRoller` (PR #76), because `run_rolling_cycle()` runs inside the backtest replay and the roller now owns a resolver that can query production trade history against `CURRENT_TIMESTAMP()`. Both halves of that link are pinned by test (engine→roller, roller→resolver), each mutation-verified. Do not remove the forwarding when reviving the roller; the hazard is latent only because the path dies upstream.

**Links:** FC-062 (roller floor — the floor half is done, PR #76; `execute_roll` gate routing remains), FC-039 (cause 3 only), FC-011 (item 1 — delivered by the cadence change), FC-033 (escalation interaction), FC-071 (at-floor scoring asymmetry), `docs/investigations/covered-call-starvation-2026-07-18.md`, `docs/plans/fc-065.md` §Phase 2, `src/strategy/call_roller.py:102-104,148,487,506-508`, `src/api/alpaca_client.py:317-324`.

---

### FC-067: the trade journal labels every covered call as a put

**Status:** CLOSED 2026-08-21 — fixed by PR #88 (`b02f48d`), hardened by its adversarial review to derive the leg **OCC-symbol-first** (`strict_option_type(option_symbol)` → `option_type` → `type` → infer-from-strategy; strategy follows the resolved leg; absent leg → null, never a guessed 'put'), because `execute_batch` routes by the symbol on declared/type drift (FC-048) and the audit row must record the leg actually traded. Root cause: scanner opps carry `type`, not `option_type`/`strategy`, so `record_trade`'s defaults fired on EVERY row. Forward-only — historical mislabels corrected 2026-08-24 via the Seam 4 R5 rider: **62 rows** (grown from the entry's 29 while the fix sat merged-but-undeployed, FC-081) UPDATEd to `call`/`sell_call`; snapshot/rollback table `options_wheel.fc067_label_snapshot_20260824`; audit-after 0; census now 115 put / 63 call. Tests: `tests/test_trade_journal_labeling.py` (7). Was the hard prerequisite for FC-075 Phase 2.
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not yet

> **Filing note (2026-07-31):** investigated 2026-07-18, entry lost in the same index collision as FC-038/FC-066. Refiled.

**Problem:** `TradeJournal.record_trade` defaults `option_type` → `'put'` and `strategy` → `'sell_put'` (`src/data/trade_journal.py:148,161`). Scanner-produced call opportunities carry `type: 'call'` but neither of those keys, so the defaults win. Verified by OCC-symbol regex against the labels: **29 rows in `options_wheel.trades` whose symbol is a C-contract are labeled `option_type='put', strategy='sell_put'`**; zero rows are labeled as calls.

**Why it matters beyond tidiness:** this mislabeling actively derailed two investigations. On 2026-07-17 it made a covered-call roll-down chain read as put activity and sent the analysis toward the wrong subsystem entirely; the correction took a second full investigation the next day. Any future analysis that segments by `strategy` or `option_type` on this table silently sees a put-only book.

**Fix direction:** derive `option_type` and `strategy` from the **OCC symbol** inside `record_trade` — the symbol is authoritative and this retro-fixes any future producer that forgets the keys — using the canonical `strict_option_type` / `parse_option_symbol` primitives (FC-048/FC-052 family; six OCC-substring bugs say do not hand-roll this). Backfill the 29 existing rows under the synthetic/corrective-write discipline in `~/CLAUDE.md` (prefix-tagged, audit query, rollback recipe documented in the plan).

**Open questions:**
- Backfill only `strategy`/`option_type`, or also normalize `expiration`/`dte`, which are null on many rows?
- Does any dashboard query read these columns today, or is the damage confined to ad-hoc analysis? (`trades_from_activities` is the dashboard's source, so likely the latter — verify before sizing.)

**Links:** `docs/investigations/covered-call-starvation-2026-07-18.md`, FC-048/FC-052 (parser primitives), FC-046 (the other half of this observability gap), `src/data/trade_journal.py:148,161`.

---

### FC-065: FC-029's drawdown pause (R3) is also dead on the production path

**Status:** Executing — **Phase 1 (floor inversion) merged + deploy-verified 2026-08-01** (PR #75, squash `630c0bd`; two adversarial reviews + confirmation pass; floors live at avg_entry_price 303.50/261.20/368.34/218.43, cross-check `status=ok` all four lots). Phases 2 and 4 next. Scope: floor = Alpaca `avg_entry_price` with the resolver chain inverted, fail-closed everywhere including a divergence cross-check with teeth; roller floor unified; durable per-symbol decision record (subsumes FC-044 Phase 1, repoints the FC-030/031 pause alert to `uncovered_days`). **The drawdown pause is deliberately NOT ported** (OQ-3: floor-only gating; legibility via decision-record labels). Engine-path deletion + backtest repoint split out as FC-068 (now blocked only on Phase 1).
**Size estimate:** M–L (four phases, one PR each)
**Owner:** Claude / zeshan
**Plan file:** `docs/plans/fc-065.md`

**Problem:** FC-050 restored FC-029's **R2** (cost-basis floor) on the `/scan → /run` path production actually executes. Checking whether FC-029's other remediations landed on the used path — a question FC-050's own entry raised — the answer for **R3 (drawdown pause) is no.** The pause exists solely at `src/strategy/call_seller.py:134` inside `evaluate_covered_call_opportunity`, the `wheel_engine` path production never calls; `src/data/options_scanner.py` contains **zero** drawdown references (`grep -c drawdown` → 0). So `call_drawdown_pause_threshold: 0.05` has never gated a production covered call, and neither has the companion defer-on-bad-quote behavior. Same structural cause as FC-045/FC-048/FC-050: a remediation built on the unused half of a two-path system.

**Live illustration, 2026-07-31:** a GOOGL 8/7 $370 call was written while GOOGL sat at $353.55 — **4.0% below** its $368.34 basis. Not a rule violation (under the 5% threshold, and the strike is at/above the floor, so called-away is still profitable), but it demonstrates the mechanism is absent rather than merely unfired. The same position at 10% underwater would still get a call, because nothing on the scanner path consults the threshold.

**Partially compensated, which is why it went unnoticed:** the cost-basis floor implicitly excludes deeply-underwater symbols — NVDA (9.3% under basis, floor $220) produced no call opportunities the same day. FC-029's R3 rationale explicitly anticipated this ("when shares are deeply underwater every delta-range strike is at-or-below cost basis") and added the pause anyway, so the decision would be **explicit and observable** rather than an emergent side effect. That observability is what is missing.

**Fix direction:** port the pause (and the defer-on-unusable-quote behavior) to the scanner path, reusing FC-050's `CostBasisResolver` for the basis and emitting the existing `covered_call_drawdown_pause` / `covered_call_quote_missing` events so the backtest's rejection taxonomy (`src/backtesting/engine/rejections.py:44`) and any dashboard consumer keep working. Consider whether the pause belongs in the scanner (fewer opportunities emitted) or in selection (opportunity emitted, dropped with a reason) — the latter fits FC-038's drop-reason model.

**Open questions:**
- Scanner-side skip vs selection-side drop-with-reason?
- Is 5% still the right threshold now that the floor is the raw assigning strike (FC-050) rather than the premium-adjusted broker basis — the two interact, since the floor rose for every position?
- Does FC-033 (drawdown-pause escalation) presuppose a pause that has never actually run? Its premise needs re-examination in light of this.

**Links:** FC-029 (R3), FC-050 (restored R2; raised this question), FC-033 (escalation — premise depends on this), FC-030/FC-031 (pause observability + alerting, which consume pause state that production never produces), `src/strategy/call_seller.py:120-143`, `src/data/options_scanner.py`.

---

### FC-068: delete the dead engine call path; repoint the backtest to the real pipeline

**Status:** Consideration — **blocked on FC-065 Phase 1 only** (the deleted path's floor semantics must be replaced first). *The former Phase-3 blocker dissolved 2026-07-31: the operator decided the drawdown pause is NOT ported (FC-065 OQ-3, floor-only gating), so deletion orphans nothing — the pause tests and the `covered_call_drawdown_pause` taxonomy entry are removed with the path, deliberately.*
**Size estimate:** M (small code delta, large measurement consequence)
**Owner:** unassigned
**Plan file:** not yet (split out of `docs/plans/fc-065.md` Phase 5, 2026-07-31)

**Problem:** `CallSeller.evaluate_covered_call_opportunity` and its orchestration (`wheel_engine._find_new_opportunities` / `_manage_existing_positions`) have not been called by production since 2025-10-03 (`842dcce`), yet the backtest simulator replays exactly that path — so every backtest measures a strategy with a drawdown pause, phase gating, and single-candidate selection that production does not have, and without the committed-share accounting it does have. Ten months of remediations landed on the unused half (FC-029 R2/R3, FC-048's origin, FC-050's root cause, FC-065's finding).

**Scope:** port-then-delete (sequencing owned by FC-065); repoint `simulator.py` to replay `scan → select → execute`; migrate — not delete — the 27 FC-029 guard tests and the FC-048 golden replay; re-run affected backtest verdicts, since pre/post numbers are non-comparable (FC-048 precedent).

**Links:** `docs/plans/fc-065.md` (§Phase 5 rationale), FC-048, FC-029, `src/backtesting/engine/simulator.py:322-330`, `src/strategy/wheel_engine.py`, commit `842dcce`.

**Carried in from the FC-065 Phase 1 review (PR #75, reviewer 2, LOW-4) — do not lose this on repoint:** `OptionsScanner` hardcodes `allow_bigquery=True` when it builds its `CostBasisResolver` (`src/data/options_scanner.py:37`). Today that is correct, because only production runs the scanner. The moment FC-068 repoints the simulator to replay `scan → select → execute`, the flag must be **threaded through to the scanner** — otherwise every backtest replay issues the divergence cross-check against *production* trade history (`options_wheel.trades_from_activities`, against `CURRENT_TIMESTAMP()`), which is exactly the leak `allow_bigquery=False` exists to prevent on the seller path.

---

### FC-069: decommission the fictional layer — revive-or-delete every dead control, then rewire what remains for continuity

> **CLOSED 2026-08-04 — EXECUTED IN FULL, all five PRs merged + deployed + live-verified in one day** (plan + per-PR execution records: `docs/plans/fc-069.md` §Execution): **S1** knob sweep PR #83 (`de64b51` + hotfix `388470d`) — dead policy layer, gap module (SHA pointer `afb6698`), card-16 `/regression` sync; **S2** `wheel_cycles` retirement PR #84 (`8726ab1`) — writer stopped, table snapshotted (78 rows, ≥30d) and **dropped**; **S3** scanner rewire PR #85 (`d1e3fc2`) — the one live behavior change, collision set proven {F⊂MSFT, F⊂PFE}, skip event live, `ENGINE_VERSION → fc-069-scanner-rewire`; **S5** orphan deletion PR #86 (`7073f9f`); **S6** WheelStateManager shrink PR #87 (`8d487b5`) — 747→331 lines, FC-039 closed; **item 14** docs rewrite direct-to-main (`beff842`). Ten adversarial reviews + three confirmation passes across the five PRs; suite 1237 → 1262. FC-014/FC-015/FC-039 closed with lineage; **FC-079 filed** (reconcile-path OCC-substring instances found during S2). Decisions executed exactly per the §Operator sign-off record; item 4 was already live via FC-013 and item 13 resolved via FC-078 before execution began. Entry below preserved for history.

**Status:** **Plan SIGNED OFF by operator 2026-08-01** (docs/plans/fc-069.md §Operator sign-off record — all 17 cards decided: item 1 flipped to DELETE, item 4 revived via FC-013 rev 2.1, item 7 sub-decision spawned FC-074, card 16 SYNC, item 5 module deleted with SHA pointer, rest confirmed). Execution queued after FC-068 lands; S1–S6 partition as amended (S4 dropped).
**Size estimate:** L (many S items; the cost is decisions, not code)
**Owner:** zeshan + Claude
**Plan file:** not yet

**Problem / opportunity:** the three-agent covered-call investigation (2026-07-31) established that production runs a *stateless* wheel while the codebase maintains a parallel *fictional stateful* one, plus a layer of config knobs and risk controls that read as live but are consumed by nothing on the path that trades. FC-065 and FC-068 remove what sits in their blast radius (the floor chain, the dead engine decision path); everything below survives them. Each item is a **decision, not just a deletion** — several are risk controls the operator may want *revived*: production currently has **no global position cap, no per-ticker exposure cap, and no earnings gate** on the live path. End state: every mechanism in the repo either runs on the path that trades, or does not exist — and the docs describe the system that actually runs.

**Inventory (pre-tagged recommendations — confirm or flip each at plan time):**

| # | Item | Today | Recommendation |
|---|---|---|---|
| 1 | `max_total_positions: 10` | Read only by dead `_can_open_new_positions`; **no global cap exists** | **REVIVE** — enforce in `select_batch` |
| 2 | `max_exposure_per_ticker: 40000` | Dead (only consumer is retired `validate_new_position`) | **DELETE** — structurally implied: one put per underlying × `max_stock_price 400` × 100 = exactly $40k; document the equivalence |
| 3 | `max_positions_per_stock: 1` | Dead knob; invariant emergent from Stage-6 block + share ledger | **DELETE** knob; document the emergent invariant in `docs/gates.md` (FC-013's artifact) |
| 4 | Earnings blackout (`earnings.*`, `earnings_avoidance_days`, `EarningsCalendarService`, FINNHUB key) | Never on the live sell path; only consumer is the dead roller | **REVIVE** via FC-013 (its published plan already specifies wiring both sellers) — or delete service+knobs+key; this FC forces the choice |
| 5 | Gap controls (`execution_gap_threshold: 999` shadow, stage-2 unwired) | Stage-4 wiring dies with FC-068; detector + knobs remain | **DELETE** the shadow knob; resolve stage-2 per FC-049's study (revive only with evidence) |
| 6 | `min_hold_hours: 4` + `_entry_times` (both sellers) | Dead since inception (per-request instances); DTE bands carry the load (05-12 evidence) | **DELETE** — absorbs FC-015 (if ever wanted, derive from Alpaca `submitted_at`, don't persist) |
| 7 | `RiskManager.validate_new_position` | Zero call sites; contains the original substring bug | **DELETE** — absorbs FC-014 (keep `validate_roll`, the roller uses it) |
| 8 | `WheelStateManager` fictional layer (phases, premium accumulators, roll counts, GCS persistence, `can_sell_*`) | Post-FC-068 consumers: reconcile telemetry + dead roller; persistence never worked | **DELETE/SHRINK** to what reconciliation telemetry actually needs — absorbs FC-039 per the coherence principle: derive from durable truth, delete the fiction. Coordinate with FC-066 (roller goes stateless-from-Alpaca anyway) |
| 9 | `call_drawdown_pause_threshold` knob + its `/config` dashboard exposure | Pause deliberately not built (FC-065 OQ-3); knob now permanently dead | **DELETE** knob + `/config` field |
| 10 | `wheel_cycles` writer in `reconcile_positions` | Writes duplicate zero-gain rows ~6×/day/assignment (36/31/7 verified); dashboard reads the activities view instead | **STOP writing; drop table** (FC-035 precedent: `order_statuses`) |
| 11 | Opportunity blob `expires_at` | Written, never read (operative gate is `opportunity_max_age_minutes`) | **DELETE** field |
| 12 | 7th OCC-substring instance: `_has_existing_position` (`options_scanner.py:575`, put-side, `symbol in position['symbol']`) | Over-blocks (any PFE option blocks F puts) | **REWIRE** to canonical parsers — completes the FC-052 family |
| 13 | `rolling.*` knobs (11) | Roller structurally dead pending FC-062/FC-066 | **HOLD** — fate decided by FC-066's revival decision, not here |
| 14 | `docs/CLAUDE.md` Risk Management Philosophy + architecture claims | States call stop-losses protect positions (false since FC-010) and `RiskManager` "validates all positions" (false always) | **REWRITE** to describe the system that runs |
| 15 | Process-local fragile state (`_closed_today`, `_failed_symbols`, `strategy_status`) | Live but amnesiac across cold starts | **NOTE only** — FC-009 owns `_closed_today`; others documented as accepted |

Explicitly out of scope: call-side limit pricing drift (`mid × 0.95` vs the put side's spread-aware pricing) — a behavior *improvement*, not dead code; deserves its own S-sized FC so its economics get reviewed on their own.

**Sequencing:** after FC-068 (the deletions there determine what is actually left to sweep). Items 1 and 4 are the two genuine policy revivals and should lead the plan discussion.

**Absorbs / supersedes on completion:** FC-014, FC-015, FC-039 (each closes with lineage to this FC's decision on it).

**Links:** `docs/plans/fc-065.md` (three design principles + spin-offs), FC-068, FC-013 (earnings plan, published), FC-049 (gap study), FC-066 (roller), FC-009, the 2026-07-31 mechanism-inventory/coherence investigations recorded via `docs/plans/fc-065.md` Context.

---

### FC-070: pin Alpaca's partial-disposal `avg_entry_price` semantics; consider asymmetric divergence tolerance

**Status:** Consideration — filed 2026-08-01 from the FC-065 Phase 1 review (PR #75, reviewer 2, HIGH-1)
**Size estimate:** S (the empirical pin is one observation; the tolerance change is a small, reviewable diff)
**Owner:** unassigned
**Plan file:** not yet

**Problem:** FC-065 Phase 1's divergence cross-check reconstructs the expected basis by consuming OPASN lots **newest-first** — FIFO-shaped, on the assumption that a partial disposal removes the oldest shares. Alpaca's `avg_entry_price`, however, is an **entry average that is not recomputed when shares are partially sold**. The two therefore measure different quantities the moment a multi-lot position is partially called away: the broker keeps the blended entry average across *all* lots ever acquired, while the reconstruction prices only the newest remaining ones.

**Consequence, and why it is not urgent:** the first multi-lot partial call-away will produce a **false divergence** roughly equal to the spread between the lot bases, and the symbol will fail closed — writing no calls — until the position fully cycles out. The direction is safe (fail-closed, no unprotected write, no money at risk) and the runbook now documents it as benign cause (a) with a sanctioned remediation path, but it is a real false-positive that stops premium collection on a live symbol.

**Why it was not pinned in Phase 1:** confirming the semantics empirically requires either a **deliberate partial sell** — an operator action, since this pipeline never places orders — or waiting for a natural occurrence. Neither is available to a hermetic build. All four live lots are single-lot today, so the case has not yet arisen. Reviewer 2 sanctioned deferring it explicitly, conditional on the runbook note landing with Phase 1 (it did).

**Fix directions, in order of cost:**

1. **Pin the semantics.** Observe one partial disposal — natural, or an operator-initiated partial sell on a paper lot — and record what Alpaca reports for `avg_entry_price` before and after. That single observation decides whether the reconstruction should be FIFO, entry-average, or something else.
2. **Asymmetric / two-tier tolerance (reviewer 2's design alternative).** The two directions are not symmetric in risk: when the **broker's basis sits above** the reconstruction, the floor is *stricter* than the history implies — the conservative direction, which cannot cause an unprotected write. That direction could be tolerated far more loosely at **zero risk cost**, while the dangerous direction (broker below reconstruction — the split/corruption case the check exists for) keeps today's tight bound. This roughly halves the false-positive surface and specifically absorbs the partial-disposal case, since a blended entry average across lots sits above a newest-lots reconstruction whenever the newer lots are cheaper.

Doing (2) without (1) is defensible — it is conservative in the direction that matters — but (1) is what turns the tolerance choice from a guess into a measurement.

**Links:** PR #75 (FC-065 Phase 1 review, reviewer 2 HIGH-1), `docs/plans/fc-065.md` §Phase 1, `src/strategy/cost_basis.py` (`reconstruct_expected_basis`, `_cross_check`), `deploy/monitoring/drawdown_pause_alert.md` §"Divergence triage" cause (a).

---

### FC-071: at-floor candidates are flagged at-or-above basis but scored below it

**Status:** Closed — merged 2026-08-03 (PR #80, `965e819`). Aligned to `>=` per operator decision; dual-APPROVE reviews. FC-073 records the consequence (basis component now constant in production scoring). Plan: `docs/plans/fc-071.md`.
**Size estimate:** XS (one comparison operator; the work is the decision, not the diff)
**Owner:** unassigned
**Plan file:** not yet

**Problem:** FC-065 Phase 2 reconciled `assignment_above_cost_basis` with the gates by changing it to `strike >= cost_basis_per_share` (`options_scanner.py:447`) — at-floor writes are permitted and correct, so the flag now says so. But the **scoring** input three lines earlier was left alone: `options_scanner.py:426` still passes `strike > cost_basis_per_share` into `_calculate_call_attractiveness_score` as its `above_cost_basis` argument, which awards **15 points when true and 5 when false** (`:569-573`).

So an at-floor candidate is now flagged at-or-above basis and simultaneously scored as if it were below it — a 10-point penalty out of 100 on a strike the plan explicitly calls "genuinely profitable" (GOOGL's 370C). The two readings of the same predicate disagree by construction.

**Why it was not fixed in Phase 2:** the plan named only the flag, and FC-065's non-goals exclude changing scoring. A ranking change is a behavior change that deserves its own review rather than being swept in under a consistency fix. Deliberately left for a decision — recorded here so it is not rediscovered as a bug.

**Why it is ranking-only, not a safety issue:** the score orders candidates, it does not gate them. No floor, delta band, premium floor or DTE check consults it. The worst case is that an at-floor strike loses a close ranking contest to a slightly-above-floor one — a mild conservative bias, not an unprotected write. Nothing here can cause a below-basis call.

**Fix directions:**

1. **Change `:426` to `>=`** so both readings agree. Simplest, and consistent with the decided policy that at-floor is a good write.
2. **Leave it, deliberately, and document the asymmetry** — arguing the bonus should reward genuine capital gain, of which an at-floor write has exactly none. Defensible: the flag answers "is this permitted?" and the score answers "how attractive is it?", which are different questions.
3. **Retire the binary bonus entirely** in favour of scoring the actual capital gain per share (continuous), which makes the boundary question disappear rather than moving it.

Option (1) is the smallest change; option (3) is the one that stops this recurring. **Needs an operator decision** — it is a preference about ranking, not a defect with a right answer.

**Links:** PR #76 (FC-065 Phase 2 review, reviewer 1 INFO), `docs/plans/fc-065.md` §Phase 2 + Execution, `src/data/options_scanner.py:426,447,569-573`, FC-065 (the at-floor policy: "At-floor writes remain allowed").

---

### FC-072: call-side limit pricing never received the put side's spread-aware improvement

**Status:** Consideration — filed 2026-08-01 (spin-off from `docs/plans/fc-065.md` §Spin-offs, deliberately excluded from FC-069 so its economics get reviewed on their own)
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not yet

**Problem:** the put side prices sell-to-open limits spread-aware — `mid + 10% of spread`, biased toward the ask for premium collection (`put_seller.py:318-327`, falling back to `mid × 0.95` only when bid/ask are unusable). The call side never got that improvement: every covered-call open is priced at a blunt `mid × 0.95` (`call_seller.py:393`) — roughly 5% of mid donated per open, plus half the spread. A Symmetry Principle violation with a direct, recurring premium cost across every call write (~88 historical writes and counting).

**Fix direction:** port the put side's spread-aware formula to `execute_call_sale`'s limit calculation, with the same unusable-quote fallback. Behavior change on a money path → plan file + two-reviewer gate per ~/CLAUDE.md; the economics (expected $/write recovered, fill-rate risk of pricing closer to the ask) should be estimated from fill history before shipping.

**Links:** `docs/plans/fc-065.md` §Context item 4 + §Spin-offs, `src/strategy/call_seller.py:393`, `src/strategy/put_seller.py:318-327`, FC-069 (explicitly out of its scope), Symmetry Principle (`docs/CLAUDE.md`).

---

### FC-074: should an account-level kill switch exist? (decide deliberately — the dead one is being deleted)

**Status:** Consideration — filed 2026-08-01 by operator decision (FC-069 item 7 sub-decision, option 2)
**Size estimate:** S (the decision); S–M (the build, if REVIVE)
**Owner:** zeshan + Claude
**Plan file:** not yet

**Problem:** FC-069's sweep deletes `RiskManager`'s dead sibling methods — the repo's only *account-level* loss-limit concepts: a 10% portfolio-unrealized-loss "reduce positions" trigger, a 15% portfolio-loss emergency stop, a 5% daily-loss threshold, and an 80% margin ceiling. All were dead code (zero call sites since inception; two of the four conditions were declared but never even computed), with hardcoded thresholds nobody chose. The operator chose to delete them **with this FC filed** rather than letting the concept vanish as a rider: FC-065 OQ-3 decided the *per-symbol* pause question; the *account-level* stop was never decided.

**The question:** should any mechanism exist that halts new opens (or flattens risk) when the account as a whole is losing badly — e.g., portfolio drawdown ≥ X%, or daily P&L ≤ −Y%? Post-sweep, the account's protections are position-scoped (sizing, floors, one-per-underlying, BP exhaustion) plus the earnings gate (FC-013 rev 2); nothing reacts to aggregate drawdown. That lean posture is currently *chosen* (control-matrix sign-off, 2026-08-01) — this FC exists so it gets re-examined with real thresholds and real data rather than re-accumulated.

**Inputs when taken up:** FC-031's equity-curve/drawdown data (dashboard KPIs); the FC-065 P4 decision-record history (how often and how deep the book actually draws down); the FC-030 alert channel (a detective-only "account down X% today" alert is the cheapest first step and may be the right *entire* answer — alert-then-human beats an untested automatic halt for a one-operator book).

**Links:** FC-069 item 7 (the deletion + this sub-decision), FC-065 OQ-3 (per-symbol pause — deliberately not this), FC-033 (Deferred — per-symbol escalation, different scope), `docs/plans/fc-069.md` §control matrix, FC-030 (alert channel).

---

### FC-073: validate the attractiveness score against realized outcomes; tune or simplify

**Status:** Consideration — filed 2026-08-01 at operator request
**Size estimate:** M (the cost is analysis, not code; any weight change is then S)
**Owner:** zeshan + Claude
**Plan file:** not yet

**Problem:** the scanner's attractiveness scores (`_calculate_call_attractiveness_score` / `_calculate_put_attractiveness_score`, `src/data/options_scanner.py:472-591`) decide which strike wins per symbol and — on the call side — which symbols win at selection (`ExecutionEngine._call_rank_score`), yet **the weights are inherited, not chosen, and have never been validated against realized outcomes.** Known structural issues, all verified 2026-08-01:

- **The return component saturates and stops discriminating.** Calls: `min(35, annual_return × 3)` caps at ~11.7% annualized — nearly every 7-DTE candidate clearing the $0.50 premium floor maxes it, so delta-proximity-to-0.20 and the OTM band do almost all the intra-symbol ranking work. Puts: same shape (`min(40, annual_return × 2)`).
- **The delta component peaks mid-band** (exactly 0.20; the configured band edges 0.15/0.25 score 15/20) — an implicit preference inside the band that nobody decided.
- **The put score is computed but unused at selection** (puts are re-ranked by ROI per FC-038) — it only orders the blob; either give it a job or document that it's cosmetic.
- **The at-floor bonus asymmetry is FC-071** (strict `>` into the 15-vs-5 bonus) — resolved 2026-08-02 (aligned to `>=`, dual-APPROVE, queued behind the Monday train). **Consequence for this FC (FC-071 trader review):** post-alignment the basis component carries *zero ranking information in production* — every scored candidate is ≥ floor by construction, so all real candidates collect the same +15 and the `False → +5` arm is dead outside unit tests. Correct end state (safety is the gates' job), but this review should weigh whether a constant 15-point offset earns its slot in the 0–100 budget or should be folded out.
- Liquidity (10 pts, saturates at ~1,000 blended contracts) and DTE (5 pts) are near-tiebreaks; whether that's right is undecided.

**Evaluation approach (the data now exists or is landing):**
1. **Historical join:** score-at-scan (opportunity blobs in `gs://options-wheel-opportunities`, ~1,300 objects) × realized outcomes (`trades_from_activities` / `trades_with_outcomes`) — did higher-scored candidates deliver better premium-per-day, fewer called-away-at-loss events, better fill rates? FC-065 P4's `decision_events` (run_id-joined) makes this query clean going forward.
2. **Counterfactual replay:** the post-FC-068 backtest replays the literal production pipeline including this score — re-run the effective universe under alternative weightings (e.g., de-saturated return component, flat-in-band delta) and compare cycle outcomes. Non-comparability discipline per FC-048/FC-068 applies.
3. **Decision:** tune weights, simplify (a score most of whose components don't discriminate may reduce to delta+OTM), or keep-and-document — each with the evidence attached. Any behavior change ships under its own plan + two-reviewer gate (money-path ranking).

**Sequencing:** after FC-065 P4 and FC-068 land (both are the measurement instruments); independent of FC-069.

**Links:** FC-071 (at-floor bonus — subset decision), FC-038 (call-rank-by-score / put-rank-by-ROI asymmetry), FC-044 (grid UI reading `decision_events`), FC-048/FC-068 (replay comparability precedent), `docs/BACKTEST_ENGINE.md`, `src/data/options_scanner.py:472-591`, `src/strategy/execution_engine.py:203-215`.

---

### FC-062: the roller has its own fail-open cost-basis floor and bypasses the execute-time guard

> **CLOSED 2026-08-04 — both halves delivered**: the floor half by FC-065 Phase 2 (PR #76 — roller floor via shared `CostBasisResolver`, fail-closed); the `execute_roll` routing half by FC-078 (PR #82 — execute-time floor pinned on stored ladder candidates, commit `7326df1`, mutation-verified). Entry below preserved for history.

**Status:** Consideration — **must be resolved before any roller revival ships**
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not yet

**Problem:** FC-050 established one shared cost-basis resolver and a fail-closed execute-time guard — but `CallRoller` is a **third** floor implementation that neither uses. `call_roller.py:128-132` derives the floor from raw Alpaca `cost_basis` (not the FC-029 chain) and, when that returns 0, computes `min_strike = max(0, current_strike + 0.01)` — i.e. it **fails open**, replacing a cost-basis floor with "anything above the old strike". Worse, `execute_roll` sells the replacement call by calling `place_option_order` directly, so it **never passes through `execute_call_sale`** and inherits none of FC-050's protection.

**Why it is not urgent today and is urgent soon:** the roller has never executed a roll in production (FC-039 family: quote-key bug, Friday/DTE eligibility gap, state amnesia), so this guard has never been exercised. But the roller revival is queued, and it would ship exactly the dead/absent-guard class that FC-050 exists to eliminate — on the path where a *rolled* call is most likely to be near or below basis, since rolling is triggered by the stock rallying through the strike.

**Fix direction:** route the roller's floor through `CostBasisResolver` (FC-050's shared module) and make the zero case fail closed — refuse the roll rather than substituting a strike-based pseudo-floor; and either route the replacement leg through `execute_call_sale` or apply `opportunity_floor_per_share` at the roller's own order site. Sequence this with, or ahead of, the FC-039 roller work.

**Links:** found in the FC-050 PR #74 two-reviewer pass (reviewer 1, MEDIUM). Related: FC-050 (the shared resolver + execute-time gate), FC-029 (R2 chain), FC-039 (roller repair — this must land with it), `src/strategy/call_roller.py:128-132`, `execute_roll`.

---

### FC-063: type the scan→execute opportunity boundary — three bugs now share this root cause

**Status:** Consideration
**Size estimate:** M
**Owner:** unassigned
**Plan file:** not yet

**Problem:** opportunities cross from producers (`OptionsScanner`, `CallSeller.evaluate_covered_call_opportunity`, `PutSeller`) to consumers (`ExecutionEngine`, `CallSeller.execute_call_sale`) as **untyped dicts with per-producer key vocabularies**, and every consumer hardens against the shapes it happens to know. That has now produced three separate production defects: **FC-045** (`/monitor` misrouting call closes), **FC-048** (execution routing sending calls down the put path), and **FC-050** (the below-basis guard reading `stock_cost_basis`/`shares_covered` while scanner opportunities carry `cost_basis_per_share`/`max_contracts` — a guard that consequently never ran, and by BigQuery evidence let 15 below-basis calls through, 3 of which were called away for −$9,000).

FC-050 added `opportunity_floor_per_share()` — a third place encoding shape knowledge — because hardening the consumer was the right *urgent* move for a money-losing guard. Both FC-050 reviewers independently flagged that the pattern is now the dominant residual risk: "three instances is a pattern, not a coincidence."

**Fix direction:** a typed boundary — dataclass or TypedDict with a normalizing constructor per producer — so a missing or renamed field is a construction-time error rather than a silently-defaulted `0`. Deliberately deferred once already in `docs/plans/fc-048.md` as out-of-scope; this entry exists so the third deferral is a decision rather than an oversight.

**Open questions:**
- Dataclass with `from_scanner()` / `from_seller()` constructors, or a validating TypedDict at the consumer edge?
- Migration: strangler (normalize at the two consumer entry points first) or big-bang across producers?
- Does the GCS opportunity blob become a versioned schema, given `/run` deserializes what `/scan` wrote up to 30 minutes earlier (and across deploys)?

**Links:** FC-045, FC-048, FC-050 (`docs/plans/fc-050.md` Open Question 1; both PR #74 reviewers), `src/data/options_scanner.py`, `src/strategy/execution_engine.py`, `src/strategy/call_seller.py`.

---

### FC-064: cost-basis floor should be `max(BQ assigning strike, broker basis)` for mixed lots

**Status:** Closed — resolved by FC-065 Phase 1 (2026-08-01, PR #75). The floor is now Alpaca `avg_entry_price` — already the broker's weighted average across lots, which is exactly the semantic the mixed-lot case needed — and BigQuery no longer competes to set the floor (it is a divergence cross-check that fail-closes, with share-count mismatch as its own signal, answering this FC's second open question). The `max()` proposal was a workaround for two sources measuring different quantities; the source unification dissolves it. Lineage: `docs/plans/fc-065.md` §"The floor definition, decided" + §Spin-offs.
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not yet

**Problem:** FC-050's resolver returns the **first** positive source in FC-029's order (wheel_state → BigQuery last-OPASN put strike → Alpaca `cost_basis`). Because wheel-state persistence has never worked (FC-039), production resolves from **BigQuery** for every held symbol with OPASN history — the raw assigning-put strike. For a clean wheel lot that is conservative (raw strike ≥ premium-adjusted broker basis; verified on all four live positions 2026-07-30, where every floor *rose*: AAPL 303.50→305.00, AMZN 261.20→262.50, GOOGL 368.34→370.00, NVDA 218.43→220.00).

**The unsafe case is a mixed lot.** Assigned 100 shares at $200, later acquire more at a higher price so the *average* basis is $220: BigQuery still resolves $200, so a $205 call passes both the scanner floor and the execute-time guard while being below the account's true basis — a real loss the pre-FC-050 scanner (which used Alpaca's average) would have blocked. Raised by reviewer 2 on PR #74; reviewer 1 independently confirmed it is **unreachable today** (`_has_existing_position` blocks put sales on symbols already held, and account history shows zero manual stock purchases), which is why it was not changed mid-refactor.

**Fix direction:** resolve to `max(bq_strike, alpaca_basis_per_share)` when both are positive — identical to today's behavior for clean wheel lots (basis = strike − premium ≤ strike) and protective only where they diverge. FC-050 shipped the observability prerequisite: `cost_basis_resolved_via_fallback` now carries Alpaca's value and the delta, so the divergence can be measured on real data before the semantics change.

**Open questions:**
- Does `max()` belong in the resolver (changing FC-029 semantics globally) or only at the scanner's call site?
- Should a share count that disagrees with the OPASN quantity be its own signal (partial sale, mixed lot) rather than being inferred from a price delta?
- After FC-039 restores wheel_state, does source 1 make this moot, or does wheel_state carry the same single-assignment assumption?

**Links:** FC-050 Open Question 2 (`docs/plans/fc-050.md`; both PR #74 reviewers), FC-029 (R2 source order), FC-039 (wheel state), `src/strategy/cost_basis.py`.

### FC-075: Standalone covered-call strategy — separate account, shared machinery

**Status:** LIVE (paper) 2026-08-24 — Phases 0–3 DONE. Seam 4 merged+deployed 08-22 (PR #90 `cebb09b`; R1–R6 complete, `docs/plans/fc-075-seam-4.md`); Phase 3 provisioned + **interlock drill PASSED** 08-23 (`covered-call-engine` live, own dataset/bucket/schedulers, no `/roll`); go-live 08-24: first lots GOOGL 100 @ 348.02 + UNH 100 @ 398.08 in `PA37XLNWDLB3`, first cycle = clean fully-explained no-trade (`below_cost_basis` dominant — spot ≈ day-one basis; then premium/delta/OI). **First covered call pending qualifying strikes** (compressed shadow was an operator decision, recorded in `fc-075.md`). Remaining: first written call (organic), tunables iteration from live decision rows, cloudbuild deploy step for the CC service (image pinned to `27308f4` until then), CC alert policies, Phase 4 backtest (optional, pre-real-money).
**Size estimate:** L
**Owner:** zeshan
**Plan file:** `docs/plans/fc-075.md`

**Problem / opportunity:** Owner wants covered calls written in a **separate Alpaca account** (paper `PA37XLNWDLB3`, provisioned 2026-07-18) isolated from the wheel, by **reusing mechanical components** (FC-038 two-pool share ledger, FC-050/FC-065 cost-basis floor, OCC utils) rather than duplicating the engine, with **its own tunable parameters** (`config/covered_call.yaml`) and **separate backtests**. Architecture settled by two adversarial reviews: separate account per strategy (no tagging — the wheel's `reconcile_positions` adopts any untracked position), one repo/one image/N config-selected Cloud Run services. Plan ships Phase 1 (isolation seams: strategy-keyed opportunity store, `STRATEGY_CONFIG` accessor + account-number interlock, profile-aware config, BQ dataset threading) now; Phases 2/4 (engine + backtest) are design-gated on FC-068 + FC-069 landing. Supersedes the abandoned plan formerly renumbered FC-032→037→038 (collided with merged two-pool FC-038).

**Open questions:**
- Post-FC-068/069: reuse `OptionsScanner.scan_for_call_opportunities` with a holdings-derived list, or a thinner holdings-first scanner? (blocking, decided in the Phase 2 design pass)
- Initial tunables for `covered_call.yaml` (delta band, DTE, min premium, validator thresholds) — operator to set before first sale (non-blocking)

**Links:** `docs/plans/fc-075.md`; gates: FC-068, FC-069; prerequisites/risks: FC-067 (journal put-labeling), FC-061 (ledger vs open orders), FC-056 (backtest call pricing 0.676×), FC-062/FC-066 (roller excluded from scope).

---

### FC-078: minimal roller revival — credit-only defensive rolls, daily evaluation

**Status:** Filed 2026-08-04 by operator decision — **EXPEDITED: plan + build launching immediately** (target: live before the 8/07 expiries, hoping to capture GOOGL-class rolls on current holdings)
**Size estimate:** S-M (money path: plan + two-reviewer gate)
**Owner:** zeshan + Claude
**Plan file:** docs/plans/fc-078.md (drafting)

**Problem:** the roller has never executed a roll (FC-066: quote-key bug, Friday-only cadence, dead-state credit-only-by-accident). The earnings-pop investigation (docs/investigations/earnings-pop-callaway-2026-08-03.md) proved two things: (1) no roller can rescue a deep-ITM earnings pop (AMZN: every strike-improving roll is a $1.2-1.7k debit — the earnings gate, FC-013, is the fix for that class); (2) the as-built roller leaves free money on the table — GOOGL had a ZERO-COST credit roll (+$10 strike, up to +$1,001 upside preserved) that its own entry-delta band and DTE cap made illegal.

**Scope (deliberately minimal):** fix the quote keys (bid/ask, FC-066 cause 1); **daily evaluation** (not Friday-only, not expiry-day-only); **credit-only execution** (structurally cannot pay debits to chase runaways — cannot repeat the AMZN class); defensive-roll exemption from the entry delta band; replacement DTE up to ~14; **assignment-imminence override** (extrinsic below threshold -> take the best credit roll now). Includes FC-066's pre-revival checklist: alert-wire both roll-path cost-basis skip events, structured events for the silent pre-guard skips, the replay-BQ gate forwarding (already pinned by test).

**Timing design decision (operator question answered 2026-08-04):** daily evaluation + credit-only threshold is self-timing — marginal rolls defer automatically (credit improves toward expiry when the stock is still), transient zero-cost windows get caught the day they appear, and the imminence override prevents waiting into early assignment. Expiry-day-only was rejected: it forfeits mark-dependent windows and walks into early-assignment risk on deep-ITM positions; the theta gain from waiting is a bet on the stock standing still, not a free harvest.

**Explicitly out of scope:** debit rolls of any kind; roll-up chasing; changes to the earnings gate (a roll that would newly span an earnings date must be blocked by the same span predicate — the replacement is a new short call); FC-066's full revival question (this supersedes its mechanism list; FC-066 closes into this on completion); the rolling.* knob sweep (FC-069 item 13's HOLD resolves here: knobs consumed by this design are kept, dead ones die with this FC's plan).

**Links:** FC-066 (supersedes-on-completion, with lineage), FC-013 (the complement: gate prevents the unrescuable, roller monetizes the rescuable; span predicate applies to replacements), FC-062 (execute_roll must route through the shared floor gate — absorbed here), docs/investigations/earnings-pop-callaway-2026-08-03.md, FC-069 item 13.

---

### FC-079: OCC-substring bugs survive on reconcile paths — call assignments structurally undetectable for P-containing underlyings

**Status:** Filed 2026-08-04 (from FC-069 S2 review; flagged in PR #84)
**Size estimate:** S (canonical-parser rewire + tests; behavior change on live reconcile → own PR, two-reviewer gate)
**Owner:** unassigned
**Plan file:** not yet

**Problem:** `src/strategy/wheel_engine.py` `reconcile_positions`'s position-diff fallback classifies option legs with `if 'P' in option_symbol … elif 'C' in option_symbol` over the **full OCC symbol** (currently `:306-309`). Every leg on an underlying whose ticker contains a `P` — AAPL, PFE, SPY, PG — classifies as a put, so the position-diff path can never detect a **call** assignment for those symbols. The activity-driven path (OPASN) is unaffected, which is why this has not visibly misfired — but the position-diff branch exists precisely as the fallback when the activities path misses, and it is silently disabled for roughly a third of the universe. Discovered during FC-069 S2 when a new test using AAPL failed with a wrong classification; the S2 tests deliberately use MSFT (no `P`) — a future symbol swap to AAPL in those tests will fail until this FC lands.

**Second site, same family:** `tools/testing/regression_monitor.py` `reconcile_positions`/`reconcile_orphaned` (~`:628`) still uses `"C" in symbol` plus a hand-rolled leading-alpha parser (noted by the S1 confirmation pass; warn-only surface). FC-069 S1 rewired `check_risk_parameters` to the canonical parsers but scoped out this sibling.

**Fix direction:** `strict_option_type` / `parse_option_symbol` (`src/utils/option_symbols.py`) at both sites, with tests pinning AAPL call-assignment detection on the position-diff path (must FAIL pre-fix). Family lineage: FC-041/043/045/048/052, FC-069 item 12 (S1 monitor sync, S3 scanner rewire). Not fixed inside FC-069's sweep because both S2 reviews agreed a live reconcile behavior change cannot ride a behaviorally-inert retirement PR.

**Ride-along from the S3 review (PR #85, trader LOW — accepted in writing there, deferred here):** give `OptionsScanner._has_existing_position`'s skip event a third `reason` value, `unparseable_position`, so a fail-closed skip caused by a garbage option symbol is distinguishable from a genuine holding. Today both emit `reason='option_position'`, which means a symbol we could not parse tallies as "already holds this underlying". Expected live frequency is **zero** (Alpaca returns well-formed OCC symbols; the branch exists as posture, not as an observed case), which is why it was not worth a second round on S3 — but it belongs with this FC's parser work, since the same rewire touches how an unparseable symbol is recognized.

---

### FC-080: roller duration drift — consecutive rolls compound the horizon; evaluate roll-count and absolute-DTE controls

**Status:** Consideration — **bookmark filed 2026-08-04 by operator** after the first live occurrence; revisit deliberately, not presumed a bug
**Size estimate:** S–M (research first; any rails are small config/gate diffs but money-path → full two-reviewer gate)
**Owner:** zeshan + Claude
**Plan file:** not yet

**What happened (2026-08-04, GOOGL trading up all day):** the roller rolled the same position **twice in one day**, both credit-positive and strike-improving — morning supervised cycle: C370 8/07 → C375 8/21 (+$235); scheduled 15:30 ET cycle: C375 8/21 → **C380 9/04 (+$60)** (`call_roll_completed` 19:32:40Z). Day net: strike +$10, +$295 collected — and the covered call went from 3 DTE to **31 DTE in one trading day**. Both rolls were legal and worked as designed: the FC-078 replacement-horizon rail is `old expiry + 14 days`, and it **re-anchors on the current contract at each roll**, so a trending week can walk the expiry out ~14 days per day with no structural limit.

**Why it may matter (the case for controls):**
- **Duration drift vs. the strategy's identity:** this is a 7-DTE rapid-theta wheel; a month-out call has a different theta/gamma profile, locks the shares in a covered state longer, delays the next put-side redeployment of that capital, and spans more event risk (earnings spans are gated, but not macro).
- **Structural bias toward the horizon edge:** longer-dated replacements mechanically carry more absolute premium, and selection is **max-net-credit** — so the roller will systematically prefer the far end of the horizon window whenever the chain allows. The compounding rail turns that preference into a ratchet.
- **Diminishing credit per day of extension:** roll 1 collected ~$16.8/day of added duration; roll 2 ~$4.3/day. Nothing prices the extension.

**Why it may be fine (the case for leaving it):** both rolls banked certain dollars and raised the strike toward the runaway — exactly the GOOGL-class rescue FC-078 was built for; assignment at 380 beats assignment at 370. Credit-only remains the structural floor. This FC exists to *evaluate*, not to presume.

**Controls to evaluate (non-exclusive):**
1. **Per-position roll-count cap** (e.g., max N rolls per underlying per week or per contract chain).
2. **Absolute horizon cap** — anchor the DTE limit to *today* (e.g., never hold a covered call > X DTE), not to the current contract's expiry; kills the ratchet directly.
3. **Cooldown** — no re-roll of a position rolled within the last N sessions (would have limited today to one roll).
4. **Credit-per-extension-day floor** — require `net_credit / added_days ≥ threshold`, pricing the duration instead of capping it.
5. Leave as-is + monitoring (a `roll_chain_length`/`total_dte_extension` label on `call_roll_completed` would make the drift measurable either way).

**Research before deciding:** realized outcomes of multi-roll chains vs. taking assignment (backtest + the live chain as it plays out); how often trending weeks would trigger 3+ consecutive rolls; whether max-net-credit vs. max-strike selection changes the drift; interaction with the documented re-pin-near-the-money behavior (rolled positions generate daily roll candidates while trending — the noise and the ratchet share a cause).

**Links:** FC-078 (the rails as built: credit-only, `old_expiry+14`, Δ ≤ 0.60, span gate, max-net-credit), docs/releases/RELEASE_2026-08-04.md (first roll), the 2026-08-04 `call_roll_completed` events (both rolls), FC-072 (call-side pricing — touches the same economics).

---

### FC-081: Cloud Build trigger silently stopped firing — main is merged-but-undeployed

**Status:** RESOLVED 2026-08-21 (same day) — root cause: **the GitHub repo was renamed `options_wheel` → `agentic_options_wheel`**; trigger rebound + repo re-connected. **Closure verified end-to-end:** the push recording this entry (`dd9e56a`) auto-fired build `6be2cfe7` → SUCCESS → strategy revision `00500-waw` (image digest matched to the commit's tag) + dashboard `00443-vaw` both serving, `/health` 200/healthy, zero WARNING+ logs on the new revision. The 16-day backlog (#88 FC-067, #89 FC-075 Phase 2) is now deployed. One follow-up open: the merged-vs-deployed freshness alert (below).
**Size estimate:** S
**Owner:** zeshan + Claude
**Plan file:** not needed (infra repair; documented here)

**Problem:** the wheel service was running revision `options-wheel-strategy-00498-qen`, built 2026-08-05 from `f84b50a`. Four commits merged to main on 2026-08-21 — **including FC-067 (#88) and FC-075 Phase 2 (#89), production code** — produced **no Cloud Build at all**. The trigger `deploy-options-wheel-strategy` (`^main$`, `cloudbuild.yaml`) was present, enabled, unfiltered. This is the FC-031 failure class escalated: FC-031 was a red build nobody saw and got build-failure alerting as its fix; **a build that never starts fires no failure alert**, so the existing channel is structurally blind to this mode.

**Root cause (diagnosed 2026-08-21):** the repo was renamed on GitHub. Git pushes, PRs, `gh` API calls all follow GitHub's automatic old-name redirect — so nothing *looked* broken — but Cloud Build 1st-gen triggers match the literal `github.owner/name` pair in the trigger config, and webhook events arrive stamped with the **new** name. They matched no trigger and were dropped with zero errors anywhere: zero `CreateBuild` audit-log entries after 08-05 04:02 UTC, zero check-runs on post-rename commits (every built commit has one from the `google-cloud-build` app — that asymmetry was the conclusive evidence). The GitHub App connection itself was intact the whole time.

**Fix applied (2026-08-21):** (1) operator re-ran the console "Connect repository" flow for `memon1987/agentic_options_wheel` (1st-gen mapping is keyed by repo name and console-only; until it existed, both trigger update and create returned bare `INVALID_ARGUMENT`/`FAILED_PRECONDITION`); (2) trigger rebound to the new name via `gcloud beta builds triggers export`/`import` (same trigger id `5f4caa43`, history preserved); (3) local git remote repointed. End-to-end verification: the commit recording this entry auto-fired a build on push (see below for result).

**Consequences while broken:** production journal rows were written with FC-067's poisoned put-labels for 16 extra days (mislabeled call rows grew 29 → 62 — the count in `docs/plans/fc-075-seam-4.md` DD-5); Phase 2's wheel-side changes sat unverified in prod.

**Coda (same evening) — the repair itself triggered false "build failure" emails, and the alert filter is now fixed:** the failed trigger-update/create API calls during diagnosis (7 audit-log ERRORs, 19:32–20:24 UTC) log under `resource.type="build"`, which the FC-030-era "Cloud Build failure — options-wheel" policy matched wholesale (`severity>=ERROR` with no method discrimination) — config-management noise masquerading as build failures. Investigation of a real failed build (`17ff3495`, 2026-08-04) showed its **only** ERROR entry is the audit-log `CloudBuild.CreateBuild` completion — the `cloudbuild` logName stays INFO even on failure — so the obvious tweak (`logName:"cloudbuild"`) would have silenced the alert for every real failure. Fix applied (policy `12432709964222363712` patched via API): exclusion polarity — `AND NOT protoPayload.methodName:"BuildTrigger"/"GitHubInstallation"/"GlobalTriggerSettings"` — validated both directions against live logs (today's 7 noise entries: 0 matches; the 08-04 real failure: still matches). Policy JSON now versioned at `deploy/monitoring/build_failure_alert_policy.json`. Note `CreateBuild` is a prefix of `CreateBuildTrigger` — positive matching would need exact equality; exclusion avoids the trap and fails open toward alerting.

**Lessons / follow-ups:**
- **[OPEN — the durable fix] Merged-vs-deployed freshness alert:** a scheduled check comparing latest-push age vs. latest-build age (or `origin/main` HEAD vs. the serving revision's commit), alerting on drift > N hours via the FC-030 channel. FC-031's build-failure alert watches builds that *start*; this failure mode needs a check that watches for builds that *don't*. A rename like this would have been caught in hours, not 16 days.
- **Redirect-masking:** GitHub's old-name redirects are exactly what made this silent. ~~Sweep docs/scripts for old-name URLs~~ **done 2026-08-27** (28 files, all docs-layer, mechanical).
- **Rename checklist:** any future repo rename must same-day update: Cloud Build repo connection + trigger, local remotes, and any webhook-keyed integration.

**Links:** FC-031 (the 11-days-undeployed precedent + build-failure alerting), FC-030 (the notification channel to reuse), `docs/plans/fc-075-seam-4.md` (rollout steps R3–R5 that this gated).

---

### FC-082: regression monitor's trade-execution check queries a nonexistent column — warn-degrading every hourly run

**Status:** CLOSED 2026-08-27 — PR #93 (`4a45e2d`): timestamp_iso → real TIMESTAMP compare (+ safe `trade_timestamp()` parsing in the duplicate-order pass); `BQ_DATASET` module constant deleted, dataset now profile-derived (env var survives as explicit override, NO string fallback — unresolvable profile = loud fail); 8 schema-drift tests added (mutation-checked). `cc-regression-hourly` scheduler job created same day — the detective layer now covers the CC book. Sweep found `check_performance_baseline` dead at the DATA-SOURCE level → FC-085.
**Size estimate:** XS (one column name + a test)
**Owner:** unassigned
**Plan file:** not needed (single-file fix; add a regression test)

**Problem:** `tools/testing/regression_monitor.py:265-268` filters `options_wheel.trades` on `timestamp_iso` — a column that does not exist in the live schema (the field is `timestamp`). The trade-execution check group has therefore been silently warn-degrading on every hourly `/regression` run rather than verifying anything. Same defect class as the FC-069 S1 findings: an alarm layer that doesn't actually alarm gets muted by habit. The `/regression` endpoint's other check groups are unaffected.

**Fix direction:** correct the column reference; add a test that runs the check's SQL against the code-defined `_TABLE_SCHEMA` field names (or a fake BQ fixture) so a schema/monitor drift fails the suite, not production. While there, sweep `regression_monitor.py` for other references to columns absent from the code-defined schemas.

**Fold-in (2026-08-21, Seam 4 code review):** the same file's `BQ_DATASET = os.environ.get("BQ_DATASET", "options_wheel")` (:51) is a surviving read-side dataset default that escapes Seam 4's T7 grep gate (env-wrapped, different name) — a covered-call service's `/regression` would silently validate the *wheel's* tables. Fix alongside: derive the dataset from the service's strategy profile (or require the env var per-service, no default).

**Links:** `docs/plans/fc-075-seam-4.md` §Review disposition (discovery context), FC-069 S1 (the check-group sync precedent), `docs/CLAUDE.md` §The detective layer.

---

### FC-083: earnings-calendar cache tests are date-sensitive — went red on main with zero code changes

**Status:** Filed 2026-08-27
**Scope:** shared (test hermeticity; the earnings cache serves both strategies' FC-013 gates)
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not needed (test-only fix; exempt)

**Problem:** `tests/test_earnings_calendar.py::TestTheCacheIsSharedAndDurable::{test_l1_is_shared_across_instances, test_l2_blob_survives_a_cold_start}` fail on main as of 2026-08-27 with **no code change since the suite was 1341-green on 2026-08-24** — only docs commits landed in between, so the tests depend on ambient wall-clock date (cache TTL arithmetic or a fixture date that aged out). Reproduced with and without the PR #91 diff (stash A/B). Fourth instance of the "test depends on ambient environment" class (live-BQ / missing-FastAPI / missing-creds are blocked in conftest; the clock is not).

**Why it matters:** a red-by-calendar suite poisons every PR's validation signal — PR #91 had to carry a "2 pre-existing failures" caveat, which is exactly how a real regression eventually hides. Also: `cloudbuild.yaml` step 1 runs the full suite and **a red suite blocks all deploys** (`|| exit 1`) — these two tests will fail the next build until fixed. **This makes the fix urgent, not cosmetic: PR #91 cannot deploy until these tests are green or the build's test step is amended.**

**Fix direction:** freeze the clock in these tests (inject `clock`/`freezegun`-style, or pin the cache-entry timestamps relative to the mocked now); audit the file for other now()-relative assertions; consider a conftest guard for naked `datetime.now()` in cache-TTL test fixtures.

**Links:** PR #91 (discovery), FC-030 session lessons (ambient-environment test defects), `src/api/earnings_calendar.py` (two-layer cache under test).

---

### FC-084: cloudbuild canary chains assume "newest revision = my canary" — races under concurrent builds

**Status:** Filed 2026-08-27 (PR #91 CI/CD review)
**Scope:** shared (all three services' deploy chains: wheel, dashboard, covered-call)
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not yet

**Problem:** every smoke-test step identifies the canary it just deployed as `gcloud run revisions list --limit=1` (newest-first) and every promote step runs `update-traffic --to-latest` — neither is pinned to the revision *this build* created. Two concurrent builds on the same service can cross-validate and cross-promote each other's revisions. Not hypothetical: on 2026-08-27 two main builds started **3 seconds apart** (`88a6288`, `861a473` — the FC-083 fix-merge and its bookkeeping push). Consequences today are benign (both builds carry the same code ±docs), but a rollback-build racing a forward-build could promote the wrong revision. Also noted: the polls read `status.conditions[0].status` assuming Ready is first — true in practice, not contractual.

**Fix direction:** capture the deployed revision name at deploy time (deploy step writes it to a workspace file, or resolve the `canary` tag) and pin both the readiness poll and the promote (`update-traffic --to-revisions=REV=100`) to it, in all three chains. Alternatively serialize builds (Cloud Build concurrency control / queue-ttl) — cruder but one line.

**Links:** `cloudbuild.yaml` (all `smoke-test-*` / `promote-*` steps), `docs/plans/fc-075-cc-deploy-step.md` §Review disposition, FC-031/FC-081 (the deploy-pipeline incident lineage).

---

### FC-085: regression monitor's performance_baseline check group is dead at the data-source level

**Status:** Filed 2026-08-27 (FC-082 build's schema sweep)
**Scope:** shared
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not yet

**Problem:** both queries in `check_performance_baseline` (`tools/testing/regression_monitor.py`) read `event_category`/`metric_name`/`metric_value` off the `trades` table — columns that exist in NO code-defined schema in the repo, because performance metrics have no BigQuery table at all (`log_performance_metric` writes Cloud Logging only). Unlike FC-082's column typo, this cannot be renamed into working order: the data source doesn't exist. The group has silently "passed" (validated nothing) since inception — same class as the FC-069 S1 deletions. The method docstring now carries a `KNOWN DEAD — do not trust this group's "pass"` note (PR #93).

**Fix direction:** either (a) repoint the check at what actually exists — Cloud Logging (like `check_logs`) or `executions.duration_seconds` per endpoint — accepting that this changes what the check measures, or (b) delete the group per the FC-069 precedent (an alarm that validates nothing cries wolf by staying green). Decide deliberately; do not leave the fictional group in place.

**Links:** FC-082 (discovery, PR #93), FC-069 S1 (the delete-fictional-checks precedent), `docs/CLAUDE.md` §The detective layer.

---

### FC-076: Structural account interlock in AlpacaClient — guard every entry point, not just HTTP routes

**Status:** Consideration
**Size estimate:** S–M
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** FC-075 Phase 1 added an account-number interlock that refuses trading when the live Alpaca `account_number` != `config.expected_account_number` — the control that makes "right code, wrong credentials" impossible across separate-account strategies. But it is implemented as a Flask decorator (`require_account_match` in `deploy/cloud_run_server.py`), so it guards **only HTTP routes**. Two entry points bypass it entirely:

- `main.py` (`Config(args.config)` → drives `WheelEngine` directly) — the CLI/local path.
- Any future **Cloud Run Job** (this repo already uses Jobs for backtests, and Phase 3 of FC-075 may add one for the covered-call service) — Jobs don't go through the Flask app at all.

Both adversarial reviewers of FC-075 Phase 1 (PR #77) flagged this as the design's weakest point: the control derives from per-route decorators a developer must remember to add, rather than from the order-placing layer itself.

**Proposed direction:** move the interlock down into `AlpacaClient` — verify `get_account().account_number == config.expected_account_number` once at client construction (or on first order submission), and refuse to place orders on mismatch. Every current and future entry point (routes, `main.py`, Jobs) then inherits the guard structurally. The HTTP decorator can stay as a fast-fail-with-clean-503 layer on top, or be removed once the structural check exists.

**Open questions:**
- Check at `AlpacaClient.__init__` (one network call per construction — how often is it constructed?) vs. lazily on first order (cheaper, but read-only endpoints then never verify)?
- Latch semantics: mirror FC-075's (genuine mismatch latches, check-failure doesn't) at the client layer.
- Does a read-only construction (dashboard `/account`, `/positions`) need to hard-fail, or only order-placing paths? FC-075 deliberately left those read endpoints unguarded.
- Interaction with the backtest's `BacktestAlpacaClient` — must be exempt (no real account).

**Links:** FC-075 (introduced the route-level interlock); `docs/plans/fc-075.md` §Seam 2 "Follow-up"; PR #77 review findings.

---

### FC-077: Opportunity-store strategy_id grace window — tighten only under single-account consolidation

**Status:** Consideration (conditional — NOT actionable now)
**Size estimate:** S
**Owner:** unassigned
**Plan file:** not yet

**Problem / opportunity:** FC-075 Phase 1's opportunity-store isolation treats a blob with no `strategy_id` field as belonging to the `wheel` strategy (`data.get('strategy_id', 'wheel')` in `src/data/opportunity_store.py`). This grace existed so the wheel could consume its own legacy (pre-FC-075) blobs across the deploy boundary. It has no expiry.

Under the current architecture this is **safe-permanent**: each strategy has its own GCS bucket (Seam 1), so the wheel's bucket only ever contains wheel blobs, and a covered-call service reads a different bucket entirely. An absent field can never cause a cross-strategy read.

The grace only becomes a latent weakness **if** we ever consolidate strategies into a single shared bucket (which would accompany single-account consolidation — explicitly out of scope in FC-075, and not planned). At that point an untagged blob in a shared bucket would be silently attributed to the wheel.

**Proposed direction (only if consolidation is pursued):** require `strategy_id` on every blob (drop the default), and reject untagged blobs fail-closed. Trivial change; the reason it isn't done now is that dropping the default today would make the wheel reject its own historical blobs for no benefit.

**Open questions:**
- Is single-account / shared-bucket consolidation ever going to be pursued? If the answer stays "no," this FC can be closed as won't-do.

**Links:** FC-075 §Seam 1 + §Phase 1 tests "grace-window note"; `src/data/opportunity_store.py` `_blob_belongs_to_strategy`.

---

## Completed

_Move entries here once a plan has been published, executed, and merged. Include plan file + PR/commit link._

### FC-014: Wire RiskManager.validate_new_position() into sellers (or retire it)
- Plan: `docs/plans/fc-069.md` (item 7)
- PR: https://github.com/memon1987/agentic_options_wheel/pull/83
- Closed: 2026-08-04 by FC-069 item 7 (operator decision: retire)
- Notes: `validate_new_position` and its five sibling methods (six in all — `_validate_option_specific_risks`, `calculate_portfolio_risk_metrics`, `should_reduce_positions`, `get_emergency_stop_conditions`, `check_emergency_conditions`) were deleted with zero production call sites ever recorded; `validate_roll` survives as the roller's gate (FC-066's turf). All five checks this entry named are dispositioned: the global position cap by FC-069 item 1 (deleted — operator flip from revive), per-ticker exposure by item 2 (deleted; corrected structural bound documented — the real per-ticker bound is `max_position_size` × portfolio, which *floats with equity*, not an absolute dollar cap), per-stock cap by item 3 (deleted; emergent invariant documented with its open-order caveat), cash reserve by the item 7 rider (deleted with its detective check), and portfolio allocation by card 17 (knob deleted; its warn-only detective mirror deleted with it under card 16). The account-level loss-limit concepts inside the emergency-stop methods were dispositioned by item 7's sub-decision: carried into **FC-074** (filed 2026-08-01), the account-level kill-switch entry, whose thresholds are to be chosen from the account's actual drawdown history rather than inherited from the dead code's 15%/5%. The consolidate fork died with the method: enforcement on this system is distributed by design — scanner filters, selection ledgers, execute-time floor, plus the hourly `/regression` detective layer (card 16). This entry's third open question is answered on the record: **no**, `max_exposure_per_ticker` and `min_cash_reserve` were never checked anywhere — they were silently disabled from inception.

### FC-015: Centralize hold-period state in WheelStateManager (cold-start safe)
- Plan: `docs/plans/fc-069.md` (item 6)
- PR: https://github.com/memon1987/agentic_options_wheel/pull/83
- Closed: 2026-08-04 by FC-069 item 6 (operator decision: delete)
- Notes: `_entry_times` never survived a request (sellers are constructed per request, not per cold start as this entry assumed), so the 4h gate has been open since inception — 2026-05-12 evidence: 4 of 35 sub-4h closes. The raised DTE-band thresholds carry the hold discipline. The knob (`risk.profit_taking.min_hold_hours`), both dicts, their population sites and the gate code are deleted. If a hold gate is ever wanted, derive entry time statelessly from Alpaca order history (`filled_at`) — do not persist process state; that answer supersedes this entry's GCS-vs-Alpaca open question, and its "share infrastructure with FC-009" question is moot for the same reason.

### FC-039: Wheel state persistence has never worked in production
- Plan: `docs/plans/fc-069.md` (item 8, stage 2)
- PR: https://github.com/memon1987/agentic_options_wheel/pull/87
- Closed: 2026-08-04 by FC-069 item 8 (operator decision: delete the illusion, per the coherence principle — derive from durable truth)
- Notes: Persistence was never enabled and is not wanted: `reconcile_positions` rebuilds from Alpaca per request (this entry's own observation), the floor now resolves from Alpaca `avg_entry_price` (FC-065 P1) with no wheel_state source, and FC-066's roller direction is stateless-from-Alpaca — FC-078 shipped exactly that, which is what unblocked this stage. Stage 1 (S5, PR #86) removed the orphaned CallSeller state plumbing; stage 2 shrank `WheelStateManager` to reconcile's in-request bookkeeping: 747 → 331 lines, keeping only `symbol_states`, `handle_put_assignment`, `handle_call_assignment`, `get_position_summary` and the derived `get_wheel_phase` label, and deleting the GCS save/load, the `storage_bucket` parameter, the engine's `state_storage_bucket`/`STATE_STORAGE_BUCKET` lookup, the six roller state methods, the `can_sell_*` gates, the premium accumulators, the roll counters, and the in-memory `wheel_cycles` list. This entry's open questions are answered: **do not enable it** (question 1 — the fix was removing the illusion, not the bucket); questions 2 and 3 are moot with the code gone. Question 4 — the startup assertion — generalizes beyond this FC and is recorded as a standing rule in FC-069's plan (item 8(b)) and in `wheel_state_manager.py`'s module docstring: **any future configured-but-unresolvable persistence target must fail loudly at startup, never silently no-op.** The silent `storage_bucket=None` is how this layer stayed fictional for a year while the docs called it canonical; whoever next adds a durable-state target (FC-009's dedup, anything else) inherits that as a requirement, not a suggestion.

### FC-006: Covered call rolling engine (Friday EOW)
- Plan: `docs/plans/fc-006.md`
- PR: https://github.com/memon1987/agentic_options_wheel/pull/5 (merged 2026-04-16)
- Commit: `08fb876`
- Notes: Deployed with `rolling.enabled: false`. Pending paper testing on Fridays before enabling Cloud Scheduler job.

### FC-007: Earnings Calendar Service (Finnhub)
- Plan: `docs/plans/fc-007.md`
- PR: https://github.com/memon1987/agentic_options_wheel/pull/5 (merged 2026-04-16)
- Commit: `0ccf852`
- Notes: Finnhub API key in Secret Manager, injected into Cloud Run. Log enrichment active; PutSeller/CallSeller integration deferred.

### FC-010: Disable call stop-losses (assignment is profitable by design)
- Plan: `docs/plans/fc-010.md`
- PR: https://github.com/memon1987/agentic_options_wheel/pull/7 (merged 2026-04-17)
- Commit: `737db8a`
- Notes: Single config change (`use_call_stop_loss: false`). Deployed to Cloud Run revision `00142-vz6`.

### FC-012: Shift dashboard logging to Alpaca queries wherever authoritative
- Plan: `docs/plans/fc-012.md`
- PR: https://github.com/memon1987/agentic_options_wheel/pull/8 (merged 2026-04-24)
- Commit: `8b31a1b`
- Notes: All phases (2.1-2.7) shipped in one PR after user dropped the parity gate. New tables: `trades_from_activities` (465 rows backfilled) and `equity_history_from_alpaca` (124 rows). New views: `trades_with_outcomes`, `wheel_cycles_from_activities`. Three Cloud Scheduler jobs ingest on a split schedule. V1 tables (trades, wheel_cycles, position_snapshots, order_statuses) left inert pending manual `bq rm` — a follow-up remote routine on 2026-05-01 opens a cleanup PR. Follow-up fix PR #9 preserves `GCP_PROJECT` env var across bot deploys.

### FC-008: Stop-loss events mislabeled as profit_target_reached (superseded)
- No dedicated PR — superseded by FC-010 + FC-012.
- Closed: 2026-04-24
- Notes: Two independent mechanisms neutralized this. (1) FC-010 disabled call stop-losses, so `should_close_call_early` no longer returns True for losses — the mislabeling trigger is gone. (2) FC-012 cut dashboard reads over to `trades_with_outcomes` (Alpaca-sourced), so the corrupted `event_type=early_close_executed` + `reason=profit_target_reached` rows in the v1 `trades` table no longer affect analytics. Historical rows remain dirty but unread; the v1 table itself is scheduled for drop on 2026-05-01 via the FC-012 cleanup routine. If put early-closes ever start showing the same mislabel in structlog events, re-file as a new FC focused on the put-side path only.

### FC-018: Wheel-centric dashboard rebuild (frontend only)
- Plan: `docs/plans/fc-018.md`
- PRs: #12 (skeleton), #13 (backend), #14 (pages), #15-#18 (review fixes), #22 (Trade Log), #23 (gap-closing), #24 (PR F cutover), #25 (PR G cleanup) — final merge 2026-05-05
- Commits: `b7b9184` → `4eb74d2` (PR G)
- Notes: 3-page dashboard (Overview / By Symbol / Bot Health) shipped via strangler migration. Canonical paths are now bare (`/overview`, `/symbol`, `/bot-health`); `/v2/*` and legacy `/positions`, `/trades`, `/performance`, `/cycles` redirect for bookmark compatibility. Legacy frontend preserved under `dashboard/frontend.archive/` with emergency-revert README — recommend deletion after ~2 weeks of bake time. Mid-execution the gross-vs-net premium audit triggered FC-019; FIFO cycle pairing for overlapping share lots was scoped out as FC-020.

### FC-022: Trade Log contract IDs + ET timezone + By-Symbol summary table
- Plan: `docs/plans/fc-022.md`
- PR: https://github.com/memon1987/agentic_options_wheel/pull/26 (merged 2026-05-06)
- Commit: `cd1d47d`
- Notes: Trade Log gains OCC symbol + expiration + Alpaca order ↗ link per row. All date helpers (`fmtDate`, `fmtDateShort`, `fmtDateTime`) now ET-anchored with explicit `timeZone: 'America/New_York'` and `fmtDateTime` shows the EST/EDT marker. `/symbol` landing page replaces the pill grid with a sortable summary table (`SymbolUniverseTable`) backed by existing scorecard data. Backend `fc018_acb_timeline_per_symbol` view extended with `occ_symbol`, `order_id`, `expiration` columns. `positionState`/`stateColor` extracted from SymbolScorecard into a shared util. 4 new vitest tests assert ET-stability across system locales.

### FC-021: Synthetic activity correction for Alpaca paper-engine silent settlements
- Plan: `docs/plans/fc-021.md`
- Commit: `133ebb0` (no PR — data-only correction)
- Date: 2026-05-06
- Notes: Inserted two synthetic rows into `options_wheel.trades_from_activities` (`activity_id LIKE 'synthetic-fc-021-%'`) to reconcile the dashboard for `AMD260116C00212500`'s silent 2026-01-16 paper-engine exercise. Discovered during reconciliation diving (see `docs/investigations/amd-reconciliation.md`) — Alpaca's paper engine settled the deep-ITM call without logging OPASN/OPEXP/OPTRD; daily-P&L hypothesis fit confirmed only one silent event occurred, no second discrepancy. Effect on AMD scorecard: `share_pnl` −$24,250 → −$3,000, `total_pnl` −$17,319 → +$5,309, Cycle 2 `cap_gain` −$20,500 → $0 (clean wash). Headline Total Return remains pinned to NLV − sum(deposits) so it's unaffected; per-symbol sum across symbols ($44.9k) no longer ≈ headline ($20.1k) — accepted divergence reflecting the off-book silent settlement. Audit query: `WHERE activity_id LIKE 'synthetic-%'`. Rollback: `DELETE` same predicate.

### FC-023: Per-symbol Realized P&L reconciliation — single canonical number across drilldown
- Plan: `docs/plans/fc-023.md`
- PR: https://github.com/memon1987/agentic_options_wheel/pull/27 (merged 2026-05-07)
- Commit: `83bbd57`
- Notes: Top-of-page "Realized P&L" card and Wheel-vs-B&H "Wheel" total now both display canonical `total_realized_pnl` (option leg + share leg, FC-019) instead of two different disagreeing numbers. UNH pre-fix: top $4,334, wheel $10,222 (double-counted premium). UNH post-fix: both $2,584. View `fc018_vs_buy_and_hold_per_symbol.wheel_minus_bh` formula corrected from `realized_pnl + total_premium` to `total_realized_pnl`. B&H labeled "(price only)" — dividend-reinvested B&H is a deferred concern (FC-017's neighborhood).

### FC-024: ACB walk view rewrite — restore missing event types and ACB computation
- Plan: `docs/plans/fc-024.md`
- PRs: https://github.com/memon1987/agentic_options_wheel/pull/30 (merged 2026-05-07; replaces auto-closed [#28](https://github.com/memon1987/agentic_options_wheel/pull/28) which lost its base on FC-023's merge)
- Commit: `1a4e401`
- Notes: `fc018_acb_timeline_per_symbol` rewritten to source from `trades_from_activities` directly via four UNION-ALL blocks (opens / closes / OPASN with QUALIFY-guarded OPTRD pairing / OPEXP). Pre-fix every symbol had `rows_w_acb=0`; post-fix all 6 event types render correctly with ACB transitions during share-holding windows. Reference-dot positioning fixed (`dotAxisFor()` helper rides ACB axis when shares held, premium axis otherwise). Incidentally fixed Phase Timing observation #4 (UNH state machine now returns 4-phase split: cash 124d / short_put 61d / long_stock 10d / covered 17d). Surfaced AMZN silent-exercise data anomaly as a side discovery, filed as FC-025.

### FC-026: Decision Quality — surface Premium Received / Captured / Foregone macro stats
- Plan: `docs/plans/fc-026.md`
- PR: https://github.com/memon1987/agentic_options_wheel/pull/29 (merged 2026-05-07)
- Commit: `1b5559c`
- Notes: Capture-ratio math validated as correct against raw activities (no data fixes shipped). Three new dollar-magnitude aggregates rendered in the chart card: Received / Captured / Foregone (buybacks). UNH macros: **$5,888 / $4,334 (73.6%) / $1,554 (26.4%)** (verified 2026-05-07 against raw Alpaca activity feed). "Foregone" is qualified "(buybacks)" to disambiguate from the counterfactual reading (which would require option-chain snapshots — FC-017). Frontend-only; no view, no backend, no payload change.

### FC-027: Cycle Table — separate "Total Premium" from "Cycle P&L"
- Plan: `docs/plans/fc-027.md`
- PR: https://github.com/memon1987/agentic_options_wheel/pull/31 (merged 2026-05-07)
- Commit: `9928db8`
- Notes: Surfaced mid-trace during the FC-023/024/026 manual reconcile when the user noticed the Cycle Table column labeled "Cycle P&L" actually displayed `total_premium` only (option-side net), silently excluding `capital_gain` (share-side cash flow). For UNH Cycle 1 the column read +$1,218 but the true cycle outcome was $1,218 − $1,750 = −$532. Fix: rename existing column → "Total Premium" (matches the data), add new "Cycle P&L" column = `total_premium + capital_gain`. Same class of bug as FC-023 at cycle granularity. Peer review caught one defect (Cap Gain tooltip leaked internal nomenclature `Post-FC-019`/`OPTRD` — reverted to user-readable copy) and two test-strength suggestions (cell-position assertions) — all addressed pre-merge.

### FC-028: fmtDate calendar-date off-by-one (TZ shift on pure dates)
- PR: https://github.com/memon1987/agentic_options_wheel/pull/32 (merged 2026-05-07)
- Commit: `0c4d20d`
- Notes: Plan-exempt (single-file utility bug fix). User caught on Trade Log: OCC `UNH260424P00302500` (Apr 24 expiry) rendered "Apr 23" in the Expiration column. Root cause: `fmtDate()` parsed pure-date strings as UTC midnight then converted to ET (UTC−4) — rolled back to prior day. Same bug affected `event_date` Date column. Fix detects `YYYY-MM-DD`-shaped inputs and renders from year/month/day directly with no TZ conversion. Full ISO 8601 timestamps still ET-anchor per FC-022. 4 new vitests pin the contract; FC-022's ISO behavior verified preserved.

### FC-025: AMZN silent-exercise correction (paper-engine, Jan 16 2026)
- Plan: `docs/plans/fc-025.md`
- Investigation: `docs/investigations/amzn-reconciliation.md`
- Commit: `15625ce` (no PR — data-only correction direct to `main`, mirroring FC-021)
- Date: 2026-05-07
- Notes: Twin of FC-021's AMD silent-exercise bug. AMZN $240 put `AMZN260116P00240000` (sold 2026-01-12 at $0.73, expired 2026-01-16 with AMZN at $239.09 = $0.91 ITM) was auto-exercised silently — no OPASN/OPTRD ingested. Confirmed by behavioral evidence (Jan 23 covered call written, Apr 22 called-away at exact $240 strike). Inserted two synthetic rows into `options_wheel.trades_from_activities` (`activity_id LIKE 'synthetic-fc-025-%'`). Effect on AMZN scorecard: `share_side_pnl` +$20,500 → **−$3,500**, `total_realized_pnl` $26,206 → **$2,279**, `cycles_completed` 1 → 2, `wheel_minus_bh` +$21,094 → **−$2,833** (sign reversal — wheel actually lagged B&H on AMZN). Audit query: `WHERE activity_id LIKE 'synthetic-%'` (returns 4 rows: 2 FC-021 + 2 FC-025). Rollback: `DELETE WHERE activity_id LIKE 'synthetic-fc-025-%'`.

### FC-029: Wheel strategy Phase 1 risk re-tune (call delta + cost-basis floor + drawdown pause)
- Plan: `docs/plans/fc-029.md`
- Investigations: `docs/investigations/strategy-review-2026-05-07.md`, `docs/investigations/cost-basis-floor-validation-2026-05-08.md`
- PR: https://github.com/memon1987/agentic_options_wheel/pull/34 (merged 2026-05-08)
- Commit: `692f64e`
- Notes: Three complementary changes addressing the 3-loss-cycle pattern (-$9k share losses) found in the senior-trader strategy review. **R1**: tightened `call_delta_range` from `[0.30, 0.70]` to `[0.15, 0.25]` (calls 2-4% further OTM, 30-70% → 15-25% assignment probability). **R2**: cost-basis floor source-order rewrite — Alpaca's `cost_basis` returns 0 for assigned paper positions (both safety guards were gated on `> 0` and silently bypassed), now `CallSeller._resolve_cost_basis_floor` reads `wheel_state.stock_cost_basis` (canonical, populated from put strike at OPASN) → BQ lookup of last 90-day OPASN-put strike (handles silent assignments + cold starts; back-fills wheel_state) → Alpaca (last-resort fallback for non-wheel positions); when ALL three fail with shares > 0 the call write is blocked with `event_type=cost_basis_floor_unresolved` (operator intervention). **R3**: drawdown pause — skip covered call writes when shares ≥ 5% below cost basis with `event_type=covered_call_drawdown_pause`. Bad/missing quote now defers (`event_type=covered_call_quote_missing`) instead of failing-open. Two-reviewer process (new ~/CLAUDE.md rule for high-stakes changes) caught 4 HIGH + 3 MEDIUM the first review missed — see PR comments. Tests 27 in `TestCallSellerCostBasisFloorFC029`; 253/253 pytest green. Follow-up FC-030 filed for drawdown-pause observability metric.

### FC-019: True P&L reconciliation — JNLC + OPTRD ingest, share-side P&L
- Plan: `docs/plans/fc-019.md` (written retroactively)
- PR: https://github.com/memon1987/agentic_options_wheel/pull/19 (merged 2026-05-05)
- Commit: `78acf92` (preceded by `4862159` — interim env-var-baseline fix that this PR replaces with the real JNLC sum)
- Notes: Per-symbol scorecard now reconciles to actual account growth (sum of Total P&L = $21,808 vs account growth $20,080, with the ~$1,600 unexplained gap concentrated entirely on AMD's Alpaca-side data anomaly). New scorecard columns: Option P&L (renamed from Net P&L), Share P&L (FC-019), Total P&L (sum). `wheel_cycles_from_activities.capital_gain` now uses real OPTRD cash flow within the cycle window. `BASELINE_DEPOSITS` env var becomes a fallback only — primary source is `SUM(net_amount) WHERE activity_type='JNLC'`. Per-cycle pairing for overlapping share lots is filed as **FC-020** for follow-up.

### FC-031: Dashboard metrics overhaul — vetted portfolio metrics + bot execution health
- Plan: `docs/plans/fc-031.md`
- Investigations: `docs/investigations/dashboard-metrics-audit-2026-07-07.md` (per-metric methodology audit), `docs/investigations/fc-031-adversarial-review-2026-07-07.md` (adversarial PM review of the plan — 7 blockers incorporated pre-implementation)
- Merge: direct merge commit `1e8f622` to main (2026-07-07) — no PR: GitHub App not connected for the org this session; branch `claude/dashboard-metrics-review-k1ze5g`, commits `5fa2e48` (plan+audit), `7b5a718` (implementation), `14f299d` (adversarial code-review fixes)
- Notes: One accounting convention everywhere (net cash P&L + market value of holdings — the PM review caught that the draft's `(price − basis) × shares` add-back would have double-subtracted held-share cost, ~$24k error on AMD). Headline KPIs: Total P&L (realized cash / open value split), max drawdown (% + flow-adjusted $), XIRR labeled "annualized (single deposit)". TWR-indexed equity curve vs SPY; vs-B&H made symmetric (wheel MTM); FIFO open-lot basis + breakeven columns; cycle table RoC + $/day/$1k; separate put/call trade stats with held-to-expiry exercise-rate calibration vs live config delta bands (Symmetry Principle); net option cash flow bars. Bot Health: decision funnel, anomaly flags on the SPY-bar calendar, run reliability, drawdown-pause card (absorbs FC-030's dashboard half), falsifiable reconciliation banner (residual vs known gaps, share-count mismatch tracking). Removed: dead `win_rate`/`return_30d`, option-leg-only `/api/metrics/pnl-by-symbol`, fake freshness stamp, CAGR tile. Post-implementation 8-angle code review found + fixed 3 defects (cycle-stats fallback crash, unpopulated mismatch badge, /config not exposing threshold/bands) before merge. Tests 293 pytest + 72 vitest. **Post-merge manual steps pending:** re-apply `fc018_views.sql` + `fc031_views.sql` via bq, `POST /ingest-stock-history?backfill_days=400` (SPY), `POST /ingest-activities?after=2025-10-01` (FEE).

### FC-030: Drawdown-pause alerting — operator notification for extended pauses
- Plan: `docs/plans/fc-030.md`
- Runbook: `deploy/monitoring/drawdown_pause_alert.md`
- PRs: [#38](https://github.com/memon1987/agentic_options_wheel/pull/38) (endpoint + tests), [#40](https://github.com/memon1987/agentic_options_wheel/pull/40) (CI fix — FastAPI-free service module), [#41](https://github.com/memon1987/agentic_options_wheel/pull/41) + [#42](https://github.com/memon1987/agentic_options_wheel/pull/42) (alert-filter fix + closeout, **duplicate fixes — see note**)
- Date: 2026-07-18
- Notes: Scope was alerting only — the observability half shipped in FC-031. `POST /api/v2/bot-health/pause-alert-check` runs weekdays 17:45 ET and logs a single `DRAWDOWN_PAUSE_ALERT` line when any symbol is paused >= 7 trading days (threshold declared in `cloudbuild.yaml`); a Cloud Monitoring log-based policy emails the operator via the project's **first notification channel**. Built as a strict consumer of FC-031's `get_drawdown_pauses` — one implementation of pause state, not two. Degraded paths are loud: a live-positions outage logs `DRAWDOWN_PAUSE_ALERT_CHECK_FAILED` rather than reporting "nothing paused". **Cloud Build failure alerting shipped on the same channel** and was prioritized ahead of the pause alert — FC-031 had sat undeployed 11 days behind an unnoticed red build; the new alert then caught three real failures the same session. **The mandatory fire drill caught a fatal defect:** the policy's `severity>=WARNING` clause matched zero entries, because Cloud Run only assigns severity to structured JSON logs while Python's `logging.warning()` writes plain text — the alert would have been silent forever, discoverable only as a missing notification. Pure logic lives in `services/pause_alert.py` (the bot CI image has no FastAPI); a module-level `pytest.importorskip` was rejected after verifying it silently skips the pure tests too.
- Note on duplicate PRs: two parallel sessions independently found and fixed the same severity-filter defect (#41 and #42). The same collision duplicated this file's entire Completed section, repaired in `fix/dedupe-fc-ledger`. No code conflict resulted; the fixes were equivalent.
- ~~**Operator action outstanding:** confirm the alert email lands (Cloud Monitoring channels may need one-time verification).~~ **Resolved 2026-08-01: operator confirmed alert emails are arriving.** Delivery verified end-to-end; FC-030 fully closed. The same channel now also carries FC-065 P1's cost-basis alert policy (`1661285269015921471`).

### FC-050: the covered-call below-basis floor never ran on the production path
- Plan: `docs/plans/fc-050.md`
- PR: https://github.com/memon1987/agentic_options_wheel/pull/74 (merged 2026-07-30, `8e05134`; implementation `b4975b2` + review fixes `1485be0`)
- Date: 2026-07-30 (deployed build `1b6badcb`, revision `00384-yun`; production-verified 2026-07-31)
- Notes: `execute_call_sale` read `stock_cost_basis`/`shares_covered` while scanner opportunities — the only kind production executes — carry `cost_basis_per_share`/`max_contracts`, so the guaranteed-loss guard **had never run in production**; the scanner's own floor used raw Alpaca `cost_basis` rather than FC-029's chain. Fix: extracted FC-029's resolver into `src/strategy/cost_basis.py` (`CostBasisResolver` + `opportunity_floor_per_share`), wired it into the scanner, and made the execute-time gate real and **fail-closed** on an unresolved floor. Also fixed a `shares_covered` default of 100 that FC-038's multi-contract calls had just made reachable. **Evidence query answered the FC's long-open question: the dead guard already cost money — 15 below-basis calls written, 3 called away for −$9,000** (UNH −1,750 / AMZN −3,500 / AMD −3,750) against $2,585 premium, independently reproduced by a reviewer and matching `strategy-review-2026-05-07.md`'s three-cycle figure; the last below-basis write (NVDA 2026-06-10) was post-FC-029. Two adversarial reviews both REQUEST_CHANGES with no disagreements; **reviewer 2 mutation-proved two new tests pinned nothing** (the backtest BigQuery gate and BQ-over-Alpaca precedence both survived behavior-breaking mutations) — fixed, and the backtest's BQ isolation is now pinned for the first time. Reviewers also caught two errors in the plan itself: a Risks-section mitigation describing a resolution chain that does not exist, and a post-deploy expectation (AAPL 303.50) that would have flagged correct behavior as a bug. Confirmation pass CONFIRMED-CLEAN, re-running both mutations itself. Suite 786. **Production-verified across three scan cycles:** floors resolve from BigQuery — AAPL 305.00, AMZN 262.50, GOOGL 370.00, NVDA 220.00 — every `basis_delta` positive (+1.30 to +1.66, the conservative direction, indicating clean single-assignment lots); zero unresolved-floor blocks; `/scan` 17.1s→25.5s (~2s per held symbol, scales linearly — revisit past ~10 positions). Follow-ups filed: FC-062 (roller fail-open floor + `execute_roll` bypasses this gate), FC-063 (type the opportunity boundary — FC-045/048/050 share this root cause), FC-064 (`max(BQ, broker)` for mixed lots), FC-065 (FC-029's R3 drawdown pause is dead on the same path).

### FC-038: Covered calls charged phantom cash collateral in execution selection — call starvation
- Plan: `docs/plans/fc-038.md`
- Investigation: `docs/investigations/covered-call-starvation-2026-07-18.md`
- PR: https://github.com/memon1987/agentic_options_wheel/pull/73 (merged 2026-07-30, `0545aff`; implementation `87791c5` + review fixes `3999a8b`)
- Date: 2026-07-30 (investigated 2026-07-18; entry lost 12 days in a three-session index collision — the covered-call *extensibility* proposal that also claimed FC-038 must refile)
- Notes: `/run` treated covered calls as cash-collateralized in two places — `put_seller._calculate_position_size` (BP-gated sizing applied to all types) and `select_batch` (`strike×100` phantom charge) — starving the call side while ~50–90 opportunities/day converted to 1–3 trades; AAPL sat uncovered 07-15→07-30 despite top-scored calls in every scan. Fix: **two-pool selection** — calls consume a per-underlying available-shares ledger (canonical OCC parsers, calls-first, `attractiveness_score`-ranked), puts keep the cash/BP pool; shares-based call sizing; every ranking/selection drop logs a structured reason (5-reason enum incl. `positions_unavailable` outage marker); scanner now **fails closed** on unresolved cost basis (`call_scan_skipped_cost_basis_unresolved`); single positions snapshot per cycle threaded from `/run` (and mirrored in the backtest simulator). Golden-replay test pins the 07-17 incident blob verbatim (mutation-checked: pre-fix behavior fails 12 tests). Two adversarial reviews (trader + reliability) → REQUEST_CHANGES ×2 → 4 fixes in code, open-order ledger blindness deferred to **FC-061** (broker share-lock verified live as interim control), 6 items accepted in writing; confirmation pass CONFIRMED-CLEAN. Suite 744 passed. **Production-verified 15:15 ET same day:** AAPL 8/3 347.5C sold/filled @ $1.66 (basis $303.50), AMZN dropped `insufficient_available_shares`, GOOGL/NVDA excluded by the floor, put pool unaffected. Follow-ups queued: FC-050 (dead execute-time floor — next PR), FC-061, FC-039/roller.
