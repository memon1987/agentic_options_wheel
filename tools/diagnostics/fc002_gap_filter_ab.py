#!/usr/bin/env python3
"""FC-002 / FC-042 B1 — A/B the **stage-2** gap-risk filter.

⚠️ **STALE AS OF FC-068 (2026-08-01).** This tool studies a filter that is no
longer wired into anything. `GapDetector` was consumed only by
`WheelEngine._find_new_opportunities` (stages 2 and 4), which FC-068 deleted —
production stopped calling that path in 2025, so the filter has not affected a
live trade in over ten months, and it now affects backtests too (it used to run
in replays only because the replay drove the dead path).

⚠️ **CODE DELETED BY FC-069 item 5 (2026-08-04).** GapDetector, the
`risk.gap_risk_controls` knobs and `tests/test_gap_detector.py` are no longer in
the tree; **the code lives at pre-S1 main SHA `afb6698`**. This file is kept as
the record of the study, not as a runnable harness — every mode that imports
`src.risk.gap_detector` or reads a gap knob off `Config` will now fail at
import or attribute access. To re-run it, check out `afb6698`. Gap risk is
absent by decision; FC-049 owns any evidence-based revival.

⚠️ **FC-060 Layer 2 (2026-08-28).** Config-arm studies of this shape now have a
supported home: ``python main.py --command sweep`` replays many selection-only
config variants over many symbols, materialising each window once
(``src/backtesting/scenarios/``, ``docs/BACKTEST_ENGINE.md`` §"Scenario
sweeps"). It will NOT bring this harness back — the gap knobs it needs no longer
exist as config, and the sweep's allowlist refuses any key the replay does not
read. This note exists so nobody rebuilds a bespoke arm-runner from here. The
file stays for its SHA pointers, which FC-049 relies on.


Consequences for this file, specifically:
  * the `verify` mode asserts *source properties* of `_find_new_opportunities`,
    which no longer exists — that mode will fail;
  * the measured layers below remain valid as history (what the filter WOULD
    have excluded), not as a description of current behaviour.

SCOPE
-----
This studies `GapDetector.analyze_gap_risk` / `_is_suitable_for_trading`
(`src/risk/gap_detector.py`) — the stage-2 symbol filter that combines

  * gap **frequency**: share of days in the lookback whose overnight gap exceeds
    `quality_gap_threshold` (2.0%), capped at `max_gap_frequency` (0.15), and
  * a realized-**vol** cap: annualized vol over the same lookback, capped at
    `max_historical_vol` (0.40), and
  * a **current-gap** rule: today's overnight gap above
    `max_overnight_gap_percent` (5.0%) blocks the day outright.

It is NOT the stage-4 execution gap gate (`can_execute_trade`), which is a
different control studied in docs/investigations/fc-036-gap-gate-study.md.

Two measured facts about the live filter that the FC entry gets wrong, both
verified by `layer1 --selfcheck`:

  * `vol_lookback_days: 252` in config/settings.yaml is **dead config**. Nothing
    reads `Config.vol_lookback_days`. The vol is computed over whatever frame
    `analyze_gap_risk` fetched, which is `gap_lookback_days + 20` = **50
    CALENDAR days** (~34 sessions).
  * The same 50-calendar-day frame carries the gap-frequency ratio, so both legs
    of the filter share one window.

LAYERS
------
  layer1    Filter reconstruction from daily bars, no engine. Per symbol-day:
            gap frequency, realized vol, the trailing-1y percentile of that
            symbol's own vol, and the block verdict + binding reason for every
            arm. Answers "what is actually binding, and how often".

  layer2    Engine A/B. Drives the real backtest engine once per arm. Fixed-cap
            arms are pure config injection; the vol-relative and graduated arms
            are analysis-side monkeypatches on GapDetector (production is never
            modified).

  layer3    REAL FILLS from BigQuery `options_wheel.trades_from_activities`
            (read-only). The filter's premise is "high realized vol predicts bad
            outcomes". This tests that against 330 real sell-to-open fills by
            bucketing every entry on its trailing vol at entry and reporting
            what those entries actually earned. Also calibrates the overlay's
            premium model by inverting Black-Scholes on each real fill.

  overlay   Daily-bar synthetic short-put overlay over the full 2024-02..2026-07
            window. The engine trades so rarely under the live filter that its
            arms are near-empty; this prices a 7-DTE ~0.175-delta put on EVERY
            symbol-day from bars plus the layer-3-calibrated IV model, so
            "blocked days vs allowed days" is answered on thousands of
            observations instead of dozens. Premium is MODELED — the layer
            reports its own sign-stability under +/-20% premium scaling.

  markdown  Emit the document tables from the JSON produced above.

RE-RUN
------
    OUT=/tmp/fc002
    python3 tools/diagnostics/fc002_gap_filter_ab.py layer1  --out $OUT
    python3 tools/diagnostics/fc002_gap_filter_ab.py layer3  --out $OUT   # needs gcloud auth
    python3 tools/diagnostics/fc002_gap_filter_ab.py overlay --out $OUT
    python3 tools/diagnostics/fc002_gap_filter_ab.py layer2  --symbol NVDA --out $OUT
    python3 tools/diagnostics/fc002_gap_filter_ab.py markdown --out $OUT

`layer2` is the expensive one; run one symbol per process. It reads the parquet
chain cache under `cache/backtest/chains/` (gitignored), so a warm cache makes
the arms cheap.

Companion write-up: docs/investigations/fc-002-gap-filter-ab.md
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# --------------------------------------------------------------------------- #
# Study parameters
# --------------------------------------------------------------------------- #

# Reporting window. Alpaca's options history floor is 2024-02; daily bars go
# back further and the earlier stretch is used ONLY to warm the trailing-1y
# vol percentile that arm (d) needs.
WINDOW_START = date(2024, 2, 1)
WINDOW_END = date(2026, 7, 24)
WARMUP_START = date(2022, 12, 1)

STUDY_SYMBOLS = ("NVDA", "AMD", "GOOGL", "IWM")

# Live filter constants, mirrored here so the reconstruction is explicit rather
# than implicitly inherited. `layer1 --selfcheck` asserts each against Config.
GAP_LOOKBACK_DAYS = 30          # config: gap_risk_controls.gap_lookback_days
LOOKBACK_PAD_DAYS = 20          # analyze_gap_risk asks for lookback + 20 CALENDAR days
QUALITY_GAP_THRESHOLD = 2.0     # % — counts a day as a "significant gap"
MAX_GAP_FREQUENCY = 0.15        # share of lookback days
MAX_HISTORICAL_VOL = 0.40       # annualized
PREMARKET_GAP_THRESHOLD = 2.0   # % — sets has_gap
MAX_OVERNIGHT_GAP_PERCENT = 5.0  # % — the current-gap block

# Windows the FC-002 entry makes specific claims about, checked explicitly so
# the claims are confirmed or corrected against data rather than repeated.
#   * "NVDA was blocked on 18 of 20 decision days in Jun-Jul 2026 by the 40%
#     realized-vol cap, its 30-day vol sitting at 40-42%"
#   * "AMD is gap-filtered on 47% of scans (262 of 553)"
FOCUS_WINDOWS = (
    ("NVDA", "2026-06-01", "2026-07-24", "FC-002: blocked ~90% of Jun-Jul 2026 by the vol cap"),
    ("AMD", "2024-02-01", "2026-07-24", "FC-002: 'gap-filtered on 47% of scans'"),
    ("AMD", "2025-10-06", "2026-07-24", "AMD over the live-fills window"),
    ("NVDA", "2025-10-06", "2026-07-24", "NVDA over the live-fills window"),
)

VOL_PERCENTILE = 0.80           # arm (d): block above the symbol's own p80
VOL_PERCENTILE_WINDOW = 252     # trading days of the symbol's OWN vol history
VOL_PERCENTILE_MIN_OBS = 120    # below this the percentile is not trustworthy

GRADUATED_DELTA_RANGE = [0.10, 0.15]   # arm (e), per the plan

STAGE2_REJECTION_KEY = "gap-risk filter (stage 2)"

# Ratio bounds for the crude split detector (same shape as the provider's).
SPLIT_RATIO_LOW = 0.5
SPLIT_RATIO_HIGH = 1.9

# The engine refuses windows spanning an unmodelled corporate action
# (UnadjustedCorporateAction) and a start too close to one drags the split into
# the 60-calendar-day warm-up, where GapDetector reads it as a price move and
# silently blocks everything. NVDA split 10:1 on 2024-06-10; 2024-08-15 - 60d =
# 2024-06-16, clear of it. Same override FC-036 used, for the same reason.
SYMBOL_START_OVERRIDES: Dict[str, date] = {"NVDA": date(2024, 8, 15)}

# Overlay parameters
OVERLAY_DTE = 7                 # config: put_target_dte
OVERLAY_TARGET_DELTA = 0.175    # midpoint of put_delta_range [0.15, 0.20]
OVERLAY_RISK_FREE = 0.042       # ~2-year average 3M T-bill over the window
OVERLAY_FILL_HAIRCUT = 0.25     # engine default: quarter of the modeled spread


def _default_out() -> Path:
    return Path(os.environ.get("FC002_OUT", "/tmp/fc002"))


def _write(out: Path, name: str, payload) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_text(json.dumps(payload, indent=2, default=str))
    print("  wrote %s" % path)
    return path


def _read(out: Path, name: str):
    path = out / name
    if not path.exists():
        raise SystemExit("missing %s - run the producing subcommand first" % path)
    return json.loads(path.read_text())


def _pct(n: float, d: float) -> float:
    return 0.0 if not d else round(100.0 * n / d, 2)


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
    """Nearest-rank quantile on an already-sorted sequence."""
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


# --------------------------------------------------------------------------- #
# Arms
# --------------------------------------------------------------------------- #
#
# `vol_cap`      fixed annualized-vol ceiling, or None to disable that leg
# `freq_cap`     gap-frequency ceiling, or None to disable that leg
# `vol_pctile`   block above the symbol's own trailing-1y vol percentile
# `graduated`    over-threshold shifts the delta band instead of banning
# `enabled`      False == enable_gap_detection: false (whole filter off)

ARMS: Dict[str, dict] = {
    # (a)
    "as_is":      {"label": "(a) filter as-is",           "enabled": True,  "freq_cap": MAX_GAP_FREQUENCY, "vol_cap": 0.40},
    # (b)
    "off":        {"label": "(b) filter off",             "enabled": False, "freq_cap": None, "vol_cap": None},
    # (c) vol cap swept. vol40 is identical to as_is and is kept as the sweep's
    #     own anchor so the sweep reads monotonically without cross-referencing.
    "vol40":      {"label": "(c) vol cap 40%",            "enabled": True,  "freq_cap": MAX_GAP_FREQUENCY, "vol_cap": 0.40},
    "vol50":      {"label": "(c) vol cap 50%",            "enabled": True,  "freq_cap": MAX_GAP_FREQUENCY, "vol_cap": 0.50},
    "vol60":      {"label": "(c) vol cap 60%",            "enabled": True,  "freq_cap": MAX_GAP_FREQUENCY, "vol_cap": 0.60},
    # Decomposition arms: which leg is actually binding? Not in the plan; added
    # because the FC entry's central claim ("NVDA is blocked by the vol cap, not
    # gap frequency") is exactly a decomposition question.
    "freq_only":  {"label": "(c') vol cap removed",       "enabled": True,  "freq_cap": MAX_GAP_FREQUENCY, "vol_cap": None},
    "vol_only":   {"label": "(c') freq cap removed",      "enabled": True,  "freq_cap": None, "vol_cap": 0.40},
    # (d)
    "vol_p80":    {"label": "(d) vol > own trailing p80", "enabled": True,  "freq_cap": MAX_GAP_FREQUENCY, "vol_cap": None, "vol_pctile": VOL_PERCENTILE},
    # (e) — same block condition as (a), but the response is a delta-band shift
    #      rather than a ban. Identical to (a) in layer 1 by construction.
    "graduated":  {"label": "(e) graduated: delta -> [0.10, 0.15]", "enabled": True, "freq_cap": MAX_GAP_FREQUENCY, "vol_cap": 0.40, "graduated": True},
}

ARM_ORDER = ("as_is", "off", "vol40", "vol50", "vol60", "freq_only", "vol_only",
             "vol_p80", "graduated")


def _verdict(arm: dict, freq: Optional[float], vol: Optional[float],
             vol_pct_threshold: Optional[float], today_gap_pct: Optional[float]) -> Tuple[bool, str]:
    """Reconstruct `_is_suitable_for_trading` for one arm on one symbol-day.

    Returns ``(blocked, binding_reason)``. Reason order mirrors the live code:
    gap frequency, then vol, then the current-day gap.
    """
    if not arm.get("enabled", True):
        return False, ""

    freq_cap = arm.get("freq_cap")
    if freq_cap is not None and freq is not None and freq > freq_cap:
        return True, "gap_frequency"

    vol_cap = arm.get("vol_cap")
    if vol_cap is not None and vol is not None and vol > vol_cap:
        return True, "volatility"

    if arm.get("vol_pctile") is not None and vol is not None:
        if vol_pct_threshold is not None and vol > vol_pct_threshold:
            return True, "volatility_percentile"

    # The current-gap rule is part of the filter in every enabled arm; no arm in
    # this study sweeps it (that is stage 4's territory).
    if today_gap_pct is not None and abs(today_gap_pct) > PREMARKET_GAP_THRESHOLD:
        if abs(today_gap_pct) > MAX_OVERNIGHT_GAP_PERCENT:
            return True, "current_gap"

    return False, ""


# --------------------------------------------------------------------------- #
# Layer 1 — filter reconstruction from daily bars
# --------------------------------------------------------------------------- #

def _load_bars(symbol: str, start: date, end: date):
    from src.utils.config import Config
    from src.backtesting.data.alpaca_provider import AlpacaDataProvider

    provider = AlpacaDataProvider.from_config(Config())
    return provider.get_stock_bars(symbol, start, end)


def _clean_series(bars) -> Tuple[List[dict], List[dict]]:
    """Per-session gap and return, with corporate-action days excluded.

    A 10:1 split reads as a -90% "return" in unadjusted bars and would poison
    every window containing it. The engine refuses such windows outright; the
    measurement layer instead drops the single contaminated session, which is
    what lets the vol percentile reach back past NVDA's 2024-06-10 split.
    """
    rows: List[dict] = []
    excluded: List[dict] = []
    for prev, curr in zip(bars, bars[1:]):
        if prev.close <= 0:
            continue
        ratio = curr.close / prev.close
        if ratio < SPLIT_RATIO_LOW or ratio > SPLIT_RATIO_HIGH:
            excluded.append({"date": curr.bar_date.isoformat(), "ratio": round(ratio, 4)})
            continue
        rows.append({
            "date": curr.bar_date,
            "prev_close": prev.close,
            "open": curr.open,
            "close": curr.close,
            "gap_pct": (curr.open - prev.close) / prev.close * 100.0,
            "ret": (curr.close - prev.close) / prev.close,
        })
    return rows, excluded


def _stdev(vals: Sequence[float]) -> float:
    """Sample standard deviation (ddof=1), matching pandas' Series.std()."""
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))


