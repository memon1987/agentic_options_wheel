"""Lint gates that guard a bug class, not a style preference.

There is no CI in this repo (`.github/` is absent), so the pytest suite is the
only enforcement vehicle that actually runs.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_undefined_names_in_production_code():
    """F821 (undefined name) over src/ and deploy/ must stay clean.

    FC-035's defect was `alpaca.trading.requests...` inside `poll_order_statuses`
    with no `import alpaca` — an F821 that raised on every invocation, was
    swallowed by a bare `except`, and so sat undetected from April to July while
    the surrounding code looked healthy. It was the ONLY F821 in src/ + deploy/,
    so this gate goes green with its deletion and permanently blocks the class:
    code that never executes because it references a name that does not exist.

    Same family as FC-015 (`_entry_times` in-process, gate dead) and FC-036
    (gap gate measuring the wrong thing) — healthy-looking dead code.
    """
    # Test-level, not module-level: a module-level importorskip silently skips
    # the whole file (2026-07-18 gotcha).
    pytest.importorskip("flake8")

    result = subprocess.run(
        [sys.executable, "-m", "flake8", "--select=F821", "src", "deploy"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )

    assert result.returncode == 0, (
        "flake8 F821 found undefined name(s) — this is the FC-035 bug class "
        "(code that can never run):\n" + (result.stdout or result.stderr)
    )


# --------------------------------------------------------------------------- #
# FC-075 Phase 2 (test req 10) — the OCC-substring gate
# --------------------------------------------------------------------------- #
#
# Routing/labelling an option by `'C' in symbol` / `'P' in symbol` is the bug
# FAMILY behind FC-041/043/045/048/052/054/079 — a substring test that resolves
# a plain 'AAPL' to 'put' and any C-containing ticker to 'call'. The canonical
# `src/utils/option_symbols.strict_option_type` is the only correct primitive.
# This gate fails if the substring pattern appears in CODE anywhere under src/,
# tools/, or deploy/ outside a small allowlist of legacy files that FC-079 will
# drain. It uses tokenization, not grep, so comments and docstrings that merely
# *mention* the pattern (there are several) do not trip it — only real code,
# where `'C'`, `in`, and the option-symbol name are separate tokens, does.

# FC-079 DRAINED THIS ALLOWLIST. It listed five files when this gate shipped;
# four of them (wheel_engine, regression_monitor, and the two ad-hoc analysis
# scripts) were rewired onto `strict_option_type` / `parse_option_symbol` and
# removed from it. Only the canonical parser remains, and only because its
# documented last-resort heuristic is the one deliberate home of the idiom.
#
# Do not re-add a file here. `tests/test_no_occ_substring.py` (FC-079) is the
# wider gate — it also catches the positional `[-9]` / `[-8:]` reads and any
# variable whose name contains "sym" — and it allow-lists per *line*, via a
# `# occ-substring-allowed` marker, precisely so a whole file can never again be
# exempted to buy time.
#
# SCOPE (honest): this gate catches the bareword substring form
# `'C'|'P' in <name>` where <name> is one of _OPTION_VARS — the exact shape every
# historical member of the family (FC-041/043/045/048/052/054) used. It does NOT
# catch attribute/subscript shapes (`'C' in opp.option_symbol`,
# `'C' in opp['option_symbol']`) or a variable named outside _OPTION_VARS. It is a
# tripwire for the common form, not a proof of absence; the real defense is that
# all new option-type routing goes through `strict_option_type`.
_OCC_SUBSTRING_ALLOWLIST = {
    # The canonical parser itself: its documented last-resort tolerant heuristic
    # (`'C' if 'C' in symbol ...`) is the ONE intentional home of the pattern.
    "src/utils/option_symbols.py",
}

_OPTION_VARS = {"symbol", "opt_sym", "option_symbol", "occ_symbol", "contract_symbol"}


def _occ_substring_sites(py_file: Path):
    """Return [(lineno, text)] where CODE tests `'C'|'P' in <option-var>`.

    Token-based: a real code site is STRING('C'|'P'), then the `in` keyword, then
    a NAME in _OPTION_VARS. A prose mention lives inside a single STRING/COMMENT
    token and so is invisible here.
    """
    import tokenize
    sites = []
    try:
        with open(py_file, "rb") as fh:
            toks = [t for t in tokenize.tokenize(fh.readline)
                    if t.type not in (tokenize.NL, tokenize.NEWLINE,
                                      tokenize.INDENT, tokenize.DEDENT,
                                      tokenize.COMMENT, tokenize.ENCODING)]
    except (tokenize.TokenError, SyntaxError):
        return sites
    for i in range(len(toks) - 2):
        a, b, c = toks[i], toks[i + 1], toks[i + 2]
        if (a.type == tokenize.STRING
                and a.string.strip("\"'") in ("C", "P")
                and b.type == tokenize.NAME and b.string == "in"
                and c.type == tokenize.NAME and c.string in _OPTION_VARS):
            sites.append((a.start[0], a.line.strip()))
    return sites


def test_no_occ_substring_routing_outside_allowlist():
    offenders = []
    for base in ("src", "tools", "deploy"):
        root = REPO_ROOT / base
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            rel = py.relative_to(REPO_ROOT).as_posix()
            if rel in _OCC_SUBSTRING_ALLOWLIST:
                continue
            for lineno, text in _occ_substring_sites(py):
                offenders.append(f"{rel}:{lineno}: {text}")

    assert not offenders, (
        "OCC-substring routing found in non-allowlisted code (the "
        "FC-041/045/048/052/054 bug family). Use "
        "src.utils.option_symbols.strict_option_type instead:\n  "
        + "\n  ".join(offenders)
    )


PROBE = (
    "def f(option_symbol, opt_sym, unrelated):\n"
    "    a = 'C' in option_symbol\n"
    "    b = 'P' in opt_sym\n"
    "    c = 'C' in unrelated       # not an option var: not a site\n"
    "    return a, b, c\n"
)


def test_occ_gate_still_detects_the_pattern(tmp_path):
    """Guard the guard: if tokenization stopped finding sites, the gate above
    would pass vacuously.

    This used to assert against the real `wheel_engine.py`, which was on the
    allowlist and still carried the idiom. FC-079 drained it, so the guard now
    runs on a synthetic probe — a guard that depends on live technical debt
    stops working the moment the debt is paid, which is exactly backwards.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")

    sites = _occ_substring_sites(probe)
    assert [lineno for lineno, _ in sites] == [2, 3], sites


def test_the_allowlist_is_down_to_the_canonical_parser():
    """FC-079's contract: exactly one file may carry the idiom."""
    assert _OCC_SUBSTRING_ALLOWLIST == {"src/utils/option_symbols.py"}
