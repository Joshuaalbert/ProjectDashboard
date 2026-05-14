"""Shared Pydantic models for ProjectDashboard service state and DSL payloads."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)

LOCAL_TIME_PATTERN = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


class StrictModel(BaseModel):
    """Base model for DSL payloads with strict extra-field rejection."""

    model_config = ConfigDict(extra="forbid")


class ProcessStatus(str, Enum):
    """Explicit project-manager controlled process status."""

    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    DONE = "done"
    CANCELED = "canceled"


class BlockerSeverity(str, Enum):
    """Blocker severity values."""

    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class DependencyType(str, Enum):
    """Supported dependency edge types."""

    FINISH_TO_START = "finish_to_start"


class AllocationPolicy(str, Enum):
    """Role requirement allocation policy."""

    SPLIT_ALLOWED = "split_allowed"
    CONTIGUOUS = "contiguous"


class CostUnit(str, Enum):
    """Resource cost unit."""

    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    FIXED = "fixed"


class PlanningGranularity(str, Enum):
    """Resource planning bucket granularity."""

    HOUR = "hour"


class BlockedPolicy(str, Enum):
    """Resource scheduling blocker policy."""

    EXCLUDE = "exclude"
    INCLUDE_AS_ZERO_CAPACITY = "include_as_zero_capacity"
    INCLUDE_NORMALLY = "include_normally"


class ScheduleBasis(str, Enum):
    """Schedule basis exposed by process graph queries."""

    DEPENDENCY_ONLY = "dependency_only"
    RESOURCE_AWARE = "resource_aware"


class ComputedStatus(str, Enum):
    """Derived process status values returned by schedule projections."""

    NOT_READY = "not_ready"
    READY = "ready"
    WORK_NOW = "work_now"
    LATE_RISK = "late_risk"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    CANCELED = "canceled"
    PARTIAL = "partial"
    UNALLOCATED = "unallocated"
    BLOCKED_ZERO_CAPACITY = "blocked_zero_capacity"


class AllocationState(str, Enum):
    """Resource allocation state for a process row."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNALLOCATED = "unallocated"
    BLOCKED_ZERO_CAPACITY = "blocked_zero_capacity"


class UnallocatedReason(str, Enum):
    """Structured infeasibility reasons returned by resource queries."""

    MISSING_ROLE = "missing_role"
    NO_ELIGIBLE_RESOURCE = "no_eligible_resource"
    NO_CALENDAR_CAPACITY = "no_calendar_capacity"
    BLOCKED = "blocked"
    PREDECESSOR_UNALLOCATED = "predecessor_unallocated"
    HORIZON_EXHAUSTED = "horizon_exhausted"
    CONTIGUOUS_WINDOW_UNAVAILABLE = "contiguous_window_unavailable"
    ITERATION_NOT_CONVERGED = "iteration_not_converged"


class TopologyDirection(str, Enum):
    """Topology filter direction."""

    ANCESTORS = "ancestors"
    DESCENDANTS = "descendants"
    ANCESTORS_AND_DESCENDANTS = "ancestors_and_descendants"


class RequiredRolesTransitionMode(str, Enum):
    """Compatibility mode for legacy required_roles payloads."""

    ALLOW_LEGACY = "allow_legacy"
    DUAL_WRITE_WARN = "dual_write_warn"
    REQUIRE_ROLE_REQUIREMENTS = "require_role_requirements"


class RoleConflictPolicy(str, Enum):
    """Collapse role requirement conflict policy."""

    REJECT = "reject"


class OperationStatus(str, Enum):
    """Batch operation result status."""

    APPLIED = "applied"
    NO_OP = "no_op"
    VALIDATED_ONLY = "validated_only"


class CostGroupBy(str, Enum):
    """Cost grouping dimension."""

    RESOURCE = "resource"
    PROCESS = "process"
    ROLE = "role"
    TIME = "time"


