#!/usr/bin/env python3
"""FC-036 Half 1 — what a *working* stage-4 execution-gap gate would have done.

⚠️ **CODE DELETED BY FC-069 item 5 (2026-08-04).** `src/risk/gap_detector.py`
and the `risk.gap_risk_controls` knobs this harness monkeypatches are no longer
in the tree; **the code lives at pre-S1 main SHA `afb6698`**. Phase B was never
armed and gap risk is now absent by decision (FC-049 owns any evidence-based
revival), so this file is kept as the record of the study, not as a runnable
harness — check out `afb6698` to re-run it.

⚠️ **FC-060 Layer 2 (2026-08-28).** Config-arm studies of this shape now have a
supported home: ``python main.py --command sweep`` replays many selection-only
config variants over many symbols, materialising each window once
(``src/backtesting/scenarios/``, ``docs/BACKTEST_ENGINE.md`` §"Scenario
sweeps"). It will NOT bring this harness back — the gap knobs it needs no longer
exist as config, and the sweep's allowlist refuses any key the replay does not
read. This note exists so nobody rebuilds a bespoke arm-runner from here. The
file stays for its SHA pointers, which FC-049 relies on.


PURPOSE
-------
`GapDetector._get_previous_close` read the current session's own daily bar, so the
stage-4 gate measured ~20 minutes of intraday drift instead of the overnight gap.
It did not sit inert: the reconstruction below shows it fired on that drift on a
double-digit percentage of NVDA/AMD sessions while missing most real gap days.
FC-036 Phase A fixes the math but ships the gate disarmed
(`execution_gap_threshold: 999`). This harness produces the evidence required
before Phase B arms it:

  layer1    Daily-bar measurement, no engine. Per symbol-day: true overnight gap
            (open_D vs close_D-1) and close-to-close move. Block rates at
            {1.5, 2.5, 3.5, 5.0}, distribution tails, and the misclassification
            matrix between the two measures (the replay measures close-to-close;
            a live 9:35 gate measures the overnight gap).

  intraday   Reconstructs, from 5-minute bars, BOTH what the broken gate actually
            measured (~09:15 partial-daily-bar close vs the ~09:35 quote) and what
            the fixed gate measures at each of the three live scans (09:35 / 12:00
            / 15:00 vs the true prior close). Yields the broken-vs-fixed
            misclassification matrix and the "all three scans block" day-loss bound.
            NOTE: the plan asserted this was not historically reconstructible. It is
            — Alpaca serves historical intraday bars including pre-market.

  layer2     Engine A/B. Drives the real backtest engine (fix-branch code) once per
            threshold arm, injecting the threshold into a fresh Config per arm.
            Reports return, return-on-collateral, max drawdown, puts/calls sold,
            assignments and the stage-4 blocked-day count per arm.

  attribute  Post-hoc join of the baseline (999) arm's option-open ledger events
            against the layer1 gap table: premium the gate would have prevented,
            and how those cycles actually turned out in the world where they happened.

  layer3     The decisive layer. Joins the gap table against the REAL fills in
            BigQuery `options_wheel.trades_from_activities` (read-only) and reports
            the dollars of actually-taken trades a working gate would have
            prevented, and whether they won or lost. Needs `gcloud auth login`;
            if auth is missing it prints the query rather than approximating.

HOW TO RE-RUN
-------------
    # from the repo root, with .env holding ALPACA_API_KEY / ALPACA_SECRET_KEY
    OUT=/tmp/fc036
    python tools/diagnostics/fc036_gap_gate_study.py layer1   --out $OUT
    python tools/diagnostics/fc036_gap_gate_study.py intraday --out $OUT
    python tools/diagnostics/fc036_gap_gate_study.py layer2   --symbol NVDA --out $OUT
    python tools/diagnostics/fc036_gap_gate_study.py attribute --out $OUT
    python tools/diagnostics/fc036_gap_gate_study.py summary  --out $OUT

`layer2` is the expensive one: ~8-10 min per symbol on a cold parquet chain cache
(`cache/backtest/chains/`, gitignored), then seconds per additional arm. Run one
symbol per process; arms within a symbol share the warm cache.

Companion write-up: docs/investigations/fc-036-gap-gate-study.md
Plan: docs/plans/fc-036.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# --------------------------------------------------------------------------- #
# Study parameters (pre-registered by docs/plans/fc-036.md)
# --------------------------------------------------------------------------- #

WINDOW_START = date(2024, 2, 1)   # Alpaca options-history floor
WINDOW_END = date(2026, 7, 24)

THRESHOLDS = (1.5, 2.5, 3.5, 5.0)
BASELINE_THRESHOLD = 999.0        # Phase A shadow == the broken gate's behaviour in replay
ARMS = (BASELINE_THRESHOLD,) + THRESHOLDS

# Layer 1 runs the whole configured universe (daily bars are near-free).
# Layer 2 runs the six symbols the study brief requires; they cover the plan's
# Track-B1 set (NVDA, AMD, GOOGL, IWM) so FC-002's study can difference against this.
LAYER2_SYMBOLS = ("NVDA", "AMD", "AAPL", "GOOGL", "SPY", "IWM")

# Windows that would span an unmodelled corporate action. The engine refuses those
# outright (UnadjustedCorporateAction). NVDA split 10:1 on 2024-06-10; a start of
# 2024-07-01 (the plan's suggestion) still drags the split into the simulator's
# 60-calendar-day warm-up buffer, where it survives but poisons GapDetector's
# 30-day stage-2 statistics for the first weeks. 2024-08-15 - 60d = 2024-06-16,
# clear of it. See "what the plan got wrong" in the write-up.
SYMBOL_START_OVERRIDES: Dict[str, date] = {"NVDA": date(2024, 8, 15)}

STAGE4_REJECTION_KEY = "execution gap check (stage 4)"

# Live scan cadence (ET). ~09:35 / 12:00 / 15:00 per docs/investigations/fc-032-parity-check.md.
SCAN_TIMES_ET = ((9, 35), (12, 0), (15, 0))
# The broken gate's "previous close" was the current session's PARTIAL daily bar,
# whose close is the last print as of `now - 20min` (src/api/alpaca_client.py:391).
BROKEN_LOOKBACK_MINUTES = 20


def _default_out() -> Path:
    return Path(os.environ.get("FC036_OUT", "/tmp/fc036"))


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


# --------------------------------------------------------------------------- #
# Layer 1 — daily-bar measurement
# --------------------------------------------------------------------------- #

def _pct(n: int, d: int) -> float:
    return 0.0 if d == 0 else round(100.0 * n / d, 2)


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def _matrix(rows: Sequence[dict], a_key: str, b_key: str, threshold: float) -> dict:
    """2x2 confusion between two block signals at one threshold.

    ``a`` is the reference measure, ``b`` the one being judged against it.
    """
    both = a_only = b_only = neither = 0
    for r in rows:
        a = r.get(a_key)
        b = r.get(b_key)
        if a is None or b is None:
            continue
        ab = abs(a) > threshold
        bb = abs(b) > threshold
        if ab and bb:
            both += 1
        elif ab:
            a_only += 1
        elif bb:
            b_only += 1
        else:
            neither += 1
    total = both + a_only + b_only + neither
    agree = both + neither
    return {
        "threshold": threshold,
        "n": total,
        "both_block": both,
        f"{a_key}_only": a_only,
        f"{b_key}_only": b_only,
        "neither": neither,
        "agreement_pct": _pct(agree, total),
        "disagreement_pct": _pct(a_only + b_only, total),
        # Of the days the reference measure would block, how many does the other miss?
        "reference_blocks": both + a_only,
        "missed_by_other_pct": _pct(a_only, both + a_only),
        "other_blocks": both + b_only,
        "false_blocks_by_other_pct": _pct(b_only, both + b_only),
    }


def run_layer1(symbols: Sequence[str], start: date, end: date, out: Path) -> dict:
    from src.utils.config import Config
    from src.backtesting.data.alpaca_provider import (
        SPLIT_RATIO_HIGH,
        SPLIT_RATIO_LOW,
        AlpacaDataProvider,
    )

    config = Config()
    provider = AlpacaDataProvider.from_config(config)

    per_symbol: Dict[str, dict] = {}
    all_rows: List[dict] = []

    for symbol in symbols:
        bars = provider.get_stock_bars(symbol, start, end)
        if len(bars) < 2:
            print(f"  {symbol}: {len(bars)} bars — skipped")
            continue
        rows: List[dict] = []
        excluded: List[dict] = []
        for prev, curr in zip(bars, bars[1:]):
            if prev.close <= 0:
                continue
            ratio = curr.close / prev.close
            if ratio < SPLIT_RATIO_LOW or ratio > SPLIT_RATIO_HIGH:
                # Corporate action: the raw-bar "gap" is a unit change, not a move.
                excluded.append({"date": curr.bar_date.isoformat(), "ratio": round(ratio, 4)})
                continue
            rows.append({
                "symbol": symbol,
                "date": curr.bar_date.isoformat(),
                "prev_close": prev.close,
                "open": curr.open,
                "close": curr.close,
                "overnight_gap_pct": round((curr.open - prev.close) / prev.close * 100, 4),
                "close_to_close_pct": round((curr.close - prev.close) / prev.close * 100, 4),
            })

        gaps = sorted(abs(r["overnight_gap_pct"]) for r in rows)
        c2cs = sorted(abs(r["close_to_close_pct"]) for r in rows)
        n = len(rows)
        per_symbol[symbol] = {
            "n_days": n,
            "first_day": rows[0]["date"] if rows else None,
            "last_day": rows[-1]["date"] if rows else None,
            "excluded_corporate_actions": excluded,
            "overnight": {
                "mean_abs": round(mean(gaps), 3) if gaps else 0.0,
                "median_abs": round(_quantile(gaps, 0.50), 3),
                "p90_abs": round(_quantile(gaps, 0.90), 3),
                "p99_abs": round(_quantile(gaps, 0.99), 3),
                "max_abs": round(gaps[-1], 3) if gaps else 0.0,
                "block_rate_pct": {
                    str(t): _pct(sum(1 for g in gaps if g > t), n) for t in THRESHOLDS
                },
                "block_days": {
                    str(t): sum(1 for g in gaps if g > t) for t in THRESHOLDS
                },
            },
            "close_to_close": {
                "mean_abs": round(mean(c2cs), 3) if c2cs else 0.0,
                "median_abs": round(_quantile(c2cs, 0.50), 3),
                "p90_abs": round(_quantile(c2cs, 0.90), 3),
                "p99_abs": round(_quantile(c2cs, 0.99), 3),
                "max_abs": round(c2cs[-1], 3) if c2cs else 0.0,
                "block_rate_pct": {
                    str(t): _pct(sum(1 for g in c2cs if g > t), n) for t in THRESHOLDS
                },
                "block_days": {
                    str(t): sum(1 for g in c2cs if g > t) for t in THRESHOLDS
                },
            },
            # Reference = overnight (the live 9:35 measure); other = close-to-close
            # (what the engine replay measures at its single 16:00 decision).
            "misclassification_overnight_vs_c2c": {
                str(t): _matrix(rows, "overnight_gap_pct", "close_to_close_pct", t)
                for t in THRESHOLDS
            },
        }
        all_rows.extend(rows)
        print(f"  {symbol}: {n} days, "
              f"overnight>1.5%: {per_symbol[symbol]['overnight']['block_rate_pct']['1.5']}%, "
              f"c2c>1.5%: {per_symbol[symbol]['close_to_close']['block_rate_pct']['1.5']}%")

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "thresholds": list(THRESHOLDS),
        "per_symbol": per_symbol,
    }
    _write(out, "layer1.json", payload)
    _write(out, "layer1_rows.json", all_rows)
    return payload


def run_fc_entry_check(out: Path) -> dict:
    """Recompute the five NVDA days the FC-036 entry cited as '3 of 5'.

    The plan already corrects this to 2 of 5 from the entry's own numbers; this
    recomputes them from bars so the correction rests on data, not arithmetic on
    a claim.
    """
    from src.utils.config import Config
    from src.backtesting.data.alpaca_provider import AlpacaDataProvider

    provider = AlpacaDataProvider.from_config(Config())
    bars = provider.get_stock_bars("NVDA", date(2026, 7, 10), date(2026, 7, 24))
    rows = []
    for prev, curr in zip(bars, bars[1:]):
        rows.append({
            "date": curr.bar_date.isoformat(),
            "prev_close": prev.close,
            "open": curr.open,
            "overnight_gap_pct": round((curr.open - prev.close) / prev.close * 100, 3),
            "blocks_at_1.5": abs((curr.open - prev.close) / prev.close * 100) > 1.5,
        })
    # The FC entry's five sampled days are the sessions 2026-07-13..2026-07-17
    # (its listed gaps -2.295, -1.096, +0.076, +2.295, -1.147 match these exactly,
    # listed in reverse chronological order).
    cited = [r for r in rows if "2026-07-13" <= r["date"] <= "2026-07-17"]
    payload = {
        "nvda_recent_days": rows,
        "n_blocking_at_1.5": sum(1 for r in rows if r["blocks_at_1.5"]),
        "fc_entry_five_days": cited,
        "fc_entry_claim": "3 of 5",
        "recomputed_from_bars": f"{sum(1 for r in cited if r['blocks_at_1.5'])} of {len(cited)}",
    }
    _write(out, "fc_entry_check.json", payload)
    return payload


# --------------------------------------------------------------------------- #
# Intraday — what the BROKEN gate actually measured, reconstructed
# --------------------------------------------------------------------------- #

_ET_OFFSET_CACHE: Dict[date, int] = {}


def _et_offset_hours(d: date) -> int:
    """UTC offset of US/Eastern on ``d``, as a positive hour count (4 EDT, 5 EST).

    Evaluated at local noon so a DST-transition instant can never land in the
    ambiguous/nonexistent hour. Transitions fall on Sundays, which are not
    trading days, so a same-day mismatch cannot affect any measured session.
    """
    cached = _ET_OFFSET_CACHE.get(d)
    if cached is not None:
        return cached
    from zoneinfo import ZoneInfo

    aware = datetime.combine(d, time(12, 0), tzinfo=ZoneInfo("America/New_York"))
    off = int(round(-aware.utcoffset().total_seconds() / 3600))
    _ET_OFFSET_CACHE[d] = off
    return off


def _fetch_5min(symbol: str, start: date, end: date) -> Dict[date, Dict[int, float]]:
    """5-minute bar closes keyed by trading date, then by ET minute-of-day.

    5-minute granularity is deliberate: every timestamp this study needs (09:15,
    09:35, 12:00, 15:00) lands on a 5-minute boundary, and it cuts the wire volume
    5x versus 1-minute bars over a 2.5-year window.
    """
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from dotenv import load_dotenv

    load_dotenv()
    client = StockHistoricalDataClient(
        os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    )

    out: Dict[date, Dict[int, float]] = defaultdict(dict)
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + timedelta(days=90))
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=datetime.combine(chunk_start, time.min),
            end=datetime.combine(chunk_end, time.max),
        )
        # Alpaca's Basic plan caps at 200 req/min and paginated 5-minute history
        # is request-heavy; without backoff a multi-symbol fetch dies mid-window.
        import time as _time
        delay = 2.0
        for attempt in range(8):
            try:
                bars = client.get_stock_bars(req)
                break
            except Exception as exc:  # noqa: BLE001 - only 429s are retried
                if "too many requests" not in str(exc).lower() or attempt == 7:
                    raise
                _time.sleep(delay)
                delay = min(delay * 2, 60.0)
        for b in (bars.data or {}).get(symbol, []):
            ts = b.timestamp  # tz-aware UTC
            d_utc = ts.date()
            off = _et_offset_hours(d_utc)
            et_minutes = (ts.hour - off) * 60 + ts.minute
            if et_minutes < 0:  # pre-04:00 ET wrap into the previous ET day
                et_minutes += 24 * 60
                d_et = d_utc - timedelta(days=1)
            else:
                d_et = d_utc
            out[d_et][et_minutes] = float(b.close)
        chunk_start = chunk_end + timedelta(days=1)
    return out


def _price_at(day_bars: Dict[int, float], minute: int) -> Optional[float]:
    """Last traded price as of ET ``minute``.

    Alpaca bars are LEFT-labelled: the bar keyed 09:30 aggregates [09:30, 09:35)
    and its close is the last print before 09:35. So the price "at 09:35" is the
    close of the bar keyed 09:30 — hence ``minute - 1``, not ``minute``. Using
    ``<= minute`` would pick the 09:35 bar and return the 09:40 price, shifting
    every measurement five minutes late.
    """
    candidates = [m for m in day_bars if m <= minute - 1]
    if not candidates:
        return None
    return day_bars[max(candidates)]


def run_intraday(symbols: Sequence[str], start: date, end: date, out: Path) -> dict:
    """Reconstruct the broken gate's measurement and the fixed gate's, per scan."""
    from src.utils.config import Config
    from src.backtesting.data.alpaca_provider import (
        SPLIT_RATIO_HIGH,
        SPLIT_RATIO_LOW,
        AlpacaDataProvider,
    )

    provider = AlpacaDataProvider.from_config(Config())
    per_symbol: Dict[str, dict] = {}
    all_rows: List[dict] = []

    for symbol in symbols:
        daily = provider.get_stock_bars(symbol, start, end)
        prev_close_by_day = {}
        for prev, curr in zip(daily, daily[1:]):
            if prev.close <= 0:
                continue
            ratio = curr.close / prev.close
            if ratio < SPLIT_RATIO_LOW or ratio > SPLIT_RATIO_HIGH:
                continue
            prev_close_by_day[curr.bar_date] = prev.close

        intraday = _fetch_5min(symbol, start, end)
        rows: List[dict] = []
        no_premarket = 0
        for d, prev_close in sorted(prev_close_by_day.items()):
            day_bars = intraday.get(d)
            if not day_bars:
                continue
            # Anchor the broken gate used: last print through 09:35 - 20min = 09:15.
            broken_anchor_min = SCAN_TIMES_ET[0][0] * 60 + SCAN_TIMES_ET[0][1] - BROKEN_LOOKBACK_MINUTES
            p_anchor = _price_at(day_bars, broken_anchor_min)
            # Was there any print at all today before the anchor? If not, the
            # partial daily bar does not exist yet and the broken gate would
            # accidentally have read yesterday's close (i.e. been correct).
            premarket_exists = p_anchor is not None
            if not premarket_exists:
                no_premarket += 1
                p_anchor = prev_close

            scan_prices = {}
            for hh, mm in SCAN_TIMES_ET:
                scan_prices[f"{hh:02d}{mm:02d}"] = _price_at(day_bars, hh * 60 + mm)
            p_0935 = scan_prices["0935"]
            if p_0935 is None:
                continue

            row = {
                "symbol": symbol,
                "date": d.isoformat(),
                "prev_close": prev_close,
                "p_0915": round(p_anchor, 4),
                "premarket_print_exists": premarket_exists,
                "broken_measure_pct": round((p_0935 - p_anchor) / p_anchor * 100, 4),
                "fixed_0935_pct": round((p_0935 - prev_close) / prev_close * 100, 4),
            }
            # The broken gate ran at ALL THREE scans, not just 09:35. At noon and
            # 15:00 its "previous close" was the same partial bar, ~20 minutes
            # stale, so it measured 20 minutes of intraday drift. Reconstruct
            # each scan so total damage can be counted, not just the 09:35 leg.
            broken_by_scan = {}
            for hh, mm in SCAN_TIMES_ET:
                label = f"{hh:02d}{mm:02d}"
                px = scan_prices[label]
                if px is not None:
                    row[f"fixed_{label}_pct"] = round((px - prev_close) / prev_close * 100, 4)
                anchor_min = hh * 60 + mm - BROKEN_LOOKBACK_MINUTES
                anchor = _price_at(day_bars, anchor_min)
                if px is not None and anchor:
                    val = round((px - anchor) / anchor * 100, 4)
                    row[f"broken_{label}_pct"] = val
                    broken_by_scan[label] = val
            row["broken_max_abs_across_scans"] = (
                round(max(abs(v) for v in broken_by_scan.values()), 4) if broken_by_scan else None
            )
            rows.append(row)

        n = len(rows)
        scan_labels = [f"{hh:02d}{mm:02d}" for hh, mm in SCAN_TIMES_ET]

        def _all_scans_block(r, t) -> Optional[bool]:
            vals = [r.get(f"fixed_{lbl}_pct") for lbl in scan_labels]
            if any(v is None for v in vals):
                return None
            return all(abs(v) > t for v in vals)

        stats = {
            "n_days": n,
            "days_with_no_premarket_print": no_premarket,
            "broken": {
                "mean_abs": round(mean(abs(r["broken_measure_pct"]) for r in rows), 3) if n else 0.0,
                "max_abs": round(max(abs(r["broken_measure_pct"]) for r in rows), 3) if n else 0.0,
                "block_rate_pct": {
                    str(t): _pct(sum(1 for r in rows if abs(r["broken_measure_pct"]) > t), n)
                    for t in THRESHOLDS
                },
            },
            "fixed_0935": {
                "mean_abs": round(mean(abs(r["fixed_0935_pct"]) for r in rows), 3) if n else 0.0,
                "max_abs": round(max(abs(r["fixed_0935_pct"]) for r in rows), 3) if n else 0.0,
                "block_rate_pct": {
                    str(t): _pct(sum(1 for r in rows if abs(r["fixed_0935_pct"]) > t), n)
                    for t in THRESHOLDS
                },
            },
            # THE headline honesty instrument: reference = the fixed gate's true
            # 9:35 measure; other = what the broken gate actually computed.
            "misclassification_fixed_vs_broken": {
                str(t): _matrix(rows, "fixed_0935_pct", "broken_measure_pct", t)
                for t in THRESHOLDS
            },
            # A day is fully lost only if ALL THREE live scans exceed threshold.
            "all_three_scans_block_pct": {},
            "any_scan_blocks_pct": {},
            # The broken gate's real footprint: it ran at every scan, so a day
            # lost an entry whenever ANY scan's 20-minute drift exceeded the
            # (then-armed) threshold at the moment the strategy wanted to trade.
            "broken_any_scan_block_pct": {},
            "broken_all_scans_block_pct": {},
        }
        for t in THRESHOLDS:
            anyb, allb, usable = 0, 0, 0
            for r in rows:
                vals = [r.get(f"broken_{lbl}_pct") for lbl in scan_labels]
                vals = [v for v in vals if v is not None]
                if not vals:
                    continue
                usable += 1
                if any(abs(v) > t for v in vals):
                    anyb += 1
                if len(vals) == len(scan_labels) and all(abs(v) > t for v in vals):
                    allb += 1
            stats["broken_any_scan_block_pct"][str(t)] = _pct(anyb, usable)
            stats["broken_all_scans_block_pct"][str(t)] = _pct(allb, usable)
        for t in THRESHOLDS:
            flags = [_all_scans_block(r, t) for r in rows]
            usable = [f for f in flags if f is not None]
            stats["all_three_scans_block_pct"][str(t)] = _pct(sum(1 for f in usable if f), len(usable))
            anyflags = []
            for r in rows:
                vals = [r.get(f"fixed_{lbl}_pct") for lbl in scan_labels]
                vals = [v for v in vals if v is not None]
                anyflags.append(any(abs(v) > t for v in vals))
            stats["any_scan_blocks_pct"][str(t)] = _pct(sum(anyflags), len(anyflags))

        per_symbol[symbol] = stats
        all_rows.extend(rows)
        print(f"  {symbol}: {n} days | broken>1.5%: "
              f"{stats['broken']['block_rate_pct']['1.5']}% | fixed@0935>1.5%: "
              f"{stats['fixed_0935']['block_rate_pct']['1.5']}% | all-3-scans: "
              f"{stats['all_three_scans_block_pct']['1.5']}%")

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "scan_times_et": [f"{h:02d}:{m:02d}" for h, m in SCAN_TIMES_ET],
        "broken_anchor_note": (
            "The broken gate compared a live IEX quote midpoint to the current "
            "session's PARTIAL daily bar, whose close is the last print as of "
            "now-20min. Both legs are approximated here by consolidated 5-minute "
            "trade prints, not IEX quote midpoints."
        ),
        "per_symbol": per_symbol,
    }
    _write(out, "intraday.json", payload)
    _write(out, "intraday_rows.json", all_rows)
    return payload


# --------------------------------------------------------------------------- #
# Layer 2 — engine A/B
# --------------------------------------------------------------------------- #

def _install_rate_limit_retry(max_tries: int = 8) -> None:
    """Wrap the provider's data endpoints with 429 backoff.

    Analysis-side only — production code is never touched by this study. Alpaca's
    Basic plan caps at 200 req/min and a full-history chain build for one symbol
    issues thousands; two study processes in parallel trip it. Without this, a
    run dies 40 minutes in and throws away a warm cache's worth of work.
    """
    import time as _time

    from src.backtesting.data.alpaca_provider import AlpacaDataProvider

    if getattr(AlpacaDataProvider, "_fc036_retry_installed", False):
        return

    def _wrap(name):
        original = getattr(AlpacaDataProvider, name)

        def wrapper(self, *a, **kw):
            delay = 2.0
            for attempt in range(max_tries):
                try:
                    return original(self, *a, **kw)
                except Exception as exc:  # noqa: BLE001 - only 429s are retried
                    if "too many requests" not in str(exc).lower() or attempt == max_tries - 1:
                        raise
                    _time.sleep(delay)
                    delay = min(delay * 2, 60.0)
            raise RuntimeError("unreachable")

        wrapper.__name__ = name
        setattr(AlpacaDataProvider, name, wrapper)

    for method in ("get_option_bars", "get_contract_universe", "get_stock_bars"):
        _wrap(method)
    AlpacaDataProvider._fc036_retry_installed = True


def _fresh_config(threshold: float):
    """A brand-new Config with the stage-4 threshold injected.

    `Config.execution_gap_threshold` is a read-only property; the backing dict is
    the only injection point. A fresh Config per arm — never a shared, mutated one.
    """
    from src.utils.config import Config

    config = Config()
    config._config["risk"]["gap_risk_controls"]["execution_gap_threshold"] = threshold
    assert config.execution_gap_threshold == threshold
    return config


def run_layer2(symbol: str, arms: Sequence[float], start: date, end: date,
               out: Path, starting_cash: float = 100_000.0) -> dict:
    from src.backtesting.evaluate import evaluate_symbol

    _install_rate_limit_retry()
    sym_start = SYMBOL_START_OVERRIDES.get(symbol, start)
    results = {}
    for threshold in arms:
        config = _fresh_config(threshold)
        t0 = datetime.now()
        report, _ = evaluate_symbol(
            symbol, sym_start, end,
            config=config,
            starting_cash=starting_cash,
            # The bid-fill sensitivity pass replays the identical window a second
            # time. It answers a different question (fill assumption), doubles the
            # runtime, and is not part of this study's contract.
            run_sensitivity=False,
        )
        dq = report.data_quality or {}
        rejections = dq.get("blocked_days_by_reason", {}) or {}
        arm = {
            "threshold": threshold,
            "symbol": symbol,
            "start": sym_start.isoformat(),
            "end": end.isoformat(),
            "runtime_s": round((datetime.now() - t0).total_seconds(), 1),
            "final_equity": round(report.final_equity, 2),
            "total_pnl": round(report.total_pnl, 2),
            "total_return_pct": round(report.total_return * 100, 3),
            "return_on_collateral_pct": (
                None if report.return_on_collateral is None
                else round(report.return_on_collateral * 100, 3)
            ),
            "max_drawdown_pct": round(report.max_drawdown * 100, 3),
            "option_pnl": round(report.option_pnl, 2),
            "stock_pnl": round(report.stock_pnl, 2),
            "unrealized_stock_pnl": round(report.unrealized_stock_pnl, 2),
            "dividends": round(report.dividends, 2),
            "puts_sold": report.puts_sold,
            "calls_sold": report.calls_sold,
            "assignments": report.assignments,
            "rolls": report.rolls,
            "cycles": len(report.cycles),
            "days_in_position": report.days_in_position,
            "avg_collateral": round(report.avg_collateral, 2),
            "peak_collateral": round(report.peak_collateral, 2),
            "decision_days": dq.get("decision_days"),
            "candidate_days": dq.get("days_with_a_qualifying_candidate"),
            "stage4_blocked_days": rejections.get(STAGE4_REJECTION_KEY, 0),
            "rejections": rejections,
            "worst_cycle_pnl": (
                round(min((c.total_pnl for c in report.cycles), default=0.0), 2)
            ),
        }
        results[str(threshold)] = arm
        print(f"  {symbol} @ {threshold}: return {arm['total_return_pct']}% | "
              f"roc {arm['return_on_collateral_pct']}% | dd {arm['max_drawdown_pct']}% | "
              f"puts {arm['puts_sold']} calls {arm['calls_sold']} | "
              f"stage4-blocked {arm['stage4_blocked_days']} | {arm['runtime_s']}s")

        # The baseline arm's ledger is the input to the attribution join.
        if threshold == BASELINE_THRESHOLD:
            events = []
            for cycle in report.cycles:
                for ev in cycle.events:
                    if ev.kind in ("sell_put_open", "sell_call_open"):
                        events.append({
                            "symbol": symbol,
                            "date": ev.event_date.isoformat(),
                            "kind": ev.kind,
                            "contract": ev.symbol,
                            "contracts": ev.contracts,
                            "premium": round(ev.cash_delta, 2),
                            "cycle_start": cycle.start.isoformat(),
                            "cycle_end": cycle.end.isoformat() if cycle.end else None,
                            "cycle_pnl": round(cycle.total_pnl, 2),
                            "cycle_open": cycle.is_open,
                            "cycle_assigned": cycle.assigned,
                        })
            _write(out, f"layer2_{symbol}_baseline_opens.json", events)

    payload = {"symbol": symbol, "arms": results,
               "generated_at": datetime.now().isoformat(timespec="seconds")}
    _write(out, f"layer2_{symbol}.json", payload)
    return payload


# --------------------------------------------------------------------------- #
# Arm-A equivalence: pre-fix production behaviour == post-fix shadow behaviour
# --------------------------------------------------------------------------- #

def _broken_get_previous_close(self, symbol, current_time):
    """Verbatim pre-fix ``_get_previous_close`` (gap_detector.py @ 110694a).

    Monkeypatched onto GapDetector for ONE run inside this harness so the
    equivalence claim is tested against the actual old code rather than asserted.
    Production is never modified.
    """
    from datetime import timedelta as _td

    try:
        _ = current_time - _td(days=3)  # dead in the original too; kept verbatim
        df = self.alpaca.get_stock_bars(symbol, days=5)
        if df.empty:
            return None
        if current_time.tzinfo is None:
            import pytz
            current_time = pytz.UTC.localize(current_time)
        recent_data = df[df.index < current_time]
        if recent_data.empty:
            return None
        return recent_data['close'].iloc[-1]
    except Exception:
        return None


# One chain lake per process, a fresh ChainStore per use — the same shape
# `screen.py` uses (FC-060). A lake per call would mean a GCS client, a startup
# probe and an independent circuit breaker for every simulated arm, so an
# outage would be rediscovered instead of remembered.
_CHAIN_LAKE = None
_CHAIN_LAKE_RESOLVED = False


def _chain_store():
    global _CHAIN_LAKE, _CHAIN_LAKE_RESOLVED
    from src.backtesting.data.chain_store import ChainStore

    if not _CHAIN_LAKE_RESOLVED:
        _CHAIN_LAKE = ChainStore.lake_from_env()
        _CHAIN_LAKE_RESOLVED = True
    return ChainStore(lake=_CHAIN_LAKE)


def _run_sim(symbol: str, start: date, end: date, config):
    """Drive the Simulator directly so the raw ledger and equity curve are visible."""
    from src.backtesting.data.alpaca_provider import AlpacaDataProvider
    from src.backtesting.data.chain_builder import ChainBuilder
    from src.backtesting.data.dividends import load_default_schedule
    from src.backtesting.engine.simulator import Simulator

    provider = AlpacaDataProvider.from_config(config)
    # Shares the FC-060 chain lake when CHAIN_LAKE_BUCKET is set, so a study run
    # warms (and contributes to) the same history the Job uses.
    builder = ChainBuilder(provider, store=_chain_store())
    sim = Simulator(
        config, provider, builder, [symbol], start, end,
        starting_cash=100_000.0, max_dte=getattr(config, "put_target_dte", 7),
        fill_haircut=0.25, dividend_schedule=load_default_schedule(),
    )
    return sim.run()


def _ledger_fingerprint(result) -> List[tuple]:
    return [
        (e.event_date.isoformat(), e.kind, e.symbol, e.contracts, e.shares,
         round(e.price, 6), round(e.cash_delta, 6))
        for e in result.broker.ledger
    ]


def run_equivalence(symbol: str, start: date, end: date, out: Path) -> dict:
    """Pre-fix production (broken gate armed at 1.5) vs post-fix shadow (fixed @ 999).

    The plan asserts these are behaviourally identical in replay, because the
    broken gate compares close_D to close_D and reads exactly 0.0%. This proves it
    rather than believing it — and it is the empirical evidence that Phase A ships
    a no-op to the backtest surface.
    """
    from src.risk.gap_detector import GapDetector

    _install_rate_limit_retry()

    fixed_cfg = _fresh_config(BASELINE_THRESHOLD)
    fixed = _run_sim(symbol, start, end, fixed_cfg)
    fixed_ledger = _ledger_fingerprint(fixed)
    fixed_equity = [round(d.equity, 6) for d in fixed.daily]

    original = GapDetector._get_previous_close
    GapDetector._get_previous_close = _broken_get_previous_close
    try:
        broken_cfg = _fresh_config(1.5)  # the threshold production actually ran pre-FC-036
        broken = _run_sim(symbol, start, end, broken_cfg)
        broken_ledger = _ledger_fingerprint(broken)
        broken_equity = [round(d.equity, 6) for d in broken.daily]
    finally:
        GapDetector._get_previous_close = original

    identical = fixed_ledger == broken_ledger and fixed_equity == broken_equity
    payload = {
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "fixed_at_999": {
            "ledger_events": len(fixed_ledger),
            "final_equity": fixed_equity[-1] if fixed_equity else None,
            "stage4_blocked_days": fixed.rejections.get(STAGE4_REJECTION_KEY, 0),
        },
        "broken_at_1p5": {
            "ledger_events": len(broken_ledger),
            "final_equity": broken_equity[-1] if broken_equity else None,
            "stage4_blocked_days": broken.rejections.get(STAGE4_REJECTION_KEY, 0),
        },
        "ledgers_identical": fixed_ledger == broken_ledger,
        "equity_curves_identical": fixed_equity == broken_equity,
        "VERDICT": "EQUIVALENT" if identical else "DIVERGENT",
    }
    print(json.dumps(payload, indent=2))
    _write(out, f"equivalence_{symbol}.json", payload)
    return payload


# --------------------------------------------------------------------------- #
# Attribution — baseline opens joined against the layer-1 gap table
# --------------------------------------------------------------------------- #

def run_attribute(out: Path, symbols: Sequence[str]) -> dict:
    rows = _read(out, "layer1_rows.json")
    gap_by = {(r["symbol"], r["date"]): r for r in rows}
    try:
        intraday_rows = _read(out, "intraday_rows.json")
        intraday_by = {(r["symbol"], r["date"]): r for r in intraday_rows}
    except SystemExit:
        intraday_by = {}

    per_symbol = {}
    for symbol in symbols:
        path = out / f"layer2_{symbol}_baseline_opens.json"
        if not path.exists():
            continue
        opens = json.loads(path.read_text())
        buckets = {}
        for t in THRESHOLDS:
            would_block_c2c = []
            would_block_overnight = []
            for o in opens:
                g = gap_by.get((symbol, o["date"]))
                if not g:
                    continue
                if abs(g["close_to_close_pct"]) > t:
                    would_block_c2c.append(o)
                iv = intraday_by.get((symbol, o["date"]))
                measure = iv["fixed_0935_pct"] if iv else g["overnight_gap_pct"]
                if abs(measure) > t:
                    would_block_overnight.append(o)

            def _summarize(sel):
                cycles = {}
                for o in sel:
                    cycles[o["cycle_start"]] = o
                return {
                    "opens_blocked": len(sel),
                    "premium_blocked": round(sum(o["premium"] for o in sel), 2),
                    "cycles_started_on_blocked_day": len(cycles),
                    "pnl_of_those_cycles": round(sum(c["cycle_pnl"] for c in cycles.values()), 2),
                    "n_winners": sum(1 for c in cycles.values() if c["cycle_pnl"] > 0),
                    "n_losers": sum(1 for c in cycles.values() if c["cycle_pnl"] <= 0),
                }

            buckets[str(t)] = {
                "by_close_to_close": _summarize(would_block_c2c),
                "by_live_0935_measure": _summarize(would_block_overnight),
            }
        per_symbol[symbol] = {
            "total_opens": len(opens),
            "total_premium": round(sum(o["premium"] for o in opens), 2),
            "by_threshold": buckets,
        }
        print(f"  {symbol}: {len(opens)} baseline opens, "
              f"${per_symbol[symbol]['total_premium']:,.0f} premium")

    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "per_symbol": per_symbol}
    _write(out, "attribution.json", payload)
    return payload


# --------------------------------------------------------------------------- #
# Layer 3 — the real-fills join against BigQuery.
# --------------------------------------------------------------------------- #

# Schema notes, verified against the live table rather than assumed (the plan's
# sketch used column names this table does not have):
#   * the underlying column is `underlying`, not `underlying_symbol`
#   * sell-to-open is side='sell_short'; buy-to-close is side='buy'
#   * `net_amount` is NULL on FILL rows; `premium_total` (= price*qty*100) is the
#     populated money column. net_amount IS populated on OPTRD (the stock leg of
#     an assignment).
#   * FILL rows have activity_date NULL — only transaction_time is set, in UTC.
#   * assignment is a pair: OPASN (the option, OCC symbol) + OPTRD (the resulting
#     100-share purchase at the strike, symbol = the underlying).
#   * live fills begin 2025-10-06, NOT 2024-02 as the plan assumed.

_L3_ENTRIES_SQL = """
SELECT
  symbol                                              AS contract,
  underlying,
  DATE(transaction_time, 'America/New_York')          AS et_date,
  FORMAT_TIMESTAMP('%H:%M', transaction_time, 'America/New_York') AS et_time,
  qty,
  price,
  premium_total,
  strike_price,
  expiration,
  option_type
