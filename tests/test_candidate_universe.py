"""Structural gate: `stocks.candidates` must stay invisible to the live path.

FC-096 Phase A (A1). `config/settings.yaml` now carries a SECOND universe:
`stocks.candidates`, symbols the backtest data layer keeps chains and bars
current for so they can be simulated on demand. They are **not traded**. The
entire safety argument for adding them to the trading bot's own config file —
rather than to a separate file, a separate service or a database — is that
nothing on the live path reads the key.

That is an argument about code, so it is checked as one. A prose comment saying
"do not read this" is exactly the control that failed in this repo before: an
"unused" knob acquires a reader six months later, in a PR whose diff looks
unrelated, and the first symptom is a real order on a symbol nobody meant to
trade.

WHAT THIS GATE CATCHES, via `ast` (name-agnostic, like
`tests/test_no_occ_substring.py`, whose walker it reuses so the two cannot
disagree about which files are covered):

  (a) any attribute access `<anything>.candidate_symbols` outside the four
      places allowed to have one;
  (b) any read of the literal `"candidates"` key off a stocks-shaped
      expression — `cfg["stocks"]["candidates"]`,
      `_config.get("stocks", {}).get("candidates")`, `stocks["candidates"]` —
      outside `src/utils/config.py`, which is where the accessor lives;
  (c) any occurrence of the dotted string `"stocks.candidates"`, which is how a
      config key is named in this repo's override tables, allowlists and
      dashboard payloads.

WHAT IT DOES NOT CATCH — a tripwire for known shapes, not a proof of absence.
Out of scope, deliberately: a key held in a variable (`k = "candid" + "ates"`),
a read through `getattr(config, name)` with a computed name, a YAML re-read
that bypasses `Config` entirely, and anything reached through `eval`. The real
control is that the live universe comes from `Config.stock_symbols` and nowhere
else; this gate makes the *known* ways of breaking that loud.

Rule (a)'s allow-list is four paths and no marker comments, because unlike the
OCC gate there is no legitimate in-file exception to carve out: either a file
is one of the four consumers, or it must not read the key at all.
"""

import ast
from pathlib import Path

import pytest

from src.utils.config import Config
from tests.test_no_occ_substring import walked_files

REPO_ROOT = Path(__file__).resolve().parent.parent

ACCESSOR = "candidate_symbols"
YAML_KEY = "candidates"
STOCKS_KEY = "stocks"
DOTTED = "stocks.candidates"

# The only files allowed to reach for the candidate universe.
#
#   src/utils/config.py        - defines the accessor
#   src/backtesting/           - the consumer's home (the data layer)
#   main.py                    - `--command backfill`, which resolves the set
#   tests/                     - this file and the backfill's own tests
ALLOWED_ACCESSOR_PREFIXES = (
    "src/utils/config.py",
    "src/backtesting/",
    "main.py",
    "tests/",
)

# Reading the raw YAML key is narrower still: `Config` is the only thing that
# may know how the universe is spelled in the file.
ALLOWED_RAW_KEY_FILES = ("src/utils/config.py",)


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #
def _dashboard_files(root: Path):
    """The dashboard backend, which `walked_files` does not cover.

    It has to be in scope: the dashboard is the half of the system that would
    plausibly want to *show* candidates, and a submit path that resolved a
    universe from this key would put a candidate into a real sweep spec — and,
    one copy-paste later, into something that trades.
    """
    base = root / "dashboard" / "backend"
    return sorted(base.rglob("*.py")) if base.exists() else []


def covered_files(root: Path = None):
    root = root or REPO_ROOT
    return list(walked_files(repo_root=root)) + _dashboard_files(root)


def _is_stocks_lookup(node) -> bool:
    """Whether ``node`` evaluates to the `stocks` section of a config mapping."""
    if isinstance(node, ast.Name):
        return node.id == STOCKS_KEY
    if isinstance(node, ast.Attribute):
        return node.attr == STOCKS_KEY
    if isinstance(node, ast.Subscript):
        return _is_constant(node.slice, STOCKS_KEY)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get" and node.args:
            return _is_constant(node.args[0], STOCKS_KEY)
    return False


