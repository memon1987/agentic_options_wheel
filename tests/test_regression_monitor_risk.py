"""Shape tests for `RegressionMonitor.check_risk_parameters` (FC-069 S1, card 16).

`/regression` is a **deployed detective control**, not a dev tool: Cloud
Scheduler invokes it hourly during market hours and any check with status
`fail` makes the endpoint return HTTP 500. Before this module it had **zero**
tests, which is how four of its checks came to mirror policies that do not
exist and one of them (cost basis) came to be blind on exactly the positions it
was written to protect.

Each surviving check gets a fires-on-violation test and a passes-when-clean
test, driven off synthetic position/account fixtures. Every test here was
mutation-checked at authoring time: the check's comparison was weakened, the
test was confirmed to fail, and the code was restored.

Deliberately NOT tested here: checks 1/3/5/6, deleted with the knobs they
mirrored (`max_total_positions`, `min_cash_reserve`,
`max_portfolio_allocation`, `max_exposure_per_ticker`). `test_deleted_checks_
are_absent` pins that they stay gone — a re-added mirror of a nonexistent
policy is the failure mode card 16 exists to prevent.
"""

import pytest

from tools.testing.regression_monitor import RegressionMonitor


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _stock(symbol, qty, avg_entry_price, market_value, cost_basis=None):
    """A us_equity position as Alpaca's /positions renders it."""
    return {
        "symbol": symbol,
        "asset_class": "us_equity",
        "qty": str(qty),
        "avg_entry_price": str(avg_entry_price),
        "market_value": str(market_value),
        # Alpaca returns "0" here for ASSIGNED lots — the FC-029 finding that
        # made the old cost_basis/qty derivation blind.
        "cost_basis": str(cost_basis if cost_basis is not None else qty * avg_entry_price),
        "side": "long",
    }


def _option(symbol, qty, market_value=-500.0):
    """A us_option position. Negative qty == short."""
    return {
        "symbol": symbol,
        "asset_class": "us_option",
        "qty": str(qty),
        "market_value": str(market_value),
        "side": "short" if qty < 0 else "long",
    }


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _raw_results(monkeypatch, positions, portfolio_value=100_000.0):
    """Drive check_risk_parameters and return the raw CheckResult list.

    Use this when the *number* of findings matters (deduplication).
    """
    monitor = RegressionMonitor(service_url="http://test", api_key="k")

    def fake_get(path, timeout=30):
        if path == "/account":
            return _Resp({
                "portfolio_value": str(portfolio_value),
                "cash": "50000",
                "buying_power": "50000",
            })
        if path == "/positions":
            return _Resp(positions)
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(monitor, "_get", fake_get)

    # Check 9 hits BigQuery; force its ImportError branch so these tests never
    # touch the network or ambient credentials.
    monkeypatch.setitem(__import__("sys").modules, "google.cloud.bigquery", None)

    return monitor.check_risk_parameters()


def run_checks(monkeypatch, positions, portfolio_value=100_000.0):
    """Drive check_risk_parameters against synthetic data, keyed by check name."""
    out = {}
    for r in _raw_results(monkeypatch, positions, portfolio_value):
        # A check that fires more than once keeps its first (failing) result.
        out.setdefault(r.name, r)
    return out


def statuses(results, name):
    return results[name].status


# ---------------------------------------------------------------------------
# Check 2 — one option position per underlying
# ---------------------------------------------------------------------------

class TestDuplicateUnderlying:

    def test_passes_when_each_underlying_has_one_option(self, monkeypatch):
        res = run_checks(monkeypatch, [
            _option("AAPL260821C00250000", -1),
            _option("MSFT260821P00400000", -1),
        ])
        assert statuses(res, "risk_duplicate_underlying") == "pass"

    def test_fires_on_two_options_for_same_underlying(self, monkeypatch):
        res = run_checks(monkeypatch, [
            _option("AAPL260821C00250000", -1),
            _option("AAPL260828C00260000", -1),
        ])
        r = res["risk_duplicate_underlying"]
        assert r.status == "fail"
        assert r.details["duplicates"] == {"AAPL": 2}

    def test_canonical_parser_does_not_confuse_f_and_pfe(self, monkeypatch):
        """The hand-rolled leading-alpha parser this replaced was correct for
        the underlying, but the OCC-substring family it belongs to is the
        reason this is now parsed, not sliced. F and PFE must stay distinct."""
        res = run_checks(monkeypatch, [
            _option("F260821P00012000", -1),
            _option("PFE260821P00025000", -1),
        ])
        assert statuses(res, "risk_duplicate_underlying") == "pass"


