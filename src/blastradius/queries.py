"""The Cypher queries that power BlastRadius.

Every function here sends real Cypher to FalkorDB and returns plain Python
data. Nothing is cached, sampled or invented: if a service is not in the graph
the function says so instead of guessing.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from .db import BlastRadiusDB, path_to_arrow, path_to_dict

MAX_DEPTH_LIMIT = 6
DEFAULT_DEPTH = 4


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass
class PathStep:
    """One node on a dependency path, with the edge that leads to it."""

    service_id: str
    name: str
    tier: int | None = None
    relation: str | None = None
    criticality: str | None = None
    protocol: str | None = None


@dataclass
class ImpactedService:
    service_id: str
    name: str
    tier: int | None
    hops: int
    path: dict[str, Any]
    arrow: str
    steps: list[PathStep] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)

    @property
    def via(self) -> str:
        """The first hop: who directly depends on the failing service."""
        if len(self.steps) >= 2:
            return self.steps[-1].name
        return ""


@dataclass
class ImpactResult:
    origin: dict[str, Any] | None
    impacted: list[ImpactedService]
    teams: list[dict[str, Any]]
    max_depth: int
    cypher: str
    params: dict[str, Any]
    missing: bool = False
    message: str | None = None

    @property
    def direct(self) -> list[ImpactedService]:
        return [i for i in self.impacted if i.hops == 1]

    @property
    def customers_facing(self) -> list[ImpactedService]:
        return [i for i in self.impacted if (i.tier or 9) <= 2]

    def by_hops(self) -> dict[int, list[ImpactedService]]:
        grouped: dict[int, list[ImpactedService]] = {}
        for item in self.impacted:
            grouped.setdefault(item.hops, []).append(item)
        return dict(sorted(grouped.items()))


@dataclass
class IncidentRecord:
    incident_id: str
    title: str
    severity: str
    status: str
    started_at: str
    duration_minutes: int | None
    summary: str
    role: str  # "root cause" or "affected"
    affected_services: list[str] = field(default_factory=list)


@dataclass
class OwnershipResult:
    service: dict[str, Any]
    teams: list[dict[str, Any]]
    engineers: list[dict[str, Any]]


# --------------------------------------------------------------------------- #
# Service lookup
# --------------------------------------------------------------------------- #


_SERVICE_COLUMNS = [
    "id",
    "name",
    "tier",
    "language",
    "repo",
    "summary",
    "slo_target",
    "error_budget_minutes",
    "region",
]

_SERVICE_RETURN = (
    "s.id, s.name, s.tier, s.language, s.repo, s.summary, "
    "s.slo_target, s.error_budget_minutes, s.region"
)


def list_services(db: BlastRadiusDB) -> list[dict[str, Any]]:
    """All services, ordered by tier then name (used by the pickers)."""
    rows = db.run(
        f"MATCH (s:Service) RETURN {_SERVICE_RETURN} ORDER BY s.tier, s.name"
    )
    return [dict(zip(_SERVICE_COLUMNS, row)) for row in rows]


def get_service(db: BlastRadiusDB, service_id: str) -> dict[str, Any] | None:
    """Fetch one service by id. Returns None when it is not in the graph."""
    rows = db.run(
        f"MATCH (s:Service {{id: $id}}) RETURN {_SERVICE_RETURN} LIMIT 1",
        {"id": service_id},
    )
    if not rows:
        return None
    return dict(zip(_SERVICE_COLUMNS, rows[0]))


def get_entity(db: BlastRadiusDB, entity_id: str) -> dict[str, Any] | None:
    """Fetch any graph entity by id, whatever its label.

    BlastRadius treats services and datastores as possible failure origins, so
    callers need to know *what* they found rather than assuming a ``Service``.
    """
    rows = db.run(
        "MATCH (n {id: $id}) RETURN labels(n), properties(n) LIMIT 1",
        {"id": entity_id},
    )
    if not rows:
        return None
    labels, props = rows[0]
    entity = dict(props or {})
    entity["kind"] = str(labels[0]) if labels else "Unknown"
    return entity


def resolve_service(db: BlastRadiusDB, text: str) -> dict[str, Any] | None:
    """Find a service by exact id, then by case-insensitive id/name."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    exact = get_service(db, cleaned)
    if exact:
        return exact
    rows = db.run(
        f"MATCH (s:Service) WHERE toLower(s.id) = toLower($q) "
        f"OR toLower(s.name) = toLower($q) RETURN {_SERVICE_RETURN} LIMIT 1",
        {"q": cleaned},
    )
    if not rows:
        return None
    return dict(zip(_SERVICE_COLUMNS, rows[0]))


