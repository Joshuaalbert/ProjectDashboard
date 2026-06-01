"""Shared Pydantic models for ProjectDashboard service state and DSL payloads."""

from __future__ import annotations

import datetime as dt
import json
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
    """Legacy lifecycle assertion accepted by compatibility commands."""

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


class ScheduleBasis(str, Enum):
    """Schedule basis exposed by process graph queries."""

    DEPENDENCY_ONLY = "dependency_only"
    RESOURCE_AWARE = "resource_aware"


class ComputedStatus(str, Enum):
    """Derived process status values returned by schedule projections."""

    WAITING = "waiting"
    EARLY_START = "early_start"
    READY = "ready"
    STARTED = "started"
    DUE = "due"
    FINISHED = "finished"


class AllocationState(str, Enum):
    """Resource allocation state for a process row."""

    COMPLETE = "complete"


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


class SlackOutboxStatus(str, Enum):
    """Slack outbox delivery states."""

    DRAFT = "draft"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class SlackRunStatus(str, Enum):
    """Slack background run states."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NO_NEW_DATA = "no_new_data"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_active(self) -> bool:
        return self in {SlackRunStatus.QUEUED, SlackRunStatus.RUNNING}


class PMCommunicationEvidenceType(str, Enum):
    """Machine-readable PM communication evidence categories."""

    PROCESS_FULL_UPDATE = "process_full_update"
    PROCESS_PRE_START_3_DAY = "process_pre_start_3_day"
    PROCESS_PRE_START_24_HOUR = "process_pre_start_24_hour"
    PROCESS_OVERDUE_CHECKIN = "process_overdue_checkin"
    PROCESS_IN_PROGRESS_CHECKIN = "process_in_progress_checkin"
    RESOURCE_ASSIGNMENT_REVIEW = "resource_assignment_review"
    MESSAGE_RECEIPT_ACK = "message_receipt_ack"
    PROJECT_UPDATE_NOTICE = "project_update_notice"


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
    effort_hours: PositiveInt
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


class ResourceCalendarOverrideCommand(StrictModel):
    """Time-ranged replacement calendar for a resource."""

    rule_id: str | None = Field(default=None, min_length=1)
    calendar_id: str | None = Field(default=None, min_length=1)
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_override_interval(self) -> ResourceCalendarOverrideCommand:
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at.")
        return self


class PMEvidenceClaimCommand(StrictModel):
    """Claim that a Slack message satisfies a PM communication obligation."""

    evidence_type: PMCommunicationEvidenceType
    resource_id: str | None = Field(default=None, min_length=1)
    process_id: str | None = Field(default=None, min_length=1)
    process_symbol: str | None = Field(default=None, min_length=1)
    obligation_id: str | None = Field(default=None, min_length=1)
    content_hash: str | None = Field(default=None, min_length=1)
    evidence_note: str | None = Field(default=None, min_length=1)


def _validate_slack_blocks(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate generic Slack Block Kit payloads without owning Slack's schema."""
    if len(value) > 50:
        raise ValueError("Slack messages may contain at most 50 blocks.")
    for index, block in enumerate(value):
        block_type = block.get("type")
        if not isinstance(block_type, str) or not block_type.strip():
            raise ValueError(f"Slack block {index} must include a non-empty type.")
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Slack blocks must be JSON-serializable.") from exc
    return value