# ---------------------------------------------------------------------------
# Check 4 — max position size, re-sourced from Config
# ---------------------------------------------------------------------------

class TestMaxPositionSize:

    def test_passes_when_all_positions_within_limit(self, monkeypatch):
        # 30% of a $100k portfolio, under the live 0.35.
        res = run_checks(monkeypatch, [_stock("AAPL", 100, 300.0, 30_000.0)])
        r = res["risk_max_position_size"]
        assert r.status == "pass"
        assert r.details["limit"] == pytest.approx(0.35)

    def test_fires_when_a_position_exceeds_the_limit(self, monkeypatch):
        res = run_checks(monkeypatch, [_stock("AAPL", 100, 400.0, 40_000.0)])
        r = res["risk_max_position_size"]
        assert r.status == "fail"
        assert r.details["symbol"] == "AAPL"
        assert r.details["pct"] == pytest.approx(0.40)

    def test_limit_tracks_config_not_a_hardcoded_constant(self, monkeypatch):
        """The point of the re-source: change the policy, the alarm follows."""
        import tools.testing.regression_monitor as rm
        from src.utils.config import Config

        real_init = Config.__init__

        class TighterConfig(Config):
            def __init__(self, *a, **kw):
                real_init(self, *a, **kw)
                self._config["risk"]["max_position_size"] = 0.10

        monkeypatch.setattr("src.utils.config.Config", TighterConfig)

        # 30% now violates a 10% policy; under a hardcoded 0.35 it would pass.
        res = run_checks(monkeypatch, [_stock("AAPL", 100, 300.0, 30_000.0)])
        r = res["risk_max_position_size"]
        assert r.status == "fail"
        assert r.details["limit"] == pytest.approx(0.10)

    def test_config_is_resolved_via_STRATEGY_CONFIG(self, monkeypatch):
        """The monitor must read the SAME profile as the process it watches.

        `deploy/cloud_run_server.py`'s `strategy_config()` resolves
        STRATEGY_CONFIG; a bare `Config()` here would validate the wheel's
        policy against a covered-call service's positions.
        """
        monkeypatch.setenv("STRATEGY_CONFIG", "config/covered_call.yaml")
        res = run_checks(monkeypatch, [_stock("AAPL", 100, 300.0, 30_000.0)])
        r = res["risk_max_position_size"]
        # covered_call.yaml also carries 0.35, so assert the resolution
        # happened rather than the number: a bare Config() would have read
        # settings.yaml and never touched this file.
        assert r.status == "pass"
        assert r.details["limit"] == pytest.approx(0.35)

    def test_strategy_config_profile_drives_the_limit(self, monkeypatch, tmp_path):
        """Point STRATEGY_CONFIG at a profile with a distinct limit."""
        import shutil
        import yaml

        src = "config/covered_call.yaml"
        data = yaml.safe_load(open(src))
        data["risk"]["max_position_size"] = 0.10
        profile = tmp_path / "tight_profile.yaml"
        profile.write_text(yaml.dump(data))

        monkeypatch.setenv("STRATEGY_CONFIG", str(profile))
        # 30% violates this profile's 10%; under settings.yaml's 0.35 it passes.
        res = run_checks(monkeypatch, [_stock("AAPL", 100, 300.0, 30_000.0)])
        r = res["risk_max_position_size"]
        assert r.status == "fail"
        assert r.details["limit"] == pytest.approx(0.10)

    def test_fails_loudly_when_config_cannot_load(self, monkeypatch):
        """Never silently fall back to a stale number and report pass."""
        def boom(*a, **kw):
            raise RuntimeError("settings.yaml unreadable")

        monkeypatch.setattr("src.utils.config.Config", boom)
        res = run_checks(monkeypatch, [_stock("AAPL", 100, 300.0, 30_000.0)])
        r = res["risk_max_position_size"]
        assert r.status == "fail"
        assert "Config failed to load" in r.message


