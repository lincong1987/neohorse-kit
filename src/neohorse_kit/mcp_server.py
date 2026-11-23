"""MCP server exposing NeoHorse JEV decisions as agent tools.

Run with ``neohorse-kit-mcp`` (or ``uvx neohorse-kit-mcp``).
API keys come from the ``NEOHORSE_API_KEYS`` environment variable.
"""

from __future__ import annotations

import json
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer as _Server  # mcp >= 2
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .client import NeoHorseClient
from . import presets

mcp = _Server("neohorse")

_client: NeoHorseClient | None = None


def _get_client() -> NeoHorseClient:
    global _client
    if _client is None:
        _client = NeoHorseClient()
    return _client


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=lambda o: o.model_dump(mode="json"))


@mcp.tool()
def decide(state: Any, questions: dict[str, Any], endpoint: str = "decision") -> str:
    """Judge text or JSON data with the NeoHorse JEV decision model, probability scores out.

    Args:
        state: the text — or any JSON value — under judgement.
        questions: mapping of question name -> question spec (1..16 per call).
            Three types, all answered in ONE upstream call:
            {"type": "noul", "instructions": "<yes/no question>",
             "criteria"? : {"true": "what counts as yes", "false": "..."}}
              -> {"noul": 0..1} (probability of yes)
            {"type": "choice", "instructions": "<question>",
             "criteria": {"label1": "what fits this label", "label2": null}}
              -> {"choice": "<label>", "probabilities": {...}, "confidence": ...}
            {"type": "score", "instructions": "<rating question>",
             "criteria": ["low", "medium", "high"]}
              -> {"score": float (index into criteria), "probabilities", "confidence"}
            Example: {"should_stop": {"type": "noul", "instructions": "现在是否应停车？"}}
        endpoint: "decision" (recommended) or "systemone" (System One compatible shape).

    Returns JSON: {"model", "answers", "usage", "input_details"/"extensions",
    "decision_seconds", "latency_ms"}. Choose your own threshold on the
    probabilities rather than treating them as a verdict.
    """
    return _json(_get_client().decide(state, questions, endpoint=endpoint))


@mcp.tool()
def ask(
    state: Any,
    question: str = "Does this convey urgency?",
    kind: str = "noul",
    levels: list[str] | None = None,
    options: dict[str, str] | None = None,
) -> str:
    """Ask ONE simple question about a text; convenience wrapper over `decide`.

    Args:
        state: the text — or any JSON value — under judgement.
        question: what to judge, e.g. "Is the user angry?".
        kind: "noul" (0..1 yes-score), "score" (ordered levels) or "choice" (pick a label).
        levels: ordered level labels for kind="score", e.g. ["calm", "annoyed", "angry"].
        options: {label: description} for kind="choice", e.g. {"billing": "payment issues", "technical": "bugs"}.

    Returns JSON with the answer (noul score, or choice/score + probabilities).
    """
    if kind == "noul":
        q = presets.noul(question)
    elif kind == "score":
        q = presets.score(question, levels or ["low", "medium", "high"])
    elif kind == "choice":
        q = presets.choice(question, options or {"yes": None, "no": None})
    else:
        return _json({"error": f"unknown kind '{kind}': use noul, score or choice"})
    return _json(_get_client().decide(state, q))


@mcp.tool()
def list_models() -> str:
    """List the models available to this account on the Token Rhythm platform,
    as a key health check. JEV itself is a decision model and is not part of
    this chat-model catalog; it is called directly with model
    'NeoHorse-Jev-4B'."""
    return _json(_get_client().models())


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
