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

# Cloud Build built-in substitutions that may appear bare (single `$`) in a script.
ALLOWED_BARE_SUBSTITUTIONS = {"PROJECT_ID", "COMMIT_SHA", "BUILD_ID", "SHORT_SHA"}


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
        "its own test/build/push work, so only the deploy chain is serialized — and the "
        "gate's own 15-minute wait cap stays wider than any legitimate wait. It must not "
        "gain edges that would delay it behind the deploy chain either."
    )

    for service, roles in CHAINS.items():
        deploy_id = roles["deploy"]
        wait = by_id[deploy_id].get("waitFor") or []
        assert GATE_ID in wait, (
            f"{deploy_id} must waitFor {GATE_ID}; otherwise {service} deploys before "
            "the build ordering has been decided."
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


# Real values: gcloud builds list --format=json returns RFC3339 with a `Z` suffix
# and fixed-width microseconds, so string comparison orders builds correctly.
# (`--format='value(createTime)'` does NOT — it renders `2026-08-28T16:43:45+00:00`,
# dropping the microseconds that separate builds pushed seconds apart.)
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
    both builds until the 15-minute gate timeout fails them. Ties break on build id,
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


def test_gate_ignores_malformed_entries(gate_program):
    """A build row missing id or createTime must not crash the gate or be counted."""
    junk = [ME, {"id": "no-createtime"}, {"createTime": "2026-08-29T00:00:00.0Z"}, {}]
    assert run_gate_decision(gate_program, ME["id"], ME["createTime"], junk) == "PROCEED"
