"""FC-018: wheel-centric dashboard data endpoints.

These endpoints power the new dashboard pages (Overview, Symbol drilldown,
Bot Health). They live behind `/api/v2/` to keep them clearly separated
from the legacy `/api/...` routes during the strangler migration. PR G
will drop the `v2` prefix and retire any legacy endpoints that are no
longer consumed.
"""

import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Header, HTTPException, Query
from typing import List, Dict, Any, Optional

import services.sweeps as sweeps
from services.bigquery import get_bigquery_service
from services.pause_alert import (
    CHECK_FAILED_MARKER,
    DEFAULT_THRESHOLD_DAYS,
    format_uncovered_alert,
    select_alertable_uncovered,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Live-bot proxy helpers (soft-fail: FC-031 endpoints degrade with a
# labeled fallback instead of 5xx when the bot is unreachable). These wrap
# the canonical handlers in routers/live.py — one copy of the response
# normalization, plus the soft-fail behavior these endpoints need.
# ----------------------------------------------------------------------

async def _live_nlv() -> Optional[float]:
    try:
        from routers.live import get_account
        account = await get_account()
        v = account.get("portfolio_value")
        return float(v) if v is not None else None
    except Exception:
        return None


async def _live_positions() -> Optional[List[Dict[str, Any]]]:
    try:
        from routers.live import get_positions
        return await get_positions()
    except Exception:
        return None


async def _bot_config() -> Dict[str, Any]:
    try:
        from routers.live import get_config
        return await get_config()
    except Exception:
        return {}


# `_drawdown_pause_threshold` was removed by FC-065 Phase 4. It read the bot's
# `call_drawdown_pause_threshold` to describe a gate the bot no longer has
# (OQ-3), against a reference price Phase 1 replaced. The config key itself is
# left alone — decommissioning dead knobs is FC-069's sweep, not this phase's.


# ----------------------------------------------------------------------
# Page 1 — Overview
# ----------------------------------------------------------------------

@router.get("/scorecard")
async def scorecard(
    days: int = Query(default=365, ge=1, le=3650, description="Lookback window"),
) -> List[Dict[str, Any]]:
    """Per-symbol scorecard for the Overview matrix.

    One row per underlying that traded within the lookback window. Includes
    cycle count, premium breakdown, realized P&L, current position state,
    and vs-buy-and-hold delta.
    """
    bq = get_bigquery_service()
    return bq.get_per_symbol_scorecard(days=days)


# ----------------------------------------------------------------------
# Page 2 — Per-symbol drilldown
# ----------------------------------------------------------------------

@router.get("/symbol/{underlying}/acb-timeline")
async def acb_timeline(
    underlying: str,
    days: int = Query(default=730, ge=1, le=3650),
) -> List[Dict[str, Any]]:
    """Adjusted-cost-basis walk for one underlying.

    Returns one row per premium / assignment / called-away event with the
    running ACB. Used by the per-symbol drilldown page's ACB chart.
    """
    if not underlying or len(underlying) > 10:
        raise HTTPException(status_code=400, detail="Invalid underlying")
    bq = get_bigquery_service()
    return bq.get_acb_timeline(symbol=underlying.upper(), days=days)


@router.get("/symbol/{underlying}/decision-quality")
async def decision_quality(
    underlying: str,
    days: int = Query(default=365, ge=1, le=3650),
) -> List[Dict[str, Any]]:
    """% of max profit captured at close, per closed trade for one symbol."""
    if not underlying or len(underlying) > 10:
        raise HTTPException(status_code=400, detail="Invalid underlying")
    bq = get_bigquery_service()
    return bq.get_decision_quality(symbol=underlying.upper(), days=days)


@router.get("/symbol/{underlying}/vs-buy-and-hold")
async def vs_buy_and_hold(underlying: str) -> Dict[str, Any]:
    """Wheel vs synthetic buy-and-hold for one underlying."""
    if not underlying or len(underlying) > 10:
        raise HTTPException(status_code=400, detail="Invalid underlying")
    bq = get_bigquery_service()
    result = bq.get_vs_buy_and_hold(symbol=underlying.upper())
    if result is None:
        raise HTTPException(status_code=404,
                            detail=f"No data for {underlying.upper()}")
    return result


@router.get("/symbol/{underlying}/phase-timing")
async def phase_timing(
    underlying: str,
    days: int = Query(default=730, ge=1, le=3650),
) -> Dict[str, Any]:
    """Days spent in each phase (cash / short put / long stock / covered)."""
    if not underlying or len(underlying) > 10:
        raise HTTPException(status_code=400, detail="Invalid underlying")
    bq = get_bigquery_service()
    return bq.get_phase_timing(symbol=underlying.upper(), days=days)


@router.get("/symbol/{underlying}/cycles")
async def symbol_cycles(
    underlying: str,
    days: int = Query(default=730, ge=1, le=3650),
) -> List[Dict[str, Any]]:
    """Completed wheel cycles for one underlying. Reads the FC-018 view directly.

    Replaces the legacy /api/history/wheel-cycles call from the per-symbol
    drilldown — that endpoint capped at 90 days and read from a stale
    legacy table.
    """
    if not underlying or len(underlying) > 10:
        raise HTTPException(status_code=400, detail="Invalid underlying")
    bq = get_bigquery_service()
    return bq.get_wheel_cycles_for_symbol(symbol=underlying.upper(), days=days)


@router.get("/symbol/{underlying}/stock-history")
async def stock_history(
    underlying: str,
    days: int = Query(default=365, ge=1, le=3650),
) -> List[Dict[str, Any]]:
    """Daily OHLC bars for one underlying."""
    if not underlying or len(underlying) > 10:
        raise HTTPException(status_code=400, detail="Invalid underlying")
    bq = get_bigquery_service()
    return bq.get_stock_history(symbol=underlying.upper(), days=days)


# ----------------------------------------------------------------------
# Page 3 — Bot Health
# ----------------------------------------------------------------------

@router.get("/bot-health/ingest")
async def bot_health_ingest() -> Dict[str, Any]:
    """Last-successful-ingest timestamps for FC-012/FC-018 ingestors.

    Returns a dict keyed by table name with ISO timestamp values. Null means
    no rows yet (or the table doesn't exist).
    """
    bq = get_bigquery_service()
    return bq.get_ingest_health()


# ----------------------------------------------------------------------
# FC-031 — portfolio returns, reconciliation, cycle/put stats, bot health
# ----------------------------------------------------------------------

@router.get("/portfolio/returns")
async def portfolio_returns() -> Dict[str, Any]:
    """XIRR / TWR / max drawdown ($ and %) from JNLC flows + equity history.

    Live NLV replaces any same-date equity row; falls back to the last
    equity row (labeled in nlv_source) when the bot proxy is unreachable.
    """
    bq = get_bigquery_service()
    nlv = await _live_nlv()
    return bq.get_portfolio_returns(current_nlv=nlv)


@router.get("/portfolio/equity-curve")
async def portfolio_equity_curve(
    days: int = Query(default=3650, ge=7, le=3650),
) -> List[Dict[str, Any]]:
    """TWR-indexed account curve vs SPY price index (base 100)."""
    bq = get_bigquery_service()
    return bq.get_equity_curve_indexed(days=days)


@router.get("/cycle-stats")
async def cycle_stats(
    days: int = Query(default=3650, ge=1, le=3650),
) -> Dict[str, Any]:
    """Closed-wheel-cycle win rate / expectancy with FC-020 exclusions
    disclosed, open-cycle MTM shown, and an FC-029 regime split."""
    bq = get_bigquery_service()
    return bq.get_wheel_cycle_stats(days=days)


@router.get("/put-stats")
async def put_stats(
    days: int = Query(default=3650, ge=1, le=3650),
) -> Dict[str, Any]:
    """Unassigned-put stats (separate from cycle stats) + held-to-expiry
    assignment rate vs the put delta band."""
    bq = get_bigquery_service()
    cfg = await _bot_config()
    return bq.get_option_trade_stats("put", days=days,
                                     delta_band=cfg.get("put_delta_range"))


@router.get("/call-stats")
async def call_stats(
    days: int = Query(default=3650, ge=1, le=3650),
) -> Dict[str, Any]:
    """Call-trade stats — the symmetric twin of /put-stats (Wheel Strategy
    Symmetry Principle): held-to-expiry CALLED-AWAY rate vs the call delta
    band, the calibration that matters most post-FC-029."""
    bq = get_bigquery_service()
    cfg = await _bot_config()
    return bq.get_option_trade_stats("call", days=days,
                                     delta_band=cfg.get("call_delta_range"))


@router.get("/reconciliation")
async def reconciliation() -> Dict[str, Any]:
    """Broker-vs-ledger reconciliation identity with residual + known gaps."""
    import asyncio
    bq = get_bigquery_service()
    nlv, positions = await asyncio.gather(_live_nlv(), _live_positions())
    return bq.get_reconciliation(current_nlv=nlv, live_positions=positions)


@router.get("/monthly-cashflow")
async def monthly_cashflow(
    months: int = Query(default=24, ge=1, le=120),
) -> List[Dict[str, Any]]:
    """Net option cash flow by month (put/call split, gross in payload)."""
    bq = get_bigquery_service()
    return bq.get_monthly_cashflow(months=months)


@router.get("/bot-health/anomalies")
async def bot_health_anomalies() -> List[Dict[str, Any]]:
    """Anomaly flags on the SPY-bar trading calendar (independent of the
    scheduler, so a totally dead scheduler still lights up)."""
    bq = get_bigquery_service()
    return bq.get_bot_anomalies()


def _alert_threshold_days() -> int:
    """Trading-day threshold for the uncovered alert.

    Read from the same ``PAUSE_ALERT_THRESHOLD_DAYS`` env var FC-030 declared
    in ``cloudbuild.yaml`` — the repoint changes the alert's data source, not
    its deployment surface, so no env or scheduler edit is required.
    """
    try:
        return int(os.getenv("PAUSE_ALERT_THRESHOLD_DAYS",
                             str(DEFAULT_THRESHOLD_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD_DAYS


async def _evaluate_uncovered_symbols() -> Dict[str, Any]:
    """Shared evaluation — one computation, two consumers (the Bot Health
    card and the daily alert check).

    FC-065 Phase 4: sourced from the bot's decision records rather than from
    an OPASN-strike price inference. The old ``call_drawdown_pause_threshold``
    percentage is no longer consulted here at all: there is no pause gate for
    it to describe, and after Phase 1 it would have been compared against the
    wrong floor anyway.
    """
    bq = get_bigquery_service()
    positions = await _live_positions()
    result = bq.get_uncovered_symbols(live_positions=positions,
                                      threshold_days=_alert_threshold_days())
    # Distinguish "no symbol is uncovered" from "we could not tell" — the
    # alert path must not read a proxy outage as all-clear.
    result["positions_available"] = positions is not None
    return result


@router.get("/bot-health/drawdown-pauses")
async def bot_health_uncovered_symbols() -> Dict[str, Any]:
    """Held symbols with no covered call written, from the bot's decision
    records (FC-065 Phase 4 — was pause inference off the OPASN strike).

    The route path is unchanged on purpose: it is the frontend's contract and
    a bookmarkable URL. The payload is the new uncovered shape, and
    ``types/v2.ts`` + ``DrawdownPauseCard.tsx`` move with it.
    """
    return await _evaluate_uncovered_symbols()


@router.post("/bot-health/pause-alert-check")
async def pause_alert_check() -> Dict[str, Any]:
    """Log an alert when a held symbol has been uncovered too long.

    Triggered daily post-close by Cloud Scheduler (route and schedule
    unchanged by FC-065 Phase 4 — only the data source moved). Emits ONE
    WARNING line carrying the marker `DRAWDOWN_PAUSE_ALERT` when any symbol
    has been uncovered >= PAUSE_ALERT_THRESHOLD_DAYS trading days; a Cloud
    Monitoring log-based policy turns that into an operator email.

    Returns the evaluation either way so the check is manually invokable.
    """
    threshold_days = _alert_threshold_days()

    try:
        result = await _evaluate_uncovered_symbols()
    except Exception as exc:  # noqa: BLE001 - must never raise past the scheduler
        # A silent evaluator is the FC-006 failure mode. Log loudly.
        logger.warning("%s evaluation raised: %s", CHECK_FAILED_MARKER, exc)
        return {"status": "degraded", "reason": str(exc),
                "threshold_days": threshold_days}

    if not result.get("positions_available"):
        logger.warning("%s live positions unavailable; uncovered state "
                       "could not be evaluated", CHECK_FAILED_MARKER)
        return {"status": "degraded", "reason": "live positions unavailable",
                "threshold_days": threshold_days}

    # The decision table is now the alert's source of truth, so its
    # unavailability is an unevaluated check — not a quiet all-clear. Reported
    # with the same marker as a positions outage; both mean "unknown".
    if not result.get("decision_source_available", True):
        logger.warning("%s decision records unavailable; uncovered state "
                       "could not be evaluated", CHECK_FAILED_MARKER)
        return {"status": "degraded", "reason": "decision records unavailable",
                "threshold_days": threshold_days}

    alertable = select_alertable_uncovered(result.get("uncovered", []),
                                           threshold_days)
    if alertable:
        logger.warning(format_uncovered_alert(alertable, threshold_days))

    # A symbol whose uncovered_days could not be derived is reported as
    # degraded, not as clear: the bot is holding shares and we cannot say
    # whether it has written a call against them, which is the state this
    # alert exists to make impossible to miss.
    unknown = result.get("unknown_uncovered_days", [])
    if unknown:
        logger.warning("%s uncovered_days underivable for %s",
                       CHECK_FAILED_MARKER,
                       ",".join(str(r.get("symbol")) for r in unknown))

    return {
        "status": "ok",
        "threshold_days": threshold_days,
        "alerted": bool(alertable),
        "alert_symbols": alertable,
        "uncovered_total": len(result.get("uncovered", [])),
        "unknown_total": len(unknown),
    }


# ----------------------------------------------------------------------
# FC-060 Layer 3 — scenario sweeps.
#
# The router is deliberately THIN. Every decision worth getting wrong —
# what the allowlist permits, what the caps are, whether a token matches,
# whether another sweep is running, what the launch body looks like, how a
# cell is classified — lives in `services/sweeps.py`, which the root test
# suite exercises directly. FastAPI is absent from the bot's CI image, so
# anything decided in this file is decided untested.
#
# THE GETs ARE PUBLIC, like every other route on this dashboard (which is
# reachable by `allUsers` — FC-094 owns that decision). Sweep results are
# hypotheticals over historical data, not the live book. Only the SUBMIT is
# gated, because a submit spends money and Job time.
# ----------------------------------------------------------------------

SWEEP_JOB_NAME = os.getenv("SWEEP_JOB_NAME", "backtest-sweep")
SWEEP_REGION = os.getenv("SWEEP_JOB_REGION", "us-central1")


def _sweep_token() -> Optional[str]:
    """The configured submit token, or None when sweeps are disabled.

    Fails CLOSED: an unset `SWEEP_SUBMIT_TOKEN` disables submission entirely
    rather than accepting anything. The secret is wired out-of-band
    (`--update-secrets`, the same recipe as GITHUB_TOKEN), so "not configured
    yet" is a state this endpoint will genuinely be in between the merge and
    the operator's step 1.
    """
    return os.getenv("SWEEP_SUBMIT_TOKEN") or None


def _project() -> str:
    return (os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
            or "gen-lang-client-0607444019")


def _service_account_email() -> str:
    """Best effort, for the 403 message only. Never used for auth."""
    return os.getenv("SWEEP_JOB_SERVICE_ACCOUNT",
                     "799970961417-compute@developer.gserviceaccount.com")


async def _launch_job(body: Dict[str, Any]) -> Dict[str, Any]:
    """POST Cloud Run v2 `jobs.run`. Returns the operation, or raises HTTPException.

    The bearer is an ADC access token (metadata server on Cloud Run, gcloud ADC
    locally) — NOT the identity token `routers/live.py` uses for
    service-to-service calls. `run.googleapis.com` is the control plane and takes
    an OAuth access token with the cloud-platform scope; an identity token is
    rejected there, and the two are easy to confuse because the same service
    account issues both.
    """
    import google.auth
    import google.auth.transport.requests
    import httpx

    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(google.auth.transport.requests.Request())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=(f"could not obtain a Google credential to launch the sweep "
                    f"Job: {exc}"))

    url = sweeps.job_run_url(_project(), SWEEP_JOB_NAME, SWEEP_REGION)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            url, json=body,
            headers={"Authorization": f"Bearer {credentials.token}"})

    if response.status_code == 403:
        # The one failure mode with a specific, actionable cause. Named in full
        # rather than passed through as "403", because the grant is console-only
        # on this project and an operator sent to the CLI will not get there.
        raise HTTPException(
            status_code=502,
            detail=sweeps.forbidden_detail(_project(), SWEEP_JOB_NAME,
                                           _service_account_email()))
    if response.status_code == 404:
        raise HTTPException(
            status_code=502,
            detail=(f"Cloud Run has no Job named '{SWEEP_JOB_NAME}' in "
                    f"{SWEEP_REGION}. It is created by cloudbuild.yaml's "
                    f"`deploy-sweep-job` step — check that the merging build "
                    f"ran it (a build lacking run.jobs.create fails that step "
                    f"loudly). Nothing was launched."))
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=(f"Cloud Run refused the launch ({response.status_code}): "
                    f"{response.text[:400]}"))
    try:
        return response.json()
    except ValueError:
        return {}


@router.get("/sweeps/allowlist")
async def sweeps_allowlist() -> Dict[str, Any]:
    """The override keys, the refusals AND THEIR REASONS, presets, and caps.

    Served from the engine's own `overrides.py` (copied into this image), so the
    form cannot offer a key the Job would refuse. The refusals are served too:
    "put_target_dte is not allowed" teaches nothing, "the cached chains store
    universe_dte=8, so the arm would silently measure something else" tells the
    operator what to do instead.

    Static — no BigQuery, no auth — so the page can render its form before any
    sweep has ever run.
    """
    return sweeps.allowlist_payload()


@router.get("/sweeps")
async def list_sweeps(
    limit: int = Query(default=25, ge=1, le=100),
) -> List[Dict[str, Any]]:
    """Recent sweeps, latest status per run, newest first.

    Each row carries `stuck: true` when it is still `submitted` past the
    container-start window (D3). That is a LABEL and nothing more — this backend
    cannot cancel an execution and does not pretend to; `execution_name` is on
    the row so an operator can go and look.
    """
    bq = get_bigquery_service()
    rows = bq.get_recent_sweeps(limit=limit)
    for row in rows:
        row["stuck"] = sweeps.is_stuck(row)
    return rows


@router.get("/sweeps/{run_id}")
async def get_sweep(run_id: str) -> Dict[str, Any]:
    """One sweep: its latest status, and — once it has any — its shaped results.

    The shaping (grid, per-scenario summary, deltas over the COMMON measured
    subset, sign agreement, the bias footer) happens server-side so the UI
    renders rather than recomputes. Three reimplementations of "which cells
    count" would be three chances to average an `insuf` cell into a ranking.
    """
    if not run_id or len(run_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid run_id")
    bq = get_bigquery_service()
    row = bq.get_sweep(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No sweep {run_id}")
    rows = bq.get_sweep_rows(run_id)
    shaped = sweeps.shape_results(row, rows)
    shaped["stuck"] = sweeps.is_stuck(row)
    shaped["status"] = row.get("status")
    shaped["run_id"] = run_id
    return shaped


@router.post("/sweeps")
async def submit_sweep(
    spec: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Validate, dedup, launch, record. In that order, and the order matters.

    * **503** when `SWEEP_SUBMIT_TOKEN` is unset — sweeps disabled, fail closed.
    * **401** when the bearer does not match (constant-time compare).
    * **422** with the runner's own reason for any spec the Job would refuse.
    * **200 + `deduplicated_to`** when this exact spec already completed on this
      engine and commit: nothing is launched (D4, goal 5 — revisit, not
      recompute).
    * **409** while another sweep is `submitted`/`running` and younger than an
      hour. One 1-vCPU Job; two executions would contend on one chain cache.
    * **502** when Cloud Run refuses the launch, carrying the grant command for
      the one cause that has a specific fix.

    The `submitted` row is written BEFORE the launch. If the launch then fails,
    the row is terminalised as `failed` with the reason — a visible failed sweep,
    never a silent one. The reverse order has the worse failure: an execution
    running with no row, which no reader can see and no dedup can find.
    """
    configured = _sweep_token()
    if not configured:
        raise HTTPException(
            status_code=503,
            detail=("sweeps are disabled: SWEEP_SUBMIT_TOKEN is not configured "
                    "on this service. Create the `sweep-submit-token` secret and "
                    "wire it with `gcloud run services update "
                    "options-wheel-dashboard --update-secrets="
                    "SWEEP_SUBMIT_TOKEN=sweep-submit-token:latest`."))
    if not sweeps.token_matches(sweeps.extract_bearer(authorization), configured):
        raise HTTPException(
            status_code=401,
            detail="a valid `Authorization: Bearer <token>` is required to submit")

    try:
        normalised = sweeps.validate_spec(spec)
    except sweeps.SweepValidationError as exc:
        # 422 carries the runner's exact reason. A generic "invalid spec" would
        # send the operator to guess which of nineteen keys was the problem.
        raise HTTPException(status_code=422, detail=str(exc))

    bq = get_bigquery_service()
    git_commit = os.getenv("GIT_COMMIT") or None
    key = sweeps.compute_sweep_key(normalised, git_commit)
    submitted_at = datetime.now(timezone.utc).isoformat()

    prior = bq.find_done_sweep(key)
    if prior is not None:
        run_id = sweeps.new_run_id()
        bq.insert_sweep_status(sweeps.submitted_row(
            run_id=run_id, spec=normalised, sweep_key_value=key,
            submitted_at=submitted_at, git_commit=git_commit,
            status=sweeps.STATUS_DEDUPLICATED,
            deduplicated_to=prior["run_id"]))
        return {"run_id": run_id, "status": sweeps.STATUS_DEDUPLICATED,
                "deduplicated_to": prior["run_id"], "sweep_key": key,
                "launched": False,
                "detail": ("this exact spec already completed on this engine and "
                           "commit; nothing was replayed")}

    blocking = sweeps.blocking_sweep(bq.get_recent_sweeps(limit=25))
    if blocking is not None:
        raise HTTPException(
            status_code=409,
            detail=(f"sweep {blocking.get('run_id')} is {blocking.get('status')}; "
                    f"one sweep runs at a time (the Job is a single vCPU and two "
                    f"executions would contend on one chain cache). Wait for it, "
                    f"or re-submit after {sweeps.RUNNING_LOCK_HOURS}h if it never "
                    f"reported in."))

    run_id = sweeps.new_run_id()
    row = sweeps.submitted_row(
        run_id=run_id, spec=normalised, sweep_key_value=key,
        submitted_at=submitted_at, git_commit=git_commit)
    bq.insert_sweep_status(row)

    try:
        operation = await _launch_job(sweeps.launch_body(
            spec_json=json.dumps(normalised, sort_keys=True),
            run_id=run_id, submitted_at=submitted_at))
    except HTTPException as exc:
        # The row already exists, so the failure has to be recorded on it —
        # otherwise the submit shows as pending forever and blocks the next one
        # for an hour for no reason.
        try:
            failed = dict(row)
            failed.update(status=sweeps.STATUS_FAILED,
                          written_at=datetime.now(timezone.utc).isoformat(),
                          finished_at=datetime.now(timezone.utc).isoformat(),
                          error=str(exc.detail)[:1000])
            bq.insert_sweep_status(failed)
        except Exception:  # noqa: BLE001 - the launch error is the one to report
            logger.exception("could not record the failed sweep launch")
        raise

    execution_name = (operation or {}).get("metadata", {}).get("name")
    if execution_name:
        # A second `submitted` row carrying the execution name. Insert-only, so
        # the reader's "latest row wins" picks this one up and an operator can
        # find the execution in the console before the Job's own `running` row
        # lands ~3-4 minutes later.
        try:
            stamped = dict(row)
            stamped.update(written_at=datetime.now(timezone.utc).isoformat(),
                           execution_name=execution_name)
            bq.insert_sweep_status(stamped)
        except Exception:  # noqa: BLE001 - cosmetic; the sweep is already running
            logger.warning("could not record execution_name for sweep %s", run_id)

    return {"run_id": run_id, "status": sweeps.STATUS_SUBMITTED,
            "sweep_key": key, "launched": True,
            "execution_name": execution_name,
            "cell_count": sweeps.cell_count(normalised),
            "deduplicated_to": None}
