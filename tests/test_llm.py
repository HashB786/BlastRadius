"""Local-model (Ollama) integration tests.

These only exercise the real Ollama HTTP service. If Ollama is not running, or
the configured model is not pulled, the integration cases SKIP with an explicit
message — they are never faked and never allowed to claim a pass without a
real round trip. Pure logic (request building, JSON coercion) is tested
without touching the network.
"""

from __future__ import annotations

import requests
import pytest

from blastradius.llm import (
    DEFAULT_MODEL,
    _coerce_json,
    check_ollama,
    run_investigation,
)

OLLAMA_TIMEOUT = 5


def _probe():
    try:
        resp = requests.get("http://127.0.0.1:11434/api/tags", timeout=OLLAMA_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("models", [])
    except requests.RequestException:
        return None


_MODELS = _probe()
SERVICE_UP = _MODELS is not None
MODEL_UP = SERVICE_UP and any(
    m.get("name", "").lower().split(":")[0].split("/")[-1] == DEFAULT_MODEL.split(":")[0].split("/")[-1]
    for m in _MODELS
)

SKIP_SERVICE = "Ollama service is not running on this machine; integration tests skipped honestly"
SKIP_MODEL = f"model '{DEFAULT_MODEL}' is not pulled; run  ollama pull {DEFAULT_MODEL}"

needs_service = pytest.mark.skipif(not SERVICE_UP, reason=SKIP_SERVICE)
needs_model = pytest.mark.skipif(not MODEL_UP, reason=SKIP_MODEL)


def test_tool_call_argument_string_is_coerced():
    assert _coerce_json('{"service_id": "payment-service"}') == {"service_id": "payment-service"}
    assert _coerce_json("not json") == "not json"  # downstream validation rejects it


def test_chat_status_enum_honored():
    """The availability probe returns one of three statuses, never invents a fourth."""
    status, _detail = check_ollama(DEFAULT_MODEL)
    assert status in {"ok", "unreachable", "model_missing"}
    if SERVICE_UP and MODEL_UP:
        assert status == "ok"


@needs_service
@needs_model
def test_real_ollama_agent_investigation(seeded):  # noqa: ANN001
    """A genuine round trip: question -> model -> tool -> real graph -> answer."""
    from blastradius.agent_tools import TOOL_NAMES

    question = (
        "What breaks if payment-service fails, which teams own the affected "
        "services, and have we seen a similar incident?"
    )
    inv = run_investigation(
        seeded,
        question,
        model=DEFAULT_MODEL,
        known_ids=["payment-service", "checkout-service", "fraud-service", "ledger-service"],
    )

    if not inv.succeeded:
        pytest.fail(f"agent run against real Ollama failed: {inv.error}")

    tool_calls = [s.result for s in inv.steps if s.kind == "tool_call" and s.result]
    assert tool_calls, "the model must ask for at least one allowlisted tool"
    for call in tool_calls:
        assert call.tool in TOOL_NAMES, "the model may only select allowlisted tools"
        assert call.status in {"ok", "not_found"}, call.status
        if call.status == "ok":
            assert call.evidence, "ok tool results must carry real evidence"
    assert any(call.status == "ok" for call in tool_calls)

    ok_impacts = [c for c in tool_calls if c.tool == "impact_analysis" and c.status == "ok"]
    if ok_impacts:
        impacted = ok_impacts[0].evidence.get("impacted", [])
        assert impacted and any("payment-service" not in i["service_id"] for i in impacted)
        assert any("->" in i["path"] for i in impacted), "real path arrows must be shown"

    assert len(inv.tool_results) <= 4, "bounded: no more than 4 tool executions"
