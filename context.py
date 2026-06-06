"""Token-budgeted context assembly (ARCH.md §6).

Keeps the per-turn prompt within a token budget so long ReAct transcripts never
overflow the model's context window (which would fail the task) and so we don't
pay the full quadratic transcript cost every turn. Model-agnostic: token counts
go through litellm's counter, with a cheap char-based fallback.
"""

from __future__ import annotations

import litellm


def count_tokens(model: str, text: str) -> int:
    """Best-effort token count; never raises."""
    try:
        return litellm.token_counter(model=model, text=text)
    except Exception:
        return max(1, len(text) // 4)  # ~4 chars/token fallback


def _truncate_tokens(model: str, text: str, max_tokens: int) -> str:
    """Trim text to ~max_tokens, keeping the head (most relevant first)."""
    if count_tokens(model, text) <= max_tokens:
        return text
    # binary-ish trim on characters using the token estimate
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if count_tokens(model, text[:mid]) <= max_tokens:
            lo = mid + 1
        else:
            hi = mid
    return text[: max(0, lo - 1)].rstrip() + "\n…[truncated]"


def assemble(
    model: str,
    head: str,
    turns: list[dict],
    *,
    budget: int = 7000,
    head_cap: int = 3000,
    recent_floor: int = 2,
) -> list[dict]:
    """Build the non-system messages for call_llm within a token budget.

    head   : the first user message (task + plan + retrieved memory + rules).
    turns  : list of {"assistant": <reply>, "user": <observation>} in order.
    budget : approx token ceiling for the whole returned message list.
    Returns newest turns preferentially; older ones are dropped once the budget
    is hit (always keeps at least `recent_floor` turns if present).
    """
    head = _truncate_tokens(model, head, head_cap)
    messages: list[dict] = [{"role": "user", "content": head}]

    used = count_tokens(model, head)
    kept: list[dict] = []
    for i, t in enumerate(reversed(turns)):
        pair_text = (t.get("assistant", "") or "") + (t.get("user", "") or "")
        cost = count_tokens(model, pair_text)
        if used + cost > budget and len(kept) >= recent_floor:
            dropped = len(turns) - len(kept)
            if dropped > 0:
                messages.append(
                    {"role": "user", "content": f"[{dropped} earlier step(s) omitted to fit context]"}
                )
            break
        kept.append(t)
        used += cost

    for t in reversed(kept):
        if t.get("assistant"):
            messages.append({"role": "assistant", "content": t["assistant"]})
        messages.append({"role": "user", "content": t.get("user", "")})
    return messages
