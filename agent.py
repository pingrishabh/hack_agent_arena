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

import apidocs
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
CONTEXT_BUDGET = int(os.environ.get("CONTEXT_BUDGET", "4500"))
MAX_VERIFY_REJECTIONS = int(os.environ.get("MAX_VERIFY_REJECTIONS", "2"))
# Token-saving knobs (see ARCH.md §6 / token budget):
OBS_CAP = int(os.environ.get("OBS_CAP", "1200"))            # max chars per observation re-fed to model
API_TOPK = int(os.environ.get("API_TOPK", "12"))           # API signatures injected per task (offline index)
ACT_MAX_TOKENS = int(os.environ.get("ACT_MAX_TOKENS", "900"))
USE_PLAN = os.environ.get("USE_PLAN", "1") != "0"
USE_VERIFY = os.environ.get("USE_VERIFY", "1") != "0"

SYSTEM_PROMPT = """You are an autonomous coding agent in AppWorld. Solve the supervisor's task by \
writing Python that calls the apps via the preloaded `apis` object.

- Reply with EXACTLY ONE ```python code block per turn (or the FINISH line). Whatever you print() \
becomes the next observation.
- Relevant API signatures are provided below — prefer them. Only if you need an API not listed, call \
apis.api_docs.show_api_descriptions(app_name=...) or show_api_doc(app_name=..., api_name=...).
- Log in when needed: apis.supervisor.show_account_passwords(), then the app's login API to get an \
access_token; pass it to later calls.
- Work in small steps; inspect results before acting. Never invent API names/fields.
- Grading is state-based: leave the databases in exactly the right state; avoid wrong or extra side \
effects.
- When FULLY done, do NOT call complete_task yourself — reply a single line, no code:
  FINISH: <answer or NONE>   (answer only for question tasks). A verifier checks your work.
"""


def call_llm(messages: list[dict], system: str = SYSTEM_PROMPT, max_tokens: int = 900) -> str:
    resp = litellm.completion(
        model=MODEL,
        messages=[{"role": "system", "content": system}, *messages],
        max_tokens=max_tokens,
        num_retries=8,   # ride out free-tier rate limits (429) with backoff
    )
    return resp.choices[0].message.content or ""


def _truncate_obs(text: str, cap: int = OBS_CAP) -> str:
    """Cap an observation before re-feeding it (big API responses get re-sent every turn)."""
    if len(text) <= cap:
        return text
    head = cap * 2 // 3
    return text[:head] + f"\n…[{len(text) - cap} chars truncated]…\n" + text[-(cap - head):]


def _build_head(instruction: str, supervisor: str, plan_text: str,
                retrieved: str, api_sigs: list[str]) -> str:
    parts = [f"Supervisor: {supervisor}", f"Task: {instruction}"]
    if api_sigs:
        parts.append("\nAVAILABLE APIS (already looked up — use these; only call api_docs for "
                     "something not listed):\n" + "\n".join(api_sigs))
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

    # Offline API retrieval (no LLM tokens) — inject only the relevant signatures
    # instead of letting the agent dump api_docs into context every task.
    try:
        api_sigs = apidocs.get_index(world.apis).retrieve(instruction, k=API_TOPK)
    except Exception:
        api_sigs = []

    retrieved_items = memory.retrieve(instruction, k=6)
    retrieved = "\n".join(f"- {it.text}" for it in retrieved_items)[:1500]
    plan_text = roles.plan(call_llm, instruction, supervisor, retrieved) if USE_PLAN else ""
    head = _build_head(instruction, supervisor, plan_text, retrieved, api_sigs)

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
            if USE_VERIFY:
                verified, hint = roles.self_verify(call_llm, instruction, turns)
            else:
                verified, hint = True, ""
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
        obs = _truncate_obs(out_s)  # cap big API responses before re-feeding
        turns.append(Turn(step, roles.guess_intent(reply), code, obs, err))
        msg_turns.append({"assistant": reply, "user": f"Execution output:\n{obs}"})
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
