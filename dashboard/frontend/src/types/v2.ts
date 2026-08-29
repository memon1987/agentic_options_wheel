// FC-018 v2 dashboard types — shared across all v2 pages.

export interface ScorecardRow {
  symbol: string;
  trade_count: number | null;
  cycles_completed: number | null;
  total_premium: number | null;            // gross option premium
  put_premium: number | null;
  call_premium: number | null;
  realized_pnl: number | null;             // net option-side P&L (post-rolls)
  share_side_pnl: number | null;           // FC-019: OPTRD net cash (stock leg,
                                           //   includes acquisition cost of held shares)
  total_realized_pnl: number | null;       // FC-019: net CASH P&L (option + share cash)
  open_count: number | null;
  open_put_count: number | null;           // FC-031
  open_call_count: number | null;          // FC-031
  open_option_premium: number | null;      // FC-031: premium on still-open shorts
  put_assignment_count: number | null;
  called_away_count: number | null;
  early_close_count: number | null;
  expiration_count: number | null;
  cycle_capital_gain: number | null;
  avg_cycle_days: number | null;
  first_trade_time: string | null;
  last_trade_time: string | null;
  current_shares: number | null;
  current_acb_per_share: number | null;    // effective breakeven — never summed into P&L
  current_cumulative_net_premium: number | null;
  price_now: number | null;
  price_now_date: string | null;           // FC-031: bar date behind price_now
  price_at_start_date: string | null;      // FC-031: B&H baseline bar date
  bh_dollar_pnl: number | null;
  wheel_mtm_pnl: number | null;            // FC-031: net cash + held-share market value
  wheel_minus_bh: number | null;           // FC-031: wheel MTM − B&H (both marked)
  open_lot_shares: number | null;          // FC-031: FIFO open-lot walk
  open_lot_basis_per_share: number | null; // FC-031: display/breakeven only
  open_lot_acquired_at: string | null;
}

export interface AcbTimelineRow {
  event_time: string;
  event_date: string;
  activity_type: string;
  occ_symbol: string | null;
  order_id: string | null;
  expiration: string | null;
  option_type: string | null;
  side: string | null;
  qty: number | null;
  strike_price: number | null;
  premium_total: number | null;
  outcome: string | null;
  realized_pnl: number | null;
  net_premium_delta: number;
  shares_delta: number;
  cumulative_net_premium: number;
  running_shares: number;
  running_share_cost: number;
  acb_per_share: number | null;
  event_label: string;
}

export interface DecisionQualityRow {
  open_time: string;
  close_time: string | null;
  occ_symbol: string;
  option_type: string;
  strike_price: number;
  premium_total: number;
  close_price: number | null;
  close_qty: number | null;
  outcome: string;
  realized_pnl: number | null;
  capture_ratio: number | null;
  days_held: number | null;
}

export interface VsBuyAndHold {
  underlying: string;
  first_trade_date: string | null;
  first_strike: number | null;
  realized_pnl: number | null;            // option-leg net P&L
  share_side_pnl: number | null;          // FC-019: OPTRD net cash (stock leg)
  total_realized_pnl: number | null;      // FC-019: net cash P&L (option + share cash)
  total_premium: number | null;           // gross option premium received
  current_shares: number | null;
  current_acb_per_share: number | null;
  price_at_start: number | null;
  price_at_start_date: string | null;     // FC-031
  price_now: number | null;
  price_now_date: string | null;          // FC-031
  hypothetical_shares: number | null;
  bh_dollar_pnl: number | null;           // price-only — does not reinvest dividends
  wheel_mtm_pnl: number | null;           // FC-031: net cash + held-share market value
  wheel_minus_bh: number | null;          // FC-031: MTM vs MTM
}

