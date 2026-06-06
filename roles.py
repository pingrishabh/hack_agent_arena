"""Reasoning roles (ARCH.md §5): plan, self-verify, plus small parsing helpers.

Roles receive the `complete` callable (the litellm `call_llm` boundary) by
injection, so this module never imports agent.py (no import cycle). All parsing
is defensive — malformed model output degrades gracefully, never crashes the
run (CLAUDE.md Rule #3). `act` is the main ReAct turn and lives in the
orchestrator; `reflect` is done deterministically at consolidation time in
memory.py to conserve Groq rate budget.
"""

from __future__ import annotations

import json
import re

FINISH_RE = re.compile(r"^\s*FINISH:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_CODE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)

PLAN_SYSTEM = """You are the planner for an autonomous agent operating inside AppWorld.
Given a supervisor task, write a SHORT numbered plan (3-7 steps) of how to accomplish it
using the apps' APIs. Be concrete about which apps/APIs are likely needed and the order of
operations (log in first, fetch data, then act, then verify). Do NOT write code. Output only
the numbered plan."""

VERIFY_SYSTEM = """You are a strict verifier for an autonomous AppWorld agent. The agent thinks it
has finished. Your job: decide whether the task is TRULY and fully accomplished, with the correct
side effects and no collateral damage. Grading is state-based, so being "almost right" fails.
Reply with ONLY a JSON object: {"verified": true|false, "recheck": "<if false, what to re-read or
fix before completing>"}."""


def plan(complete, instruction: str, supervisor: str, retrieved: str) -> str:
    """Produce a short plan once per task. Returns '' on failure (agent still runs)."""
    user = f"Supervisor: {supervisor}\n\nTask: {instruction}\n"
    if retrieved.strip():
        user += f"\nRelevant past knowledge/recipes (may help, may be empty):\n{retrieved}\n"
    user += "\nWrite the numbered plan."
    try:
        out = complete([{"role": "user", "content": user}], system=PLAN_SYSTEM, max_tokens=500)
        return (out or "").strip()
    except Exception:
        return ""


def self_verify(complete, instruction: str, turns: list) -> tuple[bool, str]:
    """Return (verified, recheck_hint). Defaults to verified=True on any failure
    so the loop never deadlocks."""
    traj = []
    for t in turns[-8:]:
        obs = (t.observation or "")[:400]
        traj.append(f"[{t.step}] intent={t.intent!r}\ncode={t.code[:300]}\nobs={obs}")
    user = (
        f"Task instruction: {instruction}\n\n"
        f"What the agent did (recent steps):\n" + "\n---\n".join(traj) +
        "\n\nIs the task truly and fully complete? Reply JSON only."
    )
    try:
        out = complete([{"role": "user", "content": user}], system=VERIFY_SYSTEM, max_tokens=300)
        obj = _extract_json(out)
        if obj is None:
            return True, ""
        verified = bool(obj.get("verified", True))
        return verified, str(obj.get("recheck", "") or "")
    except Exception:
        return True, ""


# ---- parsing helpers ----------------------------------------------------

def parse_finish(reply: str) -> tuple[bool, object]:
    """If the reply signals completion via 'FINISH: <answer>', return (True, answer).
    answer is None for NONE/empty. Otherwise (False, None)."""
    m = FINISH_RE.search(reply or "")
    if not m:
        return False, None
    raw = m.group(1).strip()
    if raw == "" or raw.upper() == "NONE":
        return True, None
    # try to coerce simple literals (numbers); else keep as string
    try:
        return True, json.loads(raw)
    except Exception:
        return True, raw.strip("\"'")


def extract_code(reply: str) -> str:
    m = _CODE_RE.search(reply or "")
    return m.group(1).strip() if m else (reply or "").strip()


def guess_intent(reply: str) -> str:
    """Cheap 'why' for the episodic trail: first non-code prose line."""
    for line in (reply or "").splitlines():
        s = line.strip()
        if s and not s.startswith("```") and not s.startswith("#"):
            return s[:160]
    return ""


def _extract_json(text: str):
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return None