# ---------------------------------------------------------------------------
# Check 7 — naked call detection
# ---------------------------------------------------------------------------

class TestNakedCall:

    def test_passes_when_short_call_is_fully_covered(self, monkeypatch):
        res = run_checks(monkeypatch, [
            _stock("AAPL", 100, 200.0, 25_000.0),
            _option("AAPL260821C00250000", -1),
        ])
        assert statuses(res, "risk_naked_call") == "pass"

    def test_fires_when_shares_are_insufficient(self, monkeypatch):
        res = run_checks(monkeypatch, [
            _stock("AAPL", 100, 200.0, 25_000.0),
            _option("AAPL260821C00250000", -2),   # needs 200 shares, owns 100
        ])
        r = res["risk_naked_call"]
        assert r.status == "fail"
        assert r.details["required_shares"] == 200
        assert r.details["owned_shares"] == 100

    def test_fires_when_no_shares_are_held_at_all(self, monkeypatch):
        res = run_checks(monkeypatch, [_option("AAPL260821C00250000", -1)])
        assert statuses(res, "risk_naked_call") == "fail"

    def test_short_put_is_not_treated_as_a_call(self, monkeypatch):
        """The old `"C" in symbol` test. A cash-secured put needs no shares."""
        res = run_checks(monkeypatch, [_option("AAPL260821P00200000", -1)])
        assert statuses(res, "risk_naked_call") == "pass"

    def test_long_call_commits_no_shares(self, monkeypatch):
        res = run_checks(monkeypatch, [_option("AAPL260821C00250000", 1)])
        assert statuses(res, "risk_naked_call") == "pass"

    def test_malformed_symbol_is_not_classified_as_a_call(self, monkeypatch):
        """`strict_option_type` returns None for a non-OCC symbol, so the
        check does not inherit `parse_option_symbol`'s last-resort
        `'C' if 'C' in symbol` heuristic.

        Polarity note (S1 review): non-classification is correct, but it must
        not be SILENT — the position also has to surface as a warn finding.
        """
        res = run_checks(monkeypatch, [_option("NOT_AN_OCC", -1)])
        assert statuses(res, "risk_naked_call") == "pass"
        assert statuses(res, "risk_unclassifiable_option") == "warn"


# ---------------------------------------------------------------------------
# Check 8 — cost-basis floor, re-specced onto avg_entry_price
# ---------------------------------------------------------------------------

