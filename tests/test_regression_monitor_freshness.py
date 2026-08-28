"""Deploy-freshness check: merged-vs-deployed drift detection (FC-081 follow-up).

The build-failure alert (FC-030/FC-031) watches builds that *start*. FC-081 was
a repo rename that detached the Cloud Build trigger — **no build ever started**,
so no build failed, so nothing alerted, and `main` ran 16 days ahead of
production in silence. `check_deploy_freshness` closes that hole by comparing
what is serving (`GIT_COMMIT`) against what is on GitHub `main`.

Every test here stubs the GitHub getter (`RegressionMonitor._github_get`), the
clock seam (`_utcnow`, where a boundary is under test) and the environment.
Nothing touches the network — a detective control that needs the internet to be
tested is a detective control nobody runs in CI.

The properties worth the most, in order:

* **A rename is a REDIRECT, not a 404** (`TestRepoUnreachable`). GitHub keeps a
  rename redirect alive indefinitely and `requests` follows redirects by
  default, so the naive implementation of this check returns a cheerful 200
  from the renamed repo and reports `pass` on the exact condition it exists to
  catch. `allow_redirects=False` is the whole detector.
* **Drift beyond the window is `fail`** — `fail` is what makes `/regression`
  return HTTP 500 and page.
* **Drift inside the window is `pass`** — without it every merge pages the
  operator for the 10-12 minutes a build takes, and the alert gets muted, which
  is how FC-031's red build went unseen in the first place.
* **No warn is silent** — every degraded path emits `deploy_freshness_degraded`,
  because a control that reports `warn` forever looks exactly like one that is
  working.
"""

from datetime import datetime, timedelta, timezone

import pytest

import tools.testing.regression_monitor as rm
from tools.testing.regression_monitor import RegressionMonitor


HEAD_SHA = "a" * 39 + "1"
DEPLOYED_SHA = "b" * 39 + "2"


class _Resp:
    """Minimal stand-in for `requests.Response`."""

    def __init__(self, payload=None, status_code=200, raises=None, headers=None):
        self._payload = payload
        self.status_code = status_code
        self._raises = raises
        self.headers = headers or {}

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


def _commit_body(sha=HEAD_SHA, age_minutes=5.0, now=None):
    """A GitHub `GET /repos/{repo}/commits/main` body, `age_minutes` old."""
    committed = (now or datetime.now(timezone.utc)) - timedelta(minutes=age_minutes)
    return {
        "sha": sha,
        "commit": {"committer": {"date": committed.isoformat().replace("+00:00", "Z")}},
    }


@pytest.fixture
def env(monkeypatch):
    """A configured service: token present, GIT_COMMIT set, default window.

    Every variable the check reads is set or cleared explicitly so an ambient
    GITHUB_TOKEN on a developer machine cannot change a verdict.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    monkeypatch.setenv("GIT_COMMIT", DEPLOYED_SHA)
    monkeypatch.delenv("DEPLOY_FRESHNESS_MAX_HOURS", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    return monkeypatch


@pytest.fixture
def logged_errors(monkeypatch):
    """Capture `log_error_event` calls — the alertable, page-worthy events."""
    calls = []
    monkeypatch.setattr(rm, "log_error_event", lambda logger, **kw: calls.append(kw))
    return calls


@pytest.fixture
def degraded(monkeypatch):
    """Capture `deploy_freshness_degraded` warnings off the module logger."""
    calls = []

    class _Logger:
        def warning(self, event, **kwargs):
            calls.append({"event": event, **kwargs})

        def __getattr__(self, _name):
            return lambda *a, **k: None

    monkeypatch.setattr(rm, "logger", _Logger())
    return calls


def freeze(monkeypatch, at):
    """Pin the check's clock seam so a boundary is exactly a boundary."""
    monkeypatch.setattr(rm, "_utcnow", lambda: at)


def run_check(monkeypatch, response=None, raises=None):
    """Drive `check_deploy_freshness` against a stubbed GitHub, one result out.

    Returns the single `CheckResult`; the check's contract is exactly one
    result per run, and that is asserted here so no test can quietly depend on
    a second one appearing.
    """
    monitor = RegressionMonitor(service_url="http://test", api_key="k")
    requested = []

    def fake_github_get(url, headers, timeout=10):
        requested.append({"url": url, "headers": headers, "timeout": timeout})
        if raises is not None:
            raise raises
        return response

    monkeypatch.setattr(monitor, "_github_get", fake_github_get)

    results = monitor.check_deploy_freshness()
    assert len(results) == 1, f"expected exactly one CheckResult, got {results}"
    result = results[0]
    result.requested = requested  # test-only handle on the outbound request
    return result


