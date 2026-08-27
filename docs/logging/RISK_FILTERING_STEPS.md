# Risk Filtering Steps During Market Scan

> # ⚠️ STALE — DESCRIBES A PIPELINE THAT NO LONGER RUNS
>
> Generated 2025-10-03. That is the same day `842dcce` removed
> `WheelEngine.run_strategy_cycle()` from `/run` — so this document was describing a dead
> path from the day it was written, three days before the live account's first fill.
> **FC-068 (2026-08-01) deleted that path.**
>
> **Gone, with the code that emitted them:**
> - **Stage 2** — gap-risk analysis (`GapDetector.filter_stocks_by_gap_risk`).
>   **FC-069 item 5 deleted the detector class and all twelve `gap_risk_controls`
>   knobs on 2026-08-04** (code at pre-sweep `main` SHA `afb6698`). Gap risk is
>   absent by decision; FC-049 owns any evidence-based revival.
> - **Stage 3** — `max_stocks_evaluated_per_cycle`. **Knob deleted** by FC-068 (it was
>   `null`, so it never limited anything).
> - **Stage 4** — execution gap check (`GapDetector.can_execute_trade`).
> - **Stage 5** — wheel-state phase gating. The state layer it read has never been
>   populated (`STATE_STORAGE_BUCKET` unset since inception).
> - **Stage 6** — the engine's own duplicate/open-order guard. Live duplicate protection
>   is `options_scanner._has_existing_position` +
>   `execution_engine.filter_duplicate_opportunities` + the opportunity store's
>   idempotency machinery. The *open-order* window stage 6 uniquely covered is covered by
>   nothing on the live path, before or after — that is FC-009's standing territory.
> - **Stage 9** — `max_new_positions_per_cycle`. **Knob deleted** by FC-068 (also `null`).
> - The **covered-call drawdown pause** (`covered_call_drawdown_pause`), deleted per
>   FC-065 OQ-3. `call_drawdown_pause_threshold` and its `/config` field were
>   **removed by FC-069 item 9** (2026-08-04). There is no drawdown pause anywhere.
>
> **What actually gates a trade today:** stage 1 (price/volume band) → the options-chain
> filters (`find_suitable_puts` / `find_suitable_calls`, including the cost-basis floor on
> calls) → put position sizing → `ExecutionEngine`'s two-pool **batch selection**
> (`selection_dropped`, with a machine-readable `reason`) → the execute-time guards
> (`naked_call_blocked`, the FC-050 cost-basis floor, the wrong-seller guards). See
> `docs/logging/FILTERING_STAGES_LOGGING.md`'s banner for the event names to search on.
>
> Full rewrite is **FC-069 item 14**.

**Complete flow diagram of all risk filters applied during each hourly market scan**

Generated: October 3, 2025
Strategy: Options Wheel (Cash-Secured Puts → Assignment → Covered Calls)

---

## Overview

Each hourly scan at `:15` (execution time) filters the 14-stock universe through **7 major filter stages** before considering any trade.

**Stock Universe**: 14 symbols
→ `AAPL`, `MSFT`, `GOOGL`, `AMZN`, `NVDA`, `AMD`, `QQQ`, `SPY`, `IWM`, `UNH`, `F`, `PFE`, `KMI`, `VZ`

---

## Filter Pipeline

