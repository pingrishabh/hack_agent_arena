"""Run harness helpers (ARCH.md §7): checkpoint/resume + evaluation.

Pure functions imported by agent.py — this module does not import agent.py.
"""

from __future__ import annotations

import os


def _tasks_dir(experiment: str) -> str:
    try:
        from appworld import path_store
        base = path_store.experiment_outputs
    except Exception:
        base = os.path.join("experiments", "outputs")
    return os.path.join(base, experiment, "tasks")


def completed_task_ids(experiment: str) -> set[str]:
    """Task ids already run for this experiment (their output dir exists).
    Used to skip finished tasks on resume / multi-day runs / crash recovery."""
    d = _tasks_dir(experiment)
    if not os.path.isdir(d):
        return set()
    done = set()
    for tid in os.listdir(d):
        if os.path.isdir(os.path.join(d, tid, "dbs")):
            done.add(tid)
    return done


def select_task_ids(dataset: str, max_tasks: int, experiment: str, resume: bool) -> list[str]:
    from appworld import load_task_ids
    ids = load_task_ids(dataset)
    if max_tasks:
        ids = ids[:max_tasks]
    if resume:
        done = completed_task_ids(experiment)
        ids = [t for t in ids if t not in done]
    return ids


def evaluate(experiment: str, dataset: str) -> dict:
    """Run AppWorld's evaluator and return the aggregate metrics dict
    ({'task_goal_completion': .., 'scenario_goal_completion': ..})."""
    from appworld import evaluate_dataset
    report = evaluate_dataset(experiment_name=experiment, dataset_name=dataset,
                              suppress_errors=True, print_report=True)
    if isinstance(report, dict):
        return report.get("aggregate", report)
    return {"raw": report}