def suggest_services(db: BlastRadiusDB, text: str, limit: int = 5) -> list[str]:
    """Close matches for a typo, so a wrong id can be corrected quickly."""
    cleaned = (text or "").strip()
    services = list_services(db)
    if not cleaned:
        return [s["id"] for s in services[:limit]]

    contains = db.run(
        "MATCH (s:Service) WHERE toLower(s.id) CONTAINS toLower($q) "
        "OR toLower(s.name) CONTAINS toLower($q) "
        "RETURN s.id ORDER BY s.id LIMIT $limit",
        {"q": cleaned, "limit": limit},
    )
    found = [row[0] for row in contains]
    if found:
        return found

    known = [s["id"] for s in services]
    return difflib.get_close_matches(cleaned.lower(), known, n=limit, cutoff=0.4)


# --------------------------------------------------------------------------- #
# 1. Multi-hop impact analysis
# --------------------------------------------------------------------------- #


def impact_analysis(
    db: BlastRadiusDB,
    service_id: str,
    max_depth: int = DEFAULT_DEPTH,
) -> ImpactResult:
    """Find every service that depends on ``service_id`` within ``max_depth`` hops.

    The traversal follows ``DEPENDS_ON`` edges *backwards*: a service depends on
    the one it points at, so a failure moves upstream along the arrow.
    """
    depth = max(1, min(int(max_depth), MAX_DEPTH_LIMIT))
    origin = get_entity(db, service_id)

    if origin is None:
        return ImpactResult(
            origin=None,
            impacted=[],
            teams=[],
            max_depth=depth,
            cypher="",
            params={"service_id": service_id},
            missing=True,
            message=(
                f"{service_id!r} is not in the graph, so no impact can be "
                "computed. Nothing was assumed or substituted."
            ),
        )

    kind = origin["kind"]
    if kind == "Service":
        # A service's blast radius is everything that depends on it, directly
        # or transitively: follow DEPENDS_ON backwards from the origin.
        cypher = (
            "MATCH p = (dependent:Service)-[:DEPENDS_ON*1..{depth}]->"
            "(origin:Service {{id: $service_id}})\n"
            "WHERE dependent.id <> origin.id\n"
            "RETURN dependent.id, dependent.name, dependent.tier, length(p) AS hops, p\n"
            "ORDER BY hops ASC, dependent.name ASC"
        ).format(depth=depth)
    elif kind == "Datastore":
        # A datastore fails every service that reads or writes it, and then
        # everything upstream of those services. The path keeps the real
        # READS_FROM / WRITES_TO edge as its final hop.
        cypher = (
            "MATCH p = (dependent:Service)-[:DEPENDS_ON*0..{depth}]->(consumer:Service)"
            "-[:READS_FROM|WRITES_TO]->(origin:Datastore {{id: $service_id}})\n"
            "RETURN dependent.id, dependent.name, dependent.tier, length(p) AS hops, p\n"
            "ORDER BY hops ASC, dependent.name ASC"
        ).format(depth=depth)
    else:
        return ImpactResult(
            origin=origin,
            impacted=[],
            teams=[],
            max_depth=depth,
            cypher="",
            params={"service_id": service_id},
            message=(
                f"{origin.get('name', service_id)} is a {kind}, which has no "
                "dependency blast radius in this graph."
            ),
        )

    rows = db.run(cypher, {"service_id": service_id})

    # One row per distinct path; keep the shortest path for each service.
    best: dict[str, tuple[int, Any]] = {}
    for row in rows:
        dep_id, _name, _tier, hops, path = row
        if dep_id not in best or hops < best[dep_id][0]:
            best[dep_id] = (int(hops), path)

    if not best:
        return ImpactResult(
            origin=origin,
            impacted=[],
            teams=[],
            max_depth=depth,
            cypher=cypher,
            params={"service_id": service_id},
            message=(
                f"Nothing depends on {origin.get('name')} within {depth} hops. "
                "It is a leaf of the dependency graph."
            ),
        )

    team_rows = db.run(
        "MATCH (t:Team)-[:OWNS]->(s:Service) "
        "WHERE s.id IN $ids RETURN s.id, t.name ORDER BY t.name",
        {"ids": list(best.keys())},
    )
    teams_by_service: dict[str, list[str]] = {}
    for svc_id, team_name in team_rows:
        teams_by_service.setdefault(svc_id, []).append(team_name)

    impacted: list[ImpactedService] = []
    for dep_id, (hops, path) in best.items():
        data = path_to_dict(path)
        steps: list[PathStep] = []
        nodes = data["nodes"]
        edges = data["edges"]
        for index, node in enumerate(nodes):
            edge = edges[index] if index < len(edges) else {}
            steps.append(
                PathStep(
                    service_id=str(node.get("id", "")),
                    name=str(node.get("name", "")),
                    tier=node.get("tier"),
                    relation=edge.get("_type"),
                    criticality=edge.get("criticality"),
                    protocol=edge.get("protocol"),
                )
            )
        impacted.append(
            ImpactedService(
                service_id=str(dep_id),
                name=str(nodes[0].get("name", dep_id)),
                tier=nodes[0].get("tier"),
                hops=hops,
                path=data,
                arrow=path_to_arrow(path),
                steps=steps,
                teams=teams_by_service.get(str(dep_id), []),
            )
        )

    impacted.sort(key=lambda i: (i.hops, i.tier or 9, i.name))

    team_summary = db.run(
        "MATCH (t:Team)-[:OWNS]->(s:Service) WHERE s.id IN $ids "
        "OPTIONAL MATCH (e:Engineer)-[:MEMBER_OF]->(t) "
        "RETURN t.id, t.name, t.slack_channel, collect(DISTINCT s.name), collect(DISTINCT e.name) "
        "ORDER BY t.name",
        {"ids": [i.service_id for i in impacted]},
    )
    teams = [
        {
            "id": row[0],
            "name": row[1],
            "slack_channel": row[2],
            "services": sorted(v for v in row[3] if v),
            "engineers": sorted(v for v in row[4] if v),
        }
        for row in team_summary
    ]

    return ImpactResult(
        origin=origin,
        impacted=impacted,
        teams=teams,
        max_depth=depth,
        cypher=cypher,
        params={"service_id": service_id},
    )


