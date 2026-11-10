"""docs/plans/README.md is a derived view of the plan files' Status lines.

Two copies of a plan's status exist — its own ``**Status:**`` line and the section
its FC entry sits under in docs/FUTURE_CONSIDERATIONS.md — and they drift: the
2026-11-09 audit found a plan marked DONE whose FC had sat under Active for ten
weeks, and four README rows describing statuses that had moved on. These tests
make that drift a red build instead of a quarterly discovery.

What a failure means:
  * ``test_index_section_is_current``  -> run ``python tools/plans_index.py``
  * ``test_every_plan_has_a_recognised_status`` -> start the plan's Status line with
    one of the words in tools/plans_index.py's docstring
  * ``test_plan_status_agrees_with_fc_index`` -> flip the plan's Status or move the
    FC entry between Active and Completed — one of the two is lying
"""
import re

import pytest

from tools import plans_index as pi


# --- the live repository ----------------------------------------------------------

def test_index_section_is_current():
    plans = pi.load_plans()
    entries = pi.read_fc_entries(plans)
    assert pi.problems(plans, pi.fc_sections()) + pi.fc_problems(entries) == []
    assert pi.render_readme(plans, entries=entries) == pi.README.read_text(), (
        "docs/plans/README.md's '## Index' section is stale — run: python tools/plans_index.py")


def test_every_fc_entry_is_classified_and_consistent_with_its_plans():
    entries = pi.read_fc_entries(pi.load_plans())
    assert len(entries) > 100, "the FC index parse found suspiciously few entries"
    assert pi.fc_problems(entries) == []
    assert all(e.group is not None for e in entries)


def test_every_plan_has_a_recognised_status():
    unknown = [(p.name, p.status[:50]) for p in pi.load_plans() if p.group is None]
    assert unknown == [], f"Status line must start with a recognised word: {unknown}"


def test_plan_status_agrees_with_fc_index():
    assert pi.problems(pi.load_plans(), pi.fc_sections()) == []


def test_fc_index_cites_only_plan_files_that_exist():
    """A `Plan file:` / `- Plan:` field naming docs/plans/<x>.md must point at a file.

    'not yet' / 'not yet drafted' is the recorded way to say there is no plan, so
    a path inside such a field is a promise, not a citation.
    """
    missing = []
    for line in pi.FC_INDEX.read_text().splitlines():
        if not (line.startswith("**Plan file:**") or line.startswith("- Plan:")):
            continue
        if "not yet" in line.lower():
            continue
        for name in re.findall(r"docs/plans/([A-Za-z0-9_.-]+\.md)", line):
            if not (pi.PLANS_DIR / name).exists():
                missing.append(name)
    assert missing == [], f"FC index cites plan files that do not exist: {missing}"


# --- the classifier and renderer, on synthetic input ------------------------------

@pytest.mark.parametrize("status, group", [
    ("Done — merged 2026-08-28 (PR #99)", "Done"),
    ("**DONE — executed in full 2026-08-04.**", "Done"),
    ("Completed 2026-09-09", "Done"),
    ("Executing — both PRs merged", "In progress"),
    ("In progress (PR-2 open)", "In progress"),
    ("Draft", "Open"),
    ("Approved (build gate: operator sign-off)", "Open"),
    ("Deferred (operator decision 2026-07-31)", "Shelved"),
    ("Superseded by FC-121", "Shelved"),
    ("On hold by operator decision", "Shelved"),
    ("LIVE (paper) — Phases 0–3 DONE", None),      # the real fc-075 drift, pre-fix
    ("built, not opened", None),
    ("", None),
])
def test_classify_vocabulary(status, group):
    assert pi.classify(status) == group


def _plan(name, status, title="T"):
    return pi.Plan(name, "FC-" + name[3:6], title, status, pi.classify(status))


def test_snippet_truncates_and_closes_a_split_code_span():
    long = "Executing — " + "`abc` " * 40
    s = pi._snippet(long)
    assert len(s) <= pi.SNIPPET_CHARS + 1          # the ellipsis plus at most one closing backtick
    assert s.count("`") % 2 == 0                   # never leaves a code span open
    assert "…" in s
    assert pi._snippet("Draft") == "Draft"         # short statuses pass through untouched


def test_problems_flags_done_plan_under_active_and_missing_entry():
    plans = [_plan("fc-010.md", "Done"), _plan("fc-011.md", "Draft"), _plan("fc-012.md", "Deferred")]
    sections = {"FC-010": "Active Considerations", "FC-012": "Active Considerations"}
    out = pi.problems(plans, sections)
    assert any("fc-010.md" in m and "expected 'Completed'" in m for m in out)
    assert any("fc-011.md" in m and "no entry" in m for m in out)
    assert not any("fc-012.md" in m for m in out), "a Shelved plan may sit in either section"


