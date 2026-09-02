"""Contract tests for `cloudbuild.yaml` — FC-084 rev 2.

The deploy chain has three properties that are cheap to break by accident and
expensive to discover in production. All three came out of a real incident and
two adversarial reviews (`docs/plans/fc-084.md` rev 2):

1. **Builds of the trigger are serialized.** Two pushes to `main` 60 s apart on
   2026-08-28 produced overlapping builds and an `ABORTED: Conflict for resource`
   deploy failure. `serialize-builds` resolves the ordering from Cloud Build's own
   build list *before* any deploy runs, and a superseded build drops a
   `/workspace/superseded` marker that every later deploy/smoke/promote step honours.

2. **Each build deploys a revision it named itself**, via `--revision-suffix`.
   The rejected rev-1 mechanism read the name back from the deploy's own output
   (`status.latestCreatedRevisionName`) — gcloud re-reads the service after its Ready
   wait, so that field returns *another* build's revision when writes overlap. Proven
   live on 2026-08-27: build `046fd075` printed `revision [00530-wab]`, which build
   `0d9756c0` created.

3. **Traffic moves with `--to-latest`, never `--to-revisions`.** Pinning traffic to a
   revision removes `latestRevision: true` from the service's traffic spec permanently.
   Cloud Run then keeps that split for every later revision, so
   `gcloud run services update --update-env-vars ROLLER_ENABLED=false` creates a
   revision serving **0%** — every documented kill switch (`ROLLER_ENABLED`,
   `ROLLER_DRY_RUN`, `EARNINGS_ENABLED`; docs/CLAUDE.md §Development Notes) silently
   stops working. This is the finding that killed rev 1, and test 3 is what keeps it
   dead.

Test 5 additionally freezes the **full** `gcloud run deploy` flag set per service
against a fixture captured from `main` — a reviewer's finding that rev 1 pinned only
three flags, leaving the rest free to drift.

There is no CI in this repo (`.github/` is absent), so the pytest suite — which
Cloud Build itself runs as step 1 — is the only enforcement vehicle that runs.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CLOUDBUILD = REPO_ROOT / "cloudbuild.yaml"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cloudbuild_contract.json"

SERVICES = ("options-wheel-strategy", "covered-call-engine",
            "options-wheel-dashboard", "sim-service")

# The image each chain deploys, as it must appear in that promote's MY_IMAGE. The
# `:$COMMIT_SHA` tag is the load-bearing half: MY_IMAGE decides whether a revision this
# build did NOT create gets promoted, so an untagged (or `:latest`) value would match
# any revision built from this repo — including an older commit's — and the
# env-only-change branch would wave through code this build never tested. The
# covered-call engine deliberately runs the STRATEGY image (FC-075: one image, the
# profile is selected by STRATEGY_CONFIG).
PROMOTE_IMAGE = {
    "options-wheel-strategy": "options-wheel-strategy:$COMMIT_SHA",
    "covered-call-engine": "options-wheel-strategy:$COMMIT_SHA",
    "options-wheel-dashboard": "options-wheel-dashboard:$COMMIT_SHA",
    # The sim service runs the STRATEGY image too (FC-096 Phase B PR-c: one
    # image, the COMMAND selects the work) — `python deploy/sim_service.py`
    # instead of `deploy/cloud_run_server.py`.
    "sim-service": "options-wheel-strategy:$COMMIT_SHA",
}

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
    "sim-service": {
        "deploy": "deploy-sim-canary",
        "smoke": "smoke-test-sim",
        "promote": "promote-sim",
    },
}

# The three chains whose services carry PRODUCTION traffic. The Job deploys wait
# on these promotes and deliberately NOT on `promote-sim`: the Job steps sit at
# the tail precisely so a failure in a measurement tool cannot strand a
# production deploy, and `sim-service` is a measurement tool by the same
# definition. Making them wait on it would mean a broken sim chain also stops the
# sweep and backfill Jobs from picking up this build's image — the failure this
# whole ordering exists to prevent, arriving from the other direction.
PRODUCTION_SERVICES = ("options-wheel-strategy", "covered-call-engine",
                       "options-wheel-dashboard")

GATE_ID = "serialize-builds"
GATE_WAITS_FOR = "push-bot-image"
# The one step Cloud Build allows to omit `waitFor` (it implicitly waits on every
# step listed before it, and nothing is listed before index 0).
FIRST_STEP_ID = "run-tests"
MARKER_CHECK = '[ -f /workspace/superseded ] && { echo "skip: superseded"; exit 0; }'

# The fixture pins the deploy flags as they stand on `main`. FC-084 deliberately adds
# exactly one flag on top of that set, and this is where that exception is declared —
# the fixture stays a faithful snapshot of main rather than being edited to absorb the
# change, so a future flag addition still has to be justified here in review.
#
#   --revision-suffix : names the revision before the deploy, so the smoke test polls
#                       a revision this build owns (see module docstring, point 2).
#
# The gate/marker mechanics FC-084 adds are NOT deploy flags — they live in the step
# scripts and are pinned by tests 1-4 instead. Anything else appearing in, or
# disappearing from, the deploy flag set is drift and fails test 5.
ALLOWED_NEW_DEPLOY_FLAGS = {"--revision-suffix"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def load_steps(path=CLOUDBUILD):
    return yaml.safe_load(Path(path).read_text())["steps"]


def script_of(step):
    """The bash script of a `bash -c` step, or '' for an exec-form step."""
    args = step.get("args") or []
    if step.get("entrypoint") == "bash" and len(args) >= 2 and args[0] == "-c":
        return args[1]
    return ""


def step_text(step):
    """Every string arg joined — the step's full command surface."""
    return "\n".join(a for a in (step.get("args") or []) if isinstance(a, str))


def deploy_flags(step):
    """The flags of this step's `gcloud run deploy`, whichever form the step uses.

    Handles both the pre-FC-084 exec form (each flag its own `args` list item) and
    the current `bash -c` script form (a backslash-continued command inside a
    script), so the frozen fixture stays comparable across the restructure.
    """
    args = step.get("args") or []
    listed = [a for a in args if isinstance(a, str) and a.startswith("--")]
    if listed:
        return sorted(listed)

    script = script_of(step)
    lines = script.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if "gcloud run deploy" in ln), None
    )
    assert start is not None, f"no `gcloud run deploy` in step {step.get('id')}"

    joined = []
    i = start
    while i < len(lines):
        stripped = lines[i].rstrip()
        joined.append(stripped[:-1] if stripped.endswith("\\") else stripped)
        if not stripped.endswith("\\"):
            break
        i += 1

    command = " ".join(joined)
    # `;` / `then` / redirects tail the last line; shlex copes, we only keep flags.
    tokens = shlex.split(command, comments=False, posix=False)
    return sorted(t for t in tokens if t.startswith("--"))


def simulate_substitution(text):
    """Apply Cloud Build's substitution rules: `$$`->`$`, `$VAR`/`${VAR}`-> value.

    Raises on a substitution Cloud Build would not resolve — that is the bug this
    guards against, not an inconvenience.
    """
    builtins = {
        "PROJECT_ID": "test-project",
        "COMMIT_SHA": "abc1234def5678",
        "BUILD_ID": "7ab1f0a1-fef6-45e0-a84b-631cdca9a2f9",
        "SHORT_SHA": "abc1234",
    }
    out, i = [], 0
    while i < len(text):
        if text[i] == "$" and i + 1 < len(text):
            if text[i + 1] == "$":
                out.append("$")
                i += 2
                continue
            m = re.match(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", text[i:])
            if m:
                name = m.group(1)
                if name not in builtins:
                    raise AssertionError(
                        f"`${name}` would be substituted by Cloud Build, not by the "
                        f"shell. Shell variables must be written `$${{{name}}}`."
                    )
                out.append(builtins[name])
                i += m.end()
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def if_branch_body(script, opener):
    """The body of the `if <opener> ...; then` block, up to its matching `fi`.

    Assertions about *ordering* in the script text are not enough for branch
    behaviour: code on a path the branch skips still appears later in the file.
    """
    lines = script.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if opener in ln), None
    )
    assert start is not None, f"no line containing {opener!r}"
    indent = len(lines[start]) - len(lines[start].lstrip())
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        cur_indent = len(lines[j]) - len(lines[j].lstrip())
        if stripped == "fi" and cur_indent == indent:
            return "\n".join(lines[start + 1:j])
    raise AssertionError(f"unterminated if-block for {opener!r}")


def retry_loop_body(script):
    """The body of the `for attempt in 1 2 3; do ... done` loop in a script.

    Used to prove that a check lives INSIDE the retry, not once before it — the
    difference between re-evaluating state on every attempt and retrying blind.
    """
    start = script.index("for attempt in 1 2 3; do")
    end = script.rindex("done")
    assert end > start, "malformed retry loop"
    return script[start:end]


def extract_gate_program(script):
    """The `gate_decision.py` heredoc body from the serialize-builds script."""
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF\n", script, re.DOTALL)
    assert m, "serialize-builds must define gate_decision.py in a <<'PYEOF' heredoc"
    return m.group(1)


@pytest.fixture(scope="module")
def steps():
    return load_steps()


@pytest.fixture(scope="module")
def by_id(steps):
    return {s["id"]: s for s in steps}


@pytest.fixture(scope="module")
def frozen():
    return json.loads(FIXTURE.read_text())


def gated_step_ids():
    for roles in CHAINS.values():
        for role in ("deploy", "smoke", "promote"):
            yield roles[role]


# --------------------------------------------------------------------------
# 1. The serialization gate exists and every deploy waits on it.
# --------------------------------------------------------------------------
def test_serialize_builds_gate_exists_and_gates_every_deploy(by_id):
    assert GATE_ID in by_id, (
        "The `serialize-builds` step is the whole mechanism: without it two builds "
        "of the same trigger deploy concurrently and the newer commit can lose "
        "(reproduced 2026-08-28, builds 9aaf7d27 / a7c941ff)."
    )
    gate = by_id[GATE_ID]
    assert gate.get("waitFor") == [GATE_WAITS_FOR], (
        f"`serialize-builds` must waitFor ['{GATE_WAITS_FOR}']. Running it there rather "
        "than at ['-'] means a build that has to wait for an older one has already done "
        "its own test/build/push work, so only the deploy chain is serialized. It must "
        "not gain edges that would delay it behind the deploy chain either."
    )

    for service, roles in CHAINS.items():
        deploy_id = roles["deploy"]
        wait = by_id[deploy_id].get("waitFor") or []
        assert GATE_ID in wait, (
            f"{deploy_id} must waitFor {GATE_ID}; otherwise {service} deploys before "
            "the build ordering has been decided."
        )


