"""://agent_arena — AppWorld agent (orchestrator).

See ARCH.md for the full design. Per task the orchestrator runs:
  retrieve(memory) -> PLAN -> ReAct loop [ACT -> observe -> error-recovery]
  -> SELF-VERIFY (gate before complete_task) -> REFLECT/consolidate(memory).

Boundaries (CLAUDE.md Rule #1):
  - LLM:    `call_llm` over litellm; scored runs use groq/llama-3.3-70b-versatile.
  - Memory: HydraDB behind the Memory seam (memory.py).

Run:
  export GROQ_API_KEY=...  HYDRADB_API_KEY=...   # or put them in .env
  export APPWORLD_EXPERIMENT=pingrishabh
  export APPWORLD_DATASET=dev MAX_TASKS=20
  python agent.py
"""

import os

try:  # optional: load keys from a local .env
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import litellm
from appworld import AppWorld

import context
import roles
import run
from memory import build_memory, Turn, Outcome

# ---- config ---------------------------------------------------------------
MODEL = os.environ.get("MODEL", "groq/llama-3.3-70b-versatile")
DATASET = os.environ.get("APPWORLD_DATASET", "dev")
EXPERIMENT = os.environ.get("APPWORLD_EXPERIMENT", "team_demo")
MAX_INTERACTIONS = int(os.environ.get("MAX_INTERACTIONS", "30"))
MAX_TASKS = int(os.environ.get("MAX_TASKS", "0"))            # 0 = all tasks in split
RESUME = os.environ.get("RESUME", "1") != "0"               # skip already-run tasks
CONTEXT_BUDGET = int(os.environ.get("CONTEXT_BUDGET", "7000"))
MAX_VERIFY_REJECTIONS = int(os.environ.get("MAX_VERIFY_REJECTIONS", "2"))

SYSTEM_PROMPT = """You are an autonomous coding agent operating inside AppWorld.
You complete the supervisor's task by writing Python code that the environment executes.

RULES:
- Reply with EXACTLY ONE Python code block per turn, nothing else:
  ```python
  # your code
  ```
- A preloaded object `apis` is the ONLY way to interact with the apps. Whatever
  you print() is returned to you as the next observation.
- You do NOT know the APIs in advance. Discover them at runtime:
    print(apis.api_docs.show_app_descriptions())
    print(apis.api_docs.show_api_descriptions(app_name='<app>'))
    print(apis.api_docs.show_api_doc(app_name='<app>', api_name='<api>'))
- To act on the supervisor's accounts, get credentials and log in:
    print(apis.supervisor.show_account_passwords())
    # then call that app's login API to get an access_token, and pass it onward.
- Work in small steps: inspect results before the next action. Never invent API
  names or fields — look them up first.
- Grading is state-based: leaving the apps' databases in exactly the right state
  is what passes. Avoid unintended side effects (wrong payee, spam, etc.).
- When the task is FULLY done, do NOT call apis.supervisor.complete_task yourself.
  Instead reply with a single line and no code:
    FINISH: <answer or NONE>
  (give <answer> only for question tasks; otherwise NONE). A verifier checks your
  work and completes the task for you.
"""


def call_llm(messages: list[dict], system: str = SYSTEM_PROMPT, max_tokens: int = 1500) -> str:
    resp = litellm.completion(
        model=MODEL,
        messages=[{"role": "system", "content": system}, *messages],
        max_tokens=max_tokens,
        num_retries=8,   # ride out free-tier rate limits (429) with backoff
    )
    return resp.choices[0].message.content or ""


def _build_head(instruction: str, supervisor: str, plan_text: str, retrieved: str) -> str:
    parts = [f"Supervisor: {supervisor}", f"Task: {instruction}"]
    if plan_text:
        parts.append(f"\nPLAN:\n{plan_text}")
    if retrieved.strip():
        parts.append(f"\nRELEVANT MEMORY (from past tasks; may be empty/irrelevant):\n{retrieved}")
    parts.append(
        "\nBegin. One python code block per turn; inspect results before acting. "
        "When fully done, reply 'FINISH: <answer or NONE>' (no code)."
    )
    return "\n".join(parts)


