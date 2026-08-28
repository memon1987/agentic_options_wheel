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
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CLOUDBUILD = REPO_ROOT / "cloudbuild.yaml"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cloudbuild_contract.json"

SERVICES = ("options-wheel-strategy", "covered-call-engine", "options-wheel-dashboard")

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
}

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