def test_every_wait_for_id_is_defined_earlier_in_the_list(steps):
    """Cloud Build resolves `waitFor` against ids defined EARLIER in the list.

    A forward reference is not a runtime surprise — Cloud Build rejects the whole
    config with "depends on ... which has not been defined", so every build fails
    instantly. FC-084 rev 2 shipped exactly that bug: `serialize-builds` was listed
    first with `waitFor: ['push-bot-image']`.
    """
    seen = set()
    for step in steps:
        for dep in step.get("waitFor") or []:
            if dep == "-":
                continue
            assert dep in seen, (
                f"step `{step['id']}` waits on `{dep}`, which is not defined earlier "
                "in the steps list. Cloud Build rejects this config outright."
            )
        seen.add(step["id"])


def test_only_the_first_step_omits_wait_for(steps):
    """A step without `waitFor` implicitly waits on every step listed before it.

    That implicit edge is how the rev-2 ordering bug became a cycle: with the gate
    listed first, `run-tests` implicitly waited on the gate, and the gate waited on
    a push that waited on `run-tests`.
    """
    assert steps[0]["id"] == FIRST_STEP_ID, (
        f"the first step should be `{FIRST_STEP_ID}`; got `{steps[0]['id']}`"
    )
    implicit = [s["id"] for s in steps if "waitFor" not in s]
    assert implicit == [FIRST_STEP_ID], (
        f"only `{FIRST_STEP_ID}` may omit `waitFor`; these also omit it and would "
        f"silently wait on everything listed before them: {implicit}"
    )


# --------------------------------------------------------------------------
# 2. Every gated step honours the superseded marker, first thing.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("step_id", sorted(gated_step_ids()))
def test_gated_step_checks_superseded_marker_first(step_id, by_id):
    script = script_of(by_id[step_id])
    assert script, f"{step_id} must be a `bash -c` step to carry the marker check"

    first = next((ln.strip() for ln in script.splitlines() if ln.strip()), "")
    assert first == MARKER_CHECK, (
        f"{step_id} must begin with the marker check:\n  {MARKER_CHECK}\n"
        f"got:\n  {first}\n"
        "Cloud Build has no conditional steps — the marker is the only way a "
        "superseded build skips its deploy while still exiting 0."
    )


# --------------------------------------------------------------------------
# 3. Revision identity + promote mechanism (the rev-1 regression guard).
# --------------------------------------------------------------------------
@pytest.mark.parametrize("service", SERVICES)
def test_chain_pins_revision_and_promotes_to_latest(service, by_id):
    roles = CHAINS[service]
    rev_path = f"/workspace/rev-{service}.txt"

    deploy = script_of(by_id[roles["deploy"]])
    assert "--revision-suffix=" in deploy, (
        f"{roles['deploy']} must name its own revision with --revision-suffix. "
        "Reading the name back from the deploy's output is the rejected rev-1 "
        "mechanism: status.latestCreatedRevisionName is re-read after gcloud's Ready "
        "wait and returns another build's revision when writes overlap (proven live "
        "2026-08-27, build 046fd075)."
    )
    assert "run revisions describe" in deploy, (
        f"{roles['deploy']} must confirm the revision it named actually exists "
        "before the chain continues — a deploy can report success on a name the "
        "smoke test would then poll forever."
    )
    assert f"> {rev_path}" in deploy, (
        f"{roles['deploy']} must write the revision name to {rev_path}."
    )

    smoke = script_of(by_id[roles["smoke"]])
    assert rev_path in smoke, (
        f"{roles['smoke']} must read {rev_path} so it health-checks this build's "
        "revision, not whatever is newest on the service."
    )

    promote = script_of(by_id[roles["promote"]])
    assert rev_path in promote, f"{roles['promote']} must read {rev_path}."
    assert "latestReadyRevisionName" in promote, (
        f"{roles['promote']} must read status.latestReadyRevisionName and refuse to "
        "promote when it is not this build's revision."
    )
    assert f'"$${{LATEST}}" != "$${{REV}}"' in promote, (
        f"{roles['promote']} must keep the equality check between the latest ready "
        "revision and this build's revision, and fail loudly when they differ."
    )
    assert "--to-latest" in promote, (
        f"{roles['promote']} must promote with --to-latest — see the next assertion."
    )


@pytest.mark.parametrize("service", SERVICES)
def test_promote_promotes_an_env_only_change_on_this_builds_image(service, by_id):
    """A kill switch applied mid-build must not strand the service on old code.

    Between a chain's `--no-traffic` deploy and its promote, an operator running
    `gcloud run services update --update-env-vars ROLLER_ENABLED=false` creates a
    new 0%-traffic revision. A promote that refuses on any mismatch leaves the
    service pinned to the OLD revision with the kill switch NOT applied — the exact
    opposite of what the operator asked for. So the promote checks the image: same
    image as this build means it is that env-only change, and it gets promoted.
    """
    step_id = CHAINS[service]["promote"]
    script = script_of(by_id[step_id])
    loop = retry_loop_body(script)

    assert "latestReadyRevisionName" in loop, (
        f"{step_id} must read latestReadyRevisionName INSIDE the retry loop. Reading "
        "it once before the loop and then retrying update-traffic blind means a "
        "retry can promote a revision that appeared after the check."
    )
    assert "spec.containers[0].image" in loop, (
        f"{step_id} must compare the latest ready revision's image against this "
        "build's image, inside the retry loop."
    )
    assert "env-only change on this build's image" in loop, (
        f"{step_id} must log PROMOTING ... (env-only change on this build's image) "
        "when it promotes a revision it did not create because the image matches."
    )
    expected_image = PROMOTE_IMAGE[service]
    m = re.search(r'MY_IMAGE="([^"]+)"', script)
    assert m, f"{step_id} must pin the expected image in a double-quoted MY_IMAGE."
    assert m.group(1).endswith(expected_image), (
        f"{step_id}'s MY_IMAGE is {m.group(1)!r}; it must end with {expected_image!r}. "
        "Without the :$COMMIT_SHA tag the comparison matches any revision built from "
        "this repo — including an older commit's — so the env-only-change branch would "
        "promote code this build never tested."
    )

    # Match the LATEST assignment itself, not just any `services describe` — the
    # failure branch issues one too (for the serving revision), and matching that
    # would let a hoisted LATEST read slip through.
    read = "LATEST=$(gcloud run services describe"
    assert read in loop, (
        f"{step_id} must issue the `{read} ... latestReadyRevisionName` READ inside "
        "the `for attempt in 1 2 3` loop. Hoisting it above the loop means a retry "
        "reuses a stale answer and can promote a revision that appeared in between — "
        "which is retrying --to-latest blind, the thing this rule exists to prevent."
    )
    assert script.count(read) == 1, (
        f"{step_id} must read latestReadyRevisionName in exactly one place (inside "
        "the retry loop); a second copy outside it defeats the re-evaluation."
    )
    assert f"update-traffic {service} --to-latest" in script, (
        f"{step_id}'s failure message must give the operator the one-line remedy "
        f"`gcloud run services update-traffic {service} --to-latest`."
    )


def test_no_step_pins_traffic_or_samples_newest_revision(steps):
    """--to-revisions permanently breaks the --update-env-vars kill switches."""
    pinned = [s["id"] for s in steps if "--to-revisions" in step_text(s)]
    assert pinned == [], (
        "`update-traffic --to-revisions=REV=100` removes `latestRevision: true` from "
        "the service's traffic spec permanently. Cloud Run keeps that split for every "
        "later revision, so `gcloud run services update --update-env-vars "
        "ROLLER_ENABLED=false` creates a revision serving 0% and the kill switch "
        "silently stops working (docs/CLAUDE.md §Development Notes). Use --to-latest; "
        "serialization is what makes it safe. Offending steps: " + ", ".join(pinned)
    )

    sampled = [s["id"] for s in steps if "run revisions list" in step_text(s)]
    assert sampled == [], (
        "`gcloud run revisions list` returns revisions by recency, not ownership — "
        "that is how a smoke test ends up health-checking another build's deploy. "
        "Describe the revision this build named. Offending steps: " + ", ".join(sampled)
    )


# --------------------------------------------------------------------------
# 4. Deploy retry, including the already-exists path.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("service", SERVICES)
def test_deploy_retries_conflict_and_tolerates_existing_revision(service, by_id):
    step_id = CHAINS[service]["deploy"]
    script = script_of(by_id[step_id])

    assert "Conflict for resource" in script, (
        f"{step_id} must retry on Cloud Run's optimistic-concurrency error. With "
        "serialization this can only come from an out-of-band write (an operator "
        "`gcloud run services update` during a build), which is exactly when a build "
        "should retry rather than page someone."
    )
    assert "sleep 20" in script, f"{step_id}'s retry must back off 20s between attempts."
    assert "already exists" in script, (
        f"{step_id} retries with the SAME --revision-suffix. If the first attempt "
        "created the revision before erroring, the retry fails with 'already exists' "
        "— that is success, not failure, and must fall through to the existence check."
    )

    # The already-exists branch must CONTINUE into the shared verify-and-record path,
    # not finish the step itself. A branch that exits there leaves rev-<svc>.txt
    # unwritten, so the smoke test reads an empty revision name and polls nothing —
    # a green deploy step followed by a baffling downstream failure. Checking the
    # BRANCH BODY, not just text order: the clean path's describe/write appear later
    # in the script either way, so an ordering check alone passes on the broken code.
    branch = if_branch_body(script, "if grep -q 'already exists'")
    assert "exit" not in branch, (
        f"{step_id}'s 'already exists' branch must not exit — it must fall through to "
        f"the existence check and the /workspace/rev-{service}.txt write, which every "
        f"later step in the {service} chain depends on. Branch body:\n{branch}"
    )
    assert 'DEPLOYED="yes"' in branch and "break" in branch, (
        f"{step_id}'s 'already exists' branch must mark the deploy successful and "
        f"leave the retry loop. Branch body:\n{branch}"
    )

    # ...and the shared path it falls into does verify before recording.
    describe_at = script.index('run revisions describe "$${REV}"')
    write_at = script.index("> /workspace/rev-%s.txt" % service)
    assert describe_at < write_at, (
        f"{step_id} must confirm the revision exists BEFORE writing "
        f"/workspace/rev-{service}.txt; recording a revision that was never created "
        "just moves the failure to the smoke test."
    )