def reasons(degraded_calls):
    return [c["reason"] for c in degraded_calls]


# ---------------------------------------------------------------------------
# The happy paths: match, and prefix matching
# ---------------------------------------------------------------------------

def test_deployed_commit_matching_main_passes(env, logged_errors, degraded):
    env.setenv("GIT_COMMIT", HEAD_SHA)

    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    assert result.status == "pass"
    assert result.name == "deploy_freshness"
    assert result.details["head_sha"] == HEAD_SHA
    assert result.details["git_commit"] == HEAD_SHA
    assert result.details["authenticated"] is True
    assert not logged_errors, "a matching deploy must not emit an error event"
    assert not degraded, "a fully configured, matching check is not degraded"


def test_short_git_commit_matches_by_prefix(env):
    """`$COMMIT_SHA` is full-length, but a hand-set 7+ char SHA still matches."""
    env.setenv("GIT_COMMIT", HEAD_SHA[:7])

    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    assert result.status == "pass"


def test_prefix_shorter_than_seven_chars_is_not_a_match(env):
    """Below 7 chars a prefix collides too easily to be trusted as a match.

    A 4-char "match" that is really a collision would report `pass` while the
    service runs a completely different commit — the exact silence FC-081 was.
    """
    env.setenv("GIT_COMMIT", HEAD_SHA[:4])

    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    assert result.status == "fail"
    assert result.name == "deploy_freshness_drift"


# ---------------------------------------------------------------------------
# Drift beyond the window -> fail  (THE core regression)
# ---------------------------------------------------------------------------

def test_drift_beyond_the_window_fails_and_logs_the_event(env, logged_errors, degraded):
    """main merged 3h ago, still not deployed: `fail` (=> /regression 500)."""
    result = run_check(env, response=_Resp(_commit_body(age_minutes=180)))

    assert result.status == "fail"
    assert result.name == "deploy_freshness_drift"
    assert result.details["head_sha"] == HEAD_SHA
    assert result.details["git_commit"] == DEPLOYED_SHA
    assert result.details["max_hours"] == 2.0
    assert result.details["head_age_minutes"] == pytest.approx(180, abs=1)

    assert len(logged_errors) == 1, "drift must emit exactly one error event"
    event = logged_errors[0]
    # `log_error_event` sets event_type from error_type; `log_system_event`
    # does not (FC-047), and the alert policy matches on event_type.
    assert event["error_type"] == "deploy_freshness_drift"
    assert event["component"] == "regression_monitor"
    assert not degraded, "real drift is a page, not a degradation"


def test_fresh_mismatch_is_a_build_in_flight_not_drift(env, logged_errors):
    """A merge 20 minutes ago is a build running, not a dead trigger."""
    result = run_check(env, response=_Resp(_commit_body(age_minutes=20)))

    assert result.status == "pass"
    assert result.name == "deploy_freshness"
    assert "in flight" in result.message
    assert not logged_errors, (
        "paging on every merge is how an alert gets muted — see FC-031"
    )


class TestWindowBoundary:
    """Drift iff age > window. Exactly at the window is still in flight.

    The clock is pinned so "exactly" means exactly; without the seam the few
    microseconds between building the body and running the check push the age
    past the boundary and the test silently stops testing the boundary.
    """

    NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

    def test_age_exactly_at_the_window_is_not_drift(self, env, logged_errors):
        freeze(env, self.NOW)
        body = _commit_body(age_minutes=120, now=self.NOW)

        result = run_check(env, response=_Resp(body))

        assert result.details["head_age_minutes"] == 120.0
        assert result.status == "pass", "the window is a grace period, not a deadline"
        assert "in flight" in result.message
        assert not logged_errors

    def test_one_minute_past_the_window_is_drift(self, env):
        freeze(env, self.NOW)
        body = _commit_body(age_minutes=121, now=self.NOW)

        result = run_check(env, response=_Resp(body))

        assert result.status == "fail"
        assert result.name == "deploy_freshness_drift"


