"""Agent memory — the HydraDB brain (ARCH.md §3.2, §4).

HydraDB is the persistent, cross-task brain. This module is the only place that
knows about it; the rest of the agent talks to the `Memory` seam.

Design choices that matter:
- **Non-critical path.** Every HydraDB call is wrapped so a failure/timeout/slow
  index NEVER crashes the run loop (CLAUDE.md Rule #3). On any error we degrade
  to a no-op and the agent proceeds without that memory benefit.
- **Async-aware.** HydraDB ingestion indexes asynchronously, so just-written
  memory isn't instantly retrievable. We therefore `retrieve` at plan time and
  `remember` (consolidate) once at task end — value accrues to *later* tasks
  (cold-start live learning across the run).
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class MemoryItem:
    text: str
    score: float = 0.0
    meta: dict = field(default_factory=dict)


@dataclass
class Turn:
    step: int
    intent: str          # the "why" — model's stated goal for this action
    code: str
    observation: str
    error: str | None = None


@dataclass
class Outcome:
    completed: bool
    verified: bool
    answer: object | None = None


def _log(msg: str) -> None:
    print(f"  [memory] {msg}", file=sys.stderr)


class Memory(Protocol):
    def retrieve(self, query: str, k: int = 8) -> list[MemoryItem]: ...
    def remember(self, task_id: str, instruction: str,
                 turns: list[Turn], outcome: Outcome) -> None: ...
    def close(self) -> None: ...


class NullMemory:
    """No-op memory — used when HydraDB is unavailable. Keeps the agent running."""

    def retrieve(self, query: str, k: int = 8) -> list[MemoryItem]:
        return []

    def remember(self, task_id, instruction, turns, outcome) -> None:
        return None

    def close(self) -> None:
        return None


def _chunk_text(chunk) -> str:
    for attr in ("text", "content", "chunk"):
        v = getattr(chunk, attr, None)
        if isinstance(v, str) and v:
            return v
    if isinstance(chunk, dict):
        for key in ("text", "content", "chunk"):
            if isinstance(chunk.get(key), str):
                return chunk[key]
    return str(chunk)


class HydraDBMemory:
    """HydraDB-backed memory. Stores procedural recipes + lessons (knowledge) and
    episodic task summaries (memory); retrieves with the graph context enabled."""

    def __init__(self, token: str, tenant_id: str = "pingrishabh-appworld",
                 provision_timeout: float = 90.0):
        from hydra_db import HydraDB  # local import: optional dependency

        self.tenant_id = tenant_id
        self.ready = False
        try:
            self.client = HydraDB(token=token)
            self._ensure_tenant(provision_timeout)
        except Exception as e:  # never let memory setup kill the run
            _log(f"init failed ({type(e).__name__}: {e}); running without memory")
            self.client = None

    def _ensure_tenant(self, timeout: float) -> None:
        existing = []
        try:
            existing = list(self.client.tenants.list().data.tenant_ids or [])
        except Exception:
            pass
        if self.tenant_id not in existing:
            try:
                self.client.tenants.create(tenant_id=self.tenant_id)
                _log(f"creating tenant '{self.tenant_id}'…")
            except Exception as e:
                _log(f"tenant create failed: {e}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                infra = self.client.tenants.status(tenant_id=self.tenant_id).data.infra
                if getattr(infra, "ready_for_ingestion", False):
                    self.ready = True
                    _log(f"tenant '{self.tenant_id}' ready")
                    return
            except Exception:
                pass
            time.sleep(5)
        _log("tenant not ready within timeout; memory degraded (retrieve only if it becomes ready)")

    # ---- retrieval -------------------------------------------------------
    def retrieve(self, query: str, k: int = 8) -> list[MemoryItem]:
        if not self.client:
            return []
        try:
            resp = self.client.query(
                tenant_id=self.tenant_id,
                query=query[:2000],
                type="all",
                query_by="hybrid",
                max_results=k,
                graph_context=True,
            )
            chunks = getattr(resp.data, "chunks", None) or []
            return [MemoryItem(text=_chunk_text(c)) for c in chunks][:k]
        except Exception as e:
            _log(f"retrieve failed: {type(e).__name__}: {e}")
            return []

    # ---- consolidation (task end) ---------------------------------------
    def remember(self, task_id: str, instruction: str,
                 turns: list[Turn], outcome: Outcome) -> None:
        if not self.client or not self.ready:
            return
        try:
            # Episodic summary (memory) — always recorded.
            summary = self._summarize(task_id, instruction, turns, outcome)
            self._ingest_memory(summary)
            # Procedural recipe (knowledge) — only from verified successes.
            if outcome.completed and outcome.verified:
                recipe = self._recipe(instruction, turns)
                if recipe:
                    self._ingest_knowledge(recipe, {"kind": "procedural", "task_id": task_id})
        except Exception as e:
            _log(f"remember failed: {type(e).__name__}: {e}")

    def _ingest_memory(self, text: str) -> None:
        try:
            self.client.context.ingest(
                tenant_id=self.tenant_id,
                type="memory",
                memories=json.dumps([{"text": text}]),
            )  # fire-and-forget; indexing is async
        except Exception as e:
            _log(f"ingest(memory) failed: {type(e).__name__}: {e}")

    def _ingest_knowledge(self, text: str, meta: dict) -> None:
        try:
            self.client.context.ingest(
                tenant_id=self.tenant_id,
                type="knowledge",
                documents=[(f"{meta.get('kind','doc')}-{meta.get('task_id','x')}.txt", text)],
                document_metadata=json.dumps(meta),
            )  # fire-and-forget
        except Exception as e:
            _log(f"ingest(knowledge) failed: {type(e).__name__}: {e}")

    @staticmethod
    def _summarize(task_id, instruction, turns, outcome) -> str:
        status = "SUCCEEDED" if outcome.completed and outcome.verified else "did not verify"
        apis = sorted({a for t in turns for a in _apis_in(t.code)})
        errs = [t.error for t in turns if t.error]
        lines = [
            f"AppWorld task {task_id} {status}.",
            f"Instruction: {instruction}",
            f"APIs used: {', '.join(apis) if apis else 'none recorded'}",
        ]
        if errs:
            lines.append(f"Errors encountered ({len(errs)}): {errs[-1][:300]}")
        return "\n".join(lines)

    @staticmethod
    def _recipe(instruction, turns) -> str:
        good = [t.code for t in turns if t.code and not t.error]
        if not good:
            return ""
        return (
            f"Recipe for tasks like: {instruction}\n"
            f"Working code steps that succeeded:\n" + "\n---\n".join(good[-12:])
        )

    def close(self) -> None:
        return None


_API_RE = None


def _apis_in(code: str) -> list[str]:
    """Extract apis.<app>.<name> identifiers from a code snippet."""
    global _API_RE
    if _API_RE is None:
        import re
        _API_RE = re.compile(r"apis\.(\w+)\.(\w+)")
    return [f"{m.group(1)}.{m.group(2)}" for m in _API_RE.finditer(code or "")]


def build_memory() -> Memory:
    """Factory: HydraDBMemory if a key is present, else NullMemory."""
    key = os.environ.get("HYDRADB_API_KEY")
    if not key:
        _log("HYDRADB_API_KEY not set — using NullMemory")
        return NullMemory()
    mem = HydraDBMemory(token=key)
    if not getattr(mem, "client", None):
        return NullMemory()
    return mem
