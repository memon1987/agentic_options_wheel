"""Logging configuration for Options Wheel strategy.

Also home to the **tally dispatch** (FC-092): a single, process-stable structlog
processor that forwards every event to whichever ``RejectionTally`` is currently
active. See :func:`tally_dispatch` for why the indirection has to exist here,
in the module that configures structlog, rather than in the tally itself.
"""

import contextvars
import os
import sys
import structlog
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional


# Cloud Run sets different variables for the two runtime shapes, and only
# Services set K_SERVICE. A Cloud Run *Job* sets CLOUD_RUN_JOB and
# CLOUD_RUN_EXECUTION instead — so checking K_SERVICE alone reports False in a
# Job, which sends logs to a FILE inside a container whose filesystem is
# destroyed on exit. The monthly backtest screen runs as a Job: without this,
# it produces no observable output at all and a failure is undiagnosable.
_CLOUD_RUN_ENV_VARS = ("K_SERVICE", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION")


def _is_cloud_run() -> bool:
    """Detect Cloud Run — Services (K_SERVICE) and Jobs (CLOUD_RUN_JOB)."""
    return any(os.environ.get(v) for v in _CLOUD_RUN_ENV_VARS)


# --------------------------------------------------------------------------- #
# Tally dispatch (FC-092 item 1)
# --------------------------------------------------------------------------- #
#
# THE DEFECT, measured on `main` at 7087007: two `evaluate_symbol` calls in one
# process produced `blocked_days_by_reason={'already holds this underlying…': 16,
# 'selection: duplicate underlying': 4}` for the first and `{}` for the second.
# The monthly screen replays 14 symbols in one process, so 13 of every 14
# `backtest_runs` rows have carried an empty tally and a NULL
# `binding_constraint` since the screen went live.
#
# THE MECHANISM: `cache_logger_on_first_use=True` (below) makes a
# `BoundLoggerLazyProxy` cache its whole processor chain the first time it is
# used, and `structlog.configure()` does NOT invalidate that cache.
# `RejectionTally.__enter__` installed ITSELF — a bound method of one tally
# instance — at the front of the chain, so every strategy logger cached replay
# #1's tally and kept delivering to it for the life of the process.
#
# THE FIX, and why it is here: the thing in the cached chain must be
# PROCESS-STABLE, so the identity of the active tally has to move somewhere the
# stable function can look it up at call time. That is this contextvar. The
# dispatch is installed once (by `setup_logging`, ahead of everything else in
# the chain — see `runner._QUIET_LOGGERS` for why the FRONT is load-bearing) and
# never swapped again; entering a tally now costs a `ContextVar.set`, and the
# cached loggers reach the CURRENT tally because they never held a reference to
# any particular one.
#
# It lives in this module rather than in `rejections.py` because `rejections` is
# backtest code and this is the module that owns the processor chain: importing
# the backtest engine from `src/utils/logger.py` would put pandas and the
# simulator on the live trading path's import graph.
#
# A ContextVar rather than a module global also happens to be the right
# THREADING answer — a thread starts with a fresh context, so a tally entered on
# one thread is invisible to another. That is not licence to parallelise the
# sweep: `ExecutionEngine._failed_symbols` is still a module-global set, and
# `runner`'s docstring still says sequential.
_ACTIVE_TALLY: contextvars.ContextVar[Optional[Callable]] = contextvars.ContextVar(
    "active_rejection_tally", default=None)


def tally_dispatch(logger, name, event_dict):
    """Forward one event to the active tally processor, if there is one.

    A no-op with no tally active, which is every event the live bot ever emits.
    Never raises: a diagnostic that can break the thing it is diagnosing is not
    worth having, and this one sits in front of the entire chain.
    """
    processor = _ACTIVE_TALLY.get()
    if processor is None:
        return event_dict
    try:
        return processor(logger, name, event_dict)
    except Exception:  # noqa: BLE001 - diagnostics must never break a run
        return event_dict


def ensure_tally_dispatch() -> Optional[Dict[str, Any]]:
    """Put :func:`tally_dispatch` at the FRONT of the configured chain.

    Returns the previous configuration when it had to reconfigure, so the caller
    can restore it, and ``None`` when the dispatch was already in place — which
    is the production case, because `setup_logging` installs it. Returning None
    there is what keeps the steady state free of `structlog.configure()` calls:
    reconfiguring on every tally entry would be harmless for the tally (the
    cached proxies would keep working) but would trip the test suite's
    logging-leak guard and churn global state for nothing.

    The dispatch must be FIRST, ahead of ``structlog.stdlib.filter_by_level``,
    so the tally still counts an INFO event the stdlib level then drops — which
    is exactly what a quieted sweep does to every strategy logger.
    """
    previous = structlog.get_config()
    processors = list(previous.get("processors", []))
    if processors and processors[0] is tally_dispatch:
        return None
    structlog.configure(
        processors=[tally_dispatch]
        + [p for p in processors if p is not tally_dispatch])
    return previous


@contextmanager
def active_tally(processor: Callable) -> Iterator[None]:
    """Make ``processor`` the tally the dispatch forwards to, for this block.

    Restores the previous binding on exit — nested and sequential uses both
    behave, and a raise cannot leave a dead tally bound.
    """
    token = _ACTIVE_TALLY.set(processor)
    try:
        yield
    finally:
        _ACTIVE_TALLY.reset(token)


def setup_logging(log_level: str = "INFO", log_to_file: bool = None, log_file: str = "logs/options_wheel.log") -> None:
    """Setup structured logging for the application.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_to_file: Whether to log to file. Defaults to True for local dev,
                     False when running in Cloud Run.
        log_file: Log file path
    """
    # Default: log to file locally, log to stderr in Cloud Run
    if log_to_file is None:
        log_to_file = not _is_cloud_run()

    # Create logs directory if it doesn't exist
    if log_to_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

    # Configure standard library logging
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))

    # Remove any existing handlers to avoid duplicates
    root_logger.handlers.clear()

    formatter = logging.Formatter("%(message)s")
    if log_to_file:
        handler = logging.FileHandler(log_file, mode="a")
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    # Configure structlog.
    #
    # `tally_dispatch` is FIRST, and both facts about its position matter. It is
    # in the chain AT ALL so that a logger which caches this chain — every
    # strategy logger, under `cache_logger_on_first_use=True` — can still reach
    # a `RejectionTally` entered later, and a DIFFERENT tally on each subsequent
    # replay (FC-092 item 1). It is FIRST so the tally sees an event that
    # `filter_by_level` then drops, which is what a quieted sweep does to every
    # strategy logger (see `scenarios/runner._QUIET_LOGGERS`).
    structlog.configure(
        processors=[
            tally_dispatch,
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer()
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

def get_logger(name: str) -> Any:
    """Get a structured logger instance.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Structured logger instance
    """
    return structlog.get_logger(name)
