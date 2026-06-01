# Resource-constrained CPM via MathProg + GLPK

This package implements a resource-constrained project planning model that is close to CPM, but does not require process durations as input. Durations emerge from role-hour requirements, resource calendars, and finish-to-start dependencies.

The module writes a GNU MathProg model, calls `glpsol`, parses the solution, and returns Python dataclasses containing role work, resource-role allocation, reconstructed resource-process-role assignments, CPM-like time windows, slack, and a critical-path diagnostic.

## Files

- `rcpsp_mathprog.py` — the module.
- `example.py` — small example and MathProg-file writer.
- `README.md` — derivation, semantics, and maintenance notes.

## Installation/runtime assumptions

You need Python 3.10+ and GLPK's `glpsol` executable on the PATH.

No Python optimization package is required. The Python module only uses the standard library. It delegates optimization to GLPK by writing `model.mod` and `data.dat` files.

Typical system installation commands are environment-specific, for example:

```bash
# Debian/Ubuntu
sudo apt-get install glpk-utils

# macOS with Homebrew
brew install glpk
```

## Basic usage

```python
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
    availability={
        (resource, role, t): 1.0
        for resource in ["alice", "bob"]
        for role in ["dev", "qa"]
        for t in range(1, 9)
    },
)

solver = ResourceConstrainedCPMSolver(glpsol="glpsol")
result = solver.solve(problem, keep_files=True)

print(result.objective_makespan)
print(result.process_timings)
print(result.critical_path)
print(result.resource_process_assignments)
```

To write MathProg files without solving:

```python
paths = solver.write_mathprog_files(problem, "./mathprog_debug")
print(paths.model_path)
print(paths.data_path)
```

## Input semantics

### Time buckets

The model is time-indexed. You must choose a finite ordered horizon:

```python
buckets = [1, 2, ..., H]
```

If the horizon is too short, the model is infeasible. That should be interpreted as "this horizon is not long enough," not necessarily as "the project is impossible." In production, use a horizon from a greedy topological schedule or run an outer search over candidate horizons.

`bucket_hours[t]` defaults to 1. If omitted, bucket `t` has one hour of length. `bucket_start` and `bucket_end` are optional display coordinates; if omitted, they are computed cumulatively from `bucket_hours`.

### Requirements

`requirements[(role, process)]` is the number of role-hours required by a process:

\[
R_{ip} \ge 0.
\]

Omitted entries are zero.

### Availability

`availability[(resource, role, bucket)]` is a fraction in `[0, 1]` saying how much of that bucket resource `j` may spend on role `i`.

Internally the module converts this to an hour upper bound:

\[
U_{jit} = \text{availability}_{jit} \cdot \Delta_t.
\]

A multiskilled person may have availability 1 for several roles in the same bucket. That does not over-allocate them, because total assigned work is separately constrained by the resource capacity constraint.

### Resource capacity

`resource_capacity[(resource, bucket)]` is the total fraction of a bucket the resource may work, regardless of role. It defaults to 1.

Internally:

\[
B_{jt} = \text{resource\_capacity}_{jt} \cdot \Delta_t.
\]

A resource can multitask fractionally across roles/processes, but only up to this total capacity.

### Dependencies

`predecessors[p] = [q1, q2, ...]` means every predecessor must be complete before any work on `p` can occur.

This is strict finish-to-start precedence on the chosen time grid.

## Formulation

Sets:

- \(I\): roles
- \(J\): resources
- \(P\): processes
- \(T\): ordered time buckets

Data:

- \(R_{ip}\): required role-hours
- \(U_{jit}\): resource-role availability in hours for bucket \(t\)
- \(B_{jt}\): total resource capacity in hours for bucket \(t\)
- \(\theta_t\): end time of bucket \(t\)
- \(\operatorname{Pred}(p)\): immediate predecessors of process \(p\)

Variables:

- \(h_{ipt} \ge 0\): role-\(i\) hours assigned to process \(p\) in bucket \(t\)
- \(g_{ipt} \ge 0\): cumulative role-\(i\) hours delivered to process \(p\) by the end of bucket \(t\)
- \(u_{jit} \ge 0\): resource \(j\)'s planned allocation hours on role \(i\) in bucket \(t\), aggregated across processes
- \(z_{pt} \in \{0,1\}\): 1 if process \(p\) is complete by the end of bucket \(t\)
- \(C_p\): completion time of process \(p\)
- \(C_{\max}\): project makespan

