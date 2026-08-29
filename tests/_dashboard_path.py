"""Put ``dashboard/backend`` on ``sys.path`` without shadowing the repo's CLI.

The dashboard's backend is not a package — its modules import each other as
``services.bigquery``, ``routers.v2`` — so a test that exercises them has to put
``dashboard/backend`` on ``sys.path``. Five test modules did that with
``sys.path.insert(0, ...)``, and that is a trap:

``dashboard/backend/main.py`` is the FastAPI app; the repo root's ``main.py`` is
the CLI that owns ``--command sweep``. ``sys.path`` is process-global and
outlives the module that edited it, so an insert at position 0 makes
``import main`` resolve to the **dashboard app** for every test module collected
afterwards — alphabetically, everything from ``test_e*`` onwards. Nothing fails
until some later module actually imports ``main``
(``tests/test_scenario_persist.py`` does), and then it presents as an
``AttributeError`` about a function that plainly exists, in a file that did
nothing wrong.

So: append the backend, and keep the repo root ahead of it. Called at import
time by every dashboard test module, so whichever one runs last still leaves the
path in this order.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "dashboard" / "backend"


def add_dashboard_backend_to_path() -> None:
    """Append ``dashboard/backend``; ensure the repo root precedes it."""
    backend = str(BACKEND)
    root = str(REPO_ROOT)
    if backend not in sys.path:
        sys.path.append(backend)
    # Re-seat the repo root at the front even if it was already present: another
    # module may have inserted the backend ahead of it before we got here.
    while root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
