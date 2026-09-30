"""Add Agent: a structured form (name/purpose/key details) that drafts both
a plain-English plan and a code-mode instruction for a brand-new research/
trading agent, then hands off to codemode.py's existing request/confirm
pipeline untouched. This is a form-driven front end onto the exact same
diff-review/emailed-code/"run it" flow as regular code mode - no separate
safety mechanism is introduced here.
"""
import json
import os

import anthropic

from .agents._parsing import strip_json_fence

_client = None

MODEL = "claude-sonnet-4-5"

PROMPT_TEMPLATE = (
    "You are helping design a new automated research/trading agent for "
    "this system, to be added via a real code change to the codebase. "
    "The user described it as:\n\n"
    "Name: {name}\n"
    "Purpose: {purpose}\n"
    "Key details: {details}\n\n"
    "This system already has a pipeline of research agents (trading/"
    "agents/*.py: price/volume via Alpaca bars, fundamentals via Yahoo "
    "Finance, news sentiment via Alpaca's News API, technical analysis) "
    "that feed into trading/agents/synthesis.py (cross-references all "
    "signals into one distilled summary) and trading/agents/decision.py "
    "(turns that summary into a buy/sell/hold Signal), orchestrated by "
    "trading/pipeline.py.\n\n"
    "Produce two things:\n"
    "1. plan_explanation: a short, clear, plain-English explanation (2-4 "
    "sentences) of what you're about to build and how it fits into the "
    "existing pipeline - this is shown to the user for review before "
    "anything happens, so don't include code in it.\n"
    "2. instruction: a specific, actionable code-mode instruction (write "
    "it like you're briefing a developer) describing exactly what file(s) "
    "to create/modify and how to wire the new agent into synthesis.py and "
    "pipeline.py - match the existing agents' conventions (a module-level "
    "_client/_get_client() singleton, model claude-sonnet-4-5, a JSON-only "
    "prompt parsed via agents/_parsing.py's strip_json_fence with a "
    "defensive fallback on parse failure, same as decision.py/synthesis.py/"
    "research_fundamentals.py).\n\n"
    'Respond as JSON only: {{"plan_explanation": "...", "instruction": "..."}}'
)


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def draft_agent_plan(name, purpose, details):
    """Returns (plan_explanation, instruction). Raises ValueError if Claude
    didn't return a usable instruction, anthropic.APIError on request
    failure - both left for the caller to translate into an HTTP response."""
    prompt = PROMPT_TEMPLATE.format(name=name, purpose=purpose, details=details or "(none given)")
    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    parsed = json.loads(strip_json_fence(text))
    plan_explanation = (parsed.get("plan_explanation") or "").strip()
    instruction = (parsed.get("instruction") or "").strip()
    if not instruction:
        raise ValueError("Didn't get a usable build instruction back - try rephrasing the request.")
    return plan_explanation, instruction
