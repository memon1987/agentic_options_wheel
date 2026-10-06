"""T-1 (FC-121 PR-2): every intra-repo ``src.*`` import in shipped code resolves.

Most callers of the engine import it LAZILY, inside a function — the sim
service, the sweep CLI/Job and the battery all do — so a module deleted out from
under one of those imports fails nothing at import time and nothing at service
start. It fails the first time that code path runs (a sim submit, a battery
Saturday), and a test catches it only if some test happens to execute that exact
path. FC-121 deleted two modules that ten such lazy sites in ``main.py`` and
``deploy/sim_service.py`` imported from; this test is what makes a missed one
fail here instead of in production.

How: every ``*.py`` under ``src/``, ``deploy/`` and ``tools/``, plus ``main.py``,
is PARSED, never executed. Every ``import`` / ``from ... import ...`` node at
any nesting depth (module level, function bodies, ``try`` blocks, class bodies)
whose target is a ``src.*`` module — a relative import is resolved against the
file's own package first — must name a module that imports, and every name it
imports must exist on that module, as an attribute or as a submodule (which is
how ``from pkg import mod`` resolves).

Not checked: tests (they import what they test and fail on their own), imports
of anything outside ``src`` (stdlib and third-party are pip's concern), and
imports spelled as strings (``importlib.import_module(...)``, ``patch(...)``).
"""

from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]

SCANNED_ROOTS = ("src", "deploy", "tools")
SCANNED_FILES = ("main.py",)

#: ``{file: the one src module it may fail to resolve}`` — exactly two entries.
#:
#: FC-069 S1 deleted ``src/risk/gap_detector.py`` on purpose. These two study
#: harnesses import it and are kept anyway, as the record of the FC-002 and
#: FC-036 studies (FC-049); each file's own docstring says it no longer runs.
#: Keyed by module as well as by file, so any OTHER unresolved import in either
#: file still fails. ``test_the_allowlist_is_still_needed`` fails the day an
#: entry stops being true, so the allowlist cannot outlive its reason.
ALLOWED_UNRESOLVED: Dict[str, str] = {
    "tools/diagnostics/fc002_gap_filter_ab.py": "src.risk.gap_detector",
    "tools/diagnostics/fc036_gap_gate_study.py": "src.risk.gap_detector",
}


@dataclass(frozen=True)
class SrcImport:
    rel: str                  # repo-relative path of the importing file
    lineno: int
    statement: str            # the import as written
    module: str               # the absolute module it targets
    names: Tuple[str, ...]    # names imported FROM it; () for `import x.y`
    nested: bool              # not a module-level statement
    relative: bool            # written as `from .x import y`


def _scanned_files() -> List[Path]:
    files = [REPO / name for name in SCANNED_FILES]
    for root in SCANNED_ROOTS:
        files.extend(sorted((REPO / root).rglob("*.py")))
    return [path for path in files if "__pycache__" not in path.parts]


def _package_of(rel: str) -> str:
    """The package a relative import in ``rel`` resolves against.

    ``src/a/b.py`` and ``src/a/__init__.py`` both resolve against ``src.a``.
    """
    return ".".join(rel[: -len(".py")].split("/")[:-1])


def _resolve(package: str, level: int, module: Optional[str]) -> Optional[str]:
    """The absolute target of ``from <level dots><module> import ...``."""
    if level == 0:
        return module
    parts = package.split(".") if package else []
    if level - 1 > len(parts):
        return None  # climbs above the top-level package
    parts = parts[: len(parts) - (level - 1)]
    if module:
        parts += module.split(".")
    return ".".join(parts) or None


def _is_src(module: Optional[str]) -> bool:
    return bool(module) and (module == "src" or module.startswith("src."))


def collect_src_imports() -> List[SrcImport]:
    """Every import node targeting ``src.*`` in the scanned files, any depth."""
    found: List[SrcImport] = []
    for path in _scanned_files():
        rel = path.relative_to(REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        top_level = {id(node) for node in tree.body}
        package = _package_of(rel)
        for node in ast.walk(tree):
            nested = id(node) not in top_level
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_src(alias.name):
                        found.append(SrcImport(
                            rel, node.lineno, ast.unparse(node), alias.name,
                            (), nested, False))
            elif isinstance(node, ast.ImportFrom):
                target = _resolve(package, node.level, node.module)
                if _is_src(target):
                    found.append(SrcImport(
                        rel, node.lineno, ast.unparse(node), target,
                        tuple(alias.name for alias in node.names), nested,
                        node.level > 0))
    return found


def _import(dotted: str) -> Tuple[object, Optional[Tuple[str, str]]]:
    """``(module, None)``, or ``(None, (missing dotted name, error))``."""
    try:
        return importlib.import_module(dotted), None
    except Exception as exc:  # noqa: BLE001 - every failure is a finding
        missing = getattr(exc, "name", None) or dotted
        return None, (missing, f"{type(exc).__name__}: {exc}")


def unresolved(imp: SrcImport) -> List[Tuple[str, str]]:
    """``[(missing dotted name, error)]`` for one import; empty if it resolves."""
    module, failure = _import(imp.module)
    if failure:
        return [failure]
    problems = []
    for name in imp.names:
        if name == "*" or hasattr(module, name):
            continue
        _, failure = _import(f"{imp.module}.{name}")
        if failure:
            problems.append((failure[0],
                             f"{imp.module} has no {name!r} ({failure[1]})"))
    return problems


def _failures() -> List[Tuple[SrcImport, str, str]]:
    out = []
    for imp in collect_src_imports():
        for missing, error in unresolved(imp):
            out.append((imp, missing, error))
    return out


class TestIntraRepoImportsResolve:
    def test_every_src_import_resolves(self):
        offenders = [
            f"{imp.rel}:{imp.lineno}: {imp.statement}  ->  {error}"
            for imp, missing, error in _failures()
            if ALLOWED_UNRESOLVED.get(imp.rel) != missing
        ]
        assert offenders == [], (
            "an import names a src module or attribute that does not exist. A "
            "lazy one fails only when its code path first runs — in production, "
            "not at import or service start:\n" + "\n".join(offenders))

    def test_the_allowlist_is_still_needed(self):
        failing = {(imp.rel, missing) for imp, missing, _ in _failures()}
        for rel, module in ALLOWED_UNRESOLVED.items():
            assert (REPO / rel).is_file(), (
                f"{rel} no longer exists — drop it from ALLOWED_UNRESOLVED")
            assert (rel, module) in failing, (
                f"{rel} no longer fails to resolve {module} — drop it from "
                f"ALLOWED_UNRESOLVED so the file is checked like every other")

    def test_the_walk_reaches_lazy_and_relative_imports(self):
        """Without this, a walker that only saw module-level statements — or
        found no files at all — would pass the first test vacuously."""
        imports = collect_src_imports()
        nested_in = {imp.rel for imp in imports if imp.nested}
        assert "deploy/sim_service.py" in nested_in
        assert "main.py" in nested_in
        assert any(imp.relative for imp in imports), (
            "no relative import was resolved; src/ uses them throughout")
        for root in SCANNED_ROOTS:
            assert any(imp.rel.startswith(f"{root}/") for imp in imports), root
