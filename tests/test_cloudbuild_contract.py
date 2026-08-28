"""Contract tests for `cloudbuild.yaml` — FC-084.

Every canary/promote chain must stay pinned to the revision the *current build*
created. Before FC-084 each `smoke-test-*` step identified "my canary" as
`gcloud run revisions list --limit=1` (newest-first) and each `promote-*` step
ran `update-traffic --to-latest`; neither is bound to this build. Two pushes to
`main` 60 s apart on 2026-08-28 produced overlapping builds and a real
`ABORTED: Conflict for resource` deploy failure — the race is reproduced, not
hypothetical (`docs/plans/fc-084.md`).

These tests are regression guards on the *deploy contract*, not on style:

* 1-2 — nobody restores the "whatever is newest" identification.
* 3    — a chain (wheel / covered-call / dashboard) is never edited so that its
         deploy, smoke and promote steps disagree about which revision file to use.
* 4-5  — the SUPERSEDED guard and the conflict retry are never quietly dropped.
* 6    — the "must NOT change" list (step ids, `waitFor` edges, `--set-env-vars`
         values) is pinned to a fixture captured from the pre-FC-084 file. The
         env lists matter most: `--set-env-vars` REPLACES the whole env set, so
         losing an entry silently strips a live env var from a running service.

There is no CI in this repo (`.github/` is absent), so the pytest suite — which
Cloud Build itself runs as step 1 — is the only enforcement vehicle.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CLOUDBUILD = REPO_ROOT / "cloudbuild.yaml"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cloudbuild_contract.json"

SERVICES = ("options-wheel-strategy", "covered-call-engine", "options-wheel-dashboard")

# Which step id plays each role, per service.
CHAINS = {
    "options-wheel-strategy": {
        "deploy": "deploy-bot-canary",
        "smoke": "smoke-test-bot",
        "promote": "promote-bot",
    },
    "covered-call-engine": {
        "deploy": "deploy-cc-canary",
        "smoke": "smoke-test-cc",
        "promote": "promote-cc",
    },
    "options-wheel-dashboard": {
        "deploy": "deploy-dashboard-canary",
        "smoke": "smoke-test-dashboard",
        "promote": "promote-dashboard",
    },
}

SET_ENV_VARS_RE = re.compile(r"--set-env-vars=(\S+)")


@pytest.fixture(scope="module")
def steps():
    doc = yaml.safe_load(CLOUDBUILD.read_text())
    return doc["steps"]


@pytest.fixture(scope="module")
def steps_by_id(steps):
    return {s["id"]: s for s in steps}


def step_text(step):
    """All string args of a step joined — the step's full command surface.

    Deploy steps now run through `entrypoint: bash` + `-c`, so a flag can live
    either as its own list item or inside the script body; this flattens both.
    """
    return "\n".join(a for a in (step.get("args") or []) if isinstance(a, str))


# --------------------------------------------------------------------------
# 1. No step may route traffic with --to-latest.
# --------------------------------------------------------------------------
def test_no_step_uses_to_latest(steps):
    offenders = [s["id"] for s in steps if "--to-latest" in step_text(s)]
    assert offenders == [], (
        "--to-latest routes traffic to whichever revision is newest, which under "
        "overlapping builds is another build's revision (FC-084). Promote by name "
        "with --to-revisions=$REV=100 instead. Offending steps: " + ", ".join(offenders)
    )


# --------------------------------------------------------------------------
# 2. No smoke test may identify the canary as "the newest revision".
# --------------------------------------------------------------------------
def test_smoke_tests_do_not_sample_newest_revision(steps):
    offenders = []
    for s in steps:
        if not s["id"].startswith("smoke-test-"):
            continue
        text = step_text(s)
        if "revisions list" in text and "--limit=1" in text:
            offenders.append(s["id"])
    assert offenders == [], (
        "`gcloud run revisions list --limit=1` returns the newest revision on the "
        "service, not this build's — under overlapping builds the smoke test then "
        "health-checks somebody else's deploy (FC-084). Describe the captured "
        "revision by name instead. Offending steps: " + ", ".join(offenders)
    )


# --------------------------------------------------------------------------
# 3. Each chain hands the same /workspace/rev-<svc>.txt from deploy to smoke to promote.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("service", SERVICES)
def test_chain_shares_one_revision_file(service, steps_by_id):
    rev_path = f"/workspace/rev-{service}.txt"
    roles = CHAINS[service]

    deploy = step_text(steps_by_id[roles["deploy"]])
    assert f"> {rev_path}" in deploy, (
        f"{roles['deploy']} must capture the revision it created to {rev_path} "
        "(status.latestCreatedRevisionName) — that file is how the rest of the "
        "chain knows which revision is THIS build's."
    )
    assert "status.latestCreatedRevisionName" in deploy, (
        f"{roles['deploy']} must read the revision name from "
        "status.latestCreatedRevisionName on the deploy result."
    )

    for role in ("smoke", "promote"):
        text = step_text(steps_by_id[roles[role]])
        assert rev_path in text, (
            f"{roles[role]} must read {rev_path} — the chain for {service} is "
            "inconsistent, so it would act on a revision this build did not create."
        )


# --------------------------------------------------------------------------
# 4. Every promote pins by name and keeps the superseded guard.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("service", SERVICES)
def test_promote_pins_revision_and_guards_supersede(service, steps_by_id):
    step_id = CHAINS[service]["promote"]
    text = step_text(steps_by_id[step_id])

    assert "--to-revisions=" in text, (
        f"{step_id} must route traffic by revision name (--to-revisions=$REV=100)."
    )
    assert "SUPERSEDED" in text, (
        f"{step_id} must keep the superseded guard: when the serving revision was "
        "created after this build's canary, a later build (or a deliberate "
        "rollback) has already won — leave traffic alone and exit 0 so the "
        "build-failure alert stays quiet (FC-084)."
    )
    assert "creationTimestamp" in text, (
        f"{step_id}'s superseded guard must compare metadata.creationTimestamp; "
        "ordering by creation time is the rule, not git ancestry (the Cloud Build "
        "checkout is shallow, and a rollback should win)."
    )


# --------------------------------------------------------------------------
# 5. Every deploy retries Cloud Run's optimistic-concurrency conflict.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("service", SERVICES)
def test_deploy_retries_version_conflict(service, steps_by_id):
    step_id = CHAINS[service]["deploy"]
    text = step_text(steps_by_id[step_id])

    assert "Conflict for resource" in text, (
        f"{step_id} must retry on Cloud Run's optimistic-concurrency error "
        "(\"ABORTED: Conflict for resource ...: version 'X' was specified but "
        "current version is 'Y'\"), which is exactly what a concurrently-merging "
        "build produces — observed on build 9aaf7d27, 2026-08-28 (FC-084)."
    )
    assert "sleep 20" in text, f"{step_id}'s conflict retry must back off 20s between attempts."


# --------------------------------------------------------------------------
# 6. The must-not-change list, pinned to the pre-FC-084 file.
# --------------------------------------------------------------------------
def test_step_ids_wait_for_and_env_vars_match_frozen_fixture(steps):
    frozen = json.loads(FIXTURE.read_text())

    actual_ids = [s["id"] for s in steps]
    assert actual_ids == frozen["step_ids"], (
        "Step ids (and their order) are part of the deploy contract — Cloud Build "
        "trigger config and the waitFor graph both key off them."
    )

    actual_wait = {s["id"]: s.get("waitFor") for s in steps}
    assert actual_wait == frozen["wait_for"], (
        "The waitFor graph changed. It encodes the ordering guarantees: the CC "
        "canary runs only after the wheel promoted, so a bad image trips on the "
        "wheel's canary first."
    )

    actual_env = {}
    for s in steps:
        found = SET_ENV_VARS_RE.findall(step_text(s))
        if found:
            actual_env[s["id"]] = sorted(found)
    assert actual_env == frozen["set_env_vars"], (
        "A --set-env-vars list changed. --set-env-vars REPLACES the entire literal "
        "env set on the service, so dropping or mistyping one entry silently strips "
        "a live env var (e.g. STRATEGY_CONFIG, which is the only thing that makes "
        "covered-call-engine a covered-call service). Update the fixture only "
        "together with a deliberate, reviewed env change."
    )
