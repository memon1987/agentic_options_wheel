"""Deploy-freshness check: merged-vs-deployed drift detection (FC-081 follow-up).

The build-failure alert (FC-030/FC-031) watches builds that *start*. FC-081 was
a repo rename that detached the Cloud Build trigger — **no build ever started**,
so no build failed, so nothing alerted, and `main` ran 16 days ahead of
production in silence. `check_deploy_freshness` closes that hole by comparing
what is serving (`GIT_COMMIT`) against what is on GitHub `main`.

Every test here stubs the GitHub getter (`RegressionMonitor._github_get`) and
the environment. Nothing touches the network — a detective control that needs
the internet to be tested is a detective control nobody runs in CI.

The two properties worth the most:

* **Drift beyond the window is `fail`** (test 2) — this is the FC-081 regression
  itself, and `fail` is what makes `/regression` return HTTP 500 and page.
* **Drift inside the window is `pass`** (test 3) — without this, every merge
  pages the operator for the 10-12 minutes a build takes, and the alert gets
  muted, which is how FC-031's red build went unseen in the first place.
"""

from datetime import datetime, timedelta, timezone

import pytest

import tools.testing.regression_monitor as rm
from tools.testing.regression_monitor import RegressionMonitor


HEAD_SHA = "a" * 39 + "1"
DEPLOYED_SHA = "b" * 39 + "2"


class _Resp:
    """Minimal stand-in for `requests.Response`."""

    def __init__(self, payload=None, status_code=200, raises=None):
        self._payload = payload
        self.status_code = status_code
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


def _commit_body(sha=HEAD_SHA, age_minutes=5.0):
    """A GitHub `GET /repos/{repo}/commits/main` body, `age_minutes` old."""
    committed = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
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
    """Capture `log_error_event` calls made by the check."""
    calls = []

    def _capture(logger, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(rm, "log_error_event", _capture)
    return calls


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


# ---------------------------------------------------------------------------
# 1. Match -> pass
# ---------------------------------------------------------------------------

def test_deployed_commit_matching_main_passes(env, logged_errors):
    env.setenv("GIT_COMMIT", HEAD_SHA)

    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    assert result.status == "pass"
    assert result.name == "deploy_freshness"
    assert result.details["head_sha"] == HEAD_SHA
    assert result.details["git_commit"] == HEAD_SHA
    assert not logged_errors, "a matching deploy must not emit an error event"


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
# 2. Drift beyond the window -> fail  (THE core regression)
# ---------------------------------------------------------------------------

def test_drift_beyond_the_window_fails_and_logs_the_event(env, logged_errors):
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


# ---------------------------------------------------------------------------
# 3. Drift inside the window -> pass ("build in flight")
# ---------------------------------------------------------------------------

def test_fresh_mismatch_is_a_build_in_flight_not_drift(env, logged_errors):
    """A merge 20 minutes ago is a build running, not a dead trigger."""
    result = run_check(env, response=_Resp(_commit_body(age_minutes=20)))

    assert result.status == "pass"
    assert result.name == "deploy_freshness"
    assert "in flight" in result.message
    assert not logged_errors, (
        "paging on every merge is how an alert gets muted — see FC-031"
    )


# ---------------------------------------------------------------------------
# 4. The window is env-overridable
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


def test_unparseable_window_falls_back_to_the_default(env):
    env.setenv("DEPLOY_FRESHNESS_MAX_HOURS", "not-a-number")

    result = run_check(env, response=_Resp(_commit_body(age_minutes=90)))

    assert result.details["max_hours"] == rm.DEPLOY_FRESHNESS_MAX_HOURS_DEFAULT
    assert result.status == "pass"


# ---------------------------------------------------------------------------
# 5. 404 -> fail (the FC-081 failure mode itself)
# ---------------------------------------------------------------------------

def test_repo_404_fails(env, logged_errors):
    """A renamed/moved repo is precisely what detached the trigger in FC-081."""
    result = run_check(env, response=_Resp({"message": "Not Found"}, status_code=404))

    assert result.status == "fail"
    assert result.name == "deploy_freshness_repo_unreachable"
    assert len(logged_errors) == 1
    assert logged_errors[0]["error_type"] == "deploy_freshness_repo_unreachable"


# ---------------------------------------------------------------------------
# 6. Transient GitHub trouble -> warn, never fail
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "response,raises",
    [
        (_Resp({"message": "Bad credentials"}, status_code=401), None),
        (_Resp({"message": "Forbidden"}, status_code=403), None),
        (_Resp({"message": "boom"}, status_code=503), None),
        (_Resp(raises=ValueError("Expecting value: line 1 column 1")), None),
        (_Resp({"commit": {}}), None),                      # no sha
        (_Resp({"sha": HEAD_SHA, "commit": {}}), None),     # no committer date
        (_Resp({"sha": HEAD_SHA,
                "commit": {"committer": {"date": "not-a-date"}}}), None),
        (None, TimeoutError("read timed out")),
        (None, ConnectionError("name resolution failed")),
    ],
    ids=[
        "401", "403", "503", "non-json-body", "no-sha", "no-date",
        "bad-date", "timeout", "connection-error",
    ],
)
def test_github_trouble_warns_never_fails(env, logged_errors, response, raises):
    """A GitHub outage must not 500 the hourly monitor."""
    result = run_check(env, response=response, raises=raises)

    assert result.status == "warn"
    assert result.name == "deploy_freshness_github_error"
    assert not logged_errors, "a transient GitHub error is not an alertable event"


# ---------------------------------------------------------------------------
# 7. Unconfigured -> warn, and the reporting order between the two
# ---------------------------------------------------------------------------

def test_missing_token_warns_unconfigured(env, logged_errors):
    """The pre-secret state: visible as a warn row, never a silent pass."""
    env.delenv("GITHUB_TOKEN", raising=False)

    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    assert result.status == "warn"
    assert result.name == "deploy_freshness_unconfigured"
    assert result.requested == [], "no token means no GitHub call"
    assert not logged_errors


def test_empty_token_is_treated_as_missing(env):
    env.setenv("GITHUB_TOKEN", "   ")

    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    assert result.name == "deploy_freshness_unconfigured"


def test_missing_git_commit_warns_no_commit(env):
    """Pre-rollout: the env var is not on the revision yet."""
    env.delenv("GIT_COMMIT", raising=False)

    result = run_check(env, response=_Resp(_commit_body(age_minutes=600)))

    assert result.status == "warn"
    assert result.name == "deploy_freshness_no_commit"
    assert result.details["git_commit"] is None
    assert result.requested == [], "nothing to compare means no GitHub call"


def test_git_commit_is_reported_before_the_token(env):
    """Both missing: report the more informative one.

    Without GIT_COMMIT there is nothing to compare even with a valid token, so
    provisioning the secret alone would not clear the warn.
    """
    env.delenv("GIT_COMMIT", raising=False)
    env.delenv("GITHUB_TOKEN", raising=False)

    result = run_check(env, response=_Resp(_commit_body()))

    assert result.name == "deploy_freshness_no_commit"


# ---------------------------------------------------------------------------
# 8. The group is registered in the report
# ---------------------------------------------------------------------------
#  (the enumeration contract lives in tests/test_regression_monitor_schema.py)


# ---------------------------------------------------------------------------
# 9. GITHUB_REPO override reaches the request URL
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
    hourly run.
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