# Corporate-action sessions found by `build_daily_table`, keyed by symbol. The
# price series in `layer1_rows.json` is DISCONTINUOUS across these dates (a 10:1
# split moves the level by 10x), so any overlay trade whose holding window spans
# one must be dropped: it would otherwise book the split itself as a $1,000+
# per-share loss. Recorded here rather than re-derived, and persisted into
# layer1.json so `overlay` can read it back in a separate process.
CORPORATE_ACTIONS: Dict[str, List[str]] = {}


def build_daily_table(symbol: str, bars) -> List[dict]:
    """One row per session: the filter's own inputs, as it would compute them.

    Window semantics are the live client's: ``get_stock_bars(days=N)`` builds
    ``start = end - timedelta(days=N)``, i.e. **calendar** days, and the adapter
    keeps ``bar_date > cutoff``. So the frame is the sessions in
    ``(d - 50, d]`` — about 34 of them, not 50.
    """
    rows, excluded = _clean_series(bars)
    by_date = {r["date"]: i for i, r in enumerate(rows)}
    span = GAP_LOOKBACK_DAYS + LOOKBACK_PAD_DAYS
    out: List[dict] = []

    for i, r in enumerate(rows):
        d = r["date"]
        cutoff = d - timedelta(days=span)
        # Sessions in the frame: those with bar_date in (cutoff, d].
        j = i
        while j >= 0 and rows[j]["date"] > cutoff:
            j -= 1
        frame = rows[j + 1: i + 1]
        if len(frame) < 10:
            # `_calculate_historical_volatility` returns 0.0 below 10 bars, and
            # the frequency ratio is meaningless. Live would behave the same.
            continue
        gaps = [f["gap_pct"] for f in frame]
        rets = [f["ret"] for f in frame]
        freq = sum(1 for g in gaps if abs(g) > QUALITY_GAP_THRESHOLD) / float(len(gaps))
        vol = _stdev(rets) * math.sqrt(252)
        out.append({
            "symbol": symbol,
            "date": d.isoformat(),
            "close": r["close"],
            "frame_days": len(frame),
            "gap_frequency": round(freq, 4),
            "vol": round(vol, 4),
            "today_gap_pct": round(r["gap_pct"], 4),
        })

    # Trailing-1y percentile of the symbol's OWN vol, for arm (d). Strictly
    # backward-looking: the threshold on day d uses vols through d-1 only, so
    # the arm never reads its own future.
    vols: List[float] = []
    for row in out:
        if len(vols) >= VOL_PERCENTILE_MIN_OBS:
            window = sorted(vols[-VOL_PERCENTILE_WINDOW:])
            row["vol_p80_threshold"] = round(_quantile(window, VOL_PERCENTILE), 4)
            row["vol_pctile_rank"] = round(
                sum(1 for v in window if v <= row["vol"]) / float(len(window)), 4)
        else:
            row["vol_p80_threshold"] = None
            row["vol_pctile_rank"] = None
        vols.append(row["vol"])

    CORPORATE_ACTIONS[symbol] = [e["date"] for e in excluded]
    if excluded:
        print("  %s: excluded corporate-action sessions: %s"
              % (symbol, [e["date"] for e in excluded]))
    return out


