"""The bars cache, and the "a warm run touches nothing" contract (FC-060 D4).

Chains have been cached since FC-042; bars never were. That gap is why a "warm"
backtest still made four network calls per symbol and why a socket-blocked run
died on the first one — the chains came off disk and then
``Simulator._load_stock_bars`` went to the vendor anyway.

The two things worth being paranoid about here:

**Coverage must be proven, not inferred.** A weekday with no row is
indistinguishable between "market holiday" and "we never asked for that range",
so the file records the *request window* it answers and a read is a hit only
against that. Inferring it from the stored dates would serve a replay a window
with a hole in it, which is a silently different backtest — the exact failure
class this engine exists to remove.

**Today is never stored and never claimed.** Today's daily bar is still forming.
A run started at 11am that cached "today" would freeze a half-session as final
and every later run would read a close that never happened.
"""

from __future__ import annotations

import socket
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.backtesting.data import alpaca_provider as alpaca_module
from src.backtesting.data.bar_store import (
    BarStore,
    CachedBarProvider,
    _is_settled,
    last_settled_day,
)
from src.backtesting.data.chain_builder import ChainBuilder
from src.backtesting.data.chain_store import ChainStore
from src.backtesting.data.provider import StockBar
from src.backtesting.engine.simulator import Simulator
from src.utils.config import Config

from .test_backtest_simulator import (  # noqa: F401 - fixture used by name
    ScriptedProvider,
    falling_then_flat,
)


