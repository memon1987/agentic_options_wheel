"""The roller's per-position time budget — a MEASURED contract (FC-120 PR-2).

Plan: docs/plans/fc-120.md DD-4 (FC-113 (a), absorbed). A leaf module: it
imports nothing from ``src/`` (stdlib only), so ``call_roller``,
``wheel_engine``, ``Config`` and the tests can all import it without a cycle —
``wheel_engine`` imports ``Config``, so ``Config`` could never import
``wheel_engine``; that is why the cross-key bound lives here.

``per_position_budget_seconds`` counts every broker call on ``execute_roll``'s
path at its MODELED worst, per rung kind, along the real worst path: every poll
window times out, every bounded read hangs to its cap, and the LAST buy-to-close
attempt fills inside its settle, so the full sell-to-open ladder follows::

    R      = ORDER_READ_WORST_SECONDS                       # 9.0  one get_order_by_id at the retry worst
    C      = ORDER_ACTION_WORST_SECONDS                     # 2.0  one place_option_order / one cancel_order
    G      = max(POLL_INTERVAL_SECONDS, R)                  # 9    the trailing read of a window: the monotonic loop reads, THEN checks the deadline; sleep(min(5, remaining)) never overshoots
    P, D   = PRICING_READ_TIMEOUT_SECONDS, DIAG_READ_TIMEOUT_SECONDS          # 3.0, 2.0
    SETTLE = C + max(CANCEL_SETTLE_MIN_READS * R, CANCEL_SETTLE_TIMEOUT_SECONDS + R)   # 2 + max(18, 24) = 26  (R6-I: two reads regardless of RTT; the 15 s window is a floor)
    A      = btc_reprice_attempts + 1;   Wb = btc_fill_timeout_seconds // A;   Ws = stc_rung_timeout_seconds
    btc_attempt = 3P + C + D + (Wb + G) + SETTLE            # 9 + 2 + 2 + 49 + 26 = 88 — 3P: old option, stock, new option (execute-time on attempt 0, re-price after; all bounded — S-LOW-6 / T-LOW-2)
    btc    = A * btc_attempt + (A - 1) * D                  # + the re-priced attempts' deferred cancel-time stock read; the last attempt's fill lands inside its settle
    stc_rung = basis + C + reread + (Ws + G) + SETTLE + D + post_option
               basis: P on the primary (fresh bid) and on each fallback (its re-quote); 0 on an escalation (its basis IS the prior rung's post_option) and on the floor (quote-free)
               reread: D on every rung but the floor (it reuses rung 1's quote);  post_option: P when an escalation follows this rung, else D;  the trailing D is the cancel-time stock read
    stc    = Σ over primary, E escalations, the floor, F fallbacks
    budget = ceil(btc + stc)

Defaults (A = 3, Wb = 40, Ws = 30, E = 0, F = 2): btc = 3 × 88 + 2 × 2 = 268;
stc = primary 76 + floor 71 + 2 × fallback 76 = 299; **budget = 567 s**. With
``stc_escalation_rungs = 2``: 715 s. Today's (pre-PR-2) ladder shape (A = 1,
Wb = Ws = 120, E = 0, F = 2): 827 s. ``fallback_strike_attempts = 0``: 415 s.

The number is a CONTRACT the measurement is run against, not a measurement:
``tests/test_call_roller.py`` T-19 drives the real roller through a monotonic
fake (every bounded read hung to its cap, every order read at
``ORDER_READ_WORST_SECONDS``, every place/cancel at
``ORDER_ACTION_WORST_SECONDS``) and asserts the measured worst case is <= this
function on BOTH shipped profiles. A read added to the path without the
function following fails there.

What the budget EXCLUDES, stated so nobody has to guess:
``evaluate_roll_opportunity``'s reads (main-thread, unbounded, before
``execute_roll`` — FC-089), the ``strategy_lock`` wait (FC-113 (c)), and a
socket that never returns (FC-089): a hung read is outside any budget, which is
why the modeled RTT is a contract and not a measurement. The modeled RTT is
2.0 s (measured RTTs are 0.05–0.3 s) because the retry path — three attempts on
a 5xx/429 — is the case the budget exists for.

Consumers: ``wheel_engine.run_rolling_cycle`` (the cycle guard; logged on
``roll_cycle_started``), ``Config._validate_config`` (refuses a profile whose
budget exceeds ``MAX_PER_POSITION_BUDGET_SECONDS`` at load — fail closed),
``tests/test_cloudbuild_contract.py`` (the request-seam invariant consumes the
function; it must never re-type the formula) and the call roller (its timing
constants are re-exported from here under their historical names).
"""

import math
from typing import Any

#: /roll's cycle budget. Sized by FC-078 against the roll jobs' 1800 s
#: attemptDeadline; ``wheel_engine`` re-exports it as ``_CYCLE_BUDGET_SECONDS``.
CYCLE_BUDGET_SECONDS = 1500

#: Everything that runs before ``run_rolling_cycle``'s clock starts (the lock
#: wait, ``_is_market_open()``, building the engine) — see the seam test.
PREAMBLE_ALLOWANCE_SECONDS = 60

#: The largest per-position budget a profile may imply: one position must fit
#: in the cycle after the preamble. ``Config`` refuses anything above it.
MAX_PER_POSITION_BUDGET_SECONDS = CYCLE_BUDGET_SECONDS - PREAMBLE_ALLOWANCE_SECONDS

#: ``_poll_order_fill``'s read interval.
POLL_INTERVAL_SECONDS = 5