def run_layer1(symbols: Sequence[str], out: Path, selfcheck: bool = True) -> dict:
    if selfcheck:
        _selfcheck_constants()

    per_symbol: Dict[str, dict] = {}
    all_rows: List[dict] = []

    for symbol in symbols:
        bars = _load_bars(symbol, WARMUP_START, WINDOW_END)
        table = build_daily_table(symbol, bars)
        all_rows.extend(table)

        window = [r for r in table
                  if WINDOW_START.isoformat() <= r["date"] <= WINDOW_END.isoformat()]
        n = len(window)
        arm_stats: Dict[str, dict] = {}
        for name in ARM_ORDER:
            arm = ARMS[name]
            blocked = 0
            reasons: Dict[str, int] = defaultdict(int)
            for r in window:
                b, why = _verdict(arm, r["gap_frequency"], r["vol"],
                                  r.get("vol_p80_threshold"), r["today_gap_pct"])
                if b:
                    blocked += 1
                    reasons[why] += 1
            arm_stats[name] = {
                "blocked_days": blocked,
                "block_rate_pct": _pct(blocked, n),
                "binding_reason_counts": dict(reasons),
            }

        vols = sorted(r["vol"] for r in window)
        freqs = sorted(r["gap_frequency"] for r in window)
        per_symbol[symbol] = {
            "n_days": n,
            "first_day": window[0]["date"] if window else None,
            "last_day": window[-1]["date"] if window else None,
            "vol": {
                "median": round(_quantile(vols, 0.5), 4),
                "p10": round(_quantile(vols, 0.10), 4),
                "p90": round(_quantile(vols, 0.90), 4),
                "max": round(vols[-1], 4) if vols else None,
                "days_above_40pct": sum(1 for v in vols if v > 0.40),
                "days_in_35_45_band": sum(1 for v in vols if 0.35 <= v <= 0.45),
            },
            "gap_frequency": {
                "median": round(_quantile(freqs, 0.5), 4),
                "p90": round(_quantile(freqs, 0.90), 4),
                "max": round(freqs[-1], 4) if freqs else None,
                "days_above_cap": sum(1 for f in freqs if f > MAX_GAP_FREQUENCY),
            },
            "corporate_actions": CORPORATE_ACTIONS.get(symbol, []),
            "arms": arm_stats,
        }
        print("  %-6s %d days | median vol %.3f | as-is block %.1f%% | vol>40%% on %d days"
              % (symbol, n, per_symbol[symbol]["vol"]["median"],
                 arm_stats["as_is"]["block_rate_pct"],
                 per_symbol[symbol]["vol"]["days_above_40pct"]))

    focus = []
    by_sym: Dict[str, List[dict]] = defaultdict(list)
    for r in all_rows:
        by_sym[r["symbol"]].append(r)
    for sym, s, e, note in FOCUS_WINDOWS:
        w = [r for r in by_sym.get(sym, []) if s <= r["date"] <= e]
        if not w:
            continue
        reasons: Dict[str, int] = defaultdict(int)
        for r in w:
            b, why = _verdict(ARMS["as_is"], r["gap_frequency"], r["vol"],
                              r.get("vol_p80_threshold"), r["today_gap_pct"])
            if b:
                reasons[why] += 1
        vols = sorted(r["vol"] for r in w)
        blocked = sum(reasons.values())
        focus.append({
            "symbol": sym, "start": s, "end": e, "note": note,
            "sessions": len(w), "blocked": blocked,
            "block_rate_pct": _pct(blocked, len(w)),
            "binding_reason_counts": dict(reasons),
            "vol_min": round(vols[0], 4), "vol_median": round(_quantile(vols, 0.5), 4),
            "vol_max": round(vols[-1], 4),
        })
        print("  focus %-6s %s..%s: %d/%d blocked (%.1f%%) %s vol %.3f-%.3f"
              % (sym, s, e, blocked, len(w), _pct(blocked, len(w)),
                 dict(reasons), vols[0], vols[-1]))

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "focus_windows": focus,
        "filter_constants": {
            "gap_lookback_days": GAP_LOOKBACK_DAYS,
            "frame_calendar_days": GAP_LOOKBACK_DAYS + LOOKBACK_PAD_DAYS,
            "quality_gap_threshold_pct": QUALITY_GAP_THRESHOLD,
            "max_gap_frequency": MAX_GAP_FREQUENCY,
            "max_historical_vol": MAX_HISTORICAL_VOL,
            "max_overnight_gap_percent": MAX_OVERNIGHT_GAP_PERCENT,
        },
        "arms": {k: ARMS[k]["label"] for k in ARM_ORDER},
        "per_symbol": per_symbol,
    }
    _write(out, "layer1.json", payload)
    _write(out, "layer1_rows.json", all_rows)
    return payload


def _selfcheck_constants() -> None:
    """Assert the mirrored constants against the live Config, and prove that
    `vol_lookback_days` is dead config rather than asserting it."""
    import inspect
    from src.utils.config import Config
    from src.risk import gap_detector as gd

    c = Config()
    checks = [
        ("gap_lookback_days", c.gap_lookback_days, GAP_LOOKBACK_DAYS),
        ("quality_gap_threshold", c.quality_gap_threshold, QUALITY_GAP_THRESHOLD),
        ("max_gap_frequency", c.max_gap_frequency, MAX_GAP_FREQUENCY),
        ("max_historical_vol", c.max_historical_vol, MAX_HISTORICAL_VOL),
        ("premarket_gap_threshold", c.premarket_gap_threshold, PREMARKET_GAP_THRESHOLD),
        ("max_overnight_gap_percent", c.max_overnight_gap_percent, MAX_OVERNIGHT_GAP_PERCENT),
    ]
    for name, live, mirrored in checks:
        if live != mirrored:
            raise SystemExit("constant drift: config.%s=%r but harness mirrors %r"
                             % (name, live, mirrored))
    src = inspect.getsource(gd)
    if "vol_lookback_days" in src:
        raise SystemExit("gap_detector now reads vol_lookback_days - the study's "
                         "'dead config' finding is stale, re-derive the vol window")
    print("  selfcheck: 6 constants match config/settings.yaml; "
          "vol_lookback_days (252) is unreferenced by gap_detector -> dead config")


# --------------------------------------------------------------------------- #
# verify — is the reconstruction faithful, and is the filter even wired in?
# --------------------------------------------------------------------------- #

# (symbol, date) pairs on which the live book actually sold a put. Each is a day
# the reconstruction says the stage-2 filter would have refused the symbol, so
# each is a direct test of "reconstruction wrong" vs "filter not running".
VERIFY_DAYS = (
    ("AMD", "2025-10-06"), ("AMD", "2025-12-01"), ("AMD", "2026-01-20"),
    ("AMD", "2026-04-20"), ("AMD", "2026-05-01"),
    ("NVDA", "2026-06-02"), ("GOOGL", "2026-02-04"),
)


class _FrameClient(object):
    """Minimal AlpacaClient stand-in: serves historical daily bars as of a date.

    Mirrors `BacktestAlpacaClient.get_stock_bars` — `days` is a CALENDAR-day
    lookback and the frame is the sessions in `(today - days, today]`.
    """

    def __init__(self, bars):
        self._bars = bars
        self.today = None

    def get_stock_bars(self, symbol, days=30):
        import pandas as pd
        cutoff = self.today - timedelta(days=days)
        sel = [b for b in self._bars if cutoff < b.bar_date <= self.today]
        if not sel:
            return pd.DataFrame()
        idx = pd.DatetimeIndex(
            [datetime.combine(b.bar_date, datetime.min.time()) for b in sel]
        ).tz_localize("UTC")
        return pd.DataFrame(
            {"open": [b.open for b in sel], "high": [b.high for b in sel],
             "low": [b.low for b in sel], "close": [b.close for b in sel],
             "volume": [b.volume for b in sel]}, index=idx)


