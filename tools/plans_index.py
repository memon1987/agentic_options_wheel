#!/usr/bin/env python3
"""Regenerate the by-status index in docs/plans/README.md from each plan's Status line.

Why this exists: a plan's status lives in its own ``**Status:**`` line and, for
the FC it implements, in which section of docs/FUTURE_CONSIDERATIONS.md the entry
sits. Two copies drift (an audit on 2026-11-09 found a plan marked DONE whose FC
had sat under Active for ten weeks). This script makes the README a derived view
of the Status lines, and ``tests/test_plans_index.py`` fails CI when the README is
stale, a status word is unrecognised, or a plan disagrees with its FC entry's
section. Nothing here moves files: paths stay flat so every link keeps working.

Usage:
    python tools/plans_index.py          # rewrite the README's "## Index" section
    python tools/plans_index.py --check  # exit 1 if the README would change

Status vocabulary (first word of the Status line, bold markers ignored):
    Done / Completed                      -> Done
    Executing / In progress / Building    -> In progress
    Draft / Approved / Ready / Published  -> Open
    Deferred / Superseded / Withdrawn /
    Abandoned / Shelved / On hold         -> Shelved
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


def _snippet(status: str) -> str:
    s = status if len(status) <= SNIPPET_CHARS else status[:SNIPPET_CHARS - 1].rstrip() + "…"
    if s.count("`") % 2:          # truncation split a code span; close it
        s += "`"
    return s.replace("|", "/")


def render_index(plans: list[Plan]) -> str:
    lines = [INDEX_HEADING, "",
             "_Generated by `python tools/plans_index.py` from each plan's `**Status:**` line; "
             "`tests/test_plans_index.py` fails when this section is stale or a plan "
             "disagrees with its FC entry's section. Do not edit by hand._", ""]
    for g in GROUPS:
        rows = [p for p in plans if p.group == g]
        lines.append(f"### {g} ({len(rows)})")
        lines.append("")
        if not rows:
            lines.append("_none_")
        for p in rows:
            row = f"- [{p.name}]({p.name}) — {p.fc} — {p.title}"
            if g != "Done":
                row += f" — _{_snippet(p.status)}_"
            lines.append(row)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_readme(plans: list[Plan], current: str | None = None) -> str:
    text = current if current is not None else README.read_text()
    i = text.find("\n" + INDEX_HEADING)
    if i < 0:
        raise SystemExit(f"{README}: no '{INDEX_HEADING}' heading")
    return text[: i + 1] + render_index(plans)


def main(argv: list[str]) -> int:
    plans = load_plans()
    bad = problems(plans, fc_sections())
    if bad:
        print("plans_index: refusing to generate —")
        for b in bad:
            print("  -", b)
        return 2
    new = render_readme(plans)
    if "--check" in argv:
        if new != README.read_text():
            print("docs/plans/README.md index is stale; run: python tools/plans_index.py")
            return 1
        print("docs/plans/README.md index is current")
        return 0
    README.write_text(new)
    counts = {g: sum(1 for p in plans if p.group == g) for g in GROUPS}
    print("wrote", README.relative_to(REPO_ROOT), counts)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
