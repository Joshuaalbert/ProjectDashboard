"""
Resource-constrained CPM/RCPSP planner using GNU MathProg + GLPK.

This module writes a MathProg MILP that keeps all resource/work variables
continuous and uses binary variables only for process completion-by-time flags.
It is intended for fractional, preemptive project planning on a discrete time
calendar.

Primary public entry point:
    ResourceConstrainedCPMSolver().solve(problem)

The model assumes:
    * Role requirements are measured in hours.
    * Calendars/availability are fractional capacity per bucket.
    * Assignments may be fractional and preemptive.
    * Finish-to-start precedence is strict on the chosen time grid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import math
import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple


Key2 = Tuple[str, str]
Key3 = Tuple[str, str, int]
KeyRT = Tuple[str, int]


@dataclass(frozen=True)
class PlanningProblem:
    """Input data for the resource-constrained CPM model.

    Parameters
    ----------
    roles, resources, processes:
        Symbolic identifiers. They may contain spaces; the writer maps them to
        safe MathProg tokens internally.

    buckets:
        Ordered time bucket identifiers. These are usually integers such as
        [1, 2, ..., H]. The order of this sequence is the planning calendar.

    requirements:
        Mapping (role, process) -> required role-hours R[i,p]. Omitted entries
        are treated as zero.

    availability:
        Mapping (resource, role, bucket) -> fraction of the bucket resource j
        may spend on role i, in [0, 1]. Omitted entries are zero. Internally this
        is converted to an hour upper bound U[j,i,t] = availability * bucket_hours[t].

    predecessors:
        Mapping process -> iterable of immediate predecessor processes. A
        predecessor q of p means q must complete before any work on p can occur.

    bucket_hours:
        Mapping bucket -> bucket duration in hours. Omitted entries default to 1.

    resource_capacity:
        Mapping (resource, bucket) -> total fraction of the bucket resource j may
        work, regardless of role. Omitted entries default to 1. Internally this is
        converted to hours B[j,t] = resource_capacity * bucket_hours[t].

    bucket_start, bucket_end:
        Optional display coordinates. If omitted, starts/ends are cumulative
        using bucket_hours.
    """

    roles: Sequence[str]
    resources: Sequence[str]
    processes: Sequence[str]
    buckets: Sequence[int]
    requirements: Mapping[Key2, float]
    availability: Mapping[Key3, float]
    predecessors: Mapping[str, Iterable[str]] = field(default_factory=dict)
    bucket_hours: Mapping[int, float] = field(default_factory=dict)
    resource_capacity: Mapping[KeyRT, float] = field(default_factory=dict)
    bucket_start: Mapping[int, float] = field(default_factory=dict)
    bucket_end: Mapping[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RoleAssignment:
    role: str
    process: str
    bucket: int
    bucket_start: float
    bucket_end: float
    hours: float


@dataclass(frozen=True)
class ResourceRoleAssignment:
    resource: str
    role: str
    bucket: int
    bucket_start: float
    bucket_end: float
    hours: float


@dataclass(frozen=True)
class ResourceProcessAssignment:
    resource: str
    role: str
    process: str
    bucket: int
    bucket_start: float
    bucket_end: float
    hours: float


@dataclass(frozen=True)
class ProcessTiming:
    process: str
    es: Optional[float]
    ef: Optional[float]
    ls: Optional[float]
    lf: Optional[float]
    slack: Optional[float]
    finish_slack: Optional[float]
    start_bucket: Optional[int]
    finish_bucket: Optional[int]
    latest_start_bucket: Optional[int]
    latest_finish_bucket: Optional[int]


@dataclass(frozen=True)
class BindingResourceTime:
    resource: str
    bucket: int
    bucket_start: float
    bucket_end: float
    used_hours: float
    capacity_hours: float


@dataclass(frozen=True)
class BindingRoleTime:
    role: str
    bucket: int
    bucket_start: float
    bucket_end: float
    used_hours: float
    available_hours: float


@dataclass(frozen=True)
class SolveResult:
    status: str
    objective_makespan: float
    role_assignments: List[RoleAssignment]
    resource_role_assignments: List[ResourceRoleAssignment]
    resource_process_assignments: List[ResourceProcessAssignment]
    process_timings: Dict[str, ProcessTiming]
    critical_processes: List[str]
    critical_edges: List[Tuple[str, str]]
    critical_path: List[str]
    binding_resource_times: List[BindingResourceTime]
    binding_role_times: List[BindingRoleTime]
    raw_work_h: Dict[Tuple[str, str, int], float]
    raw_resource_role_u: Dict[Tuple[str, str, int], float]
    solver_stdout: str = ""
    solver_stderr: str = ""
    mathprog_model_path: Optional[Path] = None
    mathprog_data_path: Optional[Path] = None
    solution_output_path: Optional[Path] = None


@dataclass(frozen=True)
class MathProgPaths:
    model_path: Path
    data_path: Path
    output_path: Path


class GLPKSolveError(RuntimeError):
    """Raised when GLPK cannot produce a feasible/optimal solution."""

    def __init__(self, message: str, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class _EncodedProblem:
    problem: PlanningProblem
    role_to_sym: Dict[str, str]
    sym_to_role: Dict[str, str]
    res_to_sym: Dict[str, str]
    sym_to_res: Dict[str, str]
    proc_to_sym: Dict[str, str]
    sym_to_proc: Dict[str, str]
    bucket_to_sym: Dict[int, str]
    sym_to_bucket: Dict[str, int]
    bucket_hours: Dict[str, float]
    bucket_start: Dict[str, float]
    bucket_end: Dict[str, float]
    requirements: Dict[Tuple[str, str], float]
    availability_hours: Dict[Tuple[str, str, int], float]
    resource_capacity_hours: Dict[Tuple[str, int], float]
    predecessors: Dict[str, List[str]]
    z0: Dict[str, int]


@dataclass(frozen=True)
class _ExtraConstraints:
    fixed_cmax: Optional[float] = None
    fix_tolerance: float = 1e-7
    force_start_by: Optional[Tuple[str, str, float]] = None  # process symbol, bucket symbol, epsilon hours
    no_work_before: Optional[Tuple[str, str]] = None          # process symbol, bucket symbol


@dataclass(frozen=True)
class _RunResult:
    status: str
    cmax: float
    c_by_proc: Dict[str, float]
    z: Dict[Tuple[str, str], float]
    h: Dict[Tuple[str, str, str], float]
    u: Dict[Tuple[str, str, str], float]
    stdout: str
    stderr: str
    paths: MathProgPaths


class ResourceConstrainedCPMSolver:
    """Write MathProg, call GLPK, and decode the resource-constrained schedule."""

    def __init__(self, glpsol: str = "glpsol") -> None:
        self.glpsol = glpsol

    def solve(
        self,
        problem: PlanningProblem,
        *,
        compute_windows: bool = True,
        epsilon: float = 1e-6,
        keep_files: bool = False,
        work_dir: Optional[Path | str] = None,
        time_limit_seconds: Optional[int] = None,
        mip_gap: Optional[float] = None,
    ) -> SolveResult:
        """Solve the planning problem.

        The primary optimization is two-phase:
            1. Minimize project makespan Cmax.
            2. With Cmax fixed, maximize completion flags z[p,t] so process
               completions are reported as early as possible.

        If compute_windows is True, the solver runs additional GLPK models to
        compute CPM-like ES/EF/LS/LF windows. EF/LF are exact on the grid because
        they use binary completion flags. ES/LS are support-based and use the
        supplied epsilon to avoid the ambiguity of infinitesimal fractional work.
        """

        if epsilon <= 0:
            raise ValueError("epsilon must be positive")

        encoded = self._encode_and_validate(problem)

        root = Path(work_dir) if work_dir is not None else Path(tempfile.mkdtemp(prefix="rcpsp_mathprog_"))
        root.mkdir(parents=True, exist_ok=True)
        cleanup_root = work_dir is None and not keep_files

        try:
            run1_dir = root / "phase1_makespan"
            run1 = self._solve_once(
                encoded,
                run1_dir,
                phase="min_makespan",
                extra=_ExtraConstraints(),
                output_tolerance=epsilon / 10.0,
                time_limit_seconds=time_limit_seconds,
                mip_gap=mip_gap,
            )

            run2_dir = root / "phase2_early_completion"
            run2 = self._solve_once(
                encoded,
                run2_dir,
                phase="max_early_completion",
                extra=_ExtraConstraints(fixed_cmax=run1.cmax, fix_tolerance=max(epsilon, 1e-7)),
                output_tolerance=epsilon / 10.0,
                time_limit_seconds=time_limit_seconds,
                mip_gap=mip_gap,
            )

            role_assignments = self._decode_role_assignments(encoded, run2.h, epsilon)
            resource_role_assignments = self._decode_resource_role_assignments(encoded, run2.u, epsilon)
            resource_process_assignments = self._reconstruct_resource_process_assignments(
                encoded, run2.h, run2.u, epsilon
            )

            base_timings = self._scheduled_timings(encoded, run2, epsilon)
            if compute_windows:
                process_timings = self._compute_time_windows(
                    encoded,
                    root,
                    run1.cmax,
                    base_timings,
                    epsilon,
                    time_limit_seconds=time_limit_seconds,
                    mip_gap=mip_gap,
                )
            else:
                process_timings = base_timings

            critical_processes, critical_edges, critical_path = self._critical_path(encoded, process_timings, epsilon)
            binding_resource_times = self._binding_resource_times(encoded, run2.u, epsilon)
            binding_role_times = self._binding_role_times(encoded, run2.h, epsilon)

            raw_h = {
                (encoded.sym_to_role[i], encoded.sym_to_proc[p], encoded.sym_to_bucket[t]): v
                for (i, p, t), v in run2.h.items()
                if abs(v) > epsilon
            }
            raw_u = {
                (encoded.sym_to_res[j], encoded.sym_to_role[i], encoded.sym_to_bucket[t]): v
                for (j, i, t), v in run2.u.items()
                if abs(v) > epsilon
            }

            return SolveResult(
                status=run2.status,
                objective_makespan=run1.cmax,
                role_assignments=role_assignments,
                resource_role_assignments=resource_role_assignments,
                resource_process_assignments=resource_process_assignments,
                process_timings=process_timings,
                critical_processes=critical_processes,
                critical_edges=critical_edges,
                critical_path=critical_path,
                binding_resource_times=binding_resource_times,
                binding_role_times=binding_role_times,
                raw_work_h=raw_h,
                raw_resource_role_u=raw_u,
                solver_stdout=run1.stdout + "\n" + run2.stdout,
                solver_stderr=run1.stderr + "\n" + run2.stderr,
                mathprog_model_path=run2.paths.model_path if keep_files else None,
                mathprog_data_path=run2.paths.data_path if keep_files else None,
                solution_output_path=run2.paths.output_path if keep_files else None,
            )
        finally:
            if cleanup_root:
                shutil.rmtree(root, ignore_errors=True)

    def write_mathprog_files(
        self,
        problem: PlanningProblem,
        directory: Path | str,
        *,
        phase: str = "min_makespan",
        fixed_cmax: Optional[float] = None,
        output_tolerance: float = 1e-7,
    ) -> MathProgPaths:
        """Write standalone MathProg model/data files without solving.

        This is useful for debugging, model review, and agent maintenance.
        Valid phase values are: min_makespan, max_early_completion, feasible.
        """

        encoded = self._encode_and_validate(problem)
        extra = _ExtraConstraints(fixed_cmax=fixed_cmax) if fixed_cmax is not None else _ExtraConstraints()
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        paths = MathProgPaths(directory / "model.mod", directory / "data.dat", directory / "solution.txt")
        paths.model_path.write_text(
            self._render_model(encoded, phase=phase, extra=extra, output_path=paths.output_path, output_tolerance=output_tolerance),
            encoding="utf-8",
        )
        paths.data_path.write_text(self._render_data(encoded, output_tolerance=output_tolerance), encoding="utf-8")
        return paths

    # ------------------------------------------------------------------
    # Encoding and validation
    # ------------------------------------------------------------------

    def _encode_and_validate(self, problem: PlanningProblem) -> _EncodedProblem:
        roles = list(problem.roles)
        resources = list(problem.resources)
        processes = list(problem.processes)
        buckets = list(problem.buckets)

        self._require_unique("roles", roles)
        self._require_unique("resources", resources)
        self._require_unique("processes", processes)
        self._require_unique("buckets", buckets)

        if not roles:
            raise ValueError("at least one role is required")
        if not resources:
            raise ValueError("at least one resource is required")
        if not processes:
            raise ValueError("at least one process is required")
        if not buckets:
            raise ValueError("at least one time bucket is required")

        role_set = set(roles)
        res_set = set(resources)
        proc_set = set(processes)
        bucket_set = set(buckets)

        bucket_hours: Dict[int, float] = {}
        for b in buckets:
            dt = float(problem.bucket_hours.get(b, 1.0))
            if dt <= 0:
                raise ValueError(f"bucket_hours[{b!r}] must be positive")
            bucket_hours[b] = dt

        bucket_start: Dict[int, float] = {}
        bucket_end: Dict[int, float] = {}
        cursor = 0.0
        for b in buckets:
            start = float(problem.bucket_start.get(b, cursor))
            end = float(problem.bucket_end.get(b, start + bucket_hours[b]))
            if end < start:
                raise ValueError(f"bucket_end[{b!r}] is before bucket_start[{b!r}]")
            bucket_start[b] = start
            bucket_end[b] = end
            cursor = end

        requirements: Dict[Tuple[str, str], float] = {}
        for (role, process), value in problem.requirements.items():
            if role not in role_set:
                raise ValueError(f"unknown role in requirements: {role!r}")
            if process not in proc_set:
                raise ValueError(f"unknown process in requirements: {process!r}")
            value = float(value)
            if value < -1e-12:
                raise ValueError(f"requirement {(role, process)!r} must be nonnegative")
            if value > 0:
                requirements[(role, process)] = value

        availability_hours: Dict[Tuple[str, str, int], float] = {}
        for (resource, role, bucket), frac in problem.availability.items():
            if resource not in res_set:
                raise ValueError(f"unknown resource in availability: {resource!r}")
            if role not in role_set:
                raise ValueError(f"unknown role in availability: {role!r}")
            if bucket not in bucket_set:
                raise ValueError(f"unknown bucket in availability: {bucket!r}")
            frac = float(frac)
            if frac < -1e-12 or frac > 1 + 1e-12:
                raise ValueError(f"availability {(resource, role, bucket)!r} must be in [0, 1]")
            if frac > 0:
                availability_hours[(resource, role, bucket)] = frac * bucket_hours[bucket]

        resource_capacity_hours: Dict[Tuple[str, int], float] = {}
        for resource in resources:
            for bucket in buckets:
                frac = float(problem.resource_capacity.get((resource, bucket), 1.0))
                if frac < -1e-12 or frac > 1 + 1e-12:
                    raise ValueError(f"resource_capacity {(resource, bucket)!r} must be in [0, 1]")
                resource_capacity_hours[(resource, bucket)] = max(0.0, frac) * bucket_hours[bucket]

        predecessors: Dict[str, List[str]] = {p: [] for p in processes}
        for process, preds in problem.predecessors.items():
            if process not in proc_set:
                raise ValueError(f"unknown process in predecessors: {process!r}")
            seen: set[str] = set()
            for pred in preds:
                if pred not in proc_set:
                    raise ValueError(f"unknown predecessor {pred!r} for process {process!r}")
                if pred == process:
                    raise ValueError(f"process {process!r} cannot precede itself")
                if pred not in seen:
                    predecessors[process].append(pred)
                    seen.add(pred)

        self._validate_acyclic(processes, predecessors)

        role_to_sym = {name: f"i{idx:04d}" for idx, name in enumerate(roles, start=1)}
        res_to_sym = {name: f"j{idx:04d}" for idx, name in enumerate(resources, start=1)}
        proc_to_sym = {name: f"p{idx:04d}" for idx, name in enumerate(processes, start=1)}
        bucket_to_sym = {name: f"t{idx:04d}" for idx, name in enumerate(buckets, start=1)}

        sym_to_role = {v: k for k, v in role_to_sym.items()}
        sym_to_res = {v: k for k, v in res_to_sym.items()}
        sym_to_proc = {v: k for k, v in proc_to_sym.items()}
        sym_to_bucket = {v: k for k, v in bucket_to_sym.items()}

        enc_req = {(role_to_sym[r], proc_to_sym[p]): v for (r, p), v in requirements.items()}
        enc_avail = {
            (res_to_sym[j], role_to_sym[i], bucket_to_sym[t]): v
            for (j, i, t), v in availability_hours.items()
        }
        enc_cap = {(res_to_sym[j], bucket_to_sym[t]): v for (j, t), v in resource_capacity_hours.items()}
        enc_preds = {
            proc_to_sym[p]: [proc_to_sym[q] for q in preds]
            for p, preds in predecessors.items()
        }

        total_req = {p: sum(requirements.get((i, p), 0.0) for i in roles) for p in processes}
        z0 = {
            proc_to_sym[p]: 1 if total_req[p] <= 1e-12 and len(predecessors[p]) == 0 else 0
            for p in processes
        }

        return _EncodedProblem(
            problem=problem,
            role_to_sym=role_to_sym,
            sym_to_role=sym_to_role,
            res_to_sym=res_to_sym,
            sym_to_res=sym_to_res,
            proc_to_sym=proc_to_sym,
            sym_to_proc=sym_to_proc,
            bucket_to_sym=bucket_to_sym,
            sym_to_bucket=sym_to_bucket,
            bucket_hours={bucket_to_sym[k]: v for k, v in bucket_hours.items()},  # type: ignore[arg-type]
            bucket_start={bucket_to_sym[k]: v for k, v in bucket_start.items()},  # type: ignore[arg-type]
            bucket_end={bucket_to_sym[k]: v for k, v in bucket_end.items()},      # type: ignore[arg-type]
            requirements=enc_req,
            availability_hours=enc_avail,
            resource_capacity_hours=enc_cap,
            predecessors=enc_preds,
            z0=z0,
        )

    @staticmethod
    def _require_unique(label: str, values: Sequence[object]) -> None:
        if len(set(values)) != len(values):
            raise ValueError(f"{label} must be unique")

    @staticmethod
    def _validate_acyclic(processes: Sequence[str], predecessors: Mapping[str, Sequence[str]]) -> None:
        temp: set[str] = set()
        perm: set[str] = set()

        def visit(p: str) -> None:
            if p in perm:
                return
            if p in temp:
                raise ValueError("process dependency graph contains a cycle")
            temp.add(p)
            for q in predecessors.get(p, []):
                visit(q)
            temp.remove(p)
            perm.add(p)

        for p in processes:
            visit(p)

    # ------------------------------------------------------------------
    # GLPK calls
    # ------------------------------------------------------------------

    def _solve_once(
        self,
        encoded: _EncodedProblem,
        directory: Path,
        *,
        phase: str,
        extra: _ExtraConstraints,
        output_tolerance: float,
        time_limit_seconds: Optional[int],
        mip_gap: Optional[float],
        allow_infeasible: bool = False,
    ) -> Optional[_RunResult]:
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        paths = MathProgPaths(directory / "model.mod", directory / "data.dat", directory / "solution.txt")

        paths.model_path.write_text(
            self._render_model(encoded, phase=phase, extra=extra, output_path=paths.output_path, output_tolerance=output_tolerance),
            encoding="utf-8",
        )
        paths.data_path.write_text(self._render_data(encoded, output_tolerance=output_tolerance), encoding="utf-8")

        cmd = [self.glpsol, "--model", str(paths.model_path), "--data", str(paths.data_path)]
        if time_limit_seconds is not None:
            cmd.extend(["--tmlim", str(int(time_limit_seconds))])
        if mip_gap is not None:
            cmd.extend(["--mipgap", str(float(mip_gap))])

        try:
            proc = subprocess.run(cmd, cwd=directory, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise GLPKSolveError(
                f"Could not find GLPK executable {self.glpsol!r}. Install GLPK or pass glpsol='/path/to/glpsol'."
            ) from exc

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        combined = stdout + "\n" + stderr

        infeasible_markers = [
            "PROBLEM HAS NO PRIMAL FEASIBLE SOLUTION",
            "PROBLEM HAS NO INTEGER FEASIBLE SOLUTION",
            "PROBLEM HAS NO FEASIBLE SOLUTION",
            "LP HAS NO PRIMAL FEASIBLE SOLUTION",
            "INTEGER EMPTY",
        ]
        if any(marker in combined for marker in infeasible_markers):
            if allow_infeasible:
                return None
            raise GLPKSolveError("GLPK reported infeasibility", stdout, stderr)

        if proc.returncode != 0:
            if allow_infeasible and any("NO" in line and "FEASIBLE" in line for line in combined.splitlines()):
                return None
            raise GLPKSolveError(f"glpsol exited with status {proc.returncode}", stdout, stderr)

        if not paths.output_path.exists():
            if allow_infeasible:
                return None
            raise GLPKSolveError(
                "GLPK finished but the MathProg solution output file was not produced. "
                "Check stdout/stderr for model syntax or solve errors.",
                stdout,
                stderr,
            )

        parsed = self._parse_solution_output(paths.output_path)
        return _RunResult(
            status=parsed["status"],
            cmax=parsed["cmax"],
            c_by_proc=parsed["c"],
            z=parsed["z"],
            h=parsed["h"],
            u=parsed["u"],
            stdout=stdout,
            stderr=stderr,
            paths=paths,
        )

    # ------------------------------------------------------------------
    # MathProg rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _q(s: str | Path) -> str:
        # MathProg uses C-like string literals. Backslashes must be escaped.
        text = str(s)
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _render_model(
        self,
        encoded: _EncodedProblem,
        *,
        phase: str,
        extra: _ExtraConstraints,
        output_path: Path,
        output_tolerance: float,
    ) -> str:
        out = self._q(output_path)
        lines: List[str] = []
        lines.append("# Auto-generated by rcpsp_mathprog.py")
        lines.append("set I;")
        lines.append("set J;")
        lines.append("set P;")
        lines.append("set TT ordered;")
        lines.append("set Pred{p in P} within P, default {};")
        lines.append("param R{I,P} >= 0, default 0;")
        lines.append("param U{J,I,TT} >= 0, default 0;")
        lines.append("param B{J,TT} >= 0, default 0;")
        lines.append("param dt{TT} > 0;")
        lines.append("param theta{TT} >= 0;")
        lines.append("param z0{P} binary, default 0;")
        lines.append("param OutTol >= 0, default 1e-8;")
        lines.append("param W{p in P} := sum{i in I} R[i,p];")
        lines.append("")
        lines.append("var h{I,P,TT} >= 0;")
        lines.append("var g{I,P,TT} >= 0;")
        lines.append("var u{J,I,TT} >= 0;")
        lines.append("var z{P,TT} binary;")
        lines.append("var C{P} >= 0;")
        lines.append("var Cmax >= 0;")
        lines.append("")
        lines.append("# Cumulative role-work delivered to each process.")
        lines.append("s.t. CumDef{i in I, p in P, t in TT}:")
        lines.append("    g[i,p,t] = h[i,p,t] +")
        lines.append("        (if ord(t) = 1 then 0 else")
        lines.append("            sum{tt in TT: ord(tt) = ord(t)-1} g[i,p,tt]);")
        lines.append("")
        lines.append("# Each process receives exactly its required role-hours.")
        lines.append("s.t. Req{i in I, p in P}:")
        lines.append("    sum{t in TT} h[i,p,t] = R[i,p];")
        lines.append("")
        lines.append("# Resource-to-role allocation balances role-work demand by bucket.")
        lines.append("s.t. RoleBalance{i in I, t in TT}:")
        lines.append("    sum{p in P} h[i,p,t] = sum{j in J} u[j,i,t];")
        lines.append("")
        lines.append("# A resource's total load across all roles cannot exceed its calendar capacity.")
        lines.append("s.t. ResourceCap{j in J, t in TT}:")
        lines.append("    sum{i in I} u[j,i,t] <= B[j,t];")
        lines.append("")
        lines.append("# Role-specific resource calendar/capability.")
        lines.append("s.t. RoleAvail{j in J, i in I, t in TT}:")
        lines.append("    u[j,i,t] <= U[j,i,t];")
        lines.append("")
        lines.append("# A process can be marked complete only when every required role is complete.")
        lines.append("s.t. CompletionClaim{i in I, p in P, t in TT: R[i,p] > 0}:")
        lines.append("    g[i,p,t] >= R[i,p] * z[p,t];")
        lines.append("")
        lines.append("# Completion flags are monotone, including their time-zero value z0.")
        lines.append("s.t. ZInitial{p in P, t in TT: ord(t) = 1}:")
        lines.append("    z[p,t] >= z0[p];")
        lines.append("")
        lines.append("s.t. ZMonotone{p in P, t in TT: ord(t) > 1}:")
        lines.append("    sum{tt in TT: ord(tt) = ord(t)-1} z[p,tt] <= z[p,t];")
        lines.append("")
        lines.append("# All processes must be complete by the horizon.")
        lines.append("s.t. DoneAtHorizon{p in P, t in TT: ord(t) = card(TT)}:")
        lines.append("    z[p,t] = 1;")
        lines.append("")
        lines.append("# Finish-to-start precedence: p can progress only after q is complete.")
        lines.append("s.t. StrictPrecedenceWork{i in I, p in P, q in Pred[p], t in TT: R[i,p] > 0}:")
        lines.append("    g[i,p,t] <= R[i,p] *")
        lines.append("        (if ord(t) = 1 then z0[q] else")
        lines.append("            sum{tt in TT: ord(tt) = ord(t)-1} z[q,tt]);")
        lines.append("")
        lines.append("# Zero-work and nonzero-work process completion flags obey predecessor completion.")
        lines.append("s.t. StrictPrecedenceComplete{p in P, q in Pred[p], t in TT}:")
        lines.append("    z[p,t] <=")
        lines.append("        (if ord(t) = 1 then z0[q] else")
        lines.append("            sum{tt in TT: ord(tt) = ord(t)-1} z[q,tt]);")
        lines.append("")
        lines.append("# Completion time from the first 0->1 transition of z.")
        lines.append("s.t. CompletionTime{p in P}:")
        lines.append("    C[p] = sum{t in TT} theta[t] *")
        lines.append("        (z[p,t] - (if ord(t) = 1 then z0[p] else")
        lines.append("            sum{tt in TT: ord(tt) = ord(t)-1} z[p,tt]));")
        lines.append("")
        lines.append("s.t. ProjectMakespan{p in P}:")
        lines.append("    Cmax >= C[p];")

        if extra.fixed_cmax is not None:
            lines.append("")
            lines.append(f"s.t. FixedMakespan: Cmax <= {extra.fixed_cmax:.17g} + {extra.fix_tolerance:.17g};")

        if extra.force_start_by is not None:
            p_sym, t_sym, eps = extra.force_start_by
            target_ord = [encoded.bucket_to_sym[b] for b in encoded.problem.buckets].index(t_sym) + 1
            lines.append("")
            lines.append("# Diagnostic support constraint: process must have started by this bucket.")
            lines.append("s.t. ForceStartBy:")
            lines.append(
                f"    sum{{i in I, tt in TT: ord(tt) <= {target_ord}}} h[i,{self._q(p_sym)},tt] >= {eps:.17g};"
            )

        if extra.no_work_before is not None:
            p_sym, t_sym = extra.no_work_before
            target_ord = [encoded.bucket_to_sym[b] for b in encoded.problem.buckets].index(t_sym) + 1
            lines.append("")
            lines.append("# Diagnostic support constraint: process cannot start before this bucket.")
            lines.append("s.t. NoWorkBefore:")
            lines.append(
                f"    sum{{i in I, tt in TT: ord(tt) < {target_ord}}} h[i,{self._q(p_sym)},tt] = 0;"
            )

        lines.append("")
        if phase == "min_makespan":
            lines.append("minimize Obj: Cmax;")
        elif phase == "max_early_completion":
            lines.append("maximize Obj: sum{p in P, t in TT} z[p,t];")
        elif phase.startswith("min_completion:"):
            p_sym = phase.split(":", 1)[1]
            lines.append(f"minimize Obj: C[{self._q(p_sym)}];")
        elif phase.startswith("max_completion:"):
            p_sym = phase.split(":", 1)[1]
            lines.append(f"maximize Obj: C[{self._q(p_sym)}];")
        elif phase == "feasible":
            lines.append("minimize Obj: 0;")
        else:
            raise ValueError(f"unknown MathProg phase {phase!r}")

        lines.append("")
        lines.append("solve;")
        lines.append("")
        lines.append(f"printf \"STATUS|OK\\n\" > {out};")
        lines.append(f"printf \"CMAX|%.15g\\n\", Cmax >> {out};")
        lines.append(f"printf {{p in P}} \"C|%s|%.15g\\n\", p, C[p] >> {out};")
        lines.append(f"printf {{p in P, t in TT: z[p,t] > 0.5}} \"Z|%s|%s|%.15g\\n\", p, t, z[p,t] >> {out};")
        lines.append(
            f"printf {{i in I, p in P, t in TT: h[i,p,t] > OutTol}} \"H|%s|%s|%s|%.15g\\n\", i, p, t, h[i,p,t] >> {out};"
        )
        lines.append(
            f"printf {{j in J, i in I, t in TT: u[j,i,t] > OutTol}} \"U|%s|%s|%s|%.15g\\n\", j, i, t, u[j,i,t] >> {out};"
        )
        lines.append("")
        lines.append("end;")
        return "\n".join(lines) + "\n"

    def _render_data(self, encoded: _EncodedProblem, *, output_tolerance: float) -> str:
        q = self._q
        lines: List[str] = []
        lines.append("data;")
        lines.append("set I := " + " ".join(q(s) for s in encoded.role_to_sym.values()) + ";")
        lines.append("set J := " + " ".join(q(s) for s in encoded.res_to_sym.values()) + ";")
        lines.append("set P := " + " ".join(q(s) for s in encoded.proc_to_sym.values()) + ";")
        # Keep the user-provided order of buckets.
        bucket_syms = [encoded.bucket_to_sym[b] for b in encoded.problem.buckets]
        lines.append("set TT := " + " ".join(q(s) for s in bucket_syms) + ";")
        lines.append("")

        lines.append("param OutTol := %.17g;" % output_tolerance)
        lines.append("")

        self._append_param2(lines, "R", encoded.requirements)
        self._append_param3(lines, "U", encoded.availability_hours)
        self._append_param2(lines, "B", encoded.resource_capacity_hours)
        self._append_param1(lines, "dt", encoded.bucket_hours)
        self._append_param1(lines, "theta", encoded.bucket_end)
        self._append_param1(lines, "z0", encoded.z0)

        for p_sym, preds in encoded.predecessors.items():
            if preds:
                lines.append(f"set Pred[{q(p_sym)}] := " + " ".join(q(x) for x in preds) + ";")

        lines.append("end;")
        return "\n".join(lines) + "\n"

    def _append_param1(self, lines: List[str], name: str, data: Mapping[str, float | int]) -> None:
        lines.append(f"param {name} :=")
        for k, v in data.items():
            lines.append(f"  {self._q(k)} {float(v):.17g}")
        lines.append(";")
        lines.append("")

    def _append_param2(self, lines: List[str], name: str, data: Mapping[Tuple[str, str], float]) -> None:
        lines.append(f"param {name} :=")
        for (a, b), v in data.items():
            if abs(float(v)) > 0:
                lines.append(f"  {self._q(a)} {self._q(b)} {float(v):.17g}")
        lines.append(";")
        lines.append("")

    def _append_param3(self, lines: List[str], name: str, data: Mapping[Tuple[str, str, str], float]) -> None:
        lines.append(f"param {name} :=")
        for (a, b, c), v in data.items():
            if abs(float(v)) > 0:
                lines.append(f"  {self._q(a)} {self._q(b)} {self._q(c)} {float(v):.17g}")
        lines.append(";")
        lines.append("")

    # ------------------------------------------------------------------
    # Solution parsing and decoding
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_solution_output(path: Path) -> Dict[str, object]:
        status = "UNKNOWN"
        cmax = math.nan
        c: Dict[str, float] = {}
        z: Dict[Tuple[str, str], float] = {}
        h: Dict[Tuple[str, str, str], float] = {}
        u: Dict[Tuple[str, str, str], float] = {}

        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            parts = raw_line.rstrip("\n").split("|")
            tag = parts[0]
            if tag == "STATUS":
                status = parts[1]
            elif tag == "CMAX":
                cmax = float(parts[1])
            elif tag == "C":
                _, p, value = parts
                c[p] = float(value)
            elif tag == "Z":
                _, p, t, value = parts
                z[(p, t)] = float(value)
            elif tag == "H":
                _, i, p, t, value = parts
                h[(i, p, t)] = float(value)
            elif tag == "U":
                _, j, i, t, value = parts
                u[(j, i, t)] = float(value)

        if math.isnan(cmax):
            raise GLPKSolveError(f"solution output {path} did not contain CMAX")

        return {"status": status, "cmax": cmax, "c": c, "z": z, "h": h, "u": u}

    def _decode_role_assignments(
        self,
        encoded: _EncodedProblem,
        h: Mapping[Tuple[str, str, str], float],
        epsilon: float,
    ) -> List[RoleAssignment]:
        rows: List[RoleAssignment] = []
        for (i, p, t), hours in sorted(h.items(), key=lambda kv: (kv[0][2], kv[0][1], kv[0][0])):
            if hours <= epsilon:
                continue
            bucket = encoded.sym_to_bucket[t]
            rows.append(
                RoleAssignment(
                    role=encoded.sym_to_role[i],
                    process=encoded.sym_to_proc[p],
                    bucket=bucket,
                    bucket_start=encoded.bucket_start[t],
                    bucket_end=encoded.bucket_end[t],
                    hours=hours,
                )
            )
        return rows

    def _decode_resource_role_assignments(
        self,
        encoded: _EncodedProblem,
        u: Mapping[Tuple[str, str, str], float],
        epsilon: float,
    ) -> List[ResourceRoleAssignment]:
        rows: List[ResourceRoleAssignment] = []
        for (j, i, t), hours in sorted(u.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1])):
            if hours <= epsilon:
                continue
            bucket = encoded.sym_to_bucket[t]
            rows.append(
                ResourceRoleAssignment(
                    resource=encoded.sym_to_res[j],
                    role=encoded.sym_to_role[i],
                    bucket=bucket,
                    bucket_start=encoded.bucket_start[t],
                    bucket_end=encoded.bucket_end[t],
                    hours=hours,
                )
            )
        return rows

    def _reconstruct_resource_process_assignments(
        self,
        encoded: _EncodedProblem,
        h: Mapping[Tuple[str, str, str], float],
        u: Mapping[Tuple[str, str, str], float],
        epsilon: float,
    ) -> List[ResourceProcessAssignment]:
        """Greedily match resource-role supply u to process-role demand h.

        The main MILP aggregates process-role work h and resource-role work u.
        Because RoleBalance enforces sum_p h[i,p,t] = sum_j u[j,i,t], a detailed
        resource/process split can be recovered independently for each role/time.
        """

        assignments: List[ResourceProcessAssignment] = []
        role_syms = list(encoded.sym_to_role.keys())
        time_syms = [encoded.bucket_to_sym[b] for b in encoded.problem.buckets]
        proc_syms = list(encoded.sym_to_proc.keys())
        res_syms = list(encoded.sym_to_res.keys())

        for i in role_syms:
            for t in time_syms:
                demands = [[p, h.get((i, p, t), 0.0)] for p in proc_syms if h.get((i, p, t), 0.0) > epsilon]
                supplies = [[j, u.get((j, i, t), 0.0)] for j in res_syms if u.get((j, i, t), 0.0) > epsilon]
                d_idx = 0
                s_idx = 0
                while d_idx < len(demands) and s_idx < len(supplies):
                    p, demand = demands[d_idx]
                    j, supply = supplies[s_idx]
                    amount = min(demand, supply)
                    if amount > epsilon:
                        bucket = encoded.sym_to_bucket[t]
                        assignments.append(
                            ResourceProcessAssignment(
                                resource=encoded.sym_to_res[j],
                                role=encoded.sym_to_role[i],
                                process=encoded.sym_to_proc[p],
                                bucket=bucket,
                                bucket_start=encoded.bucket_start[t],
                                bucket_end=encoded.bucket_end[t],
                                hours=amount,
                            )
                        )
                    demands[d_idx][1] -= amount
                    supplies[s_idx][1] -= amount
                    if demands[d_idx][1] <= epsilon:
                        d_idx += 1
                    if supplies[s_idx][1] <= epsilon:
                        s_idx += 1

        return assignments

    def _scheduled_timings(
        self,
        encoded: _EncodedProblem,
        run: _RunResult,
        epsilon: float,
    ) -> Dict[str, ProcessTiming]:
        timings: Dict[str, ProcessTiming] = {}
        time_syms = [encoded.bucket_to_sym[b] for b in encoded.problem.buckets]
        role_syms = list(encoded.sym_to_role.keys())
        for p_sym, process in encoded.sym_to_proc.items():
            work_by_t: Dict[str, float] = {
                t: sum(run.h.get((i, p_sym, t), 0.0) for i in role_syms) for t in time_syms
            }
            active_times = [t for t in time_syms if work_by_t[t] > epsilon]
            if active_times:
                start_t = active_times[0]
                es = encoded.bucket_start[start_t]
                start_bucket = encoded.sym_to_bucket[start_t]
            else:
                start_t = None
                es = 0.0 if encoded.z0.get(p_sym, 0) == 1 else None
                start_bucket = None

            finish_t = self._completion_transition_bucket(encoded, run.z, p_sym)
            if finish_t is not None:
                ef = encoded.bucket_end[finish_t]
                finish_bucket = encoded.sym_to_bucket[finish_t]
            elif encoded.z0.get(p_sym, 0) == 1:
                ef = 0.0
                finish_bucket = None
            else:
                ef = run.c_by_proc.get(p_sym)
                finish_bucket = None

            timings[process] = ProcessTiming(
                process=process,
                es=es,
                ef=ef,
                ls=es,
                lf=ef,
                slack=0.0 if es is not None else None,
                finish_slack=0.0 if ef is not None else None,
                start_bucket=start_bucket,
                finish_bucket=finish_bucket,
                latest_start_bucket=start_bucket,
                latest_finish_bucket=finish_bucket,
            )
        return timings

    def _completion_transition_bucket(
        self,
        encoded: _EncodedProblem,
        z: Mapping[Tuple[str, str], float],
        p_sym: str,
    ) -> Optional[str]:
        prev = float(encoded.z0.get(p_sym, 0))
        for b in encoded.problem.buckets:
            t = encoded.bucket_to_sym[b]
            cur = z.get((p_sym, t), 0.0)
            if prev < 0.5 and cur > 0.5:
                return t
            prev = cur
        return None

    # ------------------------------------------------------------------
    # CPM-like windows and critical path diagnostics
    # ------------------------------------------------------------------

    def _compute_time_windows(
        self,
        encoded: _EncodedProblem,
        root: Path,
        cmax: float,
        base: Dict[str, ProcessTiming],
        epsilon: float,
        *,
        time_limit_seconds: Optional[int],
        mip_gap: Optional[float],
    ) -> Dict[str, ProcessTiming]:
        timings: Dict[str, ProcessTiming] = dict(base)
        time_syms = [encoded.bucket_to_sym[b] for b in encoded.problem.buckets]

        for p_sym, process in encoded.sym_to_proc.items():
            total_req = sum(encoded.requirements.get((i, p_sym), 0.0) for i in encoded.sym_to_role)
            if total_req <= epsilon:
                # For zero-work processes, report completion-derived windows only.
                ef = base[process].ef
                timings[process] = ProcessTiming(
                    process=process,
                    es=ef,
                    ef=ef,
                    ls=ef,
                    lf=ef,
                    slack=0.0,
                    finish_slack=0.0,
                    start_bucket=base[process].finish_bucket,
                    finish_bucket=base[process].finish_bucket,
                    latest_start_bucket=base[process].finish_bucket,
                    latest_finish_bucket=base[process].finish_bucket,
                )
                continue

            # Earliest start: first bucket by which at least epsilon hours can be placed.
            es_bucket_sym: Optional[str] = None
            for t_sym in time_syms:
                rr = self._solve_once(
                    encoded,
                    root / f"diag_es_{p_sym}_{t_sym}",
                    phase="feasible",
                    extra=_ExtraConstraints(
                        fixed_cmax=cmax,
                        fix_tolerance=max(epsilon, 1e-7),
                        force_start_by=(p_sym, t_sym, min(epsilon, max(epsilon, total_req * 1e-6))),
                    ),
                    output_tolerance=epsilon / 10.0,
                    time_limit_seconds=time_limit_seconds,
                    mip_gap=mip_gap,
                    allow_infeasible=True,
                )
                if rr is not None:
                    es_bucket_sym = t_sym
                    break

            # Latest start: last bucket before which all work can be held at zero.
            ls_bucket_sym: Optional[str] = None
            for t_sym in reversed(time_syms):
                rr = self._solve_once(
                    encoded,
                    root / f"diag_ls_{p_sym}_{t_sym}",
                    phase="feasible",
                    extra=_ExtraConstraints(
                        fixed_cmax=cmax,
                        fix_tolerance=max(epsilon, 1e-7),
                        no_work_before=(p_sym, t_sym),
                    ),
                    output_tolerance=epsilon / 10.0,
                    time_limit_seconds=time_limit_seconds,
                    mip_gap=mip_gap,
                    allow_infeasible=True,
                )
                if rr is not None:
                    ls_bucket_sym = t_sym
                    break

            # Earliest and latest finish are exact completion-time optimizations.
            min_finish = self._solve_once(
                encoded,
                root / f"diag_ef_{p_sym}",
                phase=f"min_completion:{p_sym}",
                extra=_ExtraConstraints(fixed_cmax=cmax, fix_tolerance=max(epsilon, 1e-7)),
                output_tolerance=epsilon / 10.0,
                time_limit_seconds=time_limit_seconds,
                mip_gap=mip_gap,
            )
            max_finish = self._solve_once(
                encoded,
                root / f"diag_lf_{p_sym}",
                phase=f"max_completion:{p_sym}",
                extra=_ExtraConstraints(fixed_cmax=cmax, fix_tolerance=max(epsilon, 1e-7)),
                output_tolerance=epsilon / 10.0,
                time_limit_seconds=time_limit_seconds,
                mip_gap=mip_gap,
            )
            assert min_finish is not None and max_finish is not None

            es = encoded.bucket_start[es_bucket_sym] if es_bucket_sym is not None else base[process].es
            ls = encoded.bucket_start[ls_bucket_sym] if ls_bucket_sym is not None else base[process].ls
            ef = min_finish.c_by_proc.get(p_sym, base[process].ef)
            lf = max_finish.c_by_proc.get(p_sym, base[process].lf)

            finish_t_min = self._completion_transition_bucket(encoded, min_finish.z, p_sym)
            finish_t_max = self._completion_transition_bucket(encoded, max_finish.z, p_sym)

            slack = None if es is None or ls is None else max(0.0, ls - es)
            finish_slack = None if ef is None or lf is None else max(0.0, lf - ef)

            timings[process] = ProcessTiming(
                process=process,
                es=es,
                ef=ef,
                ls=ls,
                lf=lf,
                slack=slack,
                finish_slack=finish_slack,
                start_bucket=encoded.sym_to_bucket[es_bucket_sym] if es_bucket_sym else base[process].start_bucket,
                finish_bucket=encoded.sym_to_bucket[finish_t_min] if finish_t_min else base[process].finish_bucket,
                latest_start_bucket=encoded.sym_to_bucket[ls_bucket_sym] if ls_bucket_sym else base[process].latest_start_bucket,
                latest_finish_bucket=encoded.sym_to_bucket[finish_t_max] if finish_t_max else base[process].latest_finish_bucket,
            )

        return timings

    def _critical_path(
        self,
        encoded: _EncodedProblem,
        timings: Mapping[str, ProcessTiming],
        epsilon: float,
    ) -> Tuple[List[str], List[Tuple[str, str]], List[str]]:
        critical_processes = [
            p for p, tm in timings.items()
            if tm.slack is not None and tm.finish_slack is not None and min(tm.slack, tm.finish_slack) <= epsilon
        ]
        critical_set = set(critical_processes)

        edges: List[Tuple[str, str]] = []
        for p_sym, preds in encoded.predecessors.items():
            p = encoded.sym_to_proc[p_sym]
            for q_sym in preds:
                q = encoded.sym_to_proc[q_sym]
                tq = timings.get(q)
                tp = timings.get(p)
                if q in critical_set and p in critical_set and tq and tp and tq.ef is not None and tp.es is not None:
                    if abs(tp.es - tq.ef) <= max(epsilon, 1e-7):
                        edges.append((q, p))

        # Longest path through the critical-edge DAG. If no critical edge exists,
        # return the latest-finishing critical process as a degenerate path.
        order = self._topological_order([encoded.sym_to_proc[s] for s in encoded.proc_to_sym.values()], encoded)
        best_path: Dict[str, List[str]] = {p: [p] for p in timings}
        incoming = {p: [] for p in timings}
        for q, p in edges:
            incoming[p].append(q)

        for p in order:
            for q in incoming.get(p, []):
                candidate = best_path.get(q, [q]) + [p]
                if len(candidate) > len(best_path.get(p, [p])):
                    best_path[p] = candidate

        if edges:
            critical_path = max(best_path.values(), key=len)
        elif critical_processes:
            critical_path = [max(critical_processes, key=lambda p: timings[p].ef if timings[p].ef is not None else -math.inf)]
        else:
            critical_path = []

        return critical_processes, edges, critical_path

    def _topological_order(self, process_names: Sequence[str], encoded: _EncodedProblem) -> List[str]:
        preds_by_name = {
            encoded.sym_to_proc[p_sym]: [encoded.sym_to_proc[q] for q in preds]
            for p_sym, preds in encoded.predecessors.items()
        }
        visited: set[str] = set()
        order: List[str] = []

        def visit(p: str) -> None:
            if p in visited:
                return
            for q in preds_by_name.get(p, []):
                visit(q)
            visited.add(p)
            order.append(p)

        for p in process_names:
            visit(p)
        return order

    def _binding_resource_times(
        self,
        encoded: _EncodedProblem,
        u: Mapping[Tuple[str, str, str], float],
        epsilon: float,
    ) -> List[BindingResourceTime]:
        rows: List[BindingResourceTime] = []
        for j in encoded.sym_to_res:
            for b in encoded.problem.buckets:
                t = encoded.bucket_to_sym[b]
                used = sum(u.get((j, i, t), 0.0) for i in encoded.sym_to_role)
                cap = encoded.resource_capacity_hours.get((j, t), 0.0)
                if cap > epsilon and cap - used <= epsilon:
                    rows.append(
                        BindingResourceTime(
                            resource=encoded.sym_to_res[j],
                            bucket=b,
                            bucket_start=encoded.bucket_start[t],
                            bucket_end=encoded.bucket_end[t],
                            used_hours=used,
                            capacity_hours=cap,
                        )
                    )
        return rows

    def _binding_role_times(
        self,
        encoded: _EncodedProblem,
        h: Mapping[Tuple[str, str, str], float],
        epsilon: float,
    ) -> List[BindingRoleTime]:
        rows: List[BindingRoleTime] = []
        for i in encoded.sym_to_role:
            for b in encoded.problem.buckets:
                t = encoded.bucket_to_sym[b]
                used = sum(h.get((i, p, t), 0.0) for p in encoded.sym_to_proc)
                available = sum(encoded.availability_hours.get((j, i, t), 0.0) for j in encoded.sym_to_res)
                # The actual role capacity is also limited by resource cross-role capacity;
                # this diagnostic only flags the simple role-calendar bound.
                if available > epsilon and available - used <= epsilon:
                    rows.append(
                        BindingRoleTime(
                            role=encoded.sym_to_role[i],
                            bucket=b,
                            bucket_start=encoded.bucket_start[t],
                            bucket_end=encoded.bucket_end[t],
                            used_hours=used,
                            available_hours=available,
                        )
                    )
        return rows


__all__ = [
    "PlanningProblem",
    "RoleAssignment",
    "ResourceRoleAssignment",
    "ResourceProcessAssignment",
    "ProcessTiming",
    "BindingResourceTime",
    "BindingRoleTime",
    "SolveResult",
    "MathProgPaths",
    "GLPKSolveError",
    "ResourceConstrainedCPMSolver",
]