class TestCostBasisProtection:

    def test_passes_when_strike_is_above_basis(self, monkeypatch):
        res = run_checks(monkeypatch, [
            _stock("AAPL", 100, 200.0, 25_000.0),
            _option("AAPL260821C00250000", -1),   # $250 strike vs $200 basis
        ])
        assert statuses(res, "risk_cost_basis_protection") == "pass"

    def test_fires_when_strike_is_below_basis(self, monkeypatch):
        res = run_checks(monkeypatch, [
            _stock("AAPL", 100, 300.0, 25_000.0),
            _option("AAPL260821C00250000", -1),   # $250 strike vs $300 basis
        ])
        r = res["risk_cost_basis_protection"]
        assert r.status == "fail"
        assert r.details["strike"] == pytest.approx(250.0)
        assert r.details["cost_basis"] == pytest.approx(300.0)

    def test_assigned_lot_with_zero_cost_basis_is_still_checked(self, monkeypatch):
        """THE regression this re-spec exists for.

        Alpaca returns ``cost_basis: 0`` for assigned positions (FC-029). The
        old derivation computed 0/qty = 0, hit the ``> 0`` guard and skipped
        **silently** — so the detective floor check was blind on exactly the
        assigned lots it was written to protect. With ``avg_entry_price`` as
        the source the below-basis call is caught.
        """
        res = run_checks(monkeypatch, [
            _stock("AAPL", 100, 300.0, 25_000.0, cost_basis=0),
            _option("AAPL260821C00250000", -1),
        ])
        r = res["risk_cost_basis_protection"]
        assert r.status == "fail"
        assert r.details["cost_basis"] == pytest.approx(300.0)

    def test_unusable_avg_entry_price_fails_instead_of_skipping(self, monkeypatch):
        """An unresolvable basis is a finding, not a silent pass."""
        res = run_checks(monkeypatch, [
            _stock("AAPL", 100, 0, 25_000.0, cost_basis=0),
            _option("AAPL260821C00250000", -1),
        ])
        r = res["risk_cost_basis_protection"]
        assert r.status == "fail"
        assert "no usable avg_entry_price" in r.message

    def test_short_put_below_basis_is_not_flagged(self, monkeypatch):
        """Only short CALLS can be written below basis."""
        res = run_checks(monkeypatch, [
            _stock("AAPL", 100, 300.0, 25_000.0),
            _option("AAPL260821P00250000", -1),
        ])
        assert statuses(res, "risk_cost_basis_protection") == "pass"

    def test_naked_call_is_check_sevens_finding_not_this_ones(self, monkeypatch):
        res = run_checks(monkeypatch, [_option("AAPL260821C00250000", -1)])
        assert statuses(res, "risk_cost_basis_protection") == "pass"
        assert statuses(res, "risk_naked_call") == "fail"


# ---------------------------------------------------------------------------
# Unclassifiable option positions must surface, not vanish (S1 review fix 1)
# ---------------------------------------------------------------------------

class TestUnclassifiableOption:

    # An adjusted contract: the root carries a digit suffix after a split or
    # special dividend, so it fails OCC_STRICT_RE. Its deliverable is NOT 100
    # shares, which is exactly why checks 7/8 must not do share arithmetic on
    # it — and exactly why a human needs to see it.
    ADJUSTED = "AAPL1260821C00250000"

    def test_passes_when_every_option_parses(self, monkeypatch):
        res = run_checks(monkeypatch, [
            _stock("AAPL", 100, 200.0, 25_000.0),
            _option("AAPL260821C00250000", -1),
        ])
        assert statuses(res, "risk_unclassifiable_option") == "pass"

    def test_adjusted_contract_produces_a_warn_finding(self, monkeypatch):
        res = run_checks(monkeypatch, [_option(self.ADJUSTED, -1)])
        r = res["risk_unclassifiable_option"]
        assert r.status == "warn"
        assert self.ADJUSTED in r.message
        # Details must carry enough for triage without re-fetching.
        assert r.details["option_symbol"] == self.ADJUSTED
        assert r.details["qty"] == "-1"
        assert r.details["side"] == "short"

    def test_adjusted_contract_is_excluded_from_naked_call_and_cost_basis(self, monkeypatch):
        """It surfaces as a warn, and does NOT get share/strike arithmetic.

        Note the position is uncovered and its nominal strike sits below the
        stock's basis — under substring matching it would have produced two
        confident, wrong findings.
        """
        res = run_checks(monkeypatch, [
            _stock("AAPL", 100, 300.0, 25_000.0),
            _option(self.ADJUSTED, -1),
        ])
        assert statuses(res, "risk_unclassifiable_option") == "warn"
        assert statuses(res, "risk_naked_call") == "pass"
        assert statuses(res, "risk_cost_basis_protection") == "pass"

    def test_one_finding_per_position_not_one_per_loop(self, monkeypatch):
        """Checks 7 and 8 both skip these; the finding is emitted once."""
        monitor_results = _raw_results(monkeypatch, [_option(self.ADJUSTED, -1)])
        hits = [r for r in monitor_results if r.name == "risk_unclassifiable_option"]
        assert len(hits) == 1

    def test_each_unparseable_position_gets_its_own_finding(self, monkeypatch):
        monitor_results = _raw_results(monkeypatch, [
            _option(self.ADJUSTED, -1),
            _option("NOT_AN_OCC", -1),
        ])
        hits = [r for r in monitor_results if r.name == "risk_unclassifiable_option"]
        assert len(hits) == 2
        assert {h.details["option_symbol"] for h in hits} == {self.ADJUSTED, "NOT_AN_OCC"}