def run_verify(out: Path) -> dict:
    """Two independent checks, both of which the study's conclusions rest on.

    1. **Fidelity.** Drive the REAL `GapDetector.analyze_gap_risk` over historical
       bars and diff its `historical_volatility` / `gap_frequency` / verdict
       against the harness reconstruction. If these disagree, every block rate
       in this study is wrong.

    2. **Wiring.** Establish, from source, which production modules can reach
       the stage-2 filter at all. The live Cloud Run trading path is
       `/scan` -> `OptionsScanner` -> opportunity store -> `/run`; the filter
       lives in `WheelEngine._find_new_opportunities`. If no module on the live
       path references `GapDetector`, the filter cannot have gated a live trade,
       and the whole A/B is a question about the backtest engine rather than
       about production.
    """
    import inspect
    from src.utils.config import Config
    from src.risk.gap_detector import GapDetector

    recon = {(r["symbol"], r["date"]): r for r in _read(out, "layer1_rows.json")}
    config = Config()

    fidelity: List[dict] = []
    bars_cache: Dict[str, list] = {}
    for symbol, ds in VERIFY_DAYS:
        if symbol not in bars_cache:
            bars_cache[symbol] = _load_bars(symbol, WARMUP_START, WINDOW_END)
        client = _FrameClient(bars_cache[symbol])
        client.today = date.fromisoformat(ds)
        gd = GapDetector(config, client)
        res = gd.analyze_gap_risk(
            symbol, datetime.combine(client.today, datetime.min.time()))
        hg = res.get("historical_gaps", {}) or {}
        r = recon.get((symbol, ds))
        rec_blocked = None
        if r:
            rec_blocked = _verdict(ARMS["as_is"], r["gap_frequency"], r["vol"],
                                   r.get("vol_p80_threshold"), r["today_gap_pct"])[0]
        fidelity.append({
            "symbol": symbol, "date": ds,
            "live_code_suitable": res.get("suitable_for_trading"),
            "live_code_vol": round(float(res.get("historical_volatility") or 0), 4),
            "live_code_gap_frequency": round(float(hg.get("gap_frequency") or 0), 4),
            "live_code_frame_days": hg.get("total_days"),
            "reconstruction_vol": r["vol"] if r else None,
            "reconstruction_gap_frequency": r["gap_frequency"] if r else None,
            "reconstruction_blocks": rec_blocked,
            "verdicts_agree": (rec_blocked is not None
                               and res.get("suitable_for_trading") is not None
                               and rec_blocked == (not res["suitable_for_trading"])),
        })
        print("  %-6s %s  live-code suitable=%-5s vol %.4f freq %.4f (n=%s) | "
              "recon vol %s freq %s blocks=%s" % (
                  symbol, ds, fidelity[-1]["live_code_suitable"],
                  fidelity[-1]["live_code_vol"], fidelity[-1]["live_code_gap_frequency"],
                  fidelity[-1]["live_code_frame_days"],
                  fidelity[-1]["reconstruction_vol"],
                  fidelity[-1]["reconstruction_gap_frequency"], rec_blocked))

    # ---- wiring -------------------------------------------------------------
    from src.data import options_scanner
    from src.strategy import wheel_engine, put_seller, call_seller
    from src.api import market_data

    modules = {
        "src/data/options_scanner.py (the LIVE /scan path)": options_scanner,
        "src/api/market_data.py": market_data,
        "src/strategy/put_seller.py": put_seller,
        "src/strategy/call_seller.py": call_seller,
        "src/strategy/wheel_engine.py (backtest + local CLI only)": wheel_engine,
    }
    wiring = {}
    for label, mod in modules.items():
        src = inspect.getsource(mod)
        wiring[label] = {
            "references_GapDetector": "GapDetector" in src,
            "calls_filter_stocks_by_gap_risk": "filter_stocks_by_gap_risk" in src,
        }
        print("  wiring: %-58s GapDetector=%s  filter_call=%s"
              % (label, wiring[label]["references_GapDetector"],
                 wiring[label]["calls_filter_stocks_by_gap_risk"]))

    server = (REPO_ROOT / "deploy" / "cloud_run_server.py").read_text()
    # WheelEngine IS constructed in the server, twice — but for
    # `reconcile_positions()` (pre-trade housekeeping on /run) and
    # `run_rolling_cycle()` (/roll). Neither enters `_find_new_opportunities`,
    # which is the only method that calls the stage-2 filter. Asserted here so
    # the claim is checked rather than eyeballed.
    wiring["deploy/cloud_run_server.py"] = {
        "references_GapDetector": "GapDetector" in server,
        "constructs_WheelEngine": "WheelEngine(" in server,
        "constructs_OptionsScanner": "OptionsScanner(" in server,
        "calls_run_strategy_cycle": "run_strategy_cycle(" in server,
        "wheel_engine_methods_called": sorted(set(
            m for m in ("reconcile_positions", "run_rolling_cycle",
                        "run_strategy_cycle", "_find_new_opportunities")
            if ("engine.%s(" % m) in server)),
    }
    stage2_callers = sorted(
        name for name, m in inspect.getmembers(wheel_engine.WheelEngine,
                                               predicate=inspect.isfunction)
        if "filter_stocks_by_gap_risk" in inspect.getsource(m))
    wiring["wheel_engine_methods_that_call_stage_2"] = stage2_callers
    print("  wiring: deploy/cloud_run_server.py  GapDetector=%s  WheelEngine=%s  "
          "OptionsScanner=%s  engine methods called=%s" % (
              wiring["deploy/cloud_run_server.py"]["references_GapDetector"],
              wiring["deploy/cloud_run_server.py"]["constructs_WheelEngine"],
              wiring["deploy/cloud_run_server.py"]["constructs_OptionsScanner"],
              wiring["deploy/cloud_run_server.py"]["wheel_engine_methods_called"]))
    print("  wiring: WheelEngine methods that reach stage 2: %s" % stage2_callers)

    live_path_gated = any(
        v.get("references_GapDetector")
        for k, v in wiring.items()
        if "LIVE /scan path" in k or k == "deploy/cloud_run_server.py")

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "fidelity": fidelity,
        "fidelity_all_agree": all(f["verdicts_agree"] for f in fidelity),
        "wiring": wiring,
        "live_trading_path_reaches_stage_2": live_path_gated,
    }
    print("  => reconstruction agrees with live code on %d/%d days; "
          "live trading path reaches stage 2: %s"
          % (sum(1 for f in fidelity if f["verdicts_agree"]), len(fidelity),
             live_path_gated))
    _write(out, "verify.json", payload)
    return payload


# --------------------------------------------------------------------------- #
# Layer 2 — engine A/B
# --------------------------------------------------------------------------- #

def _install_rate_limit_retry(max_tries: int = 8) -> None:
    """429 backoff on the provider's data endpoints. Analysis-side only."""
    import time as _time
    from src.backtesting.data.alpaca_provider import AlpacaDataProvider

    if getattr(AlpacaDataProvider, "_fc002_retry_installed", False):
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
    AlpacaDataProvider._fc002_retry_installed = True


def _config_for_arm(name: str):
    """A fresh Config with the arm's fixed thresholds injected.

    Every `gap_risk_controls` accessor on Config is a read-only property; the
    backing dict is the only injection point. Fresh per arm, never a shared
    mutated one.
    """
    from src.utils.config import Config

    arm = ARMS[name]
    config = Config()
    g = config._config["risk"]["gap_risk_controls"]
    g["enable_gap_detection"] = bool(arm.get("enabled", True))
    # A cap of None means "this leg does not bind": push it out of reach rather
    # than branching in production code.
    g["max_historical_vol"] = arm["vol_cap"] if arm.get("vol_cap") is not None else 99.0
    g["max_gap_frequency"] = arm["freq_cap"] if arm.get("freq_cap") is not None else 99.0
    assert config.max_historical_vol == g["max_historical_vol"]
    return config


class _Stage2Patch(object):
    """Analysis-side monkeypatch implementing arms (d) and (e).

    (d) vol-relative: the fixed vol cap is disabled in config and replaced by a
        per-symbol-per-day threshold read from the layer-1 table (the symbol's
        own trailing-1y p80). Where no threshold exists (percentile still
        warming) the day falls back to the fixed 40% cap so the arm is never
        accidentally unfiltered.

    (e) graduated: the (a) block condition is evaluated; instead of returning
        unsuitable, the day is allowed and the delta bands are shifted to
        [0.10, 0.15] for that day only.

    Production is never modified: the patch is applied inside a context manager
    and removed in `finally`.
    """

    def __init__(self, arm_name: str, config, vol_thresholds: Dict[Tuple[str, str], Optional[float]]):
        self.arm = ARMS[arm_name]
        self.arm_name = arm_name
        self.config = config
        self.vol_thresholds = vol_thresholds
        self._original = None
        self.stats = {"graduated_days": 0, "percentile_blocked_days": 0,
                      "percentile_fallback_days": 0}

    def __enter__(self):
        from src.risk.gap_detector import GapDetector
        from src.utils import clock

        patch = self
        original = GapDetector.analyze_gap_risk
        self._original = original
        base_put = list(self.config.put_delta_range)
        base_call = list(self.config.call_delta_range)
        self._base_put = base_put
        self._base_call = base_call

        def wrapper(gd_self, symbol, analysis_date):
            # `Simulator` runs on a DEEP COPY of the config
            # (`restrict_symbols`), so mutating the config this harness built
            # would change an object WheelEngine never reads. The detector's own
            # `config` attribute IS the copy the engine uses — mutate that.
            live_config = gd_self.config
            result = original(gd_self, symbol, analysis_date)
            day = clock.now().date().isoformat()
            vol = result.get("historical_volatility")

            if patch.arm.get("vol_pctile") is not None:
                # Config already has the fixed vol cap disabled, so `result`
                # reflects frequency + current-gap only. Add the relative rule.
                thr = patch.vol_thresholds.get((symbol, day))
                if thr is None:
                    thr = MAX_HISTORICAL_VOL
                    patch.stats["percentile_fallback_days"] += 1
                if vol is not None and vol > thr and result.get("suitable_for_trading"):
                    patch.stats["percentile_blocked_days"] += 1
                    result["suitable_for_trading"] = False

            if patch.arm.get("graduated"):
                strategy = live_config._config["strategy"]
                if not result.get("suitable_for_trading", True):
                    result["suitable_for_trading"] = True
                    strategy["put_delta_range"] = list(GRADUATED_DELTA_RANGE)
                    strategy["call_delta_range"] = list(GRADUATED_DELTA_RANGE)
                    patch.stats["graduated_days"] += 1
                else:
                    strategy["put_delta_range"] = list(base_put)
                    strategy["call_delta_range"] = list(base_call)
            return result

        GapDetector.analyze_gap_risk = wrapper
        return self

    def __exit__(self, *exc):
        from src.risk.gap_detector import GapDetector
        GapDetector.analyze_gap_risk = self._original
        self.config._config["strategy"]["put_delta_range"] = self._base_put
        self.config._config["strategy"]["call_delta_range"] = self._base_call
        return False


def _vol_threshold_map(out: Path) -> Dict[Tuple[str, str], Optional[float]]:
    rows = _read(out, "layer1_rows.json")
    return {(r["symbol"], r["date"]): r.get("vol_p80_threshold") for r in rows}