def _is_constant(node, value: str) -> bool:
    # ast.Index wraps the slice on Python < 3.9 semantics; unwrap defensively.
    node = getattr(node, "value", node) if isinstance(node, ast.Index) else node
    return isinstance(node, ast.Constant) and node.value == value


def findings_in(path: Path, root: Path = None):
    """``[(lineno, text, rule)]`` for one file."""
    root = root or REPO_ROOT
    rel = path.relative_to(root).as_posix()
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - not our problem to police
        return []
    lines = source.splitlines()

    allows_accessor = rel.startswith(ALLOWED_ACCESSOR_PREFIXES)
    allows_raw_key = rel in ALLOWED_RAW_KEY_FILES

    out = []

    def record(node, rule):
        lineno = getattr(node, "lineno", 0)
        text = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
        out.append((lineno, text, rule))

    for node in ast.walk(tree):
        if (not allows_accessor and isinstance(node, ast.Attribute)
                and node.attr == ACCESSOR):
            record(node, "accessor")
        if not allows_raw_key:
            if isinstance(node, ast.Subscript) and _is_constant(node.slice, YAML_KEY):
                if _is_stocks_lookup(node.value):
                    record(node, "raw_key")
            if isinstance(node, ast.Call):
                func = node.func
                if (isinstance(func, ast.Attribute) and func.attr == "get"
                        and node.args and _is_constant(node.args[0], YAML_KEY)
                        and _is_stocks_lookup(func.value)):
                    record(node, "raw_key")
            if isinstance(node, ast.Constant) and node.value == DOTTED:
                record(node, "dotted_key")

    return sorted(set(out))


def _all_findings(root: Path = None):
    root = root or REPO_ROOT
    out = []
    for path in covered_files(root):
        rel = path.relative_to(root).as_posix()
        for lineno, text, rule in findings_in(path, root):
            out.append((rel, lineno, text, rule))
    return out


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def test_the_walk_covers_the_live_path():
    """Guard the guard: a broken walk must not read as 'no violations'."""
    rels = {p.relative_to(REPO_ROOT).as_posix() for p in covered_files()}
    assert len(rels) > 40, f"gate walked only {len(rels)} files — walk is broken"
    for required in (
        # Every place a universe is read or an order is placed.
        "src/data/options_scanner.py",
        "src/api/market_data.py",
        "src/strategy/put_seller.py",
        "src/strategy/call_seller.py",
        "src/strategy/wheel_engine.py",
        "deploy/cloud_run_server.py",
        "main.py",
        # ...and the dashboard, which `walked_files` alone does not include.
        "dashboard/backend/services/sweeps.py",
    ):
        assert required in rels, f"{required} is not covered by the gate"


def test_nothing_on_the_live_path_reads_the_candidate_universe():
    findings = _all_findings()
    assert not findings, (
        "`stocks.candidates` is an EVALUATION-only universe (FC-096 Phase A). "
        "Its whole safety argument is that the live trading path has no reader "
        "for it. Found:\n"
        + "\n".join(f"  {rel}:{line}  [{rule}]  {text}"
                    for rel, line, text, rule in findings)
        + "\n\nIf a new consumer is genuinely wanted, it belongs in "
          "src/backtesting/ (or main.py's backfill command) — and if it is "
          "wanted on the live path, that is a decision to promote the symbols "
          "into `stocks.symbols`, not to widen this allow-list."
    )


# --------------------------------------------------------------------------- #
# The gate catches what it claims to
# --------------------------------------------------------------------------- #
def _probe(tmp_path, rel, source):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return findings_in(path, root=tmp_path)