# ---------------------------------------------------------------------------
# Endpoint response shapes (S1 review fix 4)
# ---------------------------------------------------------------------------

def test_dict_shaped_positions_response_is_unwrapped(monkeypatch):
    """Production `/positions` returns {"positions": [...]}, not a bare list.

    Pins the isinstance(dict) unwrap branch: if it regressed, `positions`
    would be a list of dict KEYS and every check would silently see zero
    positions and report `pass`.
    """
    monitor = RegressionMonitor(service_url="http://test", api_key="k")

    def fake_get(path, timeout=30):
        if path == "/account":
            return _Resp({"portfolio_value": "100000", "cash": "50000",
                          "buying_power": "50000"})
        if path == "/positions":
            return _Resp({"positions": [
                _stock("AAPL", 100, 300.0, 25_000.0),
                _option("AAPL260821C00250000", -1),   # strike below basis
            ]})
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(monitor, "_get", fake_get)
    monkeypatch.setitem(__import__("sys").modules, "google.cloud.bigquery", None)

    res = {}
    for r in monitor.check_risk_parameters():
        res.setdefault(r.name, r)

    # The positions were really seen: this fires only if check 8 got the data.
    assert res["risk_cost_basis_protection"].status == "fail"
    assert res["risk_account_data"].status == "pass"


# ---------------------------------------------------------------------------
# The deleted mirrors stay deleted
# ---------------------------------------------------------------------------

def test_deleted_checks_are_absent(monkeypatch):
    """Checks 1/3/5/6 mirrored knobs FC-069 S1 deleted.

    Re-adding any of them would alarm hourly against a policy that does not
    exist — and check 6 in particular would return HTTP 500 hourly on a healthy
    account once the portfolio crosses roughly $114k.
    """
    res = run_checks(monkeypatch, [
        _stock("AAPL", 100, 300.0, 45_000.0),
        _option("AAPL260821C00350000", -1),
    ], portfolio_value=500_000.0)

    for gone in (
        "risk_max_total_positions",
        "risk_min_cash_reserve",
        "risk_max_portfolio_allocation",
        "risk_max_exposure_per_ticker",
        "risk_max_positions_per_stock",
    ):
        assert gone not in res, f"{gone} mirrors a policy that no longer exists"


def test_healthy_account_produces_no_failures(monkeypatch):
    """The post-sweep steady state: `/regression` returns 200, not 500."""
    res = run_checks(monkeypatch, [
        _stock("AAPL", 100, 200.0, 25_000.0),
        _option("AAPL260821C00250000", -1),
        _option("MSFT260821P00400000", -1),
    ])
    for name in ("risk_duplicate_underlying", "risk_max_position_size",
                 "risk_naked_call", "risk_cost_basis_protection"):
        assert res[name].status == "pass", f"{name}: {res[name].message}"


# ---------------------------------------------------------------------------
# FC-079 — `reconcile_orphaned` (check group 4, not the risk group above)
#
# The check asked `"C" in symbol` and rooted the contract with a bare
# `^([A-Z]+)` match. Both are substring reasoning over an OCC symbol: a put on
# a root containing a C is read as a call and warned as an orphan (a short put
# holds no shares by design — that is the whole strategy), and an adjusted root
# is mis-rooted rather than surfaced.
#
# NOTE ON THE PLAN. `docs/plans/fc-079.md` §Context says this "treats PFE…P…
# puts as calls". Checked against the tree: **PFE contains no C**, so PFE puts
# were never mis-read here, and no root in either configured universe contains
# a C today. On current config this is a LATENT defect that arrives with a
# config change — which is precisely the recurrence pattern of the
# FC-041/043/045/048/052/054 family. `CVX` below is the affected shape; PFE is
# kept as the unaffected control.
#
# Plan: docs/plans/fc-079.md
# ---------------------------------------------------------------------------

