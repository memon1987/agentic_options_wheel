#!/usr/bin/env python3
"""FC-034 (FC-042 Track B2) — is a flat $0.50 put-premium floor the right *shape*?

PURPOSE
-------
`min_put_premium: 0.50` (config/settings.yaml) is a fixed dollar hurdle applied
identically to a $12 stock and a $340 one. FC-032's coverage gate found F, PFE,
KMI and VZ have ~zero usable decision days despite 122/122 bar coverage, and
FC-042 Track C found VZ collects $0 dividends because it never holds shares.
FC-034 asks: **demote those four, or re-shape the threshold?**

This harness produces the evidence. It never modifies production code or config;
every arm is injected into a *fresh* `Config` object (and, for the non-flat
shapes, by wrapping `MarketDataManager._check_{put,call}_criteria_detailed` for
the duration of one run).

LAYERS
------
  chain    Chain-level census, no engine. Walks every decision day, records
           every priced put/call in the delta band, and then evaluates ANY floor
           shape offline from that one pass. Cheap, exhaustive, and it separates
           "the contract did not exist" from "the contract existed and did not
           pay the floor". Also runs a parameter sweep the engine cannot afford.

  engine   The A/B proper. Drives the real backtest engine once per (symbol,
           arm), reporting trades enabled, return on collateral, total return,
           max drawdown, days-in-position fraction, win rate, assignment rate
           and the engine's own fitness verdict (`insufficient` included —
           a two-trade annualized number is not a result).

  fills    The real-fills layer, and the reason this study exists in this shape.
           FC-036 proved engine-only A/B can mislead: it called a threshold
           "free" that 330 real fills showed was a $1,900 tax. Here we (a) prove
           from production data which underlyings actually traded, and (b) replay
           each proposed floor against every real sell-to-open fill to price what
           the re-shape would have *destroyed* on the names that already work.

  report   Assembles the tables the write-up quotes.

USAGE
-----
    # optional: parallel cold-cache warm (see run_prefetch)
    python tools/diagnostics/fc034_premium_floor_study.py prefetch --symbol F \
        --shard 0 --shards 6
    python tools/diagnostics/fc034_premium_floor_study.py chain --symbol F
    python tools/diagnostics/fc034_premium_floor_study.py engine --symbol F
    python tools/diagnostics/fc034_premium_floor_study.py fills
    python tools/diagnostics/fc034_premium_floor_study.py report

Output defaults to $FC034_OUT or /tmp/fc034. One symbol per process; arms within
a symbol share the warm parquet chain cache (the strike window the simulator
requests is derived from bars alone, so it is arm-independent — see
`Simulator._strike_anchors` — and every arm therefore hits the same files).

Companion write-up: docs/investigations/fc-034-premium-floor-ab.md
Plan: docs/plans/fc-042.md (Track B2); FC entry: FC-034.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# --------------------------------------------------------------------------- #
# Study parameters — PRE-REGISTERED before any result was looked at.
# --------------------------------------------------------------------------- #

WINDOW_START = date(2024, 2, 1)   # Alpaca options-history floor
WINDOW_END = date(2026, 7, 24)

# The four names FC-032's coverage gate found structurally untradeable...
EXCLUDED_COHORT = ("F", "PFE", "KMI", "VZ")

# ...and the two engine controls. Justification (all figures measured):
#   * Both appear in the 330 real sell-to-open fills, so they demonstrably work
#     in production: AAPL 8 puts + 3 calls, AMZN 26 puts + 18 calls ($13,306).
#   * Both stay inside the stage-1 price band [min_stock_price 10,
#     max_stock_price 400] for the ENTIRE window (AAPL 165.00-340.08, AMZN
#     144.52-274.99). SPY (454.76-759.57) and QQQ (385.05-746.16) never do —
#     they are excluded by the *price cap*, not by the premium floor, so they
#     cannot function as controls for this question. AMD (78-581), MSFT
#     (353-542), UNH (238-625) and GOOGL (129-403) all breach 400 for part of
#     the window, which would confound arm comparison with stage-1 dropouts.
#   * Neither split in 2023-12-01..2026-07-28 (detect_split → None). NVDA did
#     (10:1 on 2024-06-10), which would force a truncated, non-comparable window.
#
# IWM was the first choice (a $175-300 ETF is the closest liquid name to the
# $12-48 cohort) and was dropped from the ENGINE layer on cost, not on merit:
# its $1 strike ladder gives it a 1042-contract universe on a sample day
# against AAPL's 408 and AMZN's 372, and each IWM chain build took ~13 min under
# the trading API's contract-discovery throttle — roughly 40x AAPL. It remains a
# control in the `fills` layer, where it carries the study's strongest control
# evidence (22 of its 35 real fills fail arm B) from real money rather than
# simulation.
CONTROLS = ("AAPL", "AMZN")

STUDY_SYMBOLS = EXCLUDED_COHORT + CONTROLS

# Live strategy values, for reference (config/settings.yaml @ this commit).
LIVE_MIN_PUT_PREMIUM = 0.50
LIVE_MIN_CALL_PREMIUM = 0.30
# The live call floor is 0.6x the live put floor. Every re-shaped arm preserves
# that ratio, so the arms change the *shape* of the threshold and nothing else.
CALL_RATIO = LIVE_MIN_CALL_PREMIUM / LIVE_MIN_PUT_PREMIUM

# Absolute tick floor applied to the two scale-free arms. Without it, arm C is
# degenerate: an 8%-annualized floor at DTE=0 is $0.00, and at DTE=1 on a $12
# stock it is $0.003 — i.e. no floor at all. $0.05/share = $5.00 per contract
# against the engine's $0.04/contract fee, so it is the smallest credit that is
# still unambiguously worth the ticket. Reported explicitly because it is an
# arm parameter, not a detail: the `chain` layer also reports every arm with the
# tick floor removed so its effect is visible rather than assumed.
TICK_FLOOR = 0.05

# The rejection bucket label. `SimulationResult.rejections` is keyed by
# DESCRIPTION (src/backtesting/engine/rejections.py::_REASONS), not by the raw
# event name; `no_suitable_puts` maps to this string. Verified at runtime by
# `engine` (it asserts the key it reads is one the tally can actually emit).
STAGE7_REJECTION_KEY = "no put cleared delta/DTE/premium (stage 7)"


# --------------------------------------------------------------------------- #
# Arms
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Arm:
    """One premium-floor *shape*.

    ``put_floor(strike, dte)`` / ``call_floor(strike, dte)`` return the minimum
    acceptable per-share mid price for a contract. Arm A ignores both arguments,
    which is precisely the property under examination.
    """

    key: str
    label: str
    kind: str            # flat | pct_of_strike | annualized_roc
    put_param: float
    tick_floor: float = 0.0

    @property
    def call_param(self) -> float:
        return self.put_param * CALL_RATIO

    def _floor(self, param: float, strike: float, dte: int) -> float:
        if self.kind == "flat":
            base = param
        elif self.kind == "pct_of_strike":
            base = param * strike
        elif self.kind == "annualized_roc":
            # premium/strike * 365/dte >= param  <=>  premium >= param*strike*dte/365
            base = param * strike * max(dte, 0) / 365.0
        else:  # pragma: no cover - guarded by ARMS construction
            raise ValueError(f"unknown arm kind {self.kind}")
        return max(base, self.tick_floor)

    def put_floor(self, strike: float, dte: int) -> float:
        return self._floor(self.put_param, strike, dte)

    def call_floor(self, strike: float, dte: int) -> float:
        return self._floor(self.call_param, strike, dte)

    @property
    def is_flat(self) -> bool:
        return self.kind == "flat"


ARM_A = Arm("A", "$0.50 flat (status quo)", "flat", LIVE_MIN_PUT_PREMIUM)
ARM_B = Arm("B", "0.40% of strike", "pct_of_strike", 0.004, TICK_FLOOR)
ARM_C = Arm("C", ">=8% annualized return on collateral", "annualized_roc", 0.08, TICK_FLOOR)
ARMS = (ARM_A, ARM_B, ARM_C)

# Chain-layer-only sweep. Free once the chain census exists, and it answers
# "was 0.4% / 8% a lucky pick?" without 30 more engine runs.
SWEEP = (
    [Arm("A", f"${p:.2f} flat", "flat", p) for p in (0.10, 0.25, 0.50, 1.00)]
    + [Arm("B", f"{p*100:.2f}% of strike", "pct_of_strike", p, TICK_FLOOR)
       for p in (0.002, 0.004, 0.006, 0.010)]
    + [Arm("C", f"{p*100:.0f}% ann. RoC", "annualized_roc", p, TICK_FLOOR)
       for p in (0.04, 0.06, 0.08, 0.12)]
)


# --------------------------------------------------------------------------- #
# Pre-registered decision rules (verbatim; evaluated by `report`)
# --------------------------------------------------------------------------- #

PREREGISTERED_RULES = {
    "outcomes": {
        "demote": "drop F/PFE/KMI/VZ from config `stocks.symbols`; leave the "
                  "$0.50 flat floor unchanged.",
        "reshape": "replace the flat dollar floor with a scale-free shape "
                   "(arm B or C) and keep all four names.",
        "hybrid": "keep the flat floor globally; add a per-symbol override for "
                  "any excluded name that passes R1+R2. Chosen when R1/R2 pass "
                  "but a control rule (R3/R4) fails.",
    },
    "R1_enablement": (
        "A re-shape is on the table only if some arm lifts an excluded name's "
        "days_in_position_fraction to >= 0.25 (the engine's own "
        "MIN_DAYS_IN_POSITION untradeability line) on >= 2 of the 4 names. If no "
        "arm does, the cohort is untradeable for reasons the floor cannot fix "
        "=> DEMOTE."
    ),
    "R2_worth_doing": (
        "Each name R1 enables must, under that arm, show positive annualized "
        "return_on_collateral clearing the engine's RISK_FREE_RATE floor of 4%, "
        "with verdict 'fit' or 'marginal' (never 'insufficient' or 'unfit')."
    ),
    "R3_control_non_regression": (
        "On BOTH controls the enabling arm must not: (a) cut annualized "
        "return_on_collateral by more than 10% relative; (b) worsen max_drawdown "
        "by more than 2 percentage points; (c) cut puts_sold by more than 10%."
    ),
    "R4_real_fills_non_regression": (
        "Applying the enabling arm's floor to the 330 real sell-to-open fills "
        "must not retroactively block fills whose realized option P&L exceeds "
        "10% of total premium collected (~$6.9k of $68,518). The FC-036 lesson: "
        "engine-only 'free' is not free."
    ),
    "R5_statistical_adequacy": (
        "Any per-symbol claim resting on < 10 closed cycles is directional only "
        "and cannot by itself satisfy R1 or R2 (cf. KMI's +138% annualized on a "
        "single 8-day cycle in 273 days)."
    ),
    "decision": "RE-SHAPE iff R1 and R2 and R3 and R4 hold for at least one arm; "
                "HYBRID if R1+R2 hold but R3 or R4 fails; otherwise DEMOTE.",
}


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #

def _default_out() -> Path:
    return Path(os.environ.get("FC034_OUT", "/tmp/fc034"))


def _write(out: Path, name: str, payload) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"  wrote {path}")
    return path


def _read(out: Path, name: str):
    path = out / name
    if not path.exists():
        raise SystemExit(f"missing {path} — run the producing subcommand first")
    return json.loads(path.read_text())


# Alpaca signals throttling with more than one wording, and the two SDK paths
# this harness drives do NOT agree. The market-data client raises a
# "too many requests" error; the *trading* client (which serves
# `get_option_contracts`, i.e. contract discovery) raises
# `APIError: {"code":42910000,"message":"rate limit exceeded"}`. Matching only
# the first phrase — as tools/diagnostics/fc036_gap_gate_study.py does — leaves
# contract discovery unprotected, and a sharded prefetch dies on it within
# minutes. Match on any of them.
_THROTTLE_MARKERS = ("too many requests", "rate limit", "42910000", "429")


def _is_throttle(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(m in text for m in _THROTTLE_MARKERS)


def _install_rate_limit_retry(max_tries: int = 10) -> None:
    """Wrap the provider's data endpoints with throttle backoff.

    Analysis-side only — production code is never touched. Alpaca's Basic plan
    caps at 200 req/min and a full 2.5-year chain build issues thousands of
    requests; parallel study processes trip it.
    """
    import time as _time

    from src.backtesting.data.alpaca_provider import AlpacaDataProvider

    if getattr(AlpacaDataProvider, "_fc034_retry_installed", False):
        return

    def _wrap(name):
        original = getattr(AlpacaDataProvider, name)

        def wrapper(self, *a, **kw):
            delay = 2.0
            for attempt in range(max_tries):
                try:
                    return original(self, *a, **kw)
                except Exception as exc:  # noqa: BLE001 - only throttles retried
                    if not _is_throttle(exc) or attempt == max_tries - 1:
                        raise
                    _time.sleep(delay)
                    delay = min(delay * 2, 60.0)
            raise RuntimeError("unreachable")

        wrapper.__name__ = name
        setattr(AlpacaDataProvider, name, wrapper)

    for method in ("get_option_bars", "get_contract_universe", "get_stock_bars"):
        _wrap(method)
    AlpacaDataProvider._fc034_retry_installed = True


# --------------------------------------------------------------------------- #
# chain — the census, no engine
# --------------------------------------------------------------------------- #

def _decision_context(symbol: str, start: date, end: date):
    """Trading days, closes and the simulator's own strike anchors.

    Anchors are replicated through the real `Simulator` object rather than
    reimplemented, so this layer's chain requests are byte-identical to the
    engine layer's and the two share cache files instead of doubling the API
    bill (and, worse, silently measuring a different chain).
    """
    from src.backtesting.data.alpaca_provider import AlpacaDataProvider
    from src.backtesting.data.chain_builder import ChainBuilder
    from src.backtesting.data.chain_store import ChainStore
    from src.backtesting.engine.simulator import Simulator
    from src.utils.config import Config

    config = Config()
    provider = AlpacaDataProvider.from_config(config)
    # from_env(): see fc036_gap_gate_study — the lake is shared with the Job.
    builder = ChainBuilder(provider, store=ChainStore.from_env())
    sim = Simulator(config, provider, builder, [symbol], start, end)
    stock_bars = sim._load_stock_bars()
    days = sim._trading_days(stock_bars)
    bars = stock_bars.get(symbol, [])
    ceiling, floor = sim._strike_anchors(bars)
    closes = {b.bar_date: b.close for b in bars}
    return builder, days, closes, ceiling, floor, sim.max_dte


def run_prefetch(symbol: str, start: date, end: date, shard: int, shards: int) -> dict:
    """Warm the parquet chain cache for one date shard of one symbol.

    The cold pass dominates this study's wall clock and is *latency* bound, not
    rate-limit bound: one process issues its contract-discovery and bars calls
    serially, at ~3 chains/min, against a 200 req/min allowance. Sharding the
    date range across processes turns a ~5-hour serial build into well under an
    hour without going near the limit.

    Safe to shard because a cache entry is keyed per ``(symbol, as_of)`` and
    ``ChainStore.put`` writes atomically (temp file + ``os.replace``), so
    disjoint date shards never contend. The anchors are taken from the real
    `Simulator`, so these files are byte-identical to what the engine layer
    would have built and are hits for it, not near-misses.

    Run as: --shard i --shards N for i in 0..N-1.
    """
    _install_rate_limit_retry()
    builder, days, closes, ceiling, low, max_dte = _decision_context(symbol, start, end)
    mine = [d for i, d in enumerate(days) if i % shards == shard]
    t0 = datetime.now()
    built = 0
    for day in mine:
        close = closes.get(day)
        if close is None:
            continue
        builder.build(symbol, day, max_dte,
                      underlying_price=close, cost_basis=ceiling, low_anchor=low)
        built += 1
        if built % 25 == 0:
            print(f"  {symbol} shard {shard}/{shards}: {built}/{len(mine)} "
                  f"({(datetime.now()-t0).total_seconds():.0f}s)", flush=True)
    print(f"  {symbol} shard {shard}/{shards}: {built} chains in "
          f"{(datetime.now()-t0).total_seconds():.0f}s")
    return {"symbol": symbol, "shard": shard, "shards": shards, "built": built}


def run_chain(symbol: str, start: date, end: date, out: Path) -> dict:
    """Per decision day, every in-band priced put/call — then arms offline."""
    _install_rate_limit_retry()
    builder, days, closes, ceiling, low, max_dte = _decision_context(symbol, start, end)

    from src.utils.config import Config

    cfg = Config()
    put_lo, put_hi = cfg.put_delta_range
    call_lo, call_hi = cfg.call_delta_range

    per_day: List[dict] = []
    t0 = datetime.now()
    for i, day in enumerate(days):
        close = closes.get(day)
        if close is None:
            continue
        snap = builder.build(symbol, day, max_dte,
                             underlying_price=close, cost_basis=ceiling, low_anchor=low)
        if snap is None:
            per_day.append({"day": day.isoformat(), "close": None, "puts": [], "calls": []})
            continue

        def _pack(quotes, lo, hi):
            return [
                {"k": q.strike, "dte": q.dte, "mark": q.mark, "delta": q.delta}
                for q in quotes
                if q.delta is not None and lo <= abs(q.delta) <= hi and q.mark is not None
            ]

        per_day.append({
            "day": day.isoformat(),
            "close": round(close, 4),
            "puts": _pack(snap.puts, put_lo, put_hi),
            "calls": _pack(snap.calls, call_lo, call_hi),
        })
        if (i + 1) % 50 == 0:
            print(f"  {symbol} {i+1}/{len(days)} days "
                  f"({(datetime.now()-t0).total_seconds():.0f}s)")

    payload = {
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "decision_days": len(per_day),
        "put_delta_band": list(cfg.put_delta_range),
        "call_delta_band": list(cfg.call_delta_range),
        "max_dte": max_dte,
        "strike_anchors": {"ceiling": ceiling, "low": low},
        "runtime_s": round((datetime.now() - t0).total_seconds(), 1),
        "per_day": per_day,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write(out, f"chain_{symbol}.json", payload)
    print(f"  {symbol}: {len(per_day)} decision days in {payload['runtime_s']}s")
    return payload


def _score_census(census: dict, arm: Arm, *, side: str = "puts",
                  tick: Optional[float] = None) -> dict:
    """Days on which >=1 in-band contract clears ``arm``'s floor."""
    if tick is not None:
        arm = Arm(arm.key, arm.label, arm.kind, arm.put_param, tick)
    floor_fn = arm.put_floor if side == "puts" else arm.call_floor
    days = census["per_day"]
    in_band_days = 0
    usable_days = 0
    best_ratios: List[float] = []
    for row in days:
        quotes = row.get(side) or []
        if quotes:
            in_band_days += 1
        if any(q["mark"] >= floor_fn(q["k"], q["dte"]) for q in quotes):
            usable_days += 1
        # How close the richest in-band contract came to the floor. <1 means the
        # floor is what blocked the day.
        ratios = [q["mark"] / floor_fn(q["k"], q["dte"])
                  for q in quotes if floor_fn(q["k"], q["dte"]) > 0]
        if ratios:
            best_ratios.append(max(ratios))
    n = len(days)
    best_ratios.sort()
    return {
        "arm": arm.key,
        "label": arm.label,
        "side": side,
        "tick_floor": arm.tick_floor,
        "decision_days": n,
        "days_with_in_band_contract": in_band_days,
        "days_clearing_floor": usable_days,
        "usable_fraction": round(usable_days / n, 4) if n else 0.0,
        "in_band_fraction": round(in_band_days / n, 4) if n else 0.0,
        "median_best_mark_over_floor": (
            round(best_ratios[len(best_ratios) // 2], 3) if best_ratios else None
        ),
    }


# --------------------------------------------------------------------------- #
# engine — the A/B proper
# --------------------------------------------------------------------------- #

def _fresh_config(arm: Arm):
    """A brand-new Config carrying ``arm``'s *flat-equivalent* value.

    `Config.min_put_premium` is a read-only property; `config._config` is the
    only injection point (verified: src/utils/config.py:286). For arm A that is
    the whole change. For the scale-free arms this value is only the fallback
    the wrapper restores after each contract check — the per-contract floor is
    applied by `_patch_floor_shape`.
    """
    from src.utils.config import Config

    config = Config()
    config._config["strategy"]["min_put_premium"] = LIVE_MIN_PUT_PREMIUM
    config._config["strategy"]["min_call_premium"] = LIVE_MIN_CALL_PREMIUM
    assert config.min_put_premium == LIVE_MIN_PUT_PREMIUM
    assert config.min_call_premium == LIVE_MIN_CALL_PREMIUM
    return config


class _FloorShape:
    """Applies a per-contract floor by wrapping the live criteria checks.

    Deliberately a *wrapper*, not a reimplementation: it temporarily sets
    `config._config["strategy"]["min_put_premium"]` to this contract's floor and
    delegates to the original `MarketDataManager._check_put_criteria_detailed`.
    Every other rule (DTE first, then premium, then delta, then liquidity) and
    the exact rejection-reason strings are therefore the production ones. A
    hand-written copy of that method would drift from production the moment
    anyone edits it — which is the failure mode FC-032 exists to avoid.

    Production is never modified: the patch is installed for the duration of one
    `evaluate_symbol` call and removed in `__exit__`.
    """

    def __init__(self, arm: Arm) -> None:
        self.arm = arm
        self._orig_put: Optional[Callable] = None
        self._orig_call: Optional[Callable] = None
        self.applied = 0

    def __enter__(self) -> "_FloorShape":
        if self.arm.is_flat:
            return self  # status quo: nothing to patch
        from src.api.market_data import MarketDataManager

        self._orig_put = MarketDataManager._check_put_criteria_detailed
        self._orig_call = MarketDataManager._check_call_criteria_detailed
        arm = self.arm
        study = self

        def _wrapped(original, key, floor_fn):
            def check(mdm_self, option):
                floor = floor_fn(option.get("strike_price") or 0.0,
                                 option.get("dte") or 0)
                backing = mdm_self.config._config["strategy"]
                saved = backing[key]
                backing[key] = floor
                study.applied += 1
                try:
                    return original(mdm_self, option)
                finally:
                    backing[key] = saved
            return check

        MarketDataManager._check_put_criteria_detailed = _wrapped(
            self._orig_put, "min_put_premium", arm.put_floor)
        MarketDataManager._check_call_criteria_detailed = _wrapped(
            self._orig_call, "min_call_premium", arm.call_floor)
        return self

    def __exit__(self, *exc) -> None:
        if self._orig_put is None:
            return
        from src.api.market_data import MarketDataManager

        MarketDataManager._check_put_criteria_detailed = self._orig_put
        MarketDataManager._check_call_criteria_detailed = self._orig_call


class _StageTally:
    """Counts, per decision day, the stages that actually stopped this symbol.

    `SimulationResult.rejections` cannot answer this study's central question on
    its own. Its `_REASONS` map has no entry for `stock_rejected_filter`, so a
    day on which stage 1 dropped the symbol on the **price band**
    (`min_stock_price 10` / `max_stock_price 400`) is recorded as no rejection
    at all — the day simply vanishes. Measured on a 2-month F replay: 39
    decision days, 13 mapped stage-7 blocks, 0 candidates, and **26 days with no
    recorded reason whatsoever**. Attributing those matters here, because
    "the premium floor is not what blocks this symbol" is exactly the finding
    that decides demote vs re-shape.

    Also captures stage 7's own rejection *breakdown* fields, which the live
    logger already emits (`rejected_premium_too_low`,
    `rejected_delta_out_of_range`, ...) and which nothing downstream reads.

    Harness-local, read-only, and never raises into the run.
    """

    _DAY_EVENTS = {
        "stock_rejected_filter": "stage 1: price/volume band",
        "stock_filtered_by_gap_risk": "stage 2: gap-risk filter",
        "rejected_high_gap_frequency": "stage 2: gap-risk filter",
        "stage_4_blocked": "stage 4: execution gap check",
        "stage_5_blocked": "stage 5: wheel state",
        "put_blocked_by_wheel_state": "stage 5: wheel state",
        "stage_6_blocked": "stage 6: already holding",
        "stage_7_complete_not_found": "stage 7: no put met criteria",
        "stage_7_complete_found": "stage 7: candidate found",
        "stage_8_blocked": "stage 8: position sizing",
        "position_size_validation_failed": "stage 8: position sizing",
        "covered_call_drawdown_pause": "drawdown pause",
        "no_suitable_puts": "stage 7: no suitable puts",
    }

    _BREAKDOWN = ("rejected_premium_too_low", "rejected_delta_out_of_range",
                  "rejected_dte_too_high", "rejected_no_liquidity")

    def __init__(self) -> None:
        self._seen: set = set()
        self._breakdown: Dict[str, int] = {k: 0 for k in self._BREAKDOWN}
        self._breakdown_days: Dict[str, set] = {k: set() for k in self._BREAKDOWN}
        self._prev_config = None
        self.events_seen = 0
        self.frozen_events = 0
        self.errors = 0
        self.last_error = None

    def processor(self, logger, name, event_dict):
        # A diagnostic must not break the run it observes, so this swallows —
        # but silent swallowing is how this class shipped broken once already
        # (`clock.now()` against an import bound as `_clock`: NameError on every
        # event, every tally empty, and nothing said so). `errors` and
        # `events_seen` are reported next to the counts so an inert tally is
        # visible in the output instead of reading as "nothing happened".
        try:
            from src.utils import clock as _clock

            self.events_seen += 1
            if not _clock.is_frozen():
                return event_dict
            self.frozen_events += 1
            day = _clock.now().date()
            et = event_dict.get("event_type", "")
            bucket = self._DAY_EVENTS.get(et)
            if bucket:
                self._seen.add((day, bucket))
            if et in ("stage_7_complete_found", "stage_7_complete_not_found"):
                for k in self._BREAKDOWN:
                    v = event_dict.get(k)
                    if isinstance(v, int) and v > 0:
                        self._breakdown[k] += v
                        self._breakdown_days[k].add(day)
        except Exception as exc:  # noqa: BLE001 - must never break a run
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
        return event_dict

    def __enter__(self) -> "_StageTally":
        import structlog

        try:
            self._prev_config = structlog.get_config()
            structlog.configure(
                processors=[self.processor] + list(self._prev_config.get("processors", [])))
        except Exception:  # noqa: BLE001
            self._prev_config = None
        return self

    def __exit__(self, *exc) -> None:
        import structlog

        if self._prev_config is not None:
            try:
                structlog.configure(**self._prev_config)
            except Exception:  # noqa: BLE001
                pass

    def summary(self) -> dict:
        from collections import Counter

        per = Counter(bucket for _d, bucket in self._seen)
        return {
            "days_by_stage": dict(per.most_common()),
            "stage7_rejected_contracts": dict(self._breakdown),
            "stage7_rejected_days": {k: len(v) for k, v in self._breakdown_days.items()},
            # Health of the tally itself. errors > 0 or events_seen == 0 means
            # the numbers above are not evidence of anything.
            "events_seen": self.events_seen,
            "frozen_events": self.frozen_events,
            "errors": self.errors,
            "last_error": self.last_error,
        }


def _arm_row(symbol: str, arm: Arm, report, runtime_s: float, patched: int) -> dict:
    dq = report.data_quality or {}
    rejections = dq.get("blocked_days_by_reason", {}) or {}
    closed = report.closed_cycles
    worst = report.worst_cycle
    return {
        "symbol": symbol,
        "arm": arm.key,
        "arm_label": arm.label,
        "runtime_s": round(runtime_s, 1),
        "contracts_floor_checked": patched,
        "verdict": report.verdict(),
        "verdict_reasons": report.verdict_reasons(),
        "decision_days": dq.get("decision_days"),
        "candidate_days": dq.get("days_with_a_qualifying_candidate"),
        "puts_sold": report.puts_sold,
        "calls_sold": report.calls_sold,
        "cycles": len(report.cycles),
        "closed_cycles": len(closed),
        "assignments": report.assignments,
        "rolls": report.rolls,
        "total_return_pct": round(report.total_return * 100, 4),
        "annualized_return_pct": round(report.annualized_return * 100, 3),
        "return_on_collateral_pct": (
            None if report.return_on_collateral is None
            else round(report.return_on_collateral * 100, 3)),
        "ann_return_on_collateral_pct": (
            None if report.annualized_return_on_collateral is None
            else round(report.annualized_return_on_collateral * 100, 3)),
        "max_drawdown_pct": round(report.max_drawdown * 100, 3),
        "days_in_position": report.days_in_position,
        "days_in_position_fraction": round(report.days_in_position_fraction, 4),
        "win_rate": (None if report.win_rate is None else round(report.win_rate, 4)),
        "assignment_rate": (None if report.assignment_rate is None
                            else round(report.assignment_rate, 4)),
        "avg_collateral": round(report.avg_collateral, 2),
        "peak_collateral": round(report.peak_collateral, 2),
        "option_pnl": round(report.option_pnl, 2),
        "stock_pnl": round(report.stock_pnl, 2),
        "unrealized_stock_pnl": round(report.unrealized_stock_pnl, 2),
        "dividends": round(report.dividends, 2),
        "fees": round(report.fees, 2),
        "total_pnl": round(report.total_pnl, 2),
        "reconciliation_gap": round(report.reconciliation_gap, 6),
        "worst_cycle_pnl": (None if worst is None else round(worst.total_pnl, 2)),
        "benchmark_total_return_pct": (
            None if report.benchmark is None
            else round(report.benchmark.total_return * 100, 3)),
        "dividend_coverage": dq.get("dividend_coverage"),
        "stage7_blocked_days": rejections.get(STAGE7_REJECTION_KEY, 0),
        "rejections": rejections,
    }


def run_engine(symbol: str, start: date, end: date, out: Path,
               arms: Sequence[Arm] = ARMS, starting_cash: float = 100_000.0,
               sensitivity: bool = False) -> dict:
    from src.backtesting.engine.rejections import _REASONS
    from src.backtesting.evaluate import evaluate_symbol

    # The rejection dict is keyed by DESCRIPTION, not event name. Assert the key
    # we read is one the tally can actually emit rather than silently reporting
    # zeros forever if someone renames a bucket.
    assert STAGE7_REJECTION_KEY in set(_REASONS.values()), (
        f"{STAGE7_REJECTION_KEY!r} is not a rejection description; "
        f"known: {sorted(set(_REASONS.values()))}")

    _install_rate_limit_retry()
    rows = {}
    for arm in arms:
        config = _fresh_config(arm)
        t0 = datetime.now()
        with _FloorShape(arm) as shape, _StageTally() as stages:
            report, sens = evaluate_symbol(
                symbol, start, end,
                config=config,
                starting_cash=starting_cash,
                # The bid-fill sensitivity pass replays the identical window a
                # second time under a different fill assumption. Different
                # question, double the runtime; not this study's contract.
                # Off by default: the bid-fill pass replays the identical
                # window a second time and doubles runtime. Turned ON via
                # --sensitivity for the arms whose conclusion depends on it —
                # the cohort's enabled trades sit at a 3-8c mid, where the
                # modelled spread is a large fraction of the premium and a
                # mid-minus-haircut fill is the least trustworthy assumption in
                # the study.
                run_sensitivity=sensitivity,
            )
        row = _arm_row(symbol, arm, report,
                       (datetime.now() - t0).total_seconds(), shape.applied)
        row["stage_tally"] = stages.summary()
        row["fill_sensitivity"] = sens
        rows[arm.key] = row
        print(f"  {symbol} arm {arm.key} ({arm.label}): "
              f"puts {row['puts_sold']} calls {row['calls_sold']} | "
              f"dip {row['days_in_position_fraction']:.2f} | "
              f"roc {row['return_on_collateral_pct']}% | "
              f"annRoC {row['ann_return_on_collateral_pct']}% | "
              f"dd {row['max_drawdown_pct']}% | {row['verdict']} | "
              f"{row['runtime_s']}s")

    payload = {"symbol": symbol, "arms": rows,
               "generated_at": datetime.now().isoformat(timespec="seconds")}
    _write(out, f"engine_{symbol}.json", payload)
    return payload


# --------------------------------------------------------------------------- #
# fills — the real-fills layer (READ ONLY against production BigQuery)
# --------------------------------------------------------------------------- #

# Schema verified against the live table by this harness's own `fills` run:
#   * sell-to-open is side='sell_short'; buy-to-close is side='buy'
#   * the underlying column is `underlying`
#   * `premium_total` (= price*qty*100) is the populated money column on FILL
#     rows; `net_amount` is NULL there
#   * FILL rows have activity_date NULL — only transaction_time (UTC) is set
#   * assignment is OPASN (option) + OPTRD (the resulting stock leg)
#   * **`dte_at_event` IS UNRELIABLE — do not use it.** It is 0 on 232 of the
#     330 sell-to-open fills (70%), while those same rows' true DTE
#     (`expiration - fill date`) averages 5.29 and ranges 2..8. Measured:
#       SELECT dte_at_event, COUNT(*),
#              AVG(DATE_DIFF(expiration, DATE(transaction_time,'America/New_York'), DAY))
#       FROM options_wheel.trades_from_activities
#       WHERE activity_type='FILL' AND side='sell_short' GROUP BY 1
#     An annualized-RoC floor scales with DTE, so reading the column instead of
#     computing it made arm C's floor $0.00 on 70% of fills and produced a
#     spurious "arm C blocks nothing" result. `dte_true` below is the computed
#     value and the only one this harness uses.

_FILLS_SQL = """
SELECT
  symbol                                              AS contract,
  underlying,
  CAST(DATE(transaction_time, 'America/New_York') AS STRING) AS et_date,
  side,
  qty,
  price,
  premium_total,
  strike_price,
  CAST(expiration AS STRING)                          AS expiration,
  option_type,
  dte_at_event,
  DATE_DIFF(expiration, DATE(transaction_time, 'America/New_York'), DAY) AS dte_true
FROM `options_wheel.trades_from_activities`
WHERE activity_type = 'FILL'
ORDER BY transaction_time
"""

_UNIVERSE_SQL = """
SELECT underlying,
       COUNTIF(option_type = 'put')  AS put_fills,
       COUNTIF(option_type = 'call') AS call_fills,
       COUNT(*)                      AS fills,
       ROUND(SUM(premium_total), 2)  AS premium,
       ROUND(MIN(price), 2)          AS min_price,
       CAST(MIN(DATE(transaction_time,'America/New_York')) AS STRING) AS first_d,
       CAST(MAX(DATE(transaction_time,'America/New_York')) AS STRING) AS last_d
FROM `options_wheel.trades_from_activities`
WHERE activity_type = 'FILL' AND side = 'sell_short'
GROUP BY underlying
ORDER BY fills DESC
"""


def _bq(sql: str) -> List[dict]:
    """Run a query through the authenticated `bq` CLI. READ ONLY.

    The CLI rather than google-cloud-bigquery because the credential present
    here is a user login (`gcloud auth login`), not application-default
    credentials. This study never writes to the production dataset.
    """
    import subprocess

    proc = subprocess.run(
        ["bq", "query", "--use_legacy_sql=false", "--format=json",
         "--max_rows=100000", sql],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"bq failed: {proc.stderr.strip()[:2000]}")
    text = proc.stdout.strip()
    start = text.find("[")
    return json.loads(text[start:]) if start != -1 else []


def run_fills(out: Path, arms: Sequence[Arm] = ARMS) -> dict:
    """What each arm would have done to the trades production actually took.

    Two questions, kept separate:

    1. **Ground truth for "structurally untradeable".** Which underlyings appear
       in the real sell-to-open fills at all? The excluded cohort's absence here
       is the production-side confirmation of the coverage gate. Absence is
       weaker evidence than presence, so it is reported alongside the fact that
       the four names were in `stocks.symbols` and scanned the whole time.

    2. **What a re-shape would have destroyed.** Each arm's floor is evaluated
       against every real sell-to-open fill using that fill's own strike and
       DTE. A fill whose actual price is BELOW the arm's floor would not have
       been taken. Its realized option P&L (premium in minus buy-to-close paid
       on the same contract, attributed pro-rata by premium across that
       contract's sell legs) is the dollars the re-shape costs. This is the
       FC-036 lesson: engine-only A/B called a gate free that real fills priced
       at $1,900.
    """
    rows = _bq(_FILLS_SQL)
    universe = _bq(_UNIVERSE_SQL)

    for r in rows:
        for k in ("qty", "price", "premium_total", "strike_price",
                  "dte_at_event", "dte_true"):
            r[k] = None if r.get(k) is None else float(r[k])

    opens = [r for r in rows if r["side"] == "sell_short"]
    closes = [r for r in rows if r["side"] == "buy"]

    # Realized option P&L per contract: premium collected minus premium paid.
    prem_in: Dict[str, float] = {}
    prem_out: Dict[str, float] = {}
    for r in opens:
        prem_in[r["contract"]] = prem_in.get(r["contract"], 0.0) + (r["premium_total"] or 0.0)
    for r in closes:
        prem_out[r["contract"]] = prem_out.get(r["contract"], 0.0) + (r["premium_total"] or 0.0)

    total_premium = sum(prem_in.values())

    per_arm = {}
    for arm in arms:
        blocked, kept = [], []
        for r in opens:
            strike = r["strike_price"] or 0.0
            dte = int(r["dte_true"] or 0)
            floor = (arm.put_floor(strike, dte) if r["option_type"] == "put"
                     else arm.call_floor(strike, dte))
            share = ((r["premium_total"] or 0.0) / prem_in[r["contract"]]
                     if prem_in.get(r["contract"]) else 0.0)
            realized = share * (prem_in.get(r["contract"], 0.0)
                                - prem_out.get(r["contract"], 0.0))
            rec = {**r, "arm_floor": round(floor, 4),
                   "cleared": r["price"] >= floor,
                   "realized_option_pnl": round(realized, 2)}
            (kept if rec["cleared"] else blocked).append(rec)

        by_und: Dict[str, dict] = {}
        for rec in blocked:
            b = by_und.setdefault(rec["underlying"], {"fills": 0, "premium": 0.0, "pnl": 0.0})
            b["fills"] += 1
            b["premium"] += rec["premium_total"] or 0.0
            b["pnl"] += rec["realized_option_pnl"]

        per_arm[arm.key] = {
            "arm": arm.key,
            "label": arm.label,
            "open_fills": len(opens),
            "blocked_fills": len(blocked),
            "blocked_pct": round(100.0 * len(blocked) / len(opens), 2) if opens else 0.0,
            "blocked_premium": round(sum(r["premium_total"] or 0.0 for r in blocked), 2),
            "blocked_realized_option_pnl": round(
                sum(r["realized_option_pnl"] for r in blocked), 2),
            "kept_premium": round(sum(r["premium_total"] or 0.0 for r in kept), 2),
            "blocked_by_underlying": {
                k: {"fills": v["fills"], "premium": round(v["premium"], 2),
                    "realized_option_pnl": round(v["pnl"], 2)}
                for k, v in sorted(by_und.items(), key=lambda kv: -kv[1]["fills"])},
            # How much headroom the *tightest* surviving real fill had. An arm
            # that blocks 0 fills but clears the closest one by a cent is one
            # bad week away from blocking plenty; reporting only "0 blocked"
            # would hide that.
            "tightest_surviving_margin": (None if not kept else min(
                ({"contract": r["contract"], "date": r["et_date"],
                  "price": r["price"], "strike": r["strike_price"],
                  "dte": r["dte_true"], "floor": r["arm_floor"],
                  "margin": round(r["price"] - r["arm_floor"], 4)}
                 for r in kept), key=lambda d: d["margin"])),
            "blocked_examples": sorted(
                ({"contract": r["contract"], "date": r["et_date"], "price": r["price"],
                  "strike": r["strike_price"], "dte": r["dte_true"],
                  "floor": r["arm_floor"], "pnl": r["realized_option_pnl"]}
                 for r in blocked), key=lambda r: r["pnl"])[:10],
        }
        print(f"  arm {arm.key} ({arm.label}): blocks "
              f"{per_arm[arm.key]['blocked_fills']}/{len(opens)} real fills "
              f"(${per_arm[arm.key]['blocked_premium']:,.0f} premium, "
              f"${per_arm[arm.key]['blocked_realized_option_pnl']:,.0f} realized P&L)")

    payload = {
        "traded_underlyings": universe,
        "excluded_cohort": list(EXCLUDED_COHORT),
        "cohort_present_in_fills": [
            u["underlying"] for u in universe if u["underlying"] in EXCLUDED_COHORT],
        "open_fills": len(opens),
        "close_fills": len(closes),
        "total_premium_collected": round(total_premium, 2),
        "min_open_fill_price": min((r["price"] for r in opens), default=None),
        "per_arm": per_arm,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write(out, "fills.json", payload)
    return payload


# --------------------------------------------------------------------------- #
# report — assemble, and apply the pre-registered rules
# --------------------------------------------------------------------------- #

def _fmt(v, spec="{:.2f}", dash="—"):
    return dash if v is None else spec.format(v)


def run_report(out: Path, symbols: Sequence[str] = STUDY_SYMBOLS) -> dict:
    engine = {}
    for s in symbols:
        path = out / f"engine_{s}.json"
        if path.exists():
            engine[s] = json.loads(path.read_text())["arms"]
    fills = json.loads((out / "fills.json").read_text()) if (out / "fills.json").exists() else None

    census = {}
    for s in symbols:
        path = out / f"chain_{s}.json"
        if path.exists():
            c = json.loads(path.read_text())
            census[s] = {
                "puts": [_score_census(c, a, side="puts") for a in SWEEP],
                "calls": [_score_census(c, a, side="calls") for a in SWEEP],
                "puts_no_tick": [_score_census(c, a, side="puts", tick=0.0) for a in SWEEP],
                "decision_days": c["decision_days"],
            }

    lines: List[str] = []
    lines.append("PER-ARM / PER-SYMBOL (engine)\n")
    hdr = (f"{'sym':<6}{'arm':<4}{'puts':>5}{'calls':>6}{'cyc':>5}{'closed':>7}"
           f"{'dip':>7}{'RoC%':>9}{'annRoC%':>9}{'tot%':>8}{'maxDD%':>8}"
           f"{'win':>6}{'assn':>6}  verdict")
    lines.append(hdr)
    for s in symbols:
        for arm in ARMS:
            r = engine.get(s, {}).get(arm.key)
            if r is None:
                continue
            lines.append(
                f"{s:<6}{arm.key:<4}{r['puts_sold']:>5}{r['calls_sold']:>6}"
                f"{r['cycles']:>5}{r['closed_cycles']:>7}"
                f"{r['days_in_position_fraction']:>7.2f}"
                f"{_fmt(r['return_on_collateral_pct'], '{:.2f}'):>9}"
                f"{_fmt(r['ann_return_on_collateral_pct'], '{:.2f}'):>9}"
                f"{r['total_return_pct']:>8.2f}{r['max_drawdown_pct']:>8.2f}"
                f"{_fmt(r['win_rate'], '{:.2f}'):>6}{_fmt(r['assignment_rate'], '{:.2f}'):>6}"
                f"  {r['verdict']}")
    verdict = _apply_rules(engine, fills)
    lines.append("")
    lines.append("PRE-REGISTERED RULES")
    for k, v in verdict["rules"].items():
        lines.append(f"  {k}: {'PASS' if v['pass'] else 'FAIL'} — {v['detail']}")
    lines.append(f"  DECISION: {verdict['decision']}")
    text = "\n".join(lines)
    print(text)

    payload = {"engine": engine, "fills": fills, "census": census,
               "rules": PREREGISTERED_RULES, "verdict": verdict, "table": text,
               "generated_at": datetime.now().isoformat(timespec="seconds")}
    _write(out, "report.json", payload)
    (out / "report.txt").write_text(text)
    return payload


def _apply_rules(engine: dict, fills: Optional[dict]) -> dict:
    """Evaluate PREREGISTERED_RULES mechanically against the collected data."""
    rules: Dict[str, dict] = {}
    reshape_arms = [a for a in ARMS if not a.is_flat]

    # R1
    r1_by_arm = {}
    for arm in reshape_arms:
        enabled = [s for s in EXCLUDED_COHORT
                   if (engine.get(s, {}).get(arm.key) or {}).get(
                       "days_in_position_fraction", 0) >= 0.25]
        r1_by_arm[arm.key] = enabled
    r1_pass = any(len(v) >= 2 for v in r1_by_arm.values())
    rules["R1_enablement"] = {
        "pass": r1_pass,
        "detail": "; ".join(f"arm {k}: {len(v)}/4 names >=0.25 days-in-position "
                            f"({', '.join(v) or 'none'})" for k, v in r1_by_arm.items()),
    }

    # R2 — only over arms that passed R1
    r2_detail, r2_pass = [], False
    for arm in reshape_arms:
        names = r1_by_arm[arm.key]
        if len(names) < 2:
            continue
        ok = []
        for s in names:
            r = engine[s][arm.key]
            roc = r.get("ann_return_on_collateral_pct")
            good = (roc is not None and roc > 4.0
                    and r["verdict"] in ("fit", "marginal")
                    and r["closed_cycles"] >= 10)
            ok.append((s, roc, r["verdict"], r["closed_cycles"], good))
        if ok and all(o[-1] for o in ok):
            r2_pass = True
        r2_detail.append(f"arm {arm.key}: " + ", ".join(
            f"{s} annRoC={_fmt(roc)}% verdict={v} closed={c} {'ok' if g else 'FAIL'}"
            for s, roc, v, c, g in ok))
    rules["R2_worth_doing"] = {
        "pass": r2_pass,
        "detail": "; ".join(r2_detail) or "not evaluated (R1 failed for every arm)",
    }

    # R3 — control non-regression, evaluated for every re-shape arm regardless
    r3_detail, r3_ok_arms = [], []
    for arm in reshape_arms:
        breaches = []
        for s in CONTROLS:
            base = engine.get(s, {}).get("A")
            test = engine.get(s, {}).get(arm.key)
            if base is None or test is None:
                breaches.append(f"{s}: missing run")
                continue
            b_roc = base.get("ann_return_on_collateral_pct")
            t_roc = test.get("ann_return_on_collateral_pct")
            if b_roc not in (None, 0) and t_roc is not None:
                rel = (t_roc - b_roc) / abs(b_roc)
                if rel < -0.10:
                    breaches.append(f"{s}: annRoC {b_roc:.2f}%→{t_roc:.2f}% ({rel*100:.0f}%)")
            if test["max_drawdown_pct"] - base["max_drawdown_pct"] < -2.0:
                breaches.append(f"{s}: maxDD {base['max_drawdown_pct']:.2f}%→"
                                f"{test['max_drawdown_pct']:.2f}%")
            if base["puts_sold"] and test["puts_sold"] < 0.90 * base["puts_sold"]:
                breaches.append(f"{s}: puts {base['puts_sold']}→{test['puts_sold']}")
        if not breaches:
            r3_ok_arms.append(arm.key)
        r3_detail.append(f"arm {arm.key}: {'clean' if not breaches else '; '.join(breaches)}")
    rules["R3_control_non_regression"] = {
        "pass": bool(r3_ok_arms),
        "detail": "; ".join(r3_detail),
    }

    # R4 — real fills
    r4_detail, r4_ok_arms = [], []
    if fills:
        budget = 0.10 * fills["total_premium_collected"]
        for arm in reshape_arms:
            a = fills["per_arm"][arm.key]
            # Positive = the blocked fills were net profitable, i.e. the arm
            # would have destroyed value. Negative = it would have avoided a
            # loser, which is not a breach.
            cost = a["blocked_realized_option_pnl"]
            ok = cost <= budget
            if ok:
                r4_ok_arms.append(arm.key)
            r4_detail.append(f"arm {arm.key}: blocks {a['blocked_fills']} real fills, "
                             f"${cost:,.0f} realized option P&L vs ${budget:,.0f} budget "
                             f"{'ok' if ok else 'FAIL'}")
    rules["R4_real_fills_non_regression"] = {
        "pass": bool(r4_ok_arms),
        "detail": "; ".join(r4_detail) or "fills layer not run",
    }

    if r1_pass and rules["R2_worth_doing"]["pass"] and r3_ok_arms and r4_ok_arms:
        decision = "RE-SHAPE"
    elif r1_pass and rules["R2_worth_doing"]["pass"]:
        decision = "HYBRID (per-symbol override)"
    else:
        decision = "DEMOTE"
    return {"rules": rules, "decision": decision, "r1_by_arm": r1_by_arm}


# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("layer", choices=("prefetch", "chain", "engine", "fills", "report"))
    p.add_argument("--symbol", help="single symbol (chain/engine)")
    p.add_argument("--symbols", help="comma-separated (report)")
    p.add_argument("--start", default=WINDOW_START.isoformat())
    p.add_argument("--end", default=WINDOW_END.isoformat())
    p.add_argument("--out", default=None)
    p.add_argument("--shard", type=int, default=0, help="prefetch: this shard index")
    p.add_argument("--shards", type=int, default=1, help="prefetch: total shards")
    p.add_argument("--sensitivity", action="store_true",
                   help="engine: also replay each arm at the bid")
    args = p.parse_args()

    out = Path(args.out) if args.out else _default_out()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    if args.layer == "prefetch":
        if not args.symbol:
            p.error("prefetch needs --symbol")
        run_prefetch(args.symbol, start, end, args.shard, args.shards)
    elif args.layer in ("chain", "engine"):
        if not args.symbol:
            p.error(f"{args.layer} needs --symbol")
        if args.layer == "chain":
            run_chain(args.symbol, start, end, out)
        else:
            run_engine(args.symbol, start, end, out, sensitivity=args.sensitivity)
    elif args.layer == "fills":
        run_fills(out)
    else:
        syms = tuple(args.symbols.split(",")) if args.symbols else STUDY_SYMBOLS
        run_report(out, syms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
