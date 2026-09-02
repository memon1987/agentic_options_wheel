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
from fastapi.responses import Response
from typing import List, Dict, Any, Optional

import services.artifacts as artifacts
import services.auth as auth
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


# FC-096 Phase D — **this route is EXEMPT from the OPERATORS chain, by
# decision.** It is a POST, but its one caller is machinery, not a person: the
# `drawdown-pause-alert-daily` Cloud Scheduler job, running as the compute
# service account. Once IAP is on, that SA is admitted by
# `roles/iap.httpsResourceAccessor` and reaches this handler with a valid
# assertion whose email is the SA's — an identity that will never be on an
# OPERATORS allowlist of human accounts. Applying the uniform write gate here
# would therefore 403 the scheduler every day at 17:45 and take the alert
# silently offline: the FC-030 failure class, arriving through the door built
# to prevent it. IAP admission IS this endpoint's authorization. It writes
# nothing, spends nothing and returns an evaluation, so there is nothing behind
# it that an admitted viewer must not see.
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


def _require_sweep_token(authorization: Optional[str]) -> None:
    """The submit gate, applied verbatim to the pin writes.

    Extracted rather than repeated a third time: the 503-when-unconfigured and
    the constant-time compare are the two halves that must not drift between
    the endpoints that spend money.
    """
    configured = _sweep_token()
    if not configured:
        raise HTTPException(
            status_code=503,
            detail=("sweeps are disabled: SWEEP_SUBMIT_TOKEN is not configured "
                    "on this service. Create the `sweep-submit-token` secret "
                    "and wire it with `gcloud run services update "
                    "options-wheel-dashboard --update-secrets="
                    "SWEEP_SUBMIT_TOKEN=sweep-submit-token:latest`."))
    if not sweeps.token_matches(sweeps.extract_bearer(authorization), configured):
        raise HTTPException(
            status_code=401,
            detail="a valid `Authorization: Bearer <token>` is required to submit")


def _require_write_access(assertion: Optional[str],
                          authorization: Optional[str]):
    """FC-096 Phase D — the ONE gate every write route on this service uses.

    Three branches, and which one runs is decided entirely by whether IAP put a
    signed assertion on the request:

    * **No assertion** (every request until the operator flips IAP on) -> the
      pre-existing token gate, called with the same argument and reaching the
      same 503/401 with the same detail strings. This is the PR-1 no-op
      property, and `TestThePr1NoOpProbe` exists to prove it: deploying this
      change before the console session must not alter the service's behaviour
      by a byte.
    * **Assertion present but invalid** -> 401 with a distinct log event,
      emitted by `services/auth.py`. **Never** a fall-through to the token
      path: a forged pre-flip header or a broken `IAP_AUDIENCE` has to be loud.
    * **Assertion present and valid** -> the `OPERATORS` allowlist decides, and
      the bearer token is IGNORED. A valid non-operator gets a 403 naming the
      mechanism rather than a second chance with a token, because one leaked
      token would otherwise defeat the whole migration.

    The decisions and the messages live in `services/auth.py`, which the root
    suite exercises without FastAPI; this function is the translation into
    `HTTPException` and nothing else -- the same division of labour the sweep
    router comment above describes.

    Returns the verified `Identity` when one authorised the write, else None
    (the token path). Nothing in PR-1 reads the return value; it is there so a
    later audit trail has an identity to record without another signature
    change.
    """
    if not isinstance(assertion, str):
        # A handler called DIRECTLY — which is how this router is tested, the
        # dashboard image's deps being absent from the bot CI image — receives
        # the `Header(...)` FieldInfo object itself, not its default: only
        # FastAPI resolves that. It is truthy, so without this line an omitted
        # argument would look like a PRESENT assertion and every direct-call
        # test in the suite would take the IAP branch. Same trap the
        # `include_inactive` note below documents for `Query`. Anything that is
        # not a string is "no assertion"; a real request always yields `str` or
        # `None`.
        assertion = None
    try:
        identity = auth.authorize_write(assertion)
    except auth.IapAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    if identity is None:
        _require_sweep_token(authorization)
    return identity


def _project() -> str:
    return (os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
            or "gen-lang-client-0607444019")


