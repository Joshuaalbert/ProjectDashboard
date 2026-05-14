"""Pydantic query models for agent and Python callers."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    Field,
    NonNegativeFloat,
    PositiveInt,
    field_validator,
    model_validator,
)

from projdash.service.models import (
    BlockedPolicy,
    CostGroupBy,
    PlanningGranularity,
    ProjectScope,
    Scope,
    StrictModel,
    TargetProcessScope,
    TopologyFilterScope,
    validate_unique_non_empty,
)


class QueryModel(StrictModel):
    """Base query model with strict field handling."""


class GetProject(QueryModel):
    """Fetch project metadata."""

    action: Literal["get_project"] = "get_project"
    project_id: str = Field(min_length=1)


class QueryProjectCatalog(QueryModel):
    """Fetch project-owned management facts for guided UI forms."""

    action: Literal["query_project_catalog"] = "query_project_catalog"
    project_id: str = Field(min_length=1)


class ResourceOptionsMixin(StrictModel):
    """Shared resource scheduling query options."""

    planning_granularity: PlanningGranularity = PlanningGranularity.HOUR
    max_iterations: PositiveInt = 20
    convergence_tolerance_hours: NonNegativeFloat = 0
    blocked_policy: BlockedPolicy = BlockedPolicy.EXCLUDE


class HorizonMixin(StrictModel):
    """Shared timezone-aware query horizon."""

    horizon_starts_at: AwareDatetime
    horizon_ends_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_horizon(self):
        if self.horizon_ends_at <= self.horizon_starts_at:
            raise ValueError("horizon_ends_at must be after horizon_starts_at.")
        return self


class ScopedProcessQuery(QueryModel):
    """Base query with an optional process scope."""

    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    now: AwareDatetime
    scope: Scope | None = None


class QuerySchedule(ScopedProcessQuery):
    """Compute dependency-only schedule projection for a project."""

    action: Literal["query_schedule"] = "query_schedule"


class QueryCriticalPath(ScopedProcessQuery):
    """Compute dependency-only critical path process ids for a project."""

    action: Literal["query_critical_path"] = "query_critical_path"


class QueryProcessGraph(QueryModel, ResourceOptionsMixin):
    """Fetch process graph with dependency-only and optional resource fields."""

    action: Literal["query_process_graph"] = "query_process_graph"
    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    now: AwareDatetime
    scope: Scope | None = None
    include_resource_fields: bool = False
    horizon_starts_at: AwareDatetime | None = None
    horizon_ends_at: AwareDatetime | None = None
    include_allocation_slices: bool = False

    @model_validator(mode="after")
    def _validate_resource_options(self) -> QueryProcessGraph:
        has_horizon = (
            self.horizon_starts_at is not None or self.horizon_ends_at is not None
        )
        if self.include_resource_fields:
            if self.horizon_starts_at is None or self.horizon_ends_at is None:
                raise ValueError(
                    "horizon_starts_at and horizon_ends_at are required when "
                    "include_resource_fields is true."
                )
            if self.horizon_ends_at <= self.horizon_starts_at:
                raise ValueError("horizon_ends_at must be after horizon_starts_at.")
            return self
        if has_horizon:
            raise ValueError(
                "Resource horizon fields are only accepted when "
                "include_resource_fields is true."
            )
        if self.include_allocation_slices:
            raise ValueError(
                "include_allocation_slices is only accepted when "
                "include_resource_fields is true."
            )
        return self


class QueryBlockers(QueryModel):
    """Fetch project blockers."""

    action: Literal["query_blockers"] = "query_blockers"
    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    process_ids: list[str] | None = None
    process_symbols: list[str] | None = None
    include_resolved: bool = False

    @field_validator("process_ids", "process_symbols")
    @classmethod
    def _validate_filter_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return validate_unique_non_empty(value, "process filter")


class QueryDueDateHistory(QueryModel):
    """Fetch due-date history."""

    action: Literal["query_due_date_history"] = "query_due_date_history"
    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    scope: Scope | None = None
    target_process_id: str | None = Field(default=None, min_length=1)
    target_process_symbol: str | None = Field(default=None, min_length=1)
    include_project_total: bool = True

    @model_validator(mode="after")
    def _validate_target_aliases(self) -> QueryDueDateHistory:
        legacy_target_count = sum(
            value is not None
            for value in (self.target_process_id, self.target_process_symbol)
        )
        if self.scope is not None and legacy_target_count:
            raise ValueError("target_process_* aliases are mutually exclusive with scope.")
        if legacy_target_count > 1:
            raise ValueError("Only one target_process_* alias may be supplied.")
        return self


class QueryResourceSchedule(QueryModel, HorizonMixin, ResourceOptionsMixin):
    """Compute resource-constrained schedule projection."""

    action: Literal["query_resource_schedule"] = "query_resource_schedule"
    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    now: AwareDatetime
    include_allocation_slices: bool = False


class QueryUtilization(QueryModel, HorizonMixin, ResourceOptionsMixin):
    """Compute resource utilization aggregates."""

    action: Literal["query_utilization"] = "query_utilization"
    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    now: AwareDatetime


class QueryCosts(QueryModel, HorizonMixin, ResourceOptionsMixin):
    """Compute cost aggregates from resource allocation evidence."""

    action: Literal["query_costs"] = "query_costs"
    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    now: AwareDatetime
    scope: Scope | None = None
    target_process_id: str | None = Field(default=None, min_length=1)
    target_process_symbol: str | None = Field(default=None, min_length=1)
    resource_ids: list[str] | None = None
    role_ids: list[str] | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    group_by: list[CostGroupBy] = Field(
        default_factory=lambda: [
            CostGroupBy.RESOURCE,
            CostGroupBy.PROCESS,
            CostGroupBy.ROLE,
            CostGroupBy.TIME,
        ]
    )

    @field_validator("resource_ids", "role_ids")
    @classmethod
    def _validate_filters(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("Filters must be non-empty when supplied.")
        return validate_unique_non_empty(value, "filter")

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @field_validator("group_by")
    @classmethod
    def _validate_group_by(cls, value: list[CostGroupBy]) -> list[CostGroupBy]:
        if len(value) != len(set(value)):
            raise ValueError("group_by values must be unique.")
        return value

    @model_validator(mode="after")
    def _validate_cost_scope_aliases(self) -> QueryCosts:
        legacy_target_count = sum(
            value is not None
            for value in (self.target_process_id, self.target_process_symbol)
        )
        if self.scope is not None and legacy_target_count:
            raise ValueError("target_process_* aliases are mutually exclusive with scope.")
        if legacy_target_count > 1:
            raise ValueError("Only one target_process_* alias may be supplied.")
        return self


class QueryResourceCapacity(QueryModel, HorizonMixin):
    """Expand resource capacity buckets."""

    action: Literal["query_resource_capacity"] = "query_resource_capacity"
    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    resource_ids: list[str] | None = None
    role_ids: list[str] | None = None
    planning_granularity: PlanningGranularity = PlanningGranularity.HOUR

    @field_validator("resource_ids", "role_ids")
    @classmethod
    def _validate_filters(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("Filters must be non-empty when supplied.")
        return validate_unique_non_empty(value, "filter")


class QueryUnallocatedRequirements(QueryModel, HorizonMixin, ResourceOptionsMixin):
    """Return unallocated role requirements for a resource schedule."""

    action: Literal["query_unallocated_requirements"] = "query_unallocated_requirements"
    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    now: AwareDatetime


Query = Annotated[
    GetProject
    | QueryProjectCatalog
    | QuerySchedule
    | QueryCriticalPath
    | QueryProcessGraph
    | QueryBlockers
    | QueryDueDateHistory
    | QueryResourceSchedule
    | QueryUtilization
    | QueryCosts
    | QueryResourceCapacity
    | QueryUnallocatedRequirements,
    Field(discriminator="action"),
]


class QueryEnvelope(StrictModel):
    """Envelope shared by Python and JSON query callers."""

    query_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    query: Query


__all__ = [
    "GetProject",
    "ProjectScope",
    "QueryBlockers",
    "QueryCosts",
    "QueryCriticalPath",
    "QueryDueDateHistory",
    "QueryEnvelope",
    "QueryProcessGraph",
    "QueryProjectCatalog",
    "QueryResourceCapacity",
    "QueryResourceSchedule",
    "QuerySchedule",
    "QueryUnallocatedRequirements",
    "QueryUtilization",
    "TargetProcessScope",
    "TopologyFilterScope",
]