# --------------------------------------------------------------------------- #
# 2. Ownership lookup
# --------------------------------------------------------------------------- #


def ownership_lookup(db: BlastRadiusDB, service_id: str) -> OwnershipResult | None:
    """Who owns this service: team metadata plus the engineers on that team."""
    service = get_service(db, service_id)
    if service is None:
        return None

    team_rows = db.run(
        "MATCH (t:Team)-[:OWNS]->(s:Service {id: $id}) "
        "RETURN t.id, t.name, t.cost_center, t.slack_channel, t.timezone ORDER BY t.name",
        {"id": service_id},
    )
    teams = [
        {
            "id": row[0],
            "name": row[1],
            "cost_center": row[2],
            "slack_channel": row[3],
            "timezone": row[4],
        }
        for row in team_rows
    ]

    engineer_rows = db.run(
        "MATCH (t:Team)-[:OWNS]->(:Service {id: $id}) "
        "MATCH (e:Engineer)-[:MEMBER_OF]->(t) "
        "RETURN e.id, e.name, e.role, t.name ORDER BY e.name",
        {"id": service_id},
    )
    engineers = [
        {"id": row[0], "name": row[1], "role": row[2], "team": row[3]}
        for row in engineer_rows
    ]
    return OwnershipResult(service=service, teams=teams, engineers=engineers)


