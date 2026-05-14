"""Pure schedule projection and critical-path calculations."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum

import networkx as nx

from projdash.engine.calendar import (
    add_business_days,
    count_business_days,
    next_business_day,
    subtract_business_days,
)


class ComputedScheduleStatus(str, Enum):
    """Computed project-management state derived from schedule facts."""

    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    LATE = "late"
    BLOCKED = "blocked"
    DUE_ELAPSED_UNVERIFIED = "due_elapsed_unverified"
    VALIDATED_DONE = "validated_done"


@dataclass(frozen=True, slots=True)
class ProjectScheduleInput:
    """Read model consumed by the pure scheduling engine."""

    project_id: str
    name: str
    start_at: dt.datetime
    processes: tuple[ProcessScheduleInput, ...]


@dataclass(frozen=True, slots=True)
class ProcessScheduleInput:
    """Planning inputs for one process at a selected historical date."""

    process_id: str
    name: str
    dependencies: tuple[str, ...]
    duration_business_days: int
    explicit_status: str
    started_at: dt.datetime | None = None
    due_at: dt.datetime | None = None
    earliest_start_at: dt.datetime | None = None
    start_at_earliest: bool = False
    delay_after_dependencies_business_days: int = 0
    unresolved_blocker_count: int = 0
    description: str = ""


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    """Computed schedule output for one process."""

    process_id: str
    name: str
    explicit_status: str
    computed_status: ComputedScheduleStatus
    dependencies: tuple[str, ...]
    earliest_start_at: dt.datetime
    earliest_finish_at: dt.datetime
    latest_start_at: dt.datetime
    latest_finish_at: dt.datetime
    total_float_business_days: int
    is_critical: bool
    due_at: dt.datetime | None

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-serializable dictionary for agents and UI adapters."""
        return {
            "process_id": self.process_id,
            "name": self.name,
            "explicit_status": self.explicit_status,
            "computed_status": self.computed_status.value,
            "dependencies": list(self.dependencies),
            "earliest_start_at": self.earliest_start_at.isoformat(),
            "earliest_finish_at": self.earliest_finish_at.isoformat(),
            "latest_start_at": self.latest_start_at.isoformat(),
            "latest_finish_at": self.latest_finish_at.isoformat(),
            "total_float_business_days": self.total_float_business_days,
            "is_critical": self.is_critical,
            "due_at": self.due_at.isoformat() if self.due_at else None,
        }


@dataclass(frozen=True, slots=True)
class ScheduleProjection:
    """Computed schedule for a project at one `as_of` date."""

    project_id: str
    project_name: str
    start_at: dt.datetime
    completion_at: dt.datetime
    rows: tuple[ScheduleRow, ...]

    @property
    def critical_path(self) -> tuple[str, ...]:
        """Return critical processes in earliest-start order."""
        return tuple(row.process_id for row in self.rows if row.is_critical)

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-serializable dictionary for agents and UI adapters."""
        return {
            "project": {
                "project_id": self.project_id,
                "name": self.project_name,
                "start_at": self.start_at.isoformat(),
                "completion_at": self.completion_at.isoformat(),
            },
            "processes": [row.to_json_dict() for row in self.rows],
        }


def compute_schedule(
    project: ProjectScheduleInput,
    now: dt.datetime,
) -> ScheduleProjection:
    """Compute CPM-style schedule rows for a project.

    Args:
        project: Project read model at the chosen historical date.
        now: Timezone-aware datetime used for computed follow-up states.

    Returns:
        Schedule projection with critical path annotations.

    Raises:
        ValueError: If dependencies reference unknown processes or form a cycle.
    """
    process_by_id = {process.process_id: process for process in project.processes}
    graph = nx.DiGraph()
    graph.add_nodes_from(process_by_id)
    for process in project.processes:
        for dependency in process.dependencies:
            if dependency not in process_by_id:
                raise ValueError(f"unknown dependency {dependency!r}")
            graph.add_edge(dependency, process.process_id)

    if not nx.is_directed_acyclic_graph(graph):
        raise ValueError("process dependencies must form a directed acyclic graph")

    earliest_start: dict[str, dt.datetime] = {}
    earliest_finish: dict[str, dt.datetime] = {}
    project_start = project.start_at

    for process_id in nx.topological_sort(graph):
        process = process_by_id[process_id]
        dependency_finish = max(
            (earliest_finish[dependency] for dependency in graph.predecessors(process_id)),
            default=project_start,
        )
        if process.started_at is not None:
            start = process.started_at
        else:
            constraints = [dependency_finish]
            if process.earliest_start_at is not None:
                constraints.append(next_business_day(process.earliest_start_at))
            if process.delay_after_dependencies_business_days:
                constraints.append(
                    add_business_days(
                        dependency_finish,
                        process.delay_after_dependencies_business_days,
                    )
                )
            start = max(constraints)

        earliest_start[process_id] = start
        earliest_finish[process_id] = add_business_days(
            start,
            process.duration_business_days,
        )

    completion_at = max(earliest_finish.values(), default=project_start)
    latest_start: dict[str, dt.datetime] = {}
    latest_finish: dict[str, dt.datetime] = {}
    total_float: dict[str, int] = {}

    for process_id in reversed(list(nx.topological_sort(graph))):
        process = process_by_id[process_id]
        if process.started_at is not None:
            latest_start[process_id] = process.started_at
            latest_finish[process_id] = earliest_finish[process_id]
            total_float[process_id] = 0
            continue
        finish = min(
            (latest_start[successor] for successor in graph.successors(process_id)),
            default=completion_at,
        )
        start = subtract_business_days(finish, process.duration_business_days)
        latest_start[process_id] = start
        latest_finish[process_id] = finish
        total_float[process_id] = (
            count_business_days(earliest_start[process_id], finish)
            - process.duration_business_days
        )

    rows = []
    for process_id in nx.topological_sort(graph):
        process = process_by_id[process_id]
        computed_status = _compute_status(
            process=process,
            now=now,
            latest_start_at=latest_start[process_id],
            latest_finish_at=latest_finish[process_id],
        )
        rows.append(
            ScheduleRow(
                process_id=process.process_id,
                name=process.name,
                explicit_status=process.explicit_status,
                computed_status=computed_status,
                dependencies=process.dependencies,
                earliest_start_at=earliest_start[process_id],
                earliest_finish_at=earliest_finish[process_id],
                latest_start_at=latest_start[process_id],
                latest_finish_at=latest_finish[process_id],
                total_float_business_days=total_float[process_id],
                is_critical=total_float[process_id] == 0,
                due_at=process.due_at,
            )
        )

    return ScheduleProjection(
        project_id=project.project_id,
        project_name=project.name,
        start_at=project.start_at,
        completion_at=completion_at,
        rows=tuple(rows),
    )


def _compute_status(
    process: ProcessScheduleInput,
    now: dt.datetime,
    latest_start_at: dt.datetime,
    latest_finish_at: dt.datetime,
) -> ComputedScheduleStatus:
    if process.explicit_status == "done":
        return ComputedScheduleStatus.VALIDATED_DONE
    if process.explicit_status == "blocked" or process.unresolved_blocker_count > 0:
        return ComputedScheduleStatus.BLOCKED
    if process.due_at is not None and process.due_at < now:
        return ComputedScheduleStatus.DUE_ELAPSED_UNVERIFIED
    if latest_start_at < now:
        return ComputedScheduleStatus.LATE
    if process.due_at is not None and latest_finish_at > process.due_at:
        return ComputedScheduleStatus.AT_RISK
    return ComputedScheduleStatus.ON_TRACK