FROM `options_wheel.trades_from_activities`
WHERE activity_type = 'FILL' AND side = 'sell_short'
ORDER BY transaction_time
"""

_L3_CONTRACT_SQL = """
SELECT
  f.symbol                                                   AS contract,
  ANY_VALUE(f.underlying)                                    AS underlying,
  ANY_VALUE(f.strike_price)                                  AS strike,
  CAST(ANY_VALUE(f.expiration) AS STRING)                    AS expiration,
  ANY_VALUE(f.option_type)                                   AS option_type,
  SUM(IF(f.side = 'sell_short', f.premium_total, 0))         AS premium_in,
  SUM(IF(f.side = 'buy', f.premium_total, 0))                AS premium_out,
  SUM(IF(f.side = 'sell_short', f.qty, 0))                   AS qty_sold,
  SUM(IF(f.side = 'buy', f.qty, 0))                          AS qty_bought
FROM `options_wheel.trades_from_activities` f
WHERE f.activity_type = 'FILL'
GROUP BY f.symbol
"""

_L3_ASSIGN_SQL = """
SELECT symbol AS contract, underlying,
       CAST(DATE(transaction_time, 'America/New_York') AS STRING) AS assign_date
FROM `options_wheel.trades_from_activities`
WHERE activity_type = 'OPASN'
"""


def _bq(sql: str) -> List[dict]:
    """Run a query through the authenticated `bq` CLI and return rows as dicts.

    Deliberately the CLI rather than google-cloud-bigquery: the credential that
    exists here is a user login (`gcloud auth login`), not application-default
    credentials, and the CLI is what that authorises. READ ONLY — this study
    never writes to the production dataset.
    """
    import subprocess

    proc = subprocess.run(
        ["bq", "query", "--use_legacy_sql=false", "--format=json",
         "--max_rows=100000", sql],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"bq failed: {proc.stderr.strip()[:2000]}")
    out = proc.stdout.strip()
    start = out.find("[")
    if start == -1:
        return []
    return json.loads(out[start:])


def run_layer3_join(out: Path) -> dict:
    """Dollars of *actually taken* trades a working gate would have prevented.

    Blocking test, per entry, at each swept threshold, under three measures:
      overnight     — (open - prior close), the plan's specified measure
      fixed_0935    — the fixed gate's own reading at the 09:35 scan
      all_3_scans   — every one of 09:35/12:00/15:00 over threshold, i.e. the
                      day is lost outright rather than just one scan

    Outcome per entry:
      option_pnl    — the contract's realized option P&L (premium in minus
                      buybacks), attributed pro-rata across that contract's
                      entries by premium. Fully sourced from BQ.
      assign_mark   — for assigned puts only: (expiry close - strike) * 100 * qty,
                      the mark-to-market taken AT assignment, from Alpaca bars.
                      This is the plan's stated fallback attribution. It is NOT
                      full cycle P&L: it ignores covered-call income and any
                      later stock recovery, so it is conservative (it makes
                      assigned trades look worse than the wheel eventually made
                      them). Reported separately, never silently merged.
    """
    from src.utils.config import Config
    from src.backtesting.data.alpaca_provider import AlpacaDataProvider

    gap_rows = _read(out, "layer1_rows.json")
    gap_by = {(r["symbol"], r["date"]): r for r in gap_rows}
    try:
        intraday_by = {(r["symbol"], r["date"]): r
                       for r in _read(out, "intraday_rows.json")}
    except SystemExit:
        intraday_by = {}

    entries = _bq(_L3_ENTRIES_SQL)
    contracts = {c["contract"]: c for c in _bq(_L3_CONTRACT_SQL)}
    assigned = {a["contract"] for a in _bq(_L3_ASSIGN_SQL)}
    print(f"  BQ: {len(entries)} sell-to-open fills, {len(contracts)} contracts, "
          f"{len(assigned)} assignments")

    # Expiry-date closes for assigned contracts, for the assignment mark.
    provider = AlpacaDataProvider.from_config(Config())
    need = {(contracts[c]["underlying"], contracts[c]["expiration"])
            for c in assigned if c in contracts}
    closes: Dict[tuple, float] = {}
    for sym in sorted({u for u, _ in need}):
        days = sorted(d for u, d in need if u == sym)
        bars = provider.get_stock_bars(
            sym, date.fromisoformat(days[0]) - timedelta(days=5),
            date.fromisoformat(days[-1]) + timedelta(days=5))
        for b in bars:
            closes[(sym, b.bar_date.isoformat())] = b.close

    # Per-contract entry premium totals, for pro-rata attribution.
    entry_prem_by_contract: Dict[str, float] = defaultdict(float)
    for e in entries:
        entry_prem_by_contract[e["contract"]] += float(e["premium_total"] or 0)

    rows: List[dict] = []
    unmatched_gap = 0
    for e in entries:
        c = contracts.get(e["contract"])
        prem = float(e["premium_total"] or 0)
        share = prem / entry_prem_by_contract[e["contract"]] if entry_prem_by_contract[e["contract"]] else 0.0
        option_pnl = share * (float(c["premium_in"]) - float(c["premium_out"])) if c else 0.0

        assign_mark = 0.0
        is_assigned = e["contract"] in assigned
        if is_assigned and c and c["option_type"] == "put":
            close = closes.get((c["underlying"], c["expiration"]))
            if close is not None:
                assign_mark = share * (close - float(c["strike"])) * 100.0 * float(c["qty_sold"] or 1)

        g = gap_by.get((e["underlying"], e["et_date"]))
        iv = intraday_by.get((e["underlying"], e["et_date"]))
        if g is None:
            unmatched_gap += 1
        scan_vals = []
        if iv:
            scan_vals = [iv.get(f"fixed_{lbl}_pct")
                         for lbl in ("0935", "1200", "1500")]
            scan_vals = [v for v in scan_vals if v is not None]

        rows.append({
            "contract": e["contract"],
            "underlying": e["underlying"],
            "date": e["et_date"],
            "et_time": e["et_time"],
            "option_type": (c or {}).get("option_type"),
            "premium": round(prem, 2),
            "option_pnl": round(option_pnl, 2),
            "assigned": is_assigned,
            "assign_mark": round(assign_mark, 2),
            "trade_pnl": round(option_pnl + assign_mark, 2),
            "overnight_gap_pct": g["overnight_gap_pct"] if g else None,
            "fixed_0935_pct": iv.get("fixed_0935_pct") if iv else None,
            "scan_vals": scan_vals,
        })

    def _blocked(r, measure, t) -> Optional[bool]:
        if measure == "overnight":
            v = r["overnight_gap_pct"]
            return None if v is None else abs(v) > t
        if measure == "fixed_0935":
            v = r["fixed_0935_pct"]
            return None if v is None else abs(v) > t
        if measure == "all_3_scans":
            if len(r["scan_vals"]) < 3:
                return None
            return all(abs(v) > t for v in r["scan_vals"])
        raise ValueError(measure)

    def _summarize(sel: List[dict]) -> dict:
        return {
            "entries_blocked": len(sel),
            "premium_blocked": round(sum(r["premium"] for r in sel), 2),
            "option_pnl_blocked": round(sum(r["option_pnl"] for r in sel), 2),
            "assignments_blocked": sum(1 for r in sel if r["assigned"]),
            "assign_mark_blocked": round(sum(r["assign_mark"] for r in sel), 2),
            "trade_pnl_blocked": round(sum(r["trade_pnl"] for r in sel), 2),
            "puts_blocked": sum(1 for r in sel if r["option_type"] == "put"),
            "calls_blocked": sum(1 for r in sel if r["option_type"] == "call"),
            "winners": sum(1 for r in sel if r["trade_pnl"] > 0),
            "losers": sum(1 for r in sel if r["trade_pnl"] < 0),
            "worst_single_trade": round(min((r["trade_pnl"] for r in sel), default=0.0), 2),
            "best_single_trade": round(max((r["trade_pnl"] for r in sel), default=0.0), 2),
        }

    totals = {
        "entries": len(rows),
        "premium": round(sum(r["premium"] for r in rows), 2),
        "option_pnl": round(sum(r["option_pnl"] for r in rows), 2),
        "assignments": sum(1 for r in rows if r["assigned"]),
        "assign_mark": round(sum(r["assign_mark"] for r in rows), 2),
        "trade_pnl": round(sum(r["trade_pnl"] for r in rows), 2),
        "winners": sum(1 for r in rows if r["trade_pnl"] > 0),
        "losers": sum(1 for r in rows if r["trade_pnl"] < 0),
        "first_entry": min(r["date"] for r in rows) if rows else None,
        "last_entry": max(r["date"] for r in rows) if rows else None,
        "entries_with_no_gap_row": unmatched_gap,
    }

    by_measure: Dict[str, dict] = {}
    for measure in ("overnight", "fixed_0935", "all_3_scans"):
        per_t = {}
        for t in THRESHOLDS:
            flags = [(r, _blocked(r, measure, t)) for r in rows]
            usable = [(r, f) for r, f in flags if f is not None]
            sel = [r for r, f in usable if f]
            s = _summarize(sel)
            s["evaluable_entries"] = len(usable)
            s["premium_share_pct"] = _pct(
                int(round(s["premium_blocked"])), int(round(totals["premium"])))
            per_t[str(t)] = s
        by_measure[measure] = per_t

    # Per-symbol at the plan's 1.5, under the live 09:35 measure.
    per_symbol: Dict[str, dict] = {}
    for sym in sorted({r["underlying"] for r in rows}):
        srows = [r for r in rows if r["underlying"] == sym]
        sel = [r for r in srows if _blocked(r, "fixed_0935", 1.5)]
        per_symbol[sym] = {
            "entries": len(srows),
            "premium": round(sum(r["premium"] for r in srows), 2),
            "trade_pnl": round(sum(r["trade_pnl"] for r in srows), 2),
            "at_1.5_fixed_0935": _summarize(sel),
        }

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "BigQuery options_wheel.trades_from_activities (read-only)",
        "window": {"first_entry": totals["first_entry"], "last_entry": totals["last_entry"]},
        "totals": totals,
        "by_measure": by_measure,
        "per_symbol_at_1p5_fixed_0935": per_symbol,
    }
    _write(out, "layer3.json", payload)
    _write(out, "layer3_entries.json", rows)

    print(f"\n  {totals['entries']} real entries {totals['first_entry']}..{totals['last_entry']}, "
          f"${totals['premium']:,.0f} premium, option P&L ${totals['option_pnl']:,.0f}, "
          f"{totals['assignments']} assignments (mark ${totals['assign_mark']:,.0f})")
    for measure, per_t in by_measure.items():
        print(f"  -- {measure}")
        for t, s in per_t.items():
            print(f"     thr {t:>4}: {s['entries_blocked']:>3} entries "
                  f"(${s['premium_blocked']:>9,.0f} premium, {s['premium_share_pct']:>5.1f}%) | "
                  f"trade P&L ${s['trade_pnl_blocked']:>10,.0f} | "
                  f"W/L {s['winners']}/{s['losers']} | "
                  f"assignments {s['assignments_blocked']}")
    return payload


# --------------------------------------------------------------------------- #
# Layer 3 fallback — the query to run when BQ is unreachable.
# --------------------------------------------------------------------------- #

LAYER3_QUERY = """
-- FC-036 Layer 3: dollars of *actually taken* trades a working 1.5% stage-4 gate
-- would have prevented, and whether those trades won or lost.
--
-- Requires: gcloud auth login  (expired at study time; no non-interactive refresh)
-- Run with:  bq query --use_legacy_sql=false --project_id=<project> < this_query
--
-- Join key is (symbol, trade date). `gap_days` must be materialised from
-- layer1_rows.json (or recomputed in SQL from options_wheel stock history if
-- that table is current) — the study harness writes it as CSV-ready JSON.
WITH entries AS (
  SELECT
    underlying_symbol            AS symbol,
    DATE(transaction_time)       AS trade_date,
    symbol                       AS contract,
    side,
    qty,
    price,
    net_amount
  FROM `options_wheel.trades_from_activities`
  WHERE side = 'sell_to_open'
    AND DATE(transaction_time) BETWEEN '2024-02-01' AND '2026-07-24'
),
gap_days AS (
  -- one row per (symbol, date) with the true overnight gap; load from
  -- layer1_rows.json produced by tools/diagnostics/fc036_gap_gate_study.py
  SELECT symbol, trade_date, overnight_gap_pct FROM `<scratch_dataset>.fc036_gaps`
)
SELECT
  e.symbol,
  COUNT(*)                                   AS entries_on_gap_days,
  SUM(e.net_amount)                          AS premium_collected_on_gap_days,
  COUNTIF(ABS(g.overnight_gap_pct) > 1.5)    AS blocked_at_1p5,
  SUM(IF(ABS(g.overnight_gap_pct) > 1.5, e.net_amount, 0)) AS premium_blocked_at_1p5