class _SpyProvider:
    """Serves canned bars and counts every fetch."""

    def __init__(self, prices, *, symbol="XYZ"):
        self._prices = dict(prices)
        self.symbol = symbol
        self.calls = []

    def get_stock_bars(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        return [
            StockBar(symbol=symbol, bar_date=d, open=p, high=p, low=p,
                     close=p, volume=1_000_000)
            for d, p in sorted(self._prices.items())
            if start <= d <= end
        ]

    def get_contract_universe(self, *a, **kw):  # pragma: no cover - not exercised
        raise AssertionError("bar cache tests must not reach contract discovery")

    def get_option_bars(self, *a, **kw):  # pragma: no cover - not exercised
        raise AssertionError("bar cache tests must not reach option bars")


def _weekday_prices(start: date, n: int, base: float = 100.0):
    out, d, i = {}, start, 0
    while len(out) < n:
        if d.weekday() < 5:
            out[d] = base + i
            i += 1
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# 4. miss -> fetch -> store; hit -> zero calls; today excluded; durability
# --------------------------------------------------------------------------- #
class TestBarStoreRoundTrip:
    def test_miss_fetches_then_hit_makes_no_call(self, tmp_path):
        prices = _weekday_prices(date(2025, 1, 6), 20)
        days = sorted(prices)
        provider = _SpyProvider(prices)
        cached = CachedBarProvider(provider, BarStore(str(tmp_path)))

        cold = cached.get_stock_bars("XYZ", days[0], days[-1])
        assert len(provider.calls) == 1, "cold read must actually fetch"
        assert [b.bar_date for b in cold] == days

        warm = cached.get_stock_bars("XYZ", days[0], days[-1])
        assert len(provider.calls) == 1, "warm read must make no provider call"
        assert warm == cold, "the cache must be invisible to results, not just fast"
        assert cached.hits == 1 and cached.fetches == 1

    def test_a_narrower_range_is_served_from_a_wider_file(self, tmp_path):
        prices = _weekday_prices(date(2025, 1, 6), 20)
        days = sorted(prices)
        provider = _SpyProvider(prices)
        cached = CachedBarProvider(provider, BarStore(str(tmp_path)))

        cached.get_stock_bars("XYZ", days[0], days[-1])
        inner = cached.get_stock_bars("XYZ", days[5], days[10])
        assert len(provider.calls) == 1
        assert [b.bar_date for b in inner] == days[5:11], "must narrow on read"

    def test_a_partial_range_costs_exactly_one_fetch_for_the_union(self, tmp_path):
        """Coverage stays ONE contiguous interval, so a disjoint ask widens it.

        The alternative — appending a second range — needs interval algebra in
        the read path, and bars are one API call, so the union is strictly
        cheaper than the bookkeeping.
        """
        prices = _weekday_prices(date(2025, 1, 6), 30)
        days = sorted(prices)
        provider = _SpyProvider(prices)
        cached = CachedBarProvider(provider, BarStore(str(tmp_path)))

        cached.get_stock_bars("XYZ", days[10], days[15])
        assert len(provider.calls) == 1

        extended = cached.get_stock_bars("XYZ", days[0], days[20])
        assert len(provider.calls) == 2, "one fetch for the whole range"
        assert provider.calls[1] == ("XYZ", days[0], days[20])
        assert [b.bar_date for b in extended] == days[0:21]

        # And the union is now covered, so a read inside it is free.
        cached.get_stock_bars("XYZ", days[2], days[18])
        assert len(provider.calls) == 2

    def test_a_disjoint_later_range_fetches_the_union_not_just_the_gap(self, tmp_path):
        prices = _weekday_prices(date(2025, 1, 6), 40)
        days = sorted(prices)
        provider = _SpyProvider(prices)
        cached = CachedBarProvider(provider, BarStore(str(tmp_path)))

        cached.get_stock_bars("XYZ", days[0], days[5])
        cached.get_stock_bars("XYZ", days[20], days[25])
        assert provider.calls[1] == ("XYZ", days[0], days[25])

        window = cached.store.covered_window("XYZ")
        assert window == (days[0], days[25])
        # The middle was never asked for directly, but the union fetch covered
        # it, so it is a genuine hit rather than a hole served as data.
        cached.get_stock_bars("XYZ", days[10], days[12])
        assert len(provider.calls) == 2

    def test_todays_bar_is_never_stored_and_never_claimed(self, tmp_path):
        """A run at 11am must not freeze a half-formed session as final."""
        today = date.today()
        yesterday = today - timedelta(days=1)
        provider = _SpyProvider({yesterday: 100.0, today: 101.0})
        store = BarStore(str(tmp_path))
        cached = CachedBarProvider(provider, store)

        out = cached.get_stock_bars("XYZ", yesterday - timedelta(days=5), today)
        assert [b.bar_date for b in out] == [yesterday, today], (
            "the wrapper returns what the provider returned; filtering unsettled "
            "bars out of the RESULT would change behaviour vs. no cache"
        )
        # ...but the file holds only the settled one, and claims only up to it.
        assert [b.bar_date for b in (store.get(
            "XYZ", yesterday - timedelta(days=5), yesterday) or [])] == [yesterday]
        assert store.covered_window("XYZ")[1] == last_settled_day()

        # A request running INTO the future is answered, because the clamp is
        # relative to now: a provider asked today for data through tomorrow
        # returns exactly what a file ending yesterday holds. The staleness this
        # guards against is the NEXT session — `covered_to` is yesterday, so once
        # the clock rolls over, `last_settled_day()` moves past it and the same
        # request becomes a miss. Pinned on the stored claim rather than on a
        # faked clock, which would test the fake.
        assert store.get("XYZ", yesterday, today + timedelta(days=1)) is not None
        stored_to = store.covered_window("XYZ")[1]
        assert stored_to < today, (
            "the file claims to cover today; tomorrow it would serve a "
            "half-formed session as final"
        )

    def test_a_window_entirely_in_the_future_is_empty_without_a_fetch(self, tmp_path):
        provider = _SpyProvider({})
        store = BarStore(str(tmp_path))
        future = date.today() + timedelta(days=3)
        assert store.get("XYZ", date.today(), future) == []
        assert provider.calls == []

    def test_the_settled_rule_matches_the_providers(self):
        """Two copies of one rule; they must agree or the cache drifts from the vendor."""
        for offset in (-2, -1, 0, 1):
            day = date.today() + timedelta(days=offset)
            assert _is_settled(day) == alpaca_module._is_settled(day)


class TestBarStoreDurability:
    def test_put_leaves_no_temp_file_behind(self, tmp_path):
        store = BarStore(str(tmp_path))
        store.put("XYZ", [StockBar("XYZ", date(2025, 1, 6), 1, 1, 1, 1, 10)],
                  covered_from=date(2025, 1, 6), covered_to=date(2025, 1, 6))
        assert not list(Path(tmp_path).glob("*.tmp"))
        assert store.has("XYZ")

    def test_a_corrupt_file_is_refetched_rather_than_raising(self, tmp_path):
        prices = _weekday_prices(date(2025, 1, 6), 10)
        days = sorted(prices)
        provider = _SpyProvider(prices)
        store = BarStore(str(tmp_path))
        cached = CachedBarProvider(provider, store)
        cached.get_stock_bars("XYZ", days[0], days[-1])
        assert len(provider.calls) == 1

        (Path(tmp_path) / "XYZ.parquet").write_bytes(b"not a parquet file")
        out = cached.get_stock_bars("XYZ", days[0], days[-1])
        assert len(provider.calls) == 2, "a corrupt file must be a miss, not a crash"
        assert [b.bar_date for b in out] == days
        # ...and it healed, rather than staying broken forever.
        cached.get_stock_bars("XYZ", days[0], days[-1])
        assert len(provider.calls) == 2

    def test_a_zero_byte_file_is_discarded(self, tmp_path):
        (Path(tmp_path)).mkdir(parents=True, exist_ok=True)
        (Path(tmp_path) / "XYZ.parquet").write_bytes(b"")
        store = BarStore(str(tmp_path))
        assert store.get("XYZ", date(2025, 1, 6), date(2025, 1, 10)) is None

    def test_a_file_from_an_older_schema_is_discarded_not_misread(self, tmp_path):
        """No provenance columns => cannot prove coverage => fail closed."""
        Path(tmp_path).mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"symbol": "XYZ", "bar_date": "2025-01-06", "open": 1.0,
                       "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}]
                     ).to_parquet(Path(tmp_path) / "XYZ.parquet", index=False)
        store = BarStore(str(tmp_path))
        assert store.get("XYZ", date(2025, 1, 6), date(2025, 1, 6)) is None

    def test_the_env_var_moves_the_cache_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BACKTEST_BARS_CACHE_DIR", str(tmp_path / "elsewhere"))
        store = BarStore()
        store.put("XYZ", [StockBar("XYZ", date(2025, 1, 6), 1, 1, 1, 1, 10)],
                  covered_from=date(2025, 1, 6), covered_to=date(2025, 1, 6))
        assert (tmp_path / "elsewhere" / "XYZ.parquet").exists()

    def test_delegation_reaches_the_wrapped_provider(self, tmp_path):
        """Composition, not a provider change: everything else passes through."""
        inner = ScriptedProvider("XYZ", {date(2025, 1, 6): 100.0}, [])
        cached = CachedBarProvider(inner, BarStore(str(tmp_path)))
        assert cached.get_contract_universe("XYZ", date(2025, 1, 6), 7) == []
        assert cached.get_option_bars([], date(2025, 1, 6), date(2025, 1, 6)) == {}
        assert cached.symbol == "XYZ", "__getattr__ must reach the inner provider"