# --------------------------------------------------------------------------
# 5. The must-not-change list, frozen from `main`.
# --------------------------------------------------------------------------
def test_pre_existing_step_ids_and_order_preserved(steps, frozen):
    actual = [s["id"] for s in steps]
    kept = [sid for sid in actual if sid in set(frozen["step_ids"])]
    assert kept == frozen["step_ids"], (
        "Pre-existing step ids (and their relative order) are part of the deploy "
        "contract — the waitFor graph keys off them. New steps may be added; existing "
        "ones may not be renamed, reordered or dropped."
    )
    added = [sid for sid in actual if sid not in set(frozen["step_ids"])]
    assert added == [GATE_ID], (
        f"Only `{GATE_ID}` should be new relative to the frozen fixture; got {added}."
    )


def test_pre_existing_wait_for_edges_preserved(steps, frozen):
    actual = {s["id"]: (s.get("waitFor") or []) for s in steps}
    for step_id, frozen_wait in frozen["wait_for"].items():
        expected = set(frozen_wait or [])
        got = set(actual.get(step_id, []))
        assert expected <= got, (
            f"{step_id} lost waitFor edge(s) {sorted(expected - got)}. The graph "
            "encodes the ordering guarantees — notably that the CC canary runs only "
            "after the wheel promoted, so a bad image trips on the wheel first."
        )
        extra = got - expected
        assert extra <= {GATE_ID}, (
            f"{step_id} gained unexpected waitFor edge(s) {sorted(extra - {GATE_ID})}. "
            f"Only an edge on `{GATE_ID}` may be added to a pre-existing step."
        )


@pytest.mark.parametrize("service", SERVICES)
def test_every_deploy_flag_matches_frozen_fixture(service, by_id, frozen):
    step_id = CHAINS[service]["deploy"]
    actual = set(deploy_flags(by_id[step_id]))
    expected = set(frozen["deploy_flags"][step_id])

    missing = expected - actual
    assert not missing, (
        f"{step_id} dropped deploy flag(s): {sorted(missing)}. Every flag here is "
        "load-bearing — --set-env-vars REPLACES the whole env set, --no-traffic is "
        "what makes the deploy a canary, and the memory/cpu/instance flags are the "
        "service's sizing. A dropped flag reverts the service to a Cloud Run default."
    )

    added_names = {f.split("=", 1)[0] for f in actual - expected}
    assert added_names <= ALLOWED_NEW_DEPLOY_FLAGS, (
        f"{step_id} gained unreviewed deploy flag(s): "
        f"{sorted(added_names - ALLOWED_NEW_DEPLOY_FLAGS)}. Add it to the fixture in "
        "the same PR that introduces it, so the change is reviewed rather than absorbed."
    )


# --------------------------------------------------------------------------
# 5b. The scenario-sweep Cloud Run Job (FC-060 Layer 3, D9).
# --------------------------------------------------------------------------
SWEEP_JOB_STEP = "deploy-sweep-job"
SWEEP_JOB_NAME = "backtest-sweep"
BACKFILL_JOB_STEP = "deploy-backfill-job"
BACKFILL_JOB_NAME = "data-backfill"

# Every Cloud Run Job this build deploys. They are the build's tail steps as a
# SET — see `test_the_job_steps_are_the_tail_and_nothing_hides_behind_them`.
JOB_STEPS = {SWEEP_JOB_STEP, BACKFILL_JOB_STEP}


def test_sweep_job_step_honours_the_superseded_marker_first(by_id):
    """A superseded build must not push its image onto the sweep Job.

    The step is deliberately NOT gated by `serialize-builds` (it deploys no
    traffic and no revision, so two builds racing here leaves the newer image on
    the job either way) — which makes the marker the ONLY thing stopping an
    obsolete build from repointing the job at code the winning build already
    replaced. Dropping this line is silent: the step still exits 0.
    """
    assert SWEEP_JOB_STEP in by_id, (
        f"`{SWEEP_JOB_STEP}` is what creates and updates the `{SWEEP_JOB_NAME}` "
        "Cloud Run Job. Without it the job either does not exist (the dashboard's "
        "submit returns 404 forever) or sits on whatever image an operator last "
        "pinned — which is exactly how `backtest-screen` ended up on a Layer-1 "
        "image with no `sweep` command."
    )
    script = script_of(by_id[SWEEP_JOB_STEP])
    assert script, f"{SWEEP_JOB_STEP} must be a `bash -c` step to carry the marker check"
    first = next((ln.strip() for ln in script.splitlines() if ln.strip()), "")
    assert first == MARKER_CHECK, (
        f"{SWEEP_JOB_STEP} must begin with the marker check:\n  {MARKER_CHECK}\n"
        f"got:\n  {first}"
    )


def test_sweep_job_step_deploys_this_builds_image_with_the_sweep_command(by_id):
    """The job runs THIS build's image, in Job mode, on the wheel profile.

    `:$COMMIT_SHA` rather than `:latest` is the load-bearing half. An ad-hoc
    sweep exists to answer a question about the code as it is now, and its
    `sweep_key` (D4) hashes `GIT_COMMIT` — a job pinned to a floating tag would
    stamp one commit on results produced by another.
    """
    script = script_of(by_id[SWEEP_JOB_STEP])
    assert f"gcloud run jobs deploy {SWEEP_JOB_NAME}" in script, (
        f"{SWEEP_JOB_STEP} must `gcloud run jobs deploy {SWEEP_JOB_NAME}` — "
        "`deploy` is create-or-update, which is what lets one step both create "
        "the job and keep it current."
    )
    assert "options-wheel-strategy:$COMMIT_SHA" in script, (
        f"{SWEEP_JOB_STEP} must deploy the strategy image at :$COMMIT_SHA."
    )
    assert "--command=python" in script and (
        "--args=main.py,--command,sweep,--spec-env,SWEEP_SPEC_JSON" in script
    ), (
        f"{SWEEP_JOB_STEP} must run `python main.py --command sweep --spec-env "
        "SWEEP_SPEC_JSON`. The spec arrives as a per-execution env override "
        "(D2); a job whose args say otherwise ignores the submission entirely."
    )
    # `backtest-screen` is SHA-pinned on purpose and must stay untouched.
    assert "backtest-screen" not in script, (
        f"{SWEEP_JOB_STEP} must not touch `backtest-screen`: the monthly screen "
        "is deliberately pinned so it stays reproducible."
    )


def test_sweep_job_step_carries_every_env_var_and_secret_the_sweep_needs(by_id):
    """`--set-env-vars` REPLACES the whole set, so every var must be listed.

    Each of these is a distinct silent failure if dropped:
      GIT_COMMIT          -> every sweep keys as commit-less; dedup never hits.
      CHAIN_LAKE_BUCKET   -> every execution materialises cold (~20 min, not 7).
      GCP_PROJECT         -> the writer disables itself and the run is invisible.
      the three secrets   -> `Config()` raises before a single day is replayed.
    """
    script = script_of(by_id[SWEEP_JOB_STEP])
    for expected in ("ALPACA_PAPER_TRADING=true", "GCP_PROJECT=$PROJECT_ID",
                     "CHAIN_LAKE_BUCKET=options-wheel-chain-lake",
                     "GIT_COMMIT=$COMMIT_SHA"):
        assert expected in script, f"{SWEEP_JOB_STEP} must set {expected}"
    for secret in ("ALPACA_API_KEY=alpaca-api-key:latest",
                   "ALPACA_SECRET_KEY=alpaca-secret-key:latest",
                   "FINNHUB_API_KEY=finnhub-api-key:latest"):
        assert secret in script, f"{SWEEP_JOB_STEP} must bind {secret}"
    assert "--max-retries=0" in script, (
        f"{SWEEP_JOB_STEP} must set --max-retries=0. A retried sweep would replay "
        "under the SAME SWEEP_RUN_ID and write a second full set of "
        "`scenario_runs` rows for one run_id — the results view would then render "
        "each cell twice."
    )
    assert "--service-account=799970961417-compute@developer.gserviceaccount.com" in script


def test_sweep_job_task_timeout_matches_the_api(by_id):
    """The Job's `--task-timeout` IS the API's liveness clock.

    `services/sweeps.py` releases the one-at-a-time lock, and labels a `running`
    sweep as stuck, once the row is older than this number plus a grace — the
    reasoning being that Cloud Run has killed the task by then. If the two drift
    apart the API is wrong in one of two ways, both bad: a lower API number
    releases the lock while a legitimate sweep is still replaying (two
    executions then contend on one chain cache), and a higher one keeps the
    endpoint locked on a task that no longer exists.

    3h rather than 1h because a COLD window materialises at ~5.5 min/symbol
    (~50 s warm), so a 12-symbol year over unseeded chains is comfortably past
    an hour — and a timeout that fires on ordinary work teaches an operator to
    ignore it.
    """
    from tests._dashboard_path import add_dashboard_backend_to_path

    add_dashboard_backend_to_path()
    from services.sweeps import JOB_TASK_TIMEOUT_SECONDS

    script = script_of(by_id[SWEEP_JOB_STEP])
    assert f"--task-timeout={JOB_TASK_TIMEOUT_SECONDS}" in script, (
        f"{SWEEP_JOB_STEP}'s --task-timeout must equal "
        f"services/sweeps.JOB_TASK_TIMEOUT_SECONDS ({JOB_TASK_TIMEOUT_SECONDS})."
    )
    assert JOB_TASK_TIMEOUT_SECONDS >= 3600, (
        "a one-hour task timeout kills ordinary cold sweeps; see the step comment"
    )


def test_sweep_job_runs_after_every_service_promote(by_id, steps):
    """It must not be able to strand a service deploy.

    This step can fail for a reason that has nothing to do with the services —
    the build service account may lack `run.jobs.create` until an operator
    grants it in the console (rollout step 2). Placed earlier, or in parallel,
    that failure aborts the build and the wheel, covered-call and dashboard
    revisions never get their traffic: an ad-hoc measurement tool taking
    production deploys down with it. Behind the promotes it still fails the
    build loudly — the operator finds out — but only after the services are out.
    """
    wait = set(by_id[SWEEP_JOB_STEP].get("waitFor") or [])
    promotes = {CHAINS[svc]["promote"] for svc in PRODUCTION_SERVICES}
    assert promotes <= wait, (
        f"{SWEEP_JOB_STEP} must waitFor every promote step {sorted(promotes)}; "
        f"got {sorted(wait)}."
    )


