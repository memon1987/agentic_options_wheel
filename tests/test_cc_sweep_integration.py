"""FC-096 Phase C — a covered-call sweep, END TO END. Review round 1, B1.

**Why this file exists.** Round 1 found that every covered-call sweep entry
point raised ``KeyError: 'put_target_dte'`` before replaying a single day:
``run_sweep`` → ``bq_writer.config_hash`` → a ``Config`` property that indexes
``_config["strategy"]`` directly, on a profile with no put leg. Thirty-five
tests of the covered-call REPLAY passed while the thing an operator would
actually run was broken end to end, because every one of them drove
``Simulator`` directly and none went through ``run_sweep``.

So these tests are deliberately coarse and deliberately whole-path: they call
the real ``run_sweep``, the real ``render_markdown``, the real artifact and
sidecar builders, and the real service/CLI resolution — the seams the unit
tests jump over. A defect that lives in the wiring rather than in the strategy
is invisible to everything else in this suite.
"""

from __future__ import annotations

import json

import pytest

from src.backtesting.reporting.bq_writer import config_hash
from src.backtesting.scenarios import persist as store
from src.backtesting.scenarios.runner import (
    Scenario, arm_max_dte, effective_max_dte, roll_horizon_reach, run_sweep,
)
from src.utils.config import Config

from tests.test_backtest_simulator import ScriptedProvider
from tests.test_cc_replay import _rising_window

CC_PROFILE = "config/covered_call.yaml"


class _NoChainStore:
    """A ChainStore that never touches GCS or the local parquet cache.

    ``run_sweep`` builds one from the environment when it is handed None, which
    would make this file's outcome depend on whether a developer has a cache
    directory — or reach the GCS chain lake. ``get``/``put`` are the two methods
    ``ChainBuilder`` actually calls (chain_builder.py:273,289); a permanent miss
    means every chain is built from the scripted provider, which is what makes
    this sweep deterministic and offline.
    """

    lake = None

    def get(self, *args, **kwargs):
        return None

    def put(self, *args, **kwargs):
        return None

    def summary(self):
        return {}


@pytest.fixture(scope="module")
def cc_sweep():
    """One real covered-call sweep: 1 symbol, 1 window, base + one arm."""
    days, closes, expirations = _rising_window()
    provider = ScriptedProvider("XYZ", closes, expirations)
    artifacts, sidecars = [], []
    result = run_sweep(
        Config(CC_PROFILE),
        [Scenario(name="tighter_delta",
                  overrides={"strategy.call_delta_range": [0.10, 0.20]})],
        ["XYZ"], days[0], days[-1],
        chain_store=_NoChainStore(),
        bar_provider=provider,
        artifact_sink=artifacts.append,
        bars_sink=sidecars.append,
        run_id="ccsweep000000001",
        engine_identity="testidentity",
        git_commit="deadbeef",
    )
    return result, artifacts, sidecars, days, closes


# --------------------------------------------------------------------------- #
# B1 — the crash itself
# --------------------------------------------------------------------------- #
class TestTheBlockerIsGone:
    def test_config_hash_survives_a_profile_with_no_put_leg(self):
        """The exact call that raised, at the exact profile that raised.

        `getattr(config, k, None)` swallows AttributeError and NOT the KeyError
        a property raises from inside itself.
        """
        cc = Config(CC_PROFILE)
        with pytest.raises(KeyError):
            _ = cc.put_target_dte  # the property still raises; that is fine
        assert config_hash(cc), "config_hash must not propagate it"

    def test_the_wheel_hash_did_not_move(self):
        """The fix must be invisible to every stored wheel row.

        `config_hash` lines a sweep row up with a `backtest_runs` row, so a
        changed value silently breaks that join for every historical run.
        """
        assert config_hash(Config()) == "595b959a249d3cc0"

    def test_an_absent_knob_hashes_as_none_rather_than_being_skipped(self):
        """Skipping the key instead would make a profile that DECLARES
        `put_target_dte: null` hash identically to one that omits it."""
        cc, wheel = Config(CC_PROFILE), Config()
        assert config_hash(cc) != config_hash(wheel)

    def test_the_whole_sweep_completes(self, cc_sweep):
        result, _artifacts, _sidecars, _days, _closes = cc_sweep
        assert result.rows, "the sweep produced no rows at all"
        assert not result.errors, (
            "cells errored: " + "; ".join(r.error or "" for r in result.errors))