class TestReconcileOrphaned:

    ADJUSTED = "AAPL1260821C00250000"

    def _run(self, monkeypatch, positions):
        """Drive check_position_reconciliation, keyed by check name.

        Both sides of the comparison get the same list, so `reconcile_positions`
        always passes and only the orphan scan is under test.
        """
        monitor = RegressionMonitor(service_url="http://test", api_key="k")

        def fake_get(path, timeout=30):
            assert path == "/positions", path
            return _Resp({"positions": positions})

        monkeypatch.setattr(monitor, "_get", fake_get)

        class _FakeClient:
            def __init__(self, config):
                pass

            def get_positions(self_inner):
                return positions

        monkeypatch.setattr("src.utils.config.Config", lambda *a, **k: object())
        monkeypatch.setattr("src.api.alpaca_client.AlpacaClient", _FakeClient)

        return {r.name: r for r in monitor.check_position_reconciliation()}

    # -- puts are never orphans ------------------------------------------- #

    def test_a_pfe_put_with_no_pfe_stock_passes(self, monkeypatch):
        """A short put holds no shares by design. The control case."""
        res = self._run(monkeypatch, [_option("PFE260821P00025000", -1)])
        assert res["reconcile_orphaned"].status == "pass"

    def test_a_c_bearing_root_put_with_no_stock_passes(self, monkeypatch):
        """Pre-fix this warned: `"C" in "CVX260821P00150000"` is true."""
        res = self._run(monkeypatch, [_option("CVX260821P00150000", -1)])
        assert res["reconcile_orphaned"].status == "pass", (
            res["reconcile_orphaned"].message)

    # -- calls without shares still warn ----------------------------------- #

    def test_a_pfe_call_with_no_stock_warns(self, monkeypatch):
        res = self._run(monkeypatch, [_option("PFE260821C00030000", -1)])
        r = res["reconcile_orphaned"]
        assert r.status == "warn"
        assert "PFE260821C00030000" in r.details["orphaned_symbols"]

    def test_a_call_with_its_shares_present_passes(self, monkeypatch):
        res = self._run(monkeypatch, [
            _stock("PFE", 100, 28.0, 2_900.0),
            _option("PFE260821C00030000", -1),
        ])
        assert res["reconcile_orphaned"].status == "pass"

    def test_the_root_comes_from_the_parser_not_a_letter_prefix(self, monkeypatch):
        """`^([A-Z]+)` and the parser agree on plain roots; pin that the check
        keys off the underlying it actually holds shares in."""
        res = self._run(monkeypatch, [
            _stock("GOOGL", 100, 180.0, 18_000.0),
            _option("GOOGL260821C00200000", -1),
            _option("AAPL260821C00250000", -1),     # no AAPL shares
        ])
        r = res["reconcile_orphaned"]
        assert r.status == "warn"
        assert r.details["orphaned_symbols"] == ["AAPL260821C00250000"]

    # -- adjusted contracts are surfaced, not guessed ---------------------- #

    def test_an_adjusted_symbol_is_listed_under_unclassifiable(self, monkeypatch):
        res = self._run(monkeypatch, [
            _option(self.ADJUSTED, -1),
            _option("AAPL260821C00250000", -1),     # a real orphan, to warn
        ])
        r = res["reconcile_orphaned"]
        assert r.status == "warn"
        assert r.details["unclassifiable"] == [self.ADJUSTED]
        # It is NOT counted as an orphan — the check cannot know its deliverable.
        assert r.details["orphaned_symbols"] == ["AAPL260821C00250000"]

    def test_an_adjusted_symbol_alone_does_not_flip_the_status(self, monkeypatch):
        """`risk_unclassifiable_option` already alerts on these. A second alarm
        for the same fact is how an alarm layer gets muted — but the symbol is
        still carried in details so the operator sees it here too."""
        res = self._run(monkeypatch, [_option(self.ADJUSTED, -1)])
        r = res["reconcile_orphaned"]
        assert r.status == "pass"
        assert r.details["unclassifiable"] == [self.ADJUSTED]