# ---------------------------------------------------------------------------
# The window env var
# ---------------------------------------------------------------------------

def test_env_window_override_is_honored(env, logged_errors):
    """90 minutes is inside the 2h default but outside an explicit 1h."""
    env.setenv("DEPLOY_FRESHNESS_MAX_HOURS", "1")

    result = run_check(env, response=_Resp(_commit_body(age_minutes=90)))

    assert result.status == "fail"
    assert result.name == "deploy_freshness_drift"
    assert result.details["max_hours"] == 1.0
    assert len(logged_errors) == 1


def test_same_age_passes_under_the_default_window(env):
    """Control for the test above: the override, not the age, moved the verdict."""
    result = run_check(env, response=_Resp(_commit_body(age_minutes=90)))

    assert result.status == "pass"
    assert "in flight" in result.message


def test_zero_window_is_legal_and_makes_any_mismatch_drift(env):
    """`DEPLOY_FRESHNESS_MAX_HOURS=0` is the documented negative drill.

    It must NOT be swept up by the "not a usable window" guard, or the drill
    would quietly test nothing.
    """
    env.setenv("DEPLOY_FRESHNESS_MAX_HOURS", "0")

    result = run_check(env, response=_Resp(_commit_body(age_minutes=5)))

    assert result.details["max_hours"] == 0.0
    assert result.status == "fail"


@pytest.mark.parametrize(
    "raw",
    ["not-a-number", "nan", "NaN", "inf", "-inf", "Infinity", "-1", "-0.5", ""],
)
def test_unusable_window_falls_back_to_the_default(env, degraded, raw):
    """NaN is the dangerous one: it loses every comparison.

    `age > nan` is False for any age, so an unguarded NaN window would make
    drift unreachable — the check would report "in flight" forever while
    looking perfectly healthy.
    """
    env.setenv("DEPLOY_FRESHNESS_MAX_HOURS", raw)

    result = run_check(env, response=_Resp(_commit_body(age_minutes=180)))

    assert result.details["max_hours"] == rm.DEPLOY_FRESHNESS_MAX_HOURS_DEFAULT
    assert result.status == "fail", "the default window must still catch 3h drift"
    if raw:  # an empty value is "unset", not "malformed"
        assert reasons(degraded) == ["bad_window"]
    else:
        assert not degraded


# ---------------------------------------------------------------------------
# Repo unreachable: the redirect (rename) and the 404 (gone)
# ---------------------------------------------------------------------------

class TestRepoUnreachable:
    """A renamed repo REDIRECTS. It does not 404.

    GitHub answers 301 with the new location and keeps doing so indefinitely.
    `requests` follows redirects by default, so a check that does not opt out
    gets a 200 from the renamed repo and reports `pass` — while the Cloud Build
    trigger, which matches the old literal name and has no forwarding, is dead.
    That is FC-081, exactly.
    """

    @pytest.mark.parametrize("status", [301, 302, 307, 308])
    def test_redirect_is_the_rename_detector(self, env, logged_errors, status):
        resp = _Resp(
            {},
            status_code=status,
            headers={"Location": "https://api.github.com/repositories/12345/commits/main"},
        )

        result = run_check(env, response=resp)

        assert result.status == "fail"
        assert result.name == "deploy_freshness_repo_unreachable"
        assert "is stale" in result.message
        assert "https://api.github.com/repositories/12345/commits/main" in result.message
        assert "Cloud Build trigger" in result.message
        assert result.details["redirect_location"].endswith("/commits/main")

        assert len(logged_errors) == 1
        assert logged_errors[0]["error_type"] == "deploy_freshness_repo_unreachable"

    def test_redirect_without_a_location_header_still_fails(self, env, logged_errors):
        """A redirect is the signal; the Location header is only the detail."""
        result = run_check(env, response=_Resp({}, status_code=301, headers={}))

        assert result.status == "fail"
        assert result.name == "deploy_freshness_repo_unreachable"
        assert len(logged_errors) == 1

    def test_the_getter_does_not_follow_redirects(self, env, monkeypatch):
        """The opt-out itself, at the one place it can be got wrong."""
        captured = {}

        def fake_requests_get(url, **kwargs):
            captured.update(kwargs)
            return _Resp({}, status_code=301)

        monkeypatch.setattr(rm.requests, "get", fake_requests_get)
        monitor = RegressionMonitor(service_url="http://test", api_key="k")

        monitor._github_get("https://api.github.com/x", headers={})

        assert captured["allow_redirects"] is False, (
            "following redirects turns a renamed repo into a passing check"
        )

    def test_404_fails_with_an_honest_message(self, env, logged_errors):
        """404 is NOT the rename case — say what it actually means."""
        result = run_check(
            env, response=_Resp({"message": "Not Found"}, status_code=404)
        )

        assert result.status == "fail"
        assert result.name == "deploy_freshness_repo_unreachable"
        assert "repo not found" in result.message
        assert "deleted, made private, or the token lost access" in result.message
        assert len(logged_errors) == 1
        assert logged_errors[0]["error_type"] == "deploy_freshness_repo_unreachable"