# --------------------------------------------------------------------------- #
# The rows, the markdown, the artifact and the sidecar
# --------------------------------------------------------------------------- #
class TestTheSweepProducesUsableEvidence:
    def test_every_cell_is_measured_and_stamped_covered_call(self, cc_sweep):
        result, _artifacts, _sidecars, _days, _closes = cc_sweep
        assert result.strategy == "covered_call"
        for row in result.rows:
            assert row.ok, row.error
            assert row.strategy == "covered_call"
            assert row.capital_base and row.capital_base > 0
            assert row.premium_yield_on_lot is not None
            assert row.coverage_by_reason

    def test_the_two_arms_hash_differently_and_both_replayed(self, cc_sweep):
        result, _artifacts, _sidecars, _days, _closes = cc_sweep
        assert set(result.scenarios) == {"base", "tighter_delta"}
        hashes = set(result.scenario_config_hashes.values())
        assert len(hashes) == 2, "the arm did not change the effective config"

    def test_the_markdown_renders_with_the_covered_call_footer(self, cc_sweep):
        from src.backtesting.scenarios.report import (
            CC_ROLL_SPLIT_NOTE, MODEL_SPREAD_BIAS, MONITOR_LEG_NOTE,
            ROLL_REACH_BIAS, SYNTHETIC_LOT_BIAS, render_markdown,
        )

        result, _artifacts, _sidecars, _days, _closes = cc_sweep
        markdown = render_markdown(result)
        assert markdown
        for title, _detail in (SYNTHETIC_LOT_BIAS, MONITOR_LEG_NOTE,
                               ROLL_REACH_BIAS, CC_ROLL_SPLIT_NOTE,
                               MODEL_SPREAD_BIAS):
            assert title in markdown, f"missing footer line: {title}"

    def test_the_markdown_drops_the_two_wheel_lines_that_are_false_here(
            self, cc_sweep):
        """M1, at the render rather than at the constant."""
        from src.backtesting.scenarios.report import (
            WHEEL_EX_DIV_TITLE, WHEEL_PROFIT_TAKING_TITLE, render_markdown,
        )

        result, _artifacts, _sidecars, _days, _closes = cc_sweep
        markdown = render_markdown(result)
        assert WHEEL_PROFIT_TAKING_TITLE not in markdown
        assert WHEEL_EX_DIV_TITLE not in markdown
        assert "profit-taking IS modelled on this profile" in markdown

    def test_the_markdown_carries_the_covered_call_detail_table(self, cc_sweep):
        """M3. The grid prints ONE number per cell and on a CC run that is the
        equity return on the lot — it carries the shares' price move. The
        headline the programme is managed to (premium yield) and the coverage
        split the verdict gates on had nowhere to appear."""
        from src.backtesting.scenarios.report import render_markdown

        result, _artifacts, _sidecars, _days, _closes = cc_sweep
        markdown = render_markdown(result)
        assert "### Covered-call detail" in markdown
        assert "premium yield" in markdown
        assert "below basis" in markdown
        # FC-116 D6 renamed the column: the split alone was never enough to
        # answer "was that roll defence or churn", and the credit is what
        # decides it.
        assert "rolls itm/otm · credit" in markdown
        assert "`lot return` is the verdict's number" in markdown

    def test_the_legend_does_not_claim_insuf_means_no_closed_cycle(self, cc_sweep):
        """M3. On a covered-call run that sentence is FALSE — and false in the
        most damaging direction: a lot never called away closes no cycle and is
        the best outcome the strategy has."""
        from src.backtesting.scenarios.report import render_markdown

        result, _artifacts, _sidecars, _days, _closes = cc_sweep
        markdown = render_markdown(result)
        assert "`insuf` no completed cycle" not in markdown
        assert "no lot was ever seeded" in markdown
        assert "closes no cycle and is the programme working" in markdown

    def test_the_json_render_is_serialisable(self, cc_sweep):
        from src.backtesting.scenarios.report import render_json

        result, _artifacts, _sidecars, _days, _closes = cc_sweep
        payload = render_json(result)
        assert json.dumps(payload), "the JSON render is not serialisable"

    def test_one_cell_artifact_per_non_errored_cell(self, cc_sweep):
        result, artifacts, _sidecars, _days, _closes = cc_sweep
        assert len(artifacts) == len([r for r in result.rows if r.ok])

    def test_the_cell_artifact_carries_the_phase_c_stamps(self, cc_sweep):
        result, artifacts, _sidecars, days, closes = cc_sweep
        art = artifacts[0]
        prov = art["provenance"]
        assert prov["strategy"] == "covered_call"
        assert prov["capital_base"] is not None, (
            "a stored covered-call cell with a null base is the REFUSAL state; "
            "the runner is supposed to stamp the scored base here")
        assert prov["capital_base"] == pytest.approx(closes[days[0]] * 100)
        assert prov["starting_cash"] == 5_000.0, "the float, stamped separately"
        assert art["coverage_by_reason"]
        assert "roll_skips" in art
        assert json.dumps(art), "the artifact is not serialisable"

    def test_the_artifact_stores_the_CORRECTED_return_not_lot_as_profit(
            self, cc_sweep):
        """M2 (review round 1).

        `SimulationResult.total_return` divides by `starting_cash` — the $5,000
        float — while the equity it divides CONTAINS the seeded lot, so the raw
        counter read +995% on the rally cell. The corrected number is the scored
        one, taken over `capital_base`.
        """
        result, artifacts, _sidecars, _days, _closes = cc_sweep
        row = next(r for r in result.rows if r.scenario == "base")
        art = next(a for a in artifacts
                   if a["provenance"]["scenario"] == "base")
        stored = art["counters"]["total_return"]
        assert stored == pytest.approx(row.total_return)
        assert abs(stored) < 2.0, (
            f"{stored:+.2%} is the lot-as-profit number, not a return")

    def test_candidate_days_is_null_on_a_covered_call_cell(self, cc_sweep):
        """M2. The counter watches `stage_7_complete_found` — the PUT leg's
        stage — so it is structurally 0 on a covered-call replay and would read
        as "the chain never offered a candidate"."""
        _result, artifacts, _sidecars, _days, _closes = cc_sweep
        assert artifacts[0]["counters"]["candidate_days"] is None

    def test_the_bars_sidecar_agrees_with_the_cell_on_the_capital_base(
            self, cc_sweep):
        """H1's console-visible consequence: one page, one denominator."""
        result, artifacts, sidecars, _days, _closes = cc_sweep
        assert sidecars, "no bars sidecar was emitted"
        sidecar = sidecars[0]
        cell = next(a for a in artifacts
                    if a["provenance"]["scenario"] == "base")
        assert sidecar["provenance"]["strategy"] == "covered_call"
        assert (sidecar["provenance"]["capital_base"]
                == cell["provenance"]["capital_base"])
        assert (sidecar["buy_and_hold"]["capital_base"]
                == cell["provenance"]["capital_base"]), (
            "the sidecar's buy-and-hold must be curved over the SAME base the "
            "cell divides by, or the console's two panels disagree")

    def test_the_replay_read_nothing_during_the_scenario_loop(self, cc_sweep):
        result, _artifacts, _sidecars, _days, _closes = cc_sweep
        assert result.provider_calls_during_replays == 0