def team_ownership(db: BlastRadiusDB, team_id: str) -> dict[str, Any] | None:
    """Everything one team owns, with the engineer roster and recent incidents."""
    rows = db.run(
        "MATCH (t:Team {id: $id}) RETURN t.id, t.name, t.cost_center, "
        "t.slack_channel, t.timezone LIMIT 1",
        {"id": team_id},
    )
    if not rows:
        return None
    team = dict(zip(["id", "name", "cost_center", "slack_channel", "timezone"], rows[0]))

    services = db.run(
        "MATCH (t:Team {id: $id})-[:OWNS]->(s:Service) "
        "RETURN s.id, s.name, s.tier, s.language ORDER BY s.tier, s.name",
        {"id": team_id},
    )
    engineers = db.run(
        "MATCH (e:Engineer)-[:MEMBER_OF]->(t:Team {id: $id}) "
        "RETURN e.id, e.name, e.role ORDER BY e.name",
        {"id": team_id},
    )
    incidents = db.run(
        "MATCH (i:Incident)-[:IMPACTED]->(s:Service)<-[:OWNS]-(t:Team {id: $id}) "
        "RETURN DISTINCT i.id, i.title, i.severity, i.started_at "
        "ORDER BY i.started_at DESC",
        {"id": team_id},
    )
    return {
        "team": team,
        "services": [
            {"id": r[0], "name": r[1], "tier": r[2], "language": r[3]} for r in services
        ],
        "engineers": [{"id": r[0], "name": r[1], "role": r[2]} for r in engineers],
        "incidents": [
            {"id": r[0], "title": r[1], "severity": r[2], "started_at": r[3]}
            for r in incidents
        ],
    }