class WarningSeverity(str, Enum):
    """Wrapper warning severity."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ServiceConfig(StrictModel):
    """Service model configuration relevant to command validation."""

    required_roles_transition_mode: RequiredRolesTransitionMode = (
        RequiredRolesTransitionMode.ALLOW_LEGACY
    )
    max_resource_schedule_iterations: PositiveInt = 20


class ProcessIdentityMixin(BaseModel):
    """Mixin for payloads that accept exactly one process identity."""

    process_id: str | None = Field(default=None, min_length=1)
    process_symbol: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_process_identity(self):
        if (self.process_id is None) == (self.process_symbol is None):
            raise ValueError("Exactly one of process_id or process_symbol is required.")
        return self


class RoleRequirementCommand(StrictModel):
    """Role effort requirement accepted by process revision commands."""

    requirement_id: str | None = Field(default=None, min_length=1)
    role_id: str = Field(min_length=1)
    effort_hours: PositiveFloat
    min_allocation_hours_per_day: NonNegativeFloat | None = None
    max_allocation_hours_per_day: PositiveFloat | None = None
    required_resource_count: PositiveInt = 1
    allocation_policy: AllocationPolicy = AllocationPolicy.SPLIT_ALLOWED

    @model_validator(mode="after")
    def _validate_daily_bounds(self) -> RoleRequirementCommand:
        if (
            self.min_allocation_hours_per_day is not None
            and self.max_allocation_hours_per_day is not None
            and self.min_allocation_hours_per_day > self.max_allocation_hours_per_day
        ):
            raise ValueError(
                "min_allocation_hours_per_day must be less than or equal to "
                "max_allocation_hours_per_day."
            )
        return self


class CalendarWeeklyWindowCommand(StrictModel):
    """Recurring local availability window command item."""

    window_id: str | None = Field(default=None, min_length=1)
    weekday: int = Field(ge=0, le=6)
    start_local_time: str = Field(min_length=1)
    end_local_time: str = Field(min_length=1)
    capacity_hours: NonNegativeFloat

    @field_validator("start_local_time", "end_local_time")
    @classmethod
    def _validate_local_time(cls, value: str) -> str:
        if LOCAL_TIME_PATTERN.fullmatch(value) is None:
            raise ValueError("local times must use HH:MM[:SS] without an offset.")
        parsed = dt.time.fromisoformat(value)
        if parsed.tzinfo is not None:
            raise ValueError("local times must not include timezone or offset data.")
        return value

    @model_validator(mode="after")
    def _validate_window_order(self) -> CalendarWeeklyWindowCommand:
        starts = dt.time.fromisoformat(self.start_local_time)
        ends = dt.time.fromisoformat(self.end_local_time)
        if ends <= starts:
            raise ValueError("end_local_time must be after start_local_time.")
        return self


class CalendarExceptionCommand(StrictModel):
    """Calendar exception command item."""

    exception_id: str | None = Field(default=None, min_length=1)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    capacity_hours: NonNegativeFloat
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_exception_interval(self) -> CalendarExceptionCommand:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at.")
        return self


class ResourceHolidayCommand(StrictModel):
    """Resource-local zero-capacity interval."""

    holiday_id: str | None = Field(default=None, min_length=1)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_holiday_interval(self) -> ResourceHolidayCommand:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at.")
        return self


class UpsertResourcePayload(StrictModel):
    """Resource payload used by top-level and batch upsert commands."""

    resource_id: str | None = Field(default=None, min_length=1)
    name: str = Field(min_length=1)
    role_ids: list[str]
    calendar_id: str = Field(min_length=1)
    available_from_at: AwareDatetime
    available_until_at: AwareDatetime | None = None
    cost_rate: Decimal = Field(ge=Decimal("0"))
    cost_unit: CostUnit
    cost_currency: str | None = Field(default=None, min_length=3, max_length=3)
    holidays: list[ResourceHolidayCommand] = Field(default_factory=list)
    active: bool = True

    @field_validator("cost_currency")
    @classmethod
    def _normalize_currency(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @field_validator("holidays")
    @classmethod
    def _validate_holiday_ids(
        cls,
        value: list[ResourceHolidayCommand],
    ) -> list[ResourceHolidayCommand]:
        holiday_ids = [
            holiday.holiday_id
            for holiday in value
            if holiday.holiday_id is not None
        ]
        if len(holiday_ids) != len(set(holiday_ids)):
            raise ValueError("holiday_id values must be unique.")
        return value

    @model_validator(mode="after")
    def _validate_availability_interval(self) -> UpsertResourcePayload:
        if (
            self.available_until_at is not None
            and self.available_until_at <= self.available_from_at
        ):
            raise ValueError("available_until_at must be after available_from_at.")
        return self


class ProjectScope(StrictModel):
    """Whole-project query scope."""

    type: Literal["project"] = "project"


class TargetProcessScope(StrictModel):
    """Target-process query scope."""

    type: Literal["target_process"] = "target_process"
    process_id: str | None = Field(default=None, min_length=1)
    process_symbol: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_target_identity(self) -> TargetProcessScope:
        if (self.process_id is None) == (self.process_symbol is None):
            raise ValueError("Exactly one of process_id or process_symbol is required.")
        return self


class TopologyFilterScope(StrictModel):
    """Topology-filtered query scope."""

    type: Literal["topo_filter"] = "topo_filter"
    root_process_symbols: list[str] = Field(min_length=1)
    direction: TopologyDirection

    @field_validator("root_process_symbols")
    @classmethod
    def _validate_root_symbols(cls, value: list[str]) -> list[str]:
        if any(not symbol for symbol in value):
            raise ValueError("root_process_symbols must contain non-empty strings.")
        if len(value) != len(set(value)):
            raise ValueError("root_process_symbols must be unique.")
        return value


Scope = ProjectScope | TargetProcessScope | TopologyFilterScope


class WorkWindow(StrictModel):
    """Work-now or late-risk window data."""

    starts_at: AwareDatetime
    ends_at: AwareDatetime
    active: bool


class BlockerSummary(StrictModel):
    """Process blocker summary returned by graph queries."""

    unresolved_count: int = Field(ge=0)
    blocking_count: int = Field(ge=0)
    blocker_ids: list[str] = Field(default_factory=list)


class DependencyOnlyFields(StrictModel):
    """Dependency-only CPM fields returned on process graph nodes."""

    es_at: AwareDatetime
    ef_at: AwareDatetime
    ls_at: AwareDatetime
    lf_at: AwareDatetime
    slack_hours: float
    criticality_label: str


class ResourceAwareFields(StrictModel):
    """Resource-aware schedule fields returned on process graph nodes."""

    ready_at: AwareDatetime | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    resource_delay_hours: float = 0
    slack_hours: float | None = None
    allocation_state: AllocationState


class ProcessGraphNode(StrictModel):
    """Process graph node projection."""

    process_id: str
    process_symbol: str
    aliases: list[str] = Field(default_factory=list)
    name: str
    description: str = ""
    duration_hours: float = Field(ge=0)
    earliest_start_at: AwareDatetime | None = None
    due_at: AwareDatetime | None = None
    status: ProcessStatus
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    computed_status: ComputedStatus
    blocker_summary: BlockerSummary
    dependency_only: DependencyOnlyFields
    resource_aware: ResourceAwareFields | None = None
    work_now_window: WorkWindow
    late_risk_window: WorkWindow


class ProcessGraphEdge(StrictModel):
    """Persisted process dependency edge projection."""

    edge_id: str
    project_id: str
    predecessor_process_id: str
    successor_process_id: str
    predecessor_process_symbol: str
    successor_process_symbol: str
    dependency_type: DependencyType = DependencyType.FINISH_TO_START


class Blocker(StrictModel):
    """Blocker query row."""

    blocker_id: str
    project_id: str
    process_id: str
    process_symbol: str
    summary: str
    details: str | None = None
    severity: BlockerSeverity = BlockerSeverity.BLOCKING
    created_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    resolution: str | None = None
    is_resolved_as_of: bool | None = None
    is_blocking_as_of: bool | None = None


class DueDateHistoryEvent(StrictModel):
    """Due-date history event projection."""

    event_id: str
    project_id: str
    process_id: str | None = None
    process_symbol: str | None = None
    mutation_action: str
    edit_at: AwareDatetime
    before_due_at: AwareDatetime | None = None
    after_due_at: AwareDatetime | None = None
    command_id: str


class AllocationSlice(StrictModel):
    """Computed resource allocation slice."""

    slice_id: str
    project_id: str
    process_id: str
    requirement_id: str
    role_id: str
    resource_id: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    effort_hours: NonNegativeFloat
    capacity_hours: NonNegativeFloat
    cost_amount: str | None = None
    cost_currency: str | None = None
    iteration: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_slice_interval(self) -> AllocationSlice:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at.")
        return self


class ResourceScheduleRow(StrictModel):
    """Process row returned by resource schedule queries."""

    process_id: str
    name: str
    description: str = ""
    ready_at: AwareDatetime | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    dependency_only_starts_at: AwareDatetime
    dependency_only_ends_at: AwareDatetime
    resource_delay_hours: float = 0
    allocation_state: AllocationState
    status: ProcessStatus
    finished_at: AwareDatetime | None = None
    requirement_ids: list[str] = Field(default_factory=list)


class ReasonChange(StrictModel):
    """Convergence reason-change evidence."""

    process_id: str
    requirement_id: str | None = None
    before_reason: str | None = None
    after_reason: str | None = None


class ConvergenceData(StrictModel):
    """Resource schedule convergence metadata."""

    converged: bool
    iteration_count: int = Field(ge=0)
    max_iterations: PositiveInt
    tolerance_hours: NonNegativeFloat
    changed_process_ids: list[str] = Field(default_factory=list)
    reason_changes: list[ReasonChange] = Field(default_factory=list)
    allocation_fingerprint_changed: bool = False


class UnallocatedRequirement(StrictModel):
    """Unallocated requirement output row."""

    project_id: str
    process_id: str
    requirement_id: str
    role_id: str
    reason: UnallocatedReason
    message: str
    remaining_effort_hours: NonNegativeFloat
    allocated_effort_hours: NonNegativeFloat
    eligible_resource_ids: list[str] = Field(default_factory=list)
    first_feasible_starts_at: AwareDatetime | None = None


class CapacityBucket(StrictModel):
    """Expanded resource capacity bucket."""

    resource_id: str
    calendar_id: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    capacity_hours: NonNegativeFloat
    available_hours: NonNegativeFloat
    allocated_hours: NonNegativeFloat
    remaining_hours: float
    role_ids: list[str] = Field(default_factory=list)
    local_date: str
    local_week: str

    @model_validator(mode="after")
    def _validate_bucket_interval(self) -> CapacityBucket:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at.")
        return self


class ResourceUtilization(StrictModel):
    """Utilization grouped by resource."""

    resource_id: str
    capacity_hours: NonNegativeFloat
    available_hours: NonNegativeFloat
    allocated_hours: NonNegativeFloat
    remaining_hours: float
    utilization_ratio: NonNegativeFloat


class RoleUtilization(StrictModel):
    """Utilization grouped by role."""

    role_id: str
    demanded_effort_hours: NonNegativeFloat
    fulfilled_effort_hours: NonNegativeFloat
    unallocated_effort_hours: NonNegativeFloat


class UtilizationBucket(StrictModel):
    """Time-series utilization bucket."""

    starts_at: AwareDatetime
    ends_at: AwareDatetime
    resource_id: str
    role_ids: list[str] = Field(default_factory=list)
    capacity_hours: NonNegativeFloat
    allocated_hours: NonNegativeFloat
    utilization_ratio: NonNegativeFloat


class ResourceCost(StrictModel):
    """Cost grouped by resource."""

    resource_id: str
    cost_unit: CostUnit
    allocated_hours: NonNegativeFloat
    currency: str
    cost_amount: str


class ProcessCost(StrictModel):
    """Cost grouped by process."""

    process_id: str
    allocated_hours: NonNegativeFloat
    currency: str
    cost_amount: str


class RoleCost(StrictModel):
    """Cost grouped by role."""

    role_id: str
    allocated_hours: NonNegativeFloat
    currency: str
    cost_amount: str


class CostBucket(StrictModel):
    """Time-series cost bucket."""

    starts_at: AwareDatetime
    ends_at: AwareDatetime
    resource_id: str | None = None
    process_id: str | None = None
    role_id: str | None = None
    allocated_hours: NonNegativeFloat
    currency: str
    cost_amount: str


def validate_iana_timezone(value: str) -> str:
    """Validate and return an IANA timezone name."""
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone name.") from exc
    return value


def validate_unique_non_empty(values: list[str], field_name: str) -> list[str]:
    """Validate that a string list has no empty or duplicate values."""
    if any(not value for value in values):
        raise ValueError(f"{field_name} must contain non-empty strings.")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must be unique.")
    return values


class ProjectRecord(StrictModel):
    """Persisted project fact."""

    project_id: str
    name: str
    start_at: AwareDatetime
    default_currency: str = "USD"


class ProcessRecord(StrictModel):
    """Persisted process fact."""

    process_id: str
    project_id: str
    symbol: str
    status: ProcessStatus = ProcessStatus.PLANNED
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None


class ScheduleSnapshotRecord(StrictModel):
    """Committed schedule completion snapshot for slippage history."""

    snapshot_id: str
    project_id: str
    committed_at: AwareDatetime
    terminal_process_symbols: list[str] = Field(default_factory=list)
    schedule_basis: ScheduleBasis = ScheduleBasis.RESOURCE_AWARE
    completion_at: AwareDatetime | None = None
    derived_due_at: AwareDatetime | None = None
    horizon_starts_at: AwareDatetime
    horizon_ends_at: AwareDatetime
    converged: bool | None = None
    unallocated_count: int = Field(ge=0, default=0)
    note: str | None = None

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        if kwargs.get("mode") == "json":
            for field in (
                "committed_at",
                "completion_at",
                "derived_due_at",
                "horizon_starts_at",
                "horizon_ends_at",
            ):
                if isinstance(data.get(field), str) and data[field].endswith("Z"):
                    data[field] = f"{data[field][:-1]}+00:00"
        return data


class ProcessRevisionRecord(StrictModel):
    """Append-only process planning revision."""

    revision_id: str
    process_id: str
    project_id: str
    effective_at: AwareDatetime
    name: str
    description: str = ""
    duration_business_days: int = Field(ge=0)
    dependencies: list[str] = Field(default_factory=list)
    due_at: AwareDatetime | None = None
    earliest_start_at: AwareDatetime | None = None
    start_at_earliest: bool = False
    delay_after_dependencies_business_days: int = Field(default=0, ge=0)
    required_roles: dict[str, float] = Field(default_factory=dict)
    role_requirements: list[RoleRequirementCommand] = Field(default_factory=list)
    assumption_note: str | None = None


class BlockerRecord(StrictModel):
    """Persisted blocker fact."""

    blocker_id: str
    project_id: str
    process_id: str
    description: str
    opened_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    summary: str | None = None
    details: str | None = None
    severity: BlockerSeverity = BlockerSeverity.BLOCKING
    created_at: AwareDatetime | None = None
    resolution: str | None = None


JsonObject = dict[str, Any]