def test_the_job_steps_are_the_tail_and_nothing_hides_behind_them(steps, by_id):
    """The Job deploys are the LAST steps, as a set (FC-096 Phase A).

    This was `ids[-1] == "deploy-sweep-job"` while there was exactly one Job.
    There are now two — `deploy-sweep-job` and `deploy-backfill-job` — and both
    belong at the tail for the same reason: either can fail on a
    `run.jobs.create` permission the build account has not been granted, and a
    step listed AFTER them inherits an implicit dependency on them, so that
    failure would strand whatever came next. Asserting the tail as a set keeps
    that guarantee while allowing a third Job to be added the same way.

    What it deliberately does NOT assert is their order relative to each other:
    they waitFor the same three promotes and Cloud Build runs them
    concurrently, so which one is listed first is not a property of the deploy.
    """
    ids = [st["id"] for st in steps]
    tail = set(ids[-len(JOB_STEPS):])
    assert tail == JOB_STEPS, (
        f"the final {len(JOB_STEPS)} steps must be exactly {sorted(JOB_STEPS)}; "
        f"got {sorted(tail)}. A `waitFor` edge alone is not enough — a step "
        "listed after a Job deploy inherits an implicit dependency on it, so a "
        "Job-permission failure would strand it."
    )
    promotes = {CHAINS[svc]["promote"] for svc in PRODUCTION_SERVICES}
    for step_id in sorted(JOB_STEPS):
        wait = set(by_id[step_id].get("waitFor") or [])
        assert promotes <= wait, (
            f"{step_id} must waitFor every promote step {sorted(promotes)}; "
            f"got {sorted(wait)}."
        )
        script = script_of(by_id[step_id])
        assert script.strip().startswith(MARKER_CHECK), (
            f"{step_id} must begin with the superseded-marker check — a Job "
            "deploy from a superseded build would leave the Job running an "
            "older commit's image than the services."
        )


# --------------------------------------------------------------------------
# 5c. The data-backfill Cloud Run Job (FC-096 Phase A).
# --------------------------------------------------------------------------
def test_backfill_job_step_deploys_this_builds_image_with_the_backfill_command(by_id):
    """The Job runs THIS build's image, on the wheel profile, in backfill mode.

    `:$COMMIT_SHA` and not `:latest`: what this Job writes is *vendor data*
    under a model fingerprint derived from the engine's own constants, so a Job
    floating on `:latest` could silently start writing chains under a different
    pricing model than the one the sweeps reading them were built with.
    """
    assert BACKFILL_JOB_STEP in by_id, (
        f"`{BACKFILL_JOB_STEP}` is what creates and updates the "
        f"`{BACKFILL_JOB_NAME}` Job; without it the weekly scheduler fires at a "
        "Job that does not exist, and `jobs:run` is async so the scheduler "
        "records success anyway."
    )
    script = script_of(by_id[BACKFILL_JOB_STEP])
    assert f"gcloud run jobs deploy {BACKFILL_JOB_NAME}" in script, (
        f"{BACKFILL_JOB_STEP} must `gcloud run jobs deploy {BACKFILL_JOB_NAME}`."
    )
    assert "options-wheel-strategy:$COMMIT_SHA" in script, (
        f"{BACKFILL_JOB_STEP} must deploy the strategy image at :$COMMIT_SHA."
    )
    assert "--command=python" in script and "--args=main.py,--command,backfill" in script, (
        f"{BACKFILL_JOB_STEP} must run `python main.py --command backfill`."
    )
    assert "backtest-screen" not in script and "backtest-sweep" not in script, (
        f"{BACKFILL_JOB_STEP} must not touch another Job's definition."
    )


def test_backfill_job_step_takes_no_baked_in_window(by_id):
    """The four BACKFILL_* overrides are per-EXECUTION, never per-deploy.

    A bare weekly execution is supposed to mean "the trailing 30 days of live +
    candidate symbols". Baking BACKFILL_SYMBOLS or BACKFILL_START into the Job
    definition would make what the Saturday scheduler does a property of
    whatever the last historical-widening deploy happened to set — and because
    `--set-env-vars` replaces the whole set, it would persist silently until
    someone noticed a symbol had stopped being refreshed.
    """
    script = script_of(by_id[BACKFILL_JOB_STEP])
    for var in ("BACKFILL_SYMBOLS", "BACKFILL_HISTORY_DAYS", "BACKFILL_START",
                "BACKFILL_END"):
        assert var not in script, (
            f"{BACKFILL_JOB_STEP} must not set {var}: it is a per-execution "
            "override (`gcloud run jobs execute --update-env-vars`), not part "
            "of the Job definition."
        )


def test_backfill_job_step_carries_every_env_var_and_secret_it_needs(by_id):
    """`--set-env-vars` REPLACES the whole set. Each omission fails silently:

      CHAIN_LAKE_BUCKET -> the run builds chains into a container filesystem
                           that is destroyed on exit. It reports success.
      GCP_PROJECT       -> the GCS client has no project to resolve.
      the three secrets -> `Config()` raises before a single bar is fetched.
    """
    script = script_of(by_id[BACKFILL_JOB_STEP])
    for expected in ("ALPACA_PAPER_TRADING=true", "GCP_PROJECT=$PROJECT_ID",
                     "CHAIN_LAKE_BUCKET=options-wheel-chain-lake",
                     "GIT_COMMIT=$COMMIT_SHA"):
        assert expected in script, f"{BACKFILL_JOB_STEP} must set {expected}"
    for secret in ("ALPACA_API_KEY=alpaca-api-key:latest",
                   "ALPACA_SECRET_KEY=alpaca-secret-key:latest",
                   "FINNHUB_API_KEY=finnhub-api-key:latest"):
        assert secret in script, f"{BACKFILL_JOB_STEP} must bind {secret}"
    assert "--service-account=799970961417-compute@developer.gserviceaccount.com" in script


def test_backfill_job_never_retries_and_gets_a_six_hour_task_timeout(by_id):
    """Sizing, both halves of it.

    `--max-retries=0`: a retried execution re-walks every day it already wrote,
    paying the lake round-trips twice, and can overlap the run it is retrying —
    two writers on one object, which is exactly the `if_generation_match` race
    the store's guard has to resolve.

    `--task-timeout=21600` (6h): the historical widening is chunked at roughly
    one symbol-year per execution, and the measured cold rate puts fourteen
    symbols over ~275 days at ~7.5h — i.e. it does NOT fit one execution, which
    is why the rollout chunks it. Six hours is the bound each chunk is sized
    against; the weekly freshness run finishes in minutes.
    """
    script = script_of(by_id[BACKFILL_JOB_STEP])
    assert "--max-retries=0" in script, (
        f"{BACKFILL_JOB_STEP} must set --max-retries=0."
    )
    assert "--task-timeout=21600" in script, (
        f"{BACKFILL_JOB_STEP} must set --task-timeout=21600 (6h); the rollout "
        "sizes every widening chunk against that number."
    )


def test_the_backfill_job_composes_the_weekly_battery(by_id):
    """`BACKFILL_THEN_BATTERY=true` is what makes the Saturday do both things.

    FC-096 Phase B B4. It lives on the Job DEFINITION rather than on the
    `backfill-weekly` scheduler on purpose — the scheduler needed no change at
    all, and a per-execution override would make what Saturday does a property
    of whoever last ran a chunked widening.

    Pinned HERE, on the literal `--set-env-vars` string, for the reason
    `CHAIN_LAKE_BUCKET` is: `--set-env-vars` REPLACES the whole env set, so an
    out-of-band `gcloud run jobs update` survives exactly until the next merge.
    Dropping it is silent in the worst way — the backfill still succeeds, the
    Job still exits 0, and the trend series simply stop gaining points.
    """
    script = script_of(by_id[BACKFILL_JOB_STEP])
    assert "BACKFILL_THEN_BATTERY=true" in script, (
        f"{BACKFILL_JOB_STEP} must set BACKFILL_THEN_BATTERY=true; without it "
        "the Saturday execution backfills and measures nothing, and nothing "
        "fails."
    )
    # It rides the backfill; it is NOT a second `--args` command.
    assert "--args=main.py,--command,backfill" in script, (
        "the Job's command stays `backfill` — the battery is composed inside "
        "main.py after a SUCCESSFUL backfill, which is what keeps the two exit "
        "classes apart (data failure pages; measurement failure nags)."
    )
    assert "--command,battery" not in script, (
        "a Job whose args said `battery` would run the battery INSTEAD of the "
        "backfill, against a lake nothing refreshed."
    )


def test_the_battery_wall_cap_fits_inside_the_backfill_jobs_timeout(by_id):
    """The sizing claim, pinned against the Job it actually shares.

    `main.BATTERY_MAX_SECONDS` bounds when a new sweep may START; the execution
    is killed at `--task-timeout`. If the cap ever met or exceeded the timeout,
    a long measurement week could take the BACKFILL's exit code with it — a
    successful data run reported as a Job failure, which is a page.
    """
    import main as cli

    script = script_of(by_id[BACKFILL_JOB_STEP])
    timeout = int(re.search(r"--task-timeout=(\d+)", script).group(1))
    assert cli.BATTERY_MAX_SECONDS < timeout
    assert timeout - cli.BATTERY_MAX_SECONDS >= 3600, (
        f"a {cli.BATTERY_MAX_SECONDS}s battery cap inside a {timeout}s "
        "execution leaves less than an hour for the backfill itself plus the "
        "longest single sweep"
    )


def test_the_wheel_service_carries_the_chain_lake_bucket(by_id):
    """`/regression`'s lake_freshness check cannot watch a bucket it is not told about.

    FC-096 Phase A. The check reads `CHAIN_LAKE_BUCKET`; without it every hourly
    run reports `lake_freshness_unconfigured` — a `warn`, so the report stays
    green, and the one control that notices a paused `backfill-weekly` scheduler
    is silently doing nothing. That is the same failure shape as FC-081: not an
    alarm that fired wrongly, but nothing watching at all.

    It is pinned HERE, on the literal `--set-env-vars` string, because
    `--set-env-vars` replaces the entire env set on every deploy. Setting it
    out-of-band with `gcloud run services update` works right up until the next
    merge silently removes it again.
    """
    script = script_of(by_id[CHAINS["options-wheel-strategy"]["deploy"]])
    assert "CHAIN_LAKE_BUCKET=options-wheel-chain-lake" in script, (
        "deploy-bot-canary must set CHAIN_LAKE_BUCKET; /regression's "
        "lake_freshness check reads it and warns 'unconfigured' without it."
    )