def list_teams(db: BlastRadiusDB) -> list[dict[str, Any]]:
    rows = db.run(
        "MATCH (t:Team) OPTIONAL MATCH (t)-[:OWNS]->(s:Service) "
        "RETURN t.id, t.name, t.slack_channel, count(s) AS services ORDER BY t.name"
    )
    return [
        {"id": r[0], "name": r[1], "slack_channel": r[2], "services": r[3]}
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# 3. Incident history
# --------------------------------------------------------------------------- #


def incident_history(
    db: BlastRadiusDB, service_id: str, limit: int = 50
) -> list[IncidentRecord] | None:
    """Incidents caused by, or known to affect, an entity. None = unknown id."""
    entity = get_entity(db, service_id)
    if entity is None:
        return None

    rows = db.run(
        "MATCH (i:Incident)-[:ROOT_CAUSE]->(s {id: $id}) "
        "RETURN i.id AS id, i.title AS title, i.severity AS severity, "
        "i.status AS status, i.started_at AS started_at, "
        "i.duration_minutes AS duration_minutes, i.summary AS summary, "
        "'root cause' AS role "
        "UNION ALL "
        "MATCH (i:Incident)-[:IMPACTED]->(s {id: $id}) "
        "RETURN i.id AS id, i.title AS title, i.severity AS severity, "
        "i.status AS status, i.started_at AS started_at, "
        "i.duration_minutes AS duration_minutes, i.summary AS summary, "
        "'affected' AS role",
        {"id": service_id},
    )

    merged: dict[str, IncidentRecord] = {}
    for row in rows:
        inc_id = str(row[0])
        role = str(row[7])
        existing = merged.get(inc_id)
        if existing is not None:
            if role == "root cause":
                existing.role = "root cause"
            continue
        merged[inc_id] = IncidentRecord(
            incident_id=inc_id,
            title=str(row[1]),
            severity=str(row[2]),
            status=str(row[3]),
            started_at=str(row[4]),
            duration_minutes=row[5],
            summary=str(row[6]),
            role=role,
        )

    records = sorted(merged.values(), key=lambda r: r.started_at, reverse=True)[:limit]

    if records:
        detail = db.run(
            "UNWIND $ids AS inc_id "
            "MATCH (i:Incident {id: inc_id})-[:IMPACTED]->(s:Service) "
            "RETURN i.id, collect(DISTINCT s.name)",
            {"ids": [r.incident_id for r in records]},
        )
        affected = {row[0]: sorted(row[1]) for row in detail}
        for record in records:
            record.affected_services = affected.get(record.incident_id, [])

    return records


def list_incidents(db: BlastRadiusDB, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.run(
        "MATCH (i:Incident) OPTIONAL MATCH (i)-[:ROOT_CAUSE]->(s) "
        "OPTIONAL MATCH (i)-[:IMPACTED]->(a:Service) "
        "RETURN i.id, i.title, i.severity, i.started_at, i.duration_minutes, "
        "s.name, count(DISTINCT a) "
        "ORDER BY i.started_at DESC LIMIT $limit",
        {"limit": limit},
    )
    return [
        {
            "id": r[0],
            "title": r[1],
            "severity": r[2],
            "started_at": r[3],
            "duration_minutes": r[4],
            "root_cause": r[5],
            "affected_count": r[6],
        }
        for r in rows
    ]


def incident_detail(db: BlastRadiusDB, incident_id: str) -> dict[str, Any] | None:
    """One incident with its root cause, affected services and downstream paths."""
    rows = db.run(
        "MATCH (i:Incident {id: $id}) "
        "OPTIONAL MATCH (i)-[:ROOT_CAUSE]->(root) "
        "OPTIONAL MATCH (i)-[:IMPACTED]->(s:Service) "
        "RETURN i.id, i.title, i.severity, i.status, i.started_at, i.resolved_at, "
        "i.duration_minutes, i.summary, i.detected_by, root.id, root.name, "
        "labels(root), collect(DISTINCT s.name) LIMIT 1",
        {"id": incident_id},
    )
    if not rows:
        return None
    keys = [
        "id",
        "title",
        "severity",
        "status",
        "started_at",
        "resolved_at",
        "duration_minutes",
        "summary",
        "detected_by",
        "root_cause_id",
        "root_cause_name",
        "root_cause_labels",
        "affected_services",
    ]
    record = dict(zip(keys, rows[0]))
    record["affected_services"] = sorted(v for v in record["affected_services"] if v)
    labels = record.pop("root_cause_labels") or []
    record["root_cause_kind"] = str(labels[0]) if labels else None

    # Reuse the impact query so the downstream paths are the same real paths
    # the Blast Radius tab shows, not a second, subtly different traversal.
    if record["root_cause_id"]:
        impact = impact_analysis(db, record["root_cause_id"], 4)
        record["downstream"] = [
            {"service": item.name, "hops": item.hops, "path": item.arrow}
            for item in impact.impacted[:25]
        ]
    else:
        record["downstream"] = []
    return record


# --------------------------------------------------------------------------- #
# 4. Dashboard overview
# --------------------------------------------------------------------------- #


def overview(db: BlastRadiusDB) -> dict[str, Any]:
    """Counts and rankings read straight from the graph."""
    counts = db.run(
        "MATCH (s:Service) WITH count(s) AS services "
        "MATCH (t:Team) WITH services, count(t) AS teams "
        "MATCH (e:Engineer) WITH services, teams, count(e) AS engineers "
        "MATCH (i:Incident) WITH services, teams, engineers, count(i) AS incidents "
        "MATCH ()-[r:DEPENDS_ON]->() "
        "RETURN services, teams, engineers, incidents, count(r)"
    )[0]

    fan_in = db.run(
        "MATCH (d:Service)-[:DEPENDS_ON]->(s:Service) "
        "RETURN s.name, s.tier, count(d) AS dependents "
        "ORDER BY dependents DESC, s.name LIMIT 8"
    )
    fan_out = db.run(
        "MATCH (s:Service)-[:DEPENDS_ON]->(d:Service) "
        "RETURN s.name, count(d) AS dependencies "
        "ORDER BY dependencies DESC, s.name LIMIT 8"
    )
    severity = db.run(
        "MATCH (i:Incident) RETURN i.severity, count(i) ORDER BY i.severity"
    )
    tiers = db.run("MATCH (s:Service) RETURN s.tier, count(s) ORDER BY s.tier")

    return {
        "services": counts[0],
        "teams": counts[1],
        "engineers": counts[2],
        "incidents": counts[3],
        "dependencies": counts[4],
        "top_dependents": [
            {"name": r[0], "tier": r[1], "dependents": r[2]} for r in fan_in
        ],
        "top_dependencies": [{"name": r[0], "dependencies": r[1]} for r in fan_out],
        "severity": {str(r[0]): r[1] for r in severity},
        "tiers": {f"Tier {r[0]}": r[1] for r in tiers},
    }