# --------------------------------------------------------------------------- #
# H3 — the widened reach, on a real sweep
# --------------------------------------------------------------------------- #
class TestTheWheelMarkdownIsUnchanged:
    """The claim `_covered_call_table` makes about itself, enforced.

    It returns "" on a wheel run, but the separators around it were appended
    unconditionally — so every wheel sweep report grew two blank lines.
    Harmless to a reader and a false statement in a docstring, which is the
    kind of thing that makes the next person stop trusting the rest of it.
    Byte-checked here rather than eyeballed.
    """

    def test_a_wheel_sweep_report_has_no_stray_blank_run(self):
        from src.backtesting.scenarios.report import render_markdown
        from src.backtesting.scenarios.runner import Scenario, run_sweep
        from src.utils.config import Config

        from tests.test_backtest_simulator import dip_then_recovering_window

        days, closes, expirations = dip_then_recovering_window()
        result = run_sweep(
            Config(),
            [Scenario(name="tighter", overrides={"strategy.min_put_premium": 0.6})],
            ["XYZ"], days[0], days[-1],
            chain_store=_NoChainStore(),
            bar_provider=ScriptedProvider("XYZ", closes, expirations),
        )
        markdown = render_markdown(result)
        assert result.strategy == "wheel"
        assert "### Covered-call detail" not in markdown
        assert "\n\n\n" not in markdown, (
            "the wheel report grew a blank run where the covered-call table "
            "would have gone")