# ---------------------------------------------------------------------------
# Transient GitHub trouble -> warn, never fail, never silent
# ---------------------------------------------------------------------------

def _body_with(sha):
    return {
        "sha": sha,
        "commit": {"committer": {"date": "2026-08-28T10:00:00Z"}},
    }


@pytest.mark.parametrize(
    "response,raises,reason",
    [
        (_Resp({"message": "Bad credentials"}, status_code=401), None, "http_error"),
        (_Resp({"message": "Forbidden"}, status_code=403), None, "http_error"),
        (_Resp({"message": "rate limited"}, status_code=429), None, "http_error"),
        (_Resp({"message": "boom"}, status_code=500), None, "http_error"),
        (_Resp({"message": "boom"}, status_code=503), None, "http_error"),
        (_Resp(raises=ValueError("Expecting value: line 1 column 1")), None, "bad_json"),
        (_Resp({"commit": {}}), None, "bad_json"),                       # no sha key
        (_Resp({"sha": HEAD_SHA, "commit": {}}), None, "bad_json"),      # no date key
        (_Resp(_body_with(None)), None, "bad_sha"),
        (_Resp(_body_with(123)), None, "bad_sha"),
        (_Resp(_body_with("not-a-sha")), None, "bad_sha"),
        (_Resp(_body_with(HEAD_SHA.upper())), None, "bad_sha"),
        (_Resp({"sha": HEAD_SHA,
                "commit": {"committer": {"date": "not-a-date"}}}), None, "bad_date"),
        (None, TimeoutError("read timed out"), "request_failed"),
        (None, ConnectionError("name resolution failed"), "request_failed"),
    ],
    ids=[
        "401", "403", "429", "500", "503", "non-json-body", "no-sha-key",
        "no-date-key", "sha-none", "sha-int", "sha-garbage", "sha-uppercase",
        "bad-date", "timeout", "connection-error",
    ],
)
def test_github_trouble_warns_never_fails(env, logged_errors, degraded,
                                          response, raises, reason):
    """A GitHub outage must not 500 the hourly monitor — and must not be silent.

    The `sha` cases matter beyond the status: every line after the parse
    assumes a 40-hex string (`head_sha[:7]`, `startswith`). An unvalidated
    None or int would raise out of the check group, which `run_all_checks`
    converts into a `fail` — a 500 on the monitor caused by a bad response body.
    """
    result = run_check(env, response=response, raises=raises)

    assert result.status == "warn"
    assert result.name == "deploy_freshness_github_error"
    assert not logged_errors, "a transient GitHub error is not an alertable event"
    assert reasons(degraded) == [reason], "exactly one degraded event, right reason"
    assert degraded[0]["event_type"] == "deploy_freshness_degraded"
    assert degraded[0]["event_category"] == "system"
    assert degraded[0]["check"] == "deploy_freshness_github_error"


# ---------------------------------------------------------------------------
# The token never reaches a message, a detail or a log
# ---------------------------------------------------------------------------

TAINTED_TOKEN = "github_pat_SECRET\rinjected"


