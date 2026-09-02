"""A provider wrapper that REFUSES to fetch option chains from the vendor.

FC-096 Phase B B3. The sim service is a scale-to-zero Cloud Run *service* that
answers an operator's interactive request in seconds. It can only do that
because every chain it needs is already in the lake: a cold chain fetch is
minutes of vendor round-trips per symbol-year, on a request budget of 120 s, on
an image with a 240 s cold-start tail.

"It should never fetch" was, until this module, an assumption. A lake miss —
a symbol nobody backfilled, a window a day too wide, an object written under a
different model fingerprint — falls straight through ``ChainBuilder.build`` into
``_build_uncached``, and the service would quietly start paying vendor latency
and vendor rate limit for a request that was supposed to be instant. Nothing in
the RESULT would show it: the numbers come out identical, just far later.

So the contract is enforced rather than assumed. Wrapped around the vendor
client, INNERMOST (see ``run_sweep(vendor_guard=...)``), this refuses the two
calls that constitute a chain fetch and lets everything else through:

* ``get_contract_universe`` — REFUSED
* ``get_option_bars``       — REFUSED
* ``get_stock_bars``        — **PERMITTED**, deliberately

**Why bars are permitted, explicitly.** Daily stock bars are one small request
per symbol-window, they are cached on disk by ``BarStore`` between runs, and
they are rate-benign next to an option chain (which is a contract-universe
listing plus a bar request per contract, hundreds of symbols wide, per session).
Refusing them would make the service unable to derive its own decision calendar
and would buy nothing: the cost this guard exists to bound is the chain fetch.
The FC-095 data-key item is the cross-reference — bars and chains are keyed and
sourced differently, and the coverage the lake guarantees is the CHAIN half.

A refusal is a hard failure, never a silent empty result. Returning ``[]`` would
produce a chain with no contracts, which replays perfectly happily as "the
strategy found nothing to sell" — a fabricated verdict, the FC-057 class. The
raise reaches ``run_sweep``'s per-window handler and every arm of that window is
recorded as an error, with this message on it.
"""

from __future__ import annotations

from typing import Any, Sequence

import structlog

logger = structlog.get_logger(__name__)

#: Provider methods that constitute a vendor CHAIN fetch. Both are called by
#: ``ChainBuilder._build_uncached`` and by nothing else on the replay path.
REFUSED_METHODS: Sequence[str] = ("get_contract_universe", "get_option_bars")

#: Emitted once per refusal. The plan names this string; monitoring and the
#: rollout check grep for it.
REFUSAL_EVENT = "sim_service_cold_fetch_refused"


class ColdChainFetchRefused(RuntimeError):
    """A replay tried to fetch an option chain the lake does not hold.

    ``RuntimeError`` rather than a bespoke base: ``run_sweep`` records it on the
    window's rows through the same path every other materialisation failure
    takes, so the operator sees it in the cell error and in the terminal status
    row's ``error``, not only in a log line.
    """


class ChainFetchRefusingProvider:
    """Wraps a data provider; chain fetches raise, everything else passes.

    Attribute access falls through to the wrapped provider (``__getattr__``), so
    this is a drop-in for the vendor client anywhere in the engine — including
    the ``_CountingProvider`` and ``CachedBarProvider`` layers that wrap it.

    ``refusals`` counts what it stopped, for the run's log line. It is not a
    "how bad was it" metric: the FIRST refusal fails the window, so the count is
    at most one per window and exists so a multi-symbol run can say which
    windows died this way.
    """

    def __init__(self, provider: Any, *, run_id: str = "") -> None:
        self._provider = provider
        self.run_id = run_id
        self.refusals = 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"ChainFetchRefusingProvider({self._provider!r}, "
                f"refusals={self.refusals})")

    # -- the two refused calls ------------------------------------------- #
    def get_contract_universe(self, underlying=None, *args, **kwargs):
        self._refuse("get_contract_universe", underlying, args, kwargs)

    def get_option_bars(self, symbols=None, *args, **kwargs):
        self._refuse("get_option_bars", symbols, args, kwargs)

    # -- the permitted one, named rather than inherited ------------------- #
    def get_stock_bars(self, *args, **kwargs):
        """Passed through. See the module docstring for why bars are allowed.

        Spelled out rather than left to ``__getattr__`` so that "bars are
        permitted" is a statement in this file, reviewable next to the refusals,
        instead of a consequence of what was not written down.
        """
        return self._provider.get_stock_bars(*args, **kwargs)

    # -------------------------------------------------------------------- #
    def _refuse(self, method: str, subject, args, kwargs) -> None:
        self.refusals += 1
        as_of = kwargs.get("as_of")
        if as_of is None and args:
            as_of = args[0]
        detail = (f"{method}(subject={subject!r}, as_of={as_of!r})")
        logger.error(
            "Chain fetch REFUSED — the sim service may not reach the options "
            "vendor; this window's chains are not in the lake",
            event_category="backtest",
            event_type=REFUSAL_EVENT,
            run_id=self.run_id, method=method, subject=str(subject)[:80],
            as_of=str(as_of)[:32],
        )
        raise ColdChainFetchRefused(
            f"the sim service refused a vendor chain fetch: {detail}. This "
            f"window is not fully covered by the chain lake, so replaying it "
            f"here would cost minutes of vendor round-trips on an interactive "
            f"request. Back the window off, or run it through the "
            f"`backtest-sweep` Job, or backfill the gap first "
            f"(`gcloud run jobs execute data-backfill "
            f"--update-env-vars=BACKFILL_SYMBOLS=<SYM>,BACKFILL_START=<start>,"
            f"BACKFILL_END=<end>`)."
        )

    def __getattr__(self, name):
        return getattr(self._provider, name)
