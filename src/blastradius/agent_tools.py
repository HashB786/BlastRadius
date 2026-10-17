"""The agent's tool layer — a small, explicit allowlist over existing queries.

This module is the reason the investigation agent cannot do anything unsafe:

* The model may only name one of three tools defined here. There is no Cypher
  passthrough: the model never sees the query language and never writes one.
* Every argument is validated against a schema *before* a tool runs; malformed
  or fabricated arguments are rejected with an error the model can read.
* ``service_id`` arguments are resolved against the real graph. An id that does
  not exist produces an honest ``not_found`` record — never invented data.
* A hard budget (``MAX_TOOL_CALLS``) bounds how many tools one investigation
  may run, so an out-of-control model cannot loop against the database.

All functions here delegate the actual graph work to :mod:`blastradius.queries`
(the same code the dashboard tabs use), so agent answers are always grounded in
real Cypher results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import queries
from .db import BlastRadiusDB

MAX_TOOL_CALLS = 4
"""Hard cap on tool executions per investigation (requirement: bounded loop)."""

_MAX_DEPTH = queries.MAX_DEPTH_LIMIT  # 6
_DEFAULT_DEPTH = queries.DEFAULT_DEPTH  # 4


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass
class ToolCall:
    """One requested tool invocation, exactly as the model asked for it."""

    tool: str
    arguments: dict[str, Any]
    round_id: int


@dataclass
class ToolResult:
    """What actually happened for one requested call. ``status`` is honest."""

    tool: str
    arguments: dict[str, Any]
    round_id: int
    status: str  # "ok" | "not_found" | "rejected" | "skipped"
    detail: str  # one-line human summary
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


TOOLS: dict[str, dict[str, Any]] = {
    "impact_analysis": {
        "description": (
            "Find every service affected if a service or datastore fails, with "
            "the dependency path that carries the impact. Args: service_id "
            "(required, string), max_depth (optional int, 1-6, default 4)."
        ),
        "required": ["service_id"],
        "optional_depth": True,
    },
    "ownership_lookup": {
        "description": (
            "Look up the teams that own a service and the engineers on those "
            "teams, with channels to page. Args: service_id (required, string)."
        ),
        "required": ["service_id"],
        "optional_depth": False,
    },
    "incident_history": {
        "description": (
            "List incidents whose root cause was this service (or that affected "
            "it), with severity, status and every impacted service. Args: "
            "service_id (required, string)."
        ),
        "required": ["service_id"],
        "optional_depth": False,
    },
}

TOOL_NAMES = tuple(TOOLS)


def tools_schema() -> list[dict[str, Any]]:
    """The allowlist in Ollama ``/api/chat`` ``tools`` format."""
    out: list[dict[str, Any]] = []
    for name, spec in TOOLS.items():
        props: dict[str, Any] = {
            "service_id": {
                "type": "string",
                "description": "The id of a service or datastore node, e.g. 'payment-service'.",
            }
        }
        required = ["service_id"]
        if spec["optional_depth"]:
            props["max_depth"] = {
                "type": "integer",
                "description": "How many dependency hops to walk, 1-6. Default 4.",
            }
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec["description"],
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required,
                    },
                },
            }
        )
    return out


def system_prompt() -> str:
    return (
        "You are BlastRadius's investigation assistant. A human asks one "
        "operational question about a microservice estate stored in a graph.\n"
        "Your ONLY job is to select the right investigation tools with correct "
        "arguments.\n"
        "You may call these tools and nothing else: "
        + ", ".join(TOOL_NAMES)
        + ".\n"
        "Rules:\n"
        "- Use service ids exactly as given in the paragraphs added to the "
        "prompt extract. When a service id is not provided, pass the words the "
        "human named; unknown ids are reported honestly by the tools.\n"
        "- Use impact_analysis to learn what breaks; ownership_lookup for who "
        "owns the affected services; incident_history for similar past "
        "incidents. Reuse ids you learned from earlier results (e.g. call "
        "ownership_lookup / incident_history on the most critical impacted "
        "service the impact analysis returned).\n"
        "- Make at most 3 tool calls total. Do not call a tool twice with the "
        "same arguments.\n"
        "- Never generate Cypher language, never invent ids, teams, incidents "
        "or severities."
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


class Rejection(Exception):
    """A tool call that failed schema validation, with a model-readable reason."""


def _parse_call(raw: Any, round_id: int) -> ToolCall:
    if not isinstance(raw, dict):
        raise Rejection("tool call must be a JSON object with a 'tool' and 'arguments'.")
    name = raw.get("tool")
    if not isinstance(name, str) or name.strip().lower() != name:
        raise Rejection(f"unknown or malformed tool name: {name!r} (allowed: {list(TOOL_NAMES)}).")
    if name not in TOOLS:
        raise Rejection(f"tool {name!r} is not on the allowlist (allowed: {list(TOOL_NAMES)}); "
                        "arbitrary queries or other tools are refused.")
    args = raw.get("arguments", {})
    if not isinstance(args, dict):
        raise Rejection("'arguments' must be a JSON object.")

    service_id = args.get("service_id")
    if not isinstance(service_id, str) or not service_id.strip():
        raise Rejection("argument 'service_id' is required and must be a non-empty string.")
    clean: dict[str, Any] = {"service_id": service_id.strip()}

    if TOOLS[name]["optional_depth"]:
        depth_value = args.get("max_depth", _DEFAULT_DEPTH)
        if isinstance(depth_value, str) and depth_value.isdigit():
            depth_value = int(depth_value)  # tolerate "4" from text-only models
        if not isinstance(depth_value, int) or isinstance(depth_value, bool) or not (
            1 <= depth_value <= _MAX_DEPTH
        ):
            raise Rejection(
                f"argument 'max_depth' must be an integer in 1..{_MAX_DEPTH} "
                f"(got {depth_value!r})."
            )
        clean["max_depth"] = depth_value

    extra = set(args) - set(clean)
    if extra:
        raise Rejection(f"unsupported argument(s) {sorted(extra)}; allowed: {sorted(clean)}.")
    return ToolCall(tool=name, arguments=clean, round_id=round_id)


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def _impact_result(db: BlastRadiusDB, service_id: str, max_depth: int) -> dict[str, Any]:
    res = queries.impact_analysis(db, service_id, max_depth)
    if res.missing:
        return {
            "status": "not_found",
            "detail": res.message,
        }
    if res.message:
        return {
            "status": "ok",
            "detail": res.message,
        }
    impacted = [
        {
            "service_id": i.service_id,
            "name": i.name,
            "tier": i.tier,
            "hops": i.hops,
            "teams": i.teams,
            "path": i.arrow,
            "customer_facing": (i.tier or 9) <= 2,
        }
        for i in res.impacted
    ]
    detail = (
        f"{len(impacted)} service(s) affected if {res.origin.get('name', service_id)} "
        f"({service_id}) fails; {len(res.teams)} team(s) involved."
    )
    return {
        "status": "ok",
        "detail": detail,
        "evidence": {
            "origin": {"kind": res.origin.get("kind"), "id": service_id, "name": res.origin.get("name")},
            "max_depth": res.max_depth,
            "cypher": res.cypher,
            "impacted": impacted,
            "teams": res.teams,
        },
    }


def _ownership_result(db: BlastRadiusDB, service_id: str) -> dict[str, Any]:
    res = queries.ownership_lookup(db, service_id)
    if res is None:
        return {"status": "not_found", "detail": f"{service_id!r} is not in the graph."}
    detail = (
        f"{service_id} is owned by {[t['name'] for t in res.teams]}, engineers: "
        f"{[e['name'] for e in res.engineers]}."
    )
    owners = {
        "teams": res.teams,
        "engineers": res.engineers,
        "service": res.service,
    }
    return {"status": "ok", "detail": detail, "evidence": owners}


def _incident_result(db: BlastRadiusDB, service_id: str) -> dict[str, Any]:
    res = queries.incident_history(db, service_id)
    if res is None:
        return {"status": "not_found", "detail": f"{service_id!r} is not in the graph."}
    if not res:
        return {
            "status": "ok",
            "detail": f"{service_id} exists, but no recorded incident impacted it.",
        }
    compact = [
        {
            "id": i.incident_id,
            "title": i.title,
            "severity": i.severity,
            "status": i.status,
            "role": i.role,
            "started_at": i.started_at,
            "affected_services": i.affected_services,
            "summary": i.summary,
        }
        for i in res
    ]
    sevs = sorted({i.severity for i in res})
    detail = f"{len(compact)} incident(s) connect to {service_id}; severities: {sevs}."
    return {"status": "ok", "detail": detail, "evidence": {"incidents": compact}}


def execute_tool_call(db: BlastRadiusDB, call: ToolCall) -> ToolResult:
    """Validate one call, run the real query, and wrap result or honest error."""
    try:
        parsed = _parse_call(
            {"tool": call.tool, "arguments": call.arguments}, call.round_id
        )
    except Rejection as exc:
        return ToolResult(
            tool=call.tool,
            arguments=call.arguments,
            round_id=call.round_id,
            status="rejected",
            detail="call rejected by validation",
            error=str(exc),
        )

    args = parsed.arguments
    service_id = args["service_id"]

    if parsed.tool == "impact_analysis":
        outcome = _impact_result(db, service_id, args.get("max_depth", _DEFAULT_DEPTH))
    elif parsed.tool == "ownership_lookup":
        outcome = _ownership_result(db, service_id)
    else:
        outcome = _incident_result(db, service_id)

    status = outcome["status"]
    evidence = outcome.get("evidence", {})
    if status == "not_found" and evidence == {}:
        suggestions = queries.suggest_services(db, service_id)
        if suggestions:
            evidence["suggestions"] = suggestions
        detail_outcome = f"{status}: {outcome['detail']}"
    else:
        detail_outcome = outcome["detail"]
    return ToolResult(
        tool=parsed.tool,
        arguments=args,
        round_id=call.round_id,
        status=status,
        detail=detail_outcome,
        evidence=evidence,
        error=outcome.get("error"),
    )


def execute_plan(
    db: BlastRadiusDB, calls: list[ToolCall], budget: int = MAX_TOOL_CALLS
) -> list[ToolResult]:
    """Run a bounded plan: execute the first ``budget`` calls, skip the rest."""
    results: list[ToolResult] = []
    executed = 0
    for index, call in enumerate(calls):
        if executed >= budget or index >= budget:
            results.append(
                ToolResult(
                    tool=call.tool,
                    arguments=call.arguments,
                    round_id=call.round_id,
                    status="skipped",
                    detail=(
                        f"tool budget of {budget} reached; call #{index + 1} was "
                        "not executed (bounded agent loop)."
                    ),
                    error="budget_exceeded",
                )
            )
            continue
        results.append(execute_tool_call(db, call))
        executed += 1
    return results