# --------------------------------------------------------------------------- #
# 5. The zero-API-calls contract, with the sockets actually taken away
# --------------------------------------------------------------------------- #
class _NoNetwork:
    """Refuses every socket, so a stray network call fails loudly and locally."""

    def __enter__(self):
        self._real = socket.socket

        def _refuse(*args, **kwargs):
            raise AssertionError(
                "a warm replay opened a socket — the run is not offline"
            )

        socket.socket = _refuse
        return self

    def __exit__(self, *exc):
        socket.socket = self._real
        return False


class _RefusingProvider:
    """Every call is a test failure. The complement to the socket block.

    Blocking sockets alone proves nothing against a mock provider; refusing at
    the provider alone would miss a network call made somewhere else. Together
    they pin the actual contract: the second run of a window reads only disk.
    """

    def get_stock_bars(self, symbol, start, end):
        raise AssertionError(f"warm run fetched stock bars for {symbol}")

    def get_contract_universe(self, *a, **kw):
        raise AssertionError("warm run fetched the contract universe")

    def get_option_bars(self, *a, **kw):
        raise AssertionError("warm run fetched option bars")


class TestASecondRunOfAWindowIsOffline:
    def test_socket_blocked_second_replay_completes(self, tmp_path, falling_then_flat):
        """Bars AND chains warm: the whole point of pairing the two caches.

        MUTATION CHECK: remove the bars cache (hand `Simulator` the raw provider)
        and this fails at `_load_stock_bars` — which is exactly how a warm run
        behaved before FC-060 Layer 2.
        """
        days, closes, exps = falling_then_flat
        chain_store = ChainStore(str(tmp_path / "chains"))
        bar_store = BarStore(str(tmp_path / "bars"))

        live = CachedBarProvider(ScriptedProvider("XYZ", closes, exps), bar_store)
        cold = Simulator(
            Config(), live, ChainBuilder(live, risk_free_rate=0.04, store=chain_store),
            ["XYZ"], days[0], days[-1], starting_cash=50_000.0, max_dte=7,
        ).run()
        assert cold.daily, "the cold run placed nothing; the warm test is vacuous"

        warm_provider = CachedBarProvider(_RefusingProvider(), bar_store)
        warm_sim = Simulator(
            Config(), warm_provider,
            ChainBuilder(warm_provider, risk_free_rate=0.04, store=chain_store),
            ["XYZ"], days[0], days[-1], starting_cash=50_000.0, max_dte=7,
        )
        with _NoNetwork():
            warm = warm_sim.run()

        assert [s.day for s in warm.daily] == [s.day for s in cold.daily]
        assert warm.final_equity == cold.final_equity, (
            "the warm run produced a different result; a cache must be invisible"
        )
        assert warm_provider.fetches == 0 and warm_provider.hits == 1


