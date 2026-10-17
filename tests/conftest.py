"""Shared pytest fixtures.

The tests run against a **real** FalkorDB server, using a separate graph
(``blastradius_test``) so the demo graph is never touched. There is no mock or
in-memory substitute: if the server is down the suite fails with an actionable
message, because a green run must mean the database really works.
"""

from __future__ import annotations

import pytest

from blastradius.config import get_config
from blastradius.db import BlastRadiusDB, DatabaseUnavailable
from blastradius.seed import seed_graph

TEST_GRAPH = "blastradius_test"


@pytest.fixture(scope="session")
def db() -> BlastRadiusDB:
    config = get_config().with_graph(TEST_GRAPH)
    database = BlastRadiusDB(config)
    try:
        database.connect()
    except DatabaseUnavailable as exc:  # pragma: no cover - environment failure
        pytest.fail(
            "FalkorDB is not reachable, so the integration tests cannot run.\n"
            f"{exc}\n"
            "Start it with: docker start blastradius-falkordb"
        )
    yield database
    # Leave the test graph empty; the demo graph keeps its data.
    database.clear_graph()
    database.close()


@pytest.fixture(scope="session")
def seeded(db: BlastRadiusDB) -> BlastRadiusDB:
    """The test graph, fully seeded and verified."""
    seed_graph(db, reset=True)
    return db
