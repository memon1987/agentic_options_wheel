"""FC-120 PR-2 (R6-J) — the quiet data plane.

The roller's bounded quote reads (diagnostic AND pricing) run in daemon worker
threads and enter ``alpaca_client.quiet_data_plane()``. Inside it,
``api_retry`` gates and records on ``_data_plane_breaker`` and logs at DEBUG
with ``event_category="data"`` — so a quote-endpoint outage can open the
data-plane breaker without touching the order-plane breaker that gates
``get_order_by_id`` (the settle's read), and a missing diagnostic never lands
an error-category row.

T-20 proper (the real decorated method driven through the roller's
``_bounded_read``) is the last class here. *Catches:* the flag leaking across
threads in either direction; a diagnostic storm opening the breaker that gates
the settle; a quiet failure counted in the error views.
"""

import threading
from unittest.mock import Mock, patch

import pytest
import requests.exceptions

from src.api import alpaca_client as m
from src.api.alpaca_client import AlpacaClient, api_retry


@pytest.fixture
def fresh_breakers(monkeypatch):
    """Isolate both module-global breakers and make retries instant."""
    monkeypatch.setattr(m, '_circuit_breaker', m.CircuitBreaker(5, 60))
    monkeypatch.setattr(m, '_data_plane_breaker',
                        m.CircuitBreaker(5, 60, name='data_plane'))
    monkeypatch.setattr('time.sleep', lambda _s: None)
    return m


def _error_rows(mock_logger):
    return [c for c in mock_logger.error.call_args_list
            if c.kwargs.get('event_category') == 'error']


def _debug(mock_logger, event_type):
    return [c for c in mock_logger.debug.call_args_list
            if c.kwargs.get('event_type') == event_type]


@pytest.fixture
def stock_client():
    """A real ``AlpacaClient`` whose IEX data client is a mock."""
    with patch('src.api.alpaca_client.TradingClient'), \
            patch('src.api.alpaca_client.StockHistoricalDataClient') as stock_cls, \
            patch('src.api.alpaca_client.OptionHistoricalDataClient'):
        config = Mock(alpaca_api_key='k', alpaca_secret_key='s',
                      paper_trading=True)
        client = AlpacaClient(config)
        yield client, stock_cls.return_value


class TestTheFlag:
    def test_it_is_thread_local_and_restored(self, fresh_breakers):
        seen = {}

        def other():
            seen['other'] = m.in_quiet_data_plane()

        assert m.in_quiet_data_plane() is False
        with m.quiet_data_plane():
            assert m.in_quiet_data_plane() is True
            worker = threading.Thread(target=other)
            worker.start()
            worker.join()
            with m.quiet_data_plane():
                pass
            assert m.in_quiet_data_plane() is True, "a nested exit cleared it"
        assert seen['other'] is False, "the flag leaked to another thread"
        assert m.in_quiet_data_plane() is False