def _service_account_email() -> str:
    """Best effort, for the 403 message only. Never used for auth."""
    return os.getenv("SWEEP_JOB_SERVICE_ACCOUNT",
                     "799970961417-compute@developer.gserviceaccount.com")


def _access_token() -> str:
    """An ADC OAuth access token for the Cloud Run control plane.

    NOT the identity token `routers/live.py` uses for service-to-service calls.
    `run.googleapis.com` is the control plane and takes an OAuth access token
    with the cloud-platform scope; an identity token is rejected there, and the
    two are easy to confuse because the same service account issues both.

    Synchronous on purpose — it is called through `run_in_threadpool`. Both
    `google.auth.default()` and `credentials.refresh()` do blocking I/O (the
    metadata server on Cloud Run, the filesystem locally); awaiting them inline
    in an `async def` stalls the whole event loop, so a metadata-server hiccup
    would freeze every other dashboard request, not just this one.
    """
    import google.auth
    import google.auth.transport.requests

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


async def _launch_job(body: Dict[str, Any]) -> Dict[str, Any]:
    """POST Cloud Run v2 `jobs.run`. Returns the operation, or raises HTTPException.

    **Every failure leaves here as an HTTPException**, including a transport
    error. `httpx` raises `ReadTimeout` / `ConnectError` rather than returning a
    response, and an escaping one used to surface as a 500 — with the
    `submitted` row already written and never terminalised, so the one-at-a-time
    gate stayed locked and the operator got a stack trace instead of a reason.
    The caller relies on that guarantee to record a `failed` row.
    """
    import httpx
    from starlette.concurrency import run_in_threadpool

    try:
        token = await run_in_threadpool(_access_token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=(f"could not obtain a Google credential to launch the sweep "
                    f"Job: {type(exc).__name__}: {exc}"))

    url = sweeps.job_run_url(_project(), SWEEP_JOB_NAME, SWEEP_REGION)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url, json=body,
                headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        # A timeout is genuinely ambiguous: the execution may or may not have
        # started. Said plainly rather than guessed at — and the run is recorded
        # `failed`, so the gate reopens and the operator can look at Cloud Run
        # and re-submit rather than waiting out a lock on an unknown.
        raise HTTPException(
            status_code=502,
            detail=(f"could not reach Cloud Run to launch the sweep Job "
                    f"({type(exc).__name__}: {exc}). The execution may or may "
                    f"not have started — check `gcloud run jobs executions list "
                    f"--job {SWEEP_JOB_NAME}` before re-submitting."))
    except Exception as exc:  # noqa: BLE001 - nothing may escape as a 500
        raise HTTPException(
            status_code=502,
            detail=(f"launching the sweep Job failed unexpectedly: "
                    f"{type(exc).__name__}: {exc}"))

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


def _sweep_store_missing(exc: Exception) -> bool:
    """Whether this failure is "the sweep tables do not exist yet"."""
    from google.cloud.exceptions import NotFound
    return isinstance(exc, NotFound)


def _tables_missing() -> HTTPException:
    return HTTPException(status_code=503,
                         detail=sweeps_bq_missing_detail())


def sweeps_bq_missing_detail() -> str:
    from services.bigquery import BigQueryService
    return BigQueryService.TABLES_MISSING_DETAIL