# --------------------------------------------------------------------------- #
# Review round 2 — an empty answer must never become a coverage claim (Q3)
# --------------------------------------------------------------------------- #
class TestAnEmptyResponseIsNeverCoverage:
    """Alpaca answers "unknown symbol", "not entitled" and "we fell over" the
    same way: HTTP 200, empty payload. A cache that believed any of them would
    serve "this symbol has no history" for the life of the file, and the only
    escape would be `use_cache=False` or deleting it by hand.
    """

    def test_an_empty_fetch_writes_no_file_at_all(self, tmp_path):
        provider = _SpyProvider({})
        store = BarStore(str(tmp_path))
        cached = CachedBarProvider(provider, store)

        assert cached.get_stock_bars("XYZ", date(2025, 1, 6), date(2025, 1, 17)) == []
        assert len(provider.calls) == 1
        assert not store.has("XYZ"), "an outage was frozen into the cache"
        assert store.covered_window("XYZ") is None

    def test_the_next_run_refetches_and_then_stores(self, tmp_path):
        """The recovery this exists for: the vendor comes back, so do we."""
        prices = _weekday_prices(date(2025, 1, 6), 10)
        days = sorted(prices)
        provider = _SpyProvider({})
        store = BarStore(str(tmp_path))
        cached = CachedBarProvider(provider, store)

        assert cached.get_stock_bars("XYZ", days[0], days[-1]) == []
        assert len(provider.calls) == 1

        provider._prices = dict(prices)  # the outage clears
        recovered = cached.get_stock_bars("XYZ", days[0], days[-1])
        assert len(provider.calls) == 2, (
            "the empty answer was cached; the second run never asked again"
        )
        assert [b.bar_date for b in recovered] == days
        assert store.covered_window("XYZ") == (days[0], days[-1])

        # ...and NOW it is a hit, because there is real data behind the claim.
        cached.get_stock_bars("XYZ", days[0], days[-1])
        assert len(provider.calls) == 2

    def test_an_empty_fetch_does_not_widen_an_existing_claim(self, tmp_path):
        """The subtler half: a later empty answer must not extend a good file.

        Widening on empty would mark a range as covered that the vendor never
        answered for, and every future read of it would return silence.
        """
        prices = _weekday_prices(date(2025, 1, 6), 10)
        days = sorted(prices)
        provider = _SpyProvider(prices)
        store = BarStore(str(tmp_path))
        cached = CachedBarProvider(provider, store)
        cached.get_stock_bars("XYZ", days[0], days[-1])
        before = store.covered_window("XYZ")

        provider._prices = {}  # the vendor falls over on the extension
        cached.get_stock_bars("XYZ", days[0], days[-1] + timedelta(days=30))
        assert store.covered_window("XYZ") == before, (
            "an empty response widened the covered window"
        )

    def test_the_returned_span_is_recorded_beside_the_request(self, tmp_path):
        """A truncated non-empty answer cannot be detected from inside.

        Nothing here can tell "the vendor returned January of the year I asked
        for" from "the symbol only traded in January". So both are recorded and
        an auditor can see them diverge in one read; `data_*` is what you can
        rely on, `covered_*` is what the hit test has to use (a request whose
        edge lands on a holiday must still be a hit).
        """
        prices = _weekday_prices(date(2025, 1, 6), 5)
        days = sorted(prices)
        provider = _SpyProvider(prices)
        store = BarStore(str(tmp_path))
        CachedBarProvider(provider, store).get_stock_bars(
            "XYZ", days[0] - timedelta(days=10), days[-1] + timedelta(days=10))

        df = pd.read_parquet(Path(tmp_path) / "XYZ.parquet")
        assert df["covered_from"].iloc[0] == (days[0] - timedelta(days=10)).isoformat()
        assert df["data_from"].iloc[0] == days[0].isoformat()
        assert df["data_to"].iloc[0] == days[-1].isoformat()


