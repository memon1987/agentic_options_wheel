"""The engine-identity hash and the dedup re-key that rides on it (FC-096 B1).

`sweep_key` used to carry `git_commit`. That invalidated every stored sweep
result on every merge to `main`, including the merges that cannot change a
replay. It now carries `engine_identity` — the content hash of `src/**` — and
the whole value of the change is in one property pair:

* it MOVES when the replayed code moves (a strategy byte, a data-table byte);
* it does NOT move when anything else moves (docs, dashboard, deploy, build).

Break the first half and stale results are served as fresh ones, silently.
Break the second half and the change bought nothing. So both halves are tested
against the REAL tree, copied to a tmpdir and mutated, rather than against a
synthetic fixture that could agree with a wrong boundary.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.backtesting.scenarios import engine_identity as EI
from src.backtesting.scenarios.identity import sweep_key

REPO_ROOT = Path(__file__).resolve().parent.parent

SPEC = {
    "symbols": ["AAPL", "NVDA"],
    "start": "2025-08-01",
    "end": "2026-07-31",
    "scenarios": [{"name": "tighter",
                   "overrides": {"strategy.min_put_premium": 0.75}}],
}


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    """A throwaway copy of the real `src/` tree, hashed and re-hashed.

    Module-scoped and copied ONCE: the tests mutate a file, hash, and restore it
    in a finally, so they share the copy without leaking into each other. 1.3 MB
    and 69 files — copying it per test would be waste, not isolation.

    `EXTRA_ROOT_FILES` are resolved from the tree's PARENT, so the copy is a
    miniature repo root: `src/` beside `requirements.txt`, exactly the layout
    `deploy/Dockerfile` produces at `/app`.
    """
    root = tmp_path_factory.mktemp("repo_copy")
    shutil.copytree(REPO_ROOT / "src", root / "src")
    for name in EI.EXTRA_ROOT_FILES:
        shutil.copy(REPO_ROOT / name, root / name)
    return root / "src"


def _mutate(path: Path, extra: bytes = b"\n# fc-096 identity probe\n"):
    """Append bytes, return a restore callable. Content-only, no metadata."""
    original = path.read_bytes()
    path.write_bytes(original + extra)
    return lambda: path.write_bytes(original)


# ==========================================================================
# (1) The boundary: what moves the hash, and what must not
# ==========================================================================
class TestWhatTheIdentityIsSensitiveTo:
    def test_a_strategy_byte_changes_it(self, tree):
        """The replay executes the LIVE strategy — this is the whole point.

        A `put_seller.py` edit changes which options a replayed day sells, so
        every stored result for every spec is now measuring a different engine.
        A hash that missed this would serve the old numbers as the new engine's.
        """
        before = EI.compute_identity(tree)
        restore = _mutate(tree / "strategy" / "put_seller.py")
        try:
            assert EI.compute_identity(tree) != before
        finally:
            restore()
        assert EI.compute_identity(tree) == before

    @pytest.mark.parametrize(
        "table", ["earnings_dates.json", "dividend_history.json"])
    def test_a_data_table_byte_changes_it(self, tree, table):
        """The two committed JSON tables are replay INPUTS, not documentation.

        `earnings_dates.json` drives the FC-013 gate's blackout days;
        `dividend_history.json` drives the dividend credit. A vendor correction
        to either changes a replay's numbers without changing a line of code —
        which is exactly why the hash covers ALL file types under `src/`, not
        just `*.py`.
        """
        path = tree / "backtesting" / "data" / table
        payload = json.loads(path.read_text())
        assert payload, f"{table} is empty; this test would prove nothing"

        before = EI.compute_identity(tree)
        restore = _mutate(path, b" ")   # a byte JSON itself does not care about
        try:
            assert EI.compute_identity(tree) != before, (
                f"a byte of {table} did not move the identity — a corrected "
                "vendor table would be served with the pre-correction results"
            )
        finally:
            restore()

    def test_a_requirements_txt_byte_changes_it(self, tree):
        """The replay's arithmetic runs inside pandas, numpy and the Alpaca SDK.

        Under the old `git_commit` key a dependency edit invalidated every
        stored result. A `src/**`-only hash would have quietly stopped doing
        that — a REGRESSION hidden inside an improvement, which is the kind that
        survives review. `requirements.txt` is in the digest so it does not.
        """
        before = EI.compute_identity(tree)
        restore = _mutate(tree.parent / "requirements.txt", b"\nfaker\n")
        try:
            assert EI.compute_identity(tree) != before, (
                "a dependency edit no longer invalidates the dedup key; a "
                "different pandas would serve the old numbers as the new ones"
            )
        finally:
            restore()
        assert EI.compute_identity(tree) == before

    def test_the_real_requirements_txt_is_in_the_manifest(self):
        """Path-framed at the repo root, not under `src/`."""
        manifest = EI.identity_manifest()
        assert "requirements.txt" in manifest
        assert manifest["requirements.txt"] > 0

    def test_a_missing_extra_file_is_refused_not_ignored(self, tmp_path):
        """Hashing a tree without it would produce a digest that silently means
        something else — the same value a complete tree could legitimately
        produce. Refusing is the only honest answer."""
        lonely = tmp_path / "src"
        lonely.mkdir()
        (lonely / "mod.py").write_text("x = 1\n")
        with pytest.raises(EI.EngineTreeError) as exc:
            EI.compute_identity(lonely)
        assert "requirements.txt" in str(exc.value)

    def test_a_new_file_changes_it(self, tree):
        """Adding a module is a change even if nothing imports it yet."""
        before = EI.compute_identity(tree)
        added = tree / "strategy" / "_fc096_probe.py"
        added.write_text("# nothing\n")
        try:
            assert EI.compute_identity(tree) != before
        finally:
            added.unlink()

    def test_a_pure_rename_changes_it(self, tree):
        """Same bytes, different module path, different engine.

        The path is hashed alongside the contents precisely so this is true: two
        trees whose files hold identical bytes under different names do not
        import the same way and do not run the same way.
        """
        before = EI.compute_identity(tree)
        original = tree / "strategy" / "put_seller.py"
        moved = tree / "strategy" / "put_seller_renamed.py"
        original.rename(moved)
        try:
            assert EI.compute_identity(tree) != before
        finally:
            moved.rename(original)

    def test_engine_version_is_mixed_in(self, tree):
        """An operator bumping ENGINE_VERSION means to invalidate the cache."""
        assert (EI.compute_identity(tree, engine_version="a")
                != EI.compute_identity(tree, engine_version="b"))


class TestWhatTheIdentityIsBlindTo:
    """The half that makes the re-key worth doing at all.

    Under the old `git_commit` key each of these invalidated every stored result
    in the table. If they still do, the change bought nothing and the weekly
    battery still re-measures everything every week.
    """

    @pytest.mark.parametrize("relative", [
        "docs/bigquery/scenario_runs.md",
        "dashboard/backend/services/sweeps.py",
        "deploy/Dockerfile",
        "cloudbuild.yaml",
        "README.md",
        "tests/test_engine_identity.py",
    ])
    def test_a_file_outside_src_cannot_move_the_identity(self, relative):
        """Asserted structurally: the file is not in the hashed manifest.

        Mutating the real repo file would be the more direct test and is not
        worth the hazard of a failed restore. The manifest IS the input set, so
        absence from it is the property.
        """
        assert (REPO_ROOT / relative).exists(), f"{relative} moved; fix the test"
        manifest = EI.identity_manifest()
        assert relative not in manifest
        # The manifest IS the input set: `src/**` plus the named extras and
        # nothing else. Spelling the extras out here rather than allowing any
        # repo-root file keeps a future addition from slipping in unreviewed.
        assert all(key.startswith("src/") or key in EI.EXTRA_ROOT_FILES
                   for key in manifest)

    def test_bytecode_and_pycache_are_skipped(self, tree):
        """mtime-immune and `__pycache__`-immune, both required by B1.

        A fresh Job checkout has no bytecode; a developer's tree that has
        imported the engine a hundred times is full of it. They must agree.
        """
        before = EI.compute_identity(tree)
        cache = tree / "strategy" / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "put_seller.cpython-311.pyc").write_bytes(b"\x00\x01compiled")
        stray = tree / "strategy" / "stray.pyc"
        stray.write_bytes(b"\x00\x02compiled")
        try:
            assert EI.compute_identity(tree) == before
        finally:
            shutil.rmtree(cache)
            stray.unlink()

    def test_mtime_alone_does_not_move_it(self, tree):
        """Content-only. A checkout stamps every file with the checkout time."""
        before = EI.compute_identity(tree)
        target = tree / "strategy" / "put_seller.py"
        os.utime(target, (1_000_000_000, 1_000_000_000))
        assert EI.compute_identity(tree) == before


class TestTheWalkOrderCannotMoveTheDigest:
    """The `entries.sort()` in `_walk` is load-bearing, and a single-filesystem
    test cannot prove it.

    A reviewer deleted the sort and all 28 tests still passed: one filesystem
    returns one readdir order, consistently, so every hash in the suite agreed
    with every other. Production does not have that luxury — the Cloud Run Job
    and the Cloud Build worker that stamps the dashboard image are two different
    filesystems, and a digest that depended on readdir order would simply never
    match across them. The dedup would stop firing, silently, and the symptom
    would be "sweeps got slow".

    So the walk order is varied deliberately and the digest must not move.
    """

    # Bound at class-definition time: the tests monkeypatch `EI.os.walk`, and
    # `EI.os` IS the stdlib module, so a harness that called `os.walk` by name
    # would call itself. This reference is captured before any patch exists.
    _REAL_WALK = staticmethod(os.walk)

    @classmethod
    def _shuffled_walk(cls, reverse_dirs, reverse_files):
        """A stand-in for `os.walk` that yields the same tree in another order.

        The real walk is materialised FIRST (with `__pycache__` pruning applied
        here, since `_walk`'s own in-place pruning is meaningless once the
        results are a list), then re-ordered. That is what a different
        filesystem does: same files, different sequence.
        """
        real = cls._REAL_WALK

        def walker(top, *args, **kwargs):
            triples = [
                (dirpath, dirnames, filenames)
                for dirpath, dirnames, filenames in real(top)
                if not any(part in EI.SKIP_DIR_NAMES
                           for part in Path(dirpath).parts)
            ]
            if reverse_dirs:
                triples.reverse()
            for dirpath, dirnames, filenames in triples:
                names = list(filenames)
                if reverse_files:
                    names.reverse()
                yield dirpath, list(dirnames), names
        return walker

    @pytest.mark.parametrize("reverse_dirs,reverse_files", [
        (False, True), (True, False), (True, True),
    ])
    def test_reordering_the_walk_does_not_move_the_digest(
            self, tree, monkeypatch, reverse_dirs, reverse_files):
        baseline = EI.compute_identity(tree)
        monkeypatch.setattr(
            EI.os, "walk",
            self._shuffled_walk(reverse_dirs, reverse_files))
        assert EI.compute_identity(tree) == baseline, (
            "the digest depends on the order os.walk happens to return — the "
            "Job and the dashboard build run on different filesystems and "
            "would compute different keys for the identical tree"
        )

    def test_the_reordering_harness_actually_reorders(self, tree, monkeypatch):
        """A harness that silently did nothing would make the test above vacuous
        — it would pass against a deleted sort, which is exactly the mutant it
        exists to kill."""
        plain = [rel for rel, _ in EI._walk(tree)]
        harness = self._shuffled_walk(True, True)

        # The RAW sequence the harness hands `_walk`, before `_walk` sorts it.
        raw = []
        for dirpath, _dirs, files in harness(tree):
            raw.extend(str(Path(dirpath) / f) for f in files)
        assert len(raw) > 1
        assert raw != sorted(raw), (
            "the harness returned an already-sorted order; it proves nothing, "
            "and the invariance tests above would pass against a deleted sort"
        )

        # ...and `_walk` still produces the identical, sorted list.
        monkeypatch.setattr(EI.os, "walk", harness)
        assert [rel for rel, _ in EI._walk(tree)] == plain


class TestSymlinksAreRefused:
    """A followed link reads bytes from outside the boundary the hash is defined
    by, and a linked DIRECTORY can add an invisible subtree or a cycle.

    Skipping them silently would be worse than either: a real source file moved
    behind a link would stop being noticed, and the identity would keep looking
    healthy. `find src -type l` is empty today, so refusing costs nothing.
    """

    def test_a_symlinked_file_under_src_raises(self, tree):
        link = tree / "strategy" / "_linked.py"
        link.symlink_to(tree / "strategy" / "put_seller.py")
        try:
            with pytest.raises(EI.EngineTreeError) as exc:
                EI.compute_identity(tree)
            assert "symlink" in str(exc.value)
        finally:
            link.unlink()
        assert EI.compute_identity(tree)   # and the tree hashes again after

    def test_a_symlinked_directory_under_src_raises(self, tree, tmp_path):
        outside = tmp_path / "outside_tree"
        outside.mkdir(exist_ok=True)
        (outside / "smuggled.py").write_text("# not part of the engine\n")
        link = tree / "strategy" / "_linked_dir"
        link.symlink_to(outside, target_is_directory=True)
        try:
            with pytest.raises(EI.EngineTreeError) as exc:
                EI.compute_identity(tree)
            assert "symlink" in str(exc.value)
        finally:
            link.unlink()

    def test_the_repo_has_no_symlinks_under_src(self):
        """States the precondition the refusal relies on, so a future commit
        that adds one fails here with an explanation rather than in a build."""
        links = [p for p in (REPO_ROOT / "src").rglob("*") if p.is_symlink()]
        assert links == [], f"symlinks under src/: {links}"


# ==========================================================================
# (2) Determinism, and the standalone entry point the build depends on
# ==========================================================================
class TestTheIdentityIsDeterministic:
    def test_repeated_calls_agree(self, tree):
        assert EI.compute_identity(tree) == EI.compute_identity(tree)

    def test_a_copy_of_the_tree_hashes_the_same(self, tree, tmp_path):
        """Different inodes, different mtimes, different absolute paths.

        This is the Job-vs-dashboard-build situation in miniature: two checkouts
        of the same commit on two machines must produce the same 16 hex
        characters or the dedup never fires across them.
        """
        clone_root = tmp_path / "clone"
        clone_root.mkdir()
        shutil.copytree(tree, clone_root / "src")
        for name in EI.EXTRA_ROOT_FILES:
            shutil.copy(tree.parent / name, clone_root / name)
        assert (EI.compute_identity(clone_root / "src")
                == EI.compute_identity(tree))

    def test_the_module_run_as_a_script_prints_the_same_value(self):
        """`python src/backtesting/scenarios/engine_identity.py` — the form
        cloudbuild.yaml uses, and the only one that works without the engine's
        dependencies installed.

        Run twice with different `PYTHONHASHSEED`s: nothing in the digest may
        depend on set or dict iteration order, which is the FC-092 item-2 class
        of defect one layer down.
        """
        outputs = []
        for seed in ("0", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.run(
                [sys.executable, "-B",
                 "src/backtesting/scenarios/engine_identity.py"],
                cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
            assert proc.returncode == 0, proc.stderr[-2000:]
            outputs.append(proc.stdout.strip())
        assert outputs[0] == outputs[1]
        assert outputs[0] == EI.engine_identity()
        assert len(outputs[0]) == 16 and set(outputs[0]) <= set("0123456789abcdef")

    def test_it_runs_with_no_third_party_packages_importable(self, tmp_path):
        """The cloudbuild step is a bare `python:3.11-slim` with no pip install.

        Reproduced by running the module with an EMPTY sys.path addition and no
        site-packages: `-S` skips `site`, so anything the module imports beyond
        the standard library fails here exactly as it would fail the build.
        """
        proc = subprocess.run(
            [sys.executable, "-S", "-B",
             "src/backtesting/scenarios/engine_identity.py"],
            cwd=str(REPO_ROOT),
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                 "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True)
        assert proc.returncode == 0, (
            "the module must import with nothing but the standard library — "
            f"cloudbuild.yaml runs it in a bare python image:\n{proc.stderr[-2000:]}"
        )
        assert proc.stdout.strip() == EI.engine_identity()

    def test_engine_identity_is_cached_per_process(self, monkeypatch):
        """Called once per row-stamp path; must not re-read 69 files each time."""
        EI.engine_identity()          # warm
        calls = []
        monkeypatch.setattr(EI, "compute_identity",
                            lambda *a, **k: calls.append(1) or "x" * 16)
        assert EI.engine_identity() == EI.engine_identity()
        assert calls == []


class TestTheEngineVersionIsNotAFork:
    def test_it_matches_screens_declaration(self):
        """Duplicated because `screen.py` imports the engine and this module
        must stay stdlib-only. Pinned, because a fork would make the Job and the
        dashboard key differently for ever."""
        from src.backtesting.screen import ENGINE_VERSION
        assert EI.ENGINE_VERSION == ENGINE_VERSION

    def test_the_dashboard_copy_matches_too(self):
        # See tests/_dashboard_path.py: the backend is APPENDED and the repo
        # root kept ahead of it, so `import main` still resolves to the CLI.
        from tests._dashboard_path import add_dashboard_backend_to_path
        add_dashboard_backend_to_path()
        import services.sweeps as S
        assert S.ENGINE_VERSION == EI.ENGINE_VERSION


# ==========================================================================
# (3) The key migration
# ==========================================================================
class TestTheKeyMigration:
    def test_the_key_is_keyed_on_identity_not_commit(self):
        """The signature change is the migration: a caller still passing a
        commit SHA gets a key, and it is a DIFFERENT key from the one the same
        spec had before — which is the one-time invalidation the plan accepts on
        the record, not a bug."""
        a = sweep_key(SPEC, engine_version="v", engine_identity="aaaaaaaaaaaaaaaa")
        b = sweep_key(SPEC, engine_version="v", engine_identity="bbbbbbbbbbbbbbbb")
        assert a != b

    def test_two_identities_that_agree_produce_one_key(self):
        """The property the whole dashboard build-arg exists to provide."""
        identity = EI.engine_identity()
        assert (sweep_key(SPEC, engine_version="v", engine_identity=identity)
                == sweep_key(SPEC, engine_version="v", engine_identity=identity))

    def test_a_missing_identity_hashes_as_empty_rather_than_raising(self):
        """Refusing to key would mean refusing to persist. The dashboard
        nonetheless refuses to USE this value — see the hint-disable test."""
        assert sweep_key(SPEC, engine_version="v", engine_identity=None) == \
            sweep_key(SPEC, engine_version="v", engine_identity="")

    def test_sweep_key_no_longer_accepts_git_commit(self):
        """A caller left on the old keyword must FAIL, not silently key wrong.

        This is the one migration hazard with a silent failure mode: a
        `git_commit=` call that still worked would compute a key nobody else
        computes, and the dedup would quietly stop firing on that path.
        """
        with pytest.raises(TypeError):
            sweep_key(SPEC, engine_version="v", git_commit="abc123")

    def test_legacy_rows_can_never_dedup_hit(self):
        """A pre-migration row has `engine_identity` NULL and a commit-keyed
        `sweep_key`. `NULL = @engine_identity` is never true in BigQuery, so
        both dedup predicates exclude it — the rows stay readable by `run_id`
        and are never served as a cache answer for a key they were not keyed by.
        """
        pytest.importorskip("google.cloud.bigquery")
        import inspect

        from src.backtesting.scenarios import persist as store
        job_sql = inspect.getsource(store.ScenarioRunWriter.find_done_sweep)
        assert "engine_identity = @engine_identity" in job_sql

        from tests._dashboard_path import add_dashboard_backend_to_path
        add_dashboard_backend_to_path()
        import services.sweeps as S
        assert "engine_identity = @engine_identity" in S.done_by_key_sql("p.d")

    def test_both_tables_carry_the_column_nullable(self):
        pytest.importorskip("google.cloud.bigquery")
        from src.backtesting.scenarios import persist as store
        for schema in (store._sweeps_schema(), store._runs_schema()):
            field = next(f for f in schema if f.name == "engine_identity")
            assert field.field_type == "STRING"
            assert (field.mode or "NULLABLE").upper() == "NULLABLE", (
                "the column must be NULLABLE and additive: the reconcile adds it "
                "to a live table, and every existing row keeps a NULL"
            )

    def test_git_commit_is_still_stored_on_both_tables(self):
        """Provenance survives the re-key. Losing it would make a stored result
        untraceable to the tree that produced it."""
        pytest.importorskip("google.cloud.bigquery")
        from src.backtesting.scenarios import persist as store
        for schema in (store._sweeps_schema(), store._runs_schema()):
            assert "git_commit" in {f.name for f in schema}
