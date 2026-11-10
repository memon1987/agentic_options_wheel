#!/usr/bin/env python3
"""Regenerate the by-status index in docs/plans/README.md from each plan's Status line.

Why this exists: a plan's status lives in its own ``**Status:**`` line and, for
the FC it implements, in which section of docs/FUTURE_CONSIDERATIONS.md the entry
sits. Two copies drift (an audit on 2026-11-09 found a plan marked DONE whose FC
had sat under Active for ten weeks). This script makes the README a derived view
of the Status lines, and ``tests/test_plans_index.py`` fails CI when the README is
stale, a status word is unrecognised, or a plan disagrees with its FC entry's
section. Nothing here moves files: paths stay flat so every link keeps working.

The index covers EVERY FC entry in docs/FUTURE_CONSIDERATIONS.md, not only the ones
with a plan file (most pending FCs have none yet): In progress, Planned (a plan
exists, nothing built), Filed (no plan yet), Shelved, Done — each row linking the
FC's plan files when it has them.

Usage:
    python tools/plans_index.py          # rewrite the README's "## Index" section
    python tools/plans_index.py --check  # exit 1 if the README would change

Status vocabulary (first word of the Status line, bold markers ignored).
Plan files:
    Done / Completed                      -> Done
    Executing / In progress / Building    -> In progress
    Draft / Approved / Ready / Published  -> Open
    Deferred / Superseded / Withdrawn /
    Abandoned / Shelved / On hold         -> Shelved
FC entries under "## Active Considerations":
    Executing                             -> In progress
    Planned / Plan published              -> Planned
    Filed / Consideration / Proposed /
    Idea                                  -> Filed (no plan yet)
    Deferred / Superseded / Withdrawn /
    On hold                               -> Shelved
Entries under "## Completed" are Done whatever their wording (the older ones use
a compact `- Plan: / - PR: / - Closed:` format with no Status line).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLANS_DIR = REPO_ROOT / "docs" / "plans"
README = PLANS_DIR / "README.md"
FC_INDEX = REPO_ROOT / "docs" / "FUTURE_CONSIDERATIONS.md"
INDEX_HEADING = "## Index"

# Order is the order of the README sections.
GROUPS = ("In progress", "Open", "Done", "Shelved")
_WORDS = {
    "done": "Done", "completed": "Done",
    "executing": "In progress", "in": "In progress", "building": "In progress",
    "draft": "Open", "approved": "Open", "ready": "Open", "published": "Open",
    "deferred": "Shelved", "superseded": "Shelved", "withdrawn": "Shelved",
    "abandoned": "Shelved", "shelved": "Shelved", "on": "Shelved",
}
# Which FC-index section each group must live in; None = either is fine.
_EXPECTED_SECTION = {"Done": "Completed", "In progress": "Active Considerations",
                     "Open": "Active Considerations", "Shelved": None}
SNIPPET_CHARS = 110


@dataclass(frozen=True)
class Plan:
    name: str          # fc-112.md
    fc: str            # FC-112
    title: str
    status: str        # the raw Status line, markers stripped
    group: str | None  # None = unrecognised


def _strip(s: str) -> str:
    """Drop bold markers only; backticks and underscores are part of the text."""
    return re.sub(r"\*+", "", s).strip()


def classify(status: str) -> str | None:
    words = _strip(status).lower().split()
    if not words:
        return None
    first = words[0].strip(".,;:—-()")
    if first == "in" and len(words) > 1 and words[1].startswith("progress"):
        return "In progress"
    if first == "on" and len(words) > 1 and words[1].startswith("hold"):
        return "Shelved"
    if first in ("in", "on"):
        return None
    return _WORDS.get(first)


def read_plan(path: Path) -> Plan:
    text = path.read_text()
    m = re.search(r"^\*\*Status:\*\*\s*(.*)$", text, re.M)
    status = _strip(m.group(1)) if m else ""
    t = re.search(r"^#\s+(.*)$", text, re.M)
    title = _strip(t.group(1)) if t else path.stem
    title = re.sub(r"^Plan:\s*", "", title)
    title = re.sub(r"^FC-\d{3}\s*[—–-]+\s*", "", title)
    fc = "FC-" + path.name[3:6]
    return Plan(path.name, fc, title, status, classify(status))


def load_plans() -> list[Plan]:
    return [read_plan(p) for p in sorted(PLANS_DIR.glob("fc-*.md"))]


def fc_sections() -> dict[str, str]:
    """FC number -> the ``## `` section its entry sits under in the FC index."""
    out: dict[str, str] = {}
    section = ""
    for line in FC_INDEX.read_text().splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        m = re.match(r"^### (FC-\d{3})\b", line)
        if m and m.group(1) not in out:
            out[m.group(1)] = section
    return out