### Cumulative work

\[
g_{ipt} = g_{ip,t-1} + h_{ipt}.
\]

### Requirements

\[
\sum_t h_{ipt} = R_{ip} \qquad \forall i,p.
\]

### Role balance

\[
\sum_p h_{ipt} = \sum_j u_{jit} \qquad \forall i,t.
\]

This says role-work demand in a bucket must be supplied by resources assigned to that role in that bucket.

### Resource capacity

\[
\sum_i u_{jit} \le B_{jt} \qquad \forall j,t.
\]

This is the multitasking constraint. A person can split time fractionally across roles, but their total load cannot exceed their calendar capacity.

### Role-specific availability

\[
u_{jit} \le U_{jit} \qquad \forall j,i,t.
\]

### Completion flags

A process can be marked complete only if all required roles are complete:

\[
g_{ipt} \ge R_{ip} z_{pt} \qquad \forall i,p,t \text{ with } R_{ip} > 0.
\]

The completion flags are monotone:

\[
z_{p,t-1} \le z_{pt}.
\]

Every process must complete by the horizon:

\[
z_{pH} = 1.
\]

### Strict finish-to-start precedence

For every predecessor \(q \in \operatorname{Pred}(p)\):

\[
g_{ipt} \le R_{ip} z_{q,t-1}
\qquad
\forall i,p,t \text{ with } R_{ip} > 0.
\]

If \(q\) is not complete by the previous bucket, no cumulative work on \(p\) can exist by the current bucket. Once \(q\) is complete, the constraint stops binding.

The model also constrains process completion flags by predecessor completion, which is important for zero-work or dummy processes:

\[
z_{pt} \le z_{q,t-1}.
\]

### Completion time

Because \(z_{pt}\) is monotone and binary, the transition \(z_{pt}-z_{p,t-1}\) is 1 at the bucket where \(p\) completes. Therefore:

\[
C_p = \sum_t \theta_t (z_{pt}-z_{p,t-1}).
\]

Project makespan:

\[
C_{\max} \ge C_p \qquad \forall p.
\]

Primary objective:

\[
\min C_{\max}.
\]

## Why this formulation

Classical CPM is an LP because durations are fixed input data. Here, process durations are not known. They emerge from role-hour requirements and resource calendars. A duration-first formulation would need to infer expressions like:

\[
\max\{t : h_{ipt} > 0\},
\]

which is a support-detection problem. Support detection is not exact with only continuous variables unless you accept proxies or introduce binaries.

This formulation avoids explicit duration variables. It asks a simpler question:

> By the end of bucket \(t\), has process \(p\) accumulated all required role-work?

That requires only \(|P|\cdot|T|\) binary variables, one completion flag per process/time. The larger assignment part stays continuous.

## What is exact and what is approximate

### Exact on the chosen grid

- Role-hour satisfaction.
- Fractional resource capacity.
- Multiskilled resource calendars.
- Strict finish-to-start precedence between buckets.
- Makespan minimization.
- Process completion times \(C_p\), EF, and LF diagnostics based on completion flags.

### Modeling assumptions

- Work is fractional and preemptive.
- A process does not need to be worked continuously once started.
- Resources may multitask fractionally, subject to total capacity.
- Time is discrete by bucket.

### Not modeled exactly

- Nonpreemptive contiguous process execution.
- Minimum assignment quanta.
- Exact support-based start time with continuous work variables.

The last point matters. With continuous assignments, a solver can place an arbitrarily tiny amount of work in a bucket. Therefore ES and LS are inherently tolerance-dependent unless you introduce start/activity binaries or a minimum work quantum.

## ES, EF, LS, LF implementation

The module computes a primary optimal schedule in two phases:

1. Minimize makespan.
2. Fix makespan and maximize \(\sum_{p,t} z_{pt}\), which reports completions as early as possible within the optimal makespan.

Then it reports process timing dataclasses.

When `compute_windows=False`, ES/EF/LS/LF are derived from the returned schedule:

- ES: first bucket with more than `epsilon` assigned work for the process.
- EF: completion time from the completion flag transition.
- LS: set equal to ES.
- LF: set equal to EF.
- slack: zero.

When `compute_windows=True`, the module runs additional diagnostic MILPs for each process:

