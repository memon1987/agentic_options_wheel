"""Structural gate: no OCC symbol may be classified by substring or blind slice.

FC-079. Eight times (FC-041/043/045/048/052/054, FC-069 item 12, and the two
reconcile sites this test shipped with) a piece of this system decided whether
an option symbol was a put or a call by asking whether the letter ``'C'``
appeared somewhere in it, split the symbol on that letter, or read a field off
a fixed negative slice. All of it is wrong for any root carrying a ``P`` or a
``C`` — ``AAPL``, ``SPY`` and ``PFE`` are three of the fourteen configured
symbols — and all of it fails silently, which is why the family kept recurring
rather than being caught once.

The canonical primitives are ``src/utils/option_symbols.strict_option_type``
(classification) and ``parse_option_symbol`` (underlying / strike / expiry).

WHAT THIS GATE CATCHES (the shapes, via ``ast`` — so it is name-agnostic; the
first version of this file matched a regex against variables whose name
happened to contain "sym", and a review pointed out that renaming the variable
walked straight past it):

  (a) ``'C' in <anything>`` / ``'C' not in <anything>`` — a membership test with
      a bare ``'C'`` or ``'P'`` on the left. Includes ``'C' in row['contract']``
      and multi-line forms.
  (b) ``<anything>.split('P')`` and friends — ``split rsplit partition
      rpartition find rfind index rindex count startswith endswith`` called with
      ``'C'`` or ``'P'`` as the first argument. ``symbol.split('P')`` was the
      shape in both ad-hoc analysis scripts.
  (c) the OCC positional reads — ``[-9]``, ``[-8:]``, ``[-9:-8]``, ``[-15:-9]``,
      ``[-15:]`` — on any expression. These encode the contract layout at a
      call site instead of asking the parser.
  (d) ``<anything>[-9] == 'C'`` — (c) and a comparison, called out separately
      because it reads like a legitimate type check.

WHAT IT DOES NOT CATCH — this is a tripwire for known shapes, not a proof of
absence. Out of scope, deliberately: ``re.search('C', sym)`` and any other
regex-mediated form; ``any(c == 'C' for c in sym)`` and comprehension forms; a
constant hoisted to a name (``CALL = 'C'; if CALL in sym``); slices computed
from variables (``sym[-n:]``); and anything reached through an f-string or
``eval``. The real defense is that all option-type routing goes through
``strict_option_type``; this gate exists to make the *known* regressions loud.

ONE ALLOW-LISTED FILE: ``src/utils/option_symbols.py``, and only on lines
carrying an ``# occ-substring-allowed`` comment. That file is where the
canonical parsers live; its documented last-resort heuristic is the one place
the idiom is deliberate. The allow-list is file-path **and** marker, and
``test_the_allowlist_holds_exactly_one_site`` pins the full set of exemptions
by file, line and text — so a second marker anywhere shows up as a test diff
rather than as silence.

``tests/test_lint_gates.py`` carries a narrower token-based version of rule (a)
from FC-075 Phase 2; it imports this module's walker so the two cannot disagree
about which files are covered.
"""

import ast
import io
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories walked in full, plus individual files. Parity with
# tests/test_lint_gates.py, which imports `walked_files` from here.
WALKED_DIRS = ("src", "deploy", "tools")
WALKED_FILES = ("main.py",)

# The only file whose marker comments are honored.
ALLOWLISTED_FILES = ("src/utils/option_symbols.py",)

MARKER = "# occ-substring-allowed"

# The exemption this repo has, in full. A new marker anywhere — including a
# second one in the allow-listed file — changes this set and fails the test.
EXPECTED_EXEMPTIONS = {
    (
        "src/utils/option_symbols.py",
        "type_char = 'C' if 'C' in symbol else ('P' if 'P' in symbol else None)"
        "  # occ-substring-allowed",
    ),
}

_TYPE_CHARS = ("C", "P")

# Rule (b): string methods that locate or split on a character.
_LOCATOR_METHODS = frozenset({
    "split", "rsplit", "partition", "rpartition",
    "find", "rfind", "index", "rindex",
    "count", "startswith", "endswith",
})