FROM entries e
JOIN gap_days g
  ON g.symbol = e.symbol AND g.trade_date = e.trade_date
GROUP BY e.symbol
ORDER BY premium_blocked_at_1p5 DESC;

-- Follow-up (the outcome half): for each blocked contract, walk
-- trades_from_activities forward for the same OCC symbol to find the closing
-- leg (buy_to_close / expiration / assignment) and sum realized P&L. That is the
-- number that decides pre-registered rule (a).
""".strip()


def run_layer3(out: Path) -> dict:
    """Run the real-fills join; fall back to printing the query if BQ is unreachable."""
    try:
        return run_layer3_join(out)
    except Exception as exc:  # noqa: BLE001
        print(f"LAYER 3 COULD NOT RUN: {exc}")
        print("If this is an auth failure, run `gcloud auth login` and retry.")
        print("The query below is what answers the question; it is NOT approximated here.")
        print()
        print(LAYER3_QUERY)
        payload = {"status": "PENDING", "blocker": str(exc), "query": LAYER3_QUERY}
        _write(out, "layer3_pending.json", payload)
        return payload


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

def run_summary(out: Path, symbols: Sequence[str]) -> None:
    l1 = _read(out, "layer1.json")
    print("\n=== LAYER 1: block rates (% of decision days) ===")
    print(f"{'symbol':<7} {'n':>5} | " + " ".join(f"on>{t:<4}" for t in THRESHOLDS)
          + " | " + " ".join(f"c2c>{t:<3}" for t in THRESHOLDS))
    for sym, s in l1["per_symbol"].items():
        on = s["overnight"]["block_rate_pct"]
        cc = s["close_to_close"]["block_rate_pct"]
        print(f"{sym:<7} {s['n_days']:>5} | "
              + " ".join(f"{on[str(t)]:>6.2f}" for t in THRESHOLDS) + " | "
              + " ".join(f"{cc[str(t)]:>6.2f}" for t in THRESHOLDS))

    print("\n=== LAYER 2: arms ===")
    for sym in symbols:
        p = out / f"layer2_{sym}.json"
        if not p.exists():
            continue
        data = json.loads(p.read_text())
        print(f"\n{sym}  ({data['arms'][str(BASELINE_THRESHOLD)]['start']} .. "
              f"{data['arms'][str(BASELINE_THRESHOLD)]['end']})")
        print(f"  {'arm':>6} {'ret%':>8} {'roc%':>8} {'maxdd%':>8} {'puts':>5} "
              f"{'calls':>6} {'asgn':>5} {'blk4':>5}")
        for key, a in data["arms"].items():
            roc = "-" if a["return_on_collateral_pct"] is None else f"{a['return_on_collateral_pct']:.2f}"
            print(f"  {key:>6} {a['total_return_pct']:>8.2f} {roc:>8} "
                  f"{a['max_drawdown_pct']:>8.2f} {a['puts_sold']:>5} "
                  f"{a['calls_sold']:>6} {a['assignments']:>5} {a['stage4_blocked_days']:>5}")


# --------------------------------------------------------------------------- #
# Markdown emitters — the write-up's tables are generated, never hand-typed.
# --------------------------------------------------------------------------- #

def run_markdown(out: Path, symbols: Sequence[str]) -> None:
    l1 = _read(out, "layer1.json")["per_symbol"]
    T = [str(t) for t in THRESHOLDS]

    print("### Layer 1 — block rate by measure (% of the 620 symbol-days)\n")
    print("| symbol | n | " + " | ".join(f"on>{t}" for t in T) + " | "
          + " | ".join(f"c2c>{t}" for t in T) + " | median \\|on\\| | p99 \\|on\\| | max \\|on\\| |")
    print("|---|---:|" + "---:|" * (2 * len(T) + 3))
    for s, v in l1.items():
        o, c = v["overnight"], v["close_to_close"]
        print(f"| {s} | {v['n_days']} | "
              + " | ".join(f"{o['block_rate_pct'][t]:.2f}%" for t in T) + " | "
              + " | ".join(f"{c['block_rate_pct'][t]:.2f}%" for t in T)
              + f" | {o['median_abs']:.2f}% | {o['p99_abs']:.2f}% | {o['max_abs']:.2f}% |")

    print("\n### Layer 1 — misclassification, overnight (live) vs close-to-close (replay), threshold 1.5\n")
    print("| symbol | both block | live-only | replay-only | neither | agreement | replay misses of live blocks | replay blocks live would pass |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for s, v in l1.items():
        m = v["misclassification_overnight_vs_c2c"]["1.5"]
        print(f"| {s} | {m['both_block']} | {m['overnight_gap_pct_only']} | "
              f"{m['close_to_close_pct_only']} | {m['neither']} | {m['agreement_pct']:.1f}% | "
              f"{m['missed_by_other_pct']:.1f}% | {m['false_blocks_by_other_pct']:.1f}% |")

    try:
        idr = _read(out, "intraday.json")["per_symbol"]
    except SystemExit:
        idr = {}
    if idr:
        print("\n### Broken gate vs fixed gate at the 09:35 scan, threshold 1.5 (5-minute reconstruction)\n")
        print("| symbol | n | broken fired | of which spurious | fixed would fire | of those, broken MISSED | agreement |")
        print("|---|---:|---:|---:|---:|---:|---:|")
        for s, v in idr.items():
            m = v["misclassification_fixed_vs_broken"]["1.5"]
            print(f"| {s} | {v['n_days']} | {m['other_blocks']} ({v['broken']['block_rate_pct']['1.5']:.2f}%) | "
                  f"{m['broken_measure_pct_only']} ({m['false_blocks_by_other_pct']:.1f}%) | "
                  f"{m['reference_blocks']} ({v['fixed_0935']['block_rate_pct']['1.5']:.2f}%) | "
                  f"{m['fixed_0935_pct_only']} ({m['missed_by_other_pct']:.1f}%) | "
                  f"{m['agreement_pct']:.1f}% |")

        print("\n### Block rate by scan-coverage, fixed gate (% of sessions)\n")
        print("| symbol | thr | fires at 09:35 | fires at >=1 of 3 scans | fires at ALL 3 scans (day fully lost) | broken gate fired at >=1 scan |")
        print("|---|---:|---:|---:|---:|---:|")
        for s, v in idr.items():
            for t in T:
                print(f"| {s} | {t} | {v['fixed_0935']['block_rate_pct'][t]:.2f}% | "
                      f"{v['any_scan_blocks_pct'][t]:.2f}% | {v['all_three_scans_block_pct'][t]:.2f}% | "
                      f"{v.get('broken_any_scan_block_pct', {}).get(t, float('nan')):.2f}% |")

    print("\n### Layer 2 — engine arms\n")
    print("| symbol | window | arm | return | return on collateral | max DD | puts | calls | assign | stage-4 blocked days | decision days |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for sym in symbols:
        p = out / f"layer2_{sym}.json"
        if not p.exists():
            continue
        data = json.loads(p.read_text())
        for key, a in data["arms"].items():
            roc = "n/a" if a["return_on_collateral_pct"] is None else f"{a['return_on_collateral_pct']:.2f}%"
            arm = "999 (shadow)" if float(key) == BASELINE_THRESHOLD else key
            print(f"| {sym} | {a['start']}..{a['end']} | {arm} | {a['total_return_pct']:.2f}% | {roc} | "
                  f"{a['max_drawdown_pct']:.2f}% | {a['puts_sold']} | {a['calls_sold']} | "
                  f"{a['assignments']} | {a['stage4_blocked_days']} | {a['decision_days']} |")

    try:
        l3 = _read(out, "layer3.json")
    except SystemExit:
        l3 = None
    if l3:
        t = l3["totals"]
        print("\n### Layer 3 — real fills: what a working gate would have prevented\n")
        print(f"Book: **{t['entries']} sell-to-open fills, {t['first_entry']} .. {t['last_entry']}**, "
              f"${t['premium']:,.0f} premium, ${t['option_pnl']:,.0f} realized option P&L, "
              f"{t['assignments']} assignments (assignment-day mark ${t['assign_mark']:,.0f}), "
              f"net realized **${t['trade_pnl']:,.0f}**.\n")
        print("| measure | thr | entries blocked | premium blocked | % of premium | option P&L forgone | assignment mark avoided | **net P&L blocked** | W/L | assignments blocked |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        names = {"overnight": "overnight gap", "fixed_0935": "fixed gate @ 09:35",
                 "all_3_scans": "all 3 scans block"}
        for measure, per_t in l3["by_measure"].items():
            for t_, s in per_t.items():
                print(f"| {names.get(measure, measure)} | {t_} | {s['entries_blocked']} | "
                      f"${s['premium_blocked']:,.0f} | {s['premium_share_pct']:.1f}% | "
                      f"${s['option_pnl_blocked']:,.0f} | ${s['assign_mark_blocked']:,.0f} | "
                      f"**${s['trade_pnl_blocked']:+,.0f}** | {s['winners']}/{s['losers']} | "
                      f"{s['assignments_blocked']} |")

        print("\n### Layer 3 — per symbol at threshold 1.5, live 09:35 measure\n")
        print("| symbol | entries | premium | net realized | blocked | premium blocked | option P&L forgone | assignment mark avoided | **net P&L blocked** | W/L |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for sym, v in l3["per_symbol_at_1p5_fixed_0935"].items():
            b = v["at_1.5_fixed_0935"]
            print(f"| {sym} | {v['entries']} | ${v['premium']:,.0f} | ${v['trade_pnl']:,.0f} | "
                  f"{b['entries_blocked']} | ${b['premium_blocked']:,.0f} | "
                  f"${b['option_pnl_blocked']:,.0f} | ${b['assign_mark_blocked']:,.0f} | "
                  f"**${b['trade_pnl_blocked']:+,.0f}** | {b['winners']}/{b['losers']} |")

    try:
        attr = _read(out, "attribution.json")["per_symbol"]
    except SystemExit:
        attr = {}
    if attr:
        print("\n### Attribution — baseline-arm opens the gate would have prevented (live 09:35 measure)\n")
        print("| symbol | opens | premium | thr | opens blocked | premium blocked | % of premium | cycles blocked | P&L of those cycles | winners | losers |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for s, v in attr.items():
            for t in T:
                b = v["by_threshold"][t]["by_live_0935_measure"]
                share = _pct(int(round(b["premium_blocked"])), int(round(v["total_premium"])))
                print(f"| {s} | {v['total_opens']} | ${v['total_premium']:,.0f} | {t} | "
                      f"{b['opens_blocked']} | ${b['premium_blocked']:,.0f} | {share:.1f}% | "
                      f"{b['cycles_started_on_blocked_day']} | ${b['pnl_of_those_cycles']:,.0f} | "
                      f"{b['n_winners']} | {b['n_losers']} |")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command",
                    choices=["layer1", "intraday", "layer2", "attribute",
                             "layer3", "summary", "fccheck", "equiv", "markdown"])
    ap.add_argument("--symbols", default="")
    ap.add_argument("--symbol", default="")
    ap.add_argument("--arms", default=",".join(str(a) for a in ARMS))
    ap.add_argument("--start", default=WINDOW_START.isoformat())
    ap.add_argument("--end", default=WINDOW_END.isoformat())
    ap.add_argument("--out", default=str(_default_out()))
    args = ap.parse_args()

    out = Path(args.out)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    if args.symbols:
        symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    else:
        from src.utils.config import Config
        symbols = tuple(Config().stock_symbols)

    if args.command == "layer1":
        run_layer1(symbols, start, end, out)
    elif args.command == "fccheck":
        run_fc_entry_check(out)
    elif args.command == "intraday":
        run_intraday(symbols if args.symbols else LAYER2_SYMBOLS, start, end, out)
    elif args.command == "layer2":
        if not args.symbol:
            raise SystemExit("layer2 needs --symbol (one process per symbol)")
        arms = tuple(float(a) for a in args.arms.split(","))
        run_layer2(args.symbol.upper(), arms, start, end, out)
    elif args.command == "equiv":
        run_equivalence(args.symbol.upper() or "NVDA", start, end, out)
    elif args.command == "attribute":
        run_attribute(out, symbols if args.symbols else LAYER2_SYMBOLS)
    elif args.command == "layer3":
        run_layer3(out)
    elif args.command == "summary":
        run_summary(out, symbols if args.symbols else LAYER2_SYMBOLS)
    elif args.command == "markdown":
        run_markdown(out, symbols if args.symbols else LAYER2_SYMBOLS)


if __name__ == "__main__":
    main()