- EF: minimize \(C_p\) with optimal \(C_{\max}\) fixed.
- LF: maximize \(C_p\) with optimal \(C_{\max}\) fixed.
- ES: find the earliest bucket by which at least `epsilon` work can be placed while preserving optimal \(C_{\max}\).
- LS: find the latest bucket before which all work on the process can be held at zero while preserving optimal \(C_{\max}\).

So EF/LF are exact on the grid. ES/LS are support-based diagnostics controlled by `epsilon`.

## Critical path interpretation

In ordinary CPM, criticality is usually a path of zero-slack activities connected by binding precedence constraints.

In a resource-constrained schedule, the bottleneck may be:

- a precedence chain,
- a scarce role,
- a specific resource calendar,
- a multiskilled-resource capacity interaction,
- or a combination of those.

The module therefore returns:

- `critical_processes`: processes with near-zero slack,
- `critical_edges`: zero-gap predecessor edges between critical processes,
- `critical_path`: the longest path through those critical edges,
- `binding_resource_times`: resource/time buckets at capacity,
- `binding_role_times`: role/time buckets where simple role-calendar capacity is saturated.

Treat `critical_path` as a CPM-style diagnostic, not as the complete explanation of delay in a resource-constrained problem. The resource binding lists are often equally important.

## Returned dataclasses

`SolveResult` fields:

- `status`: MathProg output status marker.
- `objective_makespan`: optimal project makespan.
- `role_assignments`: list of `(role, process, bucket, hours)` rows from \(h_{ipt}\).
- `resource_role_assignments`: list of `(resource, role, bucket, hours)` rows from \(u_{jit}\).
- `resource_process_assignments`: reconstructed `(resource, role, process, bucket, hours)` rows.
- `process_timings`: mapping process -> `ProcessTiming` with ES/EF/LS/LF/slack.
- `critical_processes`, `critical_edges`, `critical_path`.
- `binding_resource_times`, `binding_role_times`.
- `raw_work_h`, `raw_resource_role_u`: dictionaries for direct downstream processing.
- `solver_stdout`, `solver_stderr`: captured GLPK output.
- `mathprog_model_path`, `mathprog_data_path`, `solution_output_path`: present when `keep_files=True`.

## Reconstructing resource-to-process assignments

The optimization model deliberately aggregates resource assignment and process assignment:

- \(h_{ipt}\): process-role work demand by bucket.
- \(u_{jit}\): resource-role supply by bucket.

It does not carry \(x_{ijpt}\) in the MILP, because that would add \(|I||J||P||T|\) continuous variables.

After solving, the module reconstructs detailed resource-process-role assignments by solving each role/time matching greedily:

\[
\sum_j x_{ijpt} = h_{ipt},
\]

\[
\sum_p x_{ijpt} = u_{jit}.
\]

Since role balance already guarantees total demand equals total supply for each role/time, this reconstruction is independent across buckets and does not affect optimality.

## Computational considerations

The core MILP has:

- binary variables: \(|P||T|\),
- continuous work variables: \(|I||P||T| + |I||J||T|\),
- no binary assignment variables.

This is usually much smaller than a direct model with binary resource/process/activity variables.

`compute_windows=True` is more expensive because it runs additional models per process. For large instances, use:

```python
result = solver.solve(problem, compute_windows=False)
```

and compute detailed windows only for selected processes later, or extend the module with targeted diagnostics.

## Horizon strategy

The model requires a finite horizon. A practical approach is:

1. Build a greedy feasible topological schedule and use its end as an upper bound.
2. Use that as `buckets`.
3. If the MILP is infeasible, extend the horizon.
4. Optionally use a doubling search followed by binary search over horizon length.

Lower bounds can be obtained from:

- precedence-only CPM with optimistic durations,
- aggregate role capacity checks,
- total workload divided by total calendar capacity,
- role-specific workload divided by role-specific calendar capacity.

## Maintenance notes

### If you need nonpreemptive tasks

Add activity/start binaries. This turns the model into a more classical RCPSP formulation and will be harder computationally.

### If you need exact ES/LS starts

Introduce start/activity indicators or a minimum work quantum. Without that, continuous fractional assignments make exact support detection impossible.

### If you need named resource-to-process choices inside optimization

Replace the aggregate \(h/u\) split with explicit variables:

\[
x_{ijpt} \ge 0.
\]

Then:

\[
\sum_{j,t} x_{ijpt} = R_{ip},
\]