@pytest.mark.parametrize("label,source,rule", [
    ("attribute read", "def f(config):\n    return config.candidate_symbols\n",
     "accessor"),
    ("attribute read, renamed variable",
     "def f(c):\n    return list(c.candidate_symbols)\n", "accessor"),
    ("subscript on a stocks subscript",
     "def f(cfg):\n    return cfg['stocks']['candidates']\n", "raw_key"),
    ("get on a stocks get",
     "def f(cfg):\n    return cfg.get('stocks', {}).get('candidates', [])\n",
     "raw_key"),
    ("subscript on a bare stocks name",
     "def f(stocks):\n    return stocks['candidates']\n", "raw_key"),
    ("subscript on a stocks attribute",
     "def f(o):\n    return o.stocks['candidates']\n", "raw_key"),
    ("the dotted key as a string",
     "KEYS = ['stocks.candidates']\n", "dotted_key"),
])
def test_every_known_shape_is_caught(tmp_path, label, source, rule):
    found = _probe(tmp_path, "src/strategy/sneaky.py", source)
    assert [f[2] for f in found] == [rule], f"{label}: {found}"


def test_the_dashboard_is_walked(tmp_path):
    """The first version of this file did not walk `dashboard/backend`."""
    found = _probe(tmp_path, "dashboard/backend/services/sneaky.py",
                   "def f(config):\n    return config.candidate_symbols\n")
    assert [f[2] for f in found] == ["accessor"]
    assert (tmp_path / "dashboard" / "backend" / "services" / "sneaky.py") in \
        set(covered_files(tmp_path))


@pytest.mark.parametrize("rel", [
    "src/utils/config.py",
    "src/backtesting/data/backfill.py",
    "main.py",
])
def test_the_allowed_consumers_may_read_the_accessor(tmp_path, rel):
    assert _probe(tmp_path, rel,
                  "def f(config):\n    return config.candidate_symbols\n") == []


def test_only_config_may_read_the_raw_yaml_key(tmp_path):
    source = "def f(cfg):\n    return cfg.get('stocks', {}).get('candidates', [])\n"
    assert _probe(tmp_path, "src/utils/config.py", source) == []
    assert [f[2] for f in _probe(tmp_path, "src/backtesting/data/backfill.py",
                                 source)] == ["raw_key"]


def test_an_unrelated_candidates_key_is_not_a_finding(tmp_path):
    """`candidates` is a common word; only the STOCKS section is guarded.

    Two live examples this must not flag: the `candidates` INTEGER column in
    `analytics_writer`'s schema and the `"candidates"` field in a
    `decision_record` payload. Neither is the universe, and a gate that cried
    wolf on them would be turned off.
    """
    source = (
        "SCHEMA = [Field('candidates', 'INTEGER')]\n"
        "def row(candidates):\n"
        "    return {'candidates': int(candidates)}\n"
        "def read(bq_row):\n"
        "    return bq_row['candidates']\n"
    )
    assert _probe(tmp_path, "src/data/analytics_writer.py", source) == []


# --------------------------------------------------------------------------- #
# The accessor itself
# --------------------------------------------------------------------------- #
class TestCandidateAccessor:
    def test_reads_the_configured_list(self, tmp_path):
        config = _config_with(tmp_path, {"symbols": ["AAPL"],
                                         "candidates": ["TSLA", "COST"]})
        assert config.candidate_symbols == ["TSLA", "COST"]
        assert config.stock_symbols == ["AAPL"]

    def test_absent_key_is_an_empty_list_not_a_keyerror(self, tmp_path):
        config = _config_with(tmp_path, {"symbols": ["AAPL"]})
        assert config.candidate_symbols == []

    def test_the_shipped_config_seeds_it_empty(self):
        """A non-empty default would backfill symbols nobody asked for."""
        assert Config("config/settings.yaml").candidate_symbols == []

    def test_the_shipped_config_keeps_the_two_universes_disjoint(self):
        config = Config("config/settings.yaml")
        overlap = set(config.stock_symbols) & set(config.candidate_symbols)
        assert not overlap, (
            f"{sorted(overlap)} is both traded and a candidate; a symbol is "
            "one or the other, and promotion is a deliberate move between the "
            "two lists"
        )


def _config_with(tmp_path, stocks: dict) -> Config:
    """A Config built from the shipped settings with `stocks:` replaced."""
    import yaml

    payload = yaml.safe_load(Path("config/settings.yaml").read_text())
    payload["stocks"] = stocks
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(payload))
    return Config(str(path))