def run_layer2(symbol: str, arm_names: Sequence[str], out: Path,
               starting_cash: float = 100_000.0) -> dict:
    from src.backtesting.evaluate import evaluate_symbol

    _install_rate_limit_retry()
    thresholds = _vol_threshold_map(out)
    sym_start = SYMBOL_START_OVERRIDES.get(symbol, WINDOW_START)

    path = out / ("layer2_%s.json" % symbol)
    results = json.loads(path.read_text()).get("arms", {}) if path.exists() else {}

    for name in arm_names:
        config = _config_for_arm(name)
        t0 = datetime.now()
        needs_patch = ARMS[name].get("vol_pctile") is not None or ARMS[name].get("graduated")
        if needs_patch:
            with _Stage2Patch(name, config, thresholds) as patch:
                report, _ = evaluate_symbol(symbol, sym_start, WINDOW_END, config=config,
                                            starting_cash=starting_cash, run_sensitivity=False)
                patch_stats = dict(patch.stats)
        else:
            report, _ = evaluate_symbol(symbol, sym_start, WINDOW_END, config=config,
                                        starting_cash=starting_cash, run_sensitivity=False)
            patch_stats = {}

        dq = report.data_quality or {}
        rejections = dq.get("blocked_days_by_reason", {}) or {}
        cycles = report.cycles
        arm = {
            "arm": name,
            "label": ARMS[name]["label"],
            "symbol": symbol,
            "start": sym_start.isoformat(),
            "end": WINDOW_END.isoformat(),
            "runtime_s": round((datetime.now() - t0).total_seconds(), 1),
            "total_return_pct": round(report.total_return * 100, 3),
            "return_on_collateral_pct": (
                None if report.return_on_collateral is None
                else round(report.return_on_collateral * 100, 3)),
            "max_drawdown_pct": round(report.max_drawdown * 100, 3),
            "option_pnl": round(report.option_pnl, 2),
            "stock_pnl": round(report.stock_pnl, 2),
            "unrealized_stock_pnl": round(report.unrealized_stock_pnl, 2),
            "puts_sold": report.puts_sold,
            "calls_sold": report.calls_sold,
            "assignments": report.assignments,
            "cycles": len(cycles),
            "days_in_position": report.days_in_position,
            "avg_collateral": round(report.avg_collateral, 2),
            "worst_cycle_pnl": round(min((c.total_pnl for c in cycles), default=0.0), 2),
            "best_cycle_pnl": round(max((c.total_pnl for c in cycles), default=0.0), 2),
            "decision_days": dq.get("decision_days"),
            "stage2_blocked_days": rejections.get(STAGE2_REJECTION_KEY, 0),
            "rejections": rejections,
            "patch_stats": patch_stats,
        }
        results[name] = arm
        print("  %-6s %-10s ret %7.2f%% | roc %8s%% | dd %6.2f%% | puts %3d calls %3d | "
              "assign %d | stage2-blocked %3d | worst cycle %9.2f | dip %4d | %.0fs"
              % (symbol, name, arm["total_return_pct"], arm["return_on_collateral_pct"],
                 arm["max_drawdown_pct"], arm["puts_sold"], arm["calls_sold"],
                 arm["assignments"], arm["stage2_blocked_days"], arm["worst_cycle_pnl"],
                 arm["days_in_position"] or 0, arm["runtime_s"]))

        payload = {"symbol": symbol, "arms": results,
                   "generated_at": datetime.now().isoformat(timespec="seconds")}
        _write(out, "layer2_%s.json" % symbol, payload)

    return {"symbol": symbol, "arms": results}


# --------------------------------------------------------------------------- #
# Black-Scholes, for the fill-implied-vol calibration and the overlay
# --------------------------------------------------------------------------- #

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_put_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return -1.0 if K > S else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) - 1.0


def implied_vol_put(price: float, S: float, K: float, T: float, r: float) -> Optional[float]:
    """Bisection on sigma. Returns None if the price is outside the model's range."""
    if price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None
    intrinsic = max(0.0, K * math.exp(-r * T) - S)
    if price < intrinsic - 1e-6:
        return None
    lo, hi = 1e-4, 5.0
    if bs_put(S, K, T, r, hi) < price:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_put(S, K, T, r, mid) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _ols(xs: Sequence[float], ys: Sequence[float]) -> Tuple[float, float, float]:
    """Simple OLS y = a + b x. Returns (a, b, r_squared)."""
    n = len(xs)
    if n < 3:
        return 0.0, 0.0, 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return my, 0.0, 0.0
    b = sxy / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 0.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot
    return a, b, r2


# --------------------------------------------------------------------------- #
# Layer 3 — real fills from BigQuery
# --------------------------------------------------------------------------- #
#
# Schema notes verified against the live table (same as FC-036 found):
#   * underlying column is `underlying`, not `underlying_symbol`
#   * sell-to-open is side='sell_short'; buy-to-close is side='buy'
#   * `net_amount` is NULL on FILL rows; `premium_total` (= price*qty*100) is
#     the populated money column
#   * FILL rows have activity_date NULL; transaction_time is UTC, so ET dates
#     need an explicit timezone or a 20:05 UTC fill lands on the wrong day
#   * assignment is OPASN (option leg) + OPTRD (the 100-share purchase)
#   * live fills begin 2025-10-06