def test_the_covered_call_service_deliberately_has_no_lake_bucket(by_id):
    """The omission is a decision, so it is pinned like one.

    `cc-regression-hourly` runs the same monitor code against the covered-call
    profile, whose universe is holdings-derived — it has no `stocks.symbols` to
    check the lake against, so the check degrades to a
    `lake_freshness_no_universe` warn by design. The lake is the wheel's data
    and the wheel's check is its watcher; a second watcher measuring a different
    universe would only produce a second opinion nobody reconciles.

    Without this test, "the CC service has no CHAIN_LAKE_BUCKET" is
    indistinguishable from an oversight, and the obvious fix is to add it.
    """
    script = script_of(by_id[CHAINS["covered-call-engine"]["deploy"]])
    assert "CHAIN_LAKE_BUCKET" not in script, (
        "deploy-cc-canary must NOT set CHAIN_LAKE_BUCKET — see this test's "
        "docstring and check_lake_freshness's. If this is being changed "
        "deliberately, change the docstrings too."
    )


# --------------------------------------------------------------------------
# 5d. The interactive sim service (FC-096 Phase B PR-c).
# --------------------------------------------------------------------------
SIM_DEPLOY_STEP = "deploy-sim-canary"
SIM_SMOKE_STEP = "smoke-test-sim"


def test_sim_service_runs_the_strategy_image_on_the_sim_command(by_id):
    """One image; the COMMAND selects the work (FC-075's rule, FC-096's reuse).

    `:$COMMIT_SHA`, not `:latest`, for the reason every other chain pins it: the
    promote compares the latest ready revision's image against this build's, and
    an untagged value matches any revision built from this repo — including an
    older commit's.
    """
    script = script_of(by_id[SIM_DEPLOY_STEP])
    assert "options-wheel-strategy:$COMMIT_SHA" in script, (
        f"{SIM_DEPLOY_STEP} must deploy THIS build's strategy image."
    )
    assert "--command=python" in script and "--args=deploy/sim_service.py" in script, (
        f"{SIM_DEPLOY_STEP} must run `python deploy/sim_service.py`. Without the "
        "command override it would start `deploy/cloud_run_server.py` — the "
        "TRADING bot, on a service that is supposed to replay history."
    )


def test_sim_service_gets_instance_billing_and_serialised_concurrency(by_id):
    """The two flags without which this service is silently broken.

    `--no-cpu-throttling`: `/simulate` returns 202 and the replay continues on a
    worker thread. Under Cloud Run's default request-based billing the CPU is
    throttled the moment that response returns, and nothing ever supplies
    another in-flight request — the console polls BigQuery through the
    dashboard and never touches this service. The replay would not fail; it
    would crawl and then be scaled in. This is the plan's rev-3 blocker fix.

    `--concurrency=1`: the engine mutates process-global state during a replay
    (`ExecutionEngine._failed_symbols`, the frozen clock, stdlib logger levels,
    the analytics singleton). Two replays in one process do not crash; they
    corrupt each other's numbers.
    """
    flags = set(deploy_flags(by_id[SIM_DEPLOY_STEP]))
    assert "--no-cpu-throttling" in flags, (
        "deploy-sim-canary must pass --no-cpu-throttling. Without it the "
        "background replay starves as soon as the 202 is returned."
    )
    assert "--concurrency=1" in flags, (
        "deploy-sim-canary must pin --concurrency=1. The engine's process-"
        "global swaps are safe only serialized."
    )
    assert "--memory=2Gi" in flags and "--cpu=1" in flags, (
        "deploy-sim-canary must keep 2Gi/1cpu: the tmpfs bar and chain caches "
        "count against memory and 512Mi OOMs on a multi-symbol window."
    )
    assert "--no-allow-unauthenticated" in flags, (
        "the sim service is PRIVATE — invoker is the compute service account "
        "only. It holds Alpaca credentials and writes to the sweep store."
    )


def test_sim_service_carries_the_buckets_and_the_wheel_profile(by_id):
    """`--set-env-vars` REPLACES the env set, so everything it needs is listed.

    `CHAIN_LAKE_BUCKET` is not optional here the way it is for the covered-call
    service: without it `ChainStore.from_env()` returns a lake-less store, the
    coverage pre-flight has nothing to list, and every request is refused as
    uncoverable. `SIM_ARTIFACT_BUCKET` is where the per-cell detail artifacts
    go (PR-b). `STRATEGY_CONFIG` names the WHEEL profile explicitly — this
    service must never inherit whatever the image's default happens to be.
    """
    script = script_of(by_id[SIM_DEPLOY_STEP])
    for needed in ("CHAIN_LAKE_BUCKET=options-wheel-chain-lake",
                   "SIM_ARTIFACT_BUCKET=",
                   "STRATEGY_CONFIG=config/settings.yaml",
                   "GIT_COMMIT=$COMMIT_SHA",
                   "GCP_PROJECT=$PROJECT_ID"):
        assert needed in script, f"{SIM_DEPLOY_STEP} must set {needed}"
    for secret in ("ALPACA_API_KEY=alpaca-api-key:latest",
                   "ALPACA_SECRET_KEY=alpaca-secret-key:latest",
                   "FINNHUB_API_KEY=finnhub-api-key:latest"):
        assert secret in script, (
            f"{SIM_DEPLOY_STEP} must bind {secret}. Unlike the wheel and CC "
            "steps, this one CREATES its service, so a first deploy without "
            "the bindings produces a container whose Config() cannot validate."
        )


def test_the_dashboard_knows_where_the_sim_service_is(by_id):
    """The proxy fails closed on an absent `SIM_SERVICE_URL`, so it must be set.

    In the literal `--set-env-vars` list, because that flag replaces the whole
    env set: a value added out of band with `services update` survives exactly
    until the next merge, and the proxy would then 503 for every operator with
    a message about a variable somebody had already set.
    """
    script = script_of(by_id[CHAINS["options-wheel-dashboard"]["deploy"]])
    assert "SIM_SERVICE_URL=https://sim-service-" in script


def test_sim_chain_runs_behind_the_production_promotes(by_id, steps):
    """It must not be able to strand a production deploy.

    Same rule as the two Job steps, and for the same reason: this chain is new,
    it is the first thing here to use `--no-cpu-throttling`, and its smoke test
    makes an authenticated HTTP call. Any of those can fail for reasons that
    have nothing to do with the trading bot, and the failure must land after the
    wheel, covered-call and dashboard revisions have their traffic.
    """
    wait = set(by_id[SIM_DEPLOY_STEP].get("waitFor") or [])
    assert "promote-dashboard" in wait, (
        f"{SIM_DEPLOY_STEP} must waitFor promote-dashboard (the last production "
        f"promote); got {sorted(wait)}."
    )
    ids = [st["id"] for st in steps]
    for promote in (CHAINS[svc]["promote"] for svc in PRODUCTION_SERVICES):
        assert ids.index(promote) < ids.index(SIM_DEPLOY_STEP)


def test_the_job_deploys_do_not_wait_on_the_sim_chain(by_id):
    """...and the ordering does not run the other way either.

    The Job steps are at the tail so a measurement tool cannot strand a
    production deploy. `sim-service` IS a measurement tool, so making the Jobs
    wait on `promote-sim` would let a broken sim chain stop the sweep and
    backfill Jobs from picking up this build's image — the same failure, from
    the other direction.
    """
    for step_id in sorted(JOB_STEPS):
        wait = set(by_id[step_id].get("waitFor") or [])
        assert "promote-sim" not in wait, (
            f"{step_id} must NOT waitFor promote-sim; see this test's docstring."
        )


def test_sim_smoke_polls_readiness_then_probes_health_and_simulate(by_id):
    """The smoke's three assertions, and the two places it deliberately degrades.

    HARD: this build's revision reaches Ready. For an ASGI service that means
    uvicorn bound the port and `create_app()` imported, which is where a broken
    dependency, a syntax error or a missing secret actually surfaces.

    HARD: `/health` returns a NON-EMPTY `engine_identity`. An empty one means
    the service cannot hash its own tree, and the dashboard's baked value would
    then be keying against nothing — the dedup would silently stop firing.

    HARD: `/simulate` answers 200 (dedup hit) or 202 (accepted). BOTH, because
    the first deploy and a fresh project have no seeded spec to hit; the strict
    dedup-hit assertion is rollout verification, not a deploy gate.

    DEGRADES (named, greppable, exit 0): if an OIDC identity token cannot be
    minted from inside Cloud Build — a capability this project has never
    exercised — the HTTP half is skipped with `SMOKE_SIM_HTTP_UNAVAILABLE`
    rather than turning every merge red over an unproven capability. And a 409
    is reported as `SMOKE_SIM_SPEC_UNCOVERED`: the service answered correctly
    and the smoke spec names a window the lake does not hold, which is a data
    fact, not a deploy defect. Rollout step 3 checks this step's log for
    `PASS: /health` and resolves either marker if it appears.
    """
    script = script_of(by_id[SIM_SMOKE_STEP])
    assert "/workspace/rev-sim-service.txt" in script, (
        f"{SIM_SMOKE_STEP} must poll THIS build's revision, not whatever is "
        "newest on the service."
    )
    assert "'Ready'" in script and "seq 1 30" in script, (
        f"{SIM_SMOKE_STEP} must poll for the Ready condition rather than "
        "sampling once — a revision still coming up reports Unknown."
    )
    assert "/health" in script and "engine_identity" in script
    assert "FAIL: /health returned no engine_identity" in script, (
        f"{SIM_SMOKE_STEP} must FAIL on an empty identity, not merely print it."
    )
    assert "/simulate" in script
    for code, label in (("200)", "dedup hit"), ("202)", "accepted")):
        assert code in script, (
            f"{SIM_SMOKE_STEP} must accept {code[:-1]} ({label})."
        )
    assert "SMOKE_SIM_HTTP_UNAVAILABLE" in script, (
        f"{SIM_SMOKE_STEP}'s token-minting degrade must be NAMED, so a build "
        "log can be grepped for a control that ran nothing."
    )
    assert "SMOKE_SIM_SPEC_UNCOVERED" in script
    assert "-H 'X-Sim-Provenance: smoke'" in script, (
        f"{SIM_SMOKE_STEP} must label its rows. Its POST is a dedup hit only "
        "while the engine identity has not moved, so every build touching "
        "`src/**` or `requirements.txt` makes it a REAL replay writing real "
        "rows — and an unlabelled build artifact reads as an operator's "
        "experiment in the battery and the trend view."
    )