export interface StockBar {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface PhaseTiming {
  cash_waiting: number;   // days
  short_put: number;
  long_stock: number;
  covered: number;
  first_event: string | null;
  last_event: string | null;
  current_phase: 'cash_waiting' | 'short_put' | 'long_stock' | 'covered';
}

export interface IngestHealth {
  trades_from_activities: string | null;
  equity_history_from_alpaca: string | null;
  stock_history_from_alpaca: string | null;
}

export interface PortfolioHistoryPoint {
  date: string;
  portfolio_value: number;
  cash: number | null;
  buying_power: number | null;
}

export interface AccountBaseline {
  starting_capital: number;
  source: string;
  jnlc_event_count?: number;
}

export interface PremiumByDayPoint {
  date: string;
  total_premium: number;
  put_premium: number;
  call_premium: number;
  trade_count: number;
}

export interface MetricsSummary {
  total_trades: number;
  total_puts_sold: number;
  total_early_closes: number;
  total_scans: number;
  total_errors: number;
  trading_days: number;
  total_premium: number;     // gross
  put_premium_30d: number;
  call_premium_30d: number;
  net_realized_pnl: number;  // sum of realized_pnl across closed events
  bought_back: number;       // gross − net (cash paid in roll buybacks)
  avg_premium: number;
  // FC-031: win_rate / return_30d removed (were hardcoded null since FC-018).
  // Cycle-level win rate: /api/v2/cycle-stats. Account returns: /api/v2/portfolio/returns.
}

// ---------------------------------------------------------------------
// FC-031 types
// ---------------------------------------------------------------------

export interface PortfolioReturns {
  xirr: number | null;
  twr_cumulative: number | null;
  twr_annualized: number | null;
  max_drawdown: number | null;             // fraction ≤ 0, close-to-close
  max_drawdown_peak: string | null;
  max_drawdown_trough: string | null;
  current_drawdown: number | null;
  max_drawdown_dollars: number | null;     // flow-adjusted $
  current_drawdown_dollars: number | null;
  days_since_first_deposit: number | null;
  deposit_count: number;
  single_deposit: boolean;                 // XIRR ≡ CAGR when true — label it
  nlv_source: string;
  as_of: string | null;
}

export interface EquityCurvePoint {
  date: string;
  wheel: number | null;                    // TWR index, base 100
  benchmark: number | null;                // SPY price index, base 100
}

export interface RegimeStats {
  count: number;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  expectancy: number | null;
  pnl_per_collateral_day: number | null;   // Σ P&L / Σ collateral·days — the only valid aggregate rate
}

export interface CycleStats extends RegimeStats {
  closed_count: number;
  open_count: number;
  open_mtm_to_date: number;
  excluded_overlapping_symbols: string[];  // FC-020 mis-pairing exclusions, disclosed
  regime_pre_fc029: RegimeStats;
  regime_post_fc029: RegimeStats;
  fc029_deploy_date: string;
}

// Per-leg trade stats — one shape for puts AND calls (Wheel Strategy
// Symmetry Principle). "Exercised" = assignment for puts, called-away for
// calls; the held-to-expiry exercise rate calibrates against the leg's
// delta band (sourced from the bot's live /config when reachable).
export interface OptionTradeStats {
  option_type: 'put' | 'call';
  closed_count: number;
  win_rate: number | null;
  net_pnl: number | null;
  exercised_count: number;
  expiration_count: number;
  early_close_count: number;
  pct_closed_early: number | null;
  exercise_rate_held_to_expiry: number | null;
  delta_band: [number, number];
}

export interface KnownGap {
  symbol: string;
  amount: number;
  as_of: string;
  reason: string;
}

export interface ShareCountMismatch {
  symbol: string;
  view_shares: number;
  live_shares: number;
}

export interface Reconciliation {
  nlv: number | null;
  deposits: number;
  deposits_source: string | null;
  realized_cash_pnl: number;
  open_option_premium: number;
  fees: number;
  live_market_value: number | null;
  residual: number | null;
  residual_net_of_known_gaps: number | null;
  known_gaps: KnownGap[];
  share_count_mismatches: ShareCountMismatch[];
  status: 'ok' | 'warn' | 'unknown';
}

export interface MonthlyCashflow {
  month: string;                           // YYYY-MM
  net_option_cashflow: number;
  put_net_cashflow: number;
  call_net_cashflow: number;
  gross_premium: number;
  buyback_cost: number;
  event_count: number;
}

export interface BotAnomaly {
  severity: 'critical' | 'warning';
  code: string;
  message: string;
  since: string | null;
  evidence: unknown;
}

// FC-065 Phase 4: sourced from the bot's own decision records, not from an
// OPASN-strike price inference. There is no drawdown pause any more (OQ-3) —
// the reported state is "held with no covered call written", which is the
// thing that actually costs money.
export interface UncoveredRow {
  symbol: string;
  shares: number;
  cost_basis_per_share: number | null;   // the floor the bot enforced
  last_price: number | null;
  underwater_pct: number | null;         // signed; NEGATIVE is below the floor
  uncovered_days: number | null;         // null = could not be derived
  outcome: string;                       // closed enum, see decision_record.py
  reason: string;
  run_id: string;
  last_decision_at: string;
}

export interface UncoveredSymbols {
  threshold_days: number;
  source: string;
  // False when the decision table could not be read. NEVER render an
  // all-clear on this: silence has three causes and none of them is
  // "everything is covered".
  decision_source_available?: boolean;
  uncovered: UncoveredRow[];
  unknown_uncovered_days: UncoveredRow[];
  share_count_mismatches: ShareCountMismatch[];
}

export interface AccountData {
  portfolio_value: number;
  cash: number;
  buying_power: number;
  equity: number;
  options_buying_power?: number;
  paper_trading?: boolean;
}

export interface LivePosition {
  symbol: string;
  qty: number | string;
  side: string;
  market_value: number | string;
  cost_basis: number | string;
  unrealized_pl: number | string;
  current_price?: number | string;
}

export interface FilteringStat {
  date_et: string;
  stage: string;
  result: string;
  total_events: number;
  unique_symbols: number;
  passed: number;
  blocked: number;
  reason: string | null;
  avg_premium: number | null;
  avg_delta: number | null;
  avg_dte: number | null;
}

export interface ErrorEvent {
  timestamp: string;
  date_et: string;
  event_type: string;
  error_type: string | null;
  error_message: string | null;
  symbol: string | null;
  underlying: string | null;
  component: string | null;
  recoverable: boolean | null;
  request_id: string | null;
}

export interface DailySummary {
  date_et: string;
  total_scans: number;
  total_opportunities: number;
  total_executions: number;
  total_errors: number;
  avg_scan_duration_sec: number;
  total_trades_failed: number;
}

// ---------------------------------------------------------------------------
// FC-060 Layer 4 — scenario sweeps ("Simulations", /sims).
//
// Three shapes, three owners:
//   SweepRow    — one `options_wheel.scenario_sweeps` row (plan D1). Written by
//                 the Job, read by `GET /api/v2/sweeps` and `.../{run_id}`.
//   SweepReport — the Layer 2 JSON report (`src/backtesting/scenarios/report.py`
//                 `render_json`), passed through by the API (plan D11).
//                 THIS BLOCK MIRRORS THAT PAYLOAD KEY-FOR-KEY. The UI renders
//                 what the runner computed; it never recomputes a verdict.
//   SweepSpec   — the D2 submit body.
// ---------------------------------------------------------------------------

/** `submitted` and `running` are live; the other three are terminal. */
export type SweepStatus = 'submitted' | 'running' | 'done' | 'failed' | 'deduplicated';

export const TERMINAL_SWEEP_STATUSES: readonly SweepStatus[] = ['done', 'failed', 'deduplicated'];

/**
 * Terminal = nothing will change, so polling must stop.
 *
 * An UNRECOGNISED status counts as NON-terminal on purpose: a status this build
 * has never heard of is a reason to keep looking, not a reason to declare the
 * run finished and freeze a half-rendered page.
 */
export const isTerminalSweepStatus = (s: string | null | undefined): boolean =>
  TERMINAL_SWEEP_STATUSES.includes(s as SweepStatus);

/** One `scenario_sweeps` row (plan D1). Every column is nullable but `run_id`. */
export interface SweepRow {
  run_id: string;
  sweep_key: string | null;
  status: SweepStatus;
  deduplicated_to: string | null;
  submitted_at: string;
  started_at: string | null;
  finished_at: string | null;
  submitted_via: string | null;
  execution_name: string | null;
  git_commit: string | null;
  engine_version: string | null;
  base_config_hash: string | null;
  base_config_json: string | null;
  spec_json: string | null;
  symbols: string[] | null;
  window_start: string | null;
  window_end: string | null;
  holdout_start: string | null;
  in_sample_only: boolean | null;
  scenario_count: number | null;
  cell_count: number | null;
  wall_seconds: number | null;
  materialise_seconds: number | null;
  replay_seconds: number | null;
  provider_fetches: number | null;
  bar_cache_hits: number | null;
  lake_summary_json: string | null;
  error: string | null;
  /**
   * Server-computed: still `submitted` past the container-start window (D3).
   *
   * A LABEL and nothing more — this backend cannot cancel an execution. It is
   * read rather than re-derived so the dashboard and the API can never disagree
   * about whether a run is stuck, which is exactly the kind of split-brain a
   * second clock buys you.
   */
  stuck?: boolean;
}

/**
 * One (scenario, symbol, window) cell — `ScenarioResult.as_dict()`.
 *
 * `measured` / `insufficient` / `low_activity` / errored PARTITION every cell:
 * exactly one is true (pinned by `tests/test_scenarios.py`). The UI renders each
 * of the four distinctly and NEVER renders an unmeasured cell as a return — an
 * `insufficient` cell is a statement about the WINDOW, not a measurement of the
 * arm, and showing it as a small number is how a sweep manufactures a ranking.
 */
export interface SweepResultRow {
  scenario: string;
  symbol: string;
  start: string;
  end: string;
  split: string;                 // 'fit' | 'holdout' | 'all'
  config_hash: string;
  scenario_hash: string;
  verdict: string | null;        // 'fit' | 'marginal' | 'unfit' | 'insufficient'
  demote: boolean | null;
  total_return: number | null;
  annualized_return: number | null;
  annualized_return_on_collateral: number | null;
  benchmark_return: number | null;
  excess_return: number | null;
  option_pnl: number | null;
  stock_pnl_realized: number | null;
  stock_pnl_unrealized: number | null;
  max_drawdown: number | null;
  win_rate: number | null;
  assignment_rate: number | null;
  puts_sold: number | null;
  calls_sold: number | null;
  cycles_completed: number | null;
  cycles_open: number | null;
  decision_days: number | null;
  days_in_position_fraction: number | null;
  bid_fill_return: number | null;
  verdict_flips_on_fill: boolean | null;
  replay_seconds: number | null;
  error: string | null;
  insufficient: boolean;
  low_activity: boolean;
  measured: boolean;
}

export interface SweepWindow {
  split: string;
  start: string;
  end: string;
}

/** `windows` on the wire: `{split: {start, end}}`, from the stored columns. */
export type SweepWindowMap = Record<string, { start: string; end: string }>;

/**
 * One row of the server's per-(scenario, split) summary.
 *
 * READ, never recomputed. Every median, min, max and count on the page comes
 * from here — three implementations of "which cells count" would be three
 * chances to average an `insuf` cell into a ranking.
 */
export interface SweepSummaryRow {
  scenario: string;
  split: string;
  median: number | null;
  min: number | null;
  max: number | null;
  measured: number;
  insufficient: number;
  low_activity: number;
  errors: number;
  demote_flags: number;
  delta_vs_base: number | null;
  delta_symbols: number;
}

export interface SweepDeltaCell {
  /** Median of the per-symbol deltas over the COMMON measured subset. */
  median: number | null;
  /** How many symbols that median is over — the `n=k` the UI must show. */
  symbols: number;
}

export interface SweepSignAgreement {
  agreeing: number;
  comparable: number;
}

export interface SweepBias {
  title: string;
  detail: string;
}

/** `render_json`'s payload (report.py:504-560), passed through by the API. */
export interface SweepReport {
  scenarios: string[];
  symbols: string[];
  windows: SweepWindow[];
  starting_cash?: number | null;
  run_sensitivity?: boolean | null;
  base_config_hash?: string | null;
  scenario_config_hashes?: Record<string, string>;
  scenario_hashes?: Record<string, string>;
  scenario_overrides?: Record<string, Record<string, unknown>>;
  scenario_fill_haircuts?: Record<string, number | null>;
  in_sample_only: boolean;
  min_days_in_position: number;
  timing?: {
    materialise_seconds?: Record<string, number>;
    replay_seconds?: Record<string, number>;
    wall_seconds?: number;
  };
  provider_calls?: {
    fetches?: number;
    bar_cache_hits?: number;
    during_replays?: number;
  };
  rows: SweepResultRow[];
  /** The server's aggregates. The page renders these; it never derives them. */
  summary: SweepSummaryRow[];
  /** null when the run had no holdout — there is nothing to agree about. */
  sign_agreement: Record<string, SweepSignAgreement> | null;
  /** split -> scenario -> delta. */
  delta_vs_base: Record<string, Record<string, SweepDeltaCell>>;
  known_biases: SweepBias[];
  cross_scenario_caveat: string;
  rejection_tally_caveat: string;
  /** Non-null IFF `in_sample_only`. Rendered verbatim, never paraphrased. */
  in_sample_banner: string | null;
  holdout_semantics: string | null;
  persisted?: boolean;
}

/**
 * `GET /api/v2/sweeps/{run_id}`.
 *
 * `results` is non-null ONLY for a `done` run. A `submitted`/`running` sweep
 * comes back with `grid: {}` and `splits: []`, which would otherwise render as
 * a finished report that measured nothing — the opposite of the truth.
 */
export interface SweepDetail {
  sweep: SweepRow;
  results: SweepReport | null;
  /**
   * The response exactly as served. "Download JSON" exports THIS, not the
   * normalised reconstruction: an export that quietly differed from what the
   * API returned would be useless for reproducing or filing a bug.
   */
  raw: unknown;
}

export interface SweepCaps {
  max_symbols: number;
  max_scenarios: number;
  max_cells: number;
  max_window_days: number;
  min_holdout_days: number;
  /** Present on the live allowlist; the form prefers these over its fallbacks. */
  min_starting_cash?: number;
  max_starting_cash?: number;
  max_spec_bytes?: number;
  running_lock_hours?: number;
}

export interface SweepPreset {
  /** The arm name written into the spec. */
  name: string;
  /** Human label for the chip, when the API supplies one. */
  label?: string;
  overrides: Record<string, unknown>;
  /** A preset may vary the fill assumption instead of a config key. */
  fill_haircut?: number;
}

/** `GET /api/v2/sweeps/allowlist` — the same allowlist the runner enforces. */
export interface SweepAllowlist {
  allowed: Array<{ key: string; description: string }>;
  rejected: Array<{ key: string; reason: string }>;
  presets: SweepPreset[];
  caps: SweepCaps;
}

/** One arm. `base` is IMPLICIT and reserved — never declared here. */
export interface SweepScenarioSpec {
  name: string;
  overrides: Record<string, unknown>;
  fill_haircut?: number;
}

/** The `POST /api/v2/sweeps` body (plan D2). */
export interface SweepSpec {
  symbols: string[];
  start: string;
  end: string;
  holdout_start?: string;
  starting_cash?: number;
  run_sensitivity?: boolean;
  scenarios: SweepScenarioSpec[];
}

/** 200 body. `deduplicated_to` means nothing was launched (plan D4). */
export interface SweepSubmitAccepted {
  run_id: string;
  status: SweepStatus;
  deduplicated_to?: string | null;
}

/** The bot's sanitized `/config`, proxied by `/api/live/config`. */
export interface LiveStrategyConfig {
  paper_trading?: boolean;
  stock_symbols?: string[];
  put_delta_range?: number[];
  call_delta_range?: number[];
}
