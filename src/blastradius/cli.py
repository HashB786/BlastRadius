"""Command line entry point: health, seed, verify and ad-hoc queries.

Examples
--------
    python -m blastradius.cli health
    python -m blastradius.cli seed --reset
    python -m blastradius.cli impact payment-service --depth 4
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from .config import get_config
from .data import build_dataset
from .db import BlastRadiusDB, DatabaseUnavailable
from . import queries
from .seed import seed_graph, verify_counts


def _print_health(db: BlastRadiusDB) -> int:
    report = db.health()
    print(f"target      : {db.config.describe()}")
    if not report.reachable:
        print("status      : UNREACHABLE")
        print(f"error       : {report.error}")
        return 1
    print("status      : live")
    print(f"server      : {report.server_version}")
    print(f"round-trip  : {report.latency_ms} ms")
    print(f"nodes/edges : {report.node_count} / {report.edge_count}")
    return 0


def _print_impact(db: BlastRadiusDB, service_id: str, depth: int, as_json: bool) -> int:
    result = queries.impact_analysis(db, service_id, depth)
    if result.origin is None:
        print(f"ERROR: {result.message}")
        suggestions = queries.suggest_services(db, service_id)
        if suggestions:
            print("Did you mean: " + ", ".join(suggestions))
        return 2

    if as_json:
        print(
            json.dumps(
                {
                    "origin": result.origin,
                    "impacted": [
                        {
                            "id": i.service_id,
                            "name": i.name,
                            "hops": i.hops,
                            "path": i.arrow,
                            "teams": i.teams,
                        }
                        for i in result.impacted
                    ],
                    "max_depth": result.max_depth,
                },
                indent=2,
            )
        )
        return 0

    print(f"Origin: {result.origin['name']} ({result.origin['id']}), depth {depth}")
    if not result.impacted:
        print(result.message)
        return 0
    print(f"Impacted services: {len(result.impacted)}\n")
    for item in result.impacted:
        print(f"  {item.hops} hop(s)  {item.name}  (tier {item.tier})")
        print(f"           {item.arrow}")
    print("\nTeams paged:")
    for team in result.teams:
        print(f"  {team['name']} {team['slack_channel']} | {', '.join(team['services'])}")
    return 0


def _print_ownership(db: BlastRadiusDB, service_id: str) -> int:
    result = queries.ownership_lookup(db, service_id)
    if result is None:
        print(f"ERROR: service {service_id!r} is not in the graph.")
        return 2
    team_names = ", ".join(t["name"] for t in result.teams) or "nobody"
    print(f"{result.service['name']} is owned by {team_names}")
    for team in result.teams:
        print(f"  {team['name']} | {team['slack_channel']} | {team['cost_center']}")
    for engineer in result.engineers:
        print(f"  - {engineer['name']} ({engineer['role']})")
    return 0


def _print_incidents(db: BlastRadiusDB, service_id: str) -> int:
    records = queries.incident_history(db, service_id)
    if records is None:
        print(f"ERROR: service {service_id!r} is not in the graph.")
        return 2
    if not records:
        print(f"No incidents recorded for {service_id}.")
        return 0
    for record in records:
        print(
            f"{record.incident_id} {record.severity} {record.started_at} "
            f"({record.role}, {record.duration_minutes}m) {record.title}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blastradius", description=__doc__)
    parser.add_argument(
        "--graph", help="Override the graph name (defaults to FALKORDB_GRAPH)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="Check the live FalkorDB connection")
    sub.add_parser("dataset", help="Print dataset stats without touching the DB")

    seed_parser = sub.add_parser("seed", help="Write the synthetic graph")
    seed_parser.add_argument(
        "--reset", action="store_true", help="Delete existing graph data first"
    )

    sub.add_parser("verify", help="Print node/edge counts read back from FalkorDB")

    impact_parser = sub.add_parser("impact", help="Multi-hop impact analysis")
    impact_parser.add_argument("service_id")
    impact_parser.add_argument("--depth", type=int, default=4)
    impact_parser.add_argument("--json", action="store_true")

    owners_parser = sub.add_parser("owners", help="Ownership lookup")
    owners_parser.add_argument("service_id")

    incidents_parser = sub.add_parser("incidents", help="Incident history")
    incidents_parser.add_argument("service_id")

    sub.add_parser("overview", help="Graph statistics")

    args = parser.parse_args(argv)

    if args.command == "dataset":
        data = build_dataset()
        print(json.dumps(data.counts(), indent=2))
        return 0

    config = get_config()
    if args.graph:
        config = config.with_graph(args.graph)
    db = BlastRadiusDB(config)

    try:
        if args.command == "health":
            return _print_health(db)
        if args.command == "seed":
            counts = seed_graph(db, reset=args.reset)
            print(json.dumps(counts, indent=2))
            return 0
        if args.command == "verify":
            print(json.dumps(verify_counts(db), indent=2))
            return 0
        if args.command == "impact":
            return _print_impact(db, args.service_id, args.depth, args.json)
        if args.command == "owners":
            return _print_ownership(db, args.service_id)
        if args.command == "incidents":
            return _print_incidents(db, args.service_id)
        if args.command == "overview":
            print(json.dumps(queries.overview(db), indent=2))
            return 0
    except DatabaseUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown command {args.command}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