def solve(world: AppWorld, memory) -> Outcome:
    task = world.task
    instruction = task.instruction
    sup = task.supervisor
    supervisor = f"{getattr(sup, 'first_name', '')} {getattr(sup, 'last_name', '')} " \
                 f"<{getattr(sup, 'email', '')}>".strip()

    retrieved_items = memory.retrieve(instruction, k=6)
    retrieved = "\n".join(f"- {it.text}" for it in retrieved_items)[:2000]
    plan_text = roles.plan(call_llm, instruction, supervisor, retrieved)
    head = _build_head(instruction, supervisor, plan_text, retrieved)

    turns: list[Turn] = []
    msg_turns: list[dict] = []
    verify_rejections = 0
    verified_ok = False
    answer = None

    for step in range(MAX_INTERACTIONS):
        messages = context.assemble(MODEL, head, msg_turns, budget=CONTEXT_BUDGET)
        reply = call_llm(messages)

        is_finish, ans = roles.parse_finish(reply)
        if is_finish:
            verified, hint = roles.self_verify(call_llm, instruction, turns)
            if verified or verify_rejections >= MAX_VERIFY_REJECTIONS:
                verified_ok = verified
                answer = ans
                out = world.execute(f"apis.supervisor.complete_task(answer={ans!r})")
                print(f"  step {step+1}: FINISH (verified={verified}) -> {str(out)[:80]!r}")
                msg_turns.append({"assistant": reply, "user": f"Execution output:\n{out}"})
                break
            verify_rejections += 1
            note = (f"NOT VERIFIED. Before finishing, re-read the relevant state with read-only "
                    f"API calls and confirm: {hint or 'that every required side effect landed.'} "
                    f"Then continue.")
            print(f"  step {step+1}: verify rejected ({verify_rejections})")
            msg_turns.append({"assistant": reply, "user": note})
            turns.append(Turn(step, "verify-rejected", "", note, None))
            continue

        code = roles.extract_code(reply)
        out = world.execute(code)
        out_s = str(out)
        err = out_s if out_s.startswith("Execution failed. Traceback:") else None
        turns.append(Turn(step, roles.guess_intent(reply), code, out_s, err))
        msg_turns.append({"assistant": reply, "user": f"Execution output:\n{out_s}"})
        print(f"  step {step+1}: ran {len(code)} chars -> {out_s[:100]!r}")

        if err:  # error recovery (ARCH.md §5/§8)
            fix_items = memory.retrieve(err[:500], k=3)
            if fix_items:
                tip = "\n".join(f"- {it.text}" for it in fix_items)[:800]
                msg_turns.append({"assistant": "", "user": f"Hint from past experience:\n{tip}"})

        if world.task_completed():  # model completed directly (fallback path)
            print("  ✓ task_completed (direct)")
            break

    completed = world.task_completed()
    if not completed and verify_rejections < MAX_INTERACTIONS:
        print("  ✗ ended without completion")
    outcome = Outcome(completed=completed, verified=verified_ok, answer=answer)
    try:
        memory.remember(task.id, instruction, turns, outcome)
    except Exception:
        pass
    return outcome


def main() -> None:
    memory = build_memory()
    task_ids = run.select_task_ids(DATASET, MAX_TASKS, EXPERIMENT, RESUME)
    print(f"Running '{EXPERIMENT}' on {len(task_ids)} '{DATASET}' tasks with {MODEL}")
    for i, task_id in enumerate(task_ids, 1):
        print(f"[{i}/{len(task_ids)}] {task_id}")
        try:  # never let one task kill the whole run
            with AppWorld(task_id=task_id, experiment_name=EXPERIMENT) as world:
                solve(world, memory)
        except Exception as e:
            print(f"  ! error: {e}")
    try:
        memory.close()
    except Exception:
        pass
    print(f"\nDone. Outputs in ./experiments/outputs/{EXPERIMENT}/")
    print(f"Evaluate with:  appworld evaluate {EXPERIMENT} {DATASET}")


if __name__ == "__main__":
    main()
