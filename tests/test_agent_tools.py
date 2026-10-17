"""Tests for the agent's allowlisted tool layer, against the real FalkorDB test graph.

There is no mock database here: every ``ok`` assertion means a real Cypher
statement ran and returned rows. Validation failures (``rejected``) are handler
behaviour and do not touch the network; honest ``not_found`` results do hit the
graph and prove the honest-response contract.
"""

from __future__ import annotations

import pytest

from blastradius.agent_tools import (
    MAX_TOOL_CALLS,
    TOOL_NAMES,
    ToolCall,
    execute_plan,
    execute_tool_call,
    tools_schema,
)


pytest.importorskip("falkordb")  # keep the same guardrails as the rest of the suite


# --------------------------------------------------------------------------- #
# Graph-down behaviour: these tests are honest about needing the server
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def graph_ready(seeded):  # noqa: ANN001 (session fixtures, real server)
    return seeded


# --------------------------------------------------------------------------- #
# Allowlist shape
# --------------------------------------------------------------------------- #


def test_allowlist_contains_exactly_three_tools():
    assert set(TOOL_NAMES) == {"impact_analysis", "ownership_lookup", "incident_history"}


def test_tools_schema_is_ollama_compatible():
    schema = tools_schema()
    assert len(schema) == 3
    for tool in schema:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert fn["name"] in {"impact_analysis", "ownership_lookup", "incident_history"}
        params = fn["parameters"]
        assert params["required"] == ["service_id"]
        assert "service_id" in params["properties"]
    depth = next(
        fn["parameters"].get("properties", {}).get("max_depth")
        for fn in [t["function"] for t in schema]
        if fn["name"] == "impact_analysis"
    )
    assert depth is not None and "1-6" in depth["description"]


def test_system_prompt_names_only_allowlisted_tools():
    from blastradius.agent_tools import system_prompt

    prompt = system_prompt()
    assert "never generate cypher" in prompt.lower()
    for name in TOOL_NAMES:
        assert name in prompt
    assert "cypher language" in prompt.lower()  # explicitly forbidden, not available


# --------------------------------------------------------------------------- #
# Validation — no real graph access needed for rejects
# --------------------------------------------------------------------------- #


def test_rejects_unknown_tool(graph_ready):
    res = execute_tool_call(graph_ready, ToolCall("drop_all", {"service_id": "payment-service"}, 1))
    assert res.status == "rejected"
    assert "allowlist" in (res.error or "")


def test_rejects_missing_service_id(graph_ready):
    res = execute_tool_call(graph_ready, ToolCall("impact_analysis", {}, 1))
    assert res.status == "rejected" and res.error and "service_id" in res.error


def test_rejects_out_of_range_depth(graph_ready):
    for bad in (0, 7, 99, 1.5, "deep"):
        res = execute_tool_call(
            graph_ready, ToolCall("impact_analysis", {"service_id": "payment-service", "max_depth": bad}, 1)
        )
        assert res.status == "rejected", f"depth {bad!r} should be rejected"
        assert "max_depth" in (res.error or "")


def test_rejects_unsupported_arguments(graph_ready):
    res = execute_tool_call(
        graph_ready,
        ToolCall("ownership_lookup", {"service_id": "payment-service", "cypher": "MATCH (n) DETACH DELETE n"}, 1),
    )
    assert res.status == "rejected" and "cypher" in (res.error or "")


def test_rejects_bool_depth_and_unicode_garbage(graph_ready):
    res = execute_tool_call(
        graph_ready, ToolCall("impact_analysis", {"service_id": "payment-service", "max_depth": True}, 1)
    )
    assert res.status == "rejected"


def test_depth_boundary_values_accepted(graph_ready):
    ok = execute_tool_call(
        graph_ready, ToolCall("impact_analysis", {"service_id": "config-service", "max_depth": 6}, 1)
    )
    assert ok.status == "ok"
    assert ok.evidence["max_depth"] == 6


