# ARCH.md — End-to-End Agent Architecture

> **Status:** binding design doc. Implements the requirements in `CLAUDE.md`.
> `CLAUDE.md` = *what* we must do (ground rules, grading). `ARCH.md` = *how* we do it.
> Keep the two in sync; if a rule changes, update this doc.

## 1. Goal & Strategy

Beat AppWorld TGC (ReAct + GPT-4o ≈ 48.8) on the mandated, fixed model
`groq/llama-3.3-70b-versatile`. Since the model is fixed and weaker at code than GPT-4o, **TGC is won
by scaffolding + memory**, not model choice:

- A structured reasoning pipeline (Plan → Act → Self-verify → Reflect).
- A persistent **HydraDB** "brain" that learns the environment across tasks (cold-start, live).
- Disciplined context budgeting and never-crash robustness.

## 2. System Overview

Two swappable boundaries (LLM, Memory) + a thin orchestrator running four sequential roles around a
per-task ReAct loop. HydraDB is the cross-task brain.

```
 AppWorld task (world.task.instruction + supervisor profile)
        │
   [PLAN]  once/task: retrieve(task) from HydraDB → sub-goals + relevant APIs/recipes
        │
   ┌────▼──────────────── ReAct loop (≤ MAX_INTERACTIONS=30) ─────────────────┐
   │  [ACT]   context.assemble(plan, retrieved, recent turns)  ──token-budgeted │
   │          → call_llm → extract_code → world.execute(code) -> observation     │
   │  [OBSERVE+INGEST]  memory.ingest(episodic turn + intent "why")              │
   │          if observation startswith "Execution failed. Traceback:"           │
   │              → error-recovery: retrieve fix / re-plan                        │
   └────┬───────────────────────────────────────────────────────────────────────┘
        │ model emits a "DONE" signal
   [SELF-VERIFY]  re-read world state via read-only APIs; confirm side effects;
        │          if unmet → inject verdict, loop back into ACT
        ▼
   apis.supervisor.complete_task(answer)
        │
   [REFLECT]  once/task: memory.consolidate(trajectory, outcome)
              → promote success to a versioned procedural recipe + lessons (the "why")
```

**Call-budget discipline (Groq/HydraDB rate limits):** `retrieve` at plan time (+ on error),
`ingest` per turn (cheap), `consolidate` once per task. Plan once; re-plan only on repeated failure.

## 3. Boundaries

### 3.1 LLM boundary — `call_llm` (in `agent.py`)
- Single function over **litellm** (`litellm.completion`), `MODEL` = `provider/model`.
- **Pinned `litellm==1.34.42`**: appworld hard-pins `pydantic<2`; newer litellm needs pydantic v2 →
  runtime conflict. This version imports cleanly under pydantic v1 and supports Groq.
- Scored/submitted runs **must** use `groq/llama-3.3-70b-versatile` (CLAUDE.md Rule #1).
- `num_retries=8` absorbs Groq 429s.

### 3.2 Memory boundary — `Memory` seam (in `memory.py`)
HydraDB is the sole implementation; the seam exists only for isolation/testability.

```python
@dataclass
class MemoryItem:        # a retrieved unit
    kind: str            # "semantic" | "procedural" | "episodic" | "entity"
    text: str            # render-ready content (e.g., API doc, recipe code)
    score: float
    meta: dict

@dataclass
class Turn:
    step: int
    intent: str          # the "why" — model's stated goal for this action
    code: str
    observation: str
    error: str | None

@dataclass
class Outcome:
    completed: bool      # task_completed() was called
    verified: bool       # self-verify passed
    answer: object | None

class Memory(Protocol):
    def retrieve(self, query: str, kinds: list[str], k: int = 8) -> list[MemoryItem]: ...
    def ingest(self, record: dict) -> None: ...                       # append-only (episodic/entity)
    def consolidate(self, task_id: str, trajectory: list[Turn], outcome: Outcome) -> None: ...

class HydraDBMemory(Memory):
    def __init__(self, api_key: str, base_url: str, namespace: str = "appworld"): ...
```

Until the HydraDB key/SDK lands, `HydraDBMemory` is coded against the public docs behind the seam,
with a `NullMemory` no-op + a local record/replay stub so the rest of the system runs and is testable.

## 4. HydraDB Data Model (deep integration)

Graph-first, append-only, versioned — using HydraDB's signature features (context graph, reasoning
trail, "git-for-memory" versioning). Four content kinds:

