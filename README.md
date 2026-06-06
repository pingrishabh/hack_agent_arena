# ://agent_arena — Team `pingrishabh`

A general autonomous agent for [AppWorld](https://appworld.dev), submitted for the final
`agent_arena_eval` set. It reads a supervisor instruction and acts by writing Python that calls the
apps' APIs.

- **Team name:** `pingrishabh`
- **Model used:** `openrouter/meta-llama/llama-3.3-70b-instruct` (Meta Llama 3.3 70B — the mandated
  model, served via OpenRouter for throughput; the LLM boundary is litellm so the provider is a
  one-line config change).
- **HydraDB:** **yes** — used two ways: (1) the **semantic API-doc retrieval** layer (453 API
  signatures ingested as `knowledge`, queried with graph context, fused with a local BM25 floor via
  reciprocal-rank fusion), and (2) **cross-task memory** (episodic task summaries + versioned
  procedural recipes, consolidated at task end).
- **Self-reported score:** TGC `20.0` / SGC `20.0` (from `appworld evaluate` on `agent_arena_eval`;
  2/10 — the challenge set is 3 easy / 3 medium / 4 hard).
- **Integrity:** general agent — **no `task_id` hardcoding**, no per-task answers.

## Architecture (see [`ARCH.md`](ARCH.md) for the full design)

Per task the orchestrator runs a structured pipeline around a ReAct loop:

```
retrieve(memory + hybrid API index) → PLAN → ReAct loop[ACT → observe → error-recovery]
  → SELF-VERIFY (gate before complete_task) → REFLECT/consolidate(memory)
```

Key components:
- **LLM boundary** (`agent.py` `call_llm`, litellm) — swappable provider; scored on Llama 3.3 70B.
- **Hybrid API retrieval** (`apidocs.py`) — local BM25 over the 457 docs (zero-token, always-on
  floor) fused with HydraDB semantic retrieval; injects only the top-k relevant API signatures so the
  agent never burns turns on runtime `api_docs` discovery.
- **HydraDB memory** (`memory.py`) — the brain, behind a thin seam; fully defensive (a memory failure
  never crashes a run).
- **Verify-before-complete** (`agent.py`) — `complete_task` is intercepted inside the sandbox so a
  completion is *captured* and self-verified before it is actually submitted (the model often calls
  `complete_task` directly; this makes the verify gate unbypassable).
- **Deterministic API self-correction** — if the model invents a method name, the agent auto-feeds
  that app's real API names on the next turn.
- **Token discipline** (`context.py`) — token-budgeted context assembly + observation truncation.
- **Run harness** (`run.py`) — checkpoint/resume (skip completed tasks).

## How to run

```bash
bash setup.sh                 # uv + Python 3.11 + appworld + data
source .venv/bin/activate
# put keys in .env: OPENROUTER_API_KEY=...  HYDRADB_API_KEY=...

# install the eval set
mkdir -p data/datasets && cp eval/agent_arena_eval.txt data/datasets/agent_arena_eval.txt

export MODEL=openrouter/meta-llama/llama-3.3-70b-instruct
export APPWORLD_EXPERIMENT=team_pingrishabh
export APPWORLD_DATASET=agent_arena_eval MAX_TASKS=0
python agent.py

appworld evaluate team_pingrishabh agent_arena_eval   # prints TGC / SGC
```

Outputs (including each `tasks/<id>/dbs/` and `evaluations/agent_arena_eval.json`) are committed under
`experiments/outputs/team_pingrishabh/`.

## Requirements

See [`requirements.txt`](requirements.txt). Note `litellm==1.34.42` is pinned: `appworld` hard-pins
`pydantic<2`, which conflicts with newer litellm; this version coexists with pydantic v1 and supports
both Groq and OpenRouter.