class TestTheRollHorizonReach:
    def test_a_covered_call_sweep_materialises_to_the_roll_horizon(self, cc_sweep):
        result, artifacts, _sidecars, _days, _closes = cc_sweep
        assert result.effective_max_dte == 21, (
            "a covered-call sweep must reach the roll horizon (14 + 14, capped "
            "at the lake), not the 14-DTE call target")
        for art in artifacts:
            assert art["provenance"]["masked_reach"]["max_dte"] == 21

    def test_the_wheel_reach_is_untouched(self):
        """The golden contract: widening the CC reach must not widen the wheel's."""
        wheel = Config()
        assert roll_horizon_reach(wheel) == 0
        assert arm_max_dte(wheel) == 7
        assert effective_max_dte(wheel, []) == 7

    def test_the_horizon_is_capped_at_what_the_lake_stores(self):
        from src.backtesting.scenarios.overrides import MAX_SWEEPABLE_DTE

        cc = Config(CC_PROFILE)
        wanted = cc.call_target_dte + cc.rolling_max_extension_days
        assert wanted == 28, "the profile's roll horizon moved"
        assert roll_horizon_reach(cc) == MAX_SWEEPABLE_DTE == 21, (
            "asking past the lake would not widen the candidate set, it would "
            "make the arm read as 'nothing qualified'")

    def test_the_dashboards_reach_agrees_with_the_engines(self, cc_sweep):
        """H5. The two derive it by different routes and must not disagree."""
        from tests._dashboard_path import add_dashboard_backend_to_path

        add_dashboard_backend_to_path()
        from services import sweeps as S

        result, _artifacts, _sidecars, _days, _closes = cc_sweep
        spec = {"scenarios": [], "strategy": "covered_call"}
        assert S.spec_max_dte(spec) == result.effective_max_dte == 21
        assert S.spec_max_dte({"scenarios": []}) == 7, "the wheel's floor"

    def test_the_pinned_cc_base_reach_matches_the_profile(self):
        """H5's constant is a COPY of a number the dashboard cannot compute."""
        from tests._dashboard_path import add_dashboard_backend_to_path

        add_dashboard_backend_to_path()
        from services import sweeps as S

        assert S.CC_BASE_REACH == roll_horizon_reach(Config(CC_PROFILE))

    def test_roll_skips_are_recorded_so_a_blind_roller_is_visible(self, cc_sweep):
        """H3's observability half.

        The rising window's roller declines every evaluation (`not_itm_enough`
        at 1.00). Without the reason counts that is indistinguishable from a
        roller that could not price a single candidate.
        """
        result, artifacts, _sidecars, _days, _closes = cc_sweep
        row = next(r for r in result.rows if r.scenario == "base")
        assert row.rolls_evaluated and row.rolls_evaluated > 0
        assert row.roll_skips, "no skip reasons recorded"
        assert "not_itm_enough" in row.roll_skips
        base_art = next(a for a in artifacts
                        if a["provenance"]["scenario"] == "base")
        assert base_art["roll_skips"] == row.roll_skips, (
            "the artifact and the row must report the SAME skip reasons")


