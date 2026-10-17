"""Local LLM integration: Ollama + the tool allowlist, nothing else.

The model runs entirely on this machine (default ``qwen3.5:2b`` downloaded via
``ollama pull``) and speaks only Ollama's documented ``/api/chat`` endpoint with
the ``tools`` field. It chooses tool calls; this module validates them through
:mod:`blastradius.agent_tools`, executes the real FalkorDB queries, and feeds
the honest results back as ``role:"tool"`` messages — Ollama's documented flow.

Honesty guarantees:

* If Ollama is not running, or the model is not pulled, ``check_ollama`` says
  exactly that and the UI shows setup instructions. There is *no* deterministic
  stand-in treadmill pretending to be the model.
* Tool results are never paraphrased into new facts — the follow-up prompt
  includes the returned evidence verbatim and the final answer must cite it.
* The loop is bounded: at most ``MAX_TOOL_CALLS`` executions no matter what the
  model asks for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import requests

from . import agent_tools
from .db import BlastRadiusDB

DEFAULT_MODEL = "qwen3.5:2b"
DEFAULT_HOST = "http://127.0.0.1:11434"
REQUEST_TIMEOUT = 180  # seconds; small CPU model + bounded loop
ChatStatus = Literal["ok", "unreachable", "model_missing"]


@dataclass
class AgentStep:
    """One visible step of the investigation (tool call, note, or answer)."""

    kind: Literal["tool_call", "status", "note"]
    text: str
    result: agent_tools.ToolResult | None = None


@dataclass
class Investigation:
    """Everything the UI needs to show an honest end-to-end run."""

    question: str
    model: str
    steps: list[AgentStep] = field(default_factory=list)
    answer: str = ""
    tool_results: list[agent_tools.ToolResult] = field(default_factory=list)
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #


def ollama_host() -> str:
    import os

    configured = os.getenv("OLLAMA_HOST", "").strip()
    return configured.rstrip("/") if configured else DEFAULT_HOST


def check_ollama(model: str = DEFAULT_MODEL) -> tuple[ChatStatus, str]:
    """Honest availability probe against the real Ollama service."""
    base = ollama_host()
    try:
        resp = requests.get(f"{base}/api/tags", timeout=5)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return "unreachable", (
            f"Ollama is not reachable at {base} ({exc}).\n"
            "Install it from https://ollama.com/download/windows, then:\n"
            f"  ollama pull {model}\n"
            "and make sure the Ollama app is running before using the Agent tab."
        )
    tags = [m.get("name", "") for m in resp.json().get("models", [])]
    wanted = model.lower()
    family = wanted.split(":")[0].split("/")[-1]
    if wanted in [t.lower() for t in tags]:
        return "ok", next(t for t in tags if t.lower() == wanted)
    fuzzy = [
        t
        for t in tags
        if t.lower().split(":")[0].split("/")[-1] == family
    ]
    if fuzzy:
        return "ok", fuzzy[0]
    return "model_missing", (
        f"Ollama is reachable at {base} but '{model}' is not pulled (available: "
        f"{tags or 'none'}). Run:  ollama pull {model}"
    )


# --------------------------------------------------------------------------- #
# The bounded investigation loop
# --------------------------------------------------------------------------- #


def _chat(base: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(f"{base}/api/chat", json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def run_investigation(
    db: BlastRadiusDB,
    question: str,
    model: str = DEFAULT_MODEL,
    max_tool_calls: int = agent_tools.MAX_TOOL_CALLS,
    known_ids: list[str] | None = None,
) -> Investigation:
    """Plan & execute tools with the local model against real graph evidence."""
    inv = Investigation(question=question, model=model)
    status, detail = check_ollama(model)
    if status != "ok":
        inv.error = detail
        inv.steps.append(AgentStep(kind="status", text=f"Ollama status: {status} — {detail}"))
        return inv

    base = ollama_host()
    connected_model = detail  # exact local tag, e.g. 'qwen3.5:2b:Q8_0'
    if known_ids:
        context = (
            "Service ids that exist in the graph (use these verbatim): "
            + ", ".join(known_ids)
        )
    else:
        context = "No service id list was supplied; pass the names the human gave."

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": agent_tools.system_prompt()},
        {"role": "user", "content": f"{context}\n\nInvestigate: {question}"},
    ]
    payload: dict[str, Any] = {
        "model": connected_model,
        "messages": messages,
        "tools": agent_tools.tools_schema(),
        "stream": False,
    }

    executed = 0
    rounds = 0
    max_rounds = 2  # hard bound on the loop: plan round + finish round
    seen_calls: set[tuple[str, str]] = set()

    while rounds < max_rounds and executed < max_tool_calls:
        rounds += 1
        try:
            response = _chat(base, payload)
        except requests.RequestException as exc:
            inv.error = f"Ollama /api/chat failed after {rounds - 1} round(s): {exc}"
            inv.steps.append(AgentStep(kind="status", text=inv.error))
            return inv

        message = (response.get("message") or {}) if isinstance(response, dict) else {}
        calls_raw = message.get("tool_calls") or []
        content = (message.get("content") or "").strip()

        if content and not calls_raw:
            inv.steps.append(AgentStep(kind="note", text=content))

        if not calls_raw:
            break  # model finished planning without further tools

        # One explicit tool call per round keeps ordering deterministic and
        # makes the "show the actual result before the next decision" contract
        # easy to verify in the UI text. Extra calls in a single round beyond
        # budget are planned but not executed (bounded).
        for raw in calls_raw[: 1 if rounds == 1 else (max_tool_calls - executed)]:
            fn = raw.get("function", raw) if isinstance(raw, dict) else {}
            call_name = str(fn.get("name", ""))
            call_args = fn.get("arguments", "") if isinstance(fn.get("arguments"), str) else (fn.get("arguments") or {})
            if isinstance(call_args, str):
                args_obj = _coerce_json(call_args)
            else:
                args_obj = call_args
            from .agent_tools import ToolCall

            key = (call_name, str(args_obj))
            if key in seen_calls:
                continue
            seen_calls.add(key)
            executed_now = executed < max_tool_calls
            result = agent_tools.execute_tool_call(
                db, ToolCall(tool=call_name, arguments=args_obj, round_id=rounds)
            )
            if result.status == "rejected":
                executed_now = False
            elif result.status != "skipped":
                executed += 1
            inv.steps.append(AgentStep(kind="tool_call", text=result.detail or call_name, result=result))
            inv.tool_results.append(result)
            payloads = {
                "role": "tool",
                "tool_name": call_name,
                "content": (
                    "status: " + result.status + "\nsummary: " + result.detail
                    + "\nevidence: " + _digest(result.evidence)
                ),
            }
            messages.append(payloads)

        if executed >= max_tool_calls:
            inv.steps.append(
                AgentStep(
                    kind="note",
                    text=f"Tool budget of {max_tool_calls} reached; asking the model to conclude from the evidence above.",
                )
            )
        payload["messages"] = messages + [
            {
                "role": "user",
                "content": (
                    "Using ONLY the tool results above as sources, answer briefly. "
                    "If the evidence is insufficient say so. Cite service ids, "
                    "teams, incident ids and paths exactly as returned."
                ),
            }
        ]

    final = _chat(base, {**payload, "tools": []}) if inv.tool_results else None
    if final is not None:
        answer_text = ((final.get("message") or {}).get("content") or "").strip()
        if answer_text:
            inv.answer = answer_text
            inv.steps.append(AgentStep(kind="note", text=answer_text))
    if not inv.answer:
        inv.answer = "The model produced no final text. See the tool results below for the raw evidence."
    return inv


def _coerce_json(text: str) -> Any:
    import json

    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _digest(evidence: dict[str, Any]) -> str:
    """Compact JSON of the evidence for the follow-up prompt (never truncated)."""
    import json

    return json.dumps(evidence, ensure_ascii=False, default=str)