_L3_ENTRIES_SQL = """
SELECT
  symbol                                              AS contract,
  underlying,
  DATE(transaction_time, 'America/New_York')          AS et_date,
  FORMAT_TIMESTAMP('%H:%M', transaction_time, 'America/New_York') AS et_time,
  qty, price, premium_total, strike_price,
  CAST(expiration AS STRING)                          AS expiration,
  option_type, dte_at_event
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
  SUM(IF(f.side = 'sell_short', f.qty, 0))                   AS qty_sold
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
    """Run a query through the authenticated `bq` CLI. READ ONLY."""
    import subprocess

    proc = subprocess.run(
        ["bq", "query", "--use_legacy_sql=false", "--format=json",
         "--max_rows=100000", sql],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("bq failed: %s" % proc.stderr.strip()[:2000])
    text = proc.stdout.strip()
    start = text.find("[")
    return [] if start == -1 else json.loads(text[start:])


# Vol buckets for the outcome analysis. The 0.40 edge is the live cap, so the
# ">= 0.40" bucket is by construction almost empty under the current filter —
# which is itself the finding, and why the 0.30-0.40 bucket carries the test.
VOL_BUCKETS = ((0.0, 0.20), (0.20, 0.25), (0.25, 0.30), (0.30, 0.35),
               (0.35, 0.40), (0.40, 10.0))


def _bucket(v: Optional[float]) -> Optional[str]:
    if v is None:
        return None
    for lo, hi in VOL_BUCKETS:
        if lo <= v < hi:
            return "%.2f-%.2f" % (lo, hi) if hi < 10 else ">=0.40"
    return None


def run_layer3(out: Path) -> dict:
    """The real-money layer.

    The stage-2 filter's premise is that elevated realized vol predicts bad
    outcomes. Under a LOOSENING study the usual FC-036 framing inverts: a looser
    filter cannot have blocked a trade that was actually taken, so "premium
    forgone by the arm" is zero by construction for every loosening arm. The
    decisive real-money question is therefore the premise itself, tested on the
    entries the book DID take:

      * bucket all 330 sell-to-open fills by the underlying's trailing realized
        vol at entry (the filter's own estimator, from layer 1), and report what
        each bucket actually earned;
      * check how many taken entries the reconstructed status-quo filter says
        should have been blocked (a consistency check on the reconstruction);
      * estimate the income the status quo forwent, using each symbol's OWN
        realized per-entry economics times the entries it would have taken on
        blocked days at its observed entry rate.
    """
    from src.utils.config import Config
    from src.backtesting.data.alpaca_provider import AlpacaDataProvider

    daily = _read(out, "layer1_rows.json")
    have = {r["symbol"] for r in daily}

    entries = _bq(_L3_ENTRIES_SQL)
    contracts = {c["contract"]: c for c in _bq(_L3_CONTRACT_SQL)}
    assigned = {a["contract"] for a in _bq(_L3_ASSIGN_SQL)}
    fill_symbols = sorted({e["underlying"] for e in entries})
    print("  BQ: %d sell-to-open fills, %d contracts, %d assignments, %d underlyings"
          % (len(entries), len(contracts), len(assigned), len(fill_symbols)))

    # Any fill symbol missing from layer 1 needs its own daily table so the
    # vol-at-entry is available for every entry, not just the four study names.
    missing = [s for s in fill_symbols if s not in have]
    if missing:
        print("  building daily tables for %s" % ", ".join(missing))
        for s in missing:
            daily.extend(build_daily_table(s, _load_bars(s, WARMUP_START, WINDOW_END)))
        _write(out, "layer1_rows.json", daily)

    by_day = {(r["symbol"], r["date"]): r for r in daily}
    closes: Dict[Tuple[str, str], float] = {(r["symbol"], r["date"]): r["close"] for r in daily}

    # Expiry closes, for the assignment mark and the fill-IV inversion.
    provider = AlpacaDataProvider.from_config(Config())
    bar_close: Dict[Tuple[str, str], float] = {}
    for sym in fill_symbols:
        for b in provider.get_stock_bars(sym, date(2025, 9, 1), date(2026, 8, 5)):
            bar_close[(sym, b.bar_date.isoformat())] = b.close

    entry_prem_by_contract: Dict[str, float] = defaultdict(float)
    for e in entries:
        entry_prem_by_contract[e["contract"]] += float(e["premium_total"] or 0)

    rows: List[dict] = []
    for e in entries:
        c = contracts.get(e["contract"])
        prem = float(e["premium_total"] or 0)
        denom = entry_prem_by_contract[e["contract"]]
        share = prem / denom if denom else 0.0
        option_pnl = share * (float(c["premium_in"]) - float(c["premium_out"])) if c else 0.0

        is_assigned = e["contract"] in assigned
        assign_mark = 0.0
        if is_assigned and c and c["option_type"] == "put":
            close = bar_close.get((c["underlying"], c["expiration"]))
            if close is not None:
                assign_mark = share * (close - float(c["strike"])) * 100.0 * float(c["qty_sold"] or 1)

        d = by_day.get((e["underlying"], e["et_date"]))
        spot = bar_close.get((e["underlying"], e["et_date"]))
        strike = float(e["strike_price"]) if e["strike_price"] is not None else None
        dte = int(e["dte_at_event"]) if e.get("dte_at_event") is not None else None
        iv = None
        if (e["option_type"] == "put" and spot and strike and dte and dte > 0):
            iv = implied_vol_put(float(e["price"]), spot, strike, dte / 365.0, OVERLAY_RISK_FREE)

        rows.append({
            "contract": e["contract"],
            "underlying": e["underlying"],
            "date": e["et_date"],
            "option_type": e["option_type"],
            "qty": float(e["qty"] or 0),
            "strike": strike,
            "spot_close": spot,
            "dte": dte,
            "price": float(e["price"] or 0),
            "premium": round(prem, 2),
            "option_pnl": round(option_pnl, 2),
            "assigned": is_assigned,
            "assign_mark": round(assign_mark, 2),
            "trade_pnl": round(option_pnl + assign_mark, 2),
            "collateral": round((strike or 0) * 100 * float(e["qty"] or 0), 2),
            "vol_at_entry": d["vol"] if d else None,
            "gap_freq_at_entry": d["gap_frequency"] if d else None,
            "vol_bucket": _bucket(d["vol"] if d else None),
            "implied_vol": round(iv, 4) if iv else None,
        })

    # ---- the premise test: outcome by vol-at-entry bucket -------------------
    def _summarize(sel: List[dict]) -> dict:
        n = len(sel)
        pnls = sorted(r["trade_pnl"] for r in sel)
        coll = sum(r["collateral"] for r in sel)
        return {
            "entries": n,
            "premium": round(sum(r["premium"] for r in sel), 2),
            "option_pnl": round(sum(r["option_pnl"] for r in sel), 2),
            "assign_mark": round(sum(r["assign_mark"] for r in sel), 2),
            "trade_pnl": round(sum(pnls), 2),
            "pnl_per_entry": round(sum(pnls) / n, 2) if n else 0.0,
            "premium_per_entry": round(sum(r["premium"] for r in sel) / n, 2) if n else 0.0,
            "return_on_collateral_pct": _pct(sum(pnls), coll),
            "assignments": sum(1 for r in sel if r["assigned"]),
            "assignment_rate_pct": _pct(sum(1 for r in sel if r["assigned"]), n),
            "winners": sum(1 for r in sel if r["trade_pnl"] > 0),
            "losers": sum(1 for r in sel if r["trade_pnl"] < 0),
            "win_rate_pct": _pct(sum(1 for r in sel if r["trade_pnl"] > 0), n),
            # FOUR tail metrics, not one. `worst_decile_mean` on a 35-entry
            # bucket is the mean of three trades and can be moved by a single
            # assignment; reporting it alone would let one AMD trade decide the
            # study. The rest are stated alongside so a reader can see when they
            # disagree, which they do.
            "worst_decile_mean": round(mean(pnls[:max(1, n // 10)]), 2) if n else 0.0,
            "worst5_mean": round(mean(pnls[:min(5, n)]), 2) if n else 0.0,
            "p5": round(_quantile(pnls, 0.05), 2) if n else 0.0,
            "loss_rate_pct": _pct(sum(1 for p in pnls if p < 0), n),
            "loss_sum": round(sum(p for p in pnls if p < 0), 2),
            "loss_per_entry": round(sum(p for p in pnls if p < 0) / n, 2) if n else 0.0,
            "worst_trade": round(pnls[0], 2) if n else 0.0,
        }

    buckets = {}
    for lo, hi in VOL_BUCKETS:
        key = "%.2f-%.2f" % (lo, hi) if hi < 10 else ">=0.40"
        sel = [r for r in rows if r["vol_bucket"] == key]
        if sel:
            buckets[key] = _summarize(sel)

    # High vs low split at the median vol-at-entry, so the comparison is
    # balanced by construction rather than by an arbitrary line.
    have_vol = [r for r in rows if r["vol_at_entry"] is not None]
    med = median([r["vol_at_entry"] for r in have_vol]) if have_vol else 0.0
    split = {
        "median_vol_at_entry": round(med, 4),
        "below_median": _summarize([r for r in have_vol if r["vol_at_entry"] <= med]),
        "above_median": _summarize([r for r in have_vol if r["vol_at_entry"] > med]),
        # The rule-R1 comparison the pre-registration names: the top vol decile
        # against everything else.
        "top_decile_cut": round(_quantile(sorted(r["vol_at_entry"] for r in have_vol), 0.90), 4),
    }
    cut = split["top_decile_cut"]
    split["top_decile"] = _summarize([r for r in have_vol if r["vol_at_entry"] >= cut])
    split["rest"] = _summarize([r for r in have_vol if r["vol_at_entry"] < cut])

    # ---- the filter's OWN verdict against real outcomes ---------------------
    # The most direct real-money test there is: split the entries the book
    # actually took by what each arm's rule says about that symbol-day, and
    # compare what the two halves earned. This uses the filter's exact decision
    # rule rather than a vol proxy. It is only meaningful because the filter
    # never gated the live path (see the write-up): every entry is observable
    # under both verdicts.
    by_verdict: Dict[str, dict] = {}
    for name in ARM_ORDER:
        arm = ARMS[name]
        blocked, allowed = [], []
        for r in rows:
            d = by_day.get((r["underlying"], r["date"]))
            if not d:
                continue
            b, _why = _verdict(arm, d["gap_frequency"], d["vol"],
                               d.get("vol_p80_threshold"), d["today_gap_pct"])
            (blocked if b else allowed).append(r)
        by_verdict[name] = {
            "label": ARMS[name]["label"],
            "would_block": _summarize(blocked),
            "would_allow": _summarize(allowed),
        }

    # ---- consistency: does the reconstruction agree with what live did? -----
    recon = {"taken_but_reconstruction_blocks": 0, "by_reason": defaultdict(int),
             "evaluable": 0, "no_daily_row": 0}
    for r in rows:
        d = by_day.get((r["underlying"], r["date"]))
        if not d:
            recon["no_daily_row"] += 1
            continue
        recon["evaluable"] += 1
        blocked, why = _verdict(ARMS["as_is"], d["gap_frequency"], d["vol"],
                                d.get("vol_p80_threshold"), d["today_gap_pct"])
        if blocked:
            recon["taken_but_reconstruction_blocks"] += 1
            recon["by_reason"][why] += 1
    recon["by_reason"] = dict(recon["by_reason"])

    # ---- forgone income of the status quo, in real per-entry economics ------
    live_start = min(r["date"] for r in rows)
    live_end = max(r["date"] for r in rows)
    forgone: Dict[str, dict] = {}
    for sym in fill_symbols:
        srows = [r for r in rows if r["underlying"] == sym]
        days = [r for r in by_day.values()
                if r["symbol"] == sym and live_start <= r["date"] <= live_end]
        if not days or not srows:
            continue
        blocked_days = 0
        for d in days:
            b, _ = _verdict(ARMS["as_is"], d["gap_frequency"], d["vol"],
                            d.get("vol_p80_threshold"), d["today_gap_pct"])
            blocked_days += 1 if b else 0
        allowed = len(days) - blocked_days
        entries_on_allowed = len({r["date"] for r in srows})
        rate = entries_on_allowed / float(allowed) if allowed else 0.0
        s = _summarize(srows)
        forgone[sym] = {
            "sessions": len(days),
            "blocked_days": blocked_days,
            "block_rate_pct": _pct(blocked_days, len(days)),
            "entry_days_on_allowed": entries_on_allowed,
            "entry_rate_on_allowed": round(rate, 4),
            "est_forgone_entries": round(blocked_days * rate, 1),
            "realized_pnl_per_entry": s["pnl_per_entry"],
            "realized_premium_per_entry": s["premium_per_entry"],
            "est_forgone_premium": round(blocked_days * rate * s["premium_per_entry"], 2),
            "est_forgone_pnl": round(blocked_days * rate * s["pnl_per_entry"], 2),
            # The entry rate is measured on ALLOWED days. When a symbol has no
            # allowed days at all (AMD: blocked on 201 of 201 live sessions) the
            # rate is 0/0 and the estimate collapses to $0 — which reads as "no
            # cost" when the truth is "no denominator". Flagged, never silently
            # summed. `by_arm_verdict` is the exact measurement and supersedes
            # this estimator wherever the two are compared.
            "degenerate_no_allowed_days": allowed == 0,
        }

    # ---- IV calibration for the overlay ------------------------------------
    cal_rows = [r for r in rows
                if r["implied_vol"] and r["vol_at_entry"] and r["option_type"] == "put"]
    a, b, r2 = _ols([r["vol_at_entry"] for r in cal_rows],
                    [r["implied_vol"] for r in cal_rows])
    ratios = sorted(r["implied_vol"] / r["vol_at_entry"] for r in cal_rows)
    calibration = {
        "n": len(cal_rows),
        "ols_intercept": round(a, 4),
        "ols_slope": round(b, 4),
        "r_squared": round(r2, 4),
        "median_iv_over_rv": round(_quantile(ratios, 0.5), 4),
        "mean_iv": round(mean(r["implied_vol"] for r in cal_rows), 4) if cal_rows else None,
        "mean_rv": round(mean(r["vol_at_entry"] for r in cal_rows), 4) if cal_rows else None,
    }

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "book": {
            "entries": len(rows),
            "first": live_start, "last": live_end,
            "symbols": fill_symbols,
            "premium": round(sum(r["premium"] for r in rows), 2),
            "option_pnl": round(sum(r["option_pnl"] for r in rows), 2),
            "assign_mark": round(sum(r["assign_mark"] for r in rows), 2),
            "trade_pnl": round(sum(r["trade_pnl"] for r in rows), 2),
            "assignments": sum(1 for r in rows if r["assigned"]),
            "puts": sum(1 for r in rows if r["option_type"] == "put"),
            "calls": sum(1 for r in rows if r["option_type"] == "call"),
        },
        "vol_buckets": buckets,
        "split": split,
        "by_arm_verdict": by_verdict,
        "reconstruction_consistency": recon,
        "forgone_estimate": forgone,
        "iv_calibration": calibration,
    }
    print("  book: %d entries, $%.0f premium, $%.0f net; median vol at entry %.3f"
          % (payload["book"]["entries"], payload["book"]["premium"],
             payload["book"]["trade_pnl"], split["median_vol_at_entry"]))
    _write(out, "layer3.json", payload)
    _write(out, "layer3_entries.json", rows)
    return payload


# --------------------------------------------------------------------------- #
# Overlay — synthetic short-put on every symbol-day
# --------------------------------------------------------------------------- #

def _overlay_trades(symbol, table, closes, dates, idx, ca, sym_start, iv_fn):
    """Build one synthetic short-put trade per session under one IV model."""
    trades: List[dict] = []
    skipped_ca = 0
    T = OVERLAY_DTE / 365.0
    for r in table:
        if not (sym_start <= r["date"] <= WINDOW_END.isoformat()):
            continue
        S, rv = r["close"], r["vol"]
        if S <= 0 or rv <= 0:
            continue
        iv = iv_fn(rv)
        # Strike at the target delta: solve |bs_put_delta(K)| = 0.175. Put delta
        # magnitude rises with strike, so the bisection walks the upper bound
        # down whenever the candidate is too deep.
        lo, hi = S * 0.5, S
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if -bs_put_delta(S, mid, T, OVERLAY_RISK_FREE, iv) > OVERLAY_TARGET_DELTA:
                hi = mid
            else:
                lo = mid
        K = round(0.5 * (lo + hi), 2)
        mid_price = bs_put(S, K, T, OVERLAY_RISK_FREE, iv)
        # The engine fills at mid minus a quarter of the modeled spread. The
        # spread model is uncalibrated intraday (FC-042 C3), so rather than
        # import it, take a flat 2% of mid — the same order of magnitude, and
        # deliberately on the conservative side of the wheel's income.
        fill = mid_price * (1.0 - 0.02)
        if fill <= 0.01:
            continue

        # Expiry = first session on/after entry + 7 calendar days.
        target = (date.fromisoformat(r["date"]) + timedelta(days=OVERLAY_DTE)).isoformat()
        j = idx[r["date"]] + 1
        while j < len(dates) and dates[j] < target:
            j += 1
        if j >= len(dates):
            continue
        exp_date = dates[j]
        if any(r["date"] < c <= exp_date for c in ca):
            skipped_ca += 1
            continue
        S_T = closes[exp_date]
        intrinsic = max(0.0, K - S_T)
        trades.append({
            "symbol": symbol, "date": r["date"], "expiry": exp_date,
            "spot": round(S, 2), "strike": K, "rv": rv, "iv": round(iv, 4),
            "premium": round(fill * 100, 2),
            "intrinsic": round(intrinsic * 100, 2),
            "pnl": round((fill - intrinsic) * 100.0, 2),
            "collateral": round(K * 100, 2),
            "assigned": intrinsic > 0,
            "vol": r["vol"], "gap_frequency": r["gap_frequency"],
            "vol_p80_threshold": r.get("vol_p80_threshold"),
            "today_gap_pct": r["today_gap_pct"],
        })
    return trades, skipped_ca


def run_overlay(symbols: Sequence[str], out: Path) -> dict:
    """Price a 7-DTE ~0.175-delta short put on EVERY session and score it.

    Why this layer exists: under the live filter the engine opens between 1 and
    30 positions per symbol over 2.5 years, so its arms cannot distinguish a
    good threshold from a bad one. This one prices an entry on every session, so
    "are blocked days actually worse than allowed days" is answered on thousands
    of observations.

    Premium is MODELED, not observed: implied vol comes from the layer-3 OLS fit
    of real fill IV on trailing realized vol, and the fill takes the engine's
    0.25 haircut off a modeled spread. That is the layer's weakness, so every
    comparison is also reported under +/-20% premium scaling; a conclusion that
    flips under that scaling is not reported as a conclusion.

    Outcome is the wheel's own: hold to expiry, keep the premium, take
    assignment. `pnl = premium - max(0, K - S_T) * 100`. No early close and no
    covered-call leg after assignment, which understates BOTH arms' income and
    is why the layer is read as a comparison, not a level.
    """
    rows_all = _read(out, "layer1_rows.json")
    l1 = _read(out, "layer1.json")
    cal = _read(out, "layer3.json")["iv_calibration"]
    a, b = cal["ols_intercept"], cal["ols_slope"]

    # Two IV models, because the choice is load-bearing and extrapolative. The
    # OLS fit is anchored on real fills whose mean realized vol is ~0.33; using
    # it at AMD's 0.7-0.8 extrapolates well past the data and, with a negative
    # intercept, implies an IV/RV ratio that RISES with vol (1.30 at RV 0.8 vs
    # 1.11 at the median). That would systematically flatter high-vol days —
    # exactly the days the filter blocks — so the whole blocked-vs-allowed
    # comparison is also run under a constant-ratio model, and a conclusion
    # that does not survive both is not reported as a conclusion.
    IV_MODELS = {
        "ols": lambda rv: max(0.05, a + b * rv),
        "ratio": lambda rv: max(0.05, cal["median_iv_over_rv"] * rv),
    }

    by_symbol: Dict[str, List[dict]] = defaultdict(list)
    for r in rows_all:
        by_symbol[r["symbol"]].append(r)

    per_symbol: Dict[str, dict] = {}
    trade_rows: List[dict] = []

    for symbol in symbols:
        table = sorted(by_symbol.get(symbol, []), key=lambda r: r["date"])
        closes = {r["date"]: r["close"] for r in table}
        dates = [r["date"] for r in table]
        idx = {d: i for i, d in enumerate(dates)}

        # Corporate actions make the price series discontinuous. Two guards,
        # because one is not enough: start the symbol after any split the engine
        # also refuses to span (SYMBOL_START_OVERRIDES, so dollar-per-entry is
        # comparable within a symbol and not mixing pre/post-split price
        # levels), AND drop any holding window that still contains one.
        ca = set(l1["per_symbol"].get(symbol, {}).get("corporate_actions", []))
        sym_start = SYMBOL_START_OVERRIDES.get(symbol, WINDOW_START).isoformat()
        skipped_ca = 0

        trades_by_model: Dict[str, List[dict]] = {}
        for model_name, iv_fn in IV_MODELS.items():
            trades, skipped_ca = _overlay_trades(
                symbol, table, closes, dates, idx, ca, sym_start, iv_fn)
            trades_by_model[model_name] = trades
        trades = trades_by_model["ols"]
        trade_rows.extend(trades)

        def _score(sel: List[dict], scale: float = 1.0) -> dict:
            n = len(sel)
            if not n:
                return {"n": 0}
            pnls = sorted(t["premium"] * scale - t["intrinsic"] for t in sel)
            coll = sum(t["collateral"] for t in sel)
            return {
                "n": n,
                "pnl_total": round(sum(pnls), 2),
                "pnl_per_entry": round(sum(pnls) / n, 2),
                "premium_per_entry": round(sum(t["premium"] * scale for t in sel) / n, 2),
                "return_on_collateral_pct": _pct(sum(pnls), coll),
                "assignment_rate_pct": _pct(sum(1 for t in sel if t["assigned"]), n),
                "win_rate_pct": _pct(sum(1 for p in pnls if p > 0), n),
                "worst": round(pnls[0], 2),
                "worst_decile_mean": round(mean(pnls[:max(1, n // 10)]), 2),
                "p5": round(_quantile(pnls, 0.05), 2),
            }

        def _split(sel_trades, arm):
            blocked, allowed = [], []
            for t in sel_trades:
                b_, _why = _verdict(arm, t["gap_frequency"], t["vol"],
                                    t.get("vol_p80_threshold"), t["today_gap_pct"])
                (blocked if b_ else allowed).append(t)
            return blocked, allowed

        arms_out: Dict[str, dict] = {}
        for name in ARM_ORDER:
            arm = ARMS[name]
            blocked, allowed = _split(trades, arm)
            rb, ra = _split(trades_by_model["ratio"], arm)
            entry = {
                "blocked": _score(blocked),
                "allowed": _score(allowed),
                "block_rate_pct": _pct(len(blocked), len(trades)),
                # Sign stability of "blocked days are worse than allowed days",
                # under +/-20% premium scaling AND under the alternative
                # constant-ratio IV model.
                "sensitivity": {
                    str(s): {
                        "blocked_pnl_per_entry": _score(blocked, s).get("pnl_per_entry"),
                        "allowed_pnl_per_entry": _score(allowed, s).get("pnl_per_entry"),
                    } for s in (0.8, 1.0, 1.2)
                },
                "ratio_iv_model": {
                    "blocked_pnl_per_entry": _score(rb).get("pnl_per_entry"),
                    "allowed_pnl_per_entry": _score(ra).get("pnl_per_entry"),
                    "blocked_return_on_collateral_pct": _score(rb).get("return_on_collateral_pct"),
                    "allowed_return_on_collateral_pct": _score(ra).get("return_on_collateral_pct"),
                },
            }
            arms_out[name] = entry

        per_symbol[symbol] = {
            "n_trades": len(trades),
            "window_start": sym_start,
            "skipped_corporate_action_windows": skipped_ca,
            "all": _score(trades),
            "all_ratio_model": _score(trades_by_model["ratio"]),
            "arms": arms_out,
        }
        print("  %-6s %d synthetic entries | all: $%.2f/entry, assign %.1f%% | "
              "as-is blocked %.1f%%, blocked $%.2f/entry vs allowed $%.2f/entry"
              % (symbol, len(trades), per_symbol[symbol]["all"]["pnl_per_entry"],
                 per_symbol[symbol]["all"]["assignment_rate_pct"],
                 arms_out["as_is"]["block_rate_pct"],
                 arms_out["as_is"]["blocked"].get("pnl_per_entry", 0.0),
                 arms_out["as_is"]["allowed"].get("pnl_per_entry", 0.0)))

    # Pooled across the study symbols. Pooling dollars across four names with
    # very different price levels is a rough measure, so return on collateral is
    # given alongside and is the one to read.
    pooled: Dict[str, dict] = {}
    for name in ARM_ORDER:
        b_n = sum(per_symbol[s]["arms"][name]["blocked"].get("n", 0) for s in per_symbol)
        a_n = sum(per_symbol[s]["arms"][name]["allowed"].get("n", 0) for s in per_symbol)
        b_p = sum(per_symbol[s]["arms"][name]["blocked"].get("pnl_total", 0.0) for s in per_symbol)
        a_p = sum(per_symbol[s]["arms"][name]["allowed"].get("pnl_total", 0.0) for s in per_symbol)
        pooled[name] = {
            "blocked_n": b_n, "allowed_n": a_n,
            "blocked_pnl_total": round(b_p, 2), "allowed_pnl_total": round(a_p, 2),
            "blocked_pnl_per_entry": round(b_p / b_n, 2) if b_n else None,
            "allowed_pnl_per_entry": round(a_p / a_n, 2) if a_n else None,
        }

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pooled_across_symbols": pooled,
        "parameters": {
            "dte": OVERLAY_DTE, "target_delta": OVERLAY_TARGET_DELTA,
            "risk_free": OVERLAY_RISK_FREE,
            "iv_model_primary": "iv = %.4f + %.4f * realized_vol (R2 %.3f, n=%d real fills)"
                                % (a, b, cal["r_squared"], cal["n"]),
            "iv_model_alternative": "iv = %.4f * realized_vol (median IV/RV of the same fills)"
                                    % cal["median_iv_over_rv"],
        },
        "per_symbol": per_symbol,
    }
    _write(out, "overlay.json", payload)
    _write(out, "overlay_trades.json", trade_rows)
    return payload


# --------------------------------------------------------------------------- #
# markdown
# --------------------------------------------------------------------------- #

def _fmt(v, nd=2, dash="-"):
    if v is None:
        return dash
    if isinstance(v, float):
        return ("%%.%df" % nd) % v
    return str(v)


def run_markdown(out: Path, symbols: Sequence[str]) -> None:
    l1 = _read(out, "layer1.json")
    print("\n### Layer 1 — block rate by arm\n")
    header = "| symbol | days | median vol | days vol>40% | " + \
             " | ".join(ARMS[a]["label"] for a in ARM_ORDER) + " |"
    print(header)
    print("|" + "---|" * (4 + len(ARM_ORDER)))
    for s in symbols:
        d = l1["per_symbol"].get(s)
        if not d:
            continue
        cells = ["%.1f%%" % d["arms"][a]["block_rate_pct"] for a in ARM_ORDER]
        print("| %s | %d | %.3f | %d | %s |" % (
            s, d["n_days"], d["vol"]["median"], d["vol"]["days_above_40pct"],
            " | ".join(cells)))

    print("\n### Layer 1 — what binds, under the status quo\n")
    print("| symbol | blocked days | by gap frequency | by vol cap | by current gap |")
    print("|---|---:|---:|---:|---:|")
    for s in symbols:
        d = l1["per_symbol"].get(s)
        if not d:
            continue
        r = d["arms"]["as_is"]["binding_reason_counts"]
        print("| %s | %d | %d | %d | %d |" % (
            s, d["arms"]["as_is"]["blocked_days"], r.get("gap_frequency", 0),
            r.get("volatility", 0), r.get("current_gap", 0)))

    # Layer 2
    print("\n### Layer 2 — engine arms\n")
    print("| symbol | arm | return | return on collateral | max DD | worst cycle | "
          "puts | calls | assign | days in position | stage-2 blocked | decision days |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in symbols:
        p = out / ("layer2_%s.json" % s)
        if not p.exists():
            continue
        arms = json.loads(p.read_text())["arms"]
        for name in ARM_ORDER:
            a = arms.get(name)
            if not a:
                continue
            print("| %s | %s | %.2f%% | %s%% | %.2f%% | %s | %d | %d | %d | %s | %d | %s |" % (
                s, a["label"], a["total_return_pct"], _fmt(a["return_on_collateral_pct"]),
                a["max_drawdown_pct"], _fmt(a["worst_cycle_pnl"]), a["puts_sold"],
                a["calls_sold"], a["assignments"], _fmt(a["days_in_position"], 0),
                a["stage2_blocked_days"], _fmt(a["decision_days"], 0)))

    # Layer 3
    try:
        l3 = _read(out, "layer3.json")
    except SystemExit:
        l3 = None
    if l3:
        print("\n### Layer 3 — real fills, outcome by trailing vol at entry\n")
        print("| vol at entry | entries | premium | net P&L | P&L/entry | return on collateral | "
              "assignment rate | win rate | worst-decile mean | worst-5 mean | loss $/entry |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for k, v in l3["vol_buckets"].items():
            print("| %s | %d | $%.0f | $%.0f | $%.2f | %.2f%% | %.1f%% | %.1f%% | $%.0f | $%.0f | $%.0f |" % (
                k, v["entries"], v["premium"], v["trade_pnl"], v["pnl_per_entry"],
                v["return_on_collateral_pct"], v["assignment_rate_pct"],
                v["win_rate_pct"], v["worst_decile_mean"], v["worst5_mean"],
                v["loss_per_entry"]))

        print("\n### Layer 3 — real entries split by each arm's own verdict\n")
        print("| arm | verdict | entries | premium | net P&L | P&L/entry | return on collateral | "
              "assignment rate | loss rate | loss $/entry | worst-decile mean | worst-5 mean |")
        print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for k in ARM_ORDER:
            v = l3["by_arm_verdict"].get(k)
            if not v:
                continue
            for side, lbl in (("would_block", "blocks"), ("would_allow", "allows")):
                s = v[side]
                print("| %s | %s | %d | $%.0f | $%.0f | $%.2f | %.2f%% | %.1f%% | %.1f%% | $%.0f | $%.0f | $%.0f |" % (
                    ARMS[k]["label"], lbl, s["entries"], s["premium"], s["trade_pnl"],
                    s["pnl_per_entry"], s["return_on_collateral_pct"],
                    s["assignment_rate_pct"], s["loss_rate_pct"], s["loss_per_entry"],
                    s["worst_decile_mean"], s["worst5_mean"]))

        print("\n### Layer 3 — estimated income the status quo forwent "
              "(rate-based estimator; superseded by the exact table above)\n")
        print("| symbol | sessions | blocked | block rate | entry rate on allowed days | "
              "est. forgone entries | realized $/entry | est. forgone P&L | note |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---|")
        tot = 0.0
        for s, v in sorted(l3["forgone_estimate"].items()):
            deg = v.get("degenerate_no_allowed_days")
            if not deg:
                tot += v["est_forgone_pnl"]
            print("| %s | %d | %d | %.1f%% | %.3f | %.1f | $%.2f | $%.0f | %s |" % (
                s, v["sessions"], v["blocked_days"], v["block_rate_pct"],
                v["entry_rate_on_allowed"], v["est_forgone_entries"],
                v["realized_pnl_per_entry"], v["est_forgone_pnl"],
                "**degenerate: 0 allowed days, estimate is 0/0**" if deg else ""))
        print("| **total (excl. degenerate)** | | | | | | | **$%.0f** | |" % tot)

    # Overlay
    try:
        ov = _read(out, "overlay.json")
    except SystemExit:
        ov = None
    if ov:
        print("\n### Overlay — blocked days vs allowed days, per arm\n")
        print("| symbol | arm | block rate | blocked n | blocked $/entry | allowed n | "
              "allowed $/entry | blocked assign% | allowed assign% | blocked worst-decile | "
              "allowed worst-decile | blocked $/entry (ratio IV) | allowed $/entry (ratio IV) | "
              "blocked $/entry (-20% prem) | allowed $/entry (-20% prem) |")
        print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for s in symbols:
            d = ov["per_symbol"].get(s)
            if not d:
                continue
            for name in ARM_ORDER:
                a = d["arms"][name]
                bl, al = a["blocked"], a["allowed"]
                rat, s08 = a["ratio_iv_model"], a["sensitivity"]["0.8"]
                print("| %s | %s | %.1f%% | %d | %s | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                    s, ARMS[name]["label"], a["block_rate_pct"], bl.get("n", 0),
                    _fmt(bl.get("pnl_per_entry")), al.get("n", 0),
                    _fmt(al.get("pnl_per_entry")), _fmt(bl.get("assignment_rate_pct"), 1),
                    _fmt(al.get("assignment_rate_pct"), 1),
                    _fmt(bl.get("worst_decile_mean"), 0), _fmt(al.get("worst_decile_mean"), 0),
                    _fmt(rat["blocked_pnl_per_entry"]), _fmt(rat["allowed_pnl_per_entry"]),
                    _fmt(s08["blocked_pnl_per_entry"]), _fmt(s08["allowed_pnl_per_entry"])))


# --------------------------------------------------------------------------- #

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["layer1", "verify", "layer2", "layer3",
                                       "overlay", "markdown"])
    p.add_argument("--out", type=Path, default=_default_out())
    p.add_argument("--symbol", help="layer2: one symbol per process")
    p.add_argument("--symbols", help="comma-separated override of the study set")
    p.add_argument("--arms", help="layer2: comma-separated arm names (default: all)")
    args = p.parse_args()

    symbols = tuple(args.symbols.split(",")) if args.symbols else STUDY_SYMBOLS
    out = args.out

    if args.command == "layer1":
        run_layer1(symbols, out)
    elif args.command == "verify":
        run_verify(out)
    elif args.command == "layer2":
        if not args.symbol:
            raise SystemExit("layer2 needs --symbol (one process per symbol)")
        arms = tuple(args.arms.split(",")) if args.arms else ARM_ORDER
        run_layer2(args.symbol, arms, out)
    elif args.command == "layer3":
        run_layer3(out)
    elif args.command == "overlay":
        run_overlay(symbols, out)
    elif args.command == "markdown":
        run_markdown(out, symbols)


if __name__ == "__main__":
    main()
