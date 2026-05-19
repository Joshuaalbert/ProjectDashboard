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
    CostGroupBy,
    PlanningGranularity,
    ProjectScope,
    Scope,
    SlackOutboxStatus,
    SlackRunStatus,
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


class QueryProjects(QueryModel):
    """List projects available in the service database."""

    action: Literal["query_projects"] = "query_projects"


class QueryProjectCatalog(QueryModel):
    """Fetch project-owned management facts for guided UI forms."""

    action: Literal["query_project_catalog"] = "query_project_catalog"
    project_id: str = Field(min_length=1)


class QueryMilestones(QueryModel):
    """Fetch project milestone definitions."""

    action: Literal["query_milestones"] = "query_milestones"
    project_id: str = Field(min_length=1)
    include_inactive: bool = False


class QuerySlackProjectConfig(QueryModel):
    """Fetch Slack project settings, mappings, and collection cursors."""

    action: Literal["query_slack_project_config"] = "query_slack_project_config"
    project_id: str = Field(min_length=1)


class QuerySlackBotToken(QueryModel):
    """Fetch the encrypted UI-managed Slack bot token blob for decryption."""

    action: Literal["query_slack_bot_token"] = "query_slack_bot_token"
    project_id: str = Field(min_length=1)


class QuerySlackRuns(QueryModel):
    """Fetch Slack background run/job records."""

    action: Literal["query_slack_runs"] = "query_slack_runs"
    project_id: str = Field(min_length=1)
    statuses: list[SlackRunStatus] | None = None
    limit: PositiveInt | None = None

    @field_validator("statuses")
    @classmethod
    def _validate_statuses(
        cls,
        value: list[SlackRunStatus] | None,
    ) -> list[SlackRunStatus] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("statuses must be non-empty when supplied.")
        if len(value) != len(set(value)):
            raise ValueError("statuses must be unique.")
        return value


class QueryPendingSlackOutbox(QueryModel):
    """Fetch Slack outbox rows by status for delivery workers."""

    action: Literal["query_pending_slack_outbox"] = "query_pending_slack_outbox"
    project_id: str = Field(min_length=1)
    statuses: list[SlackOutboxStatus] = Field(
        default_factory=lambda: [SlackOutboxStatus.DRAFT],
    )
    limit: PositiveInt | None = None

    @field_validator("statuses")
    @classmethod
    def _validate_statuses(
        cls,
        value: list[SlackOutboxStatus],
    ) -> list[SlackOutboxStatus]:
        if not value:
            raise ValueError("statuses must be non-empty.")
        if len(value) != len(set(value)):
            raise ValueError("statuses must be unique.")
        return value


class QuerySlackOutbox(QueryPendingSlackOutbox):
    """Fetch Slack outbox rows by status for UI review and history."""

    action: Literal["query_slack_outbox"] = "query_slack_outbox"


class ResourceOptionsMixin(StrictModel):
    """Shared resource scheduling query options."""

    planning_granularity: PlanningGranularity = PlanningGranularity.HOUR
    max_iterations: PositiveInt = 20
    convergence_tolerance_hours: NonNegativeFloat = 0


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
    include_allocation_slices: bool = False

    @model_validator(mode="after")
    def _validate_resource_options(self) -> QueryProcessGraph:
        if self.include_allocation_slices and not self.include_resource_fields:
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


class QueryScheduleSnapshots(QueryModel):
    """Fetch committed schedule snapshots for slippage history."""

    action: Literal["query_schedule_snapshots"] = "query_schedule_snapshots"
    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    terminal_process_symbols: list[str] | None = None
    milestone_id: str | None = Field(default=None, min_length=1)

    @field_validator("terminal_process_symbols")
    @classmethod
    def _validate_terminal_symbols(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return validate_unique_non_empty(value, "terminal_process_symbols")

    @model_validator(mode="after")
    def _validate_milestone_or_terminal_symbols(self) -> QueryScheduleSnapshots:
        if self.milestone_id is not None and self.terminal_process_symbols:
            raise ValueError(
                "milestone_id and terminal_process_symbols are mutually exclusive."
            )
        return self


class QueryResourceSchedule(QueryModel, ResourceOptionsMixin):
    """Compute resource-constrained schedule projection."""

    action: Literal["query_resource_schedule"] = "query_resource_schedule"
    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    now: AwareDatetime
    scope: Scope | None = None
    include_allocation_slices: bool = False


class QueryAgentContext(QueryModel, ResourceOptionsMixin):
    """Generate a concise project-management context report for agents."""

    action: Literal["query_agent_context"] = "query_agent_context"
    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    now: AwareDatetime
    scope: Scope | None = None
    terminal_process_symbols: list[str] | None = None
    snapshot_limit: PositiveInt = 5

    @field_validator("terminal_process_symbols")
    @classmethod
    def _validate_terminal_symbols(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return validate_unique_non_empty(value, "terminal_process_symbols")


class QueryUtilization(QueryModel, ResourceOptionsMixin):
    """Compute resource utilization aggregates."""

    action: Literal["query_utilization"] = "query_utilization"
    project_id: str = Field(min_length=1)
    as_of: AwareDatetime
    now: AwareDatetime
    scope: Scope | None = None


class QueryCosts(QueryModel, ResourceOptionsMixin):
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


Query = Annotated[
    GetProject
    | QueryProjects
    | QueryProjectCatalog
    | QueryMilestones
    | QuerySlackProjectConfig
    | QuerySlackBotToken
    | QuerySlackRuns
    | QueryPendingSlackOutbox
    | QuerySlackOutbox
    | QuerySchedule
    | QueryCriticalPath
    | QueryProcessGraph
    | QueryBlockers
    | QueryScheduleSnapshots
    | QueryResourceSchedule
    | QueryAgentContext
    | QueryUtilization
    | QueryCosts
    | QueryResourceCapacity,
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
    "QueryAgentContext",
    "QueryCosts",
    "QueryCriticalPath",
    "QueryScheduleSnapshots",
    "QueryEnvelope",
    "QueryMilestones",
    "QuerySlackBotToken",
    "QuerySlackOutbox",
    "QuerySlackRuns",
    "QueryPendingSlackOutbox",
    "QueryProcessGraph",
    "QueryProjects",
    "QueryProjectCatalog",
    "QuerySlackProjectConfig",
    "QueryResourceCapacity",
    "QueryResourceSchedule",
    "QuerySchedule",
    "QueryUtilization",
    "TargetProcessScope",
    "TopologyFilterScope",
]