# --------------------------------------------------------------------------- #
# The other two entry points reaching config_hash
# --------------------------------------------------------------------------- #
class TestTheEntryPointsReachConfigHashWithoutRaising:
    def test_the_sim_service_resolution_hashes_a_covered_call_spec(self):
        """`sim_service` computes `engine_config_hash=config_hash(...)` on the
        REPLAY config — the third site the blocker fired at."""
        import deploy.sim_service as svc

        svc.reset_for_tests()
        try:
            cfg = svc.replay_config_for({"strategy": "covered_call"})
            assert cfg.strategy_id == "covered_call"
            assert config_hash(cfg)
            snapshot = store.base_config_snapshot(cfg)
            assert store.base_config_hash(snapshot)
            assert snapshot["effective"], "the snapshot lost its effective block"
        finally:
            svc.reset_for_tests()

    def test_the_cli_resolution_hashes_a_covered_call_spec(self):
        """`main.run_sweep_cmd` computes the same two hashes off
        `resolve_replay_config`'s answer."""
        import main as cli

        class _Args:
            config = None

        cfg, strategy = cli.resolve_replay_config(
            _Args(), {"strategy": "covered_call"}, Config())
        assert strategy == "covered_call"
        assert config_hash(cfg)
        assert store.base_config_hash(store.base_config_snapshot(cfg))

    def test_the_single_symbol_path_no_longer_raises_either(self):
        """`evaluate.evaluate_symbol` read `put_target_dte` the same way."""
        from src.backtesting.scenarios.runner import config_target_dte

        cc = Config(CC_PROFILE)
        assert config_target_dte(cc, "call") == 14
        # The put leg falls back rather than raising, which is what the reach
        # derivation depends on.
        assert config_target_dte(cc, "put") > 0


# --------------------------------------------------------------------------- #
# FC-116 T12/T13 — FC-112 readiness, on the WHEEL
#
# FC-112 compares `rolling.itm_trigger_ratio` in {0.98, 1.00} across the
# wheel's standing set. It reads, per row: `rolls_executed`, the ITM/OTM split,
# and the roll CREDIT split the same way. Before this PR:
#   * the split was `None` on every wheel row (gated to covered-call rows) and
#     printed only in the covered-call table;
#   * no roll-credit aggregate existed ANYWHERE — not on the result, the
#     report, the artifact or the row.
# FC-112 would have arrived to find the number it needs gated off.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def wheel_roll_sweep():
    """A real WHEEL sweep over the one scripted window that executes a roll."""
    from tests.test_backtest_simulator import dip_then_recovering_window

    days, closes, expirations = dip_then_recovering_window()
    provider = ScriptedProvider("XYZ", closes, expirations)
    artifacts = []
    result = run_sweep(
        Config(),
        [Scenario(name="trigger_100",
                  overrides={"rolling.itm_trigger_ratio": 1.00})],
        ["XYZ"], days[0], days[-1],
        starting_cash=50_000.0,
        chain_store=_NoChainStore(),
        bar_provider=provider,
        artifact_sink=artifacts.append,
        run_id="wheelroll0000001",
        engine_identity="testidentity",
        git_commit="deadbeef",
    )
    return result, artifacts