def test_render_readme_replaces_only_the_index_section():
    current = "# Plans\n\nintro text stays\n\n## Index\n\nold rows\n"
    out = pi.render_readme([_plan("fc-001.md", "Done")], current=current)
    assert out.startswith("# Plans\n\nintro text stays\n\n## Index\n")
    assert "old rows" not in out and "fc-001.md" in out


# --- FC-level classification and rendering ----------------------------------------

@pytest.mark.parametrize("section, status, group", [
    ("Completed", "", "Done"),
    ("Completed", "anything at all", "Done"),
    ("Active Considerations", "EXECUTING — PR-1 merged", "In progress"),
    ("Active Considerations", "Plan published", "Planned"),
    ("Active Considerations", "Planned — fc-020.md rev 2", "Planned"),
    ("Active Considerations", "Filed 2026-09-03 (review)", "Filed"),
    ("Active Considerations", "Consideration — high value", "Filed"),
    ("Active Considerations", "Deferred (operator decision)", "Shelved"),
    ("Active Considerations", "On hold — by operator decision", "Shelved"),
    ("Active Considerations", "Plan is being discussed", None),       # 'Plan' alone is not a status
    ("Active Considerations", "Blockers RESOLVED; code fix open", None),  # the real FC-058 wording, pre-fix
    ("Active Considerations", "", None),
])
def test_classify_fc(section, status, group):
    assert pi.classify_fc(section, status) == group


def _entry(fc, section, status, plans=()):
    return pi.FcEntry(fc, "t", section, status, tuple(plans), pi.classify_fc(section, status))


def test_fc_problems_catch_the_two_lies_and_an_unknown_word():
    plan = _plan("fc-050.md", "Done")
    out = pi.fc_problems([
        _entry("FC-050", "Active Considerations", "Filed 2026-01-01", plans=(plan,)),  # plan exists, says Filed
        _entry("FC-051", "Active Considerations", "Planned"),                           # says Planned, no plan
        _entry("FC-052", "Active Considerations", "Blockers resolved"),                # unknown word
        _entry("FC-053", "Active Considerations", "Consideration"),                    # fine
        _entry("FC-054", "Completed", ""),                                             # fine
        _entry("FC-055", "Deferred Pile", "Filed"),                                    # unknown section
    ])
    assert any("FC-050" in m and "start its Status with 'Planned' or 'Executing'" in m for m in out)
    assert any("FC-051" in m and "no docs/plans/fc-051*.md" in m for m in out)
    assert any("FC-052" in m and "recognised word" in m for m in out)
    assert any("FC-055" in m and "sits under 'Deferred Pile'" in m for m in out)
    assert not any("FC-053" in m or "FC-054" in m for m in out)


def test_render_index_groups_every_fc_and_collapses_done():
    plans = [_plan("fc-001.md", "Done"), _plan("fc-003.md", "Executing — PR open")]
    entries = [
        _entry("FC-001", "Completed", "", plans=(plans[0],)),
        _entry("FC-002", "Active Considerations", "Filed 2026-11-09 (review)"),
        _entry("FC-003", "Active Considerations", "Executing — PR-1 merged", plans=(plans[1],)),
        _entry("FC-004", "Active Considerations", "Deferred (operator)"),
    ]
    out = pi.render_index(plans, entries)
    assert "**In progress 1 · Planned 0 · Filed 1 · Shelved 1 · Done 1** — 4 FCs, 2 plan files." in out
    heads = re.findall(r"^### (.+?) \((\d+)\)$", out, re.M)
    assert [h[0].split(" — ")[0] for h in heads] == ["In progress", "Planned", "Filed", "Shelved"]
    assert "### Planned — plan drafted, nothing built (0)\n\n_none_" in out
    # Done is collapsed, rows carry plan links, pending rows carry the status snippet
    assert "<summary><strong>Done (1)</strong></summary>" in out
    assert "- **FC-001** — t — [fc-001.md](fc-001.md)\n" in out
    assert "- **FC-003** — t — [fc-003.md](fc-003.md) — _Executing — PR-1 merged_" in out
    assert "- **FC-002** — t — _Filed 2026-11-09 (review)_" in out


def test_read_fc_entries_reads_the_live_index_shape():
    entries = pi.read_fc_entries(pi.load_plans())
    by = {e.fc: e for e in entries}
    assert by["FC-112"].group == "In progress" and by["FC-112"].plans
    assert by["FC-120"].group == "Done" and len(by["FC-120"].plans) == 1
    assert by["FC-060"].group == "Done" and len(by["FC-060"].plans) == 3
    # a plan file whose FC is missing from the index would be a parse hole, not a Done
    assert all(p.fc in by for p in pi.load_plans())