def test_a_rejected_token_is_never_echoed_into_the_result(env, degraded):
    """`requests` puts header VALUES in its InvalidHeader message.

    A token with a stray CR (a trailing newline in the secret is the realistic
    version) makes `requests` raise
    `InvalidHeader("... in header value 'Bearer github_pat_SECRET\\rinjected'")`
    before any socket is opened. Interpolating `str(exc)` would copy the live
    GitHub token into a CheckResult that `/regression` returns over HTTP and
    that Cloud Logging stores — turning a config typo into a credential leak.

    The real `_github_get` is used here: the raise happens during header
    validation, so this needs no network.
    """
    env.setenv("GITHUB_TOKEN", TAINTED_TOKEN)
    monitor = RegressionMonitor(service_url="http://test", api_key="k")

    results = monitor.check_deploy_freshness()

    assert len(results) == 1
    result = results[0]
    assert result.status == "warn"
    assert result.name == "deploy_freshness_github_error"
    # The exception CLASS is informative and safe; the exception TEXT is not.
    assert "InvalidHeader" in result.message

    haystack = f"{result.message} {result.details} {degraded}"
    for secret in ("github_pat_SECRET", "SECRET", TAINTED_TOKEN):
        assert secret not in haystack, (
            f"the GitHub token leaked into the check result via {secret!r}"
        )


# ---------------------------------------------------------------------------
# Degraded-but-working: no token (public repo), no GIT_COMMIT, bad repo
# ---------------------------------------------------------------------------

class TestUnauthenticatedFallback:
    """The repo is public, so a missing token is a degradation, not a stop.

    Reporting `deploy_freshness_unconfigured` and giving up would have left the
    check inert for however long secret provisioning took — while the whole
    comparison was available unauthenticated the entire time.
    """

    def test_no_token_still_produces_a_real_verdict(self, env, logged_errors, degraded):
        env.delenv("GITHUB_TOKEN", raising=False)

        result = run_check(env, response=_Resp(_commit_body(age_minutes=180)))

        assert result.status == "fail", "an unauthenticated check still detects drift"
        assert result.name == "deploy_freshness_drift"
        assert result.details["authenticated"] is False
        assert len(logged_errors) == 1
        assert reasons(degraded) == ["unauthenticated"]

    def test_no_token_sends_no_authorization_header(self, env):
        env.delenv("GITHUB_TOKEN", raising=False)

        result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

        headers = result.requested[0]["headers"]
        assert "Authorization" not in headers
        assert headers["Accept"] == "application/vnd.github+json"

    def test_whitespace_token_is_treated_as_absent(self, env, degraded):
        env.setenv("GITHUB_TOKEN", "   ")
        env.setenv("GIT_COMMIT", HEAD_SHA)

        result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

        assert result.status == "pass"
        assert "Authorization" not in result.requested[0]["headers"]
        assert reasons(degraded) == ["unauthenticated"]

    def test_a_passing_unauthenticated_check_still_passes(self, env, degraded):
        env.delenv("GITHUB_TOKEN", raising=False)
        env.setenv("GIT_COMMIT", HEAD_SHA)

        result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

        assert result.status == "pass"
        assert reasons(degraded) == ["unauthenticated"]


def test_missing_git_commit_warns_no_commit(env, degraded):
    """Pre-rollout, and after a manual `gcloud builds submit` (no $COMMIT_SHA)."""
    env.delenv("GIT_COMMIT", raising=False)

    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    assert result.status == "warn"
    assert result.name == "deploy_freshness_no_commit"
    assert result.details["git_commit"] is None
    assert result.requested == [], "nothing to compare means no GitHub call"
    assert reasons(degraded) == ["no_commit"]


def test_missing_git_commit_wins_over_a_missing_token(env, degraded):
    """Both absent: report the one that actually blocks the comparison.

    Without GIT_COMMIT there is nothing to compare even against a public repo,
    so `unauthenticated` would send the operator after the wrong problem.
    """
    env.delenv("GIT_COMMIT", raising=False)
    env.delenv("GITHUB_TOKEN", raising=False)

    result = run_check(env, response=_Resp(_commit_body()))

    assert result.name == "deploy_freshness_no_commit"
    assert reasons(degraded) == ["no_commit"]


@pytest.mark.parametrize(
    "bad_repo",
    # NOTE: "" is deliberately absent — an empty env var reads as unset and
    # falls back to the module constant (covered by the next test), so it is
    # not a malformed repo.
    ["../../user", "memon1987", "https://github.com/memon1987/x",
     "memon1987/agentic options wheel", "a/b/c", "/x", "x/"],
    ids=["traversal", "no-slash", "full-url", "space", "three-parts",
         "empty-owner", "empty-name"],
)
def test_malformed_github_repo_is_unconfigured(env, degraded, bad_repo):
    """A repo reference is `owner/name`; anything else is an operator typo.

    Sending `../../user` to GitHub and interpreting whatever comes back is how
    a path-traversal-shaped config value becomes a confident verdict about the
    wrong repository.
    """
    env.setenv("GITHUB_REPO", bad_repo)

    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    assert result.status == "warn"
    assert result.name == "deploy_freshness_unconfigured"
    assert result.requested == [], "a malformed repo is never sent to GitHub"
    assert reasons(degraded) == ["bad_repo"]


