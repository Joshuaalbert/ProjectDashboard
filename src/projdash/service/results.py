"""Command and query result models."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NonNegativeFloat

from projdash.service.errors import Error
from projdash.service.models import (
    AllocationSlice,
    CapacityBucket,
    ConvergenceData,
    CostBucket,
    ProcessCost,
    ProcessGraphEdge,
    ProcessGraphNode,
    ResourceCost,
    ResourceScheduleRow,
    ResourceUtilization,
    RoleCost,
    RoleUtilization,
    ScheduleBasis,
    StrictModel,
    UtilizationBucket,
    WarningSeverity,
)


class Warning(StrictModel):
    """Structured result warning."""

    code: str
    message: str
    severity: WarningSeverity
    details: dict[str, Any] = Field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        """Provide dict-like access for callers migrating from plain dicts."""
        return getattr(self, key)

    def __eq__(self, other: object) -> bool:
        """Compare equal to the documented plain-dict JSON shape."""
        if isinstance(other, dict):
            return self.model_dump(mode="json") == other
        return super().__eq__(other)


class CreatedIds(StrictModel):
    """Created ids grouped by stable plural keys."""

    process_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    resource_ids: list[str] = Field(default_factory=list)
    revision_ids: list[str] = Field(default_factory=list)
    calendar_ids: list[str] = Field(default_factory=list)
    blocker_ids: list[str] = Field(default_factory=list)
    retirement_event_ids: list[str] = Field(default_factory=list)


class RetiredIds(StrictModel):
    """Soft-retired ids grouped by stable plural keys."""

    process_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    retirement_event_ids: list[str] = Field(default_factory=list)


class RemovedIds(StrictModel):
    """Removed ids grouped by stable plural keys."""

    edge_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    calendar_exception_ids: list[str] = Field(default_factory=list)


class MatchedIds(StrictModel):
    """Matched ids grouped by stable plural keys."""

    process_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    resource_ids: list[str] = Field(default_factory=list)
    calendar_ids: list[str] = Field(default_factory=list)
    revision_ids: list[str] = Field(default_factory=list)


class CandidateOnlyIds(StrictModel):
    """Batch-local ids that were validated but not persisted."""

    requirement_ids: list[str] = Field(default_factory=list)


class BatchOperationResult(StrictModel):
    """Per-operation result entry returned in entity_ids.operation_ids."""

    operation_index: int = Field(ge=0)
    operation_id: str
    action: str
    status: Literal["applied", "no_op", "validated_only"]
    revision_id: str | None = None
    requirement_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    alias_process_id: str | None = None
    created_ids: CreatedIds = Field(default_factory=CreatedIds)
    retired_ids: RetiredIds = Field(default_factory=RetiredIds)
    removed_ids: RemovedIds = Field(default_factory=RemovedIds)
    matched_ids: MatchedIds = Field(default_factory=MatchedIds)
    candidate_only_ids: CandidateOnlyIds = Field(default_factory=CandidateOnlyIds)
    no_op_reason: str | None = None
    validation_reason: str | None = None

    def __getitem__(self, key: str) -> Any:
        """Provide dict-like access for compatibility with result dictionaries."""
        return getattr(self, key)


EntityIds = dict[str, Any]


class CommandResult(BaseModel):
    """Structured successful command result."""

    model_config = ConfigDict(extra="forbid")

    command_id: uuid.UUID
    ok: Literal[True] = True
    entity_ids: EntityIds = Field(default_factory=dict)
    warnings: list[Warning] = Field(default_factory=list)


class CommandErrorResult(BaseModel):
    """Structured failed command result."""

    model_config = ConfigDict(extra="forbid")

    command_id: uuid.UUID
    ok: Literal[False] = False
    error: Error
    warnings: list[Warning] = Field(default_factory=list)


class QueryResult(BaseModel):
    """Structured successful query result."""

    model_config = ConfigDict(extra="forbid")

    query_id: uuid.UUID
    ok: Literal[True] = True
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[Warning] = Field(default_factory=list)


class QueryErrorResult(BaseModel):
    """Structured failed query result."""

    model_config = ConfigDict(extra="forbid")

    query_id: uuid.UUID
    ok: Literal[False] = False
    error: Error
    warnings: list[Warning] = Field(default_factory=list)


class DependencyScheduleData(StrictModel):
    """Dependency-only schedule query data."""

    project_id: str
    as_of: AwareDatetime
    now: AwareDatetime
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[ProcessGraphEdge] = Field(default_factory=list)
    critical_path_process_ids: list[str] = Field(default_factory=list)


class CriticalPathData(StrictModel):
    """Dependency-only critical path query data."""

    project_id: str
    as_of: AwareDatetime | None = None
    now: AwareDatetime | None = None
    critical_path_process_ids: list[str] = Field(default_factory=list)
    critical_path: list[str] = Field(default_factory=list)


class ProcessGraphData(StrictModel):
    """Process graph query data."""

    project_id: str
    as_of: AwareDatetime
    now: AwareDatetime
    schedule_basis: ScheduleBasis
    converged: bool | None = None
    nodes: list[ProcessGraphNode] = Field(default_factory=list)
    edges: list[ProcessGraphEdge] = Field(default_factory=list)
    critical_path_process_ids: list[str] = Field(default_factory=list)
    allocation_slices: list[AllocationSlice] = Field(default_factory=list)


class BlockerData(StrictModel):
    """Blocker query data."""

    project_id: str
    as_of: AwareDatetime
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    blocked_process_ids: list[str] = Field(default_factory=list)


class ResourceScheduleData(StrictModel):
    """Resource schedule query data."""

    project_id: str
    as_of: AwareDatetime
    now: AwareDatetime
    planning_granularity: str
    processes: list[ResourceScheduleRow] = Field(default_factory=list)
    allocation_slices: list[AllocationSlice] = Field(default_factory=list)
    critical_path_process_ids: list[str] = Field(default_factory=list)
    converged: bool
    iteration_count: int = Field(ge=0)
    convergence: ConvergenceData


class CapacityData(StrictModel):
    """Resource capacity query data."""

    project_id: str
    as_of: AwareDatetime
    horizon_starts_at: AwareDatetime
    horizon_ends_at: AwareDatetime
    planning_granularity: str
    buckets: list[CapacityBucket] = Field(default_factory=list)


class UtilizationData(StrictModel):
    """Utilization query data."""

    project_id: str
    as_of: AwareDatetime
    planning_granularity: str
    by_resource: list[ResourceUtilization] = Field(default_factory=list)
    by_role: list[RoleUtilization] = Field(default_factory=list)
    time_series: list[UtilizationBucket] = Field(default_factory=list)
    overallocated_buckets: list[CapacityBucket] = Field(default_factory=list)


class CostData(StrictModel):
    """Cost query data."""

    project_id: str
    as_of: AwareDatetime
    currency: str
    total_cost: str
    by_resource: list[ResourceCost] = Field(default_factory=list)
    by_process: list[ProcessCost] = Field(default_factory=list)
    by_role: list[RoleCost] = Field(default_factory=list)
    time_series: list[CostBucket] = Field(default_factory=list)


class ResourceUtilizationData(StrictModel):
    """Compatibility alias for utilization output naming."""

    utilization: UtilizationData


class CostSummaryData(StrictModel):
    """Compatibility alias for cost output naming."""

    costs: CostData


class NumericSummary(StrictModel):
    """Small reusable numeric result object."""

    value: NonNegativeFloat
    unit: str
