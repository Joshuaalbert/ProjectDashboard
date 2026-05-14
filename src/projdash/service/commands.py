"""Pydantic command models for agent and Python callers."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    Field,
    NonNegativeFloat,
    field_validator,
    model_validator,
)

from projdash.service.models import (
    BlockerSeverity,
    CalendarWeeklyWindowCommand,
    DependencyType,
    ProcessIdentityMixin,
    ProcessStatus,
    RoleConflictPolicy,
    RoleRequirementCommand,
    StrictModel,
    UpsertResourcePayload,
    validate_iana_timezone,
    validate_unique_non_empty,
)


def _validate_topology_finished_at(
    process: SubgraphProcessCommand | CollapseNewProcessCommand,
    edit_at,
) -> None:
    if process.status == ProcessStatus.DONE:
        if process.finished_at is None:
            process.finished_at = edit_at
            return
        if process.finished_at > edit_at:
            raise ValueError("finished_at must be no later than edit_at.")
        return
    if process.finished_at is not None:
        raise ValueError("finished_at is only accepted when status is done.")


class CommandModel(StrictModel):
    """Base command model with strict field handling."""


class CreateProject(CommandModel):
    """Create a project."""

    action: Literal["create_project"] = "create_project"
    project_id: str | None = Field(default=None, min_length=1)
    name: str = Field(min_length=1)
    start_at: AwareDatetime
    default_currency: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("default_currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return value.upper()


class SetProjectDefaultCurrency(CommandModel):
    """Set a project's default cost currency."""

    action: Literal["set_project_default_currency"] = "set_project_default_currency"
    project_id: str = Field(min_length=1)
    default_currency: str = Field(min_length=3, max_length=3)

    @field_validator("default_currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return value.upper()


class UpdateProject(CommandModel):
    """Update mutable project metadata."""

    action: Literal["update_project"] = "update_project"
    project_id: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1)
    start_at: AwareDatetime | None = None
    default_currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("default_currency")
    @classmethod
    def _normalize_currency(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @model_validator(mode="after")
    def _validate_update(self) -> UpdateProject:
        if self.name is None and self.start_at is None and self.default_currency is None:
            raise ValueError("At least one project field must be provided.")
        return self


class DeleteProject(CommandModel):
    """Delete a project and all project-owned facts."""

    action: Literal["delete_project"] = "delete_project"
    project_id: str = Field(min_length=1)
    confirm_project_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_confirmation(self) -> DeleteProject:
        if self.confirm_project_id != self.project_id:
            raise ValueError("confirm_project_id must exactly match project_id.")
        return self


class SetProjectDueAt(CommandModel):
    """Set the explicit project due datetime."""

    action: Literal["set_project_due_at"] = "set_project_due_at"
    project_id: str = Field(min_length=1)
    due_at: AwareDatetime
    edit_at: AwareDatetime


class ClearProjectDueAt(CommandModel):
    """Clear the explicit project due datetime."""

    action: Literal["clear_project_due_at"] = "clear_project_due_at"
    project_id: str = Field(min_length=1)
    edit_at: AwareDatetime


class UpsertProcessRevision(CommandModel):
    """Create a process if needed and append a planning revision."""

    action: Literal["upsert_process_revision"] = "upsert_process_revision"
    project_id: str = Field(min_length=1)
    process_id: str | None = Field(default=None, min_length=1)
    process_symbol: str | None = Field(default=None, min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    effective_at: AwareDatetime
    duration_business_days: int = Field(ge=0)
    dependencies: list[str] = Field(default_factory=list)
    due_at: AwareDatetime | None = None
    earliest_start_at: AwareDatetime | None = None
    start_at_earliest: bool = False
    delay_after_dependencies_business_days: int = Field(default=0, ge=0)
    required_roles: dict[str, float] = Field(
        default_factory=dict,
        exclude_if=lambda value: not value,
    )
    role_requirements: list[RoleRequirementCommand] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    assumption_note: str | None = None

    @field_validator("dependencies")
    @classmethod
    def _deduplicate_dependencies(cls, value: list[str]) -> list[str]:
        validate_unique_non_empty(value, "dependencies")
        return list(dict.fromkeys(value))


class SetProcessStatus(CommandModel, ProcessIdentityMixin):
    """Set the project-manager controlled status for a process."""

    action: Literal["set_process_status"] = "set_process_status"
    project_id: str = Field(min_length=1)
    status: ProcessStatus
    edit_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _validate_finished_at_order(self) -> SetProcessStatus:
        if self.started_at is not None and self.started_at > self.edit_at:
            raise ValueError("started_at must be no later than edit_at.")
        if self.finished_at is not None and self.finished_at > self.edit_at:
            raise ValueError("finished_at must be no later than edit_at.")
        return self

    @property
    def changed_at(self):
        """Backward-compatible timestamp name for the prototype service."""
        return self.edit_at


class SetProcessDueAt(CommandModel, ProcessIdentityMixin):
    """Set or clear a process due datetime."""

    action: Literal["set_process_due_at"] = "set_process_due_at"
    project_id: str = Field(min_length=1)
    due_at: AwareDatetime | None = None
    edit_at: AwareDatetime


class AddBlocker(CommandModel, ProcessIdentityMixin):
    """Record an unresolved blocker for a process."""

    action: Literal["add_blocker"] = "add_blocker"
    project_id: str = Field(min_length=1)
    blocker_id: str | None = Field(default=None, min_length=1)
    summary: str = Field(min_length=1)
    details: str | None = None
    severity: BlockerSeverity = BlockerSeverity.BLOCKING
    created_at: AwareDatetime

    @property
    def description(self) -> str:
        """Backward-compatible blocker text name for the prototype service."""
        return self.summary

    @property
    def opened_at(self):
        """Backward-compatible blocker timestamp name for the prototype service."""
        return self.created_at


class CommitProjectState(CommandModel):
    """Persist a committed schedule snapshot for slippage tracking."""

    action: Literal["commit_project_state"] = "commit_project_state"
    project_id: str = Field(min_length=1)
    committed_at: AwareDatetime
    terminal_process_symbols: list[str] = Field(default_factory=list)
    note: str | None = None

    @field_validator("terminal_process_symbols")
    @classmethod
    def _validate_terminal_symbols(cls, value: list[str]) -> list[str]:
        return validate_unique_non_empty(value, "terminal_process_symbols")


class ResolveBlocker(CommandModel):
    """Mark a blocker resolved."""

    action: Literal["resolve_blocker"] = "resolve_blocker"
    project_id: str = Field(min_length=1)
    blocker_id: str = Field(min_length=1)
    resolved_at: AwareDatetime
    resolution: str | None = None


class RenameProcess(CommandModel, ProcessIdentityMixin):
    """Rename a process symbol."""

    action: Literal["rename_process"] = "rename_process"
    project_id: str = Field(min_length=1)
    new_symbol: str = Field(min_length=1)
    edit_at: AwareDatetime
    keep_old_symbol_as_alias: bool = True


class AddProcessAliases(CommandModel, ProcessIdentityMixin):
    """Add process aliases."""

    action: Literal["add_process_aliases"] = "add_process_aliases"
    project_id: str = Field(min_length=1)
    aliases: list[str] = Field(min_length=1)
    edit_at: AwareDatetime

    @field_validator("aliases")
    @classmethod
    def _validate_aliases(cls, value: list[str]) -> list[str]:
        return validate_unique_non_empty(value, "aliases")


class CreateRole(CommandModel):
    """Create a role."""

    action: Literal["create_role"] = "create_role"
    project_id: str = Field(min_length=1)
    role_id: str | None = Field(default=None, min_length=1)
    name: str = Field(min_length=1)


class RenameRole(CommandModel):
    """Rename a role."""

    action: Literal["rename_role"] = "rename_role"
    project_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class DeactivateRole(CommandModel):
    """Deactivate a role."""

    action: Literal["deactivate_role"] = "deactivate_role"
    project_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    force: bool = False


class UpsertResourceCalendar(CommandModel):
    """Create or replace a resource calendar's weekly windows."""

    action: Literal["upsert_resource_calendar"] = "upsert_resource_calendar"
    project_id: str = Field(min_length=1)
    calendar_id: str | None = Field(default=None, min_length=1)
    name: str = Field(min_length=1)
    timezone: str = Field(min_length=1)
    weekly_windows: list[CalendarWeeklyWindowCommand] = Field(min_length=1)
    active: bool = True

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        return validate_iana_timezone(value)


class SetCalendarActive(CommandModel):
    """Activate or deactivate a calendar."""

    action: Literal["set_calendar_active"] = "set_calendar_active"
    project_id: str = Field(min_length=1)
    calendar_id: str = Field(min_length=1)
    active: bool
    force: bool = False


class AddCalendarException(CommandModel):
    """Add a dated calendar capacity exception."""

    action: Literal["add_calendar_exception"] = "add_calendar_exception"
    project_id: str = Field(min_length=1)
    calendar_id: str = Field(min_length=1)
    exception_id: str | None = Field(default=None, min_length=1)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    capacity_hours: NonNegativeFloat
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_interval(self) -> AddCalendarException:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at.")
        return self


class RemoveCalendarException(CommandModel):
    """Remove a calendar exception."""

    action: Literal["remove_calendar_exception"] = "remove_calendar_exception"
    project_id: str = Field(min_length=1)
    calendar_id: str = Field(min_length=1)
    exception_id: str = Field(min_length=1)


class UpsertResource(CommandModel, UpsertResourcePayload):
    """Create or update a resource."""

    action: Literal["upsert_resource"] = "upsert_resource"
    project_id: str = Field(min_length=1)


class SetResourceActive(CommandModel):
    """Activate or deactivate a resource."""

    action: Literal["set_resource_active"] = "set_resource_active"
    project_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    active: bool


class SetResourceRoles(CommandModel):
    """Replace resource role ids."""

    action: Literal["set_resource_roles"] = "set_resource_roles"
    project_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    role_ids: list[str]


class SetResourceCalendar(CommandModel):
    """Replace a resource calendar assignment."""

    action: Literal["set_resource_calendar"] = "set_resource_calendar"
    project_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    calendar_id: str = Field(min_length=1)


class BatchOperationModel(StrictModel):
    """Base batch operation model."""

    operation_id: str | None = Field(default=None, min_length=1)


class AddDependencyOperation(BatchOperationModel):
    """Add a process dependency edge."""

    action: Literal["add_dependency"] = "add_dependency"
    predecessor_process_id: str | None = Field(default=None, min_length=1)
    predecessor_process_symbol: str | None = Field(default=None, min_length=1)
    successor_process_id: str | None = Field(default=None, min_length=1)
    successor_process_symbol: str | None = Field(default=None, min_length=1)
    dependency_type: DependencyType = DependencyType.FINISH_TO_START
    edge_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_identities(self) -> AddDependencyOperation:
        if (self.predecessor_process_id is None) == (
            self.predecessor_process_symbol is None
        ):
            raise ValueError(
                "Exactly one predecessor process identity is required."
            )
        if (self.successor_process_id is None) == (self.successor_process_symbol is None):
            raise ValueError("Exactly one successor process identity is required.")
        return self


class RemoveDependencyOperation(BatchOperationModel):
    """Remove a process dependency edge."""

    action: Literal["remove_dependency"] = "remove_dependency"
    edge_id: str | None = Field(default=None, min_length=1)
    predecessor_process_id: str | None = Field(default=None, min_length=1)
    predecessor_process_symbol: str | None = Field(default=None, min_length=1)
    successor_process_id: str | None = Field(default=None, min_length=1)
    successor_process_symbol: str | None = Field(default=None, min_length=1)
    dependency_type: DependencyType = DependencyType.FINISH_TO_START

    @model_validator(mode="after")
    def _validate_remove_identity(self) -> RemoveDependencyOperation:
        has_edge = self.edge_id is not None
        has_predecessor = (self.predecessor_process_id is None) != (
            self.predecessor_process_symbol is None
        )
        has_successor = (self.successor_process_id is None) != (
            self.successor_process_symbol is None
        )
        if not has_edge and not (has_predecessor and has_successor):
            raise ValueError(
                "Either edge_id or predecessor/successor identities are required."
            )
        return self


class AddRoleRequirementOperation(BatchOperationModel, ProcessIdentityMixin):
    """Add a role requirement to a process candidate revision."""

    action: Literal["add_role_requirement"] = "add_role_requirement"
    requirement: RoleRequirementCommand


class RemoveRoleRequirementOperation(BatchOperationModel, ProcessIdentityMixin):
    """Remove a role requirement from a process candidate revision."""

    action: Literal["remove_role_requirement"] = "remove_role_requirement"
    requirement_id: str = Field(min_length=1)


class BatchUpsertResourcePayload(UpsertResourcePayload):
    """Batch resource payload with project_id inherited from the batch."""


class UpsertResourceOperation(BatchOperationModel):
    """Upsert a resource inside a graph batch."""

    action: Literal["upsert_resource"] = "upsert_resource"
    resource: BatchUpsertResourcePayload


class SetResourceRolesOperation(BatchOperationModel):
    """Set resource roles inside a graph batch."""

    action: Literal["set_resource_roles"] = "set_resource_roles"
    resource_id: str = Field(min_length=1)
    role_ids: list[str]


class SetResourceCalendarOperation(BatchOperationModel):
    """Set a resource calendar inside a graph batch."""

    action: Literal["set_resource_calendar"] = "set_resource_calendar"
    resource_id: str = Field(min_length=1)
    calendar_id: str = Field(min_length=1)


BatchOperation = Annotated[
    AddDependencyOperation
    | RemoveDependencyOperation
    | AddRoleRequirementOperation
    | RemoveRoleRequirementOperation
    | UpsertResourceOperation
    | SetResourceRolesOperation
    | SetResourceCalendarOperation,
    Field(discriminator="action"),
]


class BatchUpdateProcessGraph(CommandModel):
    """Apply an atomic graph/resource operation batch."""

    action: Literal["batch_update_process_graph"] = "batch_update_process_graph"
    project_id: str = Field(min_length=1)
    edit_at: AwareDatetime
    operations: list[BatchOperation] = Field(min_length=1)
    idempotency_key: str | None = Field(default=None, min_length=1)

    @field_validator("operations")
    @classmethod
    def _validate_operation_ids(cls, value: list[BatchOperation]) -> list[BatchOperation]:
        operation_ids = [
            operation.operation_id
            for operation in value
            if operation.operation_id is not None
        ]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("operation_id values must be unique within a batch.")
        return value


class SubgraphProcessCommand(CommandModel):
    """Child process payload for topology rewrites."""

    process_symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    duration_hours: NonNegativeFloat | None = None
    earliest_start_at: AwareDatetime | None = None
    due_at: AwareDatetime | None = None
    status: ProcessStatus = ProcessStatus.PLANNED
    finished_at: AwareDatetime | None = None
    aliases: list[str] = Field(default_factory=list)
    role_requirements: list[RoleRequirementCommand] = Field(default_factory=list)

    @field_validator("aliases")
    @classmethod
    def _validate_aliases(cls, value: list[str]) -> list[str]:
        return validate_unique_non_empty(value, "aliases")

    @model_validator(mode="after")
    def _validate_finished_at_status(self) -> SubgraphProcessCommand:
        if self.duration_hours is None:
            if not self.role_requirements:
                raise ValueError(
                    "duration_hours is required when role_requirements are omitted."
                )
            self.duration_hours = sum(
                float(requirement.effort_hours)
                for requirement in self.role_requirements
            )
        if self.status != ProcessStatus.DONE and self.finished_at is not None:
            raise ValueError("finished_at is only accepted when status is done.")
        return self


class SubgraphDependencyCommand(CommandModel):
    """Internal child dependency payload for topology rewrites."""

    predecessor_symbol: str = Field(min_length=1)
    successor_symbol: str = Field(min_length=1)
    dependency_type: DependencyType = DependencyType.FINISH_TO_START
    edge_id: str | None = Field(default=None, min_length=1)


class ReplaceProcessWithSubgraph(CommandModel, ProcessIdentityMixin):
    """Replace one process with a supplied subgraph."""

    action: Literal["replace_process_with_subgraph"] = "replace_process_with_subgraph"
    project_id: str = Field(min_length=1)
    edit_at: AwareDatetime
    processes: list[SubgraphProcessCommand] = Field(min_length=1)
    dependencies: list[SubgraphDependencyCommand]
    root_symbols: list[str] | None = None
    leaf_symbols: list[str] | None = None
    preserve_parent_symbol_as_alias: bool = True
    parent_alias_target_symbol: str | None = Field(default=None, min_length=1)

    @field_validator("root_symbols", "leaf_symbols")
    @classmethod
    def _validate_symbol_list(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        for symbol in value:
            if not symbol:
                raise ValueError("symbols entries must be non-empty strings.")
        return value

    @model_validator(mode="after")
    def _validate_alias_target(self) -> ReplaceProcessWithSubgraph:
        for process in self.processes:
            _validate_topology_finished_at(process, self.edit_at)
        for field_name in ("root_symbols", "leaf_symbols"):
            if field_name in self.model_fields_set and getattr(self, field_name) == []:
                raise ValueError(
                    f"{field_name} may be omitted for inference, but explicit empty "
                    "lists are not accepted."
                )
        if not self.preserve_parent_symbol_as_alias:
            if self.parent_alias_target_symbol is not None:
                raise ValueError(
                    "parent_alias_target_symbol is forbidden when preservation is false."
                )
            return self
        if len(self.processes) > 1 and self.parent_alias_target_symbol is None:
            raise ValueError(
                "parent_alias_target_symbol is required when preserving an alias "
                "for a multi-child replacement."
            )
        return self


class CollapseNewProcessCommand(CommandModel):
    """Replacement process payload for collapse_subgraph."""

    process_symbol: str | None = Field(default=None, min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    duration_hours: NonNegativeFloat | None = None
    earliest_start_at: AwareDatetime | None = None
    due_at: AwareDatetime | None = None
    status: ProcessStatus = ProcessStatus.PLANNED
    finished_at: AwareDatetime | None = None
    aliases: list[str] = Field(default_factory=list)
    role_requirements: list[RoleRequirementCommand] = Field(default_factory=list)

    @field_validator("aliases")
    @classmethod
    def _validate_aliases(cls, value: list[str]) -> list[str]:
        return validate_unique_non_empty(value, "aliases")

    @model_validator(mode="after")
    def _validate_finished_at_status(self) -> CollapseNewProcessCommand:
        if self.status != ProcessStatus.DONE and self.finished_at is not None:
            raise ValueError("finished_at is only accepted when status is done.")
        return self


class CollapseSubgraph(CommandModel):
    """Collapse a connected process subgraph into one replacement process."""

    action: Literal["collapse_subgraph"] = "collapse_subgraph"
    project_id: str = Field(min_length=1)
    edit_at: AwareDatetime
    process_symbols: list[str] = Field(min_length=1)
    new_process: CollapseNewProcessCommand
    role_conflict_policy: RoleConflictPolicy = RoleConflictPolicy.REJECT

    @field_validator("process_symbols")
    @classmethod
    def _validate_process_symbols(cls, value: list[str]) -> list[str]:
        return validate_unique_non_empty(value, "process_symbols")

    @model_validator(mode="after")
    def _validate_new_process_finished_at(self) -> CollapseSubgraph:
        _validate_topology_finished_at(self.new_process, self.edit_at)
        return self


Command = Annotated[
    CreateProject
    | SetProjectDefaultCurrency
    | UpdateProject
    | DeleteProject
    | SetProjectDueAt
    | ClearProjectDueAt
    | UpsertProcessRevision
    | SetProcessStatus
    | CommitProjectState
    | SetProcessDueAt
    | AddBlocker
    | ResolveBlocker
    | RenameProcess
    | AddProcessAliases
    | BatchUpdateProcessGraph
    | ReplaceProcessWithSubgraph
    | CollapseSubgraph
    | CreateRole
    | RenameRole
    | DeactivateRole
    | UpsertResourceCalendar
    | SetCalendarActive
    | AddCalendarException
    | RemoveCalendarException
    | UpsertResource
    | SetResourceActive
    | SetResourceRoles
    | SetResourceCalendar,
    Field(discriminator="action"),
]


class CommandEnvelope(StrictModel):
    """Envelope shared by Python and JSON command callers."""

    command_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    command: Command


class BatchCommandEnvelope(StrictModel):
    """Transactional group of commands."""

    batch_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    commands: list[CommandEnvelope] = Field(min_length=1)


class RequiredRolesTransitionSettings(StrictModel):
    """Schema fragment for required_roles transition mode configuration."""

    required_roles_transition_mode: Literal[
        "allow_legacy",
        "dual_write_warn",
        "require_role_requirements",
    ] = "allow_legacy"