def test_empty_github_repo_env_falls_back_to_the_constant(env):
    """An empty env var is "unset", so the module default applies."""
    env.setenv("GITHUB_REPO", "")
    env.setenv("GIT_COMMIT", HEAD_SHA)

    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    assert result.details["repo"] == rm.GITHUB_REPO
    assert result.status == "pass"


# ---------------------------------------------------------------------------
# Clock skew
# ---------------------------------------------------------------------------

def test_future_commit_date_is_clamped_and_reported(env, logged_errors, degraded):
    """A forward-dated commit would otherwise be "in flight" forever.

    `age > window` is false for every negative age, so drift would become
    permanently unreachable while the check looked healthy.
    """
    result = run_check(env, response=_Resp(_commit_body(age_minutes=-45)))

    assert result.status == "pass", "clamped to zero == inside any window"
    assert "in flight" in result.message
    assert result.details["head_age_minutes"] < 0, "the real measurement is reported"
    assert reasons(degraded) == ["future_commit_date"]
    assert not logged_errors


# ---------------------------------------------------------------------------
# The outbound request
# ---------------------------------------------------------------------------

def test_default_repo_is_used_when_no_override(env):
    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    assert result.requested[0]["url"] == (
        f"https://api.github.com/repos/{rm.GITHUB_REPO}/commits/main"
    )
    assert result.details["repo"] == rm.GITHUB_REPO


def test_github_repo_env_override_is_used_in_the_url(env):
    """The FC-081 cause was a rename; the repo must be re-pointable without a
    code change, and the override must actually reach the request."""
    env.setenv("GITHUB_REPO", "memon1987/renamed_repo")

    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    assert result.requested[0]["url"] == (
        "https://api.github.com/repos/memon1987/renamed_repo/commits/main"
    )
    assert result.details["repo"] == "memon1987/renamed_repo"


def test_request_carries_the_documented_github_headers(env):
    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    headers = result.requested[0]["headers"]
    assert headers["Authorization"] == "Bearer ghp_fake"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert result.requested[0]["timeout"] == 10


# ---------------------------------------------------------------------------
# Accumulation into `self.results`, like every other check group
# ---------------------------------------------------------------------------

def test_result_is_appended_to_the_monitors_results(env, monkeypatch):
    monitor = RegressionMonitor(service_url="http://test", api_key="k")
    monkeypatch.setattr(
        monitor, "_github_get",
        lambda url, headers, timeout=10: _Resp(_commit_body(age_minutes=600)),
    )

    monitor.check_deploy_freshness()

    assert [r.name for r in monitor.results] == ["deploy_freshness_drift"]


# ---------------------------------------------------------------------------
# /health echoes the deployed commit
# ---------------------------------------------------------------------------

class TestHealthReportsGitCommit:
    """The human-eyeball half of the same signal.

    `/regression` is the automated comparison; `/health` is what an operator
    curls at 2am to answer "what is actually serving?" without waiting for the
    next scheduled run.
    """

    @staticmethod
    def _health(monkeypatch):
        pytest.importorskip("flask")
        import importlib

        mod = importlib.import_module("deploy.cloud_run_server")
        mod.reset_strategy_state()
        monkeypatch.delenv("STRATEGY_CONFIG", raising=False)
        try:
            return mod.app.test_client().get("/health").get_json()
        finally:
            mod.reset_strategy_state()

    def test_health_reports_the_deployed_commit(self, monkeypatch):
        monkeypatch.setenv("GIT_COMMIT", HEAD_SHA)

        assert self._health(monkeypatch)["git_commit"] == HEAD_SHA

    def test_health_reports_null_when_unset(self, monkeypatch):
        """Pre-rollout, and on a locally-run instance: null, not a fake SHA."""
        monkeypatch.delenv("GIT_COMMIT", raising=False)

        body = self._health(monkeypatch)
        assert "git_commit" in body
        assert body["git_commit"] is None