class TestFC112CanReadWhatItNeeds:
    def test_a_wheel_row_carries_the_split_it_used_to_gate_off(self, wheel_roll_sweep):
        result, _artifacts = wheel_roll_sweep
        rolled = [r for r in result.rows if (r.rolls_executed or 0) > 0]
        assert rolled, "the wheel sweep executed no roll — the fixture is wrong"
        for row in rolled:
            assert row.itm_rolls is not None, (
                "the ITM/OTM split is `None` on this wheel row — that gating is "
                "exactly what FC-112 would have arrived to find")
            assert row.otm_roll_outs is not None
            assert row.rolls_executed == row.itm_rolls + row.otm_roll_outs

    def test_a_wheel_row_carries_the_roll_credit_and_its_split(self, wheel_roll_sweep):
        result, _artifacts = wheel_roll_sweep
        for row in [r for r in result.rows if (r.rolls_executed or 0) > 0]:
            assert row.roll_net_credit is not None
            assert row.itm_roll_credit is not None
            assert row.otm_roll_out_credit is not None
            assert row.roll_net_credit == pytest.approx(
                row.itm_roll_credit + row.otm_roll_out_credit)

    def test_a_wheel_row_carries_the_residual_counters_and_the_mode(
            self, wheel_roll_sweep):
        """`roll_legs_resting` is how a reader DISCOUNTS a row: it is the count
        of legs that rest on the model's one assumption."""
        result, _artifacts = wheel_roll_sweep
        for row in [r for r in result.rows if (r.rolls_executed or 0) > 0]:
            assert row.roll_legs_resting is not None
            assert row.roll_legs_marketable is not None
            assert (row.roll_legs_resting + row.roll_legs_marketable
                    == 2 * row.rolls_executed)
            assert row.failed_roll_btc_debit == 0.0
            assert row.roll_fill_mode == "limit", (
                "every arm of this sweep omitted the field, so every row must "
                "have RESOLVED to the honest default — a NULL here is the "
                "ambiguity the resolved-value posture exists to avoid")

    def test_roll_skips_is_on_the_wheel_row_too(self, wheel_roll_sweep):
        """A credit-only roller declining 40 evaluations and a roller that
        could not price a single one report the same `rolls_executed`."""
        result, _artifacts = wheel_roll_sweep
        assert any(r.roll_skips for r in result.rows)

    def test_the_wheel_markdown_prints_the_roll_credit_column(self, wheel_roll_sweep):
        from src.backtesting.scenarios.report import render_markdown

        result, _artifacts = wheel_roll_sweep
        markdown = render_markdown(result)
        assert "### Roll activity and credit" in markdown, (
            "the split and the credit were printed only in the COVERED-CALL "
            "table; FC-112's question is about the wheel")
        assert "rolls itm/otm · credit" in markdown
        assert "resting/mktable" in markdown

    def test_the_wheel_markdown_carries_the_new_footer(self, wheel_roll_sweep):
        from src.backtesting.scenarios.report import (
            ROLL_FILL_RULE, render_markdown,
        )

        result, _artifacts = wheel_roll_sweep
        markdown = render_markdown(result)
        assert ROLL_FILL_RULE[0] in markdown
        assert "roll fills" in markdown, "the scenario table must print the mode"

    def test_the_cell_artifact_counters_reconcile(self, wheel_roll_sweep):
        result, artifacts = wheel_roll_sweep
        rolled = [a for a in artifacts if a["counters"]["rolls_executed"]]
        assert rolled, "no artifact recorded a roll"
        for artifact in rolled:
            counters = artifact["counters"]
            assert counters["roll_net_credit"] == pytest.approx(
                round(sum(r["net_credit"] for r in artifact["roll_records"]), 2))
            assert counters["roll_net_credit"] == pytest.approx(
                counters["itm_roll_credit"] + counters["otm_roll_out_credit"])
            assert (counters["roll_legs_resting"]
                    + counters["roll_legs_marketable"]
                    == 2 * counters["rolls_executed"])
            assert artifact["provenance"]["fill"]["roll_fill_mode"] == "limit"


