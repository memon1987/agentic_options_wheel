# FC-112 — the wheel roll-trigger study (`itm_trigger_ratio` 0.98 vs 1.00)

> **THIS IS A TEMPLATE, NOT A RESULT.** It is committed in PR-2, before any
> `t100` cell has been replayed, because the shape of the record is part of the
> pre-registration: a write-up whose sections are chosen after the numbers are
> read is a write-up that can leave a section out.
>
> **To use it:** `cp docs/investigations/fc-112-roll-trigger-study-TEMPLATE.md
> docs/investigations/fc-112-roll-trigger-study-<YYYY-MM-DD>.md`, then fill
> every `<...>` from the tool's own output. Do not delete a section because it
> came out empty — say it came out empty. Leave this block behind in the copy's
> place: delete it only from the copy.

**Plan:** `docs/plans/fc-112.md` · **FC:** FC-112 · **Read on:** `<date>`
**Tool:** `tools/diagnostics/fc112_roll_trigger_read.py` @ `<40-hex SHA from the
verdict block>` — the SHA that froze the rule.
**Command:** `<the exact command line, including --pin-id / --run-id / --oos-run-id / --artifacts>`

---

## 1. The question

Over the wheel's live universe, trailing year with a 90-day holdout, does
replacing `rolling.itm_trigger_ratio: 0.98` with `1.00` — everything else
identical — change the wheel's return, and is the change large and consistent
enough to justify the wheel running a *different* mechanism from the
covered-call profile?

**Default: `1.00`.** The burden is on 0.98 (operator principle, FC-100 D-A,
2026-09-11). The rule below is the only way 0.98 keeps.

## 2. The rule, as pre-registered (do not edit — copy from the verdict block)

```
TIE_BREAK       = "move"          # default outcome: 1.00
MIN_EFFECT_PP   = 0.5             # pp of annualised return on $100,000
MIN_SIGN_COUNT  = {7: 6, 6: 5}    # M < 6 -> VOID(insufficient_measured)
tool commit     = <sha>
```

KEEP (0.98 stays) requires **all** of:

1. In **every** read (`limit`, `haircut`, `noimm`): `N- >= MIN_SIGN_COUNT[M]`
   AND `median(delta ann) <= -MIN_EFFECT_PP`.
2. **Located in the option leg:** the same two conditions on
   `delta option_pnl x (365/275) / starting_cash`, `limit` read.
3. The holdout does not refute (only informative at `comparable >= 4`).
4. The out-of-sample read (DD-10), where it exists, does not refute.

Everything else: **MOVE** (the mirror in every read) → 1.00 · **MIXED-NULL** →
1.00 · **MIXED-CONFLICT** → 0.98 stays, plus operator review · **VOID(reason)**
→ file it and do not read further.

### 2a. The null base rate of this rule (DD-1, computed 2026-09-12)

Copy the row the tool printed for the measured `sigma`, and say which it was.

| sigma (pp) | i.i.d.: sign only | i.i.d.: sign AND median <= -0.5 | rho = 0.5: sign only | rho = 0.5: sign AND median |
|---|---|---|---|---|
| exact binomial, M = 7 | **6.25 %** (8/128) | — | — | — |
| 0.5 | 6.2 % | 0.6 % | 25.0 % | 9.4 % |
| 1.0 | 6.3 % | 3.8 % | 25.1 % | 20.5 % |
| 2.0 | 6.3 % | 5.7 % | 25.0 % | 24.2 % |
| >= 4.0 | 6.3 % | 6.2 % | 25.0 % | 24.9 % |

For M = 6 the sign-only binomial is 7/64 = 10.9 %. The three reads are the same
trades re-priced, so **no multiplicity protection is claimed**: the joint rate
is the single-read rate. At these sample sizes the study can *refuse* a large,
consistent, option-leg effect; it cannot *find* a small one.

**Measured `sigma` this read: `<...>` pp → the `<...>` row.**

## 3. What was read

| | |
|---|---|
| decision pin | `<pin_id>` (`window_days` 365, `holdout_days` 90, verified) |
| control pin | `<pin_id>` (same) |
| window_end | `<YYYY-MM-DD>` (on the SWEEP row; fit cells carry `holdout_start - 1`) |
| engine_version / identity | `<...>` / `<...>` (uniform, or the read is VOID) |
| symbols | `<...>` |
| one-shot run(s) | `<run_id>` (`t099`, and the seven non-measuring symbols) |
| OOS run | `<run_id>` (DD-10 prior year) or **none → condition 4 vacuous, single-window** |
| cells read | `<n>` pin + `<n>` standing |

## 4. Structural checks

Every one, pass or fail. **Any FAIL is a VOID: file it and stop.**

| check | result |
|---|---|
| `t100_arms_have_no_otm_roll_outs` | `<PASS/FAIL>` |
| `roll_fill_mode_matches_its_arm` | `<...>` |
| `no_pre_fc116_rows` | `<...>` |
| `rolls_executed_equals_itm_plus_otm` | `<...>` |
| `engine_version_uniform` / `engine_identity_uniform` | `<...>` |
| `placebo_gate` | `<...>` |
| `pin_base_equals_standing_base` | `<...>` |
| `M >= 6` | `<...>` |

## 5. The three reads

One table per read: per-symbol `delta ann` (pp), then `N+ / N- / N0`,
`median`, `sigma`.

| symbol | base ann | t100 ann | delta (pp) |
|---|---:|---:|---:|
| `<...>` | | | |

