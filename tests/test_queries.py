"""Integration tests: every assertion below is backed by a live Cypher query."""

from __future__ import annotations

import pytest

from blastradius import queries
from blastradius.db import BlastRadiusDB
from blastradius.seed import verify_counts


def test_health_reports_live_server(db: BlastRadiusDB) -> None:
    report = db.health()
    assert report.reachable, report.error
    assert report.latency_ms is not None
    assert report.server_version


def test_seed_counts_match_the_dataset(seeded: BlastRadiusDB) -> None:
    counts = verify_counts(seeded)
    assert counts["Service"] == 40
    assert counts["Team"] == 8
    assert counts["Incident"] == 10
    assert counts["DEPENDS_ON"] == 90
    assert counts["ROOT_CAUSE"] == 10
    assert counts["nodes_total"] > 0


def test_every_service_has_an_owning_team(seeded: BlastRadiusDB) -> None:
    orphans = seeded.run(
        "MATCH (s:Service) WHERE NOT (:Team)-[:OWNS]->(s) RETURN s.id"
    )
    assert orphans == []


def test_impact_returns_known_direct_dependent(seeded: BlastRadiusDB) -> None:
    result = queries.impact_analysis(seeded, "payment-service", max_depth=3)
    ids = {item.service_id: item for item in result.impacted}
    assert "checkout-service" in ids
    assert ids["checkout-service"].hops == 1


def test_impact_paths_are_real_and_correct_length(seeded: BlastRadiusDB) -> None:
    result = queries.impact_analysis(seeded, "payment-service", max_depth=4)
    assert result.impacted

    for item in result.impacted:
        node_ids = [node.get("id") for node in item.path["nodes"]]
        assert node_ids[0] == item.service_id
        assert node_ids[-1] == "payment-service"
        assert len(item.path["edges"]) == len(node_ids) - 1
        assert item.hops == len(item.path["edges"])
        for edge in item.path["edges"]:
            assert edge["_type"] == "DEPENDS_ON"


def test_impact_depth_is_respected_and_monotonic(seeded: BlastRadiusDB) -> None:
    shallow = queries.impact_analysis(seeded, "payment-service", max_depth=1)
    deep = queries.impact_analysis(seeded, "payment-service", max_depth=3)

    assert all(item.hops == 1 for item in shallow.impacted)
    assert all(item.hops <= 3 for item in deep.impacted)
    assert {i.service_id for i in shallow.impacted} <= {i.service_id for i in deep.impacted}


def test_impact_finds_multi_hop_chain(seeded: BlastRadiusDB) -> None:
    result = queries.impact_analysis(seeded, "payment-service", max_depth=4)
    longest = max(item.hops for item in result.impacted)
    assert longest >= 3, "expected a chain of at least three hops in the demo data"
    gateway = next(i for i in result.impacted if i.service_id == "api-gateway")
    assert gateway.hops >= 2
    assert gateway.arrow.endswith("Payment Service")


def test_impact_reports_teams_to_page(seeded: BlastRadiusDB) -> None:
    result = queries.impact_analysis(seeded, "payment-service", max_depth=4)
    team_names = {team["name"] for team in result.teams}
    assert "Payments Platform" in team_names
    assert "Checkout" in team_names
    for team in result.teams:
        assert team["services"]


def test_datastore_failure_traverses_service_dependencies(seeded: BlastRadiusDB) -> None:
    result = queries.impact_analysis(seeded, "redis-cart", max_depth=3)
    assert result.origin is not None
    assert result.origin["kind"] == "Datastore"
    ids = {item.service_id for item in result.impacted}
    assert {"cart-service", "checkout-service"} <= ids
    direct = next(item for item in result.impacted if item.service_id == "cart-service")
    assert direct.path["edges"][-1]["_type"] in {"READS_FROM", "WRITES_TO"}
    assert direct.arrow.endswith("cart-cache")


