"""Cloud Run detection must recognise Jobs, not just Services.

FC-059: `_is_cloud_run()` checked only `K_SERVICE`. Cloud Run **Services** set
that variable; Cloud Run **Jobs** set `CLOUD_RUN_JOB` / `CLOUD_RUN_EXECUTION`.
So inside a Job the check returned False, `log_to_file` became True, and logs
were written to a file inside a container whose filesystem is destroyed on exit.

The monthly backtest screen runs as a Job. Without this, it emits **nothing** to
Cloud Logging and a failure is undiagnosable — the operator sees "task failed"
with no reason. Found by watching the first real Job execution produce zero
application logs in 19 minutes.
"""

import os
import pytest

from src.utils.logger import _is_cloud_run

CLOUD_RUN_VARS = ("K_SERVICE", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in CLOUD_RUN_VARS:
        monkeypatch.delenv(v, raising=False)


def test_local_dev_is_not_cloud_run():
    """Local runs must still log to a file — that behaviour is wanted."""
    assert _is_cloud_run() is False


@pytest.mark.parametrize("var", CLOUD_RUN_VARS)
def test_every_cloud_run_shape_is_detected(monkeypatch, var):
    """Services set K_SERVICE; Jobs set the other two. All must log to stderr."""
    monkeypatch.setenv(var, "some-value")
    assert _is_cloud_run() is True, (
        f"{var} is set but _is_cloud_run() is False — logs would go to a file "
        f"inside an ephemeral container and never reach Cloud Logging"
    )


def test_the_job_case_specifically(monkeypatch):
    """Pins FC-059 rather than only the general rule.

    A Cloud Run Job sets CLOUD_RUN_JOB and CLOUD_RUN_EXECUTION but NOT
    K_SERVICE. That exact combination was the bug.
    """
    monkeypatch.setenv("CLOUD_RUN_JOB", "data-backfill")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "data-backfill-abc12")
    assert os.environ.get("K_SERVICE") is None
    assert _is_cloud_run() is True


def test_empty_string_is_not_treated_as_present(monkeypatch):
    """An explicitly-blank var should not flip the decision."""
    monkeypatch.setenv("CLOUD_RUN_JOB", "")
    assert _is_cloud_run() is False