| Kind | Stored | Keyed / linked | Used by |
|------|--------|----------------|---------|
| **Semantic** | API docs: `{app, api_name, signature, params, returns, description}` (= API-retrieval lever) | node `app:api`; edges app→api | PLAN, error-recovery |
| **Procedural** | Recipes: `{goal_pattern, code, apis_used, preconditions, confidence, version}` | versioned; supersede, rollback | PLAN, ACT |
| **Episodic** | Per-turn: `{task_id, step, intent("why"), code, observation, error?}` time-ordered | edges turn→apis, turn→entities | REFLECT, error-recovery |
| **Entity** | People / accounts / credential patterns + relationships | graph edges | ACT (resolve "my roommate") |

- **Reasoning trail:** every episodic record stores intent ("why") + outcome → auditable; powers
  error→fix retrieval.
- **Versioned/append-only:** lessons never overwritten; new recipe versions supersede; confidence +
  recency rank retrieval; bad lessons can be rolled back.
- **Cold start:** memory begins empty for the scored run and compounds live across the 168 tasks.

## 5. Reasoning Roles (`roles.py`)

All roles go through the one `call_llm`. Each returns a parsed result; parsing is defensive
(malformed output never crashes — CLAUDE.md Rule #3).

- `plan(task, retrieved) -> Plan` — once/task; decompose into ordered sub-goals; cite relevant
  APIs/recipes from `retrieved`. Re-plan on repeated failure.
- `act(state) -> str` — the ReAct turn; returns one python code block (reuse `extract_code`).
- `self_verify(task, world) -> Verdict{ok: bool, reason: str, fix_hint: str}` — before
  `complete_task`: re-read state via read-only APIs and confirm the intended side effects landed
  (directly attacks `task_completed() ≠ correct`). If `not ok`, loop back into ACT.
  **Completion interception:** the model often calls `complete_task` directly instead of using the
  FINISH signal, bypassing verification. We patch `complete_task` inside the persisted sandbox so the
  call is *captured* (side effects still run, completion held); we then self-verify and only restore +
  complete for real if it passes. This makes the verify gate unbypassable.
- `reflect(trajectory, outcome) -> Lessons` — once/task; summarize what worked/failed → feeds
  `consolidate` into procedural recipes + entity/semantic updates.

## 5b. Token Efficiency (the free tier is 100k tokens/day — see CLAUDE.md note)

Per-task token use is the binding operational constraint. Mitigations:
- **Hybrid API-retrieval index (`apidocs.py`).** The 457 API docs load via local Python (zero LLM
  tokens). We build a cached BM25 catalog once and inject only the top-k relevant API *signatures*
  into the prompt — replacing runtime `api_docs` discovery, whose large output otherwise lands in
  context and is re-sent every turn (the biggest token sink). Retrieval is **hybrid** (`ApiRetriever`,
  `API_RETRIEVAL=bm25|hydra|hybrid`): **BM25 is the always-on local floor**; the same API signatures
  are also ingested into **HydraDB** (`kind='api'`) and retrieved semantically, then fused with BM25
  via reciprocal-rank fusion. Any HydraDB failure/empty result falls back to BM25 — the token-critical
  path never goes dark. (Measured offline: HydraDB recovers APIs BM25 misses, e.g. `venmo.create_transaction`
  for "pay my roommate", where BM25 returned only balance APIs.)
- **Observation truncation** (`OBS_CAP`) before re-feeding big API responses.
- **Slim system prompt** (paid every turn) + **tight `CONTEXT_BUDGET`** with old-turn dropping.
- **Conditional roles** (`USE_PLAN`, `USE_VERIFY`) and lower `ACT_MAX_TOKENS`.

Even optimized (~2–3k tokens/task) a full 168-task run exceeds 100k/day → spread across days via the
resume harness, or use a higher Groq tier.

## 6. Context Manager (`context.py`)

- `assemble(system, plan, retrieved, turns, budget) -> messages` — packs, in priority order: system
  prompt, plan, retrieved memory, most-recent turns; older observations summarized/truncated to fit.
- Token accounting via litellm `token_counter` (no model lock-in). Prevents context-window overflow
  (which would fail tasks) and curbs the quadratic transcript blow-up.

## 7. Run Harness (`run.py`)

- Task loop over `load_task_ids(DATASET)`; `APPWORLD_EXPERIMENT=pingrishabh`.
- **Checkpoint/resume:** skip any task whose `experiments/outputs/<exp>/tasks/<task_id>/` already
  exists → multi-day/free-tier runs and crash/rate-limit recovery.
- Per-task `try/except` so one failure never kills the run (reuse starter pattern).
- Groq 429 pacing (on top of `num_retries=8`).
- Eval helper wrapping `appworld evaluate <exp> <split>` → reads
  `aggregate.task_goal_completion` (TGC) + `scenario_goal_completion` (SGC).

## 8. Per-Turn Pseudocode

```python
def solve(world, memory):
    task = world.task
    retrieved = memory.retrieve(task.instruction, ["semantic","procedural","entity"], k=8)
    plan = roles.plan(task, retrieved)
    turns = []
    for step in range(MAX_INTERACTIONS):
        messages = context.assemble(SYSTEM_PROMPT, plan, retrieved, turns, budget=BUDGET)
        reply = call_llm(messages)
        if signals_done(reply):
            verdict = roles.self_verify(task, world)        # read-only re-check
            if verdict.ok:
                world.execute("apis.supervisor.complete_task(answer=%r)" % verdict.answer)
                break
            else:
                turns.append(Turn(step, "verify-failed", "", verdict.reason, None)); continue
        code = extract_code(reply)
        obs = world.execute(code)                           # str; errors are "Execution failed..."
        err = obs if obs.startswith("Execution failed. Traceback:") else None
        turn = Turn(step, intent_of(reply), code, obs, err)
        turns.append(turn); memory.ingest(episodic(task.id, turn))
        if err:                                             # error recovery (lever 4)
            retrieved += memory.retrieve(err, ["semantic","procedural","episodic"], k=4)
        if world.task_completed(): break
    outcome = Outcome(world.task_completed(), verified=True, answer=...)
    memory.consolidate(task.id, turns, outcome)             # REFLECT → recipes + lessons
```

## 9. Measurement Protocol

- Fixed **20-task dev subset**. Each change runs under a fresh `APPWORLD_EXPERIMENT`
  (`pingrishabh_devN`) → `appworld evaluate <exp> dev` → log **TGC (primary) + SGC**.
- Maintain a phase→TGC table. No improvement is claimed without before/after on the same tasks
  (CLAUDE.md Rule #4).
- Submission: full `test_normal` on Groq; verify `evaluations/test_normal.json` + `tasks/<id>/dbs/`.

## 10. Constraints → Ground-Rule Mapping

| Ground rule (CLAUDE.md) | How ARCH satisfies it |
|-------------------------|-----------------------|
| #1 LLM-agnostic, scored on Groq | single `call_llm` over litellm; Groq fixed for scoring |
| #2 Optimize TGC first | roles + memory chosen for TGC; measured each phase |
| #3 Don't break the run loop | per-task try/except; defensive parsing; resume; 429 retries |
| #4 Measure before claiming | per-phase A/B on the 20-task subset |
| #5 Respect benchmark rules | memory stores environment knowledge, never task answers; self-verify, no peeking |
| Grading: state-based, self-verify | SELF-VERIFY role re-reads state before `complete_task` |

## 11. File Map

```
agent.py    # orchestrator + call_llm boundary + SYSTEM_PROMPT + token knobs
roles.py    # plan / act / self_verify / reflect
memory.py   # Memory seam + HydraDBMemory (+ NullMemory fallback)
context.py  # token-budgeted assembly
apidocs.py  # offline API-retrieval index (BM25 over the 457 docs; the #1 token saver)
run.py      # task loop, checkpoint/resume, eval helper
```

## 12. Open Items / Prerequisites

- **HydraDB cloud access** (SDK package name, signatures, API key) — request from organizers; gates
  full memory. System runs with `NullMemory` until then.
- **Valid `GROQ_API_KEY`** — current key returns 401; needed for any run/measurement.
- Optional **BM25 re-rank** over API docs — added only if measured retrieval recall is poor.
- Optional **fresh-context verifier** — not included; inline `self_verify` covers verification.
