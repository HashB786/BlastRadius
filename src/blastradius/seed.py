"""Write the synthetic dataset into FalkorDB using real Cypher.

Seeding is idempotent: every write is a ``MERGE``, so running it twice does not
duplicate nodes. ``--reset`` (used by the CLI) clears the graph first.
"""

from __future__ import annotations

from typing import Any

from .data import Dataset, build_dataset
from .db import BlastRadiusDB

INDEX_STATEMENTS = [
    "CREATE INDEX FOR (s:Service) ON (s.id)",
    "CREATE INDEX FOR (s:Service) ON (s.name)",
    "CREATE INDEX FOR (t:Team) ON (t.id)",
    "CREATE INDEX FOR (e:Engineer) ON (e.id)",
    "CREATE INDEX FOR (i:Incident) ON (i.id)",
    "CREATE INDEX FOR (d:Datastore) ON (d.id)",
    "CREATE INDEX FOR (t:Topic) ON (t.id)",
    "CREATE INDEX FOR (r:Region) ON (r.id)",
]


def existing_indexes(db: BlastRadiusDB) -> set[tuple[str, str]]:
    """``(label, property)`` pairs that FalkorDB reports as already indexed."""
    pairs: set[tuple[str, str]] = set()
    for row in db.run("CALL db.indexes()"):
        label = str(row[0])
        for prop in row[1] or []:
            pairs.add((label, str(prop)))
    return pairs


def create_indexes(db: BlastRadiusDB) -> list[str]:
    """Create the missing label+property indexes; return the statements run.

    FalkorDB accepts the modern ``CREATE INDEX FOR (n:Label) ON (n.prop)``
    form. Existing indexes are skipped (creating one twice is an error), and a
    legacy-syntax fallback covers older images. If both forms fail the error
    surfaces instead of being silently ignored.
    """
    present = existing_indexes(db)
    created: list[str] = []
    failures: list[str] = []
    for statement in INDEX_STATEMENTS:
        label = statement.split("(")[1].split(":")[1].split(")")[0]
        prop = statement.rsplit(".", 1)[1].split(")")[0]
        if (label, prop) in present:
            continue
        try:
            db.run(statement)
            created.append(statement)
        except Exception:
            fallback = f"CREATE INDEX ON :{label}({prop})"
            try:
                db.run(fallback)
                created.append(fallback)
            except Exception:
                failures.append(statement)
    if failures:
        raise RuntimeError(
            "Could not create indexes with either Cypher syntax: " + ", ".join(failures)
        )
    return created


_PROPERTY_SETS: dict[str, dict[str, list[str]]] = {
    "Team": {
        "match": ["id"],
        "props": ["id", "name", "cost_center", "slack_channel", "timezone"],
    },
    "Engineer": {"match": ["id"], "props": ["id", "name", "role"]},
    "Region": {"match": ["id"], "props": ["id", "name", "region", "provider"]},
    "Service": {
        "match": ["id"],
        "props": [
            "id",
            "name",
            "tier",
            "language",
            "repo",
            "summary",
            "slo_target",
            "error_budget_minutes",
            "region",
        ],
    },
    "Datastore": {
        "match": ["id"],
        "props": ["id", "name", "engine", "region", "managed"],
    },
    "Topic": {"match": ["id"], "props": ["id", "name", "broker", "partitions"]},
    "Incident": {
        "match": ["id"],
        "props": [
            "id",
            "title",
            "severity",
            "status",
            "started_at",
            "resolved_at",
            "duration_minutes",
            "summary",
            "detected_by",
        ],
    },
}


def _merge_nodes(
    db: BlastRadiusDB,
    label: str,
    rows: list[dict[str, Any]],
    *,
    batch_size: int = 200,
) -> None:
    """MERGE nodes of one label, setting an explicit property list."""
    spec = _PROPERTY_SETS[label]
    match_key = spec["match"][0]
    assignments = ", ".join(f"n.{prop} = row.{prop}" for prop in spec["props"])
    cypher = (
        f"UNWIND $rows AS row MERGE (n:{label} {{{match_key}: row.{match_key}}}) "
        f"SET {assignments} RETURN count(n)"
    )
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        db.run(cypher, {"rows": batch})