def test_sim_smoke_probes_unauthenticated_before_it_blames_the_identity(by_id):
    """The IAM/identity confusion the review caught, closed.

    A 403 on the authenticated `/health` used to fall through to "no
    engine_identity" — a message about hashing, for what is an invoker grant.
    The unauthenticated probe runs FIRST and is a HARD gate that separates the
    two states the authed call cannot:

    * ``000`` (refused/DNS/timeout) -> the service is not serving. FAIL.
    * ``401``/``403``               -> up, and IAM is enforcing. Proceed.
    * ``200``                       -> the service is answering ANONYMOUS
      callers, on a container holding broker credentials and writing to the
      sweep store. FAIL, loudly — `--no-allow-unauthenticated` is in the deploy
      flags and something has overridden it.

    Only then, if the authed call 403s, the failure names the missing grant and
    the command that fixes it.
    """
    script = script_of(by_id[SIM_SMOKE_STEP])
    # The ASSIGNMENT must be the curl itself. Asserting only that the block
    # mentions curl lets `ANON="401"` sit above a commented-out request and the
    # gate becomes a constant — which is a control that watches nothing.
    assert re.search(r"^\s*ANON=\$\(curl\b", script, re.M), (
        "smoke-test-sim's unauthenticated probe must actually issue the request "
        "it gates on"
    )
    anon = script[script.index("ANON="):script.index("TOKEN=")]
    assert "Authorization" not in anon, (
        "the first probe must be UNAUTHENTICATED; that is the whole point"
    )
    for state in ("401|403)", "200)", "000)"):
        assert state in anon, f"the probe must distinguish {state}"
    assert "not serving" in anon or "not up" in anon.lower()
    assert "made it public" in anon, (
        "a private service answering anonymously is a security defect, not a "
        "curiosity"
    )
    assert "BUILD service account is not an invoker" in script
    assert "roles/run.invoker" in script
    assert "799970961417@cloudbuild.gserviceaccount.com" in script, (
        "the failure has to name the exact member an operator must grant"
    )
    assert "NOT a missing engine identity" in script


def test_sim_smoke_separates_the_three_409s(by_id):
    """Busy is not a lake gap, and the log line rollout greps must not say it is.

    Three different 409s reach this step: BUSY (a replay of this spec is in
    flight — names a run_id), COVERAGE (the smoke spec's window is not in the
    lake — carries `missing_symbol_days`), and BUDGET (the smoke spec has grown
    too big to be a smoke, which is a real defect and fails).
    """
    script = script_of(by_id[SIM_SMOKE_STEP])
    assert "missing_symbol_days" in script
    assert "SMOKE_SIM_BUSY" in script
    assert "not a smoke" in script, (
        "an unexplained 409 must FAIL — otherwise a smoke spec that outgrew the "
        "service's own budget would report for ever as a lake gap"
    )


def test_the_smoke_spec_exists_and_is_small_enough_to_be_a_smoke():
    """The file the smoke POSTs. A spec too big for the service is not a smoke.

    One symbol, no declared arms: one cell, and an estimate far under
    `SIM_MAX_ESTIMATE_SECONDS`. If it ever grew past the service's own budget
    the smoke would start reporting a 409 for the wrong reason and the
    `SMOKE_SIM_SPEC_UNCOVERED` marker would stop meaning what it says.
    """
    spec = json.loads((REPO_ROOT / "deploy" / "smoke_sim_spec.json").read_text())
    assert spec["symbols"] and len(spec["symbols"]) == 1
    assert spec.get("scenarios") == []
    assert spec.get("run_sensitivity") is False, (
        "the smoke spec must not ask for the sensitivity pass — the service "
        "refuses it with a 422, which the smoke reports as a FAIL."
    )
    assert spec["start"] < spec["end"]
    script = script_of(load_steps()[0])  # touch the loader so a rename fails here
    assert isinstance(script, str)


def test_dashboard_service_knows_the_sweep_job_name(by_id):
    """The API cannot launch a job it cannot name.

    Pinned here rather than left to a code default because the default and the
    deployed job name drifting apart produces a 404 from `jobs.run` that reads
    like a permissions problem.
    """
    script = script_of(by_id[CHAINS["options-wheel-dashboard"]["deploy"]])
    assert f"SWEEP_JOB_NAME={SWEEP_JOB_NAME}" in script


def test_dashboard_image_ships_the_pure_scenario_modules():
    """The sweep API must refuse exactly what the sweep Job refuses (D7).

    Two halves, and both are needed:

    1. The dashboard build context is the REPO ROOT — `dashboard/` alone cannot
       reach `src/`, so a `dir: dashboard` build makes the COPY below impossible.
    2. `dashboard/Dockerfile` copies the two stdlib-only modules to the FLAT
       names `services/sweeps.py` falls back to.

    Break either and the image builds a dashboard whose allowlist is whatever
    `services/sweeps.py`'s fallback import happens to find — which, if someone
    later adds a same-named module, is worse than an ImportError.
    """
    step = next(s for s in load_steps() if s["id"] == "build-dashboard-image")
    assert "dir" not in step, (
        "build-dashboard-image must build from the REPO ROOT (no `dir:`), so the "
        "image can copy src/backtesting/scenarios/*.py. See dashboard/Dockerfile."
    )
    # The step became a `bash -c` script in FC-096 Phase B (it has to read the
    # ENGINE_IDENTITY build-arg out of /workspace), so the invocation is asserted
    # on the script text rather than on the exec-form arg list.
    script = script_of(step)
    assert script, "build-dashboard-image must be a `bash -c` step"
    assert "-f dashboard/Dockerfile" in script, script
    assert re.search(r"^\s*\.\s*$", script, re.M), (
        "the build context must be the repo root — the trailing `.` of the "
        f"docker build is missing:\n{script}"
    )

    dockerfile = (REPO_ROOT / "dashboard" / "Dockerfile").read_text()
    for src, dest in (
        ("src/backtesting/scenarios/overrides.py", "./scenario_overrides.py"),
        ("src/backtesting/scenarios/identity.py", "./scenario_identity.py"),
    ):
        assert f"COPY {src} {dest}" in dockerfile, (
            f"dashboard/Dockerfile must `COPY {src} {dest}` — "
            "dashboard/backend/services/sweeps.py imports the flat name as its "
            "fallback, and tests/test_dashboard_sweeps.py pins that name."
        )


# --------------------------------------------------------------------------
# 5b. The engine identity reaches the dashboard image (FC-096 Phase B PR-a).
# --------------------------------------------------------------------------
IDENTITY_STEP = "compute-engine-identity"
IDENTITY_MODULE = "src/backtesting/scenarios/engine_identity.py"
IDENTITY_FILE = "/workspace/engine_identity.txt"


def test_the_identity_step_invokes_the_shared_module_not_a_reimplementation():
    """Parity by INVOCATION. A second hash implementation would drift silently.

    The dashboard would then compute a `sweep_key` that matches nothing the Job
    ever writes and the dedup would simply stop firing — no error, no log, just
    a full replay on every repeat submission for ever. So the build is required
    to execute the same file the engine imports.

    It must execute the FILE, not `python -c "from src.backtesting.scenarios…"`:
    that package's `__init__.py` imports the runner, which imports pandas and the
    Alpaca SDK, so the import form needs the engine's whole dependency set and
    this step deliberately has no pip install.
    """
    step = next(s for s in load_steps() if s["id"] == IDENTITY_STEP)
    script = script_of(step)
    assert script, f"{IDENTITY_STEP} must be a `bash -c` step"
    assert f"python {IDENTITY_MODULE}" in script, (
        f"{IDENTITY_STEP} must run the shared module as a file:\n{script}"
    )
    assert "hashlib" not in script and "sha256" not in script, (
        f"{IDENTITY_STEP} must not compute the hash itself — invoke "
        f"{IDENTITY_MODULE}. A yaml-side reimplementation drifts, and its drift "
        f"is silent.\n{script}"
    )
    assert IDENTITY_FILE in script, (
        f"{IDENTITY_STEP} must write its answer to {IDENTITY_FILE}, which is "
        "where build-dashboard-image reads it."
    )


def test_the_shared_module_has_the_main_entry_point_the_build_calls():
    """The build's invocation and the module's entry point are one contract.

    Asserted on the source rather than by running it, because the interesting
    failure is someone deleting the `__main__` block while the module still
    imports fine everywhere else — at which point the step prints nothing, the
    build-arg is empty and the dashboard build fails on a message about a
    disabled dedup hint.
    """
    module = (REPO_ROOT / IDENTITY_MODULE).read_text()
    assert 'if __name__ == "__main__":' in module, (
        f"{IDENTITY_MODULE} must keep its `__main__` block: cloudbuild.yaml's "
        f"`{IDENTITY_STEP}` runs the file directly."
    )
    assert "engine_identity()" in module.split('if __name__ == "__main__":')[1]


def test_the_dashboard_build_bakes_the_identity_and_refuses_an_empty_one():
    """An empty build-arg must fail the build, not ship a crippled dashboard.

    A dashboard image with no `ENGINE_IDENTITY` cannot compute `sweep_key` at
    all: it disables its dedup hint for the life of the revision and says so
    once in the logs. That is the right runtime posture and the wrong build
    posture — at build time the fix is trivial and the failure should be loud.
    """
    step = next(s for s in load_steps() if s["id"] == "build-dashboard-image")
    script = script_of(step)
    assert "--build-arg ENGINE_IDENTITY=" in script, script
    assert IDENTITY_FILE in script, (
        "build-dashboard-image must read the identity computed by "
        f"{IDENTITY_STEP} out of {IDENTITY_FILE}"
    )
    assert "exit 1" in script, (
        "build-dashboard-image must FAIL on an empty identity rather than "
        f"baking one:\n{script}"
    )
    assert IDENTITY_STEP in (step.get("waitFor") or []), (
        "build-dashboard-image must waitFor the step that writes the file it "
        "reads; Cloud Build runs unrelated steps concurrently."
    )

    dockerfile = (REPO_ROOT / "dashboard" / "Dockerfile").read_text()
    assert "ARG ENGINE_IDENTITY" in dockerfile, (
        "dashboard/Dockerfile must declare the build-arg, or docker warns and "
        "drops it and the ENV is never set."
    )
    assert "ENV ENGINE_IDENTITY=$ENGINE_IDENTITY" in dockerfile, (
        "the build-arg must become an ENV: `services/sweeps.py` reads it out of "
        "os.environ at request time, not at build time."
    )


def _gitignore_entries():
    """The `.gitignore` patterns, comments and blank lines removed.

    Parsed rather than substring-matched: `"engine_identity.txt" in text` is
    satisfied by a COMMENT mentioning the file, so a rule that was deleted but
    still described in prose would read as present.
    """
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    return {ln.strip() for ln in lines
            if ln.strip() and not ln.strip().startswith("#")}


