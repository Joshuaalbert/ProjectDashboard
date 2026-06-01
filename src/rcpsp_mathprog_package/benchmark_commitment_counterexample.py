"""Benchmark the commitment-window MCTS counterexample.

Run from the package directory with:

    PYTHONPATH=. python benchmark_commitment_counterexample.py /tmp/out

The script constructs a small exact-searchable project that respects the
single-day context principle.  It then compares the raw commitment heuristic to
commitment-window MCTS with ten complete terminal rollouts per legal root action.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from .rcpsp_commitment_mcts import (
    CommitmentMCTSOptions,
    CommitmentMCTSPlanner,
    make_single_day_context_counterexample,
    write_commitment_result_csvs,
)


def main(output_dir: str = "commitment_counterexample_report") -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    problem = make_single_day_context_counterexample()

    t0 = time.perf_counter()
    exact = CommitmentMCTSPlanner(CommitmentMCTSOptions(use_mcts=False)).exact_optimal_makespan(
        problem,
        include_idle_when_work_legal=True,
    )
    exact_wall = time.perf_counter() - t0

    raw_planner = CommitmentMCTSPlanner(CommitmentMCTSOptions(use_mcts=False))
    t0 = time.perf_counter()
    raw = raw_planner.solve_raw_heuristic(problem)
    raw_wall = time.perf_counter() - t0

    mcts_options = CommitmentMCTSOptions(
        use_mcts=True,
        complete_rollouts_per_action=10,
        rollout_transition_limit=5000,
        allow_idle_when_work_legal=False,
    )
    mcts_planner = CommitmentMCTSPlanner(mcts_options)
    t0 = time.perf_counter()
    mcts = mcts_planner.solve(problem)
    mcts_wall = time.perf_counter() - t0

    write_commitment_result_csvs(raw, out / "raw")
    write_commitment_result_csvs(mcts, out / "mcts")

    rows = [
        {
            "case": "single_day_context_counterexample",
            "exact_optimal_makespan_h": exact,
            "raw_heuristic_makespan_h": raw.objective_makespan,
            "mcts_makespan_h": mcts.objective_makespan,
            "raw_minus_mcts_h": raw.objective_makespan - mcts.objective_makespan,
            "raw_wall_seconds": raw_wall,
            "mcts_wall_seconds": mcts_wall,
            "exact_wall_seconds": exact_wall,
            "searched_decisions": mcts_planner.stats.searched_decisions,
            "root_actions_evaluated": mcts_planner.stats.root_actions_evaluated,
            "complete_rollouts": mcts_planner.stats.complete_rollouts,
            "complete_rollouts_per_action": mcts_options.complete_rollouts_per_action,
            "rollout_calls_including_baseline": mcts_planner.stats.rollout_calls,
        }
    ]

    with (out / "results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    (out / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")

    report = []
    report.append("# Commitment-window MCTS counterexample")
    report.append("")
    report.append("This instance respects the single-day context principle: one action commits a resource to a process-role until either the role requirement is complete or the current consecutive availability window ends.")
    report.append("")
    report.append("## Project")
    report.append("")
    report.append("Resources:")
    report.append("- `F`: flexible, can perform roles `X` and `Y` on days 1, 2, and 3.")
    report.append("- `SX`: specialist, can perform role `X` only on day 1.")
    report.append("")
    report.append("Processes:")
    report.append("- `A`: requires 8 h of `X` and precedes `C`.")
    report.append("- `B`: requires 8 h of `Y` and has no successor.")
    report.append("- `C`: requires 8 h of `Y` after `A`.")
    report.append("")
    report.append("The CPM-like raw heuristic picks `F -> A-X` first because `A` has downstream work. That strands the day-1-only `SX` specialist and delays one `Y` task to day 3. The MCTS rollout comparison tries `F -> B-Y`; then `SX -> A-X` can happen in parallel on day 1, and `F -> C-Y` completes on day 2.")
    report.append("")
    report.append("## Results")
    report.append("")
    report.append("| Metric | Value |")
    report.append("|---|---:|")
    report.append(f"| Exact optimal makespan | {exact:.1f} h |")
    report.append(f"| Raw heuristic makespan | {raw.objective_makespan:.1f} h |")
    report.append(f"| MCTS makespan | {mcts.objective_makespan:.1f} h |")
    report.append(f"| Raw - MCTS | {raw.objective_makespan - mcts.objective_makespan:.1f} h |")
    report.append(f"| Raw wall time | {raw_wall:.6f} s |")
    report.append(f"| MCTS wall time | {mcts_wall:.6f} s |")
    report.append(f"| Exact exhaustive-search wall time | {exact_wall:.6f} s |")
    report.append(f"| Searched decisions | {mcts_planner.stats.searched_decisions} |")
    report.append(f"| Root actions evaluated | {mcts_planner.stats.root_actions_evaluated} |")
    report.append(f"| Complete terminal rollouts | {mcts_planner.stats.complete_rollouts} |")
    report.append(f"| Complete rollouts per action | {mcts_options.complete_rollouts_per_action} |")
    report.append("")
    report.append("## Raw schedule")
    report.append("")
    for a in raw.resource_process_assignments:
        if a.bucket_start in (0.0, 24.0, 48.0):
            report.append(f"- start {a.bucket_start:>4.0f} h: `{a.resource}` works `{a.process}-{a.role}`")
    report.append("")
    report.append("## MCTS schedule")
    report.append("")
    for a in mcts.resource_process_assignments:
        if a.bucket_start in (0.0, 24.0, 48.0):
            report.append(f"- start {a.bucket_start:>4.0f} h: `{a.resource}` works `{a.process}-{a.role}`")
    report.append("")
    (out / "report.md").write_text("\n".join(report), encoding="utf-8")


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "commitment_counterexample_report")
