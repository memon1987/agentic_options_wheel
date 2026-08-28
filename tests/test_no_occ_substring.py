"""Grep gate: no OCC symbol may be classified by substring or blind slice.

FC-079. Eight times now (FC-041/043/045/048/052/054, FC-069 item 12, and the
two reconcile sites this test shipped with) a piece of this system decided
whether an option symbol was a put or a call by asking whether the letter
``'C'`` appeared somewhere in it, or read a strike off a fixed ``[-8:]`` slice.
Both idioms are wrong for a third of the configured universe — ``AAPL``,
``SPY`` and ``PFE`` all carry a ``P`` or a ``C`` in the root — and both fail
silently, which is why the family kept recurring rather than being caught once.

This test is the stop. It walks the production tree (``src/``) and the served
detective layer (``tools/testing/``) for those idioms applied to anything that
looks like a symbol, and fails naming ``file:line``. The canonical primitives
are ``src/utils/option_symbols.strict_option_type`` (classification) and
``parse_option_symbol`` (underlying / strike / expiry) — use them.

**One allow-listed file**: ``src/utils/option_symbols.py`` itself, and only on
lines carrying the ``# occ-substring-allowed`` marker comment. That file is
where the canonical parsers live; its documented last-resort heuristic is the
one place the idiom is deliberate, and ``strict_option_type``'s docstring
explains why a caller must not inherit it. The allow-list is file-path **and**
marker: a marker comment anywhere else does not exempt a line.
"""

import ast
import io
import re
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Trees walked by the gate.
WALKED_DIRS = ("src", "tools/testing")

# The only file whose marker comments are honored.
ALLOWLISTED_FILES = ("src/utils/option_symbols.py",)

MARKER = "# occ-substring-allowed"

# ``'C' in <something with "sym" in it>`` — the classification idiom.
MEMBERSHIP_RE = re.compile(r"""(['"])[PC]\1\s+in\s+(.{0,60})""")
# ``sym[-9]`` — the "type character" positional read.
SLICE_TYPE_RE = re.compile(r"""([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*-\s*9\s*\]""")
# ``sym[-8:]`` — the "strike" positional read.
SLICE_STRIKE_RE = re.compile(r"""([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*-\s*8\s*:\s*\]""")


def _looks_like_symbol(text: str) -> bool:
    return "sym" in text.lower()


def _code_only_lines(source: str):
    """Yield ``(lineno, code_text)`` with docstrings and comments blanked.

    Docstrings legitimately quote the banned idiom (this module's own does),
    and so do explanatory comments; the gate is about executable code. Comments
    are stripped *after* the marker check, so the marker still works.
    """
    lines = source.splitlines()
    live = [True] * len(lines)

    tree = ast.parse(source)
    for node in ast.walk(tree):
        # Any bare string expression statement — module/class/function
        # docstrings and free-floating string blocks alike.
        if (isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            end = node.end_lineno or node.lineno
            for i in range(node.lineno - 1, end):
                if 0 <= i < len(live):
                    live[i] = False

    # Truncate each line at its comment token (tokenize knows a '#' inside a
    # string literal is not a comment).
    cut_at = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                cut_at[row] = min(cut_at.get(row, col), col)
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        pass

    for idx, raw in enumerate(lines):
        lineno = idx + 1
        if not live[idx]:
            continue
        code = raw[: cut_at[lineno]] if lineno in cut_at else raw
        if code.strip():
            yield lineno, code


def _violations_in(path: Path, repo_root: Path = None):
    """Return ``[(lineno, code, pattern_name)]`` for one file."""
    root = repo_root or REPO_ROOT
    rel = path.relative_to(root).as_posix()
    source = path.read_text(encoding="utf-8")
    raw_lines = source.splitlines()
    allowlisted_file = rel in ALLOWLISTED_FILES

    found = []
    for lineno, code in _code_only_lines(source):
        raw = raw_lines[lineno - 1]
        if allowlisted_file and MARKER in raw:
            continue

        hits = []
        for m in MEMBERSHIP_RE.finditer(code):
            if _looks_like_symbol(m.group(2)):
                hits.append("substring option-type test")
        for m in SLICE_TYPE_RE.finditer(code):
            if _looks_like_symbol(m.group(1)):
                hits.append("[-9] positional type read")
        for m in SLICE_STRIKE_RE.finditer(code):
            if _looks_like_symbol(m.group(1)):
                hits.append("[-8:] positional strike read")

        for hit in hits:
            found.append((lineno, code.strip(), hit))
    return found


def _walked_files():
    files = []
    for d in WALKED_DIRS:
        base = REPO_ROOT / d
        if not base.exists():  # pragma: no cover
            continue
        files.extend(sorted(p for p in base.rglob("*.py")))
    return files


def test_walked_tree_is_non_empty():
    """Guard the guard: a broken walk must not pass as 'no violations'."""
    files = _walked_files()
    assert len(files) > 20, f"gate walked only {len(files)} files — walk is broken"
    assert any(p.as_posix().endswith("src/strategy/wheel_engine.py") for p in files)
    assert any(p.as_posix().endswith("tools/testing/regression_monitor.py") for p in files)


def test_no_occ_substring_classification():
    """No OCC symbol is classified by substring or read by blind slice."""
    violations = []
    for path in _walked_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, code, pattern in _violations_in(path):
            violations.append(f"{rel}:{lineno}: [{pattern}] {code}")

    assert not violations, (
        "OCC substring/positional classification found — use "
        "strict_option_type() / parse_option_symbol() from "
        "src/utils/option_symbols.py instead:\n  "
        + "\n  ".join(violations)
    )


def test_allowlisted_marker_still_present():
    """The allow-listed file still carries its documented marker."""
    assert ALLOWLISTED_FILES == ("src/utils/option_symbols.py",)
    target = REPO_ROOT / "src/utils/option_symbols.py"
    assert MARKER in target.read_text(encoding="utf-8"), (
        "the allow-listed last-resort fallback lost its marker comment; "
        "either restore it or drop the file from ALLOWLISTED_FILES"
    )


def test_marker_outside_allowlisted_file_does_not_exempt(tmp_path):
    """A marker comment in another file must not silence the gate."""
    fake = tmp_path / "src" / "rogue.py"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        "def f(option_symbol):\n"
        "    return 'C' in option_symbol  # occ-substring-allowed\n",
        encoding="utf-8",
    )
    assert _violations_in(fake, repo_root=tmp_path), (
        "marker outside the allow-listed file exempted a line"
    )


def test_gate_detects_each_banned_pattern(tmp_path):
    """All three patterns are actually detected (the gate is not vacuous)."""
    fake = tmp_path / "src" / "probe.py"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        "def f(opt_sym):\n"
        "    a = 'C' in opt_sym\n"
        "    b = opt_sym[-9]\n"
        "    c = float(opt_sym[-8:]) / 1000.0\n"
        "    d = 'C' in some_other_variable\n"
        "    return a, b, c, d\n",
        encoding="utf-8",
    )
    hits = _violations_in(fake, repo_root=tmp_path)
    assert {h[2] for h in hits} == {
        "substring option-type test",
        "[-9] positional type read",
        "[-8:] positional strike read",
    }
    # The non-symbol variable on line 5 is not flagged.
    assert all(h[0] != 5 for h in hits)