def test_the_identity_file_is_ignored_and_untracked():
    """A COMMITTED `engine_identity.txt` would bake a stale identity into the
    dashboard image; a GENERATED one is the build working correctly.

    The first version of this test asserted `not (REPO_ROOT /
    "engine_identity.txt").exists()` and turned main red on every build. In
    Cloud Build the repo root IS `/workspace`, `compute-engine-identity` has
    `waitFor: ['-']` so it writes the file immediately, and `run-tests` then
    runs for two minutes in that same directory. `exists()` cannot tell the two
    cases apart — it was asserting the absence of the very artifact the step
    this suite pins is supposed to produce. It passed locally only because no
    build step runs there.

    So the property is stated directly instead: the file is IGNORED, and git is
    not tracking it. Both are true whether or not a build has just written one.

    (The gate's own runtime files — `superseded`, `gate_decision.py`, the two
    gate JSONs — keep their `exists()` check and are not affected: their writer,
    `serialize-builds`, waits on `push-bot-image`, so it cannot have run before
    `run-tests`. Ordering is what makes that assertion safe, and this step's
    `waitFor: ['-']` is exactly what took it away.)
    """
    assert "engine_identity.txt" in _gitignore_entries(), (
        ".gitignore must ignore `engine_identity.txt`: Cloud Build writes it "
        "into /workspace, which is the repo root there, so without the rule a "
        "`git add -A` in any agent or developer session would commit a stale "
        "identity that build-dashboard-image would then bake into the image."
    )

    # Part (b) needs BOTH a .git checkout AND a git binary: Cloud Build's

    # python:3.11-slim run-tests image ships a .git directory in /workspace

    # but no git executable (FileNotFoundError crashed build ad532ab0 —

    # the fifth ambient-environment flavor). Absent either, part (a)

    # (the parsed .gitignore rule) still guards, everywhere.

    if shutil.which("git") is None:

        return


    if not (REPO_ROOT / ".git").exists():
        # Cloud Build checks the repo out as a TARBALL — no `.git` at all — so
        # there is nothing to interrogate and nothing that could have committed
        # anything. Skipped silently rather than xfailed: this is a normal,
        # expected environment, not a degraded one. The ignore assertion above
        # is NOT skipped, and it is the half that can actually regress.
        return

    tracked = subprocess.run(
        ["git", "ls-files", "engine_identity.txt"],
        cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert tracked.returncode == 0, tracked.stderr
    assert tracked.stdout.strip() == "", (
        "engine_identity.txt is TRACKED by git. It is a build artifact: "
        "cloudbuild.yaml's `compute-engine-identity` step writes it into "
        "/workspace, and build-dashboard-image reads it back. A committed copy "
        "would be checked out over it and could bake a stale engine identity "
        "into the dashboard image — the exact outcome the build's empty-value "
        "check exists to prevent. Remove it: `git rm --cached engine_identity.txt`."
    )


def test_gate_status_filter_covers_pending_builds(by_id):
    """A build sitting in PENDING is a real competitor and must be counted.

    Filtering only QUEUED/WORKING makes a PENDING sibling invisible: this build sees
    itself alone, PROCEEDs, and deploys straight into the race the gate exists to
    remove.
    """
    script = script_of(by_id[GATE_ID])
    m = re.search(r"status:\(([^)]*)\)", script)
    assert m, "serialize-builds must filter `gcloud builds list` on build status"
    statuses = {x.strip() for x in m.group(1).split(",")}
    assert {"PENDING", "QUEUED", "WORKING"} <= statuses, (
        f"gate status filter is {sorted(statuses)}; it must include PENDING, QUEUED "
        "and WORKING — every state in which another build may still deploy."
    )


def test_gate_deadline_tracks_the_build_timeout(by_id):
    """The gate gives up 90s before Cloud Build would, so its message wins.

    If `timeout:` is raised and BUILD_TIMEOUT_SECONDS is not, the gate quits early
    and throws away exactly the headroom the raise bought; if it is lowered and the
    constant is not, the build dies with an opaque TIMEOUT instead of the gate's
    explanation of which build it was waiting for.
    """
    doc = yaml.safe_load(CLOUDBUILD.read_text())
    timeout_seconds = int(str(doc["timeout"]).rstrip("s"))

    script = script_of(by_id[GATE_ID])
    m = re.search(r"BUILD_TIMEOUT_SECONDS=(\d+)", script)
    assert m, "serialize-builds must define BUILD_TIMEOUT_SECONDS"
    assert int(m.group(1)) == timeout_seconds, (
        f"BUILD_TIMEOUT_SECONDS={m.group(1)} but the build `timeout:` is "
        f"{timeout_seconds}s — they must match."
    )
    assert "- 90" in script, "the gate must give up 90s before the build timeout"


def test_build_timeout_matches_frozen_fixture(frozen):
    """The build timeout is pinned like any other must-not-change value.

    FC-084 deliberately raises it from main's 1200s to 1500s: an overlapping pair
    needs ~1158s worst case (wait <=798s for the older build + ~360s for this
    build's own deploy chain), which left only ~42s of headroom. The fixture records
    the NEW value, and this is the one place the fixture intentionally departs from
    main — see `_deviations` in the fixture file.
    """
    doc = yaml.safe_load(CLOUDBUILD.read_text())
    assert str(doc["timeout"]) == frozen["timeout"], (
        f"build timeout is {doc['timeout']}, fixture pins {frozen['timeout']}. "
        "Changing it is a deploy-behaviour change: update the fixture in the same "
        "reviewed commit, and BUILD_TIMEOUT_SECONDS in serialize-builds with it."
    )


# --------------------------------------------------------------------------
# 6. Every script is valid bash once Cloud Build has substituted it.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "step_id", [s["id"] for s in load_steps() if script_of(s)]
)
def test_script_is_valid_bash_after_substitution(step_id, by_id):
    """Catches the `$VAR` vs `$${VAR}` trap — Cloud Build eats the former.

    A shell variable written `$VAR` is consumed by Cloud Build's substitution pass
    before bash ever sees it, and an unknown key fails the build at parse time.
    `simulate_substitution` raises on any bare `$IDENT` outside the built-in set,
    which is the real assertion here; `bash -n` then catches quoting and heredoc
    mistakes in what is left.
    """
    script = simulate_substitution(script_of(by_id[step_id]))
    for env_entry in by_id[step_id].get("env") or []:
        simulate_substitution(env_entry)

    result = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True
    )
    assert result.returncode == 0, (
        f"{step_id}'s script is not valid bash after substitution:\n{result.stderr}"
    )


@pytest.mark.parametrize(
    "name", ["superseded", "gate_decision.py", "gate-me.json", "gate-others.json"]
)
def test_gate_runtime_files_are_not_committed(name):
    """These live in /workspace at build time and must never be tracked files.

    Cloud Build copies the repo into /workspace, so a committed file named
    `superseded` at the repo root would make EVERY build skip EVERY deploy while
    still reporting SUCCESS — a silent, total deploy outage.
    """
    assert not (REPO_ROOT / name).exists(), (
        f"`{name}` must not exist at the repo root: Cloud Build checks the repo out "
        "into /workspace, where the gate writes this file at runtime. A committed "
        "copy would be read as if the gate had written it."
    )


def test_no_committed_revision_files():
    """Same hazard as above for the per-service revision hand-off files."""
    stray = sorted(p.name for p in REPO_ROOT.glob("rev-*.txt"))
    assert stray == [], (
        f"revision hand-off files must not be committed at the repo root: {stray}. "
        "They are written into /workspace by each deploy step at build time."
    )


# --------------------------------------------------------------------------
# 7. The gate's decision logic, unit-tested directly.
# --------------------------------------------------------------------------
def run_gate_decision(program, my_id, my_create_time, others):
    env = dict(os.environ, MY_BUILD_ID=my_id, MY_CREATE_TIME=my_create_time)
    result = subprocess.run(
        [sys.executable, "-c", program],
        input=json.dumps(others),
        text=True,
        capture_output=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture(scope="module")
def gate_program(by_id):
    return extract_gate_program(script_of(by_id[GATE_ID]))


# Real values: gcloud builds list --format=json returns RFC3339 with a `Z` suffix and
# a fraction whose WIDTH VARIES (protobuf JSON emits 0/3/6/9 digits), which is why the
# gate parses to a datetime instead of comparing strings — see
# test_gate_orders_mixed_precision_timestamps_correctly for the inversion that causes.
# (`--format='value(createTime)'` is worse still: it renders
# `2026-08-28T16:43:45+00:00` and drops the fraction entirely.)
ME = {"id": "a7c941ff-356b-44a1", "createTime": "2026-08-28T15:08:01.500000Z"}
OLDER = {"id": "9aaf7d27-1111-2222", "createTime": "2026-08-28T15:07:01.500000Z"}
NEWER = {"id": "7ab1f0a1-3333-4444", "createTime": "2026-08-28T16:43:45.221420Z"}


def test_gate_proceeds_when_alone(gate_program):
    assert run_gate_decision(gate_program, ME["id"], ME["createTime"], [ME]) == "PROCEED"


def test_gate_proceeds_when_list_is_empty(gate_program):
    """The build can be absent from its own list (status changed between calls)."""
    assert run_gate_decision(gate_program, ME["id"], ME["createTime"], []) == "PROCEED"


def test_gate_supersedes_when_a_newer_build_exists(gate_program):
    out = run_gate_decision(gate_program, ME["id"], ME["createTime"], [ME, NEWER])
    assert out.startswith("SUPERSEDED "), out
    assert NEWER["id"] in out


def test_gate_waits_when_an_older_build_is_still_running(gate_program):
    out = run_gate_decision(gate_program, ME["id"], ME["createTime"], [ME, OLDER])
    assert out.startswith("WAIT "), out
    assert OLDER["id"] in out


def test_gate_supersedes_when_both_older_and_newer_exist(gate_program):
    """Newer wins: waiting for the older build would deploy code already obsolete."""
    out = run_gate_decision(
        gate_program, ME["id"], ME["createTime"], [OLDER, ME, NEWER]
    )
    assert out.startswith("SUPERSEDED "), out
    assert NEWER["id"] in out


def test_gate_breaks_createtime_ties_deterministically(gate_program):
    """Two builds sharing a createTime must not both WAIT (deadlock) or both PROCEED.

    Sub-second `createTime` collisions are unlikely but not impossible, and both
    failure modes are bad: two PROCEEDs is the race this FC removes, two WAITs hangs
    both builds until the gate's deadline (startTime + BUILD_TIMEOUT_SECONDS - 90)
    fails them. Ties break on build id,
    so the pair resolves exactly like any older/newer pair — one SUPERSEDED, one
    waiting for it to finish.
    """
    ts = "2026-08-28T15:08:01.500000Z"
    low = {"id": "aaaa1111", "createTime": ts}
    high = {"id": "bbbb2222", "createTime": ts}

    low_sees = run_gate_decision(gate_program, low["id"], ts, [low, high])
    high_sees = run_gate_decision(gate_program, high["id"], ts, [low, high])

    decisions = {low_sees.split()[0], high_sees.split()[0]}
    assert decisions == {"SUPERSEDED", "WAIT"}, (
        f"tie must resolve to one SUPERSEDED and one WAIT, got "
        f"low={low_sees!r} high={high_sees!r}"
    )
    assert low_sees.startswith("SUPERSEDED "), low_sees
    assert high["id"] in low_sees


@pytest.fixture(scope="module")
def gate_module(gate_program):
    """The gate program's namespace, so its helpers can be tested directly."""
    ns = {"__name__": "gate_decision_under_test"}
    exec(compile(gate_program, "gate_decision.py", "exec"), ns)
    return ns


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-28T16:43:45Z",              # 0 fractional digits
        "2026-08-28T16:43:45.221Z",          # 3  (protobuf JSON emits 0/3/6/9)
        "2026-08-28T16:43:45.221420Z",       # 6
        "2026-08-28T16:43:45.221420123Z",    # 9
        "2026-08-28T16:43:45.221420+00:00",  # explicit offset
        "2026-08-28T16:43:45+0000",          # offset without the colon
    ],
)
def test_gate_parses_every_rfc3339_shape_the_api_emits(gate_module, value):
    """Python 3.9's fromisoformat rejects both 'Z' and 9 fractional digits.

    The cloud-sdk image ships 3.9, and protobuf JSON emits 0, 3, 6 or 9 digits, so
    an unnormalized parse crashes the gate on a perfectly ordinary timestamp — and
    a crashed gate is a failed build (fail-closed), not a silent skip.
    """
    dt = gate_module["parse_rfc3339"](value)
    assert dt.tzinfo is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) == (
        2026, 8, 28, 16, 43, 45,
    )