### STAGE 1: Basic Stock Suitability
**Component**: `market_data.filter_suitable_stocks()` → `get_stock_metrics()`
**Location**: [src/api/market_data.py:33-62](../src/api/market_data.py#L33-L62)

#### Filters Applied:

1. **Price Range Filter**
   - **Min**: $10.00
   - **Max**: $400.00
   - **Config**: [config/settings.yaml:30-31](../config/settings.yaml#L30-L31)
   - **Rejects**: Stocks outside price range

2. **Volume Filter**
   - **Minimum**: 2,000,000 shares/day average (30-day)
   - **Config**: [config/settings.yaml:32](../config/settings.yaml#L32)
   - **Rejects**: Illiquid stocks

3. **Combined Check**
   - Must pass BOTH price AND volume criteria
   - **Result**: `suitable_for_wheel = true/false`

**Output**: `suitable_stocks` (sorted by volume descending, volatility ascending)

---

### STAGE 2: Gap Risk Analysis (Historical)
**Component**: `gap_detector.filter_stocks_by_gap_risk()`
**Location**: [src/risk/gap_detector.py:304-341](../src/risk/gap_detector.py#L304-L341)

For each suitable stock, performs historical analysis:

#### Sub-Filter 2A: Historical Gap Frequency
**Function**: `_calculate_overnight_gaps()`
**Lookback**: 30 days
**Config**: [config/settings.yaml:66-67](../config/settings.yaml#L66-L67)

**Analysis**:
- Counts overnight gaps > 2.0% (quality_gap_threshold)
- Calculates: `gap_frequency = significant_gaps / total_days`
- **Rejects if**: `gap_frequency > 0.15` (15%)

**Example from Oct 2**:
- AMD: gap_frequency = 20.6% → **REJECTED** ❌
- UNH: gap_frequency = 8.8% → Passes this check ✅

#### Sub-Filter 2B: Historical Volatility
**Function**: `_calculate_historical_volatility()`
**Config**: [config/settings.yaml:82](../config/settings.yaml#L82)

**Analysis**:
- Calculates annualized volatility from 30-day returns
- Formula: `std(returns) * sqrt(252)`
- **Rejects if**: `volatility > 0.40` (40%)

#### Sub-Filter 2C: Current Day Gap (if market open)
**Function**: `_detect_current_gap()`
**Config**: [config/settings.yaml:65](../config/settings.yaml#L65)

**Analysis**:
- Compares today's open vs yesterday's close
- **Rejects if**: Current gap > 5.0% (max_overnight_gap_percent)

**Combined Decision**: `_is_suitable_for_trading()`
- Must pass ALL THREE: gap frequency, volatility, current gap
- If ANY fail → stock excluded from `gap_filtered_symbols`

**Output**: `gap_filtered_symbols` (list of symbols passing gap risk)

---

### STAGE 3: Stock Evaluation Limit (Configurable)
**Component**: `wheel_engine._find_new_opportunities()`
**Location**: [src/strategy/wheel_engine.py:273-281](../src/strategy/wheel_engine.py#L273-L281)

**Configurable Limit**:
- **Parameter**: `max_stocks_evaluated_per_cycle`
- **Config**: [config/settings.yaml:39](../config/settings.yaml#L39)
- **Default**: `null` (no limit - evaluate all gap-filtered stocks)
- **Can be set to**: Any positive integer to limit evaluation (e.g., 5, 10)

**Purpose**:
- Control computational load per cycle
- Focus on highest-ranked opportunities when needed
- Fully configurable: set to number or `null` to disable

**Current Setting**: `null` (no limit applied)

---

### STAGE 4: Execution Gap Check (Real-time)
**Component**: `gap_detector.can_execute_trade()`
**Location**: [src/risk/gap_detector.py:343-426](../src/risk/gap_detector.py#L343-L426)

**Per-stock real-time check before any trade consideration:**

#### Real-Time Gap Calculation:
1. Get current market price (mid of bid/ask)
2. Get previous trading day's close
3. Calculate: `gap_percent = ((current - previous) / previous) * 100`

**Hard Stop**:
- **Threshold**: 1.5% (execution_gap_threshold)
- **Config**: [config/settings.yaml:74](../config/settings.yaml#L74)
- **Action**: If `|gap_percent| > 1.5%` → **Block trade**, skip to next stock

**Failure Modes**:
- `no_previous_close_data` - Can't find previous close
- `execution_gap_exceeded` - Gap too large
- `gap_check_error` - Exception during check

**Log Output**: "Trade execution blocked by gap check" with reason

---

### STAGE 5: Wheel State Check — **DELETED, does not exist**

> **This stage never ran on the live path, and its code is now gone.**
>
> It was described as `wheel_state.can_sell_puts()` / `can_sell_calls()` gating
> trades by wheel phase, called from the `WheelEngine` orchestration path. Two
> things ended it:
>
> - **FC-068 (2026-08-01)** deleted that orchestration path. It had not been
>   called by production since 2025-10-03 — three days *before* the live
>   account's first fill. Production runs `/scan` → `/run` → `ExecutionEngine`
>   → `PutSeller`/`CallSeller`, none of which ever consulted wheel state.
> - **FC-069 item 8 stage 2 (2026-08-04)** deleted `can_sell_puts` and
>   `can_sell_calls` themselves, along with the rest of the fictional state
>   layer. The state they read was never persisted (`STATE_STORAGE_BUCKET`
>   unset since inception — FC-039), so even had the gate been called it would
>   have been evaluating an empty dict rebuilt on every request.
>
> **What actually prevents selling a put on a symbol you already hold** is the
> scanner-level held-position skip (FC-069 item 12, event
> `put_scan_skipped_existing_position`) and `select_batch`'s ledgers — not a
> phase machine. `WheelPhase` survives only as a label on assignment
> telemetry.
>
> The full rewrite of this document is FC-069 item 14; this note exists so the
> stage is not read as a live control in the meantime.

---

### STAGE 6: Existing Position Check
**Component**: `_has_existing_option_position()`
**Location**: [src/strategy/wheel_engine.py:306-307](../src/strategy/wheel_engine.py#L306-L307)

**Filter**:
- Checks Alpaca account for existing option positions in this symbol
- **Skip if**: Already have options in this underlying
- **Purpose**: Prevent multiple concurrent positions in same stock

---

### STAGE 7: Options Chain Criteria (Put-Specific)
**Component**: `market_data.find_suitable_puts()` → `_meets_put_criteria()`
**Location**: [src/api/market_data.py:204-231](../src/api/market_data.py#L204-L231)

**For each put contract in the options chain:**

#### Filter 7A: Days to Expiration (DTE)
- **Target**: 7 days
- **Max**: Must be ≤ 7 days
- **Config**: [config/settings.yaml:14](../config/settings.yaml#L14)
- **Rejects**: Options expiring > 7 days out

#### Filter 7B: Minimum Premium
- **Threshold**: $0.50 per contract
- **Config**: [config/settings.yaml:26](../config/settings.yaml#L26)
- **Rejects**: Puts with mid_price < $0.50

#### Filter 7C: Delta Range
- **Range**: 0.10 to 0.20 (absolute value)
- **Config**: [config/settings.yaml:18](../config/settings.yaml#L18)
- **Rejects**: Puts outside delta range
- **Purpose**: Target ~10-20% probability of assignment

#### Filter 7D: Liquidity Check
- **Minimum**: Open Interest ≥ 10 contracts OR volume > 0
- **Rejects**: Illiquid contracts
- **Purpose**: Ensure ability to close position if needed

#### Ranking:
After filtering, remaining puts are ranked by:
- **Score**: `annual_return * (1 - |delta|)`
- **Preference**: Higher return, lower delta (less likely assignment)
- **Best put**: First in sorted list selected

**Output**: Best put opportunity or `None` if no suitable options

---

### STAGE 8: Position Sizing Validation
**Component**: `put_seller._calculate_position_size()`
**Location**: [src/strategy/put_seller.py](../src/strategy/put_seller.py)

**Checks**:

1. **Account Buying Power**
   - Requires: `strike_price * 100 * contracts` in cash
   - For multiple contracts: scales accordingly

2. **Max Exposure Per Ticker**
   - **Limit**: $25,000 per underlying
   - **Config**: [config/settings.yaml:39](../config/settings.yaml#L39)

3. **Portfolio Allocation**
   - **Max total**: 80% of portfolio
   - **Min cash reserve**: 20%
   - **Config**: [config/settings.yaml:44-46](../config/settings.yaml#L44-L46)

**Rejects if**: Insufficient buying power or exceeds risk limits

---

### STAGE 9: New Positions Per Cycle Limit (Configurable)
**Component**: `wheel_engine._find_new_opportunities()`
**Location**: [src/strategy/wheel_engine.py:328-334](../src/strategy/wheel_engine.py#L328-L334)

**Configurable Limit**:
- **Parameter**: `max_new_positions_per_cycle`
- **Config**: [config/settings.yaml:40](../config/settings.yaml#L40)
- **Default**: `null` (no limit - open as many positions as pass all filters)
- **Can be set to**: Any positive integer to limit new positions per cycle (e.g., 1, 2, 3)

**Purpose**:
- Control rate of portfolio buildup
- Conservative gradual entry when needed
- Fully configurable: set to number or `null` to disable

**Current Setting**: `null` (no limit - portfolio controlled by Stage 6 & 8)

**Note**: Even with no limit, natural portfolio constraints still apply:
- Stage 6: No duplicate positions in same underlying
- Stage 8: $25k per ticker, 80% portfolio max, 10 total positions
- Buying power availability

---

## Summary: Filter Funnel

```
14 Stocks (universe)
    ↓
STAGE 1: Price & Volume
    ↓ (~12-14 stocks typically pass)
STAGE 2: Gap Risk Analysis
    ├── Historical Gap Frequency ≤ 15%
    ├── Historical Volatility ≤ 40%
    └── Current Gap ≤ 5%
    ↓ (~7-10 stocks pass)
STAGE 3: Stock Evaluation Limit (CONFIGURABLE)
    └── Default: null (no limit)
    └── Can set: e.g., 5 stocks max
    ↓ (Currently: ALL gap-filtered stocks evaluated)
STAGE 4: Execution Gap Check (per stock)
    └── Real-time gap ≤ 1.5%
    ↓
STAGE 5: Wheel State Check (per stock)
    └── Can sell puts/calls in current phase?
    ↓
STAGE 6: Existing Position Check (per stock)
    └── No existing options in this symbol?
    ↓
STAGE 7: Options Chain Criteria (per contract)
    ├── DTE ≤ 7 days
    ├── Premium ≥ $0.50
    ├── Delta: 0.10-0.20
    └── Liquidity ≥ 10 OI or volume > 0
    ↓
STAGE 8: Position Sizing (per opportunity)
    ├── Sufficient buying power?
    ├── ≤ $25k exposure per ticker?
    ├── ≤ 80% portfolio allocation?
    └── ≤ 10 total positions (config)
    ↓
STAGE 9: New Positions Per Cycle Limit (CONFIGURABLE)
    └── Default: null (no limit)
    └── Can set: e.g., 1, 2, 3 positions max
    ↓ (Currently: no limit)
0-N Trades Executed (limited by config, buying power, and risk controls)
```

---

## Why Stocks Get Filtered Out

### Common Rejection Reasons (by stage):

**Stage 1 - Price/Volume**:
- Stock too cheap (< $10) or expensive (> $400)
- Low liquidity (< 2M avg volume)

**Stage 2 - Gap Risk**:
- **Most common rejection**
- AMD: High gap frequency (20.6% > 15%)
- Volatile tech stocks often fail here

**Stage 4 - Execution Gap**:
- Intraday movement > 1.5% from previous close
- Market volatility during trading hours

**Stage 5 - Wheel State**:
- Already holding stock (can't sell puts)
- No stock position (can't sell calls)

**Stage 6 - Existing Position**:
- Already have options in this underlying
- Portfolio concentration limit

**Stage 7 - Options Chain**:
- **Very common**
- No 7-day options available
- Strikes outside delta range
- Premiums too low (< $0.50)
- Illiquid options (OI < 10)

**Stage 8 - Position Sizing**:
- Insufficient buying power
- Would exceed $25k single-ticker limit
- Portfolio already at 80% allocation
- Already at max 10 total positions

**Stage 3 - Stock Evaluation Limit** (CONFIGURABLE):
- Currently disabled (`null`)
- Can limit to N stocks if set (e.g., 5 stocks/cycle)

**Stage 9 - New Positions Limit** (CONFIGURABLE):
- Currently disabled (`null`)
- Can limit to N positions if set (e.g., 1 position/cycle for conservative entry)

---

## Configuration Reference

> ⚠️ **STALE — do not use as a config reference.** Every "Config Line" number
> below has drifted, `max_exposure_per_ticker` never was `$25,000` by the time
> this table was written, and **eight of these keys no longer exist**:
> `max_stocks_evaluated_per_cycle` and `max_new_positions_per_cycle` (deleted by
> FC-068); `max_gap_frequency`, `max_historical_vol`, `max_overnight_gap_percent`,
> `execution_gap_threshold`, `quality_gap_threshold`, `max_exposure_per_ticker`
> and `max_portfolio_allocation` (deleted by FC-069 S1, 2026-08-04). The
> authoritative list of live keys with their verified consumers is the knob
> census in `docs/plans/fc-069.md`; the live gate inventory is `docs/gates.md`.
> Kept as the historical record of what this document once claimed.

All thresholds are configurable in [config/settings.yaml](../config/settings.yaml)

| Filter | Parameter | Default | Config Line |
|--------|-----------|---------|-------------|
| Price Range | min_stock_price | $10.00 | 30 |
| Price Range | max_stock_price | $400.00 | 31 |
| Volume | min_avg_volume | 2M shares | 32 |
| **Stock Eval Limit** | **max_stocks_evaluated_per_cycle** | **null (no limit)** | **39** |
| **Position Limit** | **max_new_positions_per_cycle** | **null (no limit)** | **40** |
| Gap Frequency | max_gap_frequency | 15% | 67 |
| Historical Vol | max_historical_vol | 40% | 82 |
| Current Gap | max_overnight_gap_percent | 5.0% | 65 |
| Execution Gap | execution_gap_threshold | 1.5% | 74 |
| Quality Gap | quality_gap_threshold | 2.0% | 71 |
| Put DTE | put_target_dte | 7 days | 14 |
| Put Premium | min_put_premium | $0.50 | 26 |
| Put Delta | put_delta_range | [0.10, 0.20] | 18 |
| Max Exposure | max_exposure_per_ticker | $25,000 | 43 |
| Portfolio Limit | max_portfolio_allocation | 80% | 47 |

---

## Monitoring Filters in Production

### Key Logs to Watch:

**After Stage 1**:
```json
{
  "event": "Filtered suitable stocks",
  "total_analyzed": 14,
  "suitable_count": N
}
```

**After Stage 2**:
```json
{
  "event": "Evaluating new opportunities with wheel state logic",
  "suitable_stocks": N,
  "gap_filtered_stocks": M,
  "gap_filtered_symbols": ["MSFT", "GOOGL", ...],
  "filtered_out_symbols": ["AMD", "UNH"]
}
```

**Stage 3 (if limit enabled)**:
```json
{
  "event": "Applying stock evaluation limit",
  "max_stocks": 5,
  "total_available": 10,
  "evaluating": 5
}
```

**Stage 2 Rejections**:
```json
{
  "event_type": "stock_filtered_by_gap_risk",
  "symbol": "AMD",
  "gap_risk_score": 0.74,
  "gap_frequency": 0.206,
  "historical_volatility": 0.35
}
```

**Stage 4 Blocks**:
```json
{
  "event": "Trade execution blocked by gap check",
  "symbol": "AAPL",
  "reason": "execution_gap_exceeded",
  "gap_percent": 1.8
}
```

**Stage 9 (if limit enabled)**:
```json
{
  "event": "Reached max new positions per cycle limit",
  "max_positions": 1,
  "positions_found": 1
}
```

**Final Summary**:
```json
{
  "event": "New opportunities search completed",
  "opportunities_found": N,
  "put_opportunities": N,
  "call_opportunities": N
}
```

---

## Recent Improvements (Oct 3, 2025)

1. **Fixed datetime bug** in gap detector affecting 5 stocks
   - [Commit bcbbb23](https://github.com/memon1987/agentic_options_wheel/commit/bcbbb23)

2. **Fixed logging configuration** to see all filter decisions
   - [Commit 9fea03d](https://github.com/memon1987/agentic_options_wheel/commit/9fea03d)

3. **Added enhanced debug logging** for gap filtering
   - Shows exactly which stocks pass/fail at each stage

---

**Next**: Monitor first hourly execution at 10:00am ET today to see improved visibility into filtering decisions.