def test_missing_service_is_reported_not_invented(seeded: BlastRadiusDB) -> None:
    result = queries.impact_analysis(seeded, "does-not-exist", max_depth=3)
    assert result.missing is True
    assert result.origin is None
    assert result.impacted == []
    assert "not in the graph" in (result.message or "")

    suggestions = queries.suggest_services(seeded, "paymnt-service")
    assert "payment-service" in suggestions


def test_ownership_lookup_returns_team_and_engineers(seeded: BlastRadiusDB) -> None:
    result = queries.ownership_lookup(seeded, "ledger-service")
    assert result is not None
    assert result.service["id"] == "ledger-service"
    assert [team["name"] for team in result.teams] == ["Payments Platform"]
    assert result.teams[0]["slack_channel"] == "#payments-oncall"
    assert {engineer["name"] for engineer in result.engineers} >= {
        "Daniel Okafor",
        "Sofia Rossi",
    }


def test_ownership_lookup_missing_service(seeded: BlastRadiusDB) -> None:
    assert queries.ownership_lookup(seeded, "nope") is None


def test_team_ownership_lists_services(seeded: BlastRadiusDB) -> None:
    result = queries.team_ownership(seeded, "team-payments")
    assert result is not None
    owned = {service["id"] for service in result["services"]}
    assert {"payment-service", "ledger-service"} <= owned
    assert result["engineers"]


def test_incident_history_includes_root_cause(seeded: BlastRadiusDB) -> None:
    records = queries.incident_history(seeded, "ledger-service")
    assert records is not None
    by_id = {record.incident_id: record for record in records}
    assert "INC-2029" in by_id
    assert by_id["INC-2029"].role == "root cause"
    assert by_id["INC-2029"].affected_services


def test_incident_history_flags_affected_services(seeded: BlastRadiusDB) -> None:
    records = queries.incident_history(seeded, "payment-service")
    assert records
    by_id = {record.incident_id: record for record in records}
    assert by_id["INC-2038"].role == "affected"
    assert by_id["INC-2029"].role == "affected"


def test_incident_history_for_datastore(seeded: BlastRadiusDB) -> None:
    records = queries.incident_history(seeded, "redis-cart")
    assert records
    assert records[0].incident_id == "INC-2041"
    assert records[0].role == "root cause"


def test_incident_history_missing_entity(seeded: BlastRadiusDB) -> None:
    assert queries.incident_history(seeded, "no-such-thing") is None


def test_incident_detail_links_root_cause_and_downstream(
    seeded: BlastRadiusDB,
) -> None:
    detail = queries.incident_detail(seeded, "INC-2038")
    assert detail is not None
    assert detail["root_cause_id"] == "fraud-service"
    assert detail["root_cause_kind"] == "Service"
    assert "Payment Service" in detail["affected_services"]
    assert detail["downstream"]
    assert all("path" in row for row in detail["downstream"])


def test_incident_detail_handles_datastore_root_cause(seeded: BlastRadiusDB) -> None:
    detail = queries.incident_detail(seeded, "INC-2041")
    assert detail is not None
    assert detail["root_cause_kind"] == "Datastore"
    assert any(row["service"] == "Checkout Service" for row in detail["downstream"])


def test_overview_counts_come_from_the_graph(seeded: BlastRadiusDB) -> None:
    stats = queries.overview(seeded)
    assert stats["services"] == 40
    assert stats["dependencies"] == 90
    assert stats["incidents"] == 10
    assert stats["severity"]["SEV1"] >= 1
    assert stats["top_dependents"][0]["dependents"] >= 1


@pytest.mark.parametrize("depth", [0, -3, 99])
def test_depth_is_clamped_not_trusted(seeded: BlastRadiusDB, depth: int) -> None:
    result = queries.impact_analysis(seeded, "payment-service", max_depth=depth)
    assert 1 <= result.max_depth <= queries.MAX_DEPTH_LIMIT
    assert all(item.hops <= result.max_depth for item in result.impacted)
