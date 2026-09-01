"""Structural gate on the versioned alert policies in `deploy/monitoring/`.

FC-096 Phase A. These JSON files are the *source* an operator pastes into
`gcloud alpha monitoring policies create`, and until now nothing checked them —
so a malformed one is discovered when the paste fails, or worse, when it
succeeds and watches the wrong thing.

The specific defect that prompted this: a policy shipped with **two**
`conditionMatchedLog` conditions. A log-based alerting policy takes exactly one;
Cloud Monitoring rejects more. Every policy already in this directory is
single-condition, so the rule was being followed by imitation rather than by
anything that would notice when it stopped.

These are cheap, mechanical checks — shape, not judgement. They cannot tell you
a filter matches the right logs; the policy documentation and the check's own
tests are for that. What they can do is stop a file that could never have been
created from sitting in the tree looking deployed.
"""

import json
from pathlib import Path

import pytest

POLICY_DIR = Path(__file__).resolve().parent.parent / "deploy" / "monitoring"

# The channel every policy here notifies. Pinned so a typo'd or removed channel
# is a test failure rather than a policy that creates cleanly and pages nobody.
NOTIFICATION_CHANNEL = (
    "projects/gen-lang-client-0607444019/notificationChannels/"
    "10474915111056992031"
)


def policy_files():
    return sorted(POLICY_DIR.glob("*_alert_policy.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture(params=policy_files(), ids=lambda p: p.name)
def policy(request):
    return request.param, load(request.param)


def test_the_walk_finds_the_policies():
    """Guard the guard: an empty glob must not read as 'all policies pass'."""
    names = {p.name for p in policy_files()}
    assert len(names) >= 10, f"only found {sorted(names)} — the walk is broken"
    for required in (
        "build_failure_alert_policy.json",
        "deploy_freshness_alert_policy.json",
        "deploy_freshness_degraded_alert_policy.json",
        # FC-096 Phase A
        "job_failure_alert_policy.json",
        "lake_freshness_alert_policy.json",
        "lake_freshness_degraded_alert_policy.json",
    ):
        assert required in names, f"{required} is missing from {POLICY_DIR}"


def test_a_log_based_policy_has_exactly_one_condition(policy):
    """Cloud Monitoring rejects a log-based policy with more than one.

    This is the check that would have caught the FC-096 defect at author time
    instead of at paste time.
    """
    path, doc = policy
    conditions = doc.get("conditions") or []
    log_based = [c for c in conditions if "conditionMatchedLog" in c]
    if not log_based:
        return  # a metric-threshold policy; a different shape, not this rule
    assert len(conditions) == 1, (
        f"{path.name} declares {len(conditions)} conditions. A log-based "
        f"alerting policy takes exactly ONE conditionMatchedLog — the API "
        f"refuses the rest, so this file cannot be created as written. Widen "
        f"the single filter with OR instead."
    )


def test_every_policy_is_shaped_like_one(policy):
    path, doc = policy
    assert doc.get("displayName"), f"{path.name} has no displayName"
    assert doc.get("combiner") == "OR", f"{path.name}: combiner must be OR"
    assert doc.get("enabled") is True, f"{path.name} is not enabled"
    assert doc.get("conditions"), f"{path.name} declares no condition"
    for condition in doc["conditions"]:
        assert condition.get("displayName"), (
            f"{path.name}: every condition needs a displayName — it is what "
            f"the incident is titled with"
        )


def test_every_policy_notifies_the_real_channel(policy):
    """A policy with no channel fires into a console nobody opens."""
    path, doc = policy
    channels = doc.get("notificationChannels") or []
    assert channels == [NOTIFICATION_CHANNEL], (
        f"{path.name} notifies {channels}; expected the project's one channel. "
        f"An alert that pages nobody is indistinguishable from no alert."
    )


def test_every_policy_documents_itself(policy):
    """The doc block is the runbook; it is read at 2am, not at review time.

    Deliberately a presence check, not a length one. Some of the older policies
    here are two sentences and adequate; a length threshold would fail them for
    no defect and teach the next author to pad. What is not acceptable is a
    policy that fires with nothing to say.
    """
    path, doc = policy
    content = (doc.get("documentation") or {}).get("content") or ""
    assert content.strip(), (
        f"{path.name} has no documentation. It is what an operator sees in the "
        f"incident — say what fired, why it matters, and what to run next."
    )
    assert (doc["documentation"].get("mimeType") or "").startswith("text/"), (
        f"{path.name}: documentation needs a text mimeType"
    )


def test_a_log_filter_names_a_resource_type(policy):
    """An unscoped log filter matches the whole project."""
    path, doc = policy
    for condition in doc["conditions"]:
        matched = condition.get("conditionMatchedLog")
        if not matched:
            continue
        assert "resource.type=" in (matched.get("filter") or ""), (
            f"{path.name}: the log filter must scope itself with "
            f"resource.type= or it matches every log in the project"
        )


class TestTheFC096Policies:
    """The three this phase adds, and the properties each one exists for."""

    def test_the_job_policy_watches_all_three_jobs(self):
        doc = load(POLICY_DIR / "job_failure_alert_policy.json")
        f = doc["conditions"][0]["conditionMatchedLog"]["filter"]
        assert 'resource.type="cloud_run_job"' in f, (
            "no other policy in this directory watches Jobs — they match "
            "cloud_run_revision or build — which is the gap this closes"
        )
        for job in ("backtest-screen", "backtest-sweep", "data-backfill"):
            assert job in f, f"{job} is not watched"
        assert "severity>=ERROR" in f

    def test_the_stale_policy_matches_the_event_the_check_emits(self):
        """The policy and the check must agree on the event name.

        They are in different files and different languages; nothing but this
        stops one being renamed without the other.
        """
        import tools.testing.regression_monitor as rm

        doc = load(POLICY_DIR / "lake_freshness_alert_policy.json")
        f = doc["conditions"][0]["conditionMatchedLog"]["filter"]
        assert "lake_freshness_stale" in f
        source = __import__("inspect").getsource(rm.RegressionMonitor.check_lake_freshness)
        assert 'error_type="lake_freshness_stale"' in source, (
            "the check no longer emits the event this policy matches"
        )

    def test_the_degraded_policy_is_a_nag_not_a_page(self):
        """24h rate limit, exactly like its deploy-freshness twin.

        A degraded check is not an outage; mailing six times a day about one is
        how the whole channel gets filtered.
        """
        doc = load(POLICY_DIR / "lake_freshness_degraded_alert_policy.json")
        assert doc["alertStrategy"]["notificationRateLimit"]["period"] == "86400s"
        twin = load(POLICY_DIR / "deploy_freshness_degraded_alert_policy.json")
        assert (doc["alertStrategy"]["notificationRateLimit"]
                == twin["alertStrategy"]["notificationRateLimit"])
        f = doc["conditions"][0]["conditionMatchedLog"]["filter"]
        assert "lake_freshness_degraded" in f

    def test_the_degraded_policy_matches_every_reason_the_check_can_emit(self):
        """It matches on the EVENT, so a new `reason` is covered automatically.

        Pinned because the obvious "improvement" — filtering on specific
        reasons — would silently stop covering a reason added later, which is
        the failure mode a degraded-check nag exists to prevent.
        """
        doc = load(POLICY_DIR / "lake_freshness_degraded_alert_policy.json")
        f = doc["conditions"][0]["conditionMatchedLog"]["filter"]
        assert "jsonPayload.reason" not in f, (
            "match the event, not its reasons — a reason added later would "
            "stop being watched with nothing to show for it"
        )

    def test_both_lake_policies_watch_the_services_that_serve_regression(self):
        for name in ("lake_freshness_alert_policy.json",
                     "lake_freshness_degraded_alert_policy.json"):
            f = load(POLICY_DIR / name)["conditions"][0]["conditionMatchedLog"]["filter"]
            assert 'resource.type="cloud_run_revision"' in f, name
            assert "options-wheel-strategy" in f, name
            assert "covered-call-engine" in f, (
                f"{name}: the CC service reports a steady "
                "lake_freshness_no_universe warn by design, and the degraded "
                "policy is where that becomes visible rather than silent"
            )