class TestAMalformedFrameIsAMiss:
    """Q12. Content garbage behaves like a corrupt parquet: discard and refetch.

    Same policy as `ChainStore.get`'s unreadable-file path, and for the same
    reason — a cache that can wedge every future run on one bad cell is worse
    than no cache.
    """

    def test_an_unparseable_bar_date_is_discarded_rather_than_raised(self, tmp_path):
        prices = _weekday_prices(date(2025, 1, 6), 10)
        days = sorted(prices)
        provider = _SpyProvider(prices)
        store = BarStore(str(tmp_path))
        cached = CachedBarProvider(provider, store)
        cached.get_stock_bars("XYZ", days[0], days[-1])
        assert len(provider.calls) == 1

        path = Path(tmp_path) / "XYZ.parquet"
        df = pd.read_parquet(path)
        df.loc[0, "bar_date"] = "not-a-date"
        df.to_parquet(path, index=False)

        out = cached.get_stock_bars("XYZ", days[0], days[-1])
        assert len(provider.calls) == 2, "a bad cell must be a miss, not a crash"
        assert [b.bar_date for b in out] == days
        assert not path.with_suffix(".parquet.tmp").exists()

    def test_a_non_castable_volume_is_discarded_too(self, tmp_path):
        prices = _weekday_prices(date(2025, 1, 6), 6)
        days = sorted(prices)
        provider = _SpyProvider(prices)
        store = BarStore(str(tmp_path))
        cached = CachedBarProvider(provider, store)
        cached.get_stock_bars("XYZ", days[0], days[-1])

        path = Path(tmp_path) / "XYZ.parquet"
        df = pd.read_parquet(path)
        df["volume"] = df["volume"].astype(str)
        df.loc[0, "volume"] = "many"
        df.to_parquet(path, index=False)

        assert store.get("XYZ", days[0], days[-1]) is None
        assert not path.exists(), "the unusable file was left in place"


class TestADiscardedExistingFileTakesItsWindowWithIt:
    """S4. A rewrite must not inherit coverage it can no longer back with data.

    `_decode` discards a file whose schema passed but whose contents will not
    parse. If `put` then kept the OLD file's `covered_from`/`covered_to` while
    holding only the newly-fetched bars, the file it writes would claim a window
    it has no rows for — and unlike the empty-response case it would look
    perfectly healthy, so nothing would ever re-ask for the missing days.
    """

    def test_the_rewritten_file_claims_only_what_it_can_serve(self, tmp_path):
        prices = _weekday_prices(date(2025, 1, 6), 30)
        days = sorted(prices)
        provider = _SpyProvider(prices)
        store = BarStore(str(tmp_path))
        CachedBarProvider(provider, store).get_stock_bars("XYZ", days[0], days[-1])
        assert store.covered_window("XYZ") == (days[0], days[-1])

        # Corrupt the CONTENTS (not the schema) of the stored file.
        path = Path(tmp_path) / "XYZ.parquet"
        df = pd.read_parquet(path)
        df["bar_date"] = "not-a-date"
        df.to_parquet(path, index=False)

        # A later, NARROWER fetch rewrites the file from scratch.
        store.put("XYZ", [
            StockBar("XYZ", d, 1.0, 1.0, 1.0, 1.0, 100) for d in days[20:25]
        ], covered_from=days[20], covered_to=days[24])

        window = store.covered_window("XYZ")
        assert window == (days[20], days[24]), (
            f"the rewritten file claims {window} while holding only "
            f"{days[20]}..{days[24]} — it inherited the discarded file's window"
        )
        # ...so the days it lost are a MISS, and get refetched.
        assert store.get("XYZ", days[0], days[-1]) is None
        served = store.get("XYZ", days[20], days[24])
        assert served is not None and len(served) == 5

    def test_a_readable_existing_file_still_widens_normally(self, tmp_path):
        """The guard must not break the merge it sits inside."""
        prices = _weekday_prices(date(2025, 1, 6), 30)
        days = sorted(prices)
        store = BarStore(str(tmp_path))
        store.put("XYZ", [StockBar("XYZ", d, 1.0, 1.0, 1.0, 1.0, 10)
                          for d in days[0:10]],
                  covered_from=days[0], covered_to=days[9])
        store.put("XYZ", [StockBar("XYZ", d, 1.0, 1.0, 1.0, 1.0, 10)
                          for d in days[10:20]],
                  covered_from=days[10], covered_to=days[19])
        assert store.covered_window("XYZ") == (days[0], days[19])
        assert len(store.get("XYZ", days[0], days[19]) or []) == 20