def seed_graph(
    db: BlastRadiusDB, dataset: Dataset | None = None, *, reset: bool = True
) -> dict[str, int]:
    """Create the whole graph and return the counts read back from FalkorDB."""
    data = dataset or build_dataset()

    if reset:
        db.clear_graph()

    create_indexes(db)

    _merge_nodes(db, "Team", data.teams)
    _merge_nodes(db, "Engineer", data.engineers)
    _merge_nodes(db, "Region", data.regions)
    _merge_nodes(db, "Datastore", data.datastores)
    _merge_nodes(db, "Topic", data.topics)

    services = [{k: v for k, v in s.items() if k != "team_id"} for s in data.services]
    _merge_nodes(db, "Service", services)

    _merge_nodes(db, "Incident", data.incidents)

    # Ownership: (Team)-[:OWNS]->(Service)
    db.run(
        "UNWIND $rows AS row "
        "MATCH (t:Team {id: row.team_id}) MATCH (s:Service {id: row.id}) "
        "MERGE (t)-[:OWNS]->(s)",
        {"rows": [{"id": s["id"], "team_id": s["team_id"]} for s in data.services]},
    )

    # Team membership: (Engineer)-[:MEMBER_OF]->(Team)
    db.run(
        "UNWIND $rows AS row "
        "MATCH (e:Engineer {id: row.id}) MATCH (t:Team {id: row.team}) "
        "MERGE (e)-[:MEMBER_OF]->(t)",
        {"rows": [{"id": e["id"], "team": e["team"]} for e in data.engineers]},
    )

    # Placement: (Service)-[:HOSTED_IN]->(Region)
    db.run(
        "UNWIND $rows AS row "
        "MATCH (s:Service {id: row.id}) MATCH (r:Region {id: row.region}) "
        "MERGE (s)-[:HOSTED_IN]->(r)",
        {"rows": [{"id": s["id"], "region": s["region"]} for s in data.services]},
    )

    # Dependencies: (consumer)-[:DEPENDS_ON]->(provider)
    db.run(
        "UNWIND $rows AS row "
        "MATCH (a:Service {id: row.source}) MATCH (b:Service {id: row.target}) "
        "MERGE (a)-[r:DEPENDS_ON]->(b) "
        "SET r.protocol = row.protocol, r.criticality = row.criticality, "
        "r.calls_per_min = row.calls_per_min",
        {"rows": data.dependencies},
    )

    # Data access split into two edge types so traversals can filter on intent.
    read_write = [r for r in data.datastore_access if r["mode"] != "READ"]
    read_only = [r for r in data.datastore_access if r["mode"] == "READ"]
    for edge_type, rows in (("WRITES_TO", read_write), ("READS_FROM", read_only)):
        db.run(
            f"UNWIND $rows AS row "
            f"MATCH (s:Service {{id: row.service}}) "
            f"MATCH (d:Datastore {{id: row.datastore}}) "
            f"MERGE (s)-[r:{edge_type}]->(d) SET r.mode = row.mode",
            {"rows": rows},
        )

    publishes = [r for r in data.topic_links if r["direction"] == "PUBLISHES_TO"]
    subscribes = [r for r in data.topic_links if r["direction"] == "SUBSCRIBES_TO"]
    for edge_type, rows in (("PUBLISHES_TO", publishes), ("SUBSCRIBES_TO", subscribes)):
        db.run(
            f"UNWIND $rows AS row "
            f"MATCH (s:Service {{id: row.service}}) MATCH (t:Topic {{id: row.topic}}) "
            f"MERGE (s)-[:{edge_type}]->(t)",
            {"rows": rows},
        )

    # Incident provenance and blast radius.
    db.run(
        "UNWIND $rows AS row "
        "MATCH (i:Incident {id: row.id}) MATCH (s) WHERE s.id = row.root_cause "
        "MERGE (i)-[:ROOT_CAUSE]->(s)",
        {"rows": [{"id": i["id"], "root_cause": i["root_cause"]} for i in data.incidents]},
    )
    affected_rows = [
        {
            "id": incident["id"],
            "service": service_id,
            "role": "origin"
            if service_id == incident["root_cause"]
            else "downstream",
        }
        for incident in data.incidents
        for service_id in incident["affected"]
    ]
    db.run(
        "UNWIND $rows AS row "
        "MATCH (i:Incident {id: row.id}) MATCH (s) WHERE s.id = row.service "
        "MERGE (i)-[r:IMPACTED]->(s) SET r.role = row.role",
        {"rows": affected_rows},
    )

    return verify_counts(db)


def verify_counts(db: BlastRadiusDB) -> dict[str, int]:
    """Counts read back from the database (not from the generator)."""
    labels = ["Service", "Team", "Engineer", "Datastore", "Topic", "Incident", "Region"]
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = int(db.scalar(f"MATCH (n:{label}) RETURN count(n)") or 0)
    rels = ["DEPENDS_ON", "OWNS", "MEMBER_OF", "IMPACTED", "ROOT_CAUSE"]
    for rel in rels:
        counts[rel] = int(db.scalar(f"MATCH ()-[r:{rel}]->() RETURN count(r)") or 0)
    counts["nodes_total"] = int(db.scalar("MATCH (n) RETURN count(n)") or 0)
    counts["edges_total"] = int(db.scalar("MATCH ()-[r]->() RETURN count(r)") or 0)
    return counts