# Rule (c): the OCC positional reads. Slices as (lower, upper), where `None`
# means the bound was omitted; plus the bare [-9] index.
#   [-9]      the type character        [-8:]     the strike
#   [-9:-8]   the type character        [-15:-9]  the YYMMDD date part
#   [-15:]    everything but the root
_BANNED_SLICES = frozenset({(-9, -8), (-15, -9), (-15, None), (-8, None)})
_BANNED_INDEX = -9


def _is_type_char_constant(node) -> bool:
    return (isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _TYPE_CHARS)


def _int_literal(node):
    """The int value of a (possibly negated) integer literal, else None."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, int) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _int_literal(node.operand)
        return None if inner is None else -inner
    return None


def _subscript_bounds(node: ast.Subscript):
    """``('index', n)`` / ``('slice', lower, upper)`` / None for anything else."""
    sl = node.slice
    # Python 3.9+: the index is the bare expression; 3.8 wrapped it in ast.Index.
    if hasattr(ast, "Index") and isinstance(sl, getattr(ast, "Index")):
        sl = sl.value  # pragma: no cover - 3.8 only
    if isinstance(sl, ast.Slice):
        if sl.step is not None:
            return None
        return ("slice", _int_literal(sl.lower), _int_literal(sl.upper))
    value = _int_literal(sl)
    if value is None:
        return None
    return ("index", value)


def _findings_for_node(node):
    """Yield rule labels for one AST node."""
    # (a) membership test with a bare 'C' / 'P' on the left.
    if isinstance(node, ast.Compare) and _is_type_char_constant(node.left):
        if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            yield "(a) substring membership test on a bare 'C'/'P'"

    # (d) <expr>[-9] == 'C' — checked before (c) so it gets the clearer label.
    if isinstance(node, ast.Compare) and isinstance(node.left, ast.Subscript):
        bounds = _subscript_bounds(node.left)
        if bounds == ("index", _BANNED_INDEX) and any(
                _is_type_char_constant(c) for c in node.comparators):
            yield "(d) positional [-9] compared against 'C'/'P'"

    # (b) locator/splitter method called with 'C' or 'P'.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in _LOCATOR_METHODS and node.args \
                and _is_type_char_constant(node.args[0]):
            yield f"(b) .{node.func.attr}() on a bare 'C'/'P'"

    # (c) OCC positional reads.
    if isinstance(node, ast.Subscript):
        bounds = _subscript_bounds(node)
        if bounds is None:
            return
        if bounds[0] == "index" and bounds[1] == _BANNED_INDEX:
            yield "(c) positional [-9] read"
        elif bounds[0] == "slice":
            lower, upper = bounds[1], bounds[2]
            if (lower, upper) in _BANNED_SLICES:
                low = '' if lower is None else lower
                up = '' if upper is None else upper
                yield f"(c) positional [{low}:{up}] read"


def _marker_lines(source: str):
    """Line numbers carrying the marker in a real COMMENT token.

    Tokenizing rather than substring-matching the raw line: a marker that
    appears inside a string literal must not exempt anything.
    """
    lines = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and MARKER in tok.string:
                lines.add(tok.start[0])
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        pass
    return lines


def findings_in(path: Path, repo_root: Path = None, honor_allowlist: bool = True):
    """Return ``[(lineno, line_text, rule)]`` for one file."""
    root = repo_root or REPO_ROOT
    rel = path.relative_to(root).as_posix()
    source = path.read_text(encoding="utf-8")
    raw_lines = source.splitlines()

    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - not our problem to police
        return []

    exempt = _marker_lines(source) if (
        honor_allowlist and rel in ALLOWLISTED_FILES) else set()

    out = []
    for node in ast.walk(tree):
        for rule in _findings_for_node(node):
            lineno = node.lineno
            if lineno in exempt:
                continue
            text = raw_lines[lineno - 1].strip() if lineno <= len(raw_lines) else ""
            out.append((lineno, text, rule))
    return sorted(set(out))


def walked_files(repo_root: Path = None):
    """Every Python file the gate covers. Shared with tests/test_lint_gates.py."""
    root = repo_root or REPO_ROOT
    files = []
    for d in WALKED_DIRS:
        base = root / d
        if base.exists():
            files.extend(sorted(base.rglob("*.py")))
    for f in WALKED_FILES:
        target = root / f
        if target.exists():
            files.append(target)
    return files


def _all_findings(honor_allowlist: bool = True):
    out = []
    for path in walked_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, text, rule in findings_in(
                path, honor_allowlist=honor_allowlist):
            out.append((rel, lineno, text, rule))
    return out


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def test_the_walk_covers_the_whole_served_surface():
    """Guard the guard: a broken walk must not read as 'no violations'."""
    rels = {p.relative_to(REPO_ROOT).as_posix() for p in walked_files()}
    assert len(rels) > 40, f"gate walked only {len(rels)} files — walk is broken"
    for required in (
        "src/strategy/wheel_engine.py",
        "src/data/options_scanner.py",
        "src/utils/option_symbols.py",
        "tools/testing/regression_monitor.py",
        # The reviewer's finding: deploy/ was outside the first version's walk,
        # and it is where the /run entrypoint lives.
        "deploy/cloud_run_server.py",
        "main.py",
    ):
        assert required in rels, f"{required} is not covered by the gate"


def test_no_occ_substring_classification():
    """No OCC symbol is classified by substring, split, or blind slice."""
    violations = [f"{rel}:{lineno}: {rule} -> {text}"
                  for rel, lineno, text, rule in _all_findings()]

    assert not violations, (
        "OCC substring/positional classification found — use "
        "strict_option_type() / parse_option_symbol() from "
        "src/utils/option_symbols.py instead:\n  " + "\n  ".join(violations))


def test_the_allowlist_holds_exactly_one_site():
    """The exemption set, pinned by file and line text.

    Run with the allow-list disabled, every marker-exempted line reappears as a
    finding. Asserting the exact set means a second marker — anywhere, including
    inside the allow-listed file — is a test diff rather than a silent
    widening. (Line *numbers* are deliberately excluded: they churn on every
    edit above the site and would make this a maintenance tax rather than a
    gate.)
    """
    assert ALLOWLISTED_FILES == ("src/utils/option_symbols.py",)

    with_allowlist = {(r, l, t) for r, l, t, _ in _all_findings(True)}
    assert with_allowlist == set(), with_allowlist

    exempted = {(rel, text) for rel, _lineno, text, _rule in _all_findings(False)}
    assert exempted == EXPECTED_EXEMPTIONS, (
        "the set of allow-listed OCC-substring sites changed.\n"
        f"  found:    {sorted(exempted)}\n"
        f"  expected: {sorted(EXPECTED_EXEMPTIONS)}")


# --------------------------------------------------------------------------- #
# The gate's own tests: every shape a rename or a rewrite could hide behind.
# --------------------------------------------------------------------------- #

EVASIONS = [
    ("bare membership",        "def f(s):\n    return 'C' in s\n"),
    ("renamed variable",       "def f(contract):\n    return 'P' in contract\n"),
    ("negated membership",     "def f(sym):\n    return 'C' not in sym\n"),
    ("subscript operand",      "def f(row):\n    return 'C' in row['contract']\n"),
    ("split on the type char", "def f(sym):\n    return sym.split('P')[0]\n"),
    ("find on the type char",  "def f(sym):\n    return sym.find('C') >= 0\n"),
    ("positional compare",     "def f(sym):\n    return sym[-9] == 'C'\n"),
    ("type-char slice",        "def f(sym):\n    return sym[-9:-8]\n"),
    ("date-part slice",        "def f(symbol):\n    return symbol[-15:-9]\n"),
    ("strike slice",           "def f(x):\n    return float(x[-8:]) / 1000\n"),
    ("root slice",             "def f(x):\n    return x[-15:]\n"),
    ("multi-line membership",  "def f(sym):\n    return ('C'\n            in sym)\n"),
    ("attribute operand",      "def f(opp):\n    return 'C' in opp.option_symbol\n"),
    ("startswith",             "def f(sym):\n    return sym.startswith('C')\n"),
]


def _probe(tmp_path, rel, source):
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


@pytest.mark.parametrize("label,source", EVASIONS, ids=[e[0] for e in EVASIONS])
def test_every_known_evasion_is_caught(tmp_path, label, source):
    probe = _probe(tmp_path, "src/probe.py", source)
    assert findings_in(probe, repo_root=tmp_path), (
        f"{label} slipped past the gate:\n{source}")


def test_an_evasion_inside_deploy_is_caught(tmp_path):
    """deploy/ is walked. The first version of this gate did not walk it."""
    _probe(tmp_path, "deploy/cloud_run_server.py",
           "def handler(sym):\n    return sym.split('C')[0]\n")
    files = walked_files(repo_root=tmp_path)
    rels = {p.relative_to(tmp_path).as_posix() for p in files}
    assert "deploy/cloud_run_server.py" in rels

    findings = findings_in(tmp_path / "deploy/cloud_run_server.py",
                           repo_root=tmp_path)
    assert findings, "a deploy/ evasion was not caught"


def test_main_py_is_walked(tmp_path):
    _probe(tmp_path, "main.py", "def f(sym):\n    return 'C' in sym\n")
    rels = {p.relative_to(tmp_path).as_posix()
            for p in walked_files(repo_root=tmp_path)}
    assert "main.py" in rels


def test_documented_out_of_scope_shapes_are_not_claimed(tmp_path):
    """Honesty pin: the docstring says these are NOT caught. If a future
    change starts catching them, update the docstring — do not let the two
    drift, because an over-claimed gate is worse than a narrow one."""
    probe = _probe(tmp_path, "src/probe.py",
                   "import re\n"
                   "CALL = 'C'\n"
                   "def f(sym, n):\n"
                   "    a = re.search('C', sym)\n"
                   "    b = any(c == 'C' for c in sym)\n"
                   "    c = CALL in sym\n"
                   "    d = sym[-n:]\n"
                   "    return a, b, c, d\n")
    assert findings_in(probe, repo_root=tmp_path) == []


def test_prose_is_not_code(tmp_path):
    """Docstrings and comments that merely mention the idiom do not trip it."""
    probe = _probe(tmp_path, "src/probe.py",
                   '"""Never write \'C\' in symbol; use strict_option_type."""\n'
                   "def f(sym):\n"
                   "    # 'C' in sym is the bug; sym[-8:] too\n"
                   "    return sym\n")
    assert findings_in(probe, repo_root=tmp_path) == []


def test_a_marker_outside_the_allowlisted_file_does_not_exempt(tmp_path):
    probe = _probe(tmp_path, "src/rogue.py",
                   "def f(option_symbol):\n"
                   "    return 'C' in option_symbol  # occ-substring-allowed\n")
    assert findings_in(probe, repo_root=tmp_path), (
        "a marker outside the allow-listed file exempted a line")


def test_a_marker_inside_the_allowlisted_file_exempts_only_its_own_line(tmp_path):
    probe = _probe(tmp_path, "src/utils/option_symbols.py",
                   "def f(sym):\n"
                   "    a = 'C' in sym  # occ-substring-allowed\n"
                   "    b = 'P' in sym\n"
                   "    return a, b\n")
    findings = findings_in(probe, repo_root=tmp_path)
    assert [lineno for lineno, _, _ in findings] == [3], findings


def test_a_marker_in_a_string_literal_does_not_exempt(tmp_path):
    probe = _probe(tmp_path, "src/utils/option_symbols.py",
                   "def f(sym):\n"
                   "    note = '# occ-substring-allowed'\n"
                   "    return 'C' in sym, note\n")
    assert findings_in(probe, repo_root=tmp_path), (
        "a marker inside a string literal exempted a line")