def problems(plans: list[Plan], sections: dict[str, str]) -> list[str]:
    """Everything the test refuses: unknown status words, plan/FC disagreement."""
    out = []
    for p in plans:
        if p.group is None:
            out.append(f"{p.name}: Status line does not start with a recognised word "
                       f"(got {p.status[:60]!r}); see tools/plans_index.py docstring")
            continue
        want = _EXPECTED_SECTION[p.group]
        have = sections.get(p.fc)
        if have is None:
            out.append(f"{p.name}: no entry for {p.fc} in docs/FUTURE_CONSIDERATIONS.md")
        elif want and have != want:
            out.append(f"{p.name}: plan says {p.group} but {p.fc} sits under "
                       f"'{have}' in docs/FUTURE_CONSIDERATIONS.md (expected '{want}')")
    return out


FC_GROUPS = ("In progress", "Planned", "Filed", "Shelved", "Done")
FC_GROUP_TITLES = {
    "In progress": "In progress — plan executing",
    "Planned": "Planned — plan drafted, nothing built",
    "Filed": "Filed — no plan yet",
    "Shelved": "Shelved — deferred / on hold / superseded",
    "Done": "Done",
}
_FC_WORDS = {
    "executing": "In progress",
    "planned": "Planned", "plan": "Planned",
    "filed": "Filed", "consideration": "Filed", "proposed": "Filed", "idea": "Filed",
    "deferred": "Shelved", "superseded": "Shelved", "withdrawn": "Shelved", "on": "Shelved",
}
ACTIVE_SECTION = "Active Considerations"
DONE_SECTION = "Completed"


@dataclass(frozen=True)
class FcEntry:
    fc: str
    title: str
    section: str
    status: str
    plans: tuple[Plan, ...]
    group: str | None


def classify_fc(section: str, status: str) -> str | None:
    if section == DONE_SECTION:
        return "Done"
    words = _strip(status).lower().split()
    if not words:
        return None
    first = words[0].strip(".,;:—-()")
    if first == "plan" and not (len(words) > 1 and words[1].startswith("published")):
        return None
    if first == "on" and not (len(words) > 1 and words[1].startswith("hold")):
        return None
    return _FC_WORDS.get(first)


def read_fc_entries(plans: list[Plan]) -> list[FcEntry]:
    by_fc: dict[str, list[Plan]] = {}
    for p in plans:
        by_fc.setdefault(p.fc, []).append(p)
    out: list[FcEntry] = []
    section = ""
    cur: dict | None = None

    def close():
        if cur is not None:
            out.append(FcEntry(cur["fc"], cur["title"], cur["section"], cur["status"],
                               tuple(by_fc.get(cur["fc"], ())),
                               classify_fc(cur["section"], cur["status"])))

    for line in FC_INDEX.read_text().splitlines():
        if line.startswith("## "):
            close(); cur = None
            section = line[3:].strip()
            continue
        m = re.match(r"^### (FC-\d{3})\s*[:：]?\s*(.*)$", line)
        if m:
            close()
            cur = {"fc": m.group(1), "title": _strip(m.group(2)), "section": section, "status": ""}
            continue
        if cur is not None and line.startswith("**Status:**") and not cur["status"]:
            cur["status"] = _strip(line[len("**Status:**"):])
    close()
    seen: set[str] = set()
    return [e for e in out if not (e.fc in seen or seen.add(e.fc))]


