"""Tests for the synthetic dataset that do not need a database."""

from __future__ import annotations

from blastradius.data import build_dataset


def test_dataset_is_deterministic() -> None:
    assert build_dataset() == build_dataset()


def test_expected_shape() -> None:
    data = build_dataset()
    counts = data.counts()
    assert counts["services"] >= 30
    assert counts["teams"] >= 6
    assert counts["incidents"] >= 5
    assert counts["dependencies"] >= 50


def test_ids_are_unique() -> None:
    data = build_dataset()
    for collection in ("services", "teams", "engineers", "incidents"):
        ids = [row["id"] for row in getattr(data, collection)]
        assert len(ids) == len(set(ids)), f"duplicate ids in {collection}"


def test_every_dependency_endpoint_exists() -> None:
    data = build_dataset()
    known = set(data.service_ids())
    for edge in data.dependencies:
        assert edge["source"] in known, f"unknown source {edge['source']}"
        assert edge["target"] in known, f"unknown target {edge['target']}"
        assert edge["source"] != edge["target"]


def test_dependency_graph_is_acyclic() -> None:
    data = build_dataset()
    graph: dict[str, list[str]] = {}
    for edge in data.dependencies:
        graph.setdefault(edge["source"], []).append(edge["target"])

    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        assert node not in visiting, f"cycle through {node}"
        if node in done:
            return
        visiting.add(node)
        for neighbour in graph.get(node, []):
            visit(neighbour)
        visiting.remove(node)
        done.add(node)

    for service_id in data.service_ids():
        visit(service_id)


def test_every_service_has_an_owner() -> None:
    data = build_dataset()
    team_ids = {team["id"] for team in data.teams}
    for service in data.services:
        assert service["team_id"] in team_ids


def test_every_engineer_belongs_to_a_team() -> None:
    data = build_dataset()
    team_ids = {team["id"] for team in data.teams}
    for engineer in data.engineers:
        assert engineer["team"] in team_ids


def test_incidents_reference_real_entities() -> None:
    data = build_dataset()
    known = set(data.service_ids()) | {d["id"] for d in data.datastores}
    for incident in data.incidents:
        assert incident["root_cause"] in known
        assert incident["affected"], "an incident must list affected services"
        for service_id in incident["affected"]:
            assert service_id in known
        assert incident["severity"] in {"SEV1", "SEV2", "SEV3"}


def test_datastore_access_references_real_entities() -> None:
    data = build_dataset()
    services = set(data.service_ids())
    stores = {store["id"] for store in data.datastores}
    for access in data.datastore_access:
        assert access["service"] in services
        assert access["datastore"] in stores


def test_topic_links_reference_real_entities() -> None:
    data = build_dataset()
    services = set(data.service_ids())
    topics = {topic["id"] for topic in data.topics}
    for link in data.topic_links:
        assert link["service"] in services
        assert link["topic"] in topics
        assert link["direction"] in {"PUBLISHES_TO", "SUBSCRIBES_TO"}
