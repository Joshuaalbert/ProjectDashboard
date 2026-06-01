from pathlib import Path
from rcpsp_mathprog import PlanningProblem, ResourceConstrainedCPMSolver

problem = PlanningProblem(
    roles=["dev", "qa"],
    resources=["alice", "bob"],
    processes=["A", "B", "C"],
    buckets=list(range(1, 9)),
    requirements={
        ("dev", "A"): 2,
        ("qa", "A"): 1,
        ("dev", "B"): 2,
        ("qa", "C"): 2,
    },
    predecessors={
        "B": ["A"],
        "C": ["A"],
    },
    # Optional release dates / earliest allowed process starts, in the
    # same coordinate system as bucket_start/bucket_end. With default
    # one-hour buckets, bucket 1 starts at 0, bucket 4 starts at 3.
    earliest_start={"C": 3.0},
    availability={
        (r, role, t): 1.0
        for r in ["alice", "bob"]
        for role in ["dev", "qa"]
        for t in range(1, 9)
    },
)

solver = ResourceConstrainedCPMSolver()

# Write files for review even if GLPK is not installed locally.
paths = solver.write_mathprog_files(problem, Path(__file__).with_name("mathprog_debug"))
print(paths)

# Solve when glpsol is installed:
# result = solver.solve(problem, keep_files=True)
# print(result.objective_makespan)
# print(result.process_timings)
# print(result.critical_path)