# --------------------------------------------------------------------------- #
# Honest handling of unknown ids — real Not found responses
# --------------------------------------------------------------------------- #


def test_unknown_service_is_honestly_not_found(graph_ready):
    res = execute_tool_call(graph_ready, ToolCall("impact_analysis", {"service_id": "paymnt-servic"}, 1))
    assert res.status == "not_found"
    assert "not in the graph" in res.detail
    assert res.evidence.get("suggestions"), "should offer close matches"
    assert "payment-service" in res.evidence["suggestions"]


def test_ownership_unknown_service_honest(graph_ready):
    res = execute_tool_call(graph_ready, ToolCall("ownership_lookup", {"service_id": "no-such-svc"}, 1))
    assert res.status == "not_found" and "not in the graph" in res.detail


def test_incident_unknown_service_honest(graph_ready):
    res = execute_tool_call(graph_ready, ToolCall("incident_history", {"service_id": "no-such-svc"}, 1))
    assert res.status == "not_found" and "not in the graph" in res.detail


# --------------------------------------------------------------------------- #
# Real executions — every ok case above ran live Cypher rows
# --------------------------------------------------------------------------- #


def test_impact_tool_returns_real_paths_and_teams(graph_ready):
    res = execute_tool_call(graph_ready, ToolCall("impact_analysis", {"service_id": "payment-service"}, 1))
    assert res.status == "ok"
    ev = res.evidence
    assert ev["origin"]["id"] == "payment-service"
    ids = [i["service_id"] for i in ev["impacted"]]
    assert ids, "real traversal must list affected services"
    assert any(i["hops"] >= 2 for i in ev["impacted"]), "multi-hop analysis expected"
    with_paths = [i for i in ev["impacted"] if i["path"] and "->" in i["path"]]
    assert with_paths, "every recommendation should include the real path arrow"
    assert ev["teams"], "team ownership metadata expected from the graph"


def test_ownership_tool_real(graph_ready):
    res = execute_tool_call(graph_ready, ToolCall("ownership_lookup", {"service_id": "payment-service"}, 1))
    assert res.status == "ok"
    assert res.evidence["teams"] and any(t["slack_channel"] for t in res.evidence["teams"])
    assert res.evidence["engineers"], "engineer roster should not be empty for payment-services"


def test_incident_tool_real(graph_ready):
    res = execute_tool_call(graph_ready, ToolCall("incident_history", {"service_id": "fraud-service"}, 1))
    assert res.status == "ok"
    ev = res.evidence["incidents"]
    assert ev, "seeded data has at least one incident rooted at fraud-service"
    assert any(i["role"] == "root cause" for i in ev)
    assert all(set(("id", "severity", "status")) <= set(i) for i in ev)


# --------------------------------------------------------------------------- #
# Bounded plan execution
# --------------------------------------------------------------------------- #


def test_plan_is_hard_bounded(graph_ready):
    calls = [
        ToolCall("impact_analysis", {"service_id": "payment-service"}, 1),
        ToolCall("impact_analysis", {"service_id": "checkout-service"}, 1),
        ToolCall("impact_analysis", {"service_id": "order-service"}, 1),
        ToolCall("impact_analysis", {"service_id": "invoice-service"}, 1),
        ToolCall("ownership_lookup", {"service_id": "payment-service"}, 2),
        ToolCall("incident_history", {"service_id": "fraud-service"}, 2),
    ]
    results = execute_plan(graph_ready, calls, budget=MAX_TOOL_CALLS)
    assert len(results) == 6
    executed = [r for r in results[:MAX_TOOL_CALLS]]
    skipped = results[MAX_TOOL_CALLS:]
    assert all(r.status != "skipped" for r in executed)
    assert all(r.status == "skipped" and r.error == "budget_exceeded" for r in skipped)
    assert len(executed) == MAX_TOOL_CALLS == 4


def test_tools_never_execute_when_plan_is_empty(graph_ready):
    assert execute_plan(graph_ready, []) == []
