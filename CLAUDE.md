# CLAUDE.md — Requirements & Ground Rules

> These are binding. Everything in this repo MUST follow the rules below.
> This is a living document — we add requirements and ground rules here over time.

## Goal

Build an agent (**LLM-agnostic**) that performs **better than others on this benchmark**
([AppWorld](https://appworld.dev)). Success is measured by **Task Goal Completion (TGC)** —
the percentage of tasks fully completed. Beat the reference baseline (ReAct + GPT-4o ≈ **48.8 TGC**),
then beat everyone else.

## Ground Rules

1. **LLM-agnostic, but scored on a fixed model.** The agent must never hard-depend on one
   provider. All model access goes through a single swappable boundary (`call_llm` in `agent.py`,
   backed by **litellm** — `MODEL` is a `provider/model` string). Any backend is fine for local
   dev, BUT **scored/submitted runs MUST use `groq/llama-3.3-70b-versatile`** (organizer mandate
   for fair comparison). Switching backends must stay a config change — not a code rewrite.
2. **Optimize for TGC first.** Every change is judged by whether it raises task completion on the
   official split. Cleverness that doesn't move TGC is not a priority.
3. **Don't break the run loop.** One task failing must never kill the whole run. Robustness
   (error recovery, malformed-output handling, turn limits) is part of the score, not optional.
4. **Measure before claiming.** Report TGC/SGC from actual `appworld evaluate` output. Never assert
   an improvement without a before/after number on the same tasks.
5. **Respect the benchmark rules.** No hard-coding task answers, no peeking at eval/ground-truth
   data, no shortcuts that wouldn't generalize. The agent must solve tasks by reasoning + API use.

## How AppWorld Grades Us

- **State-based, not code-based.** Grading inspects the **final database state** of the 9 apps
  (plus the returned `answer` for question tasks). Any code path that leaves the world in the
  correct state passes — there is no single "right" solution.
- **`task_completed()` ≠ correct.** It only means the agent called `complete_task`. Correctness is
  decided later by hidden checks → **the agent must self-verify** (re-read state, confirm the side
  effect landed) *before* calling `complete_task`.
- **No collateral damage.** Checks penalize unintended side effects (paying the wrong person,
  spamming). Be precise, not just "eventually right."
- **Run and scoring are decoupled.** `python agent.py` saves outputs + final DBs; `appworld
  evaluate $EXPERIMENT <split>` re-reads those DBs offline and computes the metrics. This is why
  the submission MUST include `tasks/<id>/dbs/`.
- **Metrics:** **TGC** (primary) = % of tasks where *all* checks pass (all-or-nothing per task).
  **SGC** = finer-grained scenario completion, used as the tiebreaker.

## Project Facts (context, not rules)

- **Benchmark:** AppWorld — 9 apps, 457 APIs, ~100 simulated people. Agent reads a supervisor
  instruction and acts by writing Python that calls the apps' APIs.
- **Entry point:** `agent.py` (a working ReAct code agent). Run with `python agent.py`.
- **Splits:** `dev` (build/debug), `test_normal` (168 tasks, submission), `test_challenge`.
- **Submission:** zip `experiments/outputs/$APPWORLD_EXPERIMENT/` (must include
  `evaluations/test_normal.json` and `tasks/<id>/dbs/`).
- **Bonus:** integrating **HydraDB** earns extra credit.
- **Architecture:** the binding end-to-end design lives in **`ARCH.md`** — the agent we implement
  against (boundaries, HydraDB memory model, reasoning roles, per-turn flow). Keep it in sync with
  these rules; if a rule here changes, update `ARCH.md`.

## Current Setup

- LLM access: **litellm** (`call_llm` in `agent.py`). Default + mandated scored model:
  **`groq/llama-3.3-70b-versatile`** (free, cloud, no billing). Key in `.env` as `GROQ_API_KEY`.
  Other backends (`gemini/…`, `anthropic/…`, `ollama/…`) allowed for dev only.
- Secrets live in `.env` (gitignored) — NEVER in `.env.example` (tracked).
- Remotes: `origin` = your fork (`pingrishabh/hack_agent_arena`), `upstream` = the original.