@router.get("/sweeps/allowlist")
async def sweeps_allowlist() -> Dict[str, Any]:
    """The override keys, the refusals AND THEIR REASONS, presets, and caps.

    Served from the engine's own `overrides.py` (copied into this image), so the
    form cannot offer a key the Job would refuse. The refusals are served too:
    "universe.min_open_interest is not allowed" teaches nothing, "the engine
    hardcodes open_interest to 0, so any floor rejects EVERY call and the arm
    reads as a threshold that killed the call leg" tells the operator what to do
    instead.

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
    try:
        rows = bq.get_recent_sweeps(limit=limit)
    except Exception as exc:  # noqa: BLE001
        if _sweep_store_missing(exc):
            raise _tables_missing()
        raise
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
    try:
        row = bq.get_sweep(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No sweep {run_id}")
        rows = bq.get_sweep_rows(run_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        if _sweep_store_missing(exc):
            raise _tables_missing()
        raise
    shaped = sweeps.shape_results(row, rows)
    shaped["stuck"] = sweeps.is_stuck(row)
    shaped["status"] = row.get("status")
    shaped["run_id"] = run_id
    # FC-096 Phase B. Whether every non-errored cell of this run also stored its
    # detail artifact — surfaced HERE because this is the payload the console
    # opens a cell from, and a cell that 404s needs to be explicable as "the
    # evidence was not stored" rather than as a broken link. `None` on runs that
    # wrote no artifacts at all, and on rows written before the column existed.
    shaped["artifacts_complete"] = row.get("artifacts_complete")
    return shaped


@router.get("/sweeps/{run_id}/artifacts/{scenario}/{symbol}/{split}")
def sweep_cell_artifact(run_id: str, scenario: str, symbol: str,
                        split: str) -> Response:
    """One replayed cell's detail artifact: curve, ledger, cycles, rolls, tally.

    FC-096 Phase B B2. The engine gzips one of these per non-errored cell as it
    replays; this hands it back. Phase E renders it — nothing does yet, and the
    endpoint exists now because the objects are being written now and an
    unreachable artifact is indistinguishable from an unwritten one.

    * **400** when any path segment could not address an artifact. The scenario
      is checked by the ENGINE's own `validate_scenario_name`, so a name that
      could not have been submitted cannot be requested.
    * **404** when the object is absent — which is the normal answer for an
      errored cell (it has no replay to serialise), for a run that predates this
      PR, and for a run launched from the CLI without `--persist`. The detail
      says so rather than leaving the operator to guess.
    * **503** when no artifact bucket is configured for this deployment.
    * **200** with the DECOMPRESSED JSON. See `services/artifacts.py` for why
      the gzip is not passed through.

    **A sync `def` on purpose, unlike every other route in this file.** The GCS
    client is blocking, so an `async def` would run `download_as_bytes` ON the
    event loop and one stalled read (a 30 s timeout on a degraded bucket) would
    freeze every other request the worker is serving. FastAPI dispatches a sync
    handler to its threadpool instead, which is exactly the right shape for one
    blocking I/O call. The other routes here are `async` because they await
    `httpx` or call BigQuery through a service that is already offloaded.

    Exposure: this sits on the public dashboard until IAP lands. Same class as
    the rest of `/api/v2` (FC-094 / Phase D), called out in the plan's open
    questions rather than quietly.
    """
    store = artifacts.get_artifact_store()
    if not store.enabled:
        raise HTTPException(
            status_code=503,
            detail="No artifact bucket is configured for this deployment "
                   "(SIM_ARTIFACT_BUCKET), so no detail artifacts can be served.")
    try:
        name = artifacts.object_name(run_id, scenario, symbol, split)
    except artifacts.ArtifactPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        payload = store.fetch(run_id, scenario, symbol, split)
    except HTTPException:
        raise
    except artifacts.ArtifactReadError as exc:
        # The object exists but is unusable (empty). Not a 404: serving an empty
        # 200 would put "this cell did nothing" in front of a reader whose cell
        # actually lost its evidence to a truncated write.
        logger.error("Artifact object unusable: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    except artifacts.ArtifactBucketError as exc:
        # The bucket, not the object. Named separately because GCS raises the
        # same NotFound for both and only this distinction keeps a missing IAM
        # grant from reading as "no artifacts exist" on every cell for ever.
        logger.error("Artifact bucket unreachable: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Artifact read failed for %s", name)
        raise HTTPException(
            status_code=502,
            detail=f"Could not read the artifact object: "
                   f"{type(exc).__name__}: {exc}")
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=f"No detail artifact for {scenario}/{symbol}/{split} in run "
                   f"{run_id}. Cells that errored have none, and neither do "
                   f"runs replayed before artifacts existed or from the CLI "
                   f"without --persist.")
    return Response(content=payload, media_type="application/json",
                    headers=artifacts.artifact_headers(name))


# ----------------------------------------------------------------------
# FC-096 Phase B PR-c — the interactive sim service proxy.
#
# `POST /api/v2/sims/run` forwards a spec to the private `sim-service` Cloud Run
# service and passes its answer back VERBATIM. It is a proxy, not a second API:
# every rule about what a spec may contain, what it costs, and whether the chain
# lake can cover it lives in the service, which holds a real `Config` and can
# see the lake. Duplicating any of it here would be the two-parsers failure
# FC-060 D7 exists to prevent, one layer up.
# ----------------------------------------------------------------------

SIM_SERVICE_URL = os.getenv("SIM_SERVICE_URL") or None


def _sim_identity_token(audience: str) -> str:
    """An OIDC IDENTITY token for the sim service.

    **`routers/live.py`'s pattern, NOT `_access_token`'s.** The two are easy to
    confuse because the same service account issues both, and this repo has one
    of each for good reason: `run.googleapis.com` is the Cloud Run CONTROL plane
    and takes an OAuth access token with the cloud-platform scope, while a
    private Cloud Run SERVICE authenticates its callers with an ID token whose
    `aud` is the service's own URL. An access token presented to the sim service
    is a 401; an ID token presented to the control plane is a 401. This is the
    service call, so it is the ID token.

    Synchronous on purpose — called through `run_in_threadpool`. Minting hits
    the metadata server, and awaiting that inline in an `async def` stalls the
    whole event loop rather than just this request.
    """
    import google.auth.transport.requests
    import google.oauth2.id_token

    request = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(request, audience)


@router.post("/sims/run")
async def run_sim(
    spec: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(default=None),
    x_goog_iap_jwt_assertion: Optional[str] = Header(default=None),
) -> Response:
    """Forward a spec to the sim service; return its answer unchanged.

    * **Auth: `_require_write_access`** (FC-096 Phase D). With no IAP assertion
      on the request — every request until the console flip — this is exactly
      the token gate it always was: **503** when `SWEEP_SUBMIT_TOKEN` is unset
      (submissions disabled, fail closed) and **401** when the bearer does not
      match (constant-time compare). With an assertion: **401** if it does not
      verify, **403** if the verified identity is not in `OPERATORS`, and the
      bearer token is ignored entirely.
    * **503** when `SIM_SERVICE_URL` is unset — the service is not deployed, or
      this revision predates it. Said plainly rather than guessed at: without
      the URL there is no audience to mint a token for, and a hardcoded default
      would point at a service that may not exist.
    * **502** when the token cannot be minted or the service cannot be reached.
    * otherwise **the service's own status code and body, verbatim** — 200 for a
      dedup hit, 202 for an accepted run, 409/422/503 for a refusal. Those
      refusal bodies carry the estimate, the missing symbol-days and the
      backfill command; re-wrapping them would lose the part an operator acts on.
    """
    import httpx
    from starlette.concurrency import run_in_threadpool

    _require_write_access(x_goog_iap_jwt_assertion, authorization)

    # Re-read the env rather than trusting only the import-time constant: the
    # module-level value is what production uses, and the re-read is what lets a
    # deploy that sets the variable out of band take effect without a rebuild.
    url = SIM_SERVICE_URL or os.getenv("SIM_SERVICE_URL") or None
    if not url:
        raise HTTPException(
            status_code=503,
            detail=("sim service not configured: SIM_SERVICE_URL is unset on "
                    "this revision, so there is no audience to mint an identity "
                    "token for. It is set by cloudbuild.yaml's "
                    "`deploy-dashboard-canary` step; a revision without it "
                    "predates the sim service or was deployed out of band. Use "
                    "POST /api/v2/sweeps (the batch Job) meanwhile."))
    url = url.rstrip("/")

    try:
        token = await run_in_threadpool(_sim_identity_token, url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=(f"could not obtain an identity token for the sim service: "
                    f"{type(exc).__name__}: {exc}. The dashboard's service "
                    f"account needs roles/run.invoker on `sim-service`."))

    try:
        # 150 s, comfortably above the service's own `--timeout=120`, so a
        # request the service is still working on is never abandoned by the
        # proxy first — a client timeout on a submit that in fact landed is the
        # worst outcome here, because the operator resubmits and the second one
        # 409s against the first. It is deliberately NOT above the measured
        # ~240 s cold-start tail: a request that has to wait for a cold instance
        # can still 502, and the message below says so rather than pretending a
        # bigger number would fix it. Retrying is the right move — the instance
        # the first attempt woke is warm by then.
        async with httpx.AsyncClient(timeout=150.0) as client:
            response = await client.post(
                f"{url}/simulate", json=spec,
                headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=(f"could not reach the sim service ({type(exc).__name__}: "
                    f"{exc}). It is scale-to-zero and its cold start has a "
                    f"measured tail of up to ~4 minutes, which is longer than "
                    f"this proxy's 150 s timeout — so a FIRST request after an "
                    f"idle period can legitimately land here. Retry: the "
                    f"instance that attempt woke is warm now. Otherwise use "
                    f"POST /api/v2/sweeps."))
    except Exception as exc:  # noqa: BLE001 - nothing may escape as a 500
        raise HTTPException(
            status_code=502,
            detail=(f"the sim service call failed unexpectedly: "
                    f"{type(exc).__name__}: {exc}"))

    return Response(content=response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type",
                                                    "application/json"))


# ----------------------------------------------------------------------
# FC-096 Phase B B4 — pinned scenarios.
#
# A pin is a spec the weekly battery re-measures for ever. The three routes
# below are a thin caller over `services/sweeps.py`, where every rule lives and
# where the bot CI image can actually run it.
#
# The GET is public, like every other read on this dashboard (FC-094 owns that
# decision): a pin is a hypothetical over historical data, and the same spec is
# already visible on any sweep it has produced. The WRITES are token-gated,
# because a pin spends Job time every Saturday for ever — which is a bigger
# commitment than one submit, not a smaller one.
# ----------------------------------------------------------------------


@router.get("/sims/pins")
async def list_pins(include_inactive: bool = False) -> List[Dict[str, Any]]:
    """Every pin's CURRENT state — latest row per `pin_id`, newest first.

    Active only by default: that is the set the battery runs, and it is the
    question an operator is nearly always asking. `?include_inactive=true`
    shows what has been un-pinned as well, which the store keeps for ever
    because a pin is retired by an `active=false` row rather than a delete.

    The spec comes back DECODED. A pin whose stored text will not parse is
    returned with `spec: null` and the raw text on `spec_json` rather than
    hidden — it is precisely the row worth seeing, because the battery refuses
    it every week and says so.

    A plain `bool` default rather than `Query(default=False)`: FastAPI reads it
    as a query parameter either way, and the plain default is what makes a
    direct call to this handler — which is how it is tested, the dashboard
    image's deps not being present in the bot CI image — pass a real `False`
    instead of a truthy `Query` object.
    """
    bq = get_bigquery_service()
    try:
        rows = bq.get_pins(active_only=not include_inactive)
    except Exception as exc:  # noqa: BLE001
        if _sweep_store_missing(exc):
            raise _tables_missing()
        raise
    return [sweeps.shape_pin(row) for row in rows]


@router.post("/sims/pins", status_code=201)
async def create_pin(
    body: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(default=None),
    x_goog_iap_jwt_assertion: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Pin a spec into the weekly battery, as a ROLLING window.

    Body: `{"spec": {...}, "note": "optional"}` — the same absolute-dated spec
    `POST /sweeps` takes, validated by the SAME `validate_spec`, so a pin the
    Job would refuse is refused now rather than discovered by the battery three
    Saturdays later.

    **The window the spec carries becomes a SHAPE.** `(end - start)` and
    `(end - holdout_start)` are stored as `window_days` / `holdout_days`, and
    every Saturday the battery re-anchors them to the last settled session. So
    a pin measures the same question over a window that MOVES, which is what
    makes its weekly rows a trend series rather than one answer repeated. The
    dates you post are kept verbatim in the stored spec as the record of what
    you asked for; they are not what gets replayed after the first week.

    * **Auth: `_require_write_access`** — the same chain as `POST /sweeps`.
      Pre-flip: **503** when `SWEEP_SUBMIT_TOKEN` is unset, **401** when the
      bearer does not match. Post-flip: **401** on an unverifiable IAP
      assertion, **403** when the verified identity is not in `OPERATORS`.
    * **422** with the runner's own reason for a spec that could not run, for a
      body that is not `{spec, note}`, or for a pinned `force`.
    * **409** when there are already `MAX_ACTIVE_PINS` active — naming the cap,
      because the remedy is to un-pin one and the operator needs to know which
      list to look at.
    * **409** when an ACTIVE pin already carries this exact spec — naming THAT
      pin, so "it is already pinned" is actionable rather than merely a refusal.
    * **201** with the new pin.

    Nothing here launches anything. The pin takes effect on the next battery,
    which runs after the Saturday backfill.
    """
    _require_write_access(x_goog_iap_jwt_assertion, authorization)
    try:
        spec, note = sweeps.validate_pin_body(body)
    except sweeps.SweepValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    spec_json = sweeps.pin_spec_json(spec)
    # The window the operator typed becomes a SHAPE here, and this is the whole
    # of the conversion: the spec they posted is stored verbatim as the record,
    # and these two numbers are what the battery re-anchors every Saturday.
    # Without them a pin is frozen to a historical window whose answer cannot
    # change, so the dedup hits from the second week on and the trend series
    # holds one point (FC-096 D1; the defect this PR's data review caught).
    window_days, holdout_days = sweeps.pin_window_shape(spec)
    bq = get_bigquery_service()
    try:
        existing = bq.get_pins(active_only=True)
    except Exception as exc:  # noqa: BLE001
        if _sweep_store_missing(exc):
            raise _tables_missing()
        raise

    duplicate = sweeps.duplicate_active_pin(
        spec, window_days=window_days, holdout_days=holdout_days,
        pins=existing)
    if duplicate is not None:
        # Checked BEFORE the cap: an operator re-pinning something already
        # pinned should be told that, not told the list is full — the second
        # message would send them to remove a pin to make room for one that is
        # already there.
        raise HTTPException(
            status_code=409,
            detail=(f"pin {duplicate.get('pin_id')} already asks this exact "
                    f"question"
                    + (f" (note: {duplicate.get('note')})"
                       if duplicate.get('note') else "")
                    + ". The battery would replay one and deduplicate the "
                      "other, so the second pin would cost a row and measure "
                      "nothing new. Edit or un-pin that one instead."))
    active = sweeps.active_pin_count(existing)
    if active >= sweeps.MAX_ACTIVE_PINS:
        raise HTTPException(
            status_code=409,
            detail=(f"{active} pins are already active, which is the cap of "
                    f"{sweeps.MAX_ACTIVE_PINS} (FC-096 D1). Every pin is a "
                    f"sweep the battery runs every Saturday inside one Job "
                    f"execution, so the cap is what keeps that execution inside "
                    f"its wall clock. Un-pin one first: GET "
                    f"/api/v2/sims/pins, then DELETE "
                    f"/api/v2/sims/pins/{{pin_id}}."))

    pin_id = sweeps.new_pin_id()
    row = sweeps.pin_row(pin_id=pin_id, spec_json=spec_json, active=True,
                         window_days=window_days, holdout_days=holdout_days,
                         note=note)
    try:
        bq.insert_pin(row)
    except Exception as exc:  # noqa: BLE001
        if _sweep_store_missing(exc):
            raise _tables_missing()
        raise
    return {"pin_id": pin_id, "active": True, "note": note, "spec": spec,
            # Echoed so the caller can SEE that the window became rolling — the
            # dates they posted are a record, and the two numbers are what runs.
            "window_days": window_days, "holdout_days": holdout_days,
            "active_pins": active + 1, "max_active_pins": sweeps.MAX_ACTIVE_PINS}