#: The cancel-and-settle window — a FLOOR, not a cap: the settle also reads the
#: order at least ``CANCEL_SETTLE_MIN_READS`` times however slow the reads are.
CANCEL_SETTLE_TIMEOUT_SECONDS = 15
CANCEL_SETTLE_MIN_READS = 2

#: Hard caps on the roller's bounded reads (``call_roller._bounded_read``).
#: Diagnostic reads (instrumentation) and pricing reads (a limit is computed
#: from them) both run on the quiet data-plane path.
DIAG_READ_TIMEOUT_SECONDS = 2.0
PRICING_READ_TIMEOUT_SECONDS = 3.0

#: The modeled broker round-trip (T-15's reference RTT). Measured: 0.05–0.3 s.
MODELED_BROKER_RTT_SECONDS = 2.0

#: ``alpaca_client.api_retry``: ``stop_after_attempt(3)`` with
#: ``wait_exponential(multiplier=1, min=1)`` — 1 s + 2 s of backoff.
API_RETRY_ATTEMPTS = 3
API_RETRY_BACKOFF_SECONDS = 1.0 + 2.0

#: One ``get_order_by_id`` at ``api_retry``'s worst: 3 × 2.0 + 3.0 = 9.0 s.
ORDER_READ_WORST_SECONDS = (API_RETRY_ATTEMPTS * MODELED_BROKER_RTT_SECONDS
                            + API_RETRY_BACKOFF_SECONDS)

#: ``place_option_order`` and ``cancel_order`` carry NO ``@api_retry``: one
#: attempt each.
ORDER_ACTION_WORST_SECONDS = MODELED_BROKER_RTT_SECONDS

#: The five ``rolling.*`` keys the budget reads (the buffer is not a timing
#: input). Named in ``Config``'s refusal and in ``roll_cycle_budget_misconfigured``.
BUDGET_KEYS = ('btc_fill_timeout_seconds', 'btc_reprice_attempts',
               'stc_rung_timeout_seconds', 'stc_escalation_rungs',
               'fallback_strike_attempts')


def PRIMARY_LADDER_RUNGS(stc_escalation_rungs: int) -> int:  # noqa: N802 - a named shape
    """Rungs on the primary candidate: rung 1, ``E`` escalation rungs, the floor.

    Shipped ``E = 0`` → 2, which is the ladder's pre-PR-2 shape (R6-A)."""
    return 2 + int(stc_escalation_rungs)


def per_position_budget_seconds(*, btc_fill_timeout_seconds: int,
                                btc_reprice_attempts: int,
                                stc_rung_timeout_seconds: int,
                                stc_escalation_rungs: int,
                                fallback_strike_attempts: int) -> int:
    """The modeled worst-case wall-clock of ONE position's ``execute_roll``.

    Keyword-only ints, so ``Config._validate_config`` can call it on the raw
    values it has just bounded, before any property exists. See the module
    docstring for the formula and what it excludes.

    Raises ``TypeError`` on anything but a plain ``int`` (``bool`` included):
    a Mock config, a ``MagicMock`` (whose arithmetic would silently "work") or
    a float must fail loudly here rather than produce a plausible number.
    """
    values = {
        'btc_fill_timeout_seconds': btc_fill_timeout_seconds,
        'btc_reprice_attempts': btc_reprice_attempts,
        'stc_rung_timeout_seconds': stc_rung_timeout_seconds,
        'stc_escalation_rungs': stc_escalation_rungs,
        'fallback_strike_attempts': fallback_strike_attempts,
    }
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(
                f"roll_budget: rolling.{key} must be an int (got {value!r})")

    R = ORDER_READ_WORST_SECONDS
    C = ORDER_ACTION_WORST_SECONDS
    G = max(POLL_INTERVAL_SECONDS, R)
    P, D = PRICING_READ_TIMEOUT_SECONDS, DIAG_READ_TIMEOUT_SECONDS
    settle = C + max(CANCEL_SETTLE_MIN_READS * R, CANCEL_SETTLE_TIMEOUT_SECONDS + R)

    attempts = btc_reprice_attempts + 1
    btc_window = btc_fill_timeout_seconds // attempts
    stc_window = stc_rung_timeout_seconds
    escalations = stc_escalation_rungs
    fallbacks = fallback_strike_attempts

    btc_attempt = 3 * P + C + D + (btc_window + G) + settle
    btc = attempts * btc_attempt + (attempts - 1) * D

    def stc_rung(*, basis: float, reread: float, post_option: float) -> float:
        return basis + C + reread + (stc_window + G) + settle + D + post_option

    stc = stc_rung(basis=P, reread=D, post_option=(P if escalations > 0 else D))
    for n in range(1, escalations + 1):
        stc += stc_rung(basis=0.0, reread=D,
                        post_option=(P if n < escalations else D))
    stc += stc_rung(basis=0.0, reread=0.0, post_option=D)          # the floor
    stc += fallbacks * stc_rung(basis=P, reread=D, post_option=D)

    return int(math.ceil(btc + stc))


def from_config(config: Any) -> int:
    """``per_position_budget_seconds`` over a ``Config``'s ``rolling.*`` values.

    Reads exactly the five keys in ``BUDGET_KEYS``. A Mock config that leaves
    any of them unset raises ``TypeError`` here — the right failure, at the
    right place."""
    return per_position_budget_seconds(
        btc_fill_timeout_seconds=config.rolling_btc_fill_timeout_seconds,
        btc_reprice_attempts=config.rolling_btc_reprice_attempts,
        stc_rung_timeout_seconds=config.rolling_stc_rung_timeout_seconds,
        stc_escalation_rungs=config.rolling_stc_escalation_rungs,
        fallback_strike_attempts=config.rolling_fallback_strike_attempts,
    )
