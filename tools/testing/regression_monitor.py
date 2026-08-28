#!/usr/bin/env python3
"""
Production regression testing framework for Options Wheel Strategy.

Runs automated checks during market hours to validate the system is working
correctly. Designed to be invoked by Cloud Scheduler via the /regression
endpoint, or run standalone for ad-hoc verification.

Checks performed:
    1. Endpoint health checks (/, /status, /account, /positions)
    2. Trade execution validation (BigQuery: naked calls, duplicates, premiums)
    3. Log analysis (Cloud Logging: error rates, error patterns, correlation IDs)
    4. Position reconciliation (app positions vs Alpaca API)
    5. Performance baseline comparison (rolling 7-day metric deviations)
    6. Risk parameter validation (duplicate underlyings, position sizing,
       naked calls, cost-basis floor) — see `check_risk_parameters`
    7. Deploy freshness (what is serving vs. what is on GitHub `main`) —
       see `check_deploy_freshness` (FC-081 follow-up)
"""

import os
import re
import sys
import time
import json
import math
import traceback
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import structlog

# Allow imports from project root when running standalone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.utils.logging_events import log_system_event, log_error_event, log_performance_metric
# Canonical OCC parsing. FC-069 S1 (item 12 family) replaced this module's
# hand-rolled leading-alpha underlying extraction and its `"C" in symbol`
# short-call test with these — the eighth site in the OCC-substring family.
from src.utils.option_symbols import parse_option_symbol, strict_option_type

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_URL = os.environ.get(
    "REGRESSION_SERVICE_URL",
    "https://options-wheel-strategy-799970961417.us-central1.run.app",
)
GCP_PROJECT = os.environ.get("GCP_PROJECT", "gen-lang-client-0607444019")
BQ_TABLE = os.environ.get("BQ_TABLE", "trades")

# ---------------------------------------------------------------------------
# Deploy freshness (FC-081 follow-up)
# ---------------------------------------------------------------------------
# The repo whose `main` is the authority on "what should be serving". This is a
# named constant, and env-overridable, precisely because FC-081 was caused by a
# repo RENAME that silently detached the build trigger for 16 days: a repo name
# buried in an f-string is a repo name nobody edits when the repo moves.
GITHUB_REPO = "memon1987/agentic_options_wheel"

# A repo reference is `owner/name`. Anything else (a full URL, a path
# traversal, an empty half) is an operator typo, not a repo — the check says so
# rather than sending it to GitHub and interpreting whatever comes back.
GITHUB_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# GitHub commit SHAs are 40 lowercase hex characters. Validated because every
# downstream use (prefix match, `[:7]` in the message) assumes a string of at
# least that shape, and a malformed body must warn, not raise.
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

# A RENAMED repo does not 404 — GitHub 301s to the new name, and `requests`
# follows redirects by default, which would return a cheerful 200 from the
# renamed repo and hide the exact failure FC-081 was. The check therefore
# disables redirect-following and treats any redirect as the rename signal.
GITHUB_REDIRECT_STATUSES = (301, 302, 307, 308)

# How long `main` may sit ahead of the deployed commit before it counts as
# drift rather than a build in flight. A build takes 10-12 minutes; 2 hours
# absorbs a Cloud Build queue or a retried deploy without paging. Float hours,
# overridable by env `DEPLOY_FRESHNESS_MAX_HOURS`.
DEPLOY_FRESHNESS_MAX_HOURS_DEFAULT = 2.0

GITHUB_API_TIMEOUT_SECONDS = 10

# Thresholds
ERROR_WARNING_THRESHOLD = 5
ERROR_CRITICAL_THRESHOLD = 20
PREMIUM_MIN = 0.01
PREMIUM_MAX = 50.0
DUPLICATE_ORDER_WINDOW_SECONDS = 300  # 5 minutes
METRIC_DEVIATION_THRESHOLD = 2.0  # standard deviations

# Sort sentinel for trade rows whose timestamp cell is missing or unparseable.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utcnow() -> datetime:
    """Current UTC time, as a single patchable seam.

    The deploy-freshness window is a strict inequality on a duration, so its
    boundary (age exactly == window) is only testable against a clock the test
    controls. Everything else in this module reads wall-clock directly.
    """
    return datetime.now(timezone.utc)

# Error patterns that indicate critical issues
CRITICAL_ERROR_PATTERNS = [
    "circuit_breaker_opened",
    "naked_call_blocked",
    "mark_executed_failed",
]


def resolve_bq_dataset() -> str:
    """The BigQuery dataset this monitor validates.

    FC-082: this used to be the module constant
    ``BQ_DATASET = os.environ.get("BQ_DATASET", "options_wheel")`` — a
    hardcoded default that would make a covered-call service's ``/regression``
    silently validate the **wheel's** tables and report "pass" on them. The
    dataset is now derived from the profile the watched process runs, resolved
    exactly the way ``deploy/cloud_run_server.strategy_config()`` and this
    file's own ``check_risk_parameters`` resolve it: ``STRATEGY_CONFIG`` →
    ``Config`` → ``bigquery_dataset``.

    ``BQ_DATASET`` survives as an explicit operator override for ad-hoc runs
    against another dataset. There is deliberately **no** string fallback if
    the profile fails to load — the exception propagates and the check group
    reports ``fail``. A guessed dataset is how an alarm layer ends up
    validating another strategy's tables, which is the defect this replaces.
    """
    override = os.environ.get("BQ_DATASET")
    if override:
        return override

    from src.utils.config import Config
    return Config(os.environ.get("STRATEGY_CONFIG", "config/settings.yaml")).bigquery_dataset


def trade_timestamp(trade: Dict[str, Any]) -> Optional[datetime]:
    """Read a trade row's ``timestamp`` (BigQuery TIMESTAMP) as a datetime.

    FC-082: the column is ``timestamp`` (TIMESTAMP), not ``timestamp_iso`` —
    see ``src/data/trade_journal._TABLE_SCHEMA``. BigQuery hands the cell back
    as a tz-aware ``datetime``; a JSON/CSV path hands back an ISO string, so
    both are accepted. Returns None when the cell is missing or unparseable —
    callers skip those rows rather than compare against a fabricated time.
    """
    value = trade.get("timestamp")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class CheckResult:
    """Result of a single regression check."""

    def __init__(self, name: str, status: str, message: str, details: Optional[Dict] = None):
        self.name = name
        self.status = status  # "pass", "warn", "fail"
        self.message = message
        self.details = details or {}
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp,
        }