class TestTheScreenPathAgreesWithTheSweepPath:
    """T13. `evaluate._simulator` builds a `Simulator` for the screen /
    `backtest_runs` path and passes NO `roll_fill_mode`, taking the constructor
    default. That is the design: one source for the honest mode, so the screen
    cannot silently stay on the haircut while sweeps move.

    *Catches:* someone adding an explicit `roll_fill_mode=` to one of the two
    call sites and not the other.
    """

    def test_both_entry_points_produce_the_same_roll_leg_fills(self):
        from src.backtesting.data.chain_builder import ChainBuilder
        from src.backtesting.evaluate import _simulator as screen_simulator
        from src.backtesting.scenarios.runner import _simulator as sweep_simulator
        from tests.test_backtest_simulator import dip_then_recovering_window

        days, closes, expirations = dip_then_recovering_window()

        def legs(result):
            return [
                (e.kind, e.event_date, e.symbol, round(e.price, 6),
                 e.detail["fill_rule"], e.detail["limit_price"])
                for e in result.broker.ledger
                if e.kind in ("buy_to_close", "sell_call_open")
                and (e.detail or {}).get("limit_price") is not None
            ]

        provider = ScriptedProvider("XYZ", closes, expirations)
        screen = screen_simulator(
            "XYZ", days[0], days[-1], Config(), provider,
            ChainBuilder(provider, risk_free_rate=0.04),
            starting_cash=50_000.0, fill_haircut=0.25, max_dte=7,
        ).run()

        provider2 = ScriptedProvider("XYZ", closes, expirations)
        sweep = sweep_simulator(
            Config(), provider2, ChainBuilder(provider2, risk_free_rate=0.04),
            "XYZ", days[0], days[-1],
            starting_cash=50_000.0, max_dte=7, fill_haircut=0.25,
            dividends=None,
        ).run()

        assert legs(screen), "the screen path executed no roll leg"
        assert legs(screen) == legs(sweep)
        assert screen.roll_fill_mode == sweep.roll_fill_mode == "limit"

    def test_evaluate_states_the_mode_at_its_one_call_site(self):
        """T13, as amended by review: the screen path THREADS the mode.

        `evaluate.DEFAULT_ROLL_FILL_MODE` was declared and then never used,
        which is the worst of both worlds — a constant a reader takes for the
        screen path's setting while the screen path actually inherited the
        `Simulator` constructor default, with no test on either. Passing it
        explicitly changes no behaviour (the two are pinned equal below) and
        makes the screen path's fill rule a stated fact a test can hold.
        """
        import inspect

        from src.backtesting import evaluate
        from src.backtesting.engine.broker import ROLL_FILL_MODE_LIMIT
        from src.backtesting.engine.simulator import Simulator

        source = inspect.getsource(evaluate._simulator)
        assert "roll_fill_mode=DEFAULT_ROLL_FILL_MODE" in source, (
            "`evaluate._simulator` must state the screen path's roll fill "
            "mode; a silently inherited default is a mode no test pins")
        # ... and the value it states is the same one everything else uses, so
        # stating it cannot become a second spelling.
        assert evaluate.DEFAULT_ROLL_FILL_MODE == ROLL_FILL_MODE_LIMIT
        assert (inspect.signature(Simulator.__init__)
                .parameters["roll_fill_mode"].default
                == evaluate.DEFAULT_ROLL_FILL_MODE)

    def test_the_screen_data_quality_block_carries_the_credit(self):
        from datetime import date

        from src.backtesting.evaluate import _data_quality
        from src.backtesting.engine.simulator import SimulationResult

        result = SimulationResult(
            symbols=["XYZ"], start=date(2024, 6, 3), end=date(2024, 6, 28),
            starting_cash=50_000.0, daily=[], broker=_EmptyBroker(),
            roll_net_credit=412.5, itm_roll_credit=300.0,
            otm_roll_out_credit=112.5, roll_legs_resting=1,
            roll_legs_marketable=3, roll_fill_mode="limit",
        )
        block = _data_quality(result, [])
        assert block["roll_net_credit"] == 412.5
        assert block["roll_fill_mode"] == "limit"
        assert block["roll_legs_resting"] == 1


class _EmptyBroker:
    ledger: list = []
    cash = 50_000.0