\[
\sum_{i,p} x_{ijpt} \le B_{jt},
\]

\[
\sum_p x_{ijpt} \le U_{jit}.
\]

This is still continuous, but larger. The current module chooses the smaller aggregate model and reconstructs detailed assignments afterward.

### If you want a pure LP relaxation

Relax:

\[
z_{pt} \in \{0,1\}
\]

to:

\[
0 \le z_{pt} \le 1.
\]

The interpretation changes. Precedence becomes fluid: successor progress can follow predecessor progress fractionally. That can be useful as a lower bound, but it is not strict CPM finish-to-start logic.

---

## Commitment-window MDP/MCTS formulation

The original MDP/MCTS planner asked each resource to choose a focus every time
bucket.  That is useful as a direct time-unrolled model, but it creates very long
rollouts when tasks require many hours.  The `rcpsp_commitment_mcts.py` module
uses a coarser and more realistic transition:

```text
state:  each resource has its own cursor on its calendar
act:    assign resource j to process p, role i
apply:  fill forward from j's cursor until either
        - the process-role requirement is complete, or
        - the current consecutive availability window ends
```

For example, if a resource is available from 09:00 to 17:00 and a selected
process-role has 20 hours remaining, one action writes an 8-hour block for that
resource.  If the process-role has only 7 hours remaining, the block ends at
16:00 and the same resource can make another commitment decision for the
remaining hour of that day.  This is the "single day context" model: the planner
commits people to focus blocks instead of micromanaging hour-by-hour context
switches.

### State

The commitment state contains:

- `cursor[j]`: current time of resource `j` on its own schedule;
- `rem[i,p]`: remaining hours of role `i` required by process `p`;
- `start_time[p]` and `finish_time[p]`;
- `last_commitment[j]`, used only by the heuristic prior to prefer continuing a
  previously started process-role;
- the assignment ledger `(resource, role, process, bucket) -> hours`.

A process is legal for a resource at cursor time `t` only when all predecessor
finish times are `<= t`, its earliest-start constraint is satisfied, the resource
is allowed for that process, and the resource is currently available for one of
that process's unfinished roles.

### Transition

For action `(p, i)` by resource `j`, the environment repeatedly consumes capacity
from consecutive buckets while:

```text
resource_capacity[j,t] > 0
availability[j,i,t] > 0
rem[i,p] > 0
```

It stops at the first gap or role completion.  The resource cursor advances to
the end of the committed block, not merely to the next hour.  If no useful work
is legal, idle advances the cursor to the next relevant event: a predecessor
completion/release inside the current window, the end of the current availability
window, or the next availability window.

### Reward and MCTS comparison

At a searched root decision, the planner first computes the raw heuristic
completion from the current state.  Each legal root action is then evaluated by
`complete_rollouts_per_action` complete terminal rollouts.  The terminal reward is
subtract-one-and-clip:

```text
reward = max(0, min(reward_clip_max, heuristic_makespan / rollout_makespan - 1))
```

So a rollout equal to or worse than the heuristic gets reward `0`; a rollout that
beats the heuristic gets positive reward.  The default benchmark uses 10 complete
rollouts per action.  Deterministic rollouts make repeated evaluations identical,
but the count is retained so the interface is ready for stochastic or perturbed
rollout policies.

### Counterexample regression test

`benchmark_commitment_counterexample.py` constructs a small exact-searchable
project:

- `F`: flexible resource, can do `X` and `Y` on days 1, 2, and 3;
- `SX`: specialist, can do `X` only on day 1;
- `A`: 8 h of `X`, predecessor of `C`;
- `B`: 8 h of `Y`;
- `C`: 8 h of `Y` after `A`.

The CPM-like raw heuristic chooses `F -> A-X` first because `A` has downstream
work.  That strands `SX` and pushes one `Y` task to day 3.  Exhaustive search over
the same commitment MDP proves the optimal makespan is 32 h, while the raw
heuristic gives 56 h.  Commitment-window MCTS with 10 complete rollouts per root
action chooses `F -> B-Y`, lets `SX -> A-X` run in parallel on day 1, and reaches
the 32 h optimum.

Run:

```bash
cd rcpsp_mathprog_package
PYTHONPATH=. python benchmark_commitment_counterexample.py commitment_counterexample_report
```

The script writes a Markdown report and CSVs for the raw and MCTS schedules.