@router.delete("/sims/pins/{pin_id}")
async def delete_pin(
    pin_id: str,
    authorization: Optional[str] = Header(default=None),
    x_goog_iap_jwt_assertion: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Un-pin: write an `active=false` row. The history is never destroyed.

    Insert-only, like everything else in this store. "What was pinned last
    quarter, and when did it stop being measured" stays answerable, and the
    deactivation row carries the same spec so a reader taking the latest row can
    see what was retired without walking back.

    * **503 / 401 / 403** exactly as the create does — the same
      `_require_write_access` chain.
    * **404** when no pin has ever had this id — never a silent success, because
      a typo'd id that returned 200 would leave the operator believing a pin
      they are still paying for every Saturday is gone.
    * **200** with `deactivated: false` when it was ALREADY inactive. Idempotent
      and honest: nothing was written, and the answer says so rather than
      stacking identical rows on every retry.
    """
    _require_write_access(x_goog_iap_jwt_assertion, authorization)
    if not pin_id or len(pin_id) > 64:
        raise HTTPException(status_code=400, detail="Invalid pin_id")
    bq = get_bigquery_service()
    try:
        current = bq.get_pin(pin_id)
    except Exception as exc:  # noqa: BLE001
        if _sweep_store_missing(exc):
            raise _tables_missing()
        raise
    if current is None:
        raise HTTPException(
            status_code=404,
            detail=(f"no pin {pin_id}. GET /api/v2/sims/pins lists the active "
                    f"ones; add ?include_inactive=true to see retired ones."))
    if not current.get("active"):
        return {"pin_id": pin_id, "active": False, "deactivated": False,
                "detail": "this pin was already inactive; nothing was written"}
    row = sweeps.pin_row(pin_id=pin_id, spec_json=current.get("spec_json"),
                         active=False,
                         window_days=current.get("window_days"),
                         holdout_days=current.get("holdout_days"),
                         note=current.get("note"))
    try:
        bq.insert_pin(row)
    except Exception as exc:  # noqa: BLE001
        if _sweep_store_missing(exc):
            raise _tables_missing()
        raise
    return {"pin_id": pin_id, "active": False, "deactivated": True}


@router.post("/sweeps", status_code=202)
async def submit_sweep(
    spec: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(default=None),
    x_goog_iap_jwt_assertion: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Validate, gate, launch, record. In that order, and the order matters.

    * **Auth: `_require_write_access`** (FC-096 Phase D). Pre-flip: **503**
      when `SWEEP_SUBMIT_TOKEN` is unset (sweeps disabled, fail closed) and
      **401** when the bearer does not match (constant-time compare).
      Post-flip: **401** on an unverifiable IAP assertion, **403** when the
      verified identity is not in `OPERATORS`.
    * **422** with the runner's own reason for any spec the Job would refuse.
    * **409** while another sweep is live. One 1-vCPU Job; two executions would
      contend on one chain cache.
    * **502** when Cloud Run refuses the launch, carrying the grant command for
      the one cause that has a specific fix.
    * **202** on success — *accepted*, not completed: the sweep runs for minutes
      in a Job, and the caller polls `GET /api/v2/sweeps/{run_id}`.

    **THIS ENDPOINT NEVER DEDUPLICATES** (round-2 fix 1). It always launches.

    It used to short-circuit to a `deduplicated` row when `find_done_sweep`
    matched, binding the config predicate to the last `base_config_hash` any run
    on this commit had recorded. That was self-referential: after an operator
    flipped `ROLLER_ENABLED` on the Job, a re-submitted spec matched the
    *pre-flip* run's own hash, deduplicated to it, and — because nothing was
    launched — the Job's exact check, the one that would have caught the flip,
    never ran. The dedup became permanent and wrong, silently.

    The API cannot compute the Job's effective configuration, so it does not get
    to make that call. `find_done_sweep` is still consulted, purely as a HINT:
    the earlier run id is returned as `prior_done_run_id` so an operator can open
    it while the new execution starts. The **Job** deduplicates, against the
    config it is actually holding, and writes the `deduplicated` row itself. The
    cost of the change is one container start (~3-4 min) on a repeat submission,
    which is the right price for never serving one experiment's numbers as
    another's.

    The `submitted` row is written BEFORE the launch. If the launch then fails,
    the row is terminalised as `failed` with the reason — a visible failed sweep,
    never a silent one. The reverse order has the worse failure: an execution
    running with no row, which no reader can see and no dedup can find.
    """
    _require_write_access(x_goog_iap_jwt_assertion, authorization)

    try:
        normalised = sweeps.validate_spec(spec)
    except sweeps.SweepValidationError as exc:
        # 422 carries the runner's exact reason. A generic "invalid spec" would
        # send the operator to guess which of nineteen keys was the problem.
        raise HTTPException(status_code=422, detail=str(exc))

    bq = get_bigquery_service()
    git_commit = os.getenv("GIT_COMMIT") or None
    # FC-096 Phase B: the key is now half engine-identity — the content hash of
    # `src/**`, baked into this image at build time because the image ships no
    # `src/` tree to hash. Absent (an image built without the build-arg) it is
    # None, `key` is None, the hint is skipped and the `submitted` row carries a
    # NULL `sweep_key`. Submissions still launch and the Job still dedups
    # against the tree it is actually running; only the hint is lost. Keying
    # over a fallback would be worse than losing it — see
    # `sweeps.engine_identity_from_env`.
    identity = sweeps.engine_identity_from_env()
    key = sweeps.compute_sweep_key(normalised, identity) if identity else None
    submitted_at = datetime.now(timezone.utc).isoformat()
    force = bool(normalised.get("force"))

    try:
        # A HINT, never a decision — no `base_config_hash` is bound, so this
        # cannot see a kill switch flipped on the Job. `force` suppresses even
        # the hint, since the operator has already said they do not want one.
        prior = (None if (force or not key)
                 else bq.find_done_sweep(key, engine_identity=identity))
        history = bq.get_recent_sweeps(limit=25)
    except Exception as exc:  # noqa: BLE001
        if _sweep_store_missing(exc):
            raise _tables_missing()
        raise

    blocking = sweeps.blocking_sweep(history)
    if blocking is not None:
        # STATE-AWARE, because the two states expire on different clocks. One
        # message quoting the task timeout told an operator blocked by a
        # `submitted` row to wait three hours for a lock that frees in ten
        # minutes — and "wait three hours" is exactly the advice that gets a
        # feature abandoned.
        status = blocking.get("status")
        if status == sweeps.STATUS_SUBMITTED:
            release = (f"it releases at most {sweeps.STUCK_AFTER_MINUTES} minutes "
                       f"after that run's last update — a launch that has "
                       f"produced no `running` row by then is not running")
        else:
            # The BLOCKING ROW's own liveness bound, not the Job's constant
            # (FC-096 Phase B PR-c). A sim-service run stamps 900 s, so its lock
            # frees in ~25 minutes — telling that operator to wait three hours
            # is the same "advice that gets a feature abandoned" the `submitted`
            # branch above exists to avoid.
            bound = sweeps.row_liveness_seconds(blocking)
            window = (f"{bound // 3600}h" if bound >= 3600
                      else f"{bound // 60}m")
            release = (f"it releases once that run is older than its liveness "
                       f"bound ({window} + {sweeps.STALE_GRACE_MINUTES}m), "
                       f"because whatever was running it has been killed by "
                       f"then; a cold sweep can legitimately replay for that "
                       f"long")
        raise HTTPException(
            status_code=409,
            detail=(f"sweep {blocking.get('run_id')} is {status}; "
                    f"one sweep runs at a time (the Job is a single vCPU and two "
                    f"executions would contend on one chain cache). Wait for it "
                    f"— {release}."))

    run_id = sweeps.new_run_id()
    row = sweeps.submitted_row(
        run_id=run_id, spec=normalised, sweep_key_value=key,
        submitted_at=submitted_at, git_commit=git_commit,
        engine_identity=identity)
    bq.insert_sweep_status(row)

    try:
        operation = await _launch_job(sweeps.launch_body(
            spec_json=json.dumps(normalised, sort_keys=True),
            run_id=run_id, submitted_at=submitted_at))
    except HTTPException as exc:
        # The row already exists, so the failure has to be recorded on it —
        # otherwise the submit shows as pending forever and holds the
        # one-at-a-time lock until the stale cutoff for no reason. `_launch_job`
        # guarantees every failure arrives here as an HTTPException, transport
        # errors included, which is what makes this reachable at all.
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
            "forced": force,
            # A HINT for the UI: an earlier run that completed under this
            # `sweep_key`. The Job may or may not deduplicate to it — that
            # depends on the effective config, which only the Job can see — so
            # this is offered as "you may already have this answer", never as
            # "nothing was run". `deduplicated_to` stays None here on purpose:
            # if the Job dedups, ITS row carries it and the poll will show it.
            "prior_done_run_id": (prior or {}).get("run_id"),
            "deduplicated_to": None}