class TestApiRetryOnTheQuietPlane:
    def test_an_exhausted_retry_records_on_the_data_plane_only(
            self, fresh_breakers):
        calls = []

        @api_retry
        def flaky():
            calls.append(1)
            raise requests.exceptions.ConnectionError("conn reset")

        with patch('src.api.alpaca_client.logger') as log:
            with m.quiet_data_plane():
                with pytest.raises(requests.exceptions.ConnectionError):
                    flaky()
        assert len(calls) == 3
        assert m._data_plane_breaker.failure_count == 1
        assert m._circuit_breaker.failure_count == 0
        assert not _error_rows(log)
        exhausted = _debug(log, 'api_retry_exhausted')
        assert len(exhausted) == 1
        assert exhausted[0].kwargs['quiet'] is True
        assert exhausted[0].kwargs['event_category'] == 'data'

    def test_five_failures_open_only_the_data_plane_breaker(self, fresh_breakers):
        @api_retry
        def down():
            raise requests.exceptions.Timeout("read timed out")

        @api_retry
        def order_read():
            return {'status': 'filled'}

        with patch('src.api.alpaca_client.logger') as log:
            with m.quiet_data_plane():
                for _ in range(5):
                    with pytest.raises(requests.exceptions.Timeout):
                        down()
                with pytest.raises(m.CircuitBreakerOpen):
                    down()
            # The main thread is on the order plane, which is untouched.
            assert order_read() == {'status': 'filled'}
        assert m._data_plane_breaker.state == 'open'
        assert m._circuit_breaker.state == 'closed'
        assert not _error_rows(log)
        blocked = _debug(log, 'circuit_breaker_blocked')
        assert blocked and blocked[0].kwargs['quiet'] is True

    def test_a_refused_request_is_not_an_outage(self, fresh_breakers):
        """A non-retryable error (the broker refused the request outright) is
        not counted: the breaker exists for an endpoint that is DOWN."""
        @api_retry
        def refused():
            raise ValueError("invalid symbol")

        with m.quiet_data_plane():
            with pytest.raises(ValueError):
                refused()
        assert m._data_plane_breaker.failure_count == 0

    def test_the_order_plane_is_exactly_as_before(self, fresh_breakers):
        """Main's behaviour, pinned rather than changed. tenacity's
        ``reraise=True`` re-raises the last attempt's exception, never
        ``RetryError``, so ``api_retry``'s ``except RetryError`` branch is
        unreachable: an exhausted main-thread call records NOTHING on the
        order-plane breaker and logs no ``api_retry_exhausted``. PR-2 keeps the
        order plane byte-for-byte (DD-5); making that breaker live is its own
        decision — **FC-131** (filed from the PR-2 reviews, F4) owns it, and
        must flip this test deliberately when it lands."""
        @api_retry
        def flaky():
            raise requests.exceptions.ConnectionError("conn reset")

        with patch('src.api.alpaca_client.logger') as log:
            with pytest.raises(requests.exceptions.ConnectionError):
                flaky()
        assert m._circuit_breaker.failure_count == 0
        assert m._data_plane_breaker.failure_count == 0
        assert not [c for c in log.debug.call_args_list + log.error.call_args_list
                    if c.kwargs.get('event_type') == 'api_retry_exhausted']


class TestGetStockQuote:
    def test_it_logs_quietly_only_on_the_quiet_plane(self, fresh_breakers,
                                                      stock_client):
        client, iex = stock_client
        iex.get_stock_latest_quote.side_effect = (
            requests.exceptions.ConnectionError("conn reset"))
        with patch('src.api.alpaca_client.logger') as log:
            with m.quiet_data_plane():
                with pytest.raises(requests.exceptions.ConnectionError):
                    client.get_stock_quote('NVDA')
            assert not _error_rows(log)
            assert len(_debug(log, 'stock_quote_failed')) == 3   # per attempt
            with pytest.raises(requests.exceptions.ConnectionError):
                client.get_stock_quote('NVDA')
            loud = [c for c in _error_rows(log)
                    if c.kwargs.get('event_type') == 'stock_quote_error']
            assert len(loud) == 3, "the main-thread path must be unchanged"


class TestTheBreakerIsThreadSafe:
    """F6 (PR-2 review, SRE LOW-2): the roller's bounded-read workers record
    on the data-plane breaker CONCURRENTLY — an abandoned read can still be
    running when the next one starts. *Catches:* the open transition decided
    outside a lock, so two workers crossing the threshold together both see
    ``closed``, both log ``circuit_breaker_opened`` and race the state write.
    *Mutation:* drop the lock (log before setting ``open``, as before F6) →
    ``opened == ['a', 'b']``."""

    def test_two_threads_crossing_the_threshold_open_it_once(
            self, fresh_breakers):
        breaker = m.CircuitBreaker(failure_threshold=2, reset_timeout=60,
                                   name='data_plane')
        breaker.record_failure()                      # 1: below the threshold
        first_in, release, opened = threading.Event(), threading.Event(), []

        def warning(*_args, **kwargs):
            if kwargs.get('event_type') == 'circuit_breaker_opened':
                opened.append(threading.current_thread().name)
                if not first_in.is_set():
                    first_in.set()
                    release.wait(5)          # hold the first opener mid-log

        with patch('src.api.alpaca_client.logger') as log:
            log.warning.side_effect = warning
            a = threading.Thread(target=breaker.record_failure, name='a')
            a.start()
            assert first_in.wait(5), "thread a never crossed the threshold"
            b = threading.Thread(target=breaker.record_failure, name='b')
            b.start()
            b.join(5)
            release.set()
            a.join(5)
        assert opened == ['a']
        assert breaker.state == 'open' and breaker.failure_count == 3

    def test_a_success_landing_mid_open_leaves_a_consistent_state(
            self, fresh_breakers):
        """A success that lands while a failure is opening the breaker must
        not be overwritten by it: the transition is decided under the lock,
        so the breaker ends in a state its own count agrees with.
        *Mutation:* drop the lock → ``open`` with ``failure_count == 0`` —
        a breaker that blocks every read until the reset timeout although
        nothing has failed since the success."""
        breaker = m.CircuitBreaker(failure_threshold=1, reset_timeout=60,
                                   name='data_plane')
        in_log, release = threading.Event(), threading.Event()

        def warning(*_args, **kwargs):
            if kwargs.get('event_type') == 'circuit_breaker_opened':
                in_log.set()
                release.wait(5)

        with patch('src.api.alpaca_client.logger') as log:
            log.warning.side_effect = warning
            a = threading.Thread(target=breaker.record_failure)
            a.start()
            assert in_log.wait(5)
            breaker.record_success()                  # lands mid-open
            release.set()
            a.join(5)
        assert (breaker.state, breaker.failure_count) == ('closed', 0)