- `limit`: `<...>`
- `haircut`: `<...>`
- `noimm`: `<...>` — **DD-3's pre-registered prediction was
  `delta ann(noimm) ~ delta ann(limit)`.** State whether it held. A `noimm`
  read that moves the contrast by more than `MIN_EFFECT_PP` against 0.98 means
  the extra 0.98 rolls were being carried by resting fills — a finding about
  the model, recorded as such.

### 5a. Option-leg location (condition 2)

`<M, N+, N-, median, and whether the option leg alone would pass>`

### 5b. Holdout (condition 3)

`<agreeing/comparable>`. If `comparable < 4`, say so and say it does not block.
Say either way that AMZN and GOOGL rolled seven times each in the 09-11 holdout
and are `insufficient`, so they carry no number to take a sign from.

### 5c. Out-of-sample (condition 4)

`<median, M>`, or: no OOS instrument — **the record is labelled single-window
and condition 4 is vacuous.**

## 6. Reported but non-deciding

- per arm: `annualized_return`, `option_pnl`, `stock_pnl_realized`,
  `stock_pnl_unrealized`
- roll economics per arm: `rolls_executed`, `itm_rolls`, `otm_roll_outs`,
  `roll_net_credit` split, `failed_roll_btc_debit`, `roll_legs_resting /
  (resting + marketable)` **by roll kind** (artifacts)
- `roll_skips` by reason — `no_credit_candidate` (the CREDIT screen, where a
  thin long-dated print lands), `no_suitable_replacement` (the earlier
  horizon/strike/delta gate), `btc_quote_unavailable` (no print for the held
  contract that day; skip-only, so conservative on the arm that rolls more)
- the same contrast on `total_return`, `assignment_rate`, `cycles_completed`,
  `calls_sold`, `max_drawdown`
- the `noroll` control's delta vs base
- the `t099` dose-response arm: `otm_roll_outs(0.98) >= (0.99) >= (1.00) = 0` is
  the **expected** shape. A non-monotone count is a **finding** noted here, not
  a VOID (DD-7).
- replacement DTE at the ladder's edge, per arm (DD-2's tell for the residual
  truncation on **chained** rolls)
- **the paired-event table** — each day `base` executed an `otm_roll_out`, the
  base roll's `net_credit` and `old -> new strike`, and the terminal outcome of
  the *un-rolled* call on the `t100` side. That table is what the verdict
  *means* in trades.

## 7. The residual live-vs-sim list (DD-2) — verbatim, every time

| residual | sim | live | direction |
|---|---|---|---|
| decision quote | the day's close (the adapter's stock quote is `bid == ask == close`) | the 15:30 ET quote | both arms equally; a level shift in roll counts, not a bias between arms |
| fills | instant, at the modelled book capped by the limit (FC-116) | 120 s `_poll_order_fill` then cancel-and-verify; STO rung ladder | favours the roller: the sim never records `btc_timeout_canceled` or `stc_failed_naked_exposure` — under-counted on both arms, more on 0.98 |
| per-position budget | absent | FC-113: cancel-settle legs under-budgeted; a roll can be abandoned mid-cycle | favours the roller; both arms |
| roll reach, on rolls AFTER the first | the ladder stops at the lake's edge (`MAX_SWEEPABLE_DTE` 21) | `old_expiry + 14` = 29-36 days for a call already rolled out | **against 0.98**: fewer and shorter replacements on the arm that chains rolls. PR-1 removed the truncation on the FIRST roll only |
| the price a 0.98 roll buys back at | mid +/- 0.05 in imminence mode, on a call-leg premium the model marks ~32 % low | the ask, on the real premium | **the confound lands on the arm under test**; bounded by the bracket + `noimm`, not removed |

Bias handling (DD-3): the modelled half-spread is 2.46x the real RTH spread, so
`limit` and `haircut` bracket it and a KEEP must hold at both ends. The
resting-leg residual is **outside** that bracket and **always favours the
roller** ($0.10/share/roll, below both ends whenever the half-spread exceeds
$0.20); the `noimm` read is what bounds it.

## 8. Verdict

```
<paste the tool's verdict line verbatim>
```

**Resolves to:** `<0.98 | 1.00 | operator review>`.

Fragility monitor (DD-4 — four overlapping Saturdays are ONE read with the
edges moved, **not** out-of-sample): `<the four points, and whether the verdict
class reproduced on each>`. Any primary `limit` sign flip → MIXED-CONFLICT.

## 9. What follows

- **KEEP** → FC-078 amendment recording the measured reason; close FC-112.
- **MOVE / MIXED-NULL** → open the config-flip FC (its own PR, `Config` census
  test inverted to "no key differs", alert-twin check, FC-078 amendment).
  **Nothing in `config/`, `src/strategy/` or `deploy/` moves in FC-112.**
- **MIXED-CONFLICT** → operator review with the data; no config change.
- **VOID** → file the reason; do not read further; re-run when it is fixed.

### FC-078 amendment text

> `<the paragraph to paste into the FC-078 entry — what was measured, on what
> window, at what SHA, and what it implies for the roller's trigger>`

### Follow-up FCs opened

`<numbers and one line each, or "none">`. OQ-6 is a candidate: at exactly 1.00
with a $0.00 credit floor the roller buys back a ~50-delta call for pennies;
`min_net_credit_per_contract` is the lever. File it if `t100`'s
`itm_roll_credit` per roll is small.