def test_gate_orders_mixed_precision_timestamps_correctly(gate_module):
    """The bug string comparison would have caused, pinned.

    `...:45Z` is EARLIER than `...:45.221420Z`, but as strings "Z" > "." so the
    naive compare calls it later — inverting the order of two builds a fifth of a
    second apart and handing the deploy to the wrong commit.
    """
    parse = gate_module["parse_rfc3339"]
    assert parse("2026-08-28T16:43:45Z") < parse("2026-08-28T16:43:45.221420Z")
    assert "2026-08-28T16:43:45Z" > "2026-08-28T16:43:45.221420Z"  # the naive bug


def test_gate_supersedes_across_mixed_precision(gate_program):
    """End to end: a newer build whose timestamp has fewer digits still wins."""
    me = {"id": "aaaa1111", "createTime": "2026-08-28T16:43:45.221420Z"}
    newer = {"id": "bbbb2222", "createTime": "2026-08-28T16:43:46Z"}
    out = run_gate_decision(gate_program, me["id"], me["createTime"], [me, newer])
    assert out.startswith("SUPERSEDED "), out
    assert newer["id"] in out


def test_gate_ignores_malformed_entries(gate_program):
    """A build row missing id or createTime must not crash the gate or be counted."""
    junk = [ME, {"id": "no-createtime"}, {"createTime": "2026-08-29T00:00:00.0Z"}, {}]
    assert run_gate_decision(gate_program, ME["id"], ME["createTime"], junk) == "PROCEED"


# --------------------------------------------------------------------------
# 5e. The first-deploy bootstrap (FC-096 Phase B PR-c follow-up).
# --------------------------------------------------------------------------
SIM_CREATE_STEP = "create-sim-service-if-absent"


def run_bootstrap(script, tmp_path, *, exists, superseded=False,
                  deploy_fails_with=""):
    """Execute the bootstrap step's real bash with a stub `gcloud`.

    The `gate_decision.py` idiom, applied to a shell step: the SHIPPED script is
    run, not a paraphrase of it. Two substitutions make that possible —
    Cloud Build's `$VAR` expansion (`simulate_substitution`, the same one every
    other test here uses) and `/workspace`, which is absolute in the container
    and becomes a tmp dir so the test cannot write to a real one.

    Returns `(exit code, stdout+stderr, marker file contents, deploy argv)`.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if superseded:
        (workspace / "superseded").write_text("")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = workspace / "gcloud-calls.txt"
    stub = bindir / "gcloud"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        'case "$1 $2" in\n'
        '  "run services")\n'
        f'    exit {0 if exists else 1} ;;\n'
        '  "run deploy")\n'
        + (f'    echo {shlex.quote(deploy_fails_with)}; exit 1 ;;\n'
           if deploy_fails_with else '    echo "Deploying..."; exit 0 ;;\n')
        + 'esac\n'
        'exit 0\n'
    )
    stub.chmod(0o755)

    body = simulate_substitution(script).replace("/workspace", str(workspace))
    result = subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )
    marker = workspace / "sim-service-bootstrap.txt"
    return (
        result.returncode,
        result.stdout + result.stderr,
        marker.read_text().strip() if marker.exists() else None,
        calls.read_text().splitlines() if calls.exists() else [],
    )


def test_the_bootstrap_step_exists_and_is_marker_gated(by_id):
    assert SIM_CREATE_STEP in by_id, (
        "`gcloud run deploy --no-traffic` is rejected when the service does not "
        "exist, so the sim chain needs a creation path; build 2e1b180 died here."
    )
    script = script_of(by_id[SIM_CREATE_STEP])
    assert script.strip().startswith(MARKER_CHECK), (
        "a superseded build must not create the service either"
    )


def test_the_bootstrap_runs_before_the_canary_and_behind_the_promotes(by_id, steps):
    wait = set(by_id[SIM_CREATE_STEP].get("waitFor") or [])
    assert "promote-dashboard" in wait, (
        "creating a service can fail for reasons unrelated to the trading bot; "
        "it must land after the production revisions have their traffic"
    )
    canary_wait = set(by_id[CHAINS["sim-service"]["deploy"]].get("waitFor") or [])
    assert SIM_CREATE_STEP in canary_wait, (
        "the canary deploy must not run before the service it deploys into exists"
    )
    ids = [s["id"] for s in steps]
    assert ids.index(SIM_CREATE_STEP) < ids.index(CHAINS["sim-service"]["deploy"])


def test_the_bootstrap_deploy_omits_the_flags_creation_rejects(by_id):
    """The whole point: no `--no-traffic`, and no name the canary step wants.

    `--revision-suffix` and `--tag` are absent so this revision cannot collide
    with the canary's, and so the canary keeps sole ownership of the `canary`
    tag the smoke test reads its URL from.
    """
    script = script_of(by_id[SIM_CREATE_STEP])
    create = script[script.index("gcloud run deploy"):]
    create = create[:create.index("> ")]
    for forbidden in ("--no-traffic", "--tag=", "--revision-suffix"):
        assert forbidden not in create, (
            f"the creation deploy must not pass {forbidden}: Cloud Run rejects "
            f"--no-traffic outright at creation, and a tag or suffix here would "
            f"collide with the canary revision the next step names."
        )


def test_the_bootstrap_and_the_canary_agree_on_configuration(by_id):
    """Two deploys of one service must not disagree about what it runs.

    An `--set-env-vars` or `--set-secrets` drift would leave the bootstrap
    revision — which SERVES traffic for a minute or two on the first build —
    configured differently from everything after it.
    """
    create = script_of(by_id[SIM_CREATE_STEP])
    canary = script_of(by_id[CHAINS["sim-service"]["deploy"]])
    for shared in ("--command=python", "--args=deploy/sim_service.py",
                   "--no-allow-unauthenticated", "--no-cpu-throttling",
                   "--memory=2Gi", "--cpu=1", "--concurrency=1",
                   "--max-instances=2", "--min-instances=0", "--timeout=120"):
        assert shared in create and shared in canary, shared
    for prefix in ("--set-env-vars=", "--set-secrets="):
        c = create[create.index(prefix):].split("\n", 1)[0].rstrip(" \\")
        k = canary[canary.index(prefix):].split("\n", 1)[0].rstrip(" \\")
        assert c == k, f"{prefix} differs:\n  create: {c}\n  canary: {k}"


def test_the_promote_does_not_branch_on_the_bootstrap_marker(by_id):
    """Decided and documented: `--to-latest` is right on BOTH paths.

    The canary revision is created `--no-traffic` whether the service was just
    created or already existed, so promoting to latest is exactly the required
    instruction either way. The marker is written for the log — so a first build
    explains itself — and is asserted here rather than consumed downstream,
    which is what stops it from silently becoming load-bearing.
    """
    promote = script_of(by_id[CHAINS["sim-service"]["promote"]])
    assert "sim-service-bootstrap.txt" not in promote


@pytest.mark.parametrize("exists,expect_deploy,expect_marker", [
    (False, True, "created"),    # first build: create it
    (True, False, "exists"),     # every build after: one describe, no-op
])
def test_the_branch_behaves(tmp_path, by_id, exists, expect_deploy,
                            expect_marker):
    """The SHIPPED script, executed, against a stub `gcloud`."""
    code, out, marker, calls = run_bootstrap(
        script_of(by_id[SIM_CREATE_STEP]), tmp_path, exists=exists)
    assert code == 0, out
    assert marker == expect_marker
    deploys = [c for c in calls if c.startswith("run deploy")]
    assert bool(deploys) is expect_deploy, calls
    if expect_deploy:
        assert "--no-traffic" not in deploys[0]
        assert "--revision-suffix" not in deploys[0]


def test_a_superseded_build_creates_nothing(tmp_path, by_id):
    code, out, marker, calls = run_bootstrap(
        script_of(by_id[SIM_CREATE_STEP]), tmp_path, exists=False,
        superseded=True)
    assert code == 0 and marker is None
    assert calls == [], "a superseded build touched Cloud Run"
    assert "skip: superseded" in out


def test_a_lost_creation_race_is_success_not_failure(tmp_path, by_id):
    """Two builds can reach this at once; `already exists` means it worked."""
    code, out, marker, _calls = run_bootstrap(
        script_of(by_id[SIM_CREATE_STEP]), tmp_path, exists=False,
        deploy_fails_with="ERROR: Resource 'sim-service' already exists.")
    assert code == 0, out
    assert marker == "created"


def test_a_real_creation_failure_fails_the_build(tmp_path, by_id):
    """...and anything else must not be waved through as a race."""
    code, out, marker, _calls = run_bootstrap(
        script_of(by_id[SIM_CREATE_STEP]), tmp_path, exists=False,
        deploy_fails_with="ERROR: PERMISSION_DENIED on run.services.create")
    assert code == 1
    assert "FAIL: could not create sim-service" in out
    assert marker is None
