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
    assert pi.problems(plans, pi.fc_sections()) == []
    assert pi.render_readme(plans) == pi.README.read_text(), (
        "docs/plans/README.md's '## Index' section is stale — run: python tools/plans_index.py")


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


def test_render_groups_in_fixed_order_with_counts_and_snippets():
    plans = [
        _plan("fc-001.md", "Done — shipped"),
        _plan("fc-002.md", "Executing — PR open", title="two"),
        _plan("fc-003.md", "Draft"),
    ]
    out = pi.render_index(plans)
    heads = re.findall(r"^### (.+?) \((\d+)\)$", out, re.M)
    assert heads == [("In progress", "1"), ("Open", "1"), ("Done", "1"), ("Shelved", "0")]
    assert "### Shelved (0)\n\n_none_" in out
    # Done rows carry the title only; open/in-progress rows carry the status snippet.
    assert "- [fc-001.md](fc-001.md) — FC-001 — T\n" in out
    assert "- [fc-002.md](fc-002.md) — FC-002 — two — _Executing — PR open_" in out


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