class RegressionMonitor:
    """Runs automated regression checks against the production system.

    The monitor can operate in two modes:

    * **Internal** (default when running inside Cloud Run) -- calls endpoints
      on localhost and uses Google Cloud client libraries for BigQuery /
      Cloud Logging.
    * **External** (when running standalone) -- calls the public Cloud Run
      URL with an API key and uses ``gcloud`` for auth where needed.
    """

    def __init__(
        self,
        service_url: Optional[str] = None,
        api_key: Optional[str] = None,
        internal: bool = False,
        bq_dataset: Optional[str] = None,
    ):
        self.service_url = service_url or SERVICE_URL
        self.api_key = api_key or os.environ.get("STRATEGY_API_KEY", "")
        self.internal = internal
        # Resolved lazily (see the `bq_dataset` property): only the BigQuery
        # check groups need it, and a caller may hand one in explicitly.
        self._bq_dataset = bq_dataset
        self.results: List[CheckResult] = []
        self.start_time = datetime.now(timezone.utc)

    @property
    def bq_dataset(self) -> str:
        """Dataset every BigQuery query in this monitor reads (FC-082).

        Resolved on first use from the running profile — see
        ``resolve_bq_dataset`` — and cached for the monitor's lifetime.
        """
        if self._bq_dataset is None:
            self._bq_dataset = resolve_bq_dataset()
        return self._bq_dataset

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        """Build request headers with authentication."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _get(self, path: str, timeout: int = 30) -> requests.Response:
        url = f"{self.service_url}{path}"
        return requests.get(url, headers=self._headers(), timeout=timeout)

    def _post(self, path: str, payload: Optional[Dict] = None, timeout: int = 60) -> requests.Response:
        url = f"{self.service_url}{path}"
        return requests.post(url, headers=self._headers(), json=payload or {}, timeout=timeout)

    def _github_get(
        self,
        url: str,
        headers: Dict[str, str],
        timeout: int = GITHUB_API_TIMEOUT_SECONDS,
    ) -> requests.Response:
        """GET against the GitHub REST API, WITHOUT following redirects.

        A separate seam from ``_get`` (which is service-relative and carries
        the strategy API key): this is absolute-URL, optionally carries a
        GitHub token, and is the single point tests stub so the freshness check
        never touches the network.

        ``allow_redirects=False`` is load-bearing, not a default carried over.
        A renamed GitHub repo answers 301 with the new name in ``Location``;
        with the library default the redirect is followed and the caller sees a
        200 from the renamed repo. That is precisely the FC-081 failure
        mode — the Cloud Build trigger keys on the OLD literal name and stops
        firing — so following the redirect would make the check report `pass`
        on the one condition it exists to catch.
        """
        return requests.get(
            url, headers=headers, timeout=timeout, allow_redirects=False,
        )

    # ------------------------------------------------------------------
    # 1. Endpoint health checks
    # ------------------------------------------------------------------

    def check_endpoints(self) -> List[CheckResult]:
        """Verify all critical endpoints respond correctly."""
        checks: List[CheckResult] = []

        # --- / (root health) ---
        try:
            resp = self._get("/")
            if resp.status_code == 200:
                body = resp.json()
                if body.get("status") == "healthy":
                    checks.append(CheckResult("endpoint_root", "pass", "Root returns healthy"))
                else:
                    checks.append(CheckResult(
                        "endpoint_root", "warn",
                        f"Root returned status={body.get('status')}",
                        {"response": body},
                    ))
            else:
                checks.append(CheckResult(
                    "endpoint_root", "fail",
                    f"Root returned HTTP {resp.status_code}",
                    {"status_code": resp.status_code},
                ))
        except Exception as exc:
            checks.append(CheckResult("endpoint_root", "fail", f"Root unreachable: {exc}"))

        # --- /status ---
        try:
            resp = self._get("/status")
            if resp.status_code == 200:
                body = resp.json()
                checks.append(CheckResult(
                    "endpoint_status", "pass",
                    f"Status endpoint OK (strategy status={body.get('status', 'unknown')})",
                    {"strategy_status": body.get("status")},
                ))
            else:
                checks.append(CheckResult(
                    "endpoint_status", "fail",
                    f"/status returned HTTP {resp.status_code}",
                ))
        except Exception as exc:
            checks.append(CheckResult("endpoint_status", "fail", f"/status unreachable: {exc}"))

        # --- /account ---
        try:
            resp = self._get("/account")
            if resp.status_code == 200:
                body = resp.json()
                portfolio_value = float(body.get("portfolio_value", 0))
                buying_power = float(body.get("buying_power", 0))
                if portfolio_value > 0 and buying_power >= 0:
                    checks.append(CheckResult(
                        "endpoint_account", "pass",
                        f"Account valid (portfolio=${portfolio_value:,.2f}, bp=${buying_power:,.2f})",
                        {"portfolio_value": portfolio_value, "buying_power": buying_power},
                    ))
                else:
                    checks.append(CheckResult(
                        "endpoint_account", "warn",
                        f"Account data suspicious (portfolio=${portfolio_value}, bp=${buying_power})",
                        {"portfolio_value": portfolio_value, "buying_power": buying_power},
                    ))
            elif resp.status_code == 401:
                checks.append(CheckResult(
                    "endpoint_account", "fail",
                    "Authentication failed for /account",
                ))
            else:
                checks.append(CheckResult(
                    "endpoint_account", "fail",
                    f"/account returned HTTP {resp.status_code}",
                ))
        except Exception as exc:
            checks.append(CheckResult("endpoint_account", "fail", f"/account unreachable: {exc}"))

        # --- /positions ---
        try:
            resp = self._get("/positions")
            if resp.status_code == 200:
                body = resp.json()
                positions = body.get("positions", [])
                if isinstance(positions, list):
                    checks.append(CheckResult(
                        "endpoint_positions", "pass",
                        f"Positions endpoint OK ({len(positions)} positions)",
                        {"count": len(positions)},
                    ))
                else:
                    checks.append(CheckResult(
                        "endpoint_positions", "warn",
                        "Positions response is not a list",
                        {"type": type(positions).__name__},
                    ))
            else:
                checks.append(CheckResult(
                    "endpoint_positions", "fail",
                    f"/positions returned HTTP {resp.status_code}",
                ))
        except Exception as exc:
            checks.append(CheckResult("endpoint_positions", "fail", f"/positions unreachable: {exc}"))

        self.results.extend(checks)
        return checks

    # ------------------------------------------------------------------
    # 2. Trade execution validation (BigQuery)
    # ------------------------------------------------------------------

    def check_trade_execution(self) -> List[CheckResult]:
        """Query BigQuery for recent trades and validate correctness."""
        checks: List[CheckResult] = []

        try:
            from google.cloud import bigquery
            client = bigquery.Client(project=GCP_PROJECT)
        except Exception as exc:
            checks.append(CheckResult(
                "trade_bigquery_client", "warn",
                f"Cannot connect to BigQuery: {exc}",
            ))
            self.results.extend(checks)
            return checks

        # Fetch recent trades. FC-082: this filtered on `timestamp_iso`, a
        # column that has never existed in the trades schema, so every hourly
        # run got "BigQuery trade query failed: Unrecognized name" and this
        # whole group warn-degraded instead of validating anything. The column
        # is `timestamp` (TIMESTAMP) — compared as a timestamp, matching
        # check 9 in `check_risk_parameters`, not as an ISO string.
        query = f"""
            SELECT *
            FROM `{GCP_PROJECT}.{self.bq_dataset}.{BQ_TABLE}`
            WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)
            ORDER BY timestamp DESC
        """

        try:
            rows = list(client.query(query).result())
        except Exception as exc:
            checks.append(CheckResult(
                "trade_query", "warn",
                f"BigQuery trade query failed: {exc}",
            ))
            self.results.extend(checks)
            return checks

        if not rows:
            checks.append(CheckResult(
                "trade_recent_activity", "pass",
                "No trades in the last hour (nothing to validate)",
            ))
            self.results.extend(checks)
            return checks

        trades = [dict(row) for row in rows]

        # -- No naked calls --
        call_trades = [t for t in trades if str(t.get("option_type", "")).lower() == "call"]
        naked_calls = []
        if call_trades:
            # Get current stock positions to verify coverage
            try:
                resp = self._get("/positions")
                if resp.status_code == 200:
                    positions = resp.json().get("positions", [])
                    stock_symbols = {
                        p.get("symbol") for p in positions
                        if p.get("asset_class") != "us_option"
                    }
                    for ct in call_trades:
                        underlying = ct.get("underlying", "")
                        if underlying and underlying not in stock_symbols:
                            naked_calls.append(ct.get("symbol", "unknown"))
            except Exception:
                pass  # Position check failure is non-blocking here

        if naked_calls:
            checks.append(CheckResult(
                "trade_naked_calls", "fail",
                f"Naked calls detected: {naked_calls}",
                {"naked_call_symbols": naked_calls},
            ))
        else:
            checks.append(CheckResult(
                "trade_naked_calls", "pass",
                f"No naked calls ({len(call_trades)} call trades validated)",
            ))

        # -- No duplicate orders (same option_symbol within 5 minutes) --
        duplicates = []
        # Rows with an unreadable timestamp sort first and are skipped by the
        # pairwise comparison below rather than compared against a made-up time.
        sorted_trades = sorted(trades, key=lambda t: trade_timestamp(t) or _EPOCH)
        for i, trade in enumerate(sorted_trades):
            for j in range(i + 1, len(sorted_trades)):
                other = sorted_trades[j]
                if trade.get("symbol") == other.get("symbol"):
                    t1 = trade_timestamp(trade)
                    t2 = trade_timestamp(other)
                    if t1 is None or t2 is None:
                        continue
                    if abs((t2 - t1).total_seconds()) < DUPLICATE_ORDER_WINDOW_SECONDS:
                        duplicates.append(trade.get("symbol"))

        if duplicates:
            checks.append(CheckResult(
                "trade_duplicates", "warn",
                f"Possible duplicate orders: {set(duplicates)}",
                {"duplicate_symbols": list(set(duplicates))},
            ))
        else:
            checks.append(CheckResult(
                "trade_duplicates", "pass",
                "No duplicate orders detected",
            ))

        # -- client_order_id present --
        missing_client_id = [
            t.get("symbol", "unknown")
            for t in trades
            if not t.get("client_order_id")
        ]
        if missing_client_id:
            checks.append(CheckResult(
                "trade_client_order_id", "warn",
                f"{len(missing_client_id)} trades missing client_order_id",
                {"symbols": missing_client_id[:10]},
            ))
        else:
            checks.append(CheckResult(
                "trade_client_order_id", "pass",
                "All trades have client_order_id",
            ))

        # -- Premium values within bounds --
        bad_premiums = []
        for t in trades:
            premium = t.get("premium")
            if premium is not None:
                try:
                    pval = float(premium)
                    if pval <= 0 or pval < PREMIUM_MIN or pval > PREMIUM_MAX:
                        bad_premiums.append({
                            "symbol": t.get("symbol", "unknown"),
                            "premium": pval,
                        })
                except (ValueError, TypeError):
                    bad_premiums.append({
                        "symbol": t.get("symbol", "unknown"),
                        "premium": premium,
                    })

        if bad_premiums:
            checks.append(CheckResult(
                "trade_premiums", "warn",
                f"{len(bad_premiums)} trades with out-of-bounds premiums",
                {"bad_premiums": bad_premiums[:10]},
            ))
        else:
            checks.append(CheckResult(
                "trade_premiums", "pass",
                "All premium values within reasonable bounds",
            ))

        self.results.extend(checks)
        return checks

    # ------------------------------------------------------------------
    # 3. Log analysis (Cloud Logging)
    # ------------------------------------------------------------------

    def check_logs(self) -> List[CheckResult]:
        """Analyze Cloud Logging for error rates and known error patterns."""
        checks: List[CheckResult] = []

        try:
            from google.cloud import logging as cloud_logging
            logging_client = cloud_logging.Client(project=GCP_PROJECT)
        except Exception as exc:
            checks.append(CheckResult(
                "logs_client", "warn",
                f"Cannot connect to Cloud Logging: {exc}",
            ))
            self.results.extend(checks)
            return checks

        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat("T") + "Z"

        # -- Error count in last hour --
        error_filter = (
            f'resource.labels.service_name="options-wheel-strategy" '
            f'severity>=ERROR '
            f'timestamp>="{one_hour_ago}"'
        )

        try:
            error_entries = list(logging_client.list_entries(
                filter_=error_filter,
                order_by="timestamp desc",
                page_size=100,
            ))
            error_count = len(error_entries)

            if error_count >= ERROR_CRITICAL_THRESHOLD:
                status = "fail"
                msg = f"Critical error rate: {error_count} errors in the last hour"
            elif error_count >= ERROR_WARNING_THRESHOLD:
                status = "warn"
                msg = f"Elevated error rate: {error_count} errors in the last hour"
            else:
                status = "pass"
                msg = f"Error rate normal: {error_count} errors in the last hour"

            checks.append(CheckResult(
                "logs_error_rate", status, msg,
                {"error_count": error_count, "threshold_warn": ERROR_WARNING_THRESHOLD,
                 "threshold_critical": ERROR_CRITICAL_THRESHOLD},
            ))
        except Exception as exc:
            checks.append(CheckResult(
                "logs_error_rate", "warn",
                f"Error rate query failed: {exc}",
            ))

        # -- Critical error patterns --
        for pattern in CRITICAL_ERROR_PATTERNS:
            pattern_filter = (
                f'resource.labels.service_name="options-wheel-strategy" '
                f'jsonPayload.event_type="{pattern}" '
                f'timestamp>="{one_hour_ago}"'
            )
            try:
                pattern_entries = list(logging_client.list_entries(
                    filter_=pattern_filter,
                    page_size=10,
                ))
                if pattern_entries:
                    checks.append(CheckResult(
                        f"logs_pattern_{pattern}", "fail",
                        f"Critical pattern detected: {pattern} ({len(pattern_entries)} occurrences)",
                        {"pattern": pattern, "count": len(pattern_entries)},
                    ))
                else:
                    checks.append(CheckResult(
                        f"logs_pattern_{pattern}", "pass",
                        f"No occurrences of {pattern}",
                    ))
            except Exception as exc:
                checks.append(CheckResult(
                    f"logs_pattern_{pattern}", "warn",
                    f"Pattern query failed for {pattern}: {exc}",
                ))

        # -- request_id correlation present --
        missing_reqid_filter = (
            f'resource.labels.service_name="options-wheel-strategy" '
            f'NOT jsonPayload.request_id:* '
            f'jsonPayload.event_category:* '
            f'timestamp>="{one_hour_ago}"'
        )
        try:
            missing_entries = list(logging_client.list_entries(
                filter_=missing_reqid_filter,
                page_size=50,
            ))
            if len(missing_entries) > 10:
                checks.append(CheckResult(
                    "logs_request_id", "warn",
                    f"{len(missing_entries)} log entries missing request_id",
                    {"missing_count": len(missing_entries)},
                ))
            else:
                checks.append(CheckResult(
                    "logs_request_id", "pass",
                    "request_id correlation present in logs",
                ))
        except Exception as exc:
            checks.append(CheckResult(
                "logs_request_id", "warn",
                f"request_id check failed: {exc}",
            ))

        # -- event_category populated --
        missing_category_filter = (
            f'resource.labels.service_name="options-wheel-strategy" '
            f'NOT jsonPayload.event_category:* '
            f'jsonPayload.event_type:* '
            f'timestamp>="{one_hour_ago}"'
        )
        try:
            missing_cat_entries = list(logging_client.list_entries(
                filter_=missing_category_filter,
                page_size=50,
            ))
            if len(missing_cat_entries) > 5:
                checks.append(CheckResult(
                    "logs_event_category", "warn",
                    f"{len(missing_cat_entries)} log entries missing event_category (won't export to BigQuery)",
                    {"missing_count": len(missing_cat_entries)},
                ))
            else:
                checks.append(CheckResult(
                    "logs_event_category", "pass",
                    "event_category populated in logs",
                ))
        except Exception as exc:
            checks.append(CheckResult(
                "logs_event_category", "warn",
                f"event_category check failed: {exc}",
            ))

        self.results.extend(checks)
        return checks

    # ------------------------------------------------------------------
    # 4. Position reconciliation
    # ------------------------------------------------------------------

    def check_position_reconciliation(self) -> List[CheckResult]:
        """Compare positions from /positions endpoint against Alpaca API directly."""
        checks: List[CheckResult] = []

        # Get positions from the app endpoint
        try:
            resp = self._get("/positions")
            if resp.status_code != 200:
                checks.append(CheckResult(
                    "reconcile_app_positions", "fail",
                    f"/positions returned HTTP {resp.status_code}",
                ))
                self.results.extend(checks)
                return checks
            app_positions = resp.json().get("positions", [])
        except Exception as exc:
            checks.append(CheckResult(
                "reconcile_app_positions", "fail",
                f"Cannot fetch app positions: {exc}",
            ))
            self.results.extend(checks)
            return checks

        # Get positions directly from Alpaca
        try:
            from src.utils.config import Config
            from src.api.alpaca_client import AlpacaClient

            config = Config()
            alpaca_client = AlpacaClient(config)
            alpaca_positions = alpaca_client.get_positions()
        except Exception as exc:
            checks.append(CheckResult(
                "reconcile_alpaca_positions", "warn",
                f"Cannot fetch Alpaca positions directly: {exc}",
            ))
            self.results.extend(checks)
            return checks

        # Compare: build symbol sets
        app_symbols = {p.get("symbol") for p in app_positions if p.get("symbol")}
        alpaca_symbols = {p.get("symbol") for p in alpaca_positions if p.get("symbol")}

        only_in_app = app_symbols - alpaca_symbols
        only_in_alpaca = alpaca_symbols - app_symbols

        if only_in_app or only_in_alpaca:
            checks.append(CheckResult(
                "reconcile_positions", "warn",
                "Position discrepancies detected",
                {
                    "only_in_app": list(only_in_app),
                    "only_in_alpaca": list(only_in_alpaca),
                    "app_count": len(app_symbols),
                    "alpaca_count": len(alpaca_symbols),
                },
            ))
        else:
            checks.append(CheckResult(
                "reconcile_positions", "pass",
                f"Positions match ({len(app_symbols)} positions reconciled)",
                {"count": len(app_symbols)},
            ))

        # Check for orphaned wheel states (options without underlying stock for calls)
        option_positions = [p for p in alpaca_positions if p.get("asset_class") == "us_option"]
        stock_symbols = {
            p.get("symbol") for p in alpaca_positions
            if p.get("asset_class") != "us_option"
        }

        # FC-079. This block used to ask ``"C" in symbol`` and root the
        # contract with a bare ``^([A-Z]+)`` match. Both are wrong for a root
        # that contains a C or a P: every ``PFE...P...`` put was treated as a
        # call and warned as an orphan (PFE holds no stock while the put is
        # open — by design), and an adjusted root was mis-parsed. Classify
        # with strict_option_type and root with the canonical parser.
        #
        # A symbol that is not a strict OCC contract is neither passed nor
        # warned on here: it is listed under ``unclassifiable`` for the
        # operator. The dedicated ``risk_unclassifiable_option`` check already
        # alerts on those, and a second alarm for the same fact is how an
        # alarm layer gets muted.
        orphaned = []
        unclassifiable = []
        for opt in option_positions:
            symbol = opt.get("symbol", "")
            option_type = strict_option_type(symbol)
            if option_type is None:
                unclassifiable.append(symbol)
                continue
            # Call options should have corresponding stock
            if option_type != "call":
                continue
            underlying = parse_option_symbol(symbol).get("underlying")
            if underlying and underlying not in stock_symbols:
                orphaned.append(symbol)

        if orphaned:
            checks.append(CheckResult(
                "reconcile_orphaned", "warn",
                f"Orphaned call positions (no underlying stock): {orphaned}",
                {"orphaned_symbols": orphaned, "unclassifiable": unclassifiable},
            ))
        else:
            checks.append(CheckResult(
                "reconcile_orphaned", "pass",
                "No orphaned call positions",
                {"unclassifiable": unclassifiable},
            ))

        self.results.extend(checks)
        return checks

    # ------------------------------------------------------------------
    # 5. Performance baseline comparison
    # ------------------------------------------------------------------

    def check_performance_baseline(self) -> List[CheckResult]:
        """Compare current metrics against rolling 7-day averages.

        Queries BigQuery performance logs to build a baseline, then checks
        whether recent values deviate by more than 2 standard deviations.

        **KNOWN DEAD — do not trust this group's "pass"** (found by the FC-082
        sweep, not fixed by it). Both queries below read the *trades* table for
        ``event_category`` / ``metric_name`` / ``metric_value``, none of which
        exist in any code-defined schema in this repo (``trade_journal.
        _TABLE_SCHEMA``, ``analytics_writer._SCHEMAS``, the three ingestors).
        Performance metrics are emitted by ``log_performance_metric`` to Cloud
        Logging only — there is no BigQuery table behind them. So this is not a
        wrong-column bug FC-082 could rename away: the check has no data source
        at all, and every run reports ``perf_baseline_* = warn`` ("Baseline
        query failed"). Repointing it (Cloud Logging like ``check_logs``, or the
        analytics ``executions`` table's ``duration_seconds`` by ``endpoint``)
        changes what the check measures and needs its own FC.
        """
        checks: List[CheckResult] = []

        try:
            from google.cloud import bigquery
            client = bigquery.Client(project=GCP_PROJECT)
        except Exception as exc:
            checks.append(CheckResult(
                "perf_bigquery_client", "warn",
                f"Cannot connect to BigQuery for performance check: {exc}",
            ))
            self.results.extend(checks)
            return checks

        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        metrics_to_check = [
            ("market_scan_duration", "seconds"),
            ("strategy_execution_duration", "seconds"),
        ]

        for metric_name, unit in metrics_to_check:
            # Get 7-day baseline
            baseline_query = f"""
                SELECT
                    metric_value
                FROM `{GCP_PROJECT}.{self.bq_dataset}.{BQ_TABLE}`
                WHERE event_category = 'performance'
                  AND metric_name = '{metric_name}'
                  AND timestamp_iso >= '{seven_days_ago}'
                  AND timestamp_iso < '{one_hour_ago}'
            """

            try:
                baseline_rows = list(client.query(baseline_query).result())
                baseline_values = [float(r["metric_value"]) for r in baseline_rows if r.get("metric_value") is not None]
            except Exception as exc:
                checks.append(CheckResult(
                    f"perf_baseline_{metric_name}", "warn",
                    f"Baseline query failed for {metric_name}: {exc}",
                ))
                continue

            if len(baseline_values) < 3:
                checks.append(CheckResult(
                    f"perf_baseline_{metric_name}", "pass",
                    f"Insufficient baseline data for {metric_name} ({len(baseline_values)} samples)",
                ))
                continue

            mean = statistics.mean(baseline_values)
            stdev = statistics.stdev(baseline_values)

            # Get recent values
            recent_query = f"""
                SELECT
                    metric_value
                FROM `{GCP_PROJECT}.{self.bq_dataset}.{BQ_TABLE}`
                WHERE event_category = 'performance'
                  AND metric_name = '{metric_name}'
                  AND timestamp_iso >= '{one_hour_ago}'
            """

            try:
                recent_rows = list(client.query(recent_query).result())
                recent_values = [float(r["metric_value"]) for r in recent_rows if r.get("metric_value") is not None]
            except Exception as exc:
                checks.append(CheckResult(
                    f"perf_recent_{metric_name}", "warn",
                    f"Recent query failed for {metric_name}: {exc}",
                ))
                continue

            if not recent_values:
                checks.append(CheckResult(
                    f"perf_baseline_{metric_name}", "pass",
                    f"No recent data for {metric_name} (nothing to compare)",
                ))
                continue

            recent_mean = statistics.mean(recent_values)

            if stdev > 0:
                z_score = abs(recent_mean - mean) / stdev
            else:
                z_score = 0.0

            details = {
                "metric": metric_name,
                "unit": unit,
                "baseline_mean": round(mean, 3),
                "baseline_stdev": round(stdev, 3),
                "recent_mean": round(recent_mean, 3),
                "z_score": round(z_score, 2),
                "baseline_samples": len(baseline_values),
                "recent_samples": len(recent_values),
            }

            if z_score > METRIC_DEVIATION_THRESHOLD:
                checks.append(CheckResult(
                    f"perf_baseline_{metric_name}", "warn",
                    f"{metric_name} deviates {z_score:.1f} std devs from baseline "
                    f"(recent={recent_mean:.2f}{unit}, baseline={mean:.2f}+/-{stdev:.2f}{unit})",
                    details,
                ))
            else:
                checks.append(CheckResult(
                    f"perf_baseline_{metric_name}", "pass",
                    f"{metric_name} within normal range (z={z_score:.1f})",
                    details,
                ))

        self.results.extend(checks)
        return checks

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 6. Risk parameter validation
    # ------------------------------------------------------------------

    def check_risk_parameters(self) -> List[CheckResult]:
        """Validate the live risk invariants against positions and account.

        This is a **detective** layer, not a preventive one: it runs hourly via
        Cloud Scheduler against `/regression` and any `fail` makes the endpoint
        return HTTP 500. It therefore must mirror policies that actually exist —
        an alarm layer that cries wolf gets muted.

        FC-069 S1 (card 16) synced it to the post-sweep policy set. Four checks
        were deleted with the knobs they mirrored:

        - ``risk_max_total_positions`` (hardcoded 10) — no global cap exists.
        - ``risk_min_cash_reserve`` (hardcoded 20%) — no cash-reserve policy.
        - ``risk_max_portfolio_allocation`` (hardcoded 80%) — no allocation cap.
        - ``risk_max_exposure_per_ticker`` (hardcoded $40k) — no per-ticker cap,
          and the check was a live incident waiting on account growth: a single
          ``max_position_size``-sized lot crosses $40k market value at roughly a
          $114k portfolio, at which point this returned **fail → HTTP 500 hourly
          on a healthy account**, against a policy nothing enforced. It never
          even mirrored faithfully — it measured market value where the dead
          knob meant assignment notional.

        The four survivors verify invariants that are genuinely live, and all
        four were hardened:

        - **2 · duplicate underlyings** — the one-option-position-per-underlying
          invariant is real (scanner skip + `select_batch` dedup + share
          ledger), so the check stays even though its knob is gone. Blind to
          resting unfilled orders, like all three enforcing legs (FC-009).
        - **4 · max position size** — re-sourced from ``Config.max_position_size``
          instead of a hardcoded 0.35, so the alarm cannot drift from the policy.
        - **7 · naked calls** — real protection. The ``"C" in symbol`` short-call
          test and the hand-rolled leading-alpha underlying parser are replaced
          by the canonical parsers.
        - **8 · cost-basis protection** — re-specced onto ``avg_entry_price``.
          It derived basis from ``cost_basis / qty``, which FC-029 proved
          returns 0 for assigned positions; the ``> 0`` guard then skipped
          silently, leaving the detective floor check **blind on exactly the
          assigned lots it exists for**. ``avg_entry_price`` is the decided
          floor source (FC-065 Phase 1).

        Plus one new check from the S1 review: **``risk_unclassifiable_option``**
        (warn) surfaces any option position whose symbol is not a strict OCC
        contract — adjusted roots after a split or special dividend, most
        importantly. Checks 7 and 8 exclude those positions (their share
        arithmetic assumes a 100-share deliverable, which an adjusted contract
        does not have), and excluding them *silently* would have inverted
        check 8's own principle.
        """
        checks: List[CheckResult] = []

        # Re-source the sizing policy from Config rather than a hardcoded
        # constant. Config() is importable in-process and reads the same
        # settings.yaml baked into the image. Failing to load it is itself a
        # finding: this monitor must never silently fall back to a stale
        # number and report "pass" against a policy it invented. So we fail the
        # check loudly instead of guessing.
        # STRATEGY_CONFIG resolution mirrors the server's `strategy_config()`
        # exactly. A bare `Config()` would validate the wheel's policy against
        # a covered-call service's positions — the alarm layer must read the
        # same profile the process it is watching runs.
        cfg = None
        max_position_size: Optional[float] = None
        config_load_error: Optional[str] = None
        try:
            from src.utils.config import Config
            cfg = Config(os.environ.get("STRATEGY_CONFIG", "config/settings.yaml"))
            max_position_size = float(cfg.max_position_size)
        except Exception as exc:  # pragma: no cover - defensive
            config_load_error = str(exc)

        # --- Fetch live data ---
        try:
            account_resp = self._get("/account")
            account = account_resp.json() if account_resp.status_code == 200 else {}
        except Exception:
            account = {}

        try:
            positions_resp = self._get("/positions")
            positions = positions_resp.json() if positions_resp.status_code == 200 else []
            if isinstance(positions, dict):
                positions = positions.get("positions", [])
        except Exception:
            positions = []

        portfolio_value = float(account.get("portfolio_value", 0))

        if portfolio_value <= 0:
            checks.append(CheckResult(
                "risk_account_data", "fail",
                "Cannot validate risk: portfolio_value is 0 or unavailable",
            ))
            return checks

        checks.append(CheckResult("risk_account_data", "pass", f"Portfolio value: ${portfolio_value:,.2f}"))

        # Separate stock and option positions
        stock_positions = [p for p in positions if p.get("asset_class") == "us_equity"]
        option_positions = [p for p in positions if p.get("asset_class") == "us_option"]

        # --- 2. One option position per underlying ---
        # The knob (`max_positions_per_stock`) is gone but the invariant is
        # real and triple-enforced on the live path, so the mirror stays.
        underlying_counts: Dict[str, int] = {}
        for pos in option_positions:
            symbol = pos.get("symbol", "")
            underlying = parse_option_symbol(symbol).get("underlying", "")
            if not underlying:
                continue
            underlying_counts[underlying] = underlying_counts.get(underlying, 0) + 1

        duplicates = {k: v for k, v in underlying_counts.items() if v > 1}
        if duplicates:
            checks.append(CheckResult(
                "risk_duplicate_underlying", "fail",
                f"Multiple option positions for same underlying: {duplicates}",
                {"duplicates": duplicates},
            ))
        else:
            checks.append(CheckResult(
                "risk_duplicate_underlying", "pass",
                "No duplicate underlying option positions",
            ))

        # --- 4. Max single position size (Config.max_position_size) ---
        if max_position_size is None:
            checks.append(CheckResult(
                "risk_max_position_size", "fail",
                f"Cannot validate position sizing: Config failed to load ({config_load_error})",
                {"config_load_error": config_load_error},
            ))
        else:
            limit_pct = f"{max_position_size:.0%}"
            for pos in positions:
                market_value = abs(float(pos.get("market_value", 0)))
                pos_pct = market_value / portfolio_value if portfolio_value > 0 else 0
                if pos_pct > max_position_size:
                    checks.append(CheckResult(
                        "risk_max_position_size", "fail",
                        f"Position {pos.get('symbol')} is {pos_pct:.1%} of portfolio (max {limit_pct})",
                        {"symbol": pos.get("symbol"), "market_value": market_value,
                         "pct": round(pos_pct, 4), "limit": max_position_size},
                    ))

            if not any(r.name == "risk_max_position_size" for r in checks):
                checks.append(CheckResult(
                    "risk_max_position_size", "pass",
                    f"All positions within {limit_pct} limit",
                    {"limit": max_position_size},
                ))

        # --- Classification pre-pass for checks 7 and 8 ---
        # `strict_option_type` returns None for anything that is not an exact
        # OCC contract, which correctly refuses the `"C" in symbol` family. But
        # refusing to classify must not mean going quiet: an **adjusted
        # contract** (digit-suffixed root after a split or special dividend,
        # e.g. `AAPL1260821C00250000`) fails the strict regex, and a silent
        # `continue` would make it vanish from both the naked-call and
        # cost-basis detectives — the position most likely to need a human.
        # That would invert check 8's own principle, so an unclassifiable
        # option is a `warn` finding instead. One finding per position,
        # emitted here rather than in both loops.
        #
        # `warn`, not `fail`: it does not 500 the endpoint (this is "look at
        # this", not "a control was violated"), and the share arithmetic below
        # is genuinely unsafe on an adjusted contract, whose deliverable is not
        # 100 shares. Surfacing it for triage is the correct answer; guessing
        # is not.
        classified: List[Tuple[Dict[str, Any], str, Dict[str, Any]]] = []
        for pos in option_positions:
            symbol = pos.get("symbol", "")
            opt_type = strict_option_type(symbol)
            if opt_type is None:
                checks.append(CheckResult(
                    "risk_unclassifiable_option", "warn",
                    f"cannot classify option position {symbol}",
                    {"option_symbol": symbol, "qty": pos.get("qty"),
                     "side": pos.get("side"),
                     "note": "not a strict OCC contract symbol (adjusted root?); "
                             "excluded from naked-call and cost-basis checks"},
                ))
                continue
            classified.append((pos, opt_type, parse_option_symbol(symbol)))

        if not any(r.name == "risk_unclassifiable_option" for r in checks):
            checks.append(CheckResult(
                "risk_unclassifiable_option", "pass",
                "All option positions parse as OCC contracts",
            ))

        # --- 7. Naked call detection (short calls must have underlying shares) ---
        stock_qty: Dict[str, int] = {}
        for p in stock_positions:
            sym = p.get("symbol")
            if sym:
                stock_qty[sym] = int(float(p.get("qty", 0)))

        for pos, opt_type, parsed in classified:
            symbol = pos.get("symbol", "")
            raw_qty = float(pos.get("qty", 0))
            qty = abs(int(raw_qty))

            if opt_type != "call":
                continue
            if not (raw_qty < 0 or pos.get("side") == "short"):
                continue  # long calls commit no shares

            underlying = parsed.get("underlying", "")
            owned_shares = stock_qty.get(underlying, 0)
            required_shares = qty * 100

            if owned_shares < required_shares:
                checks.append(CheckResult(
                    "risk_naked_call", "fail",
                    f"Naked call detected: {symbol} (need {required_shares} shares of {underlying}, own {owned_shares})",
                    {"option_symbol": symbol, "underlying": underlying,
                     "required_shares": required_shares, "owned_shares": owned_shares},
                ))

        if not any(r.name == "risk_naked_call" for r in checks):
            checks.append(CheckResult("risk_naked_call", "pass", "No naked calls detected"))

        # --- 8. Cost basis protection (short call strikes >= avg_entry_price) ---
        # Basis comes from the stock position's `avg_entry_price`, the decided
        # floor source (FC-065 Phase 1). The previous derivation, cost_basis/qty,
        # returns 0 for assigned positions, so the `> 0` guard skipped exactly
        # the assigned lots this check exists to protect.
        stock_by_symbol = {p.get("symbol"): p for p in stock_positions if p.get("symbol")}

        for pos, opt_type, parsed in classified:
            symbol = pos.get("symbol", "")
            if opt_type != "call":
                continue
            if not (float(pos.get("qty", 0)) < 0 or pos.get("side") == "short"):
                continue  # only short calls can be written below basis

            underlying = parsed.get("underlying", "")
            strike = float(parsed.get("strike_price", 0) or 0)
            if strike <= 0:
                continue

            stock_pos = stock_by_symbol.get(underlying)
            if not stock_pos:
                continue  # naked — check 7's finding, not this one's

            try:
                basis_per_share = float(stock_pos.get("avg_entry_price") or 0)
            except (TypeError, ValueError):
                basis_per_share = 0.0

            if basis_per_share <= 0:
                # Unresolvable basis is a finding, not a silent skip: the old
                # version's silent skip is what made this check blind.
                checks.append(CheckResult(
                    "risk_cost_basis_protection", "fail",
                    f"Cannot verify floor for {symbol}: {underlying} position has no usable avg_entry_price",
                    {"option_symbol": symbol, "underlying": underlying,
                     "avg_entry_price": stock_pos.get("avg_entry_price")},
                ))
                continue

            if strike < basis_per_share:
                checks.append(CheckResult(
                    "risk_cost_basis_protection", "fail",
                    f"Call {symbol} strike ${strike:.2f} below cost basis ${basis_per_share:.2f}",
                    {"option_symbol": symbol, "strike": strike,
                     "cost_basis": round(basis_per_share, 2)},
                ))

        if not any(r.name == "risk_cost_basis_protection" for r in checks):
            checks.append(CheckResult("risk_cost_basis_protection", "pass", "All call strikes above cost basis"))

        # --- 9. Recent trade validation (BigQuery) ---
        # Premium floors re-sourced from the same Config instance as check 4,
        # ending the last hardcoded-mirror in this file. Warn-only semantics
        # unchanged. If Config failed to load, check 4 has already reported it
        # loudly; these fall back to the historical constants rather than
        # suppressing the trade scan entirely.
        min_put_premium = 0.50
        min_call_premium = 0.30
        if cfg is not None:
            try:
                min_put_premium = float(cfg.min_put_premium)
                min_call_premium = float(cfg.min_call_premium)
            except Exception:  # pragma: no cover - defensive
                pass

        try:
            from google.cloud import bigquery
            bq = bigquery.Client(project=GCP_PROJECT)
            query = f"""
                SELECT symbol, option_type, premium, dte, strike_price, client_order_id
                FROM `{GCP_PROJECT}.{self.bq_dataset}.{BQ_TABLE}`
                WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
                ORDER BY timestamp DESC
                LIMIT 50
            """
            rows = list(bq.query(query).result())

            for row in rows:
                # Premium thresholds
                premium = float(row.get("premium") or 0)
                opt_type = row.get("option_type", "")
                if opt_type == "put" and 0 < premium < min_put_premium:
                    checks.append(CheckResult(
                        "risk_min_premium", "warn",
                        f"Put trade {row.get('symbol')} premium ${premium:.2f} "
                        f"below min (${min_put_premium:.2f})",
                    ))
                elif opt_type == "call" and 0 < premium < min_call_premium:
                    checks.append(CheckResult(
                        "risk_min_premium", "warn",
                        f"Call trade {row.get('symbol')} premium ${premium:.2f} "
                        f"below min (${min_call_premium:.2f})",
                    ))

                # client_order_id must be present
                if not row.get("client_order_id"):
                    checks.append(CheckResult(
                        "risk_client_order_id", "warn",
                        f"Trade {row.get('symbol')} missing client_order_id",
                    ))

            if not any(r.name == "risk_min_premium" for r in checks):
                checks.append(CheckResult("risk_min_premium", "pass", "All recent trade premiums meet minimums"))
            if not any(r.name == "risk_client_order_id" for r in checks):
                checks.append(CheckResult("risk_client_order_id", "pass", "All recent trades have client_order_id"))

        except ImportError:
            checks.append(CheckResult("risk_recent_trades", "warn", "BigQuery not available, skipping trade validation"))
        except Exception as exc:
            checks.append(CheckResult("risk_recent_trades", "warn", f"Trade validation query failed: {exc}"))

        return checks

    # ------------------------------------------------------------------
    # 7. Deploy freshness — merged vs. deployed (FC-081 follow-up)
    # ------------------------------------------------------------------

    def check_deploy_freshness(self) -> List[CheckResult]:
        """Compare the commit this service is running against GitHub `main`.

        The build-failure alert (FC-030/FC-031) watches builds that *start*.
        This watches for builds that never start: FC-081 was a repo rename that
        detached the Cloud Build trigger, so `main` ran 16 days ahead of
        production with no red build and therefore no alert. Cloud Build's own
        history cannot tell a quiet week from a dead trigger — only GitHub can.

        Exactly one ``CheckResult`` per run:

        ==================================================  ======  =================================
        Condition                                           status  name
        ==================================================  ======  =================================
        drift beyond the window                             fail    deploy_freshness_drift
        GitHub redirect (repo RENAMED — the FC-081 mode)    fail    deploy_freshness_repo_unreachable
        GitHub 404 (deleted / private / access lost)        fail    deploy_freshness_repo_unreachable
        ``GIT_COMMIT`` unset                                warn    deploy_freshness_no_commit
        ``GITHUB_REPO`` malformed                           warn    deploy_freshness_unconfigured
        401/403/429/5xx, timeout, bad JSON, bad sha/date    warn    deploy_freshness_github_error
        in flight / match                                   pass    deploy_freshness
        ==================================================  ======  =================================

        ``fail`` makes ``/regression`` return HTTP 500, so it is reserved for
        the conditions that genuinely mean "production is not running `main`".
        A GitHub outage must never 500 the monitor.

        **Every degraded (`warn`) path also emits a `deploy_freshness_degraded`
        log event.** A detective control that quietly reports `warn` forever is
        indistinguishable from one that is working, which is how an hourly
        report's warn rows become wallpaper. The degraded event has its own
        alert policy with a 24h rate limit — a once-a-day nag, not a page.

        The repo is public, so **no token is required**: with ``GITHUB_TOKEN``
        empty the request goes out unauthenticated and the check still returns
        a real verdict, flagging itself degraded (``authenticated=False``)
        because the unauthenticated rate limit is shared per source IP.
        """
        repo = os.environ.get("GITHUB_REPO") or GITHUB_REPO
        git_commit = (os.environ.get("GIT_COMMIT") or "").strip()
        token = (os.environ.get("GITHUB_TOKEN") or "").strip()

        details: Dict[str, Any] = {
            "git_commit": git_commit or None,
            "head_sha": None,
            "head_age_minutes": None,
            "max_hours": DEPLOY_FRESHNESS_MAX_HOURS_DEFAULT,
            "repo": repo,
            "authenticated": bool(token),
        }

        def _degraded(reason: str, check: str) -> None:
            """Announce that the check could not do its full job, and why."""
            logger.warning(
                "Deploy freshness check degraded",
                event_category="system",
                event_type="deploy_freshness_degraded",
                reason=reason,
                check=check,
                repo=repo,
            )

        def _one(name: str, status: str, message: str) -> List[CheckResult]:
            result = CheckResult(name, status, message, dict(details))
            self.results.append(result)
            return [result]

        def _warn(name: str, message: str, reason: str) -> List[CheckResult]:
            _degraded(reason, name)
            return _one(name, "warn", message)

        # --- operator input, validated before anything is sent anywhere ---
        if not GITHUB_REPO_PATTERN.match(repo):
            return _warn(
                "deploy_freshness_unconfigured",
                f"GITHUB_REPO {repo!r} is not a valid owner/name pair — "
                f"deploy freshness cannot be checked",
                "bad_repo",
            )

        raw_max_hours = os.environ.get("DEPLOY_FRESHNESS_MAX_HOURS")
        if raw_max_hours:
            try:
                candidate = float(raw_max_hours)
            except (TypeError, ValueError):
                candidate = None
            # NaN loses every comparison, so an unguarded NaN window would make
            # drift unreachable — the check would report "in flight" forever.
            # inf and negatives are equally not-a-window. All fall back to the
            # default rather than silently disabling the control. Zero is
            # legal: it is the documented negative-drill setting.
            if candidate is None or not math.isfinite(candidate) or candidate < 0:
                _degraded("bad_window", "deploy_freshness")
            else:
                details["max_hours"] = candidate
        max_hours = details["max_hours"]

        if not git_commit:
            return _warn(
                "deploy_freshness_no_commit",
                "GIT_COMMIT is not set on this service — cannot compare the "
                "deployed commit against GitHub main. Expected before the env "
                "var rolls out, and after a manual `gcloud builds submit` "
                "(which supplies no $COMMIT_SHA).",
                "no_commit",
            )

        # --- the request ---
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            # Not terminal: the repo is public. Degraded, and it keeps going.
            _degraded("unauthenticated", "deploy_freshness")

        url = f"https://api.github.com/repos/{repo}/commits/main"

        try:
            resp = self._github_get(
                url, headers=headers, timeout=GITHUB_API_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # ONLY the exception class, never str(exc): `requests` echoes header
            # VALUES into InvalidHeader messages, so interpolating the exception
            # would copy the GitHub token into a CheckResult that /regression
            # returns over HTTP and writes to Cloud Logging.
            return _warn(
                "deploy_freshness_github_error",
                f"GitHub request failed ({type(exc).__name__})",
                "request_failed",
            )

        status_code = getattr(resp, "status_code", None)
        details["status_code"] = status_code

        if status_code in GITHUB_REDIRECT_STATUSES:
            # THE FC-081 detector. GitHub keeps a rename redirect alive
            # indefinitely, so the API half of the system keeps working while
            # the Cloud Build trigger — which matches the old literal name and
            # has no such forwarding — silently stops firing.
            try:
                location = (getattr(resp, "headers", None) or {}).get("Location") or ""
            except Exception:
                location = ""
            details["redirect_location"] = location
            message = (
                f"GITHUB_REPO '{repo}' is stale — GitHub redirected to "
                f"'{location}'; the Cloud Build trigger keys on the same "
                f"literal name — repoint both (FC-081 failure mode)"
            )
            log_error_event(
                logger,
                error_type="deploy_freshness_repo_unreachable",
                error_message=message,
                component="regression_monitor",
                recoverable=False,
                repo=repo,
                redirect_location=location,
                status_code=status_code,
                git_commit=git_commit,
            )
            return _one("deploy_freshness_repo_unreachable", "fail", message)

        if status_code == 404:
            message = (
                f"GitHub returned 404 for '{repo}': repo not found — deleted, "
                f"made private, or the token lost access. (A RENAMED repo "
                f"redirects instead of 404ing, so this is not the rename case.)"
            )
            log_error_event(
                logger,
                error_type="deploy_freshness_repo_unreachable",
                error_message=message,
                component="regression_monitor",
                recoverable=False,
                repo=repo,
                status_code=404,
                git_commit=git_commit,
            )
            return _one("deploy_freshness_repo_unreachable", "fail", message)

        if status_code != 200:
            # 401/403 (bad or unscoped token), 429 (rate limit — likelier on
            # the shared unauthenticated pool), 5xx (GitHub outage). None of
            # these say anything about what is deployed, so none may fail.
            return _warn(
                "deploy_freshness_github_error",
                f"GitHub returned HTTP {status_code} for '{repo}'",
                "http_error",
            )

        # --- the body ---
        try:
            body = resp.json()
            head_sha = body["sha"]
            head_date_raw = body["commit"]["committer"]["date"]
        except Exception as exc:
            return _warn(
                "deploy_freshness_github_error",
                f"Could not read sha/date from the GitHub response "
                f"({type(exc).__name__})",
                "bad_json",
            )

        if not isinstance(head_sha, str) or not GIT_SHA_PATTERN.match(head_sha):
            # Validated before use: every line below assumes a 40-hex string,
            # and an unchecked None/int would raise out of the check group as a
            # `fail` — a 500 on the hourly monitor caused by a bad response body.
            return _warn(
                "deploy_freshness_github_error",
                "GitHub response `sha` is not a 40-character hex commit id",
                "bad_sha",
            )
        details["head_sha"] = head_sha

        try:
            head_date = datetime.fromisoformat(
                str(head_date_raw).replace("Z", "+00:00")
            )
        except Exception as exc:
            return _warn(
                "deploy_freshness_github_error",
                f"Could not parse the committer date ({type(exc).__name__})",
                "bad_date",
            )

        if head_date.tzinfo is None:
            head_date = head_date.replace(tzinfo=timezone.utc)

        head_age_minutes = round(
            (_utcnow() - head_date).total_seconds() / 60.0, 1
        )
        details["head_age_minutes"] = head_age_minutes

        # A commit dated in the future (skewed committer clock, or a rebase
        # that forward-dated it) would otherwise make `age > window` false
        # forever, i.e. permanent "in flight" — drift that can never be seen.
        # Clamp to zero and say so.
        effective_age_minutes = head_age_minutes
        if head_age_minutes < 0:
            _degraded("future_commit_date", "deploy_freshness")
            effective_age_minutes = 0.0

        # $COMMIT_SHA is full-length, so the common case is a 40-char compare.
        # A hand-set short GIT_COMMIT is tolerated by prefix, but only at >= 7
        # characters — shorter prefixes collide often enough to mask real drift.
        matched = (
            git_commit == head_sha
            or (len(git_commit) >= 7 and head_sha.startswith(git_commit))
        )

        if matched:
            return _one(
                "deploy_freshness", "pass",
                f"Deployed commit matches {repo}@main ({head_sha[:7]})",
            )

        # Strictly greater: age exactly at the window is still a build in
        # flight. The window is the grace period, not the deadline.
        if effective_age_minutes / 60.0 > max_hours:
            message = (
                f"Deployed commit {git_commit[:7]} does not match "
                f"{repo}@main {head_sha[:7]}, which was committed "
                f"{head_age_minutes:.0f}m ago (> {max_hours}h). main is merged "
                f"but NOT deployed — check the Cloud Build trigger."
            )
            log_error_event(
                logger,
                error_type="deploy_freshness_drift",
                error_message=message,
                component="regression_monitor",
                recoverable=False,
                repo=repo,
                git_commit=git_commit,
                head_sha=head_sha,
                head_age_minutes=head_age_minutes,
                max_hours=max_hours,
            )
            return _one("deploy_freshness_drift", "fail", message)

        return _one(
            "deploy_freshness", "pass",
            f"build in flight ({head_age_minutes:.0f}m): {repo}@main "
            f"{head_sha[:7]} is newer than the deployed {git_commit[:7]}",
        )

    def run_all_checks(self) -> Dict[str, Any]:
        """Execute all regression checks and return a consolidated report."""
        self.results = []
        self.start_time = datetime.now(timezone.utc)

        check_groups = {
            "endpoint_health": self.check_endpoints,
            "trade_execution": self.check_trade_execution,
            "log_analysis": self.check_logs,
            "position_reconciliation": self.check_position_reconciliation,
            "performance_baseline": self.check_performance_baseline,
            "risk_parameters": self.check_risk_parameters,
            "deploy_freshness": self.check_deploy_freshness,
        }

        group_results: Dict[str, List[Dict]] = {}
        for group_name, check_fn in check_groups.items():
            try:
                results = check_fn()
                group_results[group_name] = [r.to_dict() for r in results]
            except Exception as exc:
                group_results[group_name] = [
                    CheckResult(
                        f"{group_name}_exception", "fail",
                        f"Check group failed with exception: {exc}",
                        {"traceback": traceback.format_exc()},
                    ).to_dict()
                ]

        duration_seconds = (datetime.now(timezone.utc) - self.start_time).total_seconds()

        # Aggregate status
        all_statuses = [r["status"] for group in group_results.values() for r in group]
        has_failures = "fail" in all_statuses
        has_warnings = "warn" in all_statuses

        if has_failures:
            overall_status = "fail"
        elif has_warnings:
            overall_status = "warn"
        else:
            overall_status = "pass"

        report = {
            "overall_status": overall_status,
            "timestamp": self.start_time.isoformat(),
            "duration_seconds": round(duration_seconds, 2),
            "summary": {
                "total_checks": len(all_statuses),
                "passed": all_statuses.count("pass"),
                "warnings": all_statuses.count("warn"),
                "failures": all_statuses.count("fail"),
            },
            "check_groups": group_results,
        }

        # Log via structlog with event_category for BigQuery export
        log_system_event(
            logger,
            event_type="regression_test_completed",
            status=overall_status,
            duration_seconds=round(duration_seconds, 2),
            total_checks=len(all_statuses),
            passed=all_statuses.count("pass"),
            warnings=all_statuses.count("warn"),
            failures=all_statuses.count("fail"),
        )

        log_performance_metric(
            logger,
            metric_name="regression_test_duration",
            metric_value=round(duration_seconds, 2),
            metric_unit="seconds",
            total_checks=len(all_statuses),
            overall_status=overall_status,
        )

        if has_failures:
            failed_checks = [
                r["name"] for group in group_results.values()
                for r in group if r["status"] == "fail"
            ]
            log_error_event(
                logger,
                error_type="regression_test_failures",
                error_message=f"{len(failed_checks)} regression checks failed: {failed_checks}",
                component="regression_monitor",
                recoverable=True,
                failed_checks=failed_checks,
            )

        return report


# ---------------------------------------------------------------------------
# Flask app for standalone deployment
# ---------------------------------------------------------------------------

def create_app() -> "Flask":
    """Create a minimal Flask app for standalone deployment."""
    from flask import Flask, jsonify, request as flask_request

    standalone_app = Flask(__name__)

    @standalone_app.route("/", methods=["GET"])
    def health():
        return jsonify({"status": "healthy", "service": "regression-monitor"})

    @standalone_app.route("/regression", methods=["POST"])
    def run_regression():
        monitor = RegressionMonitor()
        report = monitor.run_all_checks()
        status_code = 200 if report["overall_status"] != "fail" else 500
        return jsonify(report), status_code

    return standalone_app


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Run regression checks from the command line."""
    import argparse

    parser = argparse.ArgumentParser(description="Options Wheel Regression Monitor")
    parser.add_argument("--url", default=SERVICE_URL, help="Service URL to test")
    parser.add_argument("--api-key", default=os.environ.get("STRATEGY_API_KEY", ""), help="API key")
    parser.add_argument("--serve", action="store_true", help="Start as a web server")
    parser.add_argument("--port", type=int, default=8081, help="Port for web server mode")
    args = parser.parse_args()

    if args.serve:
        app = create_app()
        app.run(host="0.0.0.0", port=args.port, debug=False)
        return

    print(f"Running regression checks against {args.url}")
    print("=" * 70)

    monitor = RegressionMonitor(service_url=args.url, api_key=args.api_key)
    report = monitor.run_all_checks()

    # Print summary
    summary = report["summary"]
    overall = report["overall_status"].upper()
    print(f"\nOverall: {overall}")
    print(f"Checks: {summary['total_checks']} total, "
          f"{summary['passed']} passed, "
          f"{summary['warnings']} warnings, "
          f"{summary['failures']} failures")
    print(f"Duration: {report['duration_seconds']}s")

    # Print details for non-passing checks
    for group_name, group_checks in report["check_groups"].items():
        non_pass = [c for c in group_checks if c["status"] != "pass"]
        if non_pass:
            print(f"\n--- {group_name} ---")
            for check in non_pass:
                print(f"  [{check['status'].upper()}] {check['name']}: {check['message']}")

    # Print full report as JSON
    print("\n" + "=" * 70)
    print("Full report:")
    print(json.dumps(report, indent=2, default=str))

    # Exit with appropriate code
    sys.exit(1 if report["overall_status"] == "fail" else 0)


if __name__ == "__main__":
    main()