class TestT20ThroughTheRollersBoundedRead:
    """T-20 proper: the REAL decorated ``get_stock_quote`` driven through the
    roller's ``_bounded_read`` worker — the path every diagnostic and pricing
    stock read takes in production."""

    def test_a_failed_bounded_read_is_quiet_and_on_the_data_plane(
            self, fresh_breakers, stock_client):
        from src.strategy import call_roller as cr
        client, iex = stock_client
        iex.get_stock_latest_quote.side_effect = (
            requests.exceptions.ConnectionError("conn reset"))
        with patch('src.api.alpaca_client.logger') as log:
            value, why = cr._bounded_read(client.get_stock_quote, 'NVDA',
                                          timeout=5.0)
        assert (value, why) == (None, 'error')
        assert iex.get_stock_latest_quote.call_count == 3   # the real retry ran
        assert not _error_rows(log), "a quiet read logged an error row"
        assert m._data_plane_breaker.failure_count == 1
        assert m._circuit_breaker.failure_count == 0

    def test_five_failed_reads_open_the_data_plane_and_never_the_settle(
            self, fresh_breakers, stock_client):
        from src.strategy import call_roller as cr
        client, iex = stock_client
        iex.get_stock_latest_quote.side_effect = (
            requests.exceptions.ConnectionError("conn reset"))
        with patch('src.api.alpaca_client.logger'):
            for _ in range(5):
                cr._bounded_read(client.get_stock_quote, 'NVDA', timeout=5.0)
            assert m._data_plane_breaker.state == 'open'
            # A sixth quiet read is refused BY THE BREAKER, quietly.
            assert cr._bounded_read(client.get_stock_quote, 'NVDA',
                                    timeout=5.0) == (None, 'error')
            # The settle's read is on the order plane, which is closed.
            client.trading_client.get_order_by_id.return_value = Mock(
                id='o-1', symbol='X', status=Mock(value='canceled'), qty=1,
                filled_qty=0, filled_avg_price=None, filled_at=None,
                expired_at=None, canceled_at=None, submitted_at=None)
            assert client.get_order_by_id('o-1')['status'] == 'canceled'
        assert m._circuit_breaker.state == 'closed'

    def test_a_failed_diagnostic_option_read_leaves_one_breadcrumb(
            self, fresh_breakers):
        """``_post_settle_quote``: one ``call_roll_quote_refresh_failed`` per
        failed diagnostic option read — on the TRADE logger, never an error."""
        from src.strategy import call_roller as cr
        roller = object.__new__(cr.CallRoller)
        roller.alpaca = Mock()
        roller.alpaca.get_option_quote.side_effect = RuntimeError("down")
        roller.alpaca.get_stock_quote.side_effect = RuntimeError("down")
        with patch('src.strategy.call_roller.logger') as log:
            out = roller._post_settle_quote('X', 'GOOGL', {'ask': 1.0}, 'buy')
        assert out['cancel_quote_ask'] is None
        crumbs = [c for c in log.info.call_args_list
                  if c.kwargs.get('event_type') == 'call_roll_quote_refresh_failed']
        assert len(crumbs) == 1
        assert not [c for c in log.error.call_args_list
                    if c.kwargs.get('event_category') == 'error']