class SlackOutboxMessageCommand(StrictModel):
    """Slack outbox message payload accepted by create commands."""

    target_type: Literal["dm", "channel"] = "dm"
    resource_id: str | None = Field(default=None, min_length=1)
    slack_user_id: str | None = Field(default=None, min_length=1)
    slack_channel_id: str | None = Field(default=None, min_length=1)
    body: str = Field(min_length=1)
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    generated_body: str | None = Field(default=None, min_length=1)
    content_hash: str = Field(min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    created_at: AwareDatetime
    status: SlackOutboxStatus = SlackOutboxStatus.DRAFT
    pm_evidence_claims: list[PMEvidenceClaimCommand] = Field(default_factory=list)

    @field_validator("blocks")
    @classmethod
    def _validate_blocks(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _validate_slack_blocks(value)

    @model_validator(mode="before")
    @classmethod
    def _infer_target_type(cls, data: object) -> object:
        if not isinstance(data, dict) or data.get("target_type"):
            return data
        if not data.get("slack_user_id") and data.get("slack_channel_id"):
            return {**data, "target_type": "channel"}
        return data

    @model_validator(mode="after")
    def _validate_target(self) -> SlackOutboxMessageCommand:
        if self.target_type == "dm":
            if not self.slack_user_id:
                raise ValueError("slack_user_id is required for dm outbox messages.")
            if self.slack_channel_id is not None:
                raise ValueError(
                    "slack_channel_id is not accepted for dm outbox messages."
                )
            return self
        if not self.slack_channel_id:
            raise ValueError("slack_channel_id is required for channel outbox messages.")
        if self.slack_user_id is not None:
            raise ValueError("slack_user_id is not accepted for channel outbox messages.")
        if self.resource_id is not None:
            raise ValueError("resource_id is not accepted for channel outbox messages.")
        return self


class UpsertResourcePayload(StrictModel):
    """Resource payload used by top-level and batch upsert commands."""

    resource_id: str | None = Field(default=None, min_length=1)
    name: str = Field(min_length=1)
    resource_type: Literal["internal", "external"] = "internal"
    role_ids: list[str]
    calendar_id: str = Field(min_length=1)
    available_from_at: AwareDatetime
    available_until_at: AwareDatetime | None = None
    cost_rate: Decimal = Field(ge=Decimal("0"))
    cost_unit: CostUnit
    cost_currency: str | None = Field(default=None, min_length=3, max_length=3)
    holidays: list[ResourceHolidayCommand] = Field(default_factory=list)
    calendar_overrides: list[ResourceCalendarOverrideCommand] = Field(
        default_factory=list,
    )
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

    @field_validator("calendar_overrides")
    @classmethod
    def _validate_calendar_override_ids(
        cls,
        value: list[ResourceCalendarOverrideCommand],
    ) -> list[ResourceCalendarOverrideCommand]:
        rule_ids = [rule.rule_id for rule in value if rule.rule_id is not None]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("calendar override rule_id values must be unique.")
        return value

    @model_validator(mode="after")
    def _validate_availability_interval(self) -> UpsertResourcePayload:
        if (
            self.available_until_at is not None
            and self.available_until_at <= self.available_from_at
        ):
            raise ValueError("available_until_at must be after available_from_at.")
        ordered_rules = sorted(
            self.calendar_overrides,
            key=lambda rule: rule.starts_at,
        )
        previous_end: dt.datetime | None = None
        for index, rule in enumerate(ordered_rules):
            if previous_end is None:
                if index > 0:
                    raise ValueError("calendar overrides must not overlap.")
            elif rule.starts_at < previous_end:
                raise ValueError("calendar overrides must not overlap.")
            previous_end = rule.ends_at
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
    es_at: AwareDatetime | None = None
    ef_at: AwareDatetime | None = None
    ls_at: AwareDatetime | None = None
    lf_at: AwareDatetime | None = None
    inferred_duration_hours: float | None = None
    resource_delay_hours: float = 0
    slack_hours: float | None = None
    criticality_label: str = "non_critical"
    allocation_state: AllocationState
    allocation_diagnostic: str | None = None


class ProcessGraphNode(StrictModel):
    """Process graph node projection."""

    process_id: str
    process_symbol: str
    aliases: list[str] = Field(default_factory=list)
    name: str
    description: str = ""
    duration_hours: float = Field(ge=0)
    inferred_duration_hours: float | None = None
    earliest_start_at: AwareDatetime | None = None
    status: str
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
    resolution_owner_resource_id: str | None = None
    is_resolved_as_of: bool | None = None
    is_blocking_as_of: bool | None = None
    immediate_blocked_processes: list[dict[str, Any]] = Field(default_factory=list)
    needed_by_role_ids: list[str] = Field(default_factory=list)
    needed_by_resource_ids: list[str] = Field(default_factory=list)


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
    resource_es_at: AwareDatetime | None = None
    resource_ef_at: AwareDatetime | None = None
    resource_ls_at: AwareDatetime | None = None
    resource_lf_at: AwareDatetime | None = None
    resource_slack_hours: float | None = None
    schedule_window_starts_at: AwareDatetime | None = None
    schedule_window_ends_at: AwareDatetime | None = None
    schedule_buffer_hours: float | None = None
    schedule_elapsed_hours: float | None = None
    role_sensitivity: list[dict[str, object]] = Field(default_factory=list)
    max_makespan_sensitivity_hours: float | None = None
    sensitivity_label: str = "unknown"
    inferred_duration_hours: float | None = None
    resource_delay_hours: float = 0
    allocation_state: AllocationState
    allocation_diagnostic: str | None = None
    status: str
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
    process_type: Literal["standard", "blocker"] = "standard"


class ProcessRolePinRecord(StrictModel):
    """Pinned resource forecast for one process-role requirement."""

    pin_id: str
    project_id: str
    process_id: str
    requirement_id: str | None = None
    role_id: str
    resource_id: str
    pinned_at: AwareDatetime
    forecast_finish_at: AwareDatetime
    status: Literal["pinned_started", "pinned_finished"] = "pinned_started"
    verified_done_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    note: str | None = None

    @model_validator(mode="after")
    def _validate_pin(self) -> ProcessRolePinRecord:
        if self.pinned_at > self.updated_at:
            raise ValueError("pinned_at must be no later than updated_at.")
        if self.status == "pinned_finished" and self.verified_done_at is None:
            raise ValueError("pinned_finished requires verified_done_at.")
        if self.status == "pinned_started" and self.verified_done_at is not None:
            raise ValueError("pinned_started must not set verified_done_at.")
        if (
            self.verified_done_at is not None
            and self.verified_done_at < self.pinned_at
        ):
            raise ValueError("verified_done_at must be at or after pinned_at.")
        if (
            self.verified_done_at is not None
            and self.verified_done_at > self.updated_at
        ):
            raise ValueError("verified_done_at must be no later than updated_at.")
        if self.verified_done_at is not None:
            self.forecast_finish_at = self.verified_done_at
        if self.forecast_finish_at < self.pinned_at:
            raise ValueError("forecast_finish_at must be at or after pinned_at.")
        if self.created_at > self.updated_at:
            raise ValueError("created_at must be no later than updated_at.")
        return self


class ProcessEvidenceLineItemRecord(StrictModel):
    """Persisted PM evidence recency for one process line item."""

    evidence_line_id: str
    project_id: str
    process_id: str
    process_symbol: str
    line_item: str = Field(min_length=1)
    last_modified_at: AwareDatetime
    last_evidence_at: AwareDatetime | None = None
    evidence_note: str | None = None
    evidence_source: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_timestamps(self) -> ProcessEvidenceLineItemRecord:
        if self.created_at > self.updated_at:
            raise ValueError("created_at must be no later than updated_at.")
        if self.last_evidence_at is not None and self.last_evidence_at > self.updated_at:
            raise ValueError("last_evidence_at must be no later than updated_at.")
        return self

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        if kwargs.get("mode") == "json":
            for field in (
                "last_modified_at",
                "last_evidence_at",
                "created_at",
                "updated_at",
            ):
                if isinstance(data.get(field), str) and data[field].endswith("Z"):
                    data[field] = f"{data[field][:-1]}+00:00"
        return data


class ResourceEvidenceLineItemRecord(StrictModel):
    """Persisted PM evidence recency for one resource line item."""

    evidence_line_id: str
    project_id: str
    resource_id: str
    line_item: str = Field(min_length=1)
    last_modified_at: AwareDatetime
    last_evidence_at: AwareDatetime | None = None
    evidence_note: str | None = None
    evidence_source: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_timestamps(self) -> ResourceEvidenceLineItemRecord:
        if self.created_at > self.updated_at:
            raise ValueError("created_at must be no later than updated_at.")
        if self.last_evidence_at is not None and self.last_evidence_at > self.updated_at:
            raise ValueError("last_evidence_at must be no later than updated_at.")
        return self


class TeammateWorkPlanForecastRecord(StrictModel):
    """Legacy SQLite work-plan forecast row migrated into process-role pins."""

    forecast_id: str
    project_id: str
    work_plan_id: str
    process_id: str
    requirement_id: str | None = None
    role_id: str
    forecast_finish_at: AwareDatetime
    note: str | None = None


class TeammateWorkPlanRecord(StrictModel):
    """Legacy SQLite work-plan row migrated into process-role pins on load."""

    work_plan_id: str
    project_id: str
    resource_id: str
    starts_at: AwareDatetime
    planning_release_at: AwareDatetime
    status: Literal["accepted", "advisory", "canceled"] = "accepted"
    capacity_policy: Literal["opaque"] = "opaque"
    forecasts: list[TeammateWorkPlanForecastRecord] = Field(default_factory=list)
    source: str | None = None
    note: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_plan(self) -> TeammateWorkPlanRecord:
        if self.starts_at > self.updated_at and self.status == "accepted":
            raise ValueError("accepted work plan starts_at must be no later than updated_at.")
        if self.planning_release_at <= self.starts_at:
            raise ValueError("planning_release_at must be after starts_at.")
        forecast_ids = [forecast.forecast_id for forecast in self.forecasts]
        if len(forecast_ids) != len(set(forecast_ids)):
            raise ValueError("forecast_id values must be unique.")
        if self.status == "accepted" and not self.forecasts:
            raise ValueError("accepted work plans require at least one forecast.")
        for forecast in self.forecasts:
            if forecast.project_id != self.project_id:
                raise ValueError("forecast project_id must match work plan project_id.")
            if forecast.work_plan_id != self.work_plan_id:
                raise ValueError("forecast work_plan_id must match work_plan_id.")
            if forecast.forecast_finish_at < self.starts_at:
                raise ValueError("forecast_finish_at must be at or after starts_at.")
            if forecast.forecast_finish_at > self.planning_release_at:
                raise ValueError(
                    "forecast_finish_at must be no later than planning_release_at."
                )
        return self


class ScheduleSnapshotRecord(StrictModel):
    """Committed schedule completion snapshot for slippage history."""

    snapshot_id: str
    project_id: str
    committed_at: AwareDatetime
    terminal_process_symbols: list[str] = Field(default_factory=list)
    schedule_basis: ScheduleBasis = ScheduleBasis.RESOURCE_AWARE
    completion_at: AwareDatetime | None = None
    converged: bool | None = None
    role_sensitivity: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        if kwargs.get("mode") == "json":
            for field in (
                "committed_at",
                "completion_at",
            ):
                if isinstance(data.get(field), str) and data[field].endswith("Z"):
                    data[field] = f"{data[field][:-1]}+00:00"
        return data


class MilestoneRecord(StrictModel):
    """Named subset of project terminal processes for slippage tracking."""

    milestone_id: str
    project_id: str
    name: str
    description: str = ""
    process_symbols: list[str] = Field(default_factory=list)
    active: bool = True
    created_at: AwareDatetime
    updated_at: AwareDatetime

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        if kwargs.get("mode") == "json":
            for field in ("created_at", "updated_at"):
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
    earliest_start_at: AwareDatetime | None = None
    start_at_earliest: bool = False
    delay_after_dependencies_business_days: int = Field(default=0, ge=0)
    required_roles: dict[str, float] = Field(default_factory=dict)
    role_requirements: list[RoleRequirementCommand] = Field(min_length=1, max_length=1)
    assumption_note: str | None = None


class BlockerRecord(StrictModel):
    """Persisted blocker fact."""

    blocker_id: str
    project_id: str
    process_id: str = Field(min_length=1)
    description: str
    opened_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    summary: str | None = None
    details: str | None = None
    severity: BlockerSeverity = BlockerSeverity.BLOCKING
    created_at: AwareDatetime | None = None
    resolution: str | None = None
    resolution_owner_resource_id: str | None = None


class SlackProjectConfigRecord(StrictModel):
    """Optional project-owned Slack integration configuration."""

    project_id: str
    enabled: bool = False
    workspace_id: str | None = None
    workspace_name: str | None = None
    bot_token_secret_ref: str | None = None
    signing_secret_ref: str | None = None
    default_channel_id: str | None = None
    continuity_note: str | None = None
    continuity_updated_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None


class SlackResourceMappingRecord(StrictModel):
    """Project resource to Slack user mapping."""

    project_id: str
    resource_id: str
    slack_user_id: str | None = None
    display_name: str | None = None
    active: bool = True
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_active_mapping(self) -> SlackResourceMappingRecord:
        if self.active and self.slack_user_id is None:
            raise ValueError("slack_user_id is required for active mappings.")
        return self


class SlackCollectionCursorRecord(StrictModel):
    """Slack collection checkpoint for one project conversation."""

    project_id: str
    conversation_id: str
    conversation_type: str
    conversation_name: str | None = None
    latest_collected_ts: str | None = None
    last_run_id: str | None = None
    last_run_status: str | None = None
    updated_at: AwareDatetime
    rate_limited_until_at: AwareDatetime | None = None


class SlackEncryptedTokenRecord(StrictModel):
    """Encrypted Slack bot token blob stored for UI-managed Slack projects."""

    project_id: str
    ciphertext: str = Field(min_length=1)
    kdf: str = Field(min_length=1)
    kdf_salt: str = Field(min_length=1)
    kdf_iterations: PositiveInt
    cipher: str = Field(min_length=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class SlackRunRecord(StrictModel):
    """Persisted Slack background run/job state."""

    run_id: str
    project_id: str
    status: SlackRunStatus = SlackRunStatus.RUNNING
    trigger: str = Field(default="ui", min_length=1)
    codex_model: str | None = Field(default=None, min_length=1)
    started_at: AwareDatetime
    updated_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    collected_message_count: int = Field(default=0, ge=0)
    draft_outbox_ids: list[str] = Field(default_factory=list)
    result_json: JsonObject | None = None
    error_text: str | None = None


class SlackOutboxRecord(StrictModel):
    """Persisted Slack message outbox row."""

    outbox_id: str
    project_id: str
    status: SlackOutboxStatus = SlackOutboxStatus.DRAFT
    target_type: Literal["dm", "channel"] = "dm"
    resource_id: str | None = None
    slack_user_id: str | None = None
    body: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    generated_body: str | None = None
    content_hash: str
    run_id: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    edited_at: AwareDatetime | None = None
    sent_at: AwareDatetime | None = None
    failed_at: AwareDatetime | None = None
    skipped_at: AwareDatetime | None = None
    slack_channel_id: str | None = None
    slack_message_ts: str | None = None
    error_text: str | None = None
    skip_reason: str | None = None
    pm_evidence_claims: list[PMEvidenceClaimCommand] = Field(default_factory=list)

    @field_validator("blocks")
    @classmethod
    def _validate_blocks(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _validate_slack_blocks(value)

    @model_validator(mode="before")
    @classmethod
    def _infer_target_type(cls, data: object) -> object:
        if not isinstance(data, dict) or data.get("target_type"):
            return data
        if not data.get("slack_user_id") and data.get("slack_channel_id"):
            return {**data, "target_type": "channel"}
        return data

    @model_validator(mode="after")
    def _validate_target(self) -> SlackOutboxRecord:
        if self.target_type == "dm":
            if not self.slack_user_id:
                raise ValueError("slack_user_id is required for dm outbox rows.")
            if (
                self.slack_channel_id is not None
                and self.status != SlackOutboxStatus.SENT
            ):
                raise ValueError("slack_channel_id is not accepted for dm outbox rows.")
            return self
        if not self.slack_channel_id:
            raise ValueError("slack_channel_id is required for channel outbox rows.")
        if self.slack_user_id is not None:
            raise ValueError("slack_user_id is not accepted for channel outbox rows.")
        if self.resource_id is not None:
            raise ValueError("resource_id is not accepted for channel outbox rows.")
        return self


class PMCommunicationEvidenceRecord(StrictModel):
    """Persisted proof that a PM communication protocol item was sent."""

    evidence_id: str
    project_id: str
    evidence_type: PMCommunicationEvidenceType
    resource_id: str | None = None
    teammate_id: str | None = None
    slack_user_id: str | None = None
    slack_channel_id: str | None = None
    process_id: str | None = None
    process_symbol: str | None = None
    obligation_id: str | None = None
    outbox_id: str
    run_id: str | None = None
    content_hash: str | None = None
    communicated_at: AwareDatetime
    created_at: AwareDatetime
    evidence_note: str | None = None


JsonObject = dict[str, Any]