def fc_problems(entries: list[FcEntry]) -> list[str]:
    """What the test refuses at FC level: unknown status words, a plan nobody filed."""
    out = []
    for e in entries:
        if e.section not in (ACTIVE_SECTION, DONE_SECTION):
            out.append(f"{e.fc}: sits under '{e.section}', expected '{ACTIVE_SECTION}' or '{DONE_SECTION}'")
            continue
        if e.group is None:
            out.append(f"{e.fc}: Status line under Active does not start with a recognised word "
                       f"(got {e.status[:60]!r}); see tools/plans_index.py docstring")
            continue
        if e.group == "Filed" and e.plans:
            out.append(f"{e.fc}: says {e.status.split()[0]!r} but has plan file(s) "
                       f"{[p.name for p in e.plans]} — start its Status with 'Planned' or 'Executing'")
        if e.group in ("Planned", "In progress") and not e.plans:
            out.append(f"{e.fc}: says {e.status.split()[0]!r} but no docs/plans/fc-{e.fc[3:]}*.md exists")
    return out


def _snippet(status: str) -> str:
    s = status if len(status) <= SNIPPET_CHARS else status[:SNIPPET_CHARS - 1].rstrip() + "…"
    if s.count("`") % 2:          # truncation split a code span; close it
        s += "`"
    return s.replace("|", "/")


def _plan_links(plans: tuple[Plan, ...] | list[Plan]) -> str:
    return ", ".join(f"[{p.name}]({p.name})" for p in plans)


def render_index(plans: list[Plan], entries: list[FcEntry] | None = None) -> str:
    entries = read_fc_entries(plans) if entries is None else entries
    counts = {g: sum(1 for e in entries if e.group == g) for g in FC_GROUPS}
    lines = [INDEX_HEADING, "",
             "_Generated by `python tools/plans_index.py` from every FC entry's `**Status:**` line in "
             "`docs/FUTURE_CONSIDERATIONS.md` and every plan file's; `tests/test_plans_index.py` fails "
             "when this section is stale, a status word is unrecognised, or a plan and its FC disagree. "
             "Do not edit by hand._", "",
             "**" + " · ".join(f"{g} {counts[g]}" for g in FC_GROUPS) + f"** — {len(entries)} FCs, "
             f"{len(plans)} plan files.", ""]
    for g in FC_GROUPS:
        rows = [e for e in entries if e.group == g]
        head = f"### {FC_GROUP_TITLES[g]} ({len(rows)})"
        if g == "Done":
            lines += ["<details>", f"<summary><strong>{FC_GROUP_TITLES[g]} ({len(rows)})</strong></summary>", ""]
        else:
            lines += [head, ""]
        if not rows:
            lines.append("_none_")
        for e in rows:
            row = f"- **{e.fc}** — {e.title}"
            if e.plans:
                row += f" — {_plan_links(e.plans)}"
            if g != "Done":
                row += f" — _{_snippet(e.status)}_"
            lines.append(row)
        if g == "Done":
            lines += ["", "</details>"]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_readme(plans: list[Plan], current: str | None = None,
                  entries: list[FcEntry] | None = None) -> str:
    text = current if current is not None else README.read_text()
    i = text.find("\n" + INDEX_HEADING)
    if i < 0:
        raise SystemExit(f"{README}: no '{INDEX_HEADING}' heading")
    return text[: i + 1] + render_index(plans, entries)


def main(argv: list[str]) -> int:
    plans = load_plans()
    entries = read_fc_entries(plans)
    bad = problems(plans, fc_sections()) + fc_problems(entries)
    if bad:
        print("plans_index: refusing to generate —")
        for b in bad:
            print("  -", b)
        return 2
    new = render_readme(plans, entries=entries)
    if "--check" in argv:
        if new != README.read_text():
            print("docs/plans/README.md index is stale; run: python tools/plans_index.py")
            return 1
        print("docs/plans/README.md index is current")
        return 0
    README.write_text(new)
    counts = {g: sum(1 for e in entries if e.group == g) for g in FC_GROUPS}
    print("wrote", README.relative_to(REPO_ROOT), counts)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
