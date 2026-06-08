"""Validated service facade for commands and queries."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import inspect
import json
import threading
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

import networkx as nx

from projdash.engine.resource_schedule import compute_resource_schedule
from projdash.engine.schedule import (
    ProjectScheduleInput,
    ScheduleProjection,
    compute_schedule,
)
from projdash.service.commands import (
    AddBlocker,
    AddCalendarException,
    AddDependencyOperation,
    AddProcessAliases,
    AddRoleRequirementOperation,
    BatchCommandEnvelope,
    BatchUpdateProcessGraph,
    ClearSlackBotToken,
    CollapseSubgraph,
    CommandEnvelope,
    CommitProjectState,
    CreateProject,
    CreateRole,
    CreateSlackOutboxMessages,
    DeactivateRole,
    DeleteProcess,
    DeleteProcessRolePin,
    DeleteProject,
    FinishSlackRun,
    MarkSlackOutboxFailed,
    MarkSlackOutboxSent,
    MarkSlackOutboxSkipped,
    RecordPMCommunicationEvidence,
    RecordSlackCollectionCursor,
    RemoveCalendarException,
    RemoveDependencyOperation,
    RemoveRoleRequirementOperation,
    RenameProcess,
    RenameRole,
    ReopenBlocker,
    ReplaceProcessWithSubgraph,
    ResolveBlocker,
    SetBlockerResolutionOwner,
    SetCalendarActive,
    SetMilestoneActive,
    SetProcessStatus,
    SetProjectDefaultCurrency,
    SetResourceActive,
    SetResourceCalendar,
    SetResourceCalendarOperation,
    SetResourceRoles,
    SetResourceRolesOperation,
    SetResourceSlackUser,
    StartSlackRun,
    StoreSlackBotToken,
    UpdateProject,
    UpdateSlackContinuityNote,
    UpdateSlackOutboxBody,
    UpsertMilestone,
    UpsertProcessEvidenceLineItem,
    UpsertProcessRevision,
    UpsertProcessRolePin,
    UpsertResource,
    UpsertResourceCalendar,
    UpsertResourceEvidenceLineItem,
    UpsertResourceOperation,
    UpsertSlackProjectConfig,
)
from projdash.service.errors import Error, ServiceValidationError, ValidationIssue
from projdash.service.identifiers import new_id, symbolify
from projdash.service.models import (
    BlockerSeverity,
    CalendarWeeklyWindowCommand,
    CostUnit,
    MilestoneRecord,
    PMCommunicationEvidenceRecord,
    PMCommunicationEvidenceType,
    ProcessEvidenceLineItemRecord,
    ProcessRolePinRecord,
    ProcessStatus,
    RequiredRolesTransitionMode,
    ResourceCalendarOverrideCommand,
    ResourceEvidenceLineItemRecord,
    ResourceHolidayCommand,
    RoleRequirementCommand,
    ScheduleBasis,
    ScheduleSnapshotRecord,
    ServiceConfig,
    SlackCollectionCursorRecord,
    SlackEncryptedTokenRecord,
    SlackOutboxStatus,
    SlackProjectConfigRecord,
    SlackResourceMappingRecord,
    SlackRunRecord,
    WarningSeverity,
)
from projdash.service.queries import (
    GetProject,
    QueryAgentContext,
    QueryBlockers,
    QueryCosts,
    QueryCriticalPath,
    QueryEnvelope,
    QueryMilestones,
    QueryPendingSlackOutbox,
    QueryPMCommunicationProtocol,
    QueryPMMarkdownContext,
    QueryProcessEvidenceLineItems,
    QueryProcessGraph,
    QueryProcessRolePins,
    QueryProjectCatalog,
    QueryProjects,
    QueryResourceCapacity,
    QueryResourceEvidenceLineItems,
    QueryResourceSchedule,
    QuerySchedule,
    QueryScheduleSnapshots,
    QuerySlackBotToken,
    QuerySlackOutbox,
    QuerySlackProjectConfig,
    QuerySlackRuns,
    QueryUtilization,
)
from projdash.service.repository import (
    LEGACY_PROCESS_EVIDENCE_LINE_ITEMS,
    ProjectRepository,
)
from projdash.service.results import (
    BatchOperationResult,
    CommandErrorResult,
    CommandResult,
    CreatedIds,
    MatchedIds,
    QueryErrorResult,
    QueryResult,
    Warning,
)

DEFAULT_PROCESS_EVIDENCE_LINE_ITEMS = (
    "blockers",
    "done_criteria",
    "plan_data",
    "pin_data",
)
RESOURCE_UNDERSTANDS_PLAN = "understands_plan"
RESOURCE_COMPLETE_PIN_COMMUNICATION = "complete_pin_communication"
RESOURCE_COMPLETE_PLANNING_COMMUNICATION = "complete_planning_communication"
RESOURCE_SLIPPAGE_RISK = "slippage_risk"
DEFAULT_RESOURCE_EVIDENCE_LINE_ITEMS = (
    RESOURCE_UNDERSTANDS_PLAN,
    RESOURCE_COMPLETE_PIN_COMMUNICATION,
    RESOURCE_COMPLETE_PLANNING_COMMUNICATION,
    RESOURCE_SLIPPAGE_RISK,
)


class ProjectService:
    """Application service for agent and UI interactions."""

    def __init__(
        self,
        repository: ProjectRepository,
        *,
        required_roles_transition_mode: RequiredRolesTransitionMode | str = (
            RequiredRolesTransitionMode.ALLOW_LEGACY
        ),
        resource_scheduler=None,
        now_provider: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._config = ServiceConfig(
            required_roles_transition_mode=required_roles_transition_mode,
        )
        self._resource_scheduler = resource_scheduler
        self._now_provider = now_provider or (lambda: dt.datetime.now(dt.UTC))
        self._command_lock = threading.RLock()
        self._command_replay_cache: dict[
            object,
            dict[str, CommandResult | CommandErrorResult],
        ] = self._load_command_replay_cache()
        self._projection_cache_lock = threading.RLock()
        self._schedule_input_cache: dict[tuple[object, ...], ProjectScheduleInput] = {}
        self._schedule_projection_cache: dict[
            tuple[object, ...],
            ScheduleProjection,
        ] = {}
        self._dependency_graph_cache: dict[
            tuple[object, ...],
            dict[str, list[dict[str, object]]],
        ] = {}
        self._resource_schedule_cache: dict[
            tuple[object, ...],
            dict[str, object],
        ] = {}

    def _now(self) -> dt.datetime:
        now = self._now_provider()
        if now.tzinfo is None:
            raise ServiceValidationError(
                code="validation_error",
                message="Service now_provider must return a timezone-aware datetime.",
                field_path="now",
            )
        return now.astimezone(dt.UTC)

    def handle_command(
        self,
        envelope: CommandEnvelope,
    ) -> CommandResult | CommandErrorResult:
        """Apply one validated command.

        Args:
            envelope: Command envelope from Python or JSON callers.

        Returns:
            Structured command result.
        """
        with self._command_lock:
            return self._handle_command_transaction(envelope)

    def _handle_command_transaction(
        self,
        envelope: CommandEnvelope,
    ) -> CommandResult | CommandErrorResult:
        """Apply a mutating command while the service command lock is held."""
        fingerprint = envelope.command.model_dump_json()
        cached = self._command_replay_cache.get(envelope.command_id)
        if cached is not None:
            cached_result = cached.get(fingerprint)
            if cached_result is not None:
                return cached_result
            result = CommandErrorResult(
                command_id=envelope.command_id,
                error=Error(
                    code="idempotency_conflict",
                    message="Command id was replayed with a different payload.",
                    details={},
                ),
            )
            cached[fingerprint] = result
            self._persist_command_replay_cache()
            return result
        clone = getattr(self._repository, "clone", None)
        replace_with = getattr(self._repository, "replace_with", None)
        if not callable(clone) or not callable(replace_with):
            result = CommandErrorResult(
                command_id=envelope.command_id,
                error=Error(
                    code="transaction_required",
                    message=(
                        "Mutating commands require repository transactional staging."
                    ),
                    details={},
                ),
            )
            self._command_replay_cache[envelope.command_id] = {fingerprint: result}
            self._persist_command_replay_cache()
            return result

        staged = clone()
        staged_service = ProjectService(
            staged,
            required_roles_transition_mode=(
                self._config.required_roles_transition_mode
            ),
            resource_scheduler=self._resource_scheduler,
            now_provider=self._now_provider,
        )
        staged_service._command_replay_cache = {
            command_id: dict(records)
            for command_id, records in self._command_replay_cache.items()
        }
        try:
            result = staged_service._handle_command(envelope)
        except ServiceValidationError as exc:
            result = CommandErrorResult(
                command_id=envelope.command_id,
                error=exc.to_error(),
            )
        if result.ok:
            try:
                replace_with(staged)
            except ServiceValidationError as exc:
                result = CommandErrorResult(
                    command_id=envelope.command_id,
                    error=exc.to_error(),
                )
            except Exception as exc:  # pragma: no cover - defensive persistence guard.
                result = CommandErrorResult(
                    command_id=envelope.command_id,
                    error=Error(
                        code="persistence_error",
                        message="Repository failed while committing staged command.",
                        details={"error": str(exc)},
                    ),
                )
            else:
                self._command_replay_cache = staged_service._command_replay_cache
                self._clear_projection_cache()
        self._command_replay_cache[envelope.command_id] = {fingerprint: result}
        self._persist_command_replay_cache()
        return result

    def _handle_command(
        self,
        envelope: CommandEnvelope,
    ) -> CommandResult | CommandErrorResult:
        """Apply one command after outer error wrapping is installed."""
        command = envelope.command
        if isinstance(command, CreateProject):
            project = self._repository.create_project(
                name=command.name,
                start_at=command.start_at,
                default_currency=command.default_currency,
                project_id=command.project_id,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"project_id": project.project_id},
            )
        if isinstance(command, UpdateProject):
            project = self._repository_call(
                "update_project",
                project_id=command.project_id,
                name=command.name,
                start_at=command.start_at,
                default_currency=command.default_currency,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"project_id": project.project_id},
            )
        if isinstance(command, DeleteProject):
            self._repository_call("delete_project", project_id=command.project_id)
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"project_id": command.project_id},
            )
        if isinstance(command, DeleteProcess):
            process_id = self._resolve_process_id(
                project_id=command.project_id,
                process_id=command.process_id,
                process_symbol=command.process_symbol,
            )
            entity_ids = self._repository_call(
                "delete_process",
                project_id=command.project_id,
                process_id=process_id,
                edit_at=command.edit_at,
            )
            return CommandResult(command_id=envelope.command_id, entity_ids=entity_ids)
        if isinstance(command, SetProjectDefaultCurrency):
            self._repository_call(
                "set_project_default_currency",
                project_id=command.project_id,
                default_currency=command.default_currency,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"project_id": command.project_id},
            )
        if isinstance(command, CreateRole):
            role_id = self._repository.create_role(
                project_id=command.project_id,
                name=command.name,
                role_id=command.role_id,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"role_id": role_id},
            )
        if isinstance(command, UpsertProcessRevision):
            role_result = self._validate_required_roles_transition(envelope)
            if isinstance(role_result, CommandErrorResult):
                return role_result
            process_id = command.process_id
            if command.process_symbol is not None:
                try:
                    process_id = self._repository.resolve_process_id(
                        command.project_id,
                        command.process_symbol,
                    )
                except ServiceValidationError as exc:
                    if exc.code != "not_found":
                        raise
                    process_id = command.process_symbol
            process, revision = self._repository.upsert_process_revision(
                project_id=command.project_id,
                process_id=process_id,
                process_type=command.process_type,
                name=command.name,
                description=command.description,
                effective_at=command.effective_at,
                duration_business_days=command.duration_business_days,
                dependencies=command.dependencies,
                earliest_start_at=command.earliest_start_at,
                start_at_earliest=command.start_at_earliest,
                delay_after_dependencies_business_days=(
                    command.delay_after_dependencies_business_days
                ),
                required_roles=command.required_roles,
                role_requirements=command.role_requirements,
                assumption_note=command.assumption_note,
            )
            self._ensure_blocker_dependencies_for_process(
                project_id=command.project_id,
                process_id=process.process_id,
                effective_at=command.effective_at,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={
                    "process_id": process.process_id,
                    "revision_id": revision.revision_id,
                },
                warnings=role_result,
            )
        if isinstance(command, SetProcessStatus):
            process_id = self._resolve_process_id(
                project_id=command.project_id,
                process_id=command.process_id,
                process_symbol=command.process_symbol,
            )
            if command.status == ProcessStatus.DONE:
                self._validate_process_parents_finished_for_done(
                    project_id=command.project_id,
                    process_id=process_id,
                    edit_at=command.edit_at,
                )
                self._validate_process_role_pins_done_for_done(
                    project_id=command.project_id,
                    process_id=process_id,
                    edit_at=command.edit_at,
                )
            elif command.status in {ProcessStatus.IN_PROGRESS, ProcessStatus.PAUSED}:
                self._validate_process_has_started_pin(
                    project_id=command.project_id,
                    process_id=process_id,
                    edit_at=command.edit_at,
                )
            process, lifecycle_event_id = self._repository.set_process_status(
                project_id=command.project_id,
                process_id=process_id,
                status=command.status,
                edit_at=command.edit_at,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={
                    "process_id": process.process_id,
                    "lifecycle_event_id": lifecycle_event_id,
                },
            )
        if isinstance(command, CommitProjectState):
            snapshot = self._commit_project_state(envelope, command)
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={
                    "project_id": command.project_id,
                    "schedule_snapshot_id": snapshot.snapshot_id,
                },
            )
        if isinstance(command, UpsertMilestone):
            milestone = self._upsert_milestone(command)
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={
                    "project_id": command.project_id,
                    "milestone_id": milestone.milestone_id,
                },
            )
        if isinstance(command, SetMilestoneActive):
            milestone = self._repository.set_milestone_active(
                command.project_id,
                command.milestone_id,
                command.active,
                command.edit_at,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={
                    "project_id": command.project_id,
                    "milestone_id": milestone.milestone_id,
                },
            )
        if isinstance(command, UpsertResourceCalendar):
            calendar_id = self._repository.upsert_resource_calendar(
                project_id=command.project_id,
                calendar_id=command.calendar_id,
                name=command.name,
                timezone=command.timezone,
                weekly_windows=command.weekly_windows,
                active=command.active,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"calendar_id": calendar_id},
            )
        if isinstance(command, SetCalendarActive):
            self._repository_call(
                "set_calendar_active",
                project_id=command.project_id,
                calendar_id=command.calendar_id,
                active=command.active,
                force=command.force,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"calendar_id": command.calendar_id},
            )
        if isinstance(command, AddCalendarException):
            exception_id = self._repository.add_calendar_exception(
                project_id=command.project_id,
                calendar_id=command.calendar_id,
                starts_at=command.starts_at,
                ends_at=command.ends_at,
                capacity_hours=command.capacity_hours,
                exception_id=command.exception_id,
                reason=command.reason,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"exception_id": exception_id},
            )
        if isinstance(command, RemoveCalendarException):
            exception_id = self._repository.remove_calendar_exception(
                project_id=command.project_id,
                calendar_id=command.calendar_id,
                exception_id=command.exception_id,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"exception_id": exception_id},
            )
        if isinstance(command, UpsertResource):
            self._validate_resource_role_ids(
                role_ids=command.role_ids,
                active=command.active,
            )
            cost_currency = self._resource_project_currency(
                self._repository,
                command.project_id,
                command.cost_currency,
            )
            calendar_id = self._resource_calendar_for_upsert(
                self._repository,
                command.project_id,
                resource_type=command.resource_type,
                calendar_id=command.calendar_id,
            )
            resource_id = self._repository.upsert_resource(
                project_id=command.project_id,
                resource_id=command.resource_id,
                name=command.name,
                resource_type=command.resource_type,
                role_ids=command.role_ids,
                calendar_id=calendar_id,
                available_from_at=command.available_from_at,
                available_until_at=command.available_until_at,
                cost_rate=command.cost_rate,
                cost_unit=command.cost_unit,
                cost_currency=cost_currency,
                holidays=command.holidays,
                calendar_overrides=command.calendar_overrides,
                active=command.active,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"resource_id": resource_id},
            )
        if isinstance(command, SetResourceActive):
            self._repository.set_resource_active(
                project_id=command.project_id,
                resource_id=command.resource_id,
                active=command.active,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"resource_id": command.resource_id},
            )
        if isinstance(command, SetResourceRoles):
            self._repository.set_resource_roles(
                project_id=command.project_id,
                resource_id=command.resource_id,
                role_ids=command.role_ids,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"resource_id": command.resource_id},
            )
        if isinstance(command, SetResourceCalendar):
            self._repository.set_resource_calendar(
                project_id=command.project_id,
                resource_id=command.resource_id,
                calendar_id=command.calendar_id,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"resource_id": command.resource_id},
            )
        if isinstance(command, UpsertProcessRolePin):
            process_id = self._resolve_process_id(
                project_id=command.project_id,
                process_id=command.process_id,
                process_symbol=command.process_symbol,
            )
            pinned_at = command.pinned_at
            service_now = self._now()
            if pinned_at > service_now:
                raise ServiceValidationError(
                    code="pin_start_in_future",
                    message="A process-role pin cannot start in the future.",
                    field_path="pinned_at",
                    entity_id=process_id,
                    details={"now": service_now.isoformat()},
                )
            requirement_id, role_id = self._process_role_pin_requirement(
                project_id=command.project_id,
                process_id=process_id,
                requirement_id=command.requirement_id,
                role_id=command.role_id,
                as_of=pinned_at,
            )
            if (
                command.status == "pinned_finished"
                and command.verified_done_at is not None
            ):
                self._validate_process_parents_finished_for_pin_finish(
                    project_id=command.project_id,
                    process_id=process_id,
                    verified_done_at=command.verified_done_at,
                )
            pin = self._repository.upsert_process_role_pin(
                ProcessRolePinRecord(
                    pin_id=command.pin_id or new_id(),
                    project_id=command.project_id,
                    process_id=process_id,
                    requirement_id=requirement_id,
                    role_id=role_id,
                    resource_id=command.resource_id,
                    pinned_at=pinned_at,
                    forecast_finish_at=(
                        command.verified_done_at or command.forecast_finish_at
                    ),
                    status=command.status,
                    verified_done_at=command.verified_done_at,
                    created_at=command.updated_at,
                    updated_at=command.updated_at,
                    note=command.note,
                )
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"pin_id": pin.pin_id, "process_id": process_id},
            )
        if isinstance(command, DeleteProcessRolePin):
            self._repository.delete_process_role_pin(
                command.project_id,
                command.pin_id,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"pin_id": command.pin_id},
            )
        if isinstance(command, UpsertSlackProjectConfig):
            existing = self._repository.get_slack_project_config(command.project_id)
            config = self._repository.upsert_slack_project_config(
                SlackProjectConfigRecord(
                    project_id=command.project_id,
                    enabled=command.enabled,
                    workspace_id=command.workspace_id,
                    workspace_name=command.workspace_name,
                    bot_token_secret_ref=command.bot_token_secret_ref,
                    signing_secret_ref=command.signing_secret_ref,
                    default_channel_id=command.default_channel_id,
                    continuity_note=existing.continuity_note,
                    continuity_updated_at=existing.continuity_updated_at,
                    updated_at=command.updated_at,
                )
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"project_id": config.project_id},
            )
        if isinstance(command, UpdateSlackContinuityNote):
            existing = self._repository.get_slack_project_config(command.project_id)
            config = self._repository.upsert_slack_project_config(
                existing.model_copy(
                    update={
                        "continuity_note": command.continuity_note,
                        "continuity_updated_at": command.updated_at,
                        "updated_at": command.updated_at,
                    }
                )
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"project_id": config.project_id},
            )
        if isinstance(command, SetResourceSlackUser):
            mapping = self._repository.set_resource_slack_user(
                SlackResourceMappingRecord(
                    project_id=command.project_id,
                    resource_id=command.resource_id,
                    slack_user_id=command.slack_user_id,
                    display_name=command.display_name,
                    active=command.active,
                    updated_at=command.updated_at,
                )
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"resource_id": mapping.resource_id},
            )
        if isinstance(command, RecordSlackCollectionCursor):
            cursor = self._repository.record_slack_collection_cursor(
                SlackCollectionCursorRecord(
                    project_id=command.project_id,
                    conversation_id=command.conversation_id,
                    conversation_type=command.conversation_type,
                    conversation_name=command.conversation_name,
                    latest_collected_ts=command.latest_collected_ts,
                    last_run_id=command.last_run_id,
                    last_run_status=command.last_run_status,
                    updated_at=command.updated_at,
                    rate_limited_until_at=command.rate_limited_until_at,
                )
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"conversation_id": cursor.conversation_id},
            )
        if isinstance(command, StoreSlackBotToken):
            token = self._repository.store_slack_bot_token(
                SlackEncryptedTokenRecord(
                    project_id=command.project_id,
                    ciphertext=command.ciphertext,
                    kdf=command.kdf,
                    kdf_salt=command.kdf_salt,
                    kdf_iterations=command.kdf_iterations,
                    cipher=command.cipher,
                    created_at=command.updated_at,
                    updated_at=command.updated_at,
                )
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"project_id": token.project_id},
            )
        if isinstance(command, ClearSlackBotToken):
            self._repository.clear_slack_bot_token(command.project_id)
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"project_id": command.project_id},
            )
        if isinstance(command, StartSlackRun):
            run_id = command.run_id or new_id()
            run = self._repository.start_slack_run(
                SlackRunRecord(
                    run_id=run_id,
                    project_id=command.project_id,
                    trigger=command.trigger,
                    codex_model=command.codex_model,
                    started_at=command.started_at,
                    updated_at=command.started_at,
                )
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"run_id": run.run_id},
            )
        if isinstance(command, FinishSlackRun):
            run = self._repository.finish_slack_run(
                project_id=command.project_id,
                run_id=command.run_id,
                status=command.status,
                finished_at=command.finished_at,
                collected_message_count=command.collected_message_count,
                draft_outbox_ids=command.draft_outbox_ids,
                result_json=command.result_json,
                error_text=command.error_text,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"run_id": run.run_id},
            )
        if isinstance(command, CreateSlackOutboxMessages):
            entity_ids = self._repository.create_slack_outbox_messages(
                project_id=command.project_id,
                messages=command.messages,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids=entity_ids,
            )
        if isinstance(command, MarkSlackOutboxSent):
            outbox = self._repository.mark_slack_outbox_sent(
                project_id=command.project_id,
                outbox_id=command.outbox_id,
                sent_at=command.sent_at,
                slack_channel_id=command.slack_channel_id,
                slack_message_ts=command.slack_message_ts,
                run_id=command.run_id,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"outbox_id": outbox.outbox_id},
            )
        if isinstance(command, MarkSlackOutboxFailed):
            outbox = self._repository.mark_slack_outbox_failed(
                project_id=command.project_id,
                outbox_id=command.outbox_id,
                failed_at=command.failed_at,
                error_text=command.error_text,
                run_id=command.run_id,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"outbox_id": outbox.outbox_id},
            )
        if isinstance(command, UpdateSlackOutboxBody):
            outbox = self._repository.update_slack_outbox_body(
                project_id=command.project_id,
                outbox_id=command.outbox_id,
                body=command.body,
                updated_at=command.updated_at,
                run_id=command.run_id,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"outbox_id": outbox.outbox_id},
            )
        if isinstance(command, MarkSlackOutboxSkipped):
            outbox = self._repository.mark_slack_outbox_skipped(
                project_id=command.project_id,
                outbox_id=command.outbox_id,
                skipped_at=command.skipped_at,
                reason=command.reason,
                run_id=command.run_id,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"outbox_id": outbox.outbox_id},
            )
        if isinstance(command, RecordPMCommunicationEvidence):
            outbox = self._repository._get_slack_outbox(  # noqa: SLF001
                command.project_id,
                command.outbox_id,
            )
            if outbox.status != SlackOutboxStatus.SENT:
                raise ServiceValidationError(
                    code="pm_evidence_requires_sent_outbox",
                    message="PM communication evidence requires a sent Slack outbox row.",
                    entity_id=command.outbox_id,
                )
            if outbox.sent_at is None or not outbox.slack_message_ts:
                raise ServiceValidationError(
                    code="pm_evidence_requires_delivered_outbox",
                    message=(
                        "PM communication evidence requires a sent Slack outbox "
                        "row with Slack delivery metadata."
                    ),
                    entity_id=command.outbox_id,
                )
            if command.resource_id is not None:
                self._repository._get_resource(  # noqa: SLF001
                    command.project_id,
                    command.resource_id,
                )
            process_id = command.process_id
            process_symbol = command.process_symbol
            if process_symbol is not None and process_id is None:
                process_id = self._repository.process_id_by_symbol(
                    command.project_id,
                    process_symbol,
                )
            if process_id is not None:
                process = self._repository._get_process(  # noqa: SLF001
                    command.project_id,
                    process_id,
                )
                process_symbol = process_symbol or process.symbol
            self._validate_pm_evidence_against_outbox_claims(
                command,
                outbox,
                process_id=process_id,
                process_symbol=process_symbol,
            )
            self._validate_pm_evidence_against_protocol(
                command,
                process_id=process_id,
                process_symbol=process_symbol,
            )
            evidence = self._repository.record_pm_communication_evidence(
                PMCommunicationEvidenceRecord(
                    evidence_id=command.evidence_id or new_id(),
                    project_id=command.project_id,
                    evidence_type=command.evidence_type,
                    resource_id=command.resource_id,
                    teammate_id=command.teammate_id,
                    slack_user_id=command.slack_user_id or outbox.slack_user_id,
                    slack_channel_id=command.slack_channel_id
                    or outbox.slack_channel_id,
                    process_id=process_id,
                    process_symbol=process_symbol,
                    obligation_id=command.obligation_id,
                    outbox_id=command.outbox_id,
                    run_id=command.run_id or outbox.run_id,
                    content_hash=command.content_hash or outbox.content_hash,
                    communicated_at=command.communicated_at,
                    created_at=command.communicated_at,
                    evidence_note=command.evidence_note,
                )
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"evidence_id": evidence.evidence_id},
            )
        if isinstance(command, UpsertProcessEvidenceLineItem):
            process_id = self._resolve_process_id(
                project_id=command.project_id,
                process_id=command.process_id,
                process_symbol=command.process_symbol,
            )
            process = self._repository._get_process(  # noqa: SLF001
                command.project_id,
                process_id,
            )
            computed_last_modified_at = self._process_line_item_last_modified_at(
                command.project_id,
                process_id,
                command.line_item,
                command.updated_at,
            )
            evidence_line = self._repository.upsert_process_evidence_line_item(
                ProcessEvidenceLineItemRecord(
                    evidence_line_id=self._process_evidence_line_id(
                        command.project_id,
                        process_id,
                        command.line_item,
                    ),
                    project_id=command.project_id,
                    process_id=process_id,
                    process_symbol=process.symbol,
                    line_item=command.line_item,
                    last_modified_at=(
                        command.last_modified_at
                        or computed_last_modified_at
                        or command.updated_at
                    ),
                    last_evidence_at=command.last_evidence_at,
                    evidence_note=command.evidence_note,
                    evidence_source=command.evidence_source,
                    created_at=command.updated_at,
                    updated_at=command.updated_at,
                )
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={
                    "evidence_line_id": evidence_line.evidence_line_id,
                    "process_id": process_id,
                },
            )
        if isinstance(command, UpsertResourceEvidenceLineItem):
            resource = self._repository._get_resource(  # noqa: SLF001
                command.project_id,
                command.resource_id,
            )
            evidence_line = self._repository.upsert_resource_evidence_line_item(
                ResourceEvidenceLineItemRecord(
                    evidence_line_id=self._resource_evidence_line_id(
                        command.project_id,
                        command.resource_id,
                        command.line_item,
                    ),
                    project_id=command.project_id,
                    resource_id=str(resource["resource_id"]),
                    line_item=command.line_item,
                    last_modified_at=command.last_modified_at or command.updated_at,
                    last_evidence_at=command.last_evidence_at,
                    evidence_note=command.evidence_note,
                    evidence_source=command.evidence_source,
                    created_at=command.updated_at,
                    updated_at=command.updated_at,
                )
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={
                    "evidence_line_id": evidence_line.evidence_line_id,
                    "resource_id": command.resource_id,
                },
            )
        if isinstance(command, DeactivateRole):
            self._repository.deactivate_role(
                project_id=command.project_id,
                role_id=command.role_id,
                force=command.force,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"role_id": command.role_id},
            )
        if isinstance(command, BatchUpdateProcessGraph):
            return self._handle_batch_update_process_graph(envelope, command)
        if isinstance(command, AddBlocker):
            process_id = self._resolve_process_id(
                project_id=command.project_id,
                process_id=command.process_id,
                process_symbol=command.process_symbol,
            )
            blocker = self._repository.add_blocker(
                project_id=command.project_id,
                process_id=process_id,
                description=command.description,
                opened_at=command.opened_at,
                blocker_id=command.blocker_id,
                details=command.details,
                severity=BlockerSeverity.BLOCKING,
                resolution_owner_resource_id=command.resolution_owner_resource_id,
            )
            resolver_process_id = self._ensure_blocker_resolver_process(
                blocker=blocker,
                effective_at=command.created_at,
                link_to_blocked_process=True,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={
                    "blocker_id": blocker.blocker_id,
                    "resolver_process_id": resolver_process_id,
                },
            )
        if isinstance(command, ResolveBlocker):
            blocker = self._repository.resolve_blocker(
                project_id=command.project_id,
                blocker_id=command.blocker_id,
                resolved_at=command.resolved_at,
                resolution=command.resolution,
                resolution_owner_resource_id=command.resolution_owner_resource_id,
            )
            resolver_process_id = self._ensure_blocker_resolver_process(
                blocker=blocker,
                effective_at=command.resolved_at,
                link_to_blocked_process=True,
            )
            self._ensure_blocker_resolution_pin(
                blocker=blocker,
                resolver_process_id=resolver_process_id,
                resolved_at=command.resolved_at,
            )
            self._repository.set_process_status(
                project_id=command.project_id,
                process_id=resolver_process_id,
                status=ProcessStatus.DONE,
                edit_at=command.resolved_at,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={
                    "blocker_id": blocker.blocker_id,
                    "resolver_process_id": resolver_process_id,
                },
            )
        if isinstance(command, SetBlockerResolutionOwner):
            blocker = self._repository.set_blocker_resolution_owner(
                project_id=command.project_id,
                blocker_id=command.blocker_id,
                resolution_owner_resource_id=command.resolution_owner_resource_id,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"blocker_id": blocker.blocker_id},
            )
        if isinstance(command, ReopenBlocker):
            blocker = self._repository.reopen_blocker(
                project_id=command.project_id,
                blocker_id=command.blocker_id,
            )
            resolver_process_id = self._ensure_blocker_resolver_process(
                blocker=blocker,
                effective_at=command.edit_at,
                link_to_blocked_process=True,
            )
            self._delete_blocker_resolution_pins(
                project_id=command.project_id,
                resolver_process_id=resolver_process_id,
            )
            self._repository.set_process_status(
                project_id=command.project_id,
                process_id=resolver_process_id,
                status=ProcessStatus.PLANNED,
                edit_at=command.edit_at,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={
                    "blocker_id": blocker.blocker_id,
                    "resolver_process_id": resolver_process_id,
                },
            )
        if isinstance(command, RenameProcess):
            process_id = self._resolve_process_id(
                project_id=command.project_id,
                process_id=command.process_id,
                process_symbol=command.process_symbol,
            )
            self._repository_call(
                "rename_process",
                project_id=command.project_id,
                process_id=process_id,
                new_symbol=command.new_symbol,
                edit_at=command.edit_at,
                keep_old_symbol_as_alias=command.keep_old_symbol_as_alias,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"process_id": process_id},
            )
        if isinstance(command, AddProcessAliases):
            process_id = self._resolve_process_id(
                project_id=command.project_id,
                process_id=command.process_id,
                process_symbol=command.process_symbol,
            )
            self._repository_call(
                "add_process_aliases",
                project_id=command.project_id,
                process_id=process_id,
                aliases=command.aliases,
                edit_at=command.edit_at,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"process_id": process_id},
            )
        if isinstance(command, RenameRole):
            role_id = self._repository_call(
                "rename_role",
                project_id=command.project_id,
                role_id=command.role_id,
                name=command.name,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"role_id": role_id},
            )
        if isinstance(command, ReplaceProcessWithSubgraph):
            if command.process_ids is not None:
                process_ids = list(command.process_ids)
            else:
                process_ids = [
                    self._resolve_process_id(
                        project_id=command.project_id,
                        process_id=None,
                        process_symbol=symbol,
                    )
                    for symbol in (command.process_symbols or [])
                ]
            staged = self._repository.clone()
            ids = self._repository_call_on(
                staged,
                "replace_process_with_subgraph",
                project_id=command.project_id,
                process_ids=process_ids,
                edit_at=command.edit_at,
                processes=command.processes,
                dependencies=command.dependencies,
                root_symbols=command.root_symbols or [],
                leaf_symbols=command.leaf_symbols or [],
                command_id=str(envelope.command_id),
                preserve_parent_symbol_as_alias=(
                    command.preserve_parent_symbol_as_alias
                ),
                parent_alias_target_symbol=command.parent_alias_target_symbol,
            )
            self._repository.replace_with(staged)
            return CommandResult(command_id=envelope.command_id, entity_ids=ids)
        if isinstance(command, CollapseSubgraph):
            process_ids = [
                self._resolve_process_id(
                    project_id=command.project_id,
                    process_id=None,
                    process_symbol=symbol,
                )
                for symbol in command.process_symbols
            ]
            staged = self._repository.clone()
            ids = self._repository_call_on(
                staged,
                "collapse_subgraph",
                project_id=command.project_id,
                process_ids=process_ids,
                edit_at=command.edit_at,
                new_process=command.new_process,
                command_id=str(envelope.command_id),
            )
            self._repository.replace_with(staged)
            return CommandResult(command_id=envelope.command_id, entity_ids=ids)

        raise ServiceValidationError(
            code="unsupported_command",
            message=f"Unsupported command type: {type(command)!r}",
        )

    def handle_batch(
        self,
        envelope: BatchCommandEnvelope,
    ) -> list[CommandResult | CommandErrorResult]:
        """Apply a batch of commands transactionally when the repository supports it."""
        clone = getattr(self._repository, "clone", None)
        replace_with = getattr(self._repository, "replace_with", None)
        if not callable(clone) or not callable(replace_with):
            return [
                CommandErrorResult(
                    command_id=command.command_id,
                    error=Error(
                        code="transaction_required",
                        message=(
                            "Mutating commands require repository transactional "
                            "staging."
                        ),
                        details={},
                    ),
                )
                for command in envelope.commands
            ]

        staged = clone()
        staged_service = ProjectService(
            staged,
            required_roles_transition_mode=(
                self._config.required_roles_transition_mode
            ),
            resource_scheduler=self._resource_scheduler,
        )
        staged_service._command_replay_cache = {
            command_id: dict(records)
            for command_id, records in self._command_replay_cache.items()
        }
        results: list[CommandResult | CommandErrorResult] = []
        for command_index, command in enumerate(envelope.commands):
            result = staged_service.handle_command(command)
            if not result.ok:
                if command_index == 0:
                    self._command_replay_cache[command.command_id] = dict(
                        staged_service._command_replay_cache[command.command_id]
                    )
                rolled_back = [
                    CommandErrorResult(
                        command_id=previous.command_id,
                        error=Error(
                            code="batch_rolled_back",
                            message=(
                                "Command was rolled back because a later "
                                "command in the batch failed."
                            ),
                            details={
                                "command_index": previous_index,
                                "failed_command_id": str(command.command_id),
                                "failed_command_index": command_index,
                            },
                        ),
                    )
                    for previous_index, previous in enumerate(
                        envelope.commands[:command_index]
                    )
                ]
                skipped = [
                    CommandErrorResult(
                        command_id=remaining.command_id,
                        error=Error(
                            code="batch_skipped",
                            message=(
                                "Command was not run because an earlier "
                                "command in the batch failed."
                            ),
                            details={
                                "command_index": remaining_index,
                                "failed_command_id": str(command.command_id),
                                "failed_command_index": command_index,
                            },
                        ),
                    )
                    for remaining_index, remaining in enumerate(
                        envelope.commands[command_index + 1 :],
                        start=command_index + 1,
                    )
                ]
                return [*rolled_back, result, *skipped]
            results.append(result)
        try:
            replace_with(staged)
        except ServiceValidationError as exc:
            return [
                CommandErrorResult(
                    command_id=command.command_id,
                    error=exc.to_error(),
                )
                for command in envelope.commands
            ]
        except Exception as exc:  # pragma: no cover - defensive persistence guard.
            error = Error(
                code="persistence_error",
                message="Repository failed while committing staged batch.",
                details={"error": str(exc)},
            )
            return [
                CommandErrorResult(command_id=command.command_id, error=error)
                for command in envelope.commands
            ]
        self._command_replay_cache = staged_service._command_replay_cache
        self._persist_command_replay_cache()
        self._clear_projection_cache()
        return results

    def handle_query(self, envelope: QueryEnvelope) -> QueryResult | QueryErrorResult:
        """Run one validated query."""
        try:
            return self._handle_query(envelope)
        except ServiceValidationError as exc:
            return QueryErrorResult(query_id=envelope.query_id, error=exc.to_error())

    def _handle_query(self, envelope: QueryEnvelope) -> QueryResult:
        query = envelope.query
        warnings: list[Warning] = []
        if isinstance(query, GetProject):
            project = self._repository.get_project(query.project_id)
            data = {"project": project.model_dump(mode="json")}
        elif isinstance(query, QueryProjects):
            data = {
                "projects": [
                    project.model_dump(mode="json")
                    for project in self._repository.list_projects()
                ]
            }
        elif isinstance(query, QueryProjectCatalog):
            data = self._project_catalog_data(query)
        elif isinstance(query, QueryMilestones):
            data = self._milestone_data(query)
        elif isinstance(query, QuerySlackProjectConfig):
            data = self._slack_project_config_data(query)
        elif isinstance(query, QuerySlackBotToken):
            data = self._slack_bot_token_data(query)
        elif isinstance(query, QuerySlackRuns):
            data = self._slack_runs_data(query)
        elif isinstance(query, QueryPendingSlackOutbox):
            data = self._pending_slack_outbox_data(query)
        elif isinstance(query, QuerySlackOutbox):
            data = self._pending_slack_outbox_data(query)
        elif isinstance(query, QueryPMCommunicationProtocol):
            data = self._pm_communication_protocol_data(query)
        elif isinstance(query, QueryProcessEvidenceLineItems):
            data = self._process_evidence_line_item_data(query)
        elif isinstance(query, QueryResourceEvidenceLineItems):
            data = self._resource_evidence_line_item_data(query)
        elif isinstance(query, QueryProcessRolePins):
            data = self._process_role_pin_data(query)
        elif isinstance(query, QueryPMMarkdownContext):
            data, warnings = self._pm_markdown_context_data(query)
        elif isinstance(query, QuerySchedule):
            data = self._schedule_data(query)
        elif isinstance(query, QueryCriticalPath):
            projection = self._schedule(
                query.project_id,
                query.as_of,
                query.now,
                query.scope,
            )
            path = list(projection.critical_path)
            data = {
                "project_id": query.project_id,
                "as_of": query.as_of.isoformat(),
                "now": query.now.isoformat(),
                "critical_path_process_ids": path,
                "critical_path": path,
            }
        elif isinstance(query, QueryBlockers):
            data = self._blocker_data(query)
        elif isinstance(query, QueryScheduleSnapshots):
            data = self._schedule_snapshot_data(query)
        elif isinstance(query, QueryProcessGraph):
            data = self._process_graph_data(query)
        elif isinstance(query, QueryResourceSchedule):
            data, warnings = self._resource_schedule_data(query)
        elif isinstance(query, QueryAgentContext):
            data, warnings = self._agent_context_data(query)
        elif isinstance(query, QueryResourceCapacity):
            data = self._capacity_data(query)
        elif isinstance(query, QueryUtilization):
            data, warnings = self._utilization_data(query)
        elif isinstance(query, QueryCosts):
            data, warnings = self._cost_data(query)
        else:
            raise TypeError(f"Unsupported query type: {type(query)!r}")
        return QueryResult(query_id=envelope.query_id, data=data, warnings=warnings)

    def query_blockers(
        self,
        project_id: str,
        include_resolved: bool = False,
    ):
        """Return blockers for direct Python callers."""
        return self._repository.list_blockers(project_id, include_resolved)

    def _clear_projection_cache(self) -> None:
        with self._projection_cache_lock:
            self._schedule_input_cache.clear()
            self._schedule_projection_cache.clear()
            self._dependency_graph_cache.clear()
            self._resource_schedule_cache.clear()

    def _cached_value(
        self,
        cache: dict[tuple[object, ...], Any],
        key: tuple[object, ...],
        factory,
        *,
        copy_result: bool = False,
    ):
        with self._projection_cache_lock:
            cached = cache.get(key)
            if cached is not None:
                return copy.deepcopy(cached) if copy_result else cached
            value = factory()
            cache[key] = copy.deepcopy(value) if copy_result else value
            return copy.deepcopy(cache[key]) if copy_result else value

    def _cache_datetime(self, value: dt.datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ServiceValidationError(
                code="naive_datetime",
                message="Projection cache keys require timezone-aware datetimes.",
            )
        return value.astimezone(dt.UTC).isoformat()

    def _cache_value(self, value: Any) -> object:
        if isinstance(value, dt.datetime):
            return self._cache_datetime(value)
        if isinstance(value, Decimal):
            return str(value)
        if hasattr(value, "model_dump"):
            return self._cache_value(value.model_dump(mode="json"))
        if isinstance(value, dict):
            return tuple(
                (str(key), self._cache_value(item))
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            )
        if isinstance(value, list | tuple):
            return tuple(self._cache_value(item) for item in value)
        if isinstance(value, set):
            return tuple(sorted(self._cache_value(item) for item in value))
        return self._enum_value(value)

    def _scope_cache_key(self, scope: Any) -> object:
        return self._cache_value(scope)

    def _repository_cache_version(self, project_id: str) -> object:
        for attr_name in (
            "cache_version",
            "projection_version",
            "project_version",
            "project_generation",
        ):
            provider = getattr(self._repository, attr_name, None)
            if provider is None:
                continue
            if isinstance(provider, dict):
                return self._cache_value(provider.get(project_id))
            if callable(provider):
                return self._cache_value(
                    self._repository_cache_version_from_callable(
                        provider,
                        project_id,
                    )
                )
            return self._cache_value(provider)
        return None

    def _repository_cache_version_from_callable(
        self,
        provider,
        project_id: str,
    ) -> object:
        signature = inspect.signature(provider)
        parameters = list(signature.parameters.values())
        required = [
            parameter
            for parameter in parameters
            if parameter.default is inspect.Parameter.empty
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        ]
        if not required:
            return provider()
        if required[0].kind is inspect.Parameter.KEYWORD_ONLY:
            return provider(project_id=project_id)
        return provider(project_id)

    def _resource_schedule_cache_key(
        self,
        query,
    ) -> tuple[object, ...]:
        scope = None if isinstance(query, QueryCosts) else getattr(query, "scope", None)
        return (
            "resource_schedule",
            query.project_id,
            self._repository_cache_version(query.project_id),
            self._cache_datetime(query.as_of),
            self._cache_datetime(query.now),
            self._scope_cache_key(scope),
            self._enum_value(query.planning_granularity),
            int(query.max_iterations),
            float(query.convergence_tolerance_hours),
            getattr(query, "resource_schedule_backend", "greedy"),
            getattr(query, "resource_schedule_mcts_c_puct", None),
            getattr(query, "resource_schedule_mcts_max_actions", None),
            getattr(query, "include_resource_sensitivity", False),
            getattr(query, "resource_schedule_sensitivity_backend", None),
            getattr(query, "resource_schedule_sensitivity_workers", None),
            getattr(query, "resource_schedule_sensitivity_process_pool", True),
        )

    def _schedule_input_for_scope(
        self,
        project_id: str,
        as_of: dt.datetime,
        scope: Any,
    ) -> ProjectScheduleInput:
        key = (
            "schedule_input",
            project_id,
            self._repository_cache_version(project_id),
            self._cache_datetime(as_of),
            self._scope_cache_key(scope),
        )

        def factory() -> ProjectScheduleInput:
            return self._build_schedule_input_for_scope(project_id, as_of, scope)

        return self._cached_value(self._schedule_input_cache, key, factory)

    def _build_schedule_input_for_scope(
        self,
        project_id: str,
        as_of: dt.datetime,
        scope: Any,
    ) -> ProjectScheduleInput:
        schedule_input = self._pin_collapsed_schedule_input(
            project_id,
            self._repository.get_project_schedule_input(project_id, as_of),
            as_of,
        )
        if scope is None:
            return schedule_input
        selected_ids, _scope_data, _target_process_id = (
            self._repository.process_ids_for_scope(project_id, as_of, scope)
        )
        processes = []
        for process in schedule_input.processes:
            if process.process_id not in selected_ids:
                continue
            processes.append(
                replace(
                    process,
                    dependencies=tuple(
                        dependency
                        for dependency in process.dependencies
                        if dependency in selected_ids
                    ),
                )
            )
        return ProjectScheduleInput(
            project_id=schedule_input.project_id,
            name=schedule_input.name,
            start_at=schedule_input.start_at,
            processes=tuple(processes),
        )

    def _pin_collapsed_schedule_input(
        self,
        project_id: str,
        schedule_input: ProjectScheduleInput,
        as_of: dt.datetime,
    ) -> ProjectScheduleInput:
        processes = []
        for process in schedule_input.processes:
            facts = self._process_completedness_facts(
                project_id,
                process.process_id,
                as_of,
            )
            processes.append(
                replace(
                    process,
                    pin_started_at=facts["started_at"],
                    pin_finished_at=facts["finished_at"],
                    derived_status=str(facts["status"]),
                )
            )
        return ProjectScheduleInput(
            project_id=schedule_input.project_id,
            name=schedule_input.name,
            start_at=schedule_input.start_at,
            processes=tuple(processes),
        )

    def _schedule(self, project_id, as_of, now, scope=None):
        key = (
            "schedule_projection",
            project_id,
            self._repository_cache_version(project_id),
            self._cache_datetime(as_of),
            self._cache_datetime(now),
            self._scope_cache_key(scope),
        )

        def factory() -> ScheduleProjection:
            schedule_input = self._schedule_input_for_scope(project_id, as_of, scope)
            return compute_schedule(schedule_input, now)

        return self._cached_value(self._schedule_projection_cache, key, factory)

    def _commit_project_state(
        self,
        envelope: CommandEnvelope,
        command: CommitProjectState,
    ) -> ScheduleSnapshotRecord:
        terminal_symbols = sorted(self._commit_terminal_symbols(command))
        scope = self._terminal_scope_data(terminal_symbols)
        schedule_query = QueryResourceSchedule(
            project_id=command.project_id,
            as_of=command.committed_at,
            now=command.committed_at,
            scope=scope,
            include_allocation_slices=False,
            resource_schedule_backend=command.resource_schedule_backend,
            include_resource_sensitivity=command.include_resource_sensitivity,
            resource_schedule_sensitivity_backend=(
                command.resource_schedule_sensitivity_backend
            ),
            resource_schedule_sensitivity_workers=(
                command.resource_schedule_sensitivity_workers
            ),
            resource_schedule_sensitivity_process_pool=(
                command.resource_schedule_sensitivity_process_pool
            ),
        )
        schedule = self._compute_resource_schedule(
            schedule_query,
            include_allocation_slices=False,
        )
        process_rows = list(schedule["processes"])
        terminal_process_ids = self._terminal_process_ids(
            command.project_id,
            command.committed_at,
            terminal_symbols,
            process_rows,
        )
        terminal_rows = [
            row for row in process_rows if row.get("process_id") in terminal_process_ids
        ]
        ends_at_values = []
        for row in terminal_rows:
            end_value = row.get("finished_at") or row.get("ends_at")
            if end_value is not None:
                ends_at_values.append(self._parse_datetime(end_value))
        completion_at = None
        if terminal_rows and len(ends_at_values) == len(terminal_rows):
            completion_at = max(ends_at_values)
        snapshot = ScheduleSnapshotRecord(
            snapshot_id=self._schedule_snapshot_id(
                command.project_id,
                command.committed_at,
                terminal_symbols,
            ),
            project_id=command.project_id,
            committed_at=command.committed_at,
            terminal_process_symbols=terminal_symbols,
            schedule_basis=ScheduleBasis.RESOURCE_AWARE,
            completion_at=completion_at,
            converged=schedule.get("converged"),
            role_sensitivity=list(schedule.get("resource_sensitivity") or []),
            note=command.note,
        )
        recorder = getattr(self._repository, "record_schedule_snapshot", None)
        if recorder is None:
            raise ServiceValidationError(
                code="unsupported_repository",
                message="Repository does not support schedule snapshots.",
        )
        return recorder(snapshot)

    def _upsert_milestone(self, command: UpsertMilestone) -> MilestoneRecord:
        self._repository.get_project(command.project_id)
        process_symbols = list(
            dict.fromkeys(
                self._canonical_process_symbols(
                    command.project_id,
                    command.process_symbols,
                )
            )
        )
        milestone_id = command.milestone_id or f"milestone-{symbolify(command.name)}"
        existing = None
        for milestone in self._repository.list_milestones(
            command.project_id,
            include_inactive=True,
        ):
            if milestone.milestone_id == milestone_id:
                existing = milestone
                break
        milestone = MilestoneRecord(
            milestone_id=milestone_id,
            project_id=command.project_id,
            name=command.name,
            description=command.description,
            process_symbols=process_symbols,
            active=command.active,
            created_at=existing.created_at if existing is not None else command.edit_at,
            updated_at=command.edit_at,
        )
        return self._repository.upsert_milestone(milestone)

    def _validate_pm_evidence_against_outbox_claims(
        self,
        command: RecordPMCommunicationEvidence,
        outbox,
        *,
        process_id: str | None,
        process_symbol: str | None,
    ) -> None:
        # Evidence is only proof if it was declared on the reviewed outbox row
        # before send. This prevents later service calls from minting unrelated
        # evidence against a sent Slack message.
        if outbox.target_type == "dm":
            if command.slack_user_id and command.slack_user_id != outbox.slack_user_id:
                raise ServiceValidationError(
                    code="pm_evidence_target_mismatch",
                    message="PM evidence Slack user does not match the sent outbox row.",
                    entity_id=command.outbox_id,
                )
            if (
                command.resource_id
                and outbox.resource_id
                and command.resource_id != outbox.resource_id
            ):
                raise ServiceValidationError(
                    code="pm_evidence_target_mismatch",
                    message="PM evidence resource does not match the sent outbox row.",
                    entity_id=command.outbox_id,
                )
        elif (
            outbox.target_type == "channel"
            and command.slack_channel_id
            and outbox.slack_channel_id
            and command.slack_channel_id != outbox.slack_channel_id
        ):
            raise ServiceValidationError(
                code="pm_evidence_target_mismatch",
                message="PM evidence channel does not match the sent outbox row.",
                entity_id=command.outbox_id,
            )
        for claim in outbox.pm_evidence_claims:
            if claim.evidence_type != command.evidence_type:
                continue
            if claim.resource_id != command.resource_id:
                continue
            if claim.process_id != process_id:
                continue
            if claim.process_symbol != process_symbol:
                continue
            if claim.obligation_id != command.obligation_id:
                continue
            if claim.content_hash is not None:
                actual_hash = command.content_hash or outbox.content_hash
                if claim.content_hash != actual_hash:
                    continue
            return
        raise ServiceValidationError(
            code="pm_evidence_claim_not_on_outbox",
            message=(
                "PM communication evidence must match one of the sent outbox "
                "row's PM evidence claims."
            ),
            entity_id=command.outbox_id,
        )

    def _validate_pm_evidence_against_protocol(
        self,
        command: RecordPMCommunicationEvidence,
        *,
        process_id: str | None,
        process_symbol: str | None,
    ) -> None:
        if command.evidence_type in {
            PMCommunicationEvidenceType.MESSAGE_RECEIPT_ACK,
            PMCommunicationEvidenceType.PROJECT_UPDATE_NOTICE,
        }:
            return
        if command.obligation_id is None:
            raise ServiceValidationError(
                code="pm_evidence_obligation_required",
                message="PM protocol evidence requires an obligation_id.",
                entity_id=command.outbox_id,
            )
        protocol = self._pm_communication_protocol_data(
            QueryPMCommunicationProtocol(
                project_id=command.project_id,
                as_of=command.communicated_at,
                now=command.communicated_at,
                include_satisfied=True,
                resource_schedule_backend="mcts",
            )
        )
        obligation = next(
            (
                item
                for item in protocol.get("obligations", [])
                if item.get("obligation_id") == command.obligation_id
            ),
            None,
        )
        if obligation is None:
            raise ServiceValidationError(
                code="pm_evidence_obligation_not_current",
                message="PM evidence obligation is not part of the current protocol.",
                entity_id=command.outbox_id,
            )
        required_types = set(obligation.get("required_evidence_types") or [])
        if command.evidence_type.value not in required_types:
            raise ServiceValidationError(
                code="pm_evidence_type_not_required",
                message="PM evidence type is not required by the obligation.",
                entity_id=command.outbox_id,
            )
        if obligation.get("resource_id") != command.resource_id:
            raise ServiceValidationError(
                code="pm_evidence_obligation_mismatch",
                message="PM evidence resource does not match the protocol obligation.",
                entity_id=command.outbox_id,
            )
        if obligation.get("process_id") != process_id:
            raise ServiceValidationError(
                code="pm_evidence_obligation_mismatch",
                message="PM evidence process id does not match the protocol obligation.",
                entity_id=command.outbox_id,
            )
        if obligation.get("process_symbol") != process_symbol:
            raise ServiceValidationError(
                code="pm_evidence_obligation_mismatch",
                message=(
                    "PM evidence process symbol does not match the protocol "
                    "obligation."
                ),
                entity_id=command.outbox_id,
            )
        expected_hash = obligation.get("content_hash")
        if expected_hash and command.content_hash != expected_hash:
            raise ServiceValidationError(
                code="pm_evidence_obligation_mismatch",
                message="PM evidence content hash does not match the protocol obligation.",
                entity_id=command.outbox_id,
            )

    def _ensure_blocker_resolver_process(
        self,
        *,
        blocker,
        effective_at: dt.datetime,
        link_to_blocked_process: bool,
    ) -> str:
        resolver_symbol = self._blocker_resolver_symbol(blocker.blocker_id)
        try:
            resolver_process_id = self._repository.resolve_process_id(
                blocker.project_id,
                resolver_symbol,
            )
        except ServiceValidationError as exc:
            if exc.code != "not_found":
                raise
            resolver_process_id = resolver_symbol
        try:
            existing_revision = self._repository.selected_revision_as_of(
                blocker.project_id,
                resolver_process_id,
                effective_at,
            )
        except ServiceValidationError as exc:
            if exc.code not in {"process_not_found", "not_found"}:
                raise
            existing_revision = None
        existing_dependencies = (
            list(existing_revision.dependencies) if existing_revision is not None else []
        )
        process, _revision = self._repository.upsert_process_revision(
            project_id=blocker.project_id,
            process_id=resolver_process_id,
            process_type="blocker",
            name=f"Resolve: {blocker.summary or blocker.description}",
            description=f"Resolve blocker: {blocker.summary or blocker.description}",
            effective_at=effective_at,
            duration_business_days=0,
            dependencies=existing_dependencies,
            earliest_start_at=None,
            start_at_earliest=False,
            delay_after_dependencies_business_days=0,
            required_roles={},
            role_requirements=[],
            assumption_note=None,
        )
        if link_to_blocked_process:
            self._ensure_process_dependency(
                project_id=blocker.project_id,
                process_id=blocker.process_id,
                dependency_id=process.process_id,
                effective_at=effective_at,
            )
        return process.process_id

    def _ensure_blocker_resolution_pin(
        self,
        *,
        blocker,
        resolver_process_id: str,
        resolved_at: dt.datetime,
    ) -> None:
        resource_id = (
            blocker.resolution_owner_resource_id
            or self._ensure_blocker_resolution_resource(
                blocker.project_id,
                resolved_at,
            )
        )
        role_id = self._ensure_resource_exact_role(blocker.project_id, resource_id)
        self._ensure_resource_has_role(blocker.project_id, resource_id, role_id)
        requirement_id = f"{resolver_process_id}-resolution"
        revision = self._repository.selected_revision_as_of(
            blocker.project_id,
            resolver_process_id,
            resolved_at,
        )
        if revision is None:
            raise ServiceValidationError(
                code="process_revision_not_found",
                message="Blocker resolver process requires a revision.",
                entity_id=resolver_process_id,
            )
        requirements = [
            RoleRequirementCommand(
                requirement_id=requirement_id,
                role_id=role_id,
                effort_hours=1,
            )
        ]
        self._repository.upsert_process_revision(
            project_id=blocker.project_id,
            process_id=resolver_process_id,
            process_type="blocker",
            name=revision.name,
            description=revision.description,
            effective_at=resolved_at,
            duration_business_days=revision.duration_business_days,
            dependencies=list(revision.dependencies),
            earliest_start_at=revision.earliest_start_at,
            start_at_earliest=revision.start_at_earliest,
            delay_after_dependencies_business_days=(
                revision.delay_after_dependencies_business_days
            ),
            required_roles=dict(revision.required_roles),
            role_requirements=requirements,
            assumption_note=revision.assumption_note,
        )
        self._repository.upsert_process_role_pin(
            ProcessRolePinRecord(
                pin_id=f"{resolver_process_id}-resolution-pin",
                project_id=blocker.project_id,
                process_id=resolver_process_id,
                requirement_id=requirement_id,
                role_id=role_id,
                resource_id=resource_id,
                pinned_at=resolved_at,
                forecast_finish_at=resolved_at,
                status="pinned_finished",
                verified_done_at=resolved_at,
                created_at=resolved_at,
                updated_at=resolved_at,
                note="Verified blocker resolution.",
            )
        )

    def _delete_blocker_resolution_pins(
        self,
        *,
        project_id: str,
        resolver_process_id: str,
    ) -> None:
        for pin in list(
            self._repository.list_process_role_pins(
                project_id,
                process_id=resolver_process_id,
                include_done=True,
            )
        ):
            if pin.pin_id == f"{resolver_process_id}-resolution-pin":
                self._repository.delete_process_role_pin(project_id, pin.pin_id)

    def _ensure_blocker_resolution_resource(
        self,
        project_id: str,
        available_from_at: dt.datetime,
    ) -> str:
        role_id = self._blocker_resolution_system_role_id(project_id)
        self._ensure_role(
            project_id,
            role_id,
            f"Blocker Resolution ({project_id})",
        )
        resource_id = self._blocker_resolution_system_resource_id(project_id)
        resources = getattr(self._repository, "resources", {})
        if resource_id in resources:
            self._ensure_resource_has_role(project_id, resource_id, role_id)
            return resource_id
        calendar_id = self._ensure_external_default_calendar(self._repository, project_id)
        return self._repository.upsert_resource(
            project_id=project_id,
            resource_id=resource_id,
            name=f"Blocker Resolution ({project_id})",
            resource_type="external",
            role_ids=[role_id],
            calendar_id=calendar_id,
            available_from_at=available_from_at,
            available_until_at=None,
            cost_rate=0,
            cost_unit=CostUnit.FIXED,
            active=True,
        )

    def _ensure_resource_exact_role(self, project_id: str, resource_id: str) -> str:
        resource = self._repository._get_resource(project_id, resource_id)  # noqa: SLF001
        role_id = self._project_scoped_role_id(project_id, f"role_{resource_id}")
        self._ensure_role(
            project_id,
            role_id,
            f"Exact assignment: {resource.get('name') or resource_id}",
        )
        return role_id

    def _project_scoped_role_id(self, project_id: str, base_role_id: str) -> str:
        roles = getattr(self._repository, "roles", {})
        existing = roles.get(base_role_id)
        if existing is None or existing.get("project_id") == project_id:
            return base_role_id
        stem = f"role_{symbolify(project_id)}_{base_role_id.removeprefix('role_')}"
        candidate = stem
        suffix = 2
        while True:
            existing = roles.get(candidate)
            if existing is None or existing.get("project_id") == project_id:
                return candidate
            candidate = f"{stem}_{suffix}"
            suffix += 1

    def _ensure_resource_has_role(
        self,
        project_id: str,
        resource_id: str,
        role_id: str,
    ) -> None:
        resource = self._repository._get_resource(project_id, resource_id)  # noqa: SLF001
        role_ids = list(resource.get("role_ids", []) or [])
        if role_id in role_ids:
            return
        holidays = [
            ResourceHolidayCommand.model_validate(holiday)
            for holiday in resource.get("holidays", [])
        ]
        calendar_overrides = [
            ResourceCalendarOverrideCommand.model_validate(override)
            for override in resource.get("calendar_overrides", [])
        ]
        self._repository.upsert_resource(
            project_id=project_id,
            resource_id=resource_id,
            name=str(resource["name"]),
            resource_type=str(resource.get("resource_type", "internal")),
            role_ids=[*role_ids, role_id],
            calendar_id=str(resource["calendar_id"]),
            available_from_at=resource["available_from_at"],
            available_until_at=resource.get("available_until_at"),
            cost_rate=resource["cost_rate"],
            cost_unit=resource["cost_unit"],
            cost_currency=resource.get("cost_currency"),
            holidays=holidays,
            calendar_overrides=calendar_overrides,
            active=bool(resource.get("active", True)),
        )

    def _ensure_role(self, project_id: str, role_id: str, name: str) -> str:
        roles = getattr(self._repository, "roles", {})
        existing = roles.get(role_id)
        if existing is not None:
            if existing.get("project_id") != project_id:
                raise ServiceValidationError(
                    code="cross_project_role",
                    message="Role does not belong to the requested project.",
                    entity_id=role_id,
                )
            return role_id
        return self._repository.create_role(project_id, name=name, role_id=role_id)

    @staticmethod
    def _blocker_resolution_system_role_id(project_id: str) -> str:
        return f"role_{project_id}_blocker_resolution"

    @staticmethod
    def _blocker_resolution_system_resource_id(project_id: str) -> str:
        return f"res_{project_id}_blocker_resolution"

    def _ensure_blocker_dependencies_for_process(
        self,
        *,
        project_id: str,
        process_id: str,
        effective_at: dt.datetime,
    ) -> None:
        list_as_of = getattr(self._repository, "list_blockers_as_of", None)
        blockers = (
            list_as_of(project_id, effective_at, True)
            if list_as_of is not None
            else self._repository.list_blockers(project_id, True)
        )
        for blocker in blockers:
            if blocker.process_id != process_id:
                continue
            self._ensure_blocker_resolver_process(
                blocker=blocker,
                effective_at=effective_at,
                link_to_blocked_process=True,
            )

    def _validate_process_parents_finished_for_done(
        self,
        *,
        project_id: str,
        process_id: str,
        edit_at: dt.datetime,
    ) -> dt.datetime | None:
        unfinished, finish_times = self._process_unfinished_parent_facts(
            project_id,
            process_id,
            edit_at,
        )
        if unfinished:
            raise ServiceValidationError(
                code="unfinished_parent_processes",
                message=(
                    "A process cannot be marked done until all parent "
                    "processes are done or canceled."
                ),
                field_path="status",
                entity_id=process_id,
                details={"unfinished_parent_processes": sorted(unfinished)},
            )
        return max(finish_times, default=None)

    def _validate_process_parents_finished_for_pin_finish(
        self,
        *,
        project_id: str,
        process_id: str,
        verified_done_at: dt.datetime,
    ) -> None:
        unfinished, _finish_times = self._process_unfinished_parent_facts(
            project_id,
            process_id,
            verified_done_at,
        )
        if not unfinished:
            return
        raise ServiceValidationError(
            code="pin_finish_requires_finished_parent_processes",
            message=(
                "A process-role cannot be verified done until every parent "
                "process is finished."
            ),
            field_path="verified_done_at",
            entity_id=process_id,
            details={"unfinished_parent_processes": sorted(unfinished)},
        )

    def _process_unfinished_parent_facts(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime,
    ) -> tuple[list[str], list[dt.datetime]]:
        revision = self._repository.selected_revision_as_of(
            project_id,
            process_id,
            as_of,
        )
        if revision is None:
            return [], []
        processes = getattr(self._repository, "processes", {})
        unfinished = []
        finish_times = []
        for dependency_id in revision.dependencies:
            dependency = processes.get(dependency_id)
            if dependency is None:
                unfinished.append(dependency_id)
                continue
            facts = self._process_completedness_facts(
                project_id,
                dependency_id,
                as_of,
            )
            if not facts["is_finished"]:
                unfinished.append(getattr(dependency, "symbol", dependency_id))
                continue
            finished_at = facts["finished_at"]
            if finished_at is not None:
                if finished_at > as_of:
                    unfinished.append(getattr(dependency, "symbol", dependency_id))
                    continue
                finish_times.append(finished_at)
        return unfinished, finish_times

    def _validate_process_role_pins_done_for_done(
        self,
        *,
        project_id: str,
        process_id: str,
        edit_at: dt.datetime,
    ) -> None:
        revision = self._repository.selected_revision_as_of(
            project_id,
            process_id,
            edit_at,
        )
        if revision is None:
            return
        if not revision.role_requirements:
            raise ServiceValidationError(
                code="done_requires_verified_process_role_pins",
                message=(
                    "A process cannot be marked done until it has process-role "
                    "requirements and every process-role has a verified done pin."
                ),
                field_path="status",
                entity_id=process_id,
                details={"missing_role_requirements": True},
            )
        verified_requirement_ids = {
            self._pin_requirement_id(pin)
            for pin in self._repository.list_process_role_pins(
                project_id,
                as_of=edit_at,
                process_id=process_id,
                include_done=True,
            )
            if pin.status == "pinned_finished"
            and pin.verified_done_at is not None
            and pin.verified_done_at <= edit_at
        }
        missing = []
        for index, requirement in enumerate(revision.role_requirements):
            requirement_id = (
                requirement.requirement_id
                or f"{process_id}-requirement-{index + 1}"
            )
            if requirement_id in verified_requirement_ids:
                continue
            missing.append(
                {
                    "requirement_id": requirement_id,
                    "role_id": requirement.role_id,
                }
            )
        if missing:
            raise ServiceValidationError(
                code="done_requires_verified_process_role_pins",
                message=(
                    "A role-backed process cannot be marked done until every "
                    "process-role has a verified done pin."
                ),
                field_path="status",
                entity_id=process_id,
                details={"missing_verified_pins": missing},
            )

    def _validate_process_has_started_pin(
        self,
        *,
        project_id: str,
        process_id: str,
        edit_at: dt.datetime,
    ) -> None:
        has_started_pin = any(
            pin.pinned_at <= edit_at
            for pin in self._repository.list_process_role_pins(
                project_id,
                as_of=edit_at,
                process_id=process_id,
                include_done=True,
            )
        )
        if has_started_pin:
            return
        raise ServiceValidationError(
            code="started_requires_process_role_pin",
            message=(
                "A process cannot be marked started until at least one "
                "process-role is pinned started."
            ),
            field_path="status",
            entity_id=process_id,
        )

    def _process_pin_finished_at(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime,
    ) -> dt.datetime | None:
        verified = [
            pin.verified_done_at
            for pin in self._repository.list_process_role_pins(
                project_id,
                as_of=as_of,
                process_id=process_id,
                include_done=True,
            )
            if pin.status == "pinned_finished"
            and pin.verified_done_at is not None
            and pin.verified_done_at <= as_of
        ]
        return max(verified, default=None)

    def _is_blocker_reference_dependency(
        self,
        *,
        project_id: str,
        successor_id: str,
        predecessor_id: str,
        as_of: dt.datetime,
    ) -> bool:
        list_as_of = getattr(self._repository, "list_blockers_as_of", None)
        blockers = (
            list_as_of(project_id, as_of, True)
            if list_as_of is not None
            else self._repository.list_blockers(project_id, True)
        )
        for blocker in blockers:
            if blocker.process_id != successor_id:
                continue
            resolver_symbol = self._blocker_resolver_symbol(blocker.blocker_id)
            try:
                resolver_id = self._repository.resolve_process_id(
                    project_id,
                    resolver_symbol,
                )
            except ServiceValidationError:
                resolver_id = resolver_symbol
            if predecessor_id == resolver_id:
                return True
        return False

    def _blocker_for_resolver_dependency(
        self,
        *,
        project_id: str,
        predecessor_id: str,
        as_of: dt.datetime,
    ):
        list_as_of = getattr(self._repository, "list_blockers_as_of", None)
        blockers = (
            list_as_of(project_id, as_of, True)
            if list_as_of is not None
            else self._repository.list_blockers(project_id, True)
        )
        for blocker in blockers:
            resolver_symbol = self._blocker_resolver_symbol(blocker.blocker_id)
            try:
                resolver_id = self._repository.resolve_process_id(
                    project_id,
                    resolver_symbol,
                )
            except ServiceValidationError:
                resolver_id = resolver_symbol
            if predecessor_id == resolver_id:
                return blocker
        return None

    def _ensure_process_dependency(
        self,
        *,
        project_id: str,
        process_id: str,
        dependency_id: str,
        effective_at: dt.datetime,
    ) -> None:
        revision = self._repository.selected_revision_as_of(
            project_id,
            process_id,
            effective_at,
        )
        if revision is None:
            raise ServiceValidationError(
                code="process_not_found",
                message=f"Process {process_id!r} does not have an active revision.",
                entity_id=process_id,
            )
        if dependency_id in revision.dependencies:
            return
        updated = revision.model_copy(
            update={
                "revision_id": new_id(),
                "effective_at": effective_at,
                "dependencies": list(dict.fromkeys([*revision.dependencies, dependency_id])),
            }
        )
        validate = getattr(self._repository, "_validate_acyclic_after_revision", None)
        if callable(validate):
            validate(project_id, updated)
        self._repository.revisions_by_process[process_id].append(updated)
        edge_id = getattr(self._repository, "_dependency_edge_id", None)
        if callable(edge_id):
            edge_id(project_id, dependency_id, process_id)

    def _blocker_resolver_symbol(self, blocker_id: str) -> str:
        stem = blocker_id.removeprefix("blocker-")
        stem = self._slugify_identifier_stem(stem) or self._slugify_identifier_stem(
            blocker_id,
        )
        return f"resolve-{stem}"

    def _is_blocking_blocker(self, blocker) -> bool:
        return self._enum_value(getattr(blocker, "severity", "blocking")) == "blocking"

    def _slugify_identifier_stem(self, value: str) -> str:
        slug = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
        while "--" in slug:
            slug = slug.replace("--", "-")
        return slug.strip("-") or "blocker"

    def _commit_terminal_symbols(self, command: CommitProjectState) -> list[str]:
        if command.milestone_id is None:
            return list(command.terminal_process_symbols)
        milestone = self._milestone_by_id(command.project_id, command.milestone_id)
        return list(milestone.process_symbols)

    def _milestone_by_id(
        self,
        project_id: str,
        milestone_id: str,
        *,
        include_inactive: bool = True,
    ) -> MilestoneRecord:
        for milestone in self._repository.list_milestones(
            project_id,
            include_inactive=include_inactive,
        ):
            if milestone.milestone_id == milestone_id:
                return milestone
        raise ServiceValidationError(
            code="milestone_not_found",
            message=f"Milestone {milestone_id!r} does not exist.",
            entity_id=milestone_id,
        )

    def _terminal_process_ids(
        self,
        project_id: str,
        as_of: dt.datetime,
        terminal_symbols: list[str],
        process_rows: list[dict[str, object]],
    ) -> set[str]:
        if not terminal_symbols:
            return {str(row["process_id"]) for row in process_rows}
        return {
            self._repository.resolve_process_id(project_id, symbol)
            for symbol in terminal_symbols
        }

    def _snapshot_horizon(
        self,
        project_id: str,
        as_of: dt.datetime,
        scope: dict[str, object] | None,
    ) -> tuple[dt.datetime, dt.datetime]:
        schedule_input = self._schedule_input_for_scope(project_id, as_of, scope)
        projection = self._schedule(project_id, as_of, as_of, scope)
        total_effort_hours = 0.0
        for process in schedule_input.processes:
            revision = self._repository.selected_revision_as_of(
                project_id,
                process.process_id,
                as_of,
            )
            if revision is None:
                continue
            total_effort_hours += sum(
                float(requirement.effort_hours)
                for requirement in revision.role_requirements
            )
        horizon_start = min(schedule_input.start_at, as_of).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        horizon_finish = max(projection.completion_at, as_of)
        horizon_end = self._resource_capacity_horizon_end(
            project_id=project_id,
            horizon_start=horizon_start,
            seed_finish=horizon_finish,
            total_effort_hours=total_effort_hours,
        )
        if horizon_end <= horizon_start:
            horizon_end = horizon_start + dt.timedelta(days=1)
        return horizon_start, horizon_end

    def _resource_capacity_horizon_end(
        self,
        *,
        project_id: str,
        horizon_start: dt.datetime,
        seed_finish: dt.datetime,
        total_effort_hours: float,
    ) -> dt.datetime:
        horizon_end = (seed_finish + dt.timedelta(days=30)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        if total_effort_hours <= 0:
            return horizon_end

        for _attempt in range(8):
            if horizon_end <= horizon_start:
                horizon_end = horizon_start + dt.timedelta(days=1)
            capacity_hours = sum(
                float(bucket["capacity_hours"])
                for bucket in self._expanded_capacity(
                    project_id,
                    horizon_start,
                    horizon_end,
                    None,
                    None,
                )
            )
            if capacity_hours + 0.0001 >= total_effort_hours:
                return horizon_end
            current_days = max(1, (horizon_end - horizon_start).days)
            if capacity_hours <= 0:
                next_days = current_days * 2
            else:
                next_days = int(current_days * max(2.0, total_effort_hours / capacity_hours))
            horizon_end = horizon_start + dt.timedelta(days=min(next_days + 7, 36500))
        return horizon_end

    def _terminal_scope_data(
        self,
        terminal_symbols: list[str],
    ) -> dict[str, object] | None:
        if not terminal_symbols:
            return None
        return {
            "type": "topo_filter",
            "root_process_symbols": terminal_symbols,
            "direction": "ancestors",
        }

    def _schedule_snapshot_id(
        self,
        project_id: str,
        committed_at: dt.datetime,
        terminal_process_symbols: list[str],
    ) -> str:
        payload = json.dumps(
            {
                "project_id": project_id,
                "committed_at": committed_at.isoformat(),
                "terminal_process_symbols": terminal_process_symbols,
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"snapshot-{digest}"

    def _parse_datetime(self, value: Any) -> dt.datetime | None:
        if value is None:
            return None
        if isinstance(value, dt.datetime):
            return value
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    def _schedule_data(self, query: QuerySchedule) -> dict[str, object]:
        graph = self._dependency_graph_parts(
            query.project_id,
            query.as_of,
            query.now,
            query.scope,
        )
        return {
            "project_id": query.project_id,
            "as_of": query.as_of.isoformat(),
            "now": query.now.isoformat(),
            "nodes": graph["nodes"],
            "edges": graph["edges"],
            "critical_path_process_ids": graph["critical_path_process_ids"],
        }

    def _process_graph_data(
        self,
        query: QueryProcessGraph,
        *,
        include_warnings: bool = False,
    ) -> dict[str, object] | tuple[dict[str, object], list[Warning]]:
        graph = self._dependency_graph_parts(
            query.project_id,
            query.as_of,
            query.now,
            query.scope,
        )
        graph_nodes = list(graph["nodes"])
        graph_edges = list(graph["edges"])
        graph_critical_path_process_ids = list(graph["critical_path_process_ids"])
        if not query.include_resource_fields:
            visible_node_ids = {
                str(node["process_id"])
                for node in graph_nodes
                if str(node.get("process_type", "standard")) != "blocker"
            }
            graph_nodes = [
                node for node in graph_nodes if str(node["process_id"]) in visible_node_ids
            ]
            graph_edges = [
                edge
                for edge in graph_edges
                if str(edge["predecessor_process_id"]) in visible_node_ids
                and str(edge["successor_process_id"]) in visible_node_ids
            ]
            graph_critical_path_process_ids = [
                process_id
                for process_id in graph_critical_path_process_ids
                if str(process_id) in visible_node_ids
            ]
        resource_schedule = None
        resource_by_process: dict[str, dict[str, object]] = {}
        resource_warnings: list[Warning] = []
        if query.include_resource_fields:
            resource_schedule, resource_warnings = self._resource_schedule_data(query)
            resource_by_process = {
                row["process_id"]: row for row in resource_schedule["processes"]
            }
        nodes = []
        for node in graph_nodes:
            node = dict(node)
            if resource_schedule is None:
                node["resource_aware"] = None
            else:
                row = resource_by_process.get(node["process_id"])
                node["resource_aware"] = None
                if row is not None:
                    dependency = node.get("dependency_only") or {}
                    planned_start_at = self._parse_datetime(
                        row.get("starts_at") or dependency.get("es_at")
                    )
                    planned_finish_at = self._parse_datetime(
                        row.get("ends_at") or dependency.get("ef_at")
                    )
                    window_ends_at = self._parse_datetime(
                        row.get("schedule_window_ends_at")
                    )
                    inferred_duration_hours = row.get("inferred_duration_hours")
                    node["inferred_duration_hours"] = inferred_duration_hours
                    completedness = self._process_completedness_facts(
                        query.project_id,
                        str(node.get("process_id") or ""),
                        query.as_of,
                    )
                    node["started_at"] = (
                        completedness["started_at"].isoformat()
                        if completedness["started_at"] is not None
                        else None
                    )
                    node["finished_at"] = (
                        completedness["finished_at"].isoformat()
                        if completedness["finished_at"] is not None
                        else None
                    )
                    node["computed_status"] = str(completedness["status"])
                    node["status"] = node["computed_status"]
                    if planned_start_at is not None and planned_finish_at is not None:
                        node["work_now_window"] = {
                            "starts_at": planned_start_at.isoformat(),
                            "ends_at": planned_finish_at.isoformat(),
                            "active": (
                                node["computed_status"] in {"started", "due"}
                                and planned_start_at <= query.now < planned_finish_at
                            ),
                        }
                    if planned_finish_at is not None:
                        late_window_end = window_ends_at or planned_finish_at
                        node["late_risk_window"] = {
                            "starts_at": planned_finish_at.isoformat(),
                            "ends_at": late_window_end.isoformat(),
                            "active": False,
                        }
                    node["resource_aware"] = {
                        "ready_at": row["ready_at"],
                        "starts_at": row["starts_at"],
                        "ends_at": row["ends_at"],
                        "schedule_window_starts_at": row.get(
                            "schedule_window_starts_at"
                        ),
                        "schedule_window_ends_at": row.get("schedule_window_ends_at"),
                        "schedule_buffer_hours": row.get("schedule_buffer_hours"),
                        "schedule_elapsed_hours": row.get("schedule_elapsed_hours"),
                        "es_at": row.get("resource_es_at"),
                        "ef_at": row.get("resource_ef_at"),
                        "ls_at": row.get("resource_ls_at"),
                        "lf_at": row.get("resource_lf_at"),
                        "inferred_duration_hours": inferred_duration_hours,
                        "resource_delay_hours": row["resource_delay_hours"],
                        "slack_hours": row.get("resource_slack_hours"),
                        "criticality_label": "non_critical",
                        "role_sensitivity": row.get("role_sensitivity", []),
                        "max_makespan_sensitivity_hours": row.get(
                            "max_makespan_sensitivity_hours"
                        ),
                        "sensitivity_label": row.get(
                            "sensitivity_label",
                            "unknown",
                        ),
                        "allocation_state": row["allocation_state"],
                        "allocation_diagnostic": row.get("allocation_diagnostic"),
                    }
            if not query.include_resource_fields:
                dependency_node_fields = {
                    "process_id",
                    "process_symbol",
                    "aliases",
                    "name",
                    "description",
                    "duration_hours",
                    "inferred_duration_hours",
                    "earliest_start_at",
                    "status",
                    "started_at",
                    "finished_at",
                    "computed_status",
                    "blocker_summary",
                    "dependency_only",
                    "resource_aware",
                    "work_now_window",
                    "late_risk_window",
                    "required_roles",
                    "role_requirements",
                }
                node = {
                    key: value
                    for key, value in node.items()
                    if key in dependency_node_fields
                }
            nodes.append(node)
        data = {
            "project_id": query.project_id,
            "as_of": query.as_of.isoformat(),
            "now": query.now.isoformat(),
            "schedule_basis": (
                "resource_aware" if query.include_resource_fields else "dependency_only"
            ),
            "converged": (
                resource_schedule["converged"] if resource_schedule is not None else None
            ),
            "nodes": nodes,
            "edges": graph_edges,
            "critical_path_process_ids": (
                resource_schedule["critical_path_process_ids"]
                if resource_schedule is not None
                else graph_critical_path_process_ids
            ),
            "allocation_slices": (
                resource_schedule["allocation_slices"]
                if resource_schedule is not None and query.include_allocation_slices
                else []
            ),
        }
        if include_warnings:
            return data, resource_warnings
        return data

    def _dependency_graph_parts(
        self,
        project_id: str,
        as_of: dt.datetime,
        now: dt.datetime,
        scope: Any = None,
    ) -> dict[str, list[dict[str, object]]]:
        key = (
            "dependency_graph",
            project_id,
            self._repository_cache_version(project_id),
            self._cache_datetime(as_of),
            self._cache_datetime(now),
            self._scope_cache_key(scope),
        )

        def factory() -> dict[str, list[dict[str, object]]]:
            return self._build_dependency_graph_parts(project_id, as_of, now, scope)

        return self._cached_value(
            self._dependency_graph_cache,
            key,
            factory,
            copy_result=True,
        )

    def _build_dependency_graph_parts(
        self,
        project_id: str,
        as_of: dt.datetime,
        now: dt.datetime,
        scope: Any = None,
    ) -> dict[str, list[dict[str, object]]]:
        schedule_input = self._schedule_input_for_scope(
            project_id,
            as_of,
            scope,
        )
        projection = self._schedule(project_id, as_of, now, scope)
        input_by_id = {
            process.process_id: process for process in schedule_input.processes
        }
        processes = getattr(self._repository, "processes", {})
        aliases_by_process: dict[str, list[str]] = defaultdict(list)
        for alias, target_id in getattr(self._repository, "process_aliases", {}).get(
            project_id,
            {},
        ).items():
            if target_id in input_by_id:
                aliases_by_process[target_id].append(alias)
        blocker_summary = self._blocker_summary_by_process(project_id, as_of)
        pin_summaries = {
            process_id: self._pin_summary_for_process(project_id, process_id, as_of)
            for process_id in input_by_id
        }
        nodes = []
        edges = []
        for row in projection.rows:
            process = processes.get(row.process_id)
            revision = self._repository.selected_revision_as_of(
                project_id,
                row.process_id,
                as_of,
            )
            pin_summary = pin_summaries.get(row.process_id, {})
            process_symbol = getattr(process, "symbol", row.process_id)
            summary = blocker_summary.get(
                row.process_id,
                {
                    "unresolved_count": 0,
                    "blocking_count": 0,
                    "blocker_ids": [],
                },
            )
            facts = self._process_completedness_facts(
                project_id,
                row.process_id,
                as_of,
            )
            started_at = facts["started_at"]
            finished_at = facts["finished_at"]
            computed_status = str(facts["status"])
            duration_hours = input_by_id[row.process_id].duration_business_days * 8
            latest_start_at = row.latest_start_at
            latest_finish_at = row.latest_finish_at
            work_active = (
                computed_status in {"started", "due"}
                and row.earliest_start_at <= now < row.earliest_finish_at
            )
            late_active = False
            required_roles = dict(getattr(revision, "required_roles", {}) or {})
            role_requirements = self._role_requirements_json(
                list(getattr(revision, "role_requirements", []) or []),
                process_id=row.process_id,
                pin_summary=pin_summary,
            )
            node = {
                "process_id": row.process_id,
                "process_symbol": process_symbol,
                "process_type": getattr(process, "process_type", "standard"),
                "aliases": sorted(aliases_by_process.get(row.process_id, [])),
                "name": row.name,
                "description": revision.description if revision else "",
                "duration_hours": duration_hours,
                "duration_business_days": input_by_id[
                    row.process_id
                ].duration_business_days,
                "inferred_duration_hours": None,
                "dependencies": list(row.dependencies),
                "earliest_start_at": (
                    input_by_id[row.process_id].earliest_start_at.isoformat()
                    if input_by_id[row.process_id].earliest_start_at
                    else None
                ),
                "start_at_earliest": input_by_id[row.process_id].start_at_earliest,
                "delay_after_dependencies_business_days": input_by_id[
                    row.process_id
                ].delay_after_dependencies_business_days,
                "status": computed_status,
                "started_at": started_at.isoformat() if started_at else None,
                "finished_at": finished_at.isoformat() if finished_at else None,
                "computed_status": computed_status,
                "blocker_summary": summary,
                "dependency_only": {
                    "es_at": row.earliest_start_at.isoformat(),
                    "ef_at": row.earliest_finish_at.isoformat(),
                    "ls_at": latest_start_at.isoformat(),
                    "lf_at": latest_finish_at.isoformat(),
                    "slack_hours": row.total_float_business_days * 8,
                    "criticality_label": (
                        "critical" if row.is_critical else "non_critical"
                    ),
                },
                "resource_aware": None,
                "work_now_window": {
                    "starts_at": row.earliest_start_at.isoformat(),
                    "ends_at": latest_start_at.isoformat(),
                    "active": work_active,
                },
                "late_risk_window": {
                    "starts_at": latest_start_at.isoformat(),
                    "ends_at": latest_finish_at.isoformat(),
                    "active": late_active,
                },
            }
            if required_roles or role_requirements:
                node["required_roles"] = required_roles
            project_has_roles = bool(
                getattr(self._repository, "role_ids_by_project", {}).get(project_id, [])
            )
            if role_requirements or project_has_roles:
                node["role_requirements"] = role_requirements
            node["assumption_note"] = getattr(revision, "assumption_note", None)
            nodes.append(
                node
            )
            for predecessor_id in row.dependencies:
                predecessor = processes.get(predecessor_id)
                edge_id = getattr(self._repository, "dependency_edge_ids", {}).get(
                    (project_id, predecessor_id, row.process_id),
                    f"edge-{predecessor_id}-{row.process_id}",
                )
                edges.append(
                    {
                        "edge_id": edge_id,
                        "project_id": project_id,
                        "predecessor_process_id": predecessor_id,
                        "successor_process_id": row.process_id,
                        "predecessor_process_symbol": getattr(
                            predecessor,
                            "symbol",
                            predecessor_id,
                        ),
                        "successor_process_symbol": process_symbol,
                        "dependency_type": "finish_to_start",
                    }
                )
        return {
            "nodes": nodes,
            "edges": edges,
            "critical_path_process_ids": list(projection.critical_path),
        }

    def _completedness_status(
        self,
        *,
        started_at: dt.datetime | None,
        finished_at: dt.datetime | None,
        normal_dependencies_finished: bool,
        has_due_process_role: bool = False,
    ) -> str:
        if normal_dependencies_finished and finished_at is not None:
            return "finished"
        if started_at is not None:
            if normal_dependencies_finished:
                if has_due_process_role:
                    return "due"
                return "started"
            return "early_start"
        if not normal_dependencies_finished:
            return "waiting"
        return "ready"

    def _normal_dependencies_finished(
        self,
        processes: dict[str, object],
        dependency_ids: object,
        *,
        project_id: str | None = None,
        as_of: dt.datetime | None = None,
        memo: dict[tuple[str, str], dict[str, object]] | None = None,
    ) -> bool:
        for dependency_id in dependency_ids or []:
            dependency_id = str(dependency_id)
            dependency = processes.get(dependency_id)
            if dependency is None:
                return False
            dependency_status = self._enum_value(getattr(dependency, "status", None))
            if dependency_status == "canceled":
                continue
            if project_id is not None and as_of is not None:
                facts = self._process_completedness_facts(
                    project_id,
                    dependency_id,
                    as_of,
                    memo=memo,
                )
                if facts["is_finished"]:
                    continue
                return False
            elif dependency_status == "done":
                continue
            else:
                return False
        return True

    def _process_completedness_facts(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime,
        *,
        memo: dict[tuple[str, str], dict[str, object]] | None = None,
    ) -> dict[str, object]:
        if memo is None:
            memo = {}
        key = (project_id, process_id)
        if key in memo:
            return memo[key]
        processes = getattr(self._repository, "processes", {})
        revision = self._repository.selected_revision_as_of(
            project_id,
            process_id,
            as_of,
        )
        facts: dict[str, object] = {
            "started_at": None,
            "finished_at": None,
            "normal_dependencies_finished": False,
            "is_finished": False,
            "status": "waiting",
        }
        memo[key] = facts
        normal_dependencies_finished = (
            True
            if revision is None
            else self._normal_dependencies_finished(
                processes,
                revision.dependencies,
                project_id=project_id,
                as_of=as_of,
                memo=memo,
            )
        )
        pin_summary = self._pin_summary_for_process(project_id, process_id, as_of)
        pin_finished_at = pin_summary.get("finished_at")
        if not normal_dependencies_finished:
            pin_finished_at = None
        finished_at = pin_finished_at
        started_at = pin_summary.get("started_at")
        computed_status = self._completedness_status(
            started_at=started_at,
            finished_at=finished_at,
            normal_dependencies_finished=normal_dependencies_finished,
            has_due_process_role=bool(pin_summary.get("has_due_process_role")),
        )
        facts.update(
            {
                "started_at": started_at,
                "finished_at": finished_at,
                "normal_dependencies_finished": normal_dependencies_finished,
                "is_finished": bool(finished_at),
                "status": computed_status,
            }
        )
        return facts

    def _process_has_due_pin(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime,
    ) -> bool:
        if not process_id:
            return False
        return any(
            pin.status == "pinned_started"
            and pin.verified_done_at is None
            and pin.forecast_finish_at >= as_of
            for pin in self._repository.list_process_role_pins(
                project_id,
                as_of=as_of,
                process_id=process_id,
                include_done=False,
            )
        )

    def _blocker_summary_by_process(
        self,
        project_id: str,
        as_of: dt.datetime,
    ) -> dict[str, dict[str, object]]:
        summary = self._blocker_parent_summary_by_process(project_id, as_of)
        list_as_of = getattr(self._repository, "list_blockers_as_of", None)
        blockers = (
            list_as_of(project_id, as_of, False)
            if list_as_of is not None
            else self._repository.list_blockers(project_id, False)
        )
        active_ids = set(
            self._repository.active_process_ids_as_of(project_id, as_of)
            if hasattr(self._repository, "active_process_ids_as_of")
            else []
        )
        for blocker in blockers:
            if blocker.process_id not in active_ids:
                continue
            row = summary.setdefault(
                blocker.process_id,
                {"unresolved_count": 0, "blocking_count": 0, "blocker_ids": []},
            )
            if blocker.blocker_id in row["blocker_ids"]:
                continue
            row["unresolved_count"] += 1
            row["blocking_count"] += 1
            row["blocker_ids"].append(blocker.blocker_id)
        return summary

    def _blocker_parent_summary_by_process(
        self,
        project_id: str,
        as_of: dt.datetime,
    ) -> dict[str, dict[str, object]]:
        processes = getattr(self._repository, "processes", {})
        active_ids = set(
            self._repository.active_process_ids_as_of(project_id, as_of)
            if hasattr(self._repository, "active_process_ids_as_of")
            else []
        )
        summary: dict[str, dict[str, object]] = {}
        for process_id in sorted(active_ids):
            revision = self._repository.selected_revision_as_of(
                project_id,
                process_id,
                as_of,
            )
            if revision is None:
                continue
            for dependency_id in revision.dependencies:
                dependency = processes.get(dependency_id)
                if dependency is None:
                    continue
                if getattr(dependency, "process_type", "standard") != "blocker":
                    continue
                blocker_id = self._blocker_id_from_resolver(dependency.symbol)
                blocker = getattr(self._repository, "blockers", {}).get(blocker_id)
                if (
                    blocker is not None
                    and blocker.resolved_at is not None
                    and blocker.resolved_at <= as_of
                ):
                    continue
                row = summary.setdefault(
                    process_id,
                    {"unresolved_count": 0, "blocking_count": 0, "blocker_ids": []},
                )
                row["unresolved_count"] += 1
                row["blocking_count"] += 1
                row["blocker_ids"].append(blocker_id)
        return summary

    def _blocker_id_from_resolver(self, resolver_symbol: str) -> str:
        stem = str(resolver_symbol).removeprefix("resolve-")
        return f"blocker-{stem}"

    def _project_catalog_data(self, query: QueryProjectCatalog) -> dict[str, object]:
        self._repository.get_project(query.project_id)
        roles = [
            self._json_ready(role)
            for role in getattr(self._repository, "roles", {}).values()
            if role["project_id"] == query.project_id
        ]
        calendars = [
            self._json_ready(calendar)
            for calendar in getattr(self._repository, "calendars", {}).values()
            if calendar["project_id"] == query.project_id
        ]
        resources = [
            self._json_ready(resource)
            for resource in getattr(self._repository, "resources", {}).values()
            if resource["project_id"] == query.project_id
        ]
        return {
            "project_id": query.project_id,
            "roles": sorted(roles, key=lambda item: item["role_id"]),
            "calendars": sorted(calendars, key=lambda item: item["calendar_id"]),
            "resources": sorted(resources, key=lambda item: item["resource_id"]),
            "milestones": self._milestone_rows(query.project_id, include_inactive=True),
        }

    def _milestone_data(self, query: QueryMilestones) -> dict[str, object]:
        self._repository.get_project(query.project_id)
        return {
            "project_id": query.project_id,
            "milestones": self._milestone_rows(
                query.project_id,
                include_inactive=query.include_inactive,
            ),
        }

    def _milestone_rows(
        self,
        project_id: str,
        *,
        include_inactive: bool = False,
    ) -> list[dict[str, object]]:
        return [
            milestone.model_dump(mode="json")
            for milestone in self._repository.list_milestones(
                project_id,
                include_inactive=include_inactive,
            )
        ]

    def _slack_project_config_data(
        self,
        query: QuerySlackProjectConfig,
    ) -> dict[str, object]:
        config = self._repository.get_slack_project_config(query.project_id)
        mappings = self._repository.list_resource_slack_mappings(query.project_id)
        cursors = self._repository.list_slack_collection_cursors(query.project_id)
        token = self._repository.get_slack_bot_token(query.project_id)
        config_data = config.model_dump(mode="json")
        config_data["has_encrypted_bot_token"] = token is not None
        config_data["encrypted_bot_token_updated_at"] = (
            token.model_dump(mode="json")["updated_at"] if token is not None else None
        )
        return {
            "project_id": query.project_id,
            "config": config_data,
            "resource_mappings": [
                mapping.model_dump(mode="json") for mapping in mappings
            ],
            "collection_cursors": [
                cursor.model_dump(mode="json") for cursor in cursors
            ],
        }

    def _slack_bot_token_data(
        self,
        query: QuerySlackBotToken,
    ) -> dict[str, object]:
        token = self._repository.get_slack_bot_token(query.project_id)
        return {
            "project_id": query.project_id,
            "encrypted_token": (
                token.model_dump(mode="json") if token is not None else None
            ),
        }

    def _slack_runs_data(
        self,
        query: QuerySlackRuns,
    ) -> dict[str, object]:
        rows = self._repository.list_slack_runs(
            project_id=query.project_id,
            statuses=query.statuses,
            limit=query.limit,
        )
        return {
            "project_id": query.project_id,
            "runs": [row.model_dump(mode="json") for row in rows],
        }

    def _pending_slack_outbox_data(
        self,
        query: QueryPendingSlackOutbox,
    ) -> dict[str, object]:
        rows = self._repository.list_slack_outbox(
            project_id=query.project_id,
            statuses=query.statuses,
            limit=query.limit,
        )
        return {
            "project_id": query.project_id,
            "outbox": [row.model_dump(mode="json") for row in rows],
        }

    def _process_evidence_line_item_data(
        self,
        query: QueryProcessEvidenceLineItems,
    ) -> dict[str, object]:
        process_id = query.process_id
        if query.process_symbol is not None:
            process_id = self._resolve_process_id(
                project_id=query.project_id,
                process_id=None,
                process_symbol=query.process_symbol,
            )
        rows = self._repository.list_process_evidence_line_items(
            query.project_id,
            process_id=process_id,
            line_items=query.line_items,
        )
        return {
            "project_id": query.project_id,
            "process_id": process_id,
            "line_items": [row.model_dump(mode="json") for row in rows],
        }

    def _resource_evidence_line_item_data(
        self,
        query: QueryResourceEvidenceLineItems,
    ) -> dict[str, object]:
        rows = self._repository.list_resource_evidence_line_items(
            query.project_id,
            resource_id=query.resource_id,
            line_items=query.line_items,
        )
        return {
            "project_id": query.project_id,
            "resource_id": query.resource_id,
            "line_items": [row.model_dump(mode="json") for row in rows],
        }

    def _process_role_pin_data(
        self,
        query: QueryProcessRolePins,
    ) -> dict[str, object]:
        process_id = query.process_id
        if query.process_symbol is not None:
            process_id = self._resolve_process_id(
                project_id=query.project_id,
                process_id=None,
                process_symbol=query.process_symbol,
            )
        rows = self._repository.list_process_role_pins(
            query.project_id,
            as_of=query.as_of,
            process_id=process_id,
            resource_id=query.resource_id,
            include_done=query.include_done,
        )
        pin_rows = []
        for row in rows:
            payload = row.model_dump(mode="json")
            payload["pinned_started_at"] = payload.get("pinned_at")
            payload["verified_finished_at"] = payload.get("verified_done_at")
            pin_rows.append(payload)
        return {
            "project_id": query.project_id,
            "pins": pin_rows,
        }

    def _pm_markdown_context_data(
        self,
        query: QueryPMMarkdownContext,
    ) -> tuple[dict[str, object], list[Warning]]:
        agent_context, warnings = self._agent_context_data(
            QueryAgentContext(
                project_id=query.project_id,
                as_of=query.as_of,
                now=query.now,
                scope=query.scope,
                terminal_process_symbols=query.terminal_process_symbols,
                snapshot_limit=query.snapshot_limit,
                planning_granularity=query.planning_granularity,
                max_iterations=query.max_iterations,
                convergence_tolerance_hours=query.convergence_tolerance_hours,
                resource_schedule_backend=query.resource_schedule_backend,
                resource_schedule_mcts_c_puct=query.resource_schedule_mcts_c_puct,
                resource_schedule_mcts_max_actions=query.resource_schedule_mcts_max_actions,
                include_resource_sensitivity=query.include_resource_sensitivity,
                resource_schedule_sensitivity_backend=(
                    query.resource_schedule_sensitivity_backend
                ),
                resource_schedule_sensitivity_workers=(
                    query.resource_schedule_sensitivity_workers
                ),
                resource_schedule_sensitivity_process_pool=(
                    query.resource_schedule_sensitivity_process_pool
                ),
            )
        )
        evidence_line_items = self._pm_context_evidence_rows(
            query.project_id,
            agent_context,
            query.as_of,
        )
        markdown = self._pm_context_markdown(
            agent_context,
            evidence_line_items,
            generated_at=query.now,
        )
        return (
            {
                "project_id": query.project_id,
                "generated_at": query.now.isoformat(),
                "context_version": 1,
                "agent_context": agent_context,
                "evidence_line_items": evidence_line_items,
                "markdown": markdown,
            },
            warnings,
        )

    def _pm_context_evidence_rows(
        self,
        project_id: str,
        agent_context: dict[str, object],
        as_of: dt.datetime,
    ) -> list[dict[str, object]]:
        priority_by_symbol = self._pm_context_priority_by_symbol(agent_context)
        stored = {
            (row.process_id, row.line_item): row
            for row in self._repository.list_process_evidence_line_items(project_id)
        }
        rows = []
        for node in sorted(
            agent_context.get("graph", {}).get("nodes", []),
            key=lambda item: str(item.get("symbol") or ""),
        ):
            process_id = str(node.get("process_id") or "")
            process_symbol = str(node.get("symbol") or process_id)
            if not process_id:
                continue
            extra_line_items = sorted(
                line_item
                for stored_process_id, line_item in stored
                if stored_process_id == process_id
                and line_item not in LEGACY_PROCESS_EVIDENCE_LINE_ITEMS
                and line_item not in DEFAULT_PROCESS_EVIDENCE_LINE_ITEMS
            )
            line_items = [*DEFAULT_PROCESS_EVIDENCE_LINE_ITEMS, *extra_line_items]
            for line_item in line_items:
                record = stored.get((process_id, line_item))
                computed_last_modified_at = self._process_line_item_last_modified_at(
                    project_id,
                    process_id,
                    line_item,
                    as_of,
                )
                last_modified_at = (
                    computed_last_modified_at
                    or (record.last_modified_at if record is not None else None)
                    or as_of
                )
                effective_modified_at = last_modified_at
                if record is not None and record.last_modified_at > effective_modified_at:
                    effective_modified_at = record.last_modified_at
                rows.append(
                    self._pm_context_evidence_row(
                        project_id=project_id,
                        process_id=process_id,
                        process_symbol=process_symbol,
                        process_priority=priority_by_symbol.get(process_symbol),
                        line_item=line_item,
                        last_modified_at=effective_modified_at,
                        record=record,
                        as_of=as_of,
                    )
                )
        rows.extend(self._pm_context_resource_evidence_rows(project_id, as_of))
        return rows

    def _pm_context_resource_evidence_rows(
        self,
        project_id: str,
        as_of: dt.datetime,
    ) -> list[dict[str, object]]:
        resources = [
            (str(resource_id), resource)
            for resource_id, resource in getattr(
                self._repository,
                "resources",
                {},
            ).items()
            if resource.get("project_id") == project_id and resource.get("active", True)
        ]
        if not resources:
            return []
        resources.sort(key=lambda item: (str(item[1].get("name") or ""), item[0]))
        pins = self._repository.list_process_role_pins(project_id, as_of=as_of)
        project_plan_modified_at = self._latest_project_plan_modified_at(
            project_id,
            as_of,
        )
        pin_modified_by_resource = {
            resource_id: max(
                (pin.updated_at for pin in pins if pin.resource_id == resource_id),
                default=project_plan_modified_at,
            )
            for resource_id, _resource in resources
        }
        stored = {
            (row.resource_id, row.line_item): row
            for row in self._repository.list_resource_evidence_line_items(project_id)
        }
        rows = []
        for resource_id, resource in resources:
            resource_name = str(resource.get("name") or resource_id)
            for line_item in DEFAULT_RESOURCE_EVIDENCE_LINE_ITEMS:
                record = stored.get((resource_id, line_item))
                last_modified_at = (
                    pin_modified_by_resource[resource_id]
                    if line_item == RESOURCE_COMPLETE_PIN_COMMUNICATION
                    else project_plan_modified_at
                )
                if record is not None and record.last_modified_at > last_modified_at:
                    last_modified_at = record.last_modified_at
                rows.append(
                    self._pm_context_resource_evidence_row(
                        project_id=project_id,
                        resource_id=resource_id,
                        resource_name=resource_name,
                        line_item=line_item,
                        last_modified_at=last_modified_at,
                        record=record,
                        as_of=as_of,
                        target_days=7,
                    )
                )
        return rows

    def _latest_project_plan_modified_at(
        self,
        project_id: str,
        as_of: dt.datetime,
    ) -> dt.datetime:
        project = self._repository.get_project(project_id)
        timestamps = [project.start_at]
        for revisions in self._repository.revisions_by_process.values():
            timestamps.extend(
                revision.effective_at
                for revision in revisions
                if revision.project_id == project_id and revision.effective_at <= as_of
            )
        for pin in self._repository.list_process_role_pins(project_id, as_of=as_of):
            if pin.updated_at <= as_of:
                timestamps.append(pin.updated_at)
        return max(timestamps)

    def _pm_context_resource_evidence_row(
        self,
        *,
        project_id: str,
        resource_id: str,
        resource_name: str,
        line_item: str,
        last_modified_at: dt.datetime,
        record: ResourceEvidenceLineItemRecord | None,
        as_of: dt.datetime,
        target_days: int,
    ) -> dict[str, object]:
        last_evidence_at = record.last_evidence_at if record is not None else None
        evidence_age_exceeds_target = self._pm_evidence_age_exceeds_target(
            as_of=as_of,
            last_evidence_at=last_evidence_at,
            target_days=target_days,
        )
        target_label = f"< {target_days} {'day' if target_days == 1 else 'days'} old"
        question_by_line_item = {
            RESOURCE_UNDERSTANDS_PLAN: (
                f"Does {resource_name} accurately understand what they should "
                "be working on now and in the near future?"
            ),
            RESOURCE_COMPLETE_PIN_COMMUNICATION: (
                f"Has {resource_name} completely communicated pin data for "
                "things they worked on previously as well as presently?"
            ),
            RESOURCE_COMPLETE_PLANNING_COMMUNICATION: (
                f"Has {resource_name} completely communicated their project "
                "roadmap input so the plan is current and not adding avoidable risk?"
            ),
            RESOURCE_SLIPPAGE_RISK: (
                f"Has {resource_name} been slow to start or complete things "
                "they should do?"
            ),
        }
        if last_evidence_at is None:
            verification_state = "needs_evidence"
        elif last_evidence_at < last_modified_at:
            verification_state = "stale"
        else:
            verification_state = "current"
        evidence_is_stale = (
            verification_state != "current" or evidence_age_exceeds_target is True
        )
        return {
            "evidence_line_id": (
                record.evidence_line_id
                if record is not None
                else self._resource_evidence_line_id(project_id, resource_id, line_item)
            ),
            "project_id": project_id,
            "entity_type": "resource",
            "resource_id": resource_id,
            "resource_name": resource_name,
            "line_item": line_item,
            "last_modified_at": last_modified_at.isoformat(),
            "last_evidence_at": (
                last_evidence_at.isoformat() if last_evidence_at is not None else None
            ),
            "target_evidence_age_days": target_days,
            "target_evidence_age_label": target_label,
            "evidence_age_exceeds_target": evidence_age_exceeds_target,
            "evidence_is_stale": evidence_is_stale,
            "evidence_note": record.evidence_note if record is not None else None,
            "evidence_source": record.evidence_source if record is not None else None,
            "created_at": (
                record.created_at.isoformat() if record is not None else None
            ),
            "updated_at": (
                record.updated_at.isoformat() if record is not None else None
            ),
            "verification_state": verification_state,
            "is_current": verification_state == "current",
            "question": question_by_line_item.get(
                line_item,
                f"Do we have current evidence for {resource_name}.{line_item}?",
            ),
        }

    def _pm_context_evidence_row(
        self,
        *,
        project_id: str,
        process_id: str,
        process_symbol: str,
        process_priority: str | None,
        line_item: str,
        last_modified_at: dt.datetime,
        record: ProcessEvidenceLineItemRecord | None,
        as_of: dt.datetime,
    ) -> dict[str, object]:
        last_evidence_at = record.last_evidence_at if record is not None else None
        target_days = self._pm_evidence_freshness_target_days(process_priority)
        evidence_age_exceeds_target = self._pm_evidence_age_exceeds_target(
            as_of=as_of,
            last_evidence_at=last_evidence_at,
            target_days=target_days,
        )
        if last_evidence_at is None:
            verification_state = "needs_evidence"
        elif last_evidence_at < last_modified_at:
            verification_state = "stale"
        else:
            verification_state = "current"
        evidence_is_stale = (
            verification_state != "current" or evidence_age_exceeds_target is True
        )
        return {
            "evidence_line_id": (
                record.evidence_line_id
                if record is not None
                else self._process_evidence_line_id(project_id, process_id, line_item)
            ),
            "project_id": project_id,
            "process_id": process_id,
            "process_symbol": process_symbol,
            "process_priority": process_priority,
            "line_item": line_item,
            "last_modified_at": last_modified_at.isoformat(),
            "last_evidence_at": (
                last_evidence_at.isoformat() if last_evidence_at is not None else None
            ),
            "target_evidence_age_days": target_days,
            "target_evidence_age_label": self._pm_evidence_freshness_target_label(
                process_priority,
            ),
            "evidence_age_exceeds_target": evidence_age_exceeds_target,
            "evidence_is_stale": evidence_is_stale,
            "evidence_note": record.evidence_note if record is not None else None,
            "evidence_source": record.evidence_source if record is not None else None,
            "created_at": (
                record.created_at.isoformat() if record is not None else None
            ),
            "updated_at": (
                record.updated_at.isoformat() if record is not None else None
            ),
            "verification_state": verification_state,
            "is_current": verification_state == "current",
            "question": self._pm_evidence_question(process_symbol, line_item),
        }

    def _pm_evidence_question(self, process_symbol: str, line_item: str) -> str:
        question_by_line_item = {
            "blockers": (
                "do we have accurate knowledge of things blocking normal "
                "completion and whether blocker-type processes have timely "
                "forecasted resolutions?"
            ),
            "done_criteria": (
                "do we have an accurate objective description of the done criteria?"
            ),
            "plan_data": (
                "do we have accurate role, effort, parent, and child planning data?"
            ),
            "pin_data": (
                "do we accurately know whether someone is pinned, when they "
                "started, their finish forecast, and any verified finish?"
            ),
        }
        return (
            f"For `{process_symbol}`, "
            f"{question_by_line_item.get(line_item, f'is `{line_item}` current?')}"
        )

    def _pm_evidence_freshness_target_days(self, priority: str | None) -> int | None:
        if priority == "P0":
            return 1
        if priority == "P1":
            return 3
        if priority == "P2":
            return 7
        if priority == "P3":
            return 14
        return None

    def _pm_evidence_freshness_target_label(
        self,
        priority: str | None,
    ) -> str | None:
        days = self._pm_evidence_freshness_target_days(priority)
        if days is None:
            return None
        suffix = "day" if days == 1 else "days"
        return f"< {days} {suffix} old"

    def _pm_evidence_age_exceeds_target(
        self,
        *,
        as_of: dt.datetime,
        last_evidence_at: dt.datetime | None,
        target_days: int | None,
    ) -> bool | None:
        if target_days is None:
            return None
        if last_evidence_at is None:
            return True
        return as_of - last_evidence_at >= dt.timedelta(days=target_days)

    def _pm_context_markdown(
        self,
        agent_context: dict[str, object],
        evidence_line_items: list[dict[str, object]],
        *,
        generated_at: dt.datetime,
    ) -> str:
        project = agent_context.get("project") or {}
        lines = [
            "# Project",
            "",
            f"{project.get('name') or project.get('project_id')} (`{project.get('project_id')}`)",
            f"- Generated at: {generated_at.isoformat()}",
            f"- As of: {agent_context.get('as_of')}",
            f"- Now: {agent_context.get('now')}",
            "",
        ]
        lines.extend(self._pm_context_milestones_markdown(agent_context))
        lines.append("")
        lines.extend(
            self._pm_context_processes_markdown(
                agent_context,
                generated_at=generated_at,
            )
        )
        lines.append("")
        lines.extend(self._pm_context_continuity_markdown(project))
        lines.append("")
        lines.extend(
            self._pm_context_evidence_markdown(
                evidence_line_items,
                generated_at=generated_at,
            )
        )
        return "\n".join(lines)

    def _pm_context_milestones_markdown(
        self,
        agent_context: dict[str, object],
    ) -> list[str]:
        milestones = sorted(
            agent_context.get("milestones") or [],
            key=lambda item: (str(item.get("name") or ""), str(item.get("milestone_id"))),
        )
        lines = ["# Milestones"]
        if not milestones:
            return [*lines, "- None"]
        for milestone in milestones:
            milestone_id = str(milestone.get("milestone_id") or milestone.get("name"))
            terminal_symbols = milestone.get("process_symbols") or []
            lines.extend(
                [
                    f"## {milestone_id}",
                    f"- Terminal processes: {self._pm_plain_list(terminal_symbols)}",
                    (
                        "- Optimal make span: "
                        f"{self._pm_context_milestone_makespan(agent_context, milestone)}"
                    ),
                ]
            )
        return lines

    def _pm_context_processes_markdown(
        self,
        agent_context: dict[str, object],
        *,
        generated_at: dt.datetime,
    ) -> list[str]:
        graph = agent_context.get("graph") or {}
        relative_to = self._parse_datetime(agent_context.get("now")) or generated_at
        schedule_by_symbol = {
            str(row.get("symbol")): row
            for row in (agent_context.get("schedule") or {}).get("processes", [])
        }
        assignments_by_symbol = self._pm_context_assignments_by_symbol(agent_context)
        priority_by_symbol = self._pm_context_priority_by_symbol(agent_context)
        role_names = self._pm_context_role_names_by_id(agent_context)
        resource_names = self._pm_context_resource_names_by_id(agent_context)
        lines = [
            "# Processes",
            (
                "Priority: P0 pinned with status started, early_start, or due, "
                "or planned start < 3 days; P1 planned start >= 3 days and "
                "< 7 days; P2 planned start >= 7 days and < 14 days; P3 "
                "planned start >= 14 days."
            ),
            "Type: normal is project work; blocker is a resolver process for one blocker.",
            (
                "Mode: planned is unpinned and scheduler-planned; pinned uses a "
                "resource pin start plus forecast or verified finish."
            ),
            (
                "Status: derived process completedness: waiting, early_start, "
                "ready, started, due, or finished."
            ),
            (
                "Role requirement: the single role this process requires, plus "
                "the requirement id when present."
            ),
            "Effort hours: planning estimate only; this is not spent or remaining work.",
            (
                "Sensitivity: optimal makespan change from adding 1 hour to the "
                "process role; positive means critical."
            ),
            "Definition: done criteria for the process.",
            (
                "Parents: direct predecessor process symbols; blocker resolver "
                "processes appear here like normal parents."
            ),
            "Children: direct successor process symbols.",
            "Assigned to: scheduler-selected resource for planned mode, not ownership evidence.",
            "Planned start/finish: scheduler timing for planned mode.",
            (
                "Pre-buffer | duration | post-buffer: slack before, planned "
                "duration, and slack after the scheduled work."
            ),
            "Pinned to: resource carrying the pin.",
            "Pinned started: when the pinned resource started the process role.",
            "Forecasted finish: current forecast for an active pin.",
            "Verified finish: final completion time after the pinned resource verifies done.",
        ]
        nodes = sorted(graph.get("nodes", []), key=lambda item: str(item.get("symbol")))
        if not nodes:
            return [*lines, "- None"]
        for node in nodes:
            symbol = str(node.get("symbol") or node.get("process_id"))
            schedule = schedule_by_symbol.get(symbol, {})
            process = {
                "planned_start_at": schedule.get("planned_start_at"),
                "planned_finish_at": schedule.get("planned_finish_at"),
                "schedule_window_starts_at": schedule.get("schedule_window_starts_at"),
                "schedule_window_ends_at": schedule.get("schedule_window_ends_at"),
            }
            requirement = self._pm_context_single_role_requirement(node)
            role_id = str(requirement.get("role_id") or "") if requirement else ""
            role_label = (
                self._pm_context_role_label([role_id], role_names)
                if role_id
                else "-"
            )
            requirement_id = (
                str(requirement.get("requirement_id") or "")
                if requirement
                else ""
            )
            pins = (
                [pin for pin in requirement.get("pins", []) if isinstance(pin, dict)]
                if requirement
                else []
            )
            mode = "pinned" if pins else "planned"
            sensitivity = self._pm_assignment_sensitivity(
                schedule,
                {"role_ids": [role_id]},
            )
            lines.extend(
                [
                    (
                        f"## {priority_by_symbol.get(symbol, 'P?')} | "
                        f"{symbol} | {node.get('name') or '-'}"
                    ),
                    "",
                    f"Type: {self._pm_process_type_label(node.get('process_type'))}",
                    f"Mode: {mode}",
                    f"Status: {node.get('computed_status') or 'unknown'}",
                    (
                        "Role requirement: "
                        f"{role_label}{f' | {requirement_id}' if requirement_id else ''}"
                    ),
                    (
                        "Effort hours: "
                        f"{self._pm_format_hours(requirement.get('effort_hours'))}"
                        if requirement
                        else "Effort hours: -"
                    ),
                    f"Sensitivity: {self._pm_format_signed_hours(sensitivity)}",
                    f"Definition: {node.get('description') or 'needs confirmation'}",
                    f"Parents: {self._pm_context_braced_symbols(node.get('predecessors'))}",
                    f"Children: {self._pm_context_braced_symbols(node.get('successors'))}",
                    "",
                    *self._pm_context_process_mode_lines(
                        mode=mode,
                        pins=pins,
                        assignments=assignments_by_symbol[symbol],
                        resource_names=resource_names,
                        role_id=role_id,
                        process=process,
                        relative_to=relative_to,
                    ),
                    "",
                ]
            )
        return lines

    def _pm_context_continuity_markdown(
        self,
        project: dict[str, object],
    ) -> list[str]:
        project_id = str(project.get("project_id") or "")
        config = self._repository.get_slack_project_config(project_id)
        note = config.continuity_note or "None recorded."
        if len(note) > 4096:
            note = f"{note[:4096]}\n[continuity note truncated to 4096 characters]"
        updated_at = (
            config.continuity_updated_at.isoformat()
            if config.continuity_updated_at
            else None
        )
        lines = ["# Continuity note", note]
        if updated_at:
            lines.append(f"- Updated at: {updated_at}")
        return lines

    def _pm_context_evidence_markdown(
        self,
        evidence_line_items: list[dict[str, object]],
        *,
        generated_at: dt.datetime,
    ) -> list[str]:
        process_items = [
            item
            for item in evidence_line_items
            if item.get("entity_type") != "resource"
        ]
        resource_items = [
            item
            for item in evidence_line_items
            if item.get("entity_type") == "resource"
        ]
        lines = [
            "# Evidence",
            (
                'Go through each line item and give a "Yes" or "No" if new '
                "evidence regarding that line item is available (including "
                "affirmative negatives), followed by a short reason and outcome "
                "and if you'll record updated evidence."
            ),
            (
                "Line items with '*' are considered to have stale evidence and "
                "should be prioritised."
            ),
            "",
            "Examples:",
            (
                "- Yes, Josh confirmed no new blockers on this process, so blockers "
                "stay unchanged and I will update blocker evidence."
            ),
            (
                "- Yes, Scott said he is no longer working on this, so I will "
                "unpin him from the relevant process-role and update pin evidence."
            ),
            (
                "- No, the new messages do not mention done criteria, so I will not "
                "touch done criteria or evidence."
            ),
            "",
            "## Process Evidence",
            "Staleness targets: P0 < 1 day, P1 < 3 days, P2 < 7 days, P3 < 14 days",
            (
                "where P0=pinned with status started, early_start, or due, "
                "or planned with planned start < 3 days; "
                "P1=planned with planned start >= 3 days < 7 days; "
                "P2=planned with planned start >= 7 days < 14 days; "
                "P3=planned with planned start >= 14 days."
            ),
            "",
            (
                "`blockers`: do we have accurate knowledge of things blocking "
                "the process's normal completion as well as ensuring those "
                "blocker-type processes are getting pinned with a timely "
                "forecasted resolution?"
            ),
            (
                "`done_criteria`: do we have accurate objective description of "
                "the process's done criteria?"
            ),
            (
                "`plan_data`: do we have accurate knowledge of the role needed "
                "and effort hours needed for complete execution, as well as "
                "the parents and children are correctly identified?"
            ),
            (
                "`pin_data`: do we have accurate knowledge of whether someone "
                "is pinned working on it, and if so when they started and their "
                "finish forecast, and once they should be done, verification "
                "of when it was successfully completed?"
            ),
            "",
        ]
        lines.extend(
            self._pm_context_evidence_line_item_lines(process_items, generated_at)
        )
        lines.extend(
            [
                "",
                "## Resource Evidence",
                "Staleness targets: < 7 days.",
                "",
                (
                    "`understands_plan`: do they have accurate knowledge of "
                    "that they should be working on now and in the near future?"
                ),
                (
                    "`complete_pin_communication`: have they communicated "
                    "complete pin data for things that have worked on "
                    "previously as well as presently?"
                ),
                (
                    "`complete_planning_communication`: have they completely "
                    "communicated their input for the project roadmap, so that "
                    "our plan is the best it could be, not introducing extra "
                    "risk, and it's not out of date?"
                ),
                (
                    "`slippage_risk`: have they been slow to start or complete "
                    "things they should do?"
                ),
                "",
            ]
        )
        lines.extend(
            self._pm_context_evidence_line_item_lines(resource_items, generated_at)
        )
        return lines

    def _pm_context_evidence_line_item_lines(
        self,
        evidence_line_items: list[dict[str, object]],
        generated_at: dt.datetime,
    ) -> list[str]:
        if not evidence_line_items:
            return ["- None"]
        lines = []
        for item in evidence_line_items:
            symbol = str(item.get("resource_name") or item.get("process_symbol"))
            line_item = str(item.get("line_item"))
            priority = item.get("process_priority")
            target_label = item.get("target_evidence_age_label")
            modified_age = self._pm_relative_age(
                generated_at,
                item.get("last_modified_at"),
            )
            evidence_age = self._pm_relative_age(
                generated_at,
                item.get("last_evidence_at"),
            )
            target_text = (
                f" Target evidence age: {target_label} ({priority})."
                if target_label and priority
                else f" Target evidence age: {target_label}."
                if target_label
                else ""
            )
            prefix = "*" if item.get("evidence_is_stale") else ""
            lines.append(
                f"{prefix}{symbol}.{line_item} last modified {modified_age}, "
                f"last evidence that it's correct {evidence_age}.{target_text}"
            )
        return lines

    def _pm_evidence_line_item_definition_lines(
        self,
        evidence_line_items: list[dict[str, object]],
    ) -> list[str]:
        definitions = {
            "blockers": (
                "confirms the process's blockers and timely blocker-process "
                "resolution forecasts are still correct."
            ),
            "pin_data": (
                "confirms whether the process role is planned or pinned, and if "
                "pinned, the pinned resource, pinned start, finish forecast, and "
                "verified finish."
            ),
            "done_criteria": (
                "confirms the process done definition still matches what the "
                "team means by complete."
            ),
            "plan_data": (
                "confirms the role, effort, parents, and children are still correct."
            ),
            RESOURCE_UNDERSTANDS_PLAN: (
                "confirms whether the resource understands current and near-future work."
            ),
            RESOURCE_COMPLETE_PIN_COMMUNICATION: (
                "confirms complete current and previous pin communication."
            ),
            RESOURCE_COMPLETE_PLANNING_COMMUNICATION: (
                "confirms complete roadmap input from the resource."
            ),
            RESOURCE_SLIPPAGE_RISK: (
                "confirms whether the resource has been slow to start or finish work."
            ),
        }
        present = sorted(
            {
                str(item.get("line_item") or "")
                for item in evidence_line_items
                if item.get("line_item")
            }
        )
        if not present:
            present = sorted(definitions)
        return [
            f"- `{line_item}`: {definitions.get(line_item, 'custom evidence line item.')}"
            for line_item in present
        ]

    def _pm_context_assignments_by_symbol(
        self,
        agent_context: dict[str, object],
    ) -> dict[str, list[dict[str, object]]]:
        assignments: dict[str, list[dict[str, object]]] = defaultdict(list)
        for resource in (agent_context.get("prioritized_work") or {}).get(
            "by_resource",
            [],
        ):
            resource_id = resource.get("resource_id")
            resource_name = resource.get("resource_name") or resource_id
            for process in resource.get("processes") or []:
                symbol = str(process.get("process_symbol") or "")
                if not symbol:
                    continue
                assignments[symbol].append(
                    {
                        "resource_id": resource_id,
                        "resource_name": resource_name,
                        "role_ids": process.get("role_ids") or [],
                        "priority": process.get("priority"),
                        "effort_hours": process.get("effort_hours"),
                        "max_makespan_sensitivity_hours": process.get(
                            "max_makespan_sensitivity_hours"
                        ),
                    }
                )
        for rows in assignments.values():
            rows.sort(
                key=lambda row: (
                    str(row.get("resource_name") or ""),
                    str(row.get("resource_id") or ""),
                    ",".join(str(role_id) for role_id in row.get("role_ids") or []),
                )
            )
        return assignments

    def _pm_context_single_role_requirement(
        self,
        node: dict[str, object],
    ) -> dict[str, object] | None:
        requirements = node.get("role_requirements") or []
        for requirement in requirements:
            if isinstance(requirement, dict):
                return requirement
        return None

    def _pm_context_braced_symbols(self, value: object) -> str:
        if not value:
            return "{}"
        if isinstance(value, str):
            symbols = [value]
        elif isinstance(value, (list, tuple, set)):
            symbols = [str(item) for item in value if item]
        else:
            symbols = [str(value)]
        unique = sorted(dict.fromkeys(symbols))
        return "{" + ", ".join(unique) + "}" if unique else "{}"

    def _pm_context_assigned_resource_text(
        self,
        assignments: list[dict[str, object]],
        resource_names: dict[str, str],
        role_id: str,
    ) -> str:
        labels = []
        for assignment in assignments:
            role_ids = {str(item) for item in assignment.get("role_ids") or []}
            if role_id and role_ids and role_id not in role_ids:
                continue
            resource_id = str(assignment.get("resource_id") or "")
            if not resource_id:
                continue
            name = (
                assignment.get("resource_name")
                or resource_names.get(resource_id)
                or resource_id
            )
            labels.append(f"{name} ({resource_id})")
        labels = sorted(dict.fromkeys(labels))
        return ", ".join(labels) if labels else "unassigned"

    def _pm_context_process_mode_lines(
        self,
        *,
        mode: str,
        pins: list[dict[str, object]],
        assignments: list[dict[str, object]],
        resource_names: dict[str, str],
        role_id: str,
        process: dict[str, object],
        relative_to: dt.datetime,
    ) -> list[str]:
        if mode == "pinned":
            return self._pm_context_pinned_process_lines(
                pins,
                resource_names,
                relative_to,
            )
        return [
            (
                "Assigned to: "
                f"{self._pm_context_assigned_resource_text(assignments, resource_names, role_id)}"
            ),
            "Planned start: "
            + self._pm_markdown_datetime_with_delta(
                process.get("planned_start_at"),
                relative_to,
            ),
            "Planned finish: "
            + self._pm_markdown_datetime_with_delta(
                process.get("planned_finish_at"),
                relative_to,
            ),
            self._pm_schedule_window_line(process),
        ]

    def _pm_context_pinned_process_lines(
        self,
        pins: list[dict[str, object]],
        resource_names: dict[str, str],
        relative_to: dt.datetime,
    ) -> list[str]:
        resource_labels = []
        pinned_starts = []
        verified_finishes = []
        forecast_finishes = []
        has_unverified_pin = False
        for pin in pins:
            resource_id = str(pin.get("resource_id") or "")
            if resource_id:
                resource_labels.append(
                    self._pm_context_resource_label([resource_id], resource_names)
                )
            pinned_at = self._parse_datetime(pin.get("pinned_at") or pin.get("starts_at"))
            if pinned_at is not None:
                pinned_starts.append(pinned_at)
            verified_at = self._parse_datetime(
                pin.get("verified_finished_at")
                or pin.get("verified_done_at")
                or pin.get("ends_at")
            )
            if verified_at is not None:
                verified_finishes.append(verified_at)
            else:
                has_unverified_pin = True
            forecast_at = self._parse_datetime(pin.get("forecast_finish_at"))
            if forecast_at is not None:
                forecast_finishes.append(forecast_at)

        lines = [
            (
                "Pinned to: "
                f"{', '.join(sorted(dict.fromkeys(resource_labels))) if resource_labels else '-'}"
            ),
            "Pinned started: "
            + self._pm_markdown_datetime_with_delta(
                min(pinned_starts) if pinned_starts else None,
                relative_to,
            ),
        ]
        if has_unverified_pin or not verified_finishes:
            forecast_finish = max(forecast_finishes) if forecast_finishes else None
            lines.append(
                "Forecasted finish: "
                f"{self._pm_markdown_datetime_with_delta(forecast_finish, relative_to)}"
            )
        else:
            lines.append(
                "Verified finish: "
                f"{self._pm_markdown_datetime_with_delta(max(verified_finishes), relative_to)}"
            )
        return lines

    def _pm_context_priority_by_symbol(
        self,
        agent_context: dict[str, object],
    ) -> dict[str, str]:
        now = self._parse_datetime(agent_context.get("now"))
        if now is None:
            return {}
        schedule_by_symbol = {
            str(process.get("symbol") or ""): process
            for process in (agent_context.get("schedule") or {}).get("processes") or []
        }
        priorities: dict[str, str] = {}
        for node in (agent_context.get("graph") or {}).get("nodes") or []:
            symbol = str(node.get("symbol") or "")
            if not symbol:
                continue
            requirements = node.get("role_requirements") or []
            is_pinned = any(
                requirement.get("pins")
                for requirement in requirements
                if isinstance(requirement, dict)
            )
            status = str(node.get("computed_status") or node.get("status") or "")
            if is_pinned and status in {"started", "early_start", "due"}:
                priorities[symbol] = "P0"
                continue
            if is_pinned:
                continue
            priority = self._pm_priority_from_schedule_row(
                schedule_by_symbol.get(symbol, {}),
                now,
            )
            if priority:
                priorities[symbol] = priority
        return priorities

    def _pm_priority_from_schedule_row(
        self,
        process: dict[str, object],
        now: dt.datetime,
    ) -> str | None:
        planned_start_at = self._parse_datetime(process.get("planned_start_at"))
        if planned_start_at is None:
            return None
        time_until_start = planned_start_at - now
        if time_until_start < dt.timedelta(days=3):
            return "P0"
        if time_until_start < dt.timedelta(days=7):
            return "P1"
        if time_until_start < dt.timedelta(days=14):
            return "P2"
        return "P3"

    def _pm_priority_rank(self, value: str) -> int:
        if value.startswith("P"):
            try:
                return int(value[1:])
            except ValueError:
                return 999
        return 999

    def _pm_context_role_names_by_id(
        self,
        agent_context: dict[str, object],
    ) -> dict[str, str]:
        project_id = str((agent_context.get("project") or {}).get("project_id") or "")
        roles = getattr(self._repository, "roles", {})
        output = {}
        for role_id, role in roles.items():
            if role.get("project_id") != project_id:
                continue
            output[str(role_id)] = str(role.get("name") or role_id)
        return output

    def _pm_context_resource_names_by_id(
        self,
        agent_context: dict[str, object],
    ) -> dict[str, str]:
        project_id = str((agent_context.get("project") or {}).get("project_id") or "")
        resources = getattr(self._repository, "resources", {})
        output = {}
        for resource_id, resource in resources.items():
            if resource.get("project_id") != project_id:
                continue
            output[str(resource_id)] = str(resource.get("name") or resource_id)
        return output

    def _pm_context_role_label(
        self,
        role_ids: object,
        role_names: dict[str, str],
    ) -> str:
        ids = [str(role_id) for role_id in role_ids or [] if role_id]
        if not ids:
            return "Unassigned role"
        return ", ".join(
            f"{role_names.get(role_id, role_id)} ({role_id})"
            for role_id in ids
        )

    def _pm_context_resource_label(
        self,
        resource_ids: list[str],
        resource_names: dict[str, str],
    ) -> str:
        if not resource_ids:
            return "Nobody"
        return ", ".join(
            f"{resource_names.get(resource_id, resource_id)} ({resource_id})"
            for resource_id in resource_ids
        )

    def _pm_assignment_sensitivity(
        self,
        schedule: dict[str, object],
        assignment: dict[str, object],
    ) -> object:
        sensitivity = assignment.get("max_makespan_sensitivity_hours")
        if sensitivity is not None:
            return sensitivity
        role_ids = {str(role_id) for role_id in assignment.get("role_ids") or []}
        matching = [
            item.get("makespan_delta_hours")
            for item in schedule.get("role_sensitivity") or []
            if str(item.get("role_id") or "") in role_ids
            and item.get("makespan_delta_hours") is not None
        ]
        if matching:
            return max(matching)
        return schedule.get("max_makespan_sensitivity_hours")

    def _pm_format_signed_hours(self, value: object) -> str:
        try:
            hours = float(value) if value is not None else None
        except (TypeError, ValueError):
            hours = None
        if hours is None:
            return "-"
        sign = "+" if hours >= 0 else "-"
        absolute_hours = abs(hours)
        unit = "hour" if absolute_hours == 1 else "hours"
        return f"{sign} {self._clean_number(absolute_hours)} {unit}"

    def _pm_plain_list(self, value: object) -> str:
        if value in (None, "", []):
            return "-"
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, (list, tuple, set)):
            parts = [str(part) for part in value if part]
        else:
            parts = [str(value)]
        return ", ".join(parts) if parts else "-"

    def _pm_context_milestone_makespan(
        self,
        agent_context: dict[str, object],
        milestone: dict[str, object],
    ) -> str:
        project_start = self._parse_datetime(
            (agent_context.get("project") or {}).get("start_at")
        )
        completion = (milestone.get("slippage") or {}).get("latest") or {}
        completion_at = self._parse_datetime(completion.get("completion_at"))
        if completion_at is None:
            terminal_symbols = {
                str(symbol) for symbol in milestone.get("process_symbols") or []
            }
            terminal_finishes = [
                self._parse_datetime(row.get("planned_finish_at"))
                for row in (agent_context.get("schedule") or {}).get("processes", [])
                if str(row.get("symbol") or "") in terminal_symbols
            ]
            completion_at = self._latest_datetime(terminal_finishes)
        if completion_at is None:
            return "-"
        date_text = (
            f"{completion_at.day} {completion_at.strftime('%B')}, "
            f"{completion_at.year}"
        )
        if project_start is None:
            return date_text
        days = (completion_at - project_start).total_seconds() / 86400
        return f"{date_text} ({self._clean_number(round(days, 2))} days)"

    def _pm_relative_age(self, now: dt.datetime, value: object) -> str:
        parsed = self._parse_datetime(value)
        if parsed is None:
            return "never"
        delta_days = (now - parsed).total_seconds() / 86400
        if delta_days < 0:
            return f"in {self._clean_number(round(abs(delta_days), 2))} days"
        return f"{self._clean_number(round(delta_days, 2))} days ago"

    def _pm_context_role_requirement_line(
        self,
        node: dict[str, object],
    ) -> str:
        requirements = node.get("role_requirements") or []
        if not requirements:
            return "-"
        parts = []
        for requirement in requirements:
            role_id = requirement.get("role_id")
            effort = self._pm_format_hours(requirement.get("effort_hours"))
            active = self._pm_markdown_list(
                requirement.get("active_pinned_resource_ids") or []
            )
            recent = self._pm_markdown_list(
                requirement.get("recent_pinned_resource_ids") or []
            )
            pins = requirement.get("pins") or []
            forecast = "-"
            if pins:
                latest_pin = max(
                    pins,
                    key=lambda item: str(item.get("forecast_finish_at") or ""),
                )
                forecast = self._pm_markdown_datetime(
                    latest_pin.get("forecast_finish_at"),
                )
            parts.append(
                f"`{role_id}` effort estimate {effort}; currently pinned {active}; "
                f"recent pins {recent}; forecast finish {forecast}"
            )
        return "; ".join(parts)

    def _pm_process_type_label(self, value: object) -> str:
        process_type = str(value or "standard")
        return "normal" if process_type == "standard" else process_type

    def _pm_communication_protocol_data(
        self,
        query: QueryPMCommunicationProtocol,
    ) -> dict[str, object]:
        """Build verifiable PM communication obligations from schedule state."""
        self._repository.get_project(query.project_id)
        agent_context, _warnings = self._agent_context_data(
            QueryAgentContext(
                project_id=query.project_id,
                as_of=query.as_of,
                now=query.now,
                planning_granularity=query.planning_granularity,
                max_iterations=query.max_iterations,
                convergence_tolerance_hours=query.convergence_tolerance_hours,
                resource_schedule_backend=query.resource_schedule_backend,
                resource_schedule_mcts_c_puct=query.resource_schedule_mcts_c_puct,
                resource_schedule_mcts_max_actions=(
                    query.resource_schedule_mcts_max_actions
                ),
                include_resource_sensitivity=query.include_resource_sensitivity,
                resource_schedule_sensitivity_backend=(
                    query.resource_schedule_sensitivity_backend
                ),
                resource_schedule_sensitivity_workers=(
                    query.resource_schedule_sensitivity_workers
                ),
                resource_schedule_sensitivity_process_pool=(
                    query.resource_schedule_sensitivity_process_pool
                ),
            )
        )
        evidence_rows = self._repository.list_pm_communication_evidence(
            query.project_id,
        )
        evidence = [row.model_dump(mode="json") for row in evidence_rows]
        active_slack_mappings = {
            mapping.resource_id: mapping
            for mapping in self._repository.list_resource_slack_mappings(
                query.project_id,
            )
            if mapping.active and mapping.slack_user_id
        }
        resource_processes = self._pm_resource_process_rows(
            query.project_id,
            agent_context,
            evidence_rows,
            query.as_of,
            active_slack_mappings,
        )
        resource_processes = self._with_mapped_empty_resource_reviews(
            query.project_id,
            resource_processes,
            evidence_rows,
            active_slack_mappings,
        )
        obligations = self._pm_protocol_obligations(
            resource_processes,
            evidence_rows,
            query.now,
        )
        if not query.include_satisfied:
            obligations = [
                obligation
                for obligation in obligations
                if obligation["status"] in {"due_now", "overdue"}
            ]
        return {
            "project_id": query.project_id,
            "generated_at": query.now.isoformat(),
            "protocol_version": 1,
            "rules": {
                "pre_start_3_day_notice": (
                    "Send full process info no later than 3 days before planned "
                    "start."
                ),
                "pre_start_24_hour_notice": (
                    "Send full process info no later than 24 hours before planned "
                    "start."
                ),
                "resource_assignment_review": (
                    "Send each resource their full assigned-process list at least "
                    "every 14 days."
                ),
                "overdue_checkin": (
                    "After planned finish passes, check in every 24 hours until done."
                ),
                "in_progress_checkin": "Once in progress, check in every 3 days until done.",
                "message_receipt_ack": (
                    "Acknowledge every teammate or team-channel message received by "
                    "the PM bot."
                ),
                "project_update_notice": (
                    "Tell affected teammates explicitly when project data changes "
                    "because of their information."
                ),
            },
            "resource_processes": resource_processes,
            "obligations": obligations,
            "evidence": evidence,
        }

    def _pm_resource_process_rows(
        self,
        project_id: str,
        agent_context: dict[str, object],
        evidence_rows: list[PMCommunicationEvidenceRecord],
        as_of: dt.datetime,
        active_slack_mappings: dict[str, SlackResourceMappingRecord],
    ) -> list[dict[str, object]]:
        graph = agent_context.get("graph") or {}
        nodes = graph.get("nodes") or []
        nodes_by_symbol = {
            str(node.get("symbol") or node.get("process_symbol")): node
            for node in nodes
            if isinstance(node, dict) and (node.get("symbol") or node.get("process_symbol"))
        }
        role_names = self._pm_context_role_names_by_id(agent_context)
        resource_names = self._pm_context_resource_names_by_id(agent_context)
        open_blockers_by_process: dict[str, list[dict[str, object]]] = {}
        for blocker in agent_context.get("blockers") or []:
            if not isinstance(blocker, dict) or blocker.get("is_resolved_as_of"):
                continue
            summary = {
                "blocker_id": blocker.get("blocker_id"),
                "blocker_symbol": blocker.get("blocker_symbol")
                or blocker.get("resolver_process_symbol")
                or self._blocker_resolver_symbol(str(blocker.get("blocker_id") or "")),
                "summary": blocker.get("summary"),
                "details": blocker.get("details"),
                "severity": blocker.get("severity"),
                "resolution_owner_resource_id": blocker.get(
                    "resolution_owner_resource_id"
                ),
            }
            for key in (blocker.get("process_id"), blocker.get("process_symbol")):
                if key:
                    open_blockers_by_process.setdefault(str(key), []).append(summary)
        prioritized = agent_context.get("prioritized_work") or {}
        output = []
        for resource in prioritized.get("by_resource") or []:
            if not isinstance(resource, dict):
                continue
            resource_id = str(resource.get("resource_id") or "")
            if not resource_id:
                continue
            mapping = active_slack_mappings.get(resource_id)
            process_rows = []
            for process in resource.get("processes") or []:
                if not isinstance(process, dict):
                    continue
                symbol = str(process.get("process_symbol") or "")
                node = nodes_by_symbol.get(symbol, {})
                process_id = process.get("process_id") or node.get("process_id")
                requirement = self._pm_context_single_role_requirement(node)
                role_id = str(requirement.get("role_id") or "") if requirement else ""
                pin_history = self._pm_resource_pin_history(node, resource_id)
                planned_start_at = process.get("planned_start_at")
                planned_finish_at = process.get("planned_finish_at")
                schedule_window_starts_at = process.get("schedule_window_starts_at")
                schedule_window_ends_at = process.get("schedule_window_ends_at")
                blockers = (
                    open_blockers_by_process.get(str(process_id or ""))
                    or open_blockers_by_process.get(symbol)
                    or []
                )
                assignment_certainty = self._pm_resource_assignment_certainty(
                    node,
                    resource_id,
                )
                ownership_evidence_state = (
                    self._pm_resource_ownership_evidence_state(node, resource_id)
                )
                row = {
                    "resource_id": resource_id,
                    "resource_name": resource.get("resource_name") or resource_id,
                    "target_type": "dm" if mapping is not None else "unmapped_resource",
                    "slack_user_id": mapping.slack_user_id if mapping is not None else None,
                    "process_id": process_id,
                    "process_symbol": symbol,
                    "process_name": process.get("process_name") or node.get("name"),
                    "process_type": node.get("process_type"),
                    "priority": process.get("priority"),
                    "status": process.get("computed_status")
                    or process.get("status")
                    or node.get("status"),
                    "planned_start_at": planned_start_at,
                    "planned_finish_at": planned_finish_at,
                    "schedule_window_starts_at": schedule_window_starts_at,
                    "schedule_window_ends_at": schedule_window_ends_at,
                    "schedule_buffer_hours": process.get("schedule_buffer_hours"),
                    "hours_until_planned_start": process.get(
                        "hours_until_planned_start"
                    ),
                    "hours_until_planned_finish": process.get(
                        "hours_until_planned_finish"
                    ),
                    "role_ids": process.get("role_ids") or ([role_id] if role_id else []),
                    "role_label": (
                        self._pm_context_role_label([role_id], role_names)
                        if role_id
                        else self._pm_markdown_list(process.get("role_ids") or [])
                    ),
                    "role_requirement_id": (
                        requirement.get("requirement_id") if requirement else None
                    ),
                    "effort_hours": process.get("effort_hours")
                    or (requirement.get("effort_hours") if requirement else None),
                    "mode": "pinned" if pin_history else "planned",
                    "active_pin": bool(process.get("active_pin"))
                    or self._pm_resource_has_active_pin(node, resource_id),
                    "pin_started_at": process.get("pin_started_at")
                    or self._pm_resource_pin_started_at(node, resource_id),
                    "pin_history": pin_history,
                    "assigned_to": self._pm_context_resource_label(
                        [resource_id],
                        resource_names,
                    ),
                    "assignment_certainty": assignment_certainty,
                    "ownership_evidence_state": ownership_evidence_state,
                    "message_caveat": self._pm_assignment_message_caveat(
                        assignment_certainty,
                        ownership_evidence_state,
                    ),
                    "blockers": blockers,
                    "done_definition": node.get("description") or None,
                    "predecessors": node.get("predecessors") or [],
                    "successors": node.get("successors") or [],
                    "max_makespan_sensitivity_hours": process.get(
                        "max_makespan_sensitivity_hours"
                    ),
                    "sensitivity_label": process.get("sensitivity_label"),
                    "last_modified_at": self._process_last_modified_at(
                        project_id,
                        str(process_id) if process_id else None,
                        as_of,
                    ),
                }
                row["process_content_hash"] = self._pm_process_info_hash(
                    resource_id,
                    row,
                )
                row["content_hash"] = row["process_content_hash"]
                row["message_artifact"] = self._pm_process_message_artifact(row)
                row["message_markdown"] = row["message_artifact"][
                    "message_markdown"
                ]
                row["message_blocks"] = row["message_artifact"]["message_blocks"]
                last_full = self._latest_matching_process_evidence(
                    evidence_rows,
                    PMCommunicationEvidenceType.PROCESS_FULL_UPDATE,
                    resource_id,
                    str(process_id) if process_id else None,
                    symbol,
                    str(row["process_content_hash"]),
                )
                row["last_full_update_at"] = (
                    last_full.communicated_at.isoformat()
                    if last_full is not None
                    else None
                )
                row["last_full_update_outbox_id"] = (
                    last_full.outbox_id if last_full is not None else None
                )
                process_rows.append(row)
            assignment_hash = self._pm_assignment_list_hash(resource_id, process_rows)
            assignment_artifact = self._pm_assignment_list_message_artifact(
                resource.get("resource_name") or resource_id,
                resource_id,
                process_rows,
                assignment_hash,
            )
            review_evidence = self._latest_matching_resource_evidence(
                evidence_rows,
                PMCommunicationEvidenceType.RESOURCE_ASSIGNMENT_REVIEW,
                resource_id,
                assignment_hash,
            )
            output.append(
                {
                    "resource_id": resource_id,
                    "resource_name": resource.get("resource_name") or resource_id,
                    "target_type": "dm" if mapping is not None else "unmapped_resource",
                    "slack_user_id": mapping.slack_user_id if mapping is not None else None,
                    "assignment_count": len(process_rows),
                    "assignment_content_hash": assignment_hash,
                    "content_hash": assignment_hash,
                    "message_artifact": assignment_artifact,
                    "message_markdown": assignment_artifact["message_markdown"],
                    "message_blocks": assignment_artifact["message_blocks"],
                    "last_assignment_review_at": (
                        review_evidence.communicated_at.isoformat()
                        if review_evidence is not None
                        else None
                    ),
                    "last_assignment_review_outbox_id": (
                        review_evidence.outbox_id
                        if review_evidence is not None
                        else None
                    ),
                    "assigned_processes": process_rows,
                }
            )
        return sorted(output, key=lambda row: str(row.get("resource_id") or ""))

    def _with_mapped_empty_resource_reviews(
        self,
        project_id: str,
        resource_processes: list[dict[str, object]],
        evidence_rows: list[PMCommunicationEvidenceRecord],
        active_slack_mappings: dict[str, SlackResourceMappingRecord],
    ) -> list[dict[str, object]]:
        existing = {str(row.get("resource_id")) for row in resource_processes}
        rows = list(resource_processes)
        for mapping in active_slack_mappings.values():
            if mapping.resource_id in existing:
                continue
            resource = self._repository.resources.get(mapping.resource_id, {})
            assignment_hash = self._pm_assignment_list_hash(mapping.resource_id, [])
            resource_name = resource.get("name") or mapping.display_name or (
                mapping.resource_id
            )
            assignment_artifact = self._pm_assignment_list_message_artifact(
                resource_name,
                mapping.resource_id,
                [],
                assignment_hash,
            )
            review_evidence = self._latest_matching_resource_evidence(
                evidence_rows,
                PMCommunicationEvidenceType.RESOURCE_ASSIGNMENT_REVIEW,
                mapping.resource_id,
                assignment_hash,
            )
            rows.append(
                {
                    "resource_id": mapping.resource_id,
                    "resource_name": resource_name,
                    "target_type": "dm",
                    "slack_user_id": mapping.slack_user_id,
                    "assignment_count": 0,
                    "assignment_content_hash": assignment_hash,
                    "content_hash": assignment_hash,
                    "message_artifact": assignment_artifact,
                    "message_markdown": assignment_artifact["message_markdown"],
                    "message_blocks": assignment_artifact["message_blocks"],
                    "last_assignment_review_at": (
                        review_evidence.communicated_at.isoformat()
                        if review_evidence is not None
                        else None
                    ),
                    "last_assignment_review_outbox_id": (
                        review_evidence.outbox_id
                        if review_evidence is not None
                        else None
                    ),
                    "assigned_processes": [],
                }
            )
        return sorted(rows, key=lambda row: str(row.get("resource_id") or ""))

    def _process_evidence_line_id(
        self,
        project_id: str,
        process_id: str,
        line_item: str,
    ) -> str:
        payload = json.dumps(
            {
                "project_id": project_id,
                "process_id": process_id,
                "line_item": line_item,
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"process-evidence-{digest}"

    def _resource_evidence_line_id(
        self,
        project_id: str,
        resource_id: str,
        line_item: str,
    ) -> str:
        payload = json.dumps(
            {
                "project_id": project_id,
                "resource_id": resource_id,
                "line_item": line_item,
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"resource-evidence-{digest}"

    def _process_role_pin_requirement(
        self,
        *,
        project_id: str,
        process_id: str,
        requirement_id: str | None,
        role_id: str,
        as_of: dt.datetime,
    ) -> tuple[str, str]:
        revision = self._repository.selected_revision_as_of(
            project_id,
            process_id,
            as_of,
        )
        if revision is None:
            raise ServiceValidationError(
                code="process_revision_not_found",
                message="Process pins require a process revision.",
                entity_id=process_id,
            )
        matches: list[tuple[str, str]] = []
        for index, requirement in enumerate(revision.role_requirements):
            candidate_id = requirement.requirement_id or f"{process_id}-requirement-{index + 1}"
            if requirement_id is not None and requirement_id != candidate_id:
                continue
            if requirement.role_id == role_id:
                matches.append((candidate_id, requirement.role_id))
        if not matches:
            raise ServiceValidationError(
                code="pin_requirement_not_found",
                message="Process-role pin must reference an existing process-role.",
                entity_id=process_id,
            )
        if requirement_id is None and len(matches) > 1:
            raise ServiceValidationError(
                code="ambiguous_pin_requirement",
                message=(
                    "requirement_id is required when a process has multiple "
                    "requirements for the same role."
                ),
                field_path="requirement_id",
            )
        return matches[0]

    def _process_last_modified_at(
        self,
        project_id: str,
        process_id: str | None,
        as_of: dt.datetime,
    ) -> str | None:
        if not process_id:
            return None
        timestamps = [
            revision.effective_at
            for revision in self._repository.revisions_by_process.get(process_id, [])
            if revision.project_id == project_id and revision.effective_at <= as_of
        ]
        latest_pin_at = self._latest_pin_modified_at(project_id, process_id, as_of)
        if latest_pin_at is not None:
            timestamps.append(latest_pin_at)
        for blocker in self._repository.list_blockers(project_id, include_resolved=True):
            if blocker.process_id != process_id:
                continue
            for timestamp in (
                blocker.created_at,
                blocker.opened_at,
                blocker.resolved_at,
            ):
                if timestamp is not None and timestamp <= as_of:
                    timestamps.append(timestamp)
        if not timestamps:
            return None
        return max(timestamps).isoformat()

    def _process_line_item_last_modified_at(
        self,
        project_id: str,
        process_id: str | None,
        line_item: str,
        as_of: dt.datetime,
    ) -> dt.datetime | None:
        if not process_id:
            return None
        if line_item == "blockers":
            return self._latest_blocker_modified_at(project_id, process_id, as_of)
        if line_item == "pin_data":
            return self._latest_pin_modified_at(project_id, process_id, as_of)
        if line_item == "plan_data":
            return self._latest_plan_data_modified_at(project_id, process_id, as_of)
        revision_timestamp = self._latest_line_item_revision_modified_at(
            project_id,
            process_id,
            line_item,
            as_of,
        )
        return revision_timestamp

    def _latest_line_item_revision_modified_at(
        self,
        project_id: str,
        process_id: str,
        line_item: str,
        as_of: dt.datetime,
    ) -> dt.datetime | None:
        field_name_by_line_item = {
            "role_requirements": "role_requirements",
            "done_criteria": "description",
            "plan_data": "plan_data",
            "planned_resources_uptodate_on_process": "planned_resources",
        }
        field_name = field_name_by_line_item.get(line_item)
        if field_name is None:
            return self._latest_revision_modified_at(project_id, process_id, as_of)
        revisions = sorted(
            (
                revision
                for revision in self._repository.revisions_by_process.get(
                    process_id,
                    [],
                )
                if revision.project_id == project_id and revision.effective_at <= as_of
            ),
            key=lambda revision: revision.effective_at,
        )
        latest_modified_at = None
        previous_value = object()
        for revision in revisions:
            value = self._revision_line_item_value(revision, field_name)
            if value != previous_value:
                latest_modified_at = revision.effective_at
                previous_value = value
        return latest_modified_at

    def _revision_line_item_value(
        self,
        revision: object,
        field_name: str,
    ) -> object:
        if field_name == "role_requirements":
            return self._json_ready(getattr(revision, "role_requirements", []))
        if field_name == "description":
            return getattr(revision, "description", "")
        if field_name == "planned_resources":
            return {
                "role_requirements": self._json_ready(
                    getattr(revision, "role_requirements", []),
                ),
            }
        if field_name == "plan_data":
            return {
                "dependencies": list(getattr(revision, "dependencies", []) or []),
                "role_requirements": self._json_ready(
                    getattr(revision, "role_requirements", []),
                ),
            }
        return getattr(revision, field_name, None)

    def _latest_plan_data_modified_at(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime,
    ) -> dt.datetime | None:
        timestamps = [
            timestamp
            for timestamp in [
                self._latest_line_item_revision_modified_at(
                    project_id,
                    process_id,
                    "plan_data",
                    as_of,
                )
            ]
            if timestamp is not None
        ]
        for other_process_id, revisions in self._repository.revisions_by_process.items():
            if other_process_id == process_id:
                continue
            prior_dependency_state: bool | None = None
            for revision in sorted(
                (
                    revision
                    for revision in revisions
                    if revision.project_id == project_id
                    and revision.effective_at <= as_of
                ),
                key=lambda revision: revision.effective_at,
            ):
                has_dependency = process_id in (
                    getattr(revision, "dependencies", []) or []
                )
                if prior_dependency_state is None:
                    if has_dependency:
                        timestamps.append(revision.effective_at)
                    prior_dependency_state = has_dependency
                    continue
                if has_dependency != prior_dependency_state:
                    timestamps.append(revision.effective_at)
                    prior_dependency_state = has_dependency
        return max(timestamps) if timestamps else None

    def _latest_revision_modified_at(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime,
    ) -> dt.datetime | None:
        timestamps = [
            revision.effective_at
            for revision in self._repository.revisions_by_process.get(process_id, [])
            if revision.project_id == project_id and revision.effective_at <= as_of
        ]
        return max(timestamps) if timestamps else None

    def _latest_blocker_modified_at(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime,
    ) -> dt.datetime | None:
        timestamps = []
        for blocker in self._repository.list_blockers(project_id, include_resolved=True):
            if blocker.process_id != process_id:
                continue
            for timestamp in (
                blocker.created_at,
                blocker.opened_at,
                blocker.resolved_at,
            ):
                if timestamp is not None and timestamp <= as_of:
                    timestamps.append(timestamp)
        return max(timestamps) if timestamps else None

    def _latest_pin_modified_at(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime,
    ) -> dt.datetime | None:
        timestamps = []
        for pin in self._repository.list_process_role_pins(
            project_id,
            as_of=as_of,
            process_id=process_id,
            include_done=True,
        ):
            for timestamp in (
                pin.created_at,
                pin.updated_at,
                pin.pinned_at,
                pin.forecast_finish_at,
                pin.verified_done_at,
            ):
                if timestamp is not None and timestamp <= as_of:
                    timestamps.append(timestamp)
        return max(timestamps) if timestamps else None

    def _pm_assignment_list_hash(
        self,
        resource_id: str,
        process_rows: list[dict[str, object]],
    ) -> str:
        payload = {
            "resource_id": resource_id,
            "assigned_processes": [
                {
                    "process_symbol": row.get("process_symbol"),
                    "status": row.get("status"),
                    "planned_start_at": row.get("planned_start_at"),
                    "planned_finish_at": row.get("planned_finish_at"),
                    "role_ids": row.get("role_ids"),
                    "done_definition": row.get("done_definition"),
                    "last_modified_at": row.get("last_modified_at"),
                }
                for row in process_rows
            ],
        }
        return "sha256:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8"),
        ).hexdigest()

    def _pm_process_info_hash(
        self,
        resource_id: str,
        process_row: dict[str, object],
    ) -> str:
        # This hash is the audit contract for "all information regarding this
        # process." Schedule shifts, status changes, role changes, or done
        # definition edits produce a new hash so old notices stop satisfying
        # future protocol obligations.
        payload = {
            "resource_id": resource_id,
            "process_id": process_row.get("process_id"),
            "process_symbol": process_row.get("process_symbol"),
            "process_name": process_row.get("process_name"),
            "process_type": process_row.get("process_type"),
            "priority": process_row.get("priority"),
            "mode": process_row.get("mode"),
            "status": process_row.get("status"),
            "planned_start_at": process_row.get("planned_start_at"),
            "planned_finish_at": process_row.get("planned_finish_at"),
            "assigned_to": process_row.get("assigned_to"),
            "role_ids": process_row.get("role_ids"),
            "role_label": process_row.get("role_label"),
            "role_requirement_id": process_row.get("role_requirement_id"),
            "effort_hours": process_row.get("effort_hours"),
            "pin_history": process_row.get("pin_history"),
            "predecessors": process_row.get("predecessors"),
            "successors": process_row.get("successors"),
            "blockers": process_row.get("blockers"),
            "done_definition": process_row.get("done_definition"),
            "last_modified_at": process_row.get("last_modified_at"),
        }
        return "sha256:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8"),
        ).hexdigest()

    def _pm_assignment_list_message_artifact(
        self,
        resource_name: str,
        resource_id: str,
        process_rows: list[dict[str, object]],
        content_hash: str,
    ) -> dict[str, object]:
        markdown = self._pm_assignment_list_markdown(
            resource_name,
            resource_id,
            process_rows,
        )
        blocks = self._pm_assignment_list_blocks(
            resource_name,
            resource_id,
            process_rows,
        )
        return {
            "artifact_kind": "resource_assignment_list",
            "rendered_by": "query_pm_communication_protocol",
            "content_hash": content_hash,
            "message_markdown": markdown,
            "message_blocks": blocks,
            "required_visible_text": self._pm_blocks_visible_text(blocks),
        }

    def _pm_process_message_artifact(
        self,
        process_row: dict[str, object],
    ) -> dict[str, object]:
        markdown = self._pm_process_message_markdown(process_row)
        blocks = self._pm_process_message_blocks(process_row)
        return {
            "artifact_kind": "process_full_update",
            "rendered_by": "query_pm_communication_protocol",
            "content_hash": process_row.get("process_content_hash"),
            "message_markdown": markdown,
            "message_blocks": blocks,
            "required_visible_text": self._pm_blocks_visible_text(blocks),
        }

    def _pm_assignment_list_markdown(
        self,
        resource_name: str,
        resource_id: str,
        process_rows: list[dict[str, object]],
    ) -> str:
        if not process_rows:
            return (
                f"Current process work list for {resource_name} "
                f"(`{resource_id}`): no current or upcoming process work."
            )
        lines = [f"Current process work list for {resource_name} (`{resource_id}`):"]
        for title, group in self._pm_assignment_process_groups(process_rows):
            if not group:
                continue
            lines.append("")
            lines.append(f"{title}:")
            for index, process in enumerate(group, start=1):
                rendered = self._pm_assignment_process_markdown(process, index)
                lines.extend(f"  {line}" for line in rendered.splitlines())
        return "\n".join(lines)

    def _pm_assignment_list_blocks(
        self,
        resource_name: str,
        resource_id: str,
        process_rows: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        blocks: list[dict[str, object]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"Tasks for {resource_name}"[:150],
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Current process work list for `{resource_id}`",
                    }
                ],
            },
        ]
        if not process_rows:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "No current or upcoming process work.",
                    },
                }
            )
            return blocks
        for title, group in self._pm_assignment_process_groups(process_rows):
            if not group:
                continue
            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{title}*"},
                }
            )
            for index, process in enumerate(group, start=1):
                text = self._pm_assignment_process_markdown(process, index)
                for chunk in self._pm_chunk_slack_text(text, 2800):
                    blocks.append(
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": chunk},
                        }
                    )
                    if len(blocks) >= 50:
                        return blocks
        return blocks

    def _pm_assignment_process_groups(
        self,
        process_rows: list[dict[str, object]],
    ) -> list[tuple[str, list[dict[str, object]]]]:
        grouped: dict[str, list[dict[str, object]]] = {
            "Pinned": [],
            "Needs attention": [],
            "Upcoming": [],
            "Later": [],
        }
        for process in process_rows:
            if process.get("active_pin"):
                grouped["Pinned"].append(process)
            elif self._pm_assignment_needs_attention(process):
                grouped["Needs attention"].append(process)
            elif self._pm_assignment_is_upcoming(process):
                grouped["Upcoming"].append(process)
            else:
                grouped["Later"].append(process)
        return [(title, grouped[title]) for title in grouped]

    def _pm_assignment_needs_attention(
        self,
        process: dict[str, object],
    ) -> bool:
        if process.get("blockers"):
            return True
        if str(process.get("computed_status") or process.get("status") or "") in {
            "early_start",
            "started",
            "paused",
        }:
            return True
        finish_hours = process.get("hours_until_planned_finish")
        try:
            return finish_hours is not None and float(finish_hours) <= 0
        except (TypeError, ValueError):
            return False

    def _pm_assignment_is_upcoming(self, process: dict[str, object]) -> bool:
        start_hours = process.get("hours_until_planned_start")
        try:
            return start_hours is not None and 0 <= float(start_hours) <= 72
        except (TypeError, ValueError):
            return False

    def _pm_assignment_process_markdown(
        self,
        process: dict[str, object],
        index: int,
    ) -> str:
        symbol = process.get("process_symbol") or process.get("process_id") or "-"
        name = process.get("process_name") or symbol
        lines = [f"{index}. *{symbol} - {name}*"]
        lines.extend(self._pm_process_detail_lines(process))
        return "\n".join(lines)

    def _pm_process_message_markdown(self, process: dict[str, object]) -> str:
        symbol = process.get("process_symbol") or process.get("process_id") or "-"
        name = process.get("process_name") or symbol
        lines = [f"*{symbol} - {name}*"]
        lines.extend(self._pm_process_detail_lines(process))
        return "\n".join(lines)

    def _pm_process_message_blocks(
        self,
        process: dict[str, object],
    ) -> list[dict[str, object]]:
        title = str(
            process.get("process_name")
            or process.get("process_symbol")
            or "Process update"
        )[:150]
        blocks: list[dict[str, object]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": title},
            }
        ]
        for chunk in self._pm_chunk_slack_text(
            self._pm_process_message_markdown(process),
            2800,
        ):
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": chunk},
                }
            )
        return blocks

    def _pm_process_detail_lines(self, process: dict[str, object]) -> list[str]:
        mode = str(
            process.get("mode")
            or ("pinned" if process.get("active_pin") else "planned")
        )
        role_line = str(
            process.get("role_label")
            or self._pm_markdown_list(process.get("role_ids"))
        )
        requirement_id = process.get("role_requirement_id")
        if requirement_id:
            role_line = f"{role_line} | {requirement_id}"
        lines = [
            f"- Type: {self._pm_process_type_label(process.get('process_type'))}",
            f"- Mode: {mode}",
        ]
        status = process.get("status")
        if status:
            lines.append(f"- Status: `{status}`")
        else:
            lines.append("- Status: `unknown`")
        lines.extend(
            [
                f"- Role requirement: {role_line}",
                f"- Effort hours: {self._pm_format_hours(process.get('effort_hours'))}",
                (
                    "- Definition: "
                    f"{process.get('done_definition') or 'needs confirmation'}"
                ),
                (
                    "- Parents: "
                    f"{self._pm_context_braced_symbols(process.get('predecessors'))}"
                ),
                (
                    "- Children: "
                    f"{self._pm_context_braced_symbols(process.get('successors'))}"
                ),
            ]
        )
        if mode == "pinned":
            lines.extend(self._pm_process_pinned_detail_lines(process))
        else:
            lines.extend(
                [
                    f"- Assigned to: {process.get('assigned_to') or '-'}",
                    (
                        "- Planned start: "
                        f"{self._pm_human_datetime(process.get('planned_start_at'))}"
                    ),
                    (
                        "- Planned finish: "
                        f"{self._pm_human_datetime(process.get('planned_finish_at'))}"
                    ),
                    f"- {self._pm_schedule_window_line(process)}",
                ]
            )
        return lines

    def _pm_process_pinned_detail_lines(
        self,
        process: dict[str, object],
    ) -> list[str]:
        pin_history = [
            pin
            for pin in process.get("pin_history") or []
            if isinstance(pin, dict)
        ]
        resource_ids = [
            str(pin.get("resource_id"))
            for pin in pin_history
            if pin.get("resource_id")
        ]
        pinned_starts = [
            parsed
            for parsed in (
                self._parse_datetime(pin.get("pinned_at") or pin.get("starts_at"))
                for pin in pin_history
            )
            if parsed is not None
        ]
        verified_finishes = [
            parsed
            for parsed in (
                self._parse_datetime(
                    pin.get("verified_finished_at")
                    or pin.get("verified_done_at")
                    or pin.get("ends_at")
                )
                for pin in pin_history
            )
            if parsed is not None
        ]
        forecast_finishes = [
            parsed
            for parsed in (
                self._parse_datetime(pin.get("forecast_finish_at"))
                for pin in pin_history
            )
            if parsed is not None
        ]
        pinned_to = process.get("assigned_to")
        if resource_ids:
            unique_resource_ids = sorted(dict.fromkeys(resource_ids))
            if len(unique_resource_ids) == 1 and process.get("assigned_to"):
                pinned_to = process.get("assigned_to")
            else:
                pinned_to = ", ".join(unique_resource_ids)
        pinned_started_at = (
            min(pinned_starts) if pinned_starts else process.get("pin_started_at")
        )
        lines = [
            f"- Pinned to: {pinned_to or '-'}",
            (
                "- Pinned started: "
                f"{self._pm_human_datetime(pinned_started_at)}"
            ),
        ]
        has_unverified = any(
            not (
                pin.get("verified_finished_at")
                or pin.get("verified_done_at")
                or pin.get("ends_at")
            )
            for pin in pin_history
        )
        if has_unverified or not verified_finishes:
            forecast_finish_at = (
                max(forecast_finishes)
                if forecast_finishes
                else process.get("pin_forecast_finish_at")
            )
            lines.append(
                "- Forecasted finish: "
                f"{self._pm_human_datetime(forecast_finish_at)}"
            )
        else:
            lines.append(
                "- Verified finish: "
                f"{self._pm_human_datetime(max(verified_finishes))}"
            )
        return lines

    def _pm_human_datetime(self, value: object) -> str:
        if value in (None, ""):
            return "-"
        parsed = self._parse_datetime(value)
        if parsed is None:
            return str(value)
        return parsed.strftime("%Y-%m-%d %H:%M %Z")

    def _pm_schedule_window_line(self, process: dict[str, object]) -> str:
        start_buffer_hours = self._pm_duration_hours(
            process.get("schedule_window_starts_at"),
            process.get("planned_start_at"),
        )
        duration_hours = self._pm_duration_hours(
            process.get("planned_start_at"),
            process.get("planned_finish_at"),
        )
        finish_buffer_hours = self._pm_duration_hours(
            process.get("planned_finish_at"),
            process.get("schedule_window_ends_at"),
        )
        return (
            f"{self._pm_format_days(start_buffer_hours)} pre-buffer | "
            f"{self._pm_format_days(duration_hours)} duration | "
            f"{self._pm_format_days(finish_buffer_hours)} post-buffer"
        )

    def _pm_duration_hours(self, starts_at: object, ends_at: object) -> float | None:
        start = self._parse_datetime(starts_at)
        end = self._parse_datetime(ends_at)
        if start is None or end is None:
            return None
        return (end - start).total_seconds() / 3600

    def _pm_format_days(self, hours: float | None) -> str:
        if hours is None:
            return "-"
        days = hours / 24
        return f"{self._clean_number(round(days, 2))} days"

    def _pm_format_hours(self, value: object) -> str:
        try:
            hours = float(value) if value is not None else None
        except (TypeError, ValueError):
            hours = None
        if hours is None:
            return "-"
        unit = "hour" if abs(hours) == 1 else "hours"
        return f"{self._clean_number(hours)} {unit}"

    def _pm_markdown_datetime(self, value: object) -> str:
        if value in (None, ""):
            return "-"
        parsed = self._parse_datetime(value)
        if parsed is None:
            return str(value)
        return parsed.isoformat()

    def _pm_markdown_datetime_with_delta(
        self,
        value: object,
        relative_to: dt.datetime,
    ) -> str:
        if value in (None, ""):
            return "-"
        parsed = self._parse_datetime(value)
        if parsed is None:
            return str(value)
        return f"{parsed.isoformat()} ({self._pm_relative_age(relative_to, parsed)})"

    def _pm_actual_datetime(self, value: object, missing_label: str) -> str:
        if value in (None, ""):
            return missing_label
        return self._pm_markdown_datetime(value)

    def _pm_markdown_list(self, value: object) -> str:
        if value in (None, "", []):
            return "-"
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, (list, tuple, set)):
            parts = [str(part) for part in value if part]
        else:
            parts = [str(value)]
        return ", ".join(f"`{part}`" for part in parts) if parts else "-"

    def _pm_resource_pin_history(
        self,
        node: dict[str, object],
        resource_id: str,
    ) -> list[dict[str, object]]:
        pins = []
        for requirement in node.get("role_requirements") or []:
            if not isinstance(requirement, dict):
                continue
            role_id = requirement.get("role_id")
            for pin in requirement.get("pins", []) or []:
                if not isinstance(pin, dict):
                    continue
                if pin.get("resource_id") != resource_id:
                    continue
                pins.append(
                    {
                        **pin,
                        "role_id": pin.get("role_id") or role_id,
                    }
                )
        return sorted(pins, key=lambda item: str(item.get("pinned_at") or ""))

    def _pm_resource_has_active_pin(
        self,
        node: dict[str, object],
        resource_id: str,
    ) -> bool:
        return any(
            pin.get("status") == "pinned_started"
            for pin in self._pm_resource_pin_history(node, resource_id)
        )

    def _pm_resource_pin_started_at(
        self,
        node: dict[str, object],
        resource_id: str,
    ) -> object | None:
        active_starts = [
            pin.get("pinned_at")
            for pin in self._pm_resource_pin_history(node, resource_id)
            if pin.get("status") == "pinned_started"
        ]
        return min(active_starts) if active_starts else None

    def _pm_resource_assignment_certainty(
        self,
        node: dict[str, object],
        resource_id: str,
    ) -> str:
        pin_history = self._pm_resource_pin_history(node, resource_id)
        if any(pin.get("status") == "pinned_started" for pin in pin_history):
            return "confirmed_active_pin"
        if pin_history:
            return "confirmed_pin"
        return "scheduled_role_allocation_unconfirmed"

    def _pm_resource_ownership_evidence_state(
        self,
        node: dict[str, object],
        resource_id: str,
    ) -> str:
        if self._pm_resource_assignment_certainty(
            node,
            resource_id,
        ) != "scheduled_role_allocation_unconfirmed":
            return "confirmed_by_pin"
        return "needs_owner_confirmation"

    def _pm_assignment_message_caveat(
        self,
        assignment_certainty: str,
        ownership_evidence_state: str,
    ) -> str | None:
        if (
            assignment_certainty == "scheduled_role_allocation_unconfirmed"
            or ownership_evidence_state == "needs_owner_confirmation"
        ):
            return (
                "This is planned role work and needs owner confirmation before "
                "it is described as accepted ownership."
            )
        return None

    def _pm_chunk_slack_text(
        self,
        text: str,
        max_chars: int = 2900,
    ) -> list[str]:
        remaining = text.strip()
        chunks: list[str] = []
        while len(remaining) > max_chars:
            split_at = remaining.rfind("\n", 0, max_chars)
            if split_at < max_chars // 2:
                split_at = remaining.rfind(" ", 0, max_chars)
            if split_at < max_chars // 2:
                split_at = max_chars
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        if remaining:
            chunks.append(remaining)
        return chunks

    def _pm_blocks_visible_text(self, blocks: list[dict[str, object]]) -> str:
        texts: list[str] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"text", "alt_text", "title"} and isinstance(item, str):
                        texts.append(item)
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(blocks)
        return "\n\n".join(text.strip() for text in texts if text.strip())

    def _latest_matching_process_evidence(
        self,
        evidence_rows: list[PMCommunicationEvidenceRecord],
        evidence_type: PMCommunicationEvidenceType,
        resource_id: str,
        process_id: str | None,
        process_symbol: str | None,
        content_hash: str | None,
    ) -> PMCommunicationEvidenceRecord | None:
        process_keys = {key for key in (process_id, process_symbol) if key is not None}
        for evidence in sorted(
            evidence_rows,
            key=lambda row: (row.communicated_at, row.evidence_id),
            reverse=True,
        ):
            if evidence.evidence_type != evidence_type:
                continue
            if evidence.resource_id != resource_id:
                continue
            if (evidence.process_id or evidence.process_symbol) not in process_keys:
                continue
            if content_hash and evidence.content_hash != content_hash:
                continue
            return evidence
        return None

    def _latest_matching_resource_evidence(
        self,
        evidence_rows: list[PMCommunicationEvidenceRecord],
        evidence_type: PMCommunicationEvidenceType,
        resource_id: str,
        content_hash: str | None,
    ) -> PMCommunicationEvidenceRecord | None:
        for evidence in sorted(
            evidence_rows,
            key=lambda row: (row.communicated_at, row.evidence_id),
            reverse=True,
        ):
            if evidence.evidence_type != evidence_type:
                continue
            if evidence.resource_id != resource_id:
                continue
            if evidence.process_id is not None or evidence.process_symbol is not None:
                continue
            if content_hash and evidence.content_hash != content_hash:
                continue
            return evidence
        return None

    def _pm_protocol_obligations(
        self,
        resource_processes: list[dict[str, object]],
        evidence_rows: list[PMCommunicationEvidenceRecord],
        now: dt.datetime,
    ) -> list[dict[str, object]]:
        obligations: list[dict[str, object]] = []
        for resource in resource_processes:
            resource_id = str(resource.get("resource_id") or "")
            if resource.get("target_type") != "dm" or not resource.get("slack_user_id"):
                continue
            assignment_hash = str(resource.get("assignment_content_hash") or "")
            last_review = self._latest_matching_resource_evidence(
                evidence_rows,
                PMCommunicationEvidenceType.RESOURCE_ASSIGNMENT_REVIEW,
                resource_id,
                assignment_hash,
            )
            review_due = (
                last_review is None
                or (now - last_review.communicated_at) >= dt.timedelta(days=14)
                or (
                    last_review.content_hash is not None
                    and last_review.content_hash != assignment_hash
                )
            )
            obligations.append(
                self._pm_obligation_row(
                    evidence_type=PMCommunicationEvidenceType.RESOURCE_ASSIGNMENT_REVIEW,
                    resource_id=resource_id,
                    process_id=None,
                    process_symbol=None,
                    due=review_due,
                    due_reason=(
                        "Full assigned-process list has not been reviewed in the "
                        "last 14 days or the assignment list changed."
                    ),
                    now=now,
                    content_hash=assignment_hash,
                    required_evidence_types=[
                        PMCommunicationEvidenceType.RESOURCE_ASSIGNMENT_REVIEW.value,
                    ],
                    last_evidence=last_review,
                    target_type=str(resource["target_type"]),
                    slack_user_id=str(resource["slack_user_id"]),
                    slack_channel_id=None,
                    message_artifact=resource.get("message_artifact"),
                )
            )
            for process in resource.get("assigned_processes") or []:
                if not isinstance(process, dict):
                    continue
                obligations.extend(
                    self._pm_process_obligations(
                        resource_id,
                        process,
                        evidence_rows,
                        now,
                        target_type=str(resource["target_type"]),
                        slack_user_id=str(resource["slack_user_id"]),
                        message_artifact=process.get("message_artifact"),
                    )
                )
        return obligations

    def _pm_process_obligations(
        self,
        resource_id: str,
        process: dict[str, object],
        evidence_rows: list[PMCommunicationEvidenceRecord],
        now: dt.datetime,
        target_type: str,
        slack_user_id: str,
        message_artifact: object | None = None,
    ) -> list[dict[str, object]]:
        process_id = (
            str(process.get("process_id")) if process.get("process_id") else None
        )
        process_symbol = (
            str(process.get("process_symbol"))
            if process.get("process_symbol")
            else None
        )
        process_key = process_id or process_symbol
        if process_key is None:
            return []
        last_modified_at = self._parse_datetime(process.get("last_modified_at"))
        planned_start_at = self._parse_datetime(process.get("planned_start_at"))
        planned_finish_at = self._parse_datetime(process.get("planned_finish_at"))
        status = str(process.get("status") or "")
        done = status in {"done", "complete", "canceled", "cancelled"}
        process_content_hash = str(process.get("process_content_hash") or "")
        obligations: list[dict[str, object]] = []

        def latest_for(
            evidence_type: PMCommunicationEvidenceType,
        ) -> PMCommunicationEvidenceRecord | None:
            return self._latest_matching_process_evidence(
                evidence_rows,
                evidence_type,
                resource_id,
                process_id,
                process_symbol,
                process_content_hash,
            )

        def has_required_pair(
            cadence_type: PMCommunicationEvidenceType,
        ) -> PMCommunicationEvidenceRecord | None:
            # Cadence evidence proves that a check-in happened; process_full_update
            # proves the teammate received the full process facts. Both must match
            # the current process hash before an obligation is satisfied.
            cadence_evidence = latest_for(cadence_type)
            full_update = latest_for(PMCommunicationEvidenceType.PROCESS_FULL_UPDATE)
            if cadence_evidence is None or full_update is None:
                return None
            if (
                last_modified_at is not None
                and (
                    cadence_evidence.communicated_at < last_modified_at
                    or full_update.communicated_at < last_modified_at
                )
            ):
                return None
            return cadence_evidence

        if planned_start_at is not None and now < planned_start_at:
            three_day = has_required_pair(
                PMCommunicationEvidenceType.PROCESS_PRE_START_3_DAY,
            )
            obligations.append(
                self._pm_obligation_row(
                    evidence_type=PMCommunicationEvidenceType.PROCESS_PRE_START_3_DAY,
                    resource_id=resource_id,
                    process_id=process_id,
                    process_symbol=process_symbol,
                    due=(planned_start_at - now) <= dt.timedelta(days=3)
                    and three_day is None,
                    due_reason="Planned start is within 3 days and needs full process information.",
                    now=now,
                    required_evidence_types=[
                        PMCommunicationEvidenceType.PROCESS_PRE_START_3_DAY.value,
                        PMCommunicationEvidenceType.PROCESS_FULL_UPDATE.value,
                    ],
                    last_evidence=three_day,
                    content_hash=process_content_hash,
                    target_type=target_type,
                    slack_user_id=slack_user_id,
                    slack_channel_id=None,
                    message_artifact=message_artifact,
                )
            )
            one_day = has_required_pair(
                PMCommunicationEvidenceType.PROCESS_PRE_START_24_HOUR,
            )
            obligations.append(
                self._pm_obligation_row(
                    evidence_type=PMCommunicationEvidenceType.PROCESS_PRE_START_24_HOUR,
                    resource_id=resource_id,
                    process_id=process_id,
                    process_symbol=process_symbol,
                    due=(planned_start_at - now) <= dt.timedelta(days=1)
                    and one_day is None,
                    due_reason=(
                        "Planned start is within 24 hours and needs full process "
                        "information."
                    ),
                    now=now,
                    required_evidence_types=[
                        PMCommunicationEvidenceType.PROCESS_PRE_START_24_HOUR.value,
                        PMCommunicationEvidenceType.PROCESS_FULL_UPDATE.value,
                    ],
                    last_evidence=one_day,
                    content_hash=process_content_hash,
                    target_type=target_type,
                    slack_user_id=slack_user_id,
                    slack_channel_id=None,
                    message_artifact=message_artifact,
                )
            )
        if planned_finish_at is not None and now > planned_finish_at and not done:
            overdue = has_required_pair(
                PMCommunicationEvidenceType.PROCESS_OVERDUE_CHECKIN,
            )
            obligations.append(
                self._pm_obligation_row(
                    evidence_type=PMCommunicationEvidenceType.PROCESS_OVERDUE_CHECKIN,
                    resource_id=resource_id,
                    process_id=process_id,
                    process_symbol=process_symbol,
                    due=overdue is None
                    or (now - overdue.communicated_at) >= dt.timedelta(hours=24),
                    due_reason=(
                        "Process is past planned finish and needs a 24-hour "
                        "check-in cadence."
                    ),
                    now=now,
                    required_evidence_types=[
                        PMCommunicationEvidenceType.PROCESS_OVERDUE_CHECKIN.value,
                        PMCommunicationEvidenceType.PROCESS_FULL_UPDATE.value,
                    ],
                    last_evidence=overdue,
                    content_hash=process_content_hash,
                    target_type=target_type,
                    slack_user_id=slack_user_id,
                    slack_channel_id=None,
                    message_artifact=message_artifact,
                )
            )
        if status in {"started", "due", "early_start"} and not done:
            in_progress = has_required_pair(
                PMCommunicationEvidenceType.PROCESS_IN_PROGRESS_CHECKIN,
            )
            obligations.append(
                self._pm_obligation_row(
                    evidence_type=PMCommunicationEvidenceType.PROCESS_IN_PROGRESS_CHECKIN,
                    resource_id=resource_id,
                    process_id=process_id,
                    process_symbol=process_symbol,
                    due=in_progress is None
                    or (now - in_progress.communicated_at) >= dt.timedelta(days=3),
                    due_reason="Process is in progress and needs a 3-day check-in cadence.",
                    now=now,
                    required_evidence_types=[
                        PMCommunicationEvidenceType.PROCESS_IN_PROGRESS_CHECKIN.value,
                        PMCommunicationEvidenceType.PROCESS_FULL_UPDATE.value,
                    ],
                    last_evidence=in_progress,
                    content_hash=process_content_hash,
                    target_type=target_type,
                    slack_user_id=slack_user_id,
                    slack_channel_id=None,
                    message_artifact=message_artifact,
                )
            )
        return obligations

    def _pm_obligation_row(
        self,
        *,
        evidence_type: PMCommunicationEvidenceType,
        resource_id: str,
        process_id: str | None,
        process_symbol: str | None,
        due: bool,
        due_reason: str,
        now: dt.datetime,
        required_evidence_types: list[str],
        last_evidence: PMCommunicationEvidenceRecord | None,
        content_hash: str | None = None,
        target_type: str | None = None,
        slack_user_id: str | None = None,
        slack_channel_id: str | None = None,
        message_artifact: object | None = None,
    ) -> dict[str, object]:
        key_parts = [evidence_type.value, resource_id]
        if process_id or process_symbol:
            key_parts.append(str(process_id or process_symbol))
        obligation_id = ":".join(key_parts)
        return {
            "obligation_id": obligation_id,
            "evidence_type": evidence_type.value,
            "resource_id": resource_id,
            "process_id": process_id,
            "process_symbol": process_symbol,
            "status": "due_now" if due else "satisfied_or_not_due",
            "due": due,
            "due_reason": due_reason,
            "evaluated_at": now.isoformat(),
            "required_evidence_types": required_evidence_types,
            "content_hash": content_hash,
            "target_type": target_type,
            "slack_user_id": slack_user_id,
            "slack_channel_id": slack_channel_id,
            "message_artifact": message_artifact,
            "last_evidence_at": (
                last_evidence.communicated_at.isoformat()
                if last_evidence is not None
                else None
            ),
            "last_evidence_outbox_id": (
                last_evidence.outbox_id if last_evidence is not None else None
            ),
        }

    def _blocker_data(self, query: QueryBlockers) -> dict[str, object]:
        list_as_of = getattr(self._repository, "list_blockers_as_of", None)
        blockers = (
            list_as_of(query.project_id, query.as_of, query.include_resolved)
            if list_as_of is not None
            else self._repository.list_blockers(
                query.project_id,
                query.include_resolved,
            )
        )
        processes = getattr(self._repository, "processes", {})
        active_ids = set(
            self._repository.active_process_ids_as_of(query.project_id, query.as_of)
            if hasattr(self._repository, "active_process_ids_as_of")
            else []
        )
        filter_ids = set(query.process_ids or [])
        if query.process_symbols:
            filter_ids.update(
                self._resolve_process_id(
                    project_id=query.project_id,
                    process_id=None,
                    process_symbol=symbol,
                )
                for symbol in query.process_symbols
            )
        rows = []
        blocked_process_ids = []
        for blocker in blockers:
            if filter_ids and blocker.process_id not in filter_ids:
                continue
            blocker_symbol = self._blocker_resolver_symbol(blocker.blocker_id)
            created_at = blocker.created_at or blocker.opened_at
            resolved_at = blocker.resolved_at
            is_resolved = resolved_at is not None and resolved_at <= query.as_of
            process = processes.get(blocker.process_id)
            is_blocking = (
                not is_resolved
                and created_at <= query.as_of
                and blocker.process_id in active_ids
            )
            need_context = self._blocker_need_context(
                project_id=query.project_id,
                as_of=query.as_of,
                process_id=blocker.process_id,
                active_ids=active_ids,
            )
            rows.append(
                {
                    "blocker_id": blocker.blocker_id,
                    "blocker_symbol": blocker_symbol,
                    "resolver_process_symbol": blocker_symbol,
                    "project_id": blocker.project_id,
                    "process_id": blocker.process_id,
                    "process_symbol": getattr(process, "symbol", blocker.process_id),
                    "summary": blocker.summary or blocker.description,
                    "details": blocker.details,
                    "severity": "blocking",
                    "created_at": created_at.isoformat(),
                    "resolved_at": resolved_at.isoformat() if resolved_at else None,
                    "resolution": blocker.resolution,
                    "resolution_owner_resource_id": (
                        blocker.resolution_owner_resource_id
                    ),
                    "is_resolved_as_of": is_resolved,
                    "is_blocking_as_of": is_blocking,
                    **need_context,
                }
            )
            if is_blocking:
                blocked_process_ids.append(blocker.process_id)
        return {
            "project_id": query.project_id,
            "as_of": query.as_of.isoformat(),
            "blockers": rows,
            "blocked_process_ids": list(dict.fromkeys(blocked_process_ids)),
        }

    def _blocker_need_context(
        self,
        *,
        project_id: str,
        as_of: dt.datetime,
        process_id: str,
        active_ids: set[str],
    ) -> dict[str, object]:
        """Derive immediate process, role, and resource demand for a blocker."""
        successors: list[str] = []
        active_graph = getattr(self._repository, "_active_dependency_graph", None)
        if callable(active_graph):
            graph = active_graph(project_id, as_of)
            if process_id in graph:
                successors = sorted(str(item) for item in graph.successors(process_id))

        process_ids = [process_id, *successors]
        processes = getattr(self._repository, "processes", {})
        latest_revision = getattr(self._repository, "_latest_revision_as_of", None)
        role_ids: set[str] = set()
        resource_ids: set[str] = set()
        impacted_processes = []
        for impacted_id in process_ids:
            if impacted_id not in active_ids:
                continue
            process = processes.get(impacted_id)
            if process is None:
                continue
            revision = (
                latest_revision(impacted_id, as_of)
                if callable(latest_revision)
                else None
            )
            impacted_processes.append(
                {
                    "process_id": impacted_id,
                    "process_symbol": getattr(process, "symbol", impacted_id),
                    "name": getattr(revision, "name", None),
                    "status": self._process_completedness_facts(
                        project_id,
                        impacted_id,
                        as_of,
                    )["status"],
                    "relationship": (
                        "blocked_process"
                        if impacted_id == process_id
                        else "immediate_successor"
                    ),
                }
            )
            if revision is None:
                continue
            for requirement in getattr(revision, "role_requirements", []) or []:
                role_id = getattr(requirement, "role_id", None)
                if role_id:
                    role_ids.add(str(role_id))
            for role_id in getattr(revision, "required_roles", {}) or {}:
                role_ids.add(str(role_id))
            for pin in self._repository.list_process_role_pins(
                project_id,
                as_of=as_of,
                process_id=impacted_id,
                include_done=False,
            ):
                resource_ids.add(pin.resource_id)

        return {
            "immediate_blocked_processes": impacted_processes,
            "needed_by_role_ids": sorted(role_ids),
            "needed_by_resource_ids": sorted(
                resource_ids
                | self._resource_ids_for_roles(project_id, role_ids)
            ),
        }

    def _resource_ids_for_roles(
        self,
        project_id: str,
        role_ids: set[str],
    ) -> set[str]:
        resources = getattr(self._repository, "resources", {})
        output: set[str] = set()
        for resource in resources.values():
            if resource.get("project_id") != project_id:
                continue
            if resource.get("active", True) is False:
                continue
            resource_roles = {str(role_id) for role_id in resource.get("role_ids", [])}
            if resource_roles.intersection(role_ids):
                output.add(str(resource["resource_id"]))
        return output

    def _schedule_snapshot_data(
        self,
        query: QueryScheduleSnapshots,
    ) -> dict[str, object]:
        terminal_process_symbols = query.terminal_process_symbols
        milestone = None
        if query.milestone_id is not None:
            milestone = self._milestone_by_id(query.project_id, query.milestone_id)
            terminal_process_symbols = milestone.process_symbols
        snapshots = self._repository.schedule_snapshots_as_of(
            query.project_id,
            query.as_of,
            terminal_process_symbols,
        )
        return {
            "project_id": query.project_id,
            "as_of": query.as_of.isoformat(),
            "milestone": milestone.model_dump(mode="json") if milestone else None,
            "terminal_process_symbols": terminal_process_symbols,
            "snapshots": [
                snapshot.model_dump(mode="json")
                for snapshot in snapshots
            ],
        }

    def _agent_context_data(
        self,
        query: QueryAgentContext,
    ) -> tuple[dict[str, object], list[Warning]]:
        context_scope = self._agent_context_scope(query)
        graph_query = QueryProcessGraph(
            project_id=query.project_id,
            as_of=query.as_of,
            now=query.now,
            scope=context_scope,
            include_resource_fields=True,
            include_allocation_slices=True,
            planning_granularity=query.planning_granularity,
            max_iterations=query.max_iterations,
            convergence_tolerance_hours=query.convergence_tolerance_hours,
            resource_schedule_backend=query.resource_schedule_backend,
            resource_schedule_mcts_c_puct=query.resource_schedule_mcts_c_puct,
            resource_schedule_mcts_max_actions=query.resource_schedule_mcts_max_actions,
            include_resource_sensitivity=query.include_resource_sensitivity,
            resource_schedule_sensitivity_backend=(
                query.resource_schedule_sensitivity_backend
            ),
            resource_schedule_sensitivity_workers=(
                query.resource_schedule_sensitivity_workers
            ),
            resource_schedule_sensitivity_process_pool=(
                query.resource_schedule_sensitivity_process_pool
            ),
        )
        graph, warnings = self._process_graph_data(
            graph_query,
            include_warnings=True,
        )
        canonical_terminal_symbols = self._canonical_process_symbols(
            query.project_id,
            query.terminal_process_symbols or [],
        )
        priority_terminal_symbols = (
            canonical_terminal_symbols if query.scope is None else []
        )
        project = self._repository.get_project(query.project_id)
        scoped_process_ids = [
            str(node["process_id"])
            for node in graph["nodes"]
            if node.get("process_id")
        ]
        blockers = self._blocker_data(
            QueryBlockers(
                project_id=query.project_id,
                as_of=query.as_of,
                process_ids=scoped_process_ids,
                include_resolved=False,
            )
        )
        snapshots = self._agent_schedule_snapshots(
            query,
            canonical_terminal_symbols,
        )
        return (
            {
                "context_version": 1,
                "project": {
                    "project_id": project.project_id,
                    "name": project.name,
                    "start_at": project.start_at.isoformat(),
                    "default_currency": project.default_currency,
                },
                "as_of": query.as_of.isoformat(),
                "now": query.now.isoformat(),
                "scope": self._json_ready(context_scope or {"type": "project"}),
                "terminal_process_symbols": query.terminal_process_symbols or [],
                "canonical_terminal_process_symbols": canonical_terminal_symbols,
                "summary": self._agent_context_summary(graph),
                "graph": self._agent_graph_context(graph),
                "schedule": self._agent_schedule_context(graph),
                "slippage": self._agent_slippage_context(
                    snapshots,
                    query.snapshot_limit,
                ),
                "prioritized_work": {
                    "by_role": self._agent_role_priority_context(
                        graph,
                        query.now,
                        priority_terminal_symbols,
                    ),
                    "by_resource": self._agent_resource_priority_context(
                        graph,
                        query.now,
                        priority_terminal_symbols,
                    ),
                },
                "milestones": self._agent_milestone_context(query),
                "blockers": blockers["blockers"],
                "available_queries": [
                    "query_process_graph",
                    "query_resource_schedule",
                    "query_process_role_pins",
                    "query_schedule_snapshots",
                    "query_milestones",
                    "query_utilization",
                    "query_costs",
                    "query_resource_capacity",
                ],
            },
            warnings,
        )

    def _agent_context_scope(self, query: QueryAgentContext):
        if query.scope is not None or not query.terminal_process_symbols:
            return query.scope
        return {
            "type": "topo_filter",
            "root_process_symbols": query.terminal_process_symbols,
            "direction": "ancestors",
        }

    def _canonical_process_symbols(
        self,
        project_id: str,
        process_symbols: list[str],
    ) -> list[str]:
        processes = getattr(self._repository, "processes", {})
        output = []
        for symbol in process_symbols:
            process_id = self._repository.resolve_process_id(project_id, symbol)
            process = processes.get(process_id)
            output.append(str(getattr(process, "symbol", symbol)))
        return output

    def _agent_schedule_snapshots(
        self,
        query: QueryAgentContext,
        canonical_terminal_symbols: list[str],
    ) -> list[object]:
        symbol_sets: list[list[str] | None] = []
        if query.terminal_process_symbols:
            symbol_sets.append(query.terminal_process_symbols)
            if sorted(canonical_terminal_symbols) != sorted(
                query.terminal_process_symbols
            ):
                symbol_sets.append(canonical_terminal_symbols)
        else:
            symbol_sets.append(None)

        snapshots_by_id: dict[str, object] = {}
        for terminal_symbols in symbol_sets:
            rows = self._schedule_snapshot_data(
                QueryScheduleSnapshots(
                    project_id=query.project_id,
                    as_of=query.as_of,
                    terminal_process_symbols=terminal_symbols,
                )
            )["snapshots"]
            for snapshot in rows:
                snapshots_by_id[str(snapshot["snapshot_id"])] = snapshot
        return sorted(
            snapshots_by_id.values(),
            key=self._agent_snapshot_sort_key,
        )

    def _agent_milestone_context(
        self,
        query: QueryAgentContext,
    ) -> list[dict[str, object]]:
        rows = []
        for milestone in self._repository.list_milestones(
            query.project_id,
            include_inactive=False,
        ):
            snapshots = self._schedule_snapshot_data(
                QueryScheduleSnapshots(
                    project_id=query.project_id,
                    as_of=query.as_of,
                    terminal_process_symbols=milestone.process_symbols,
                )
            )["snapshots"]
            rows.append(
                {
                    **milestone.model_dump(mode="json"),
                    "slippage": self._agent_slippage_context(
                        snapshots,
                        query.snapshot_limit,
                    ),
                }
            )
        return rows

    def _agent_snapshot_sort_key(self, snapshot: object) -> tuple[dt.datetime, str]:
        committed_at = self._parse_datetime(snapshot.get("committed_at"))
        if committed_at is None:
            committed_at = dt.datetime.min.replace(tzinfo=dt.UTC)
        return committed_at.astimezone(dt.UTC), str(snapshot["snapshot_id"])

    def _is_blocker_graph_node(self, node: Mapping[str, object]) -> bool:
        return str(node.get("process_type", "standard")) == "blocker"

    def _agent_context_summary(self, graph: dict[str, object]) -> dict[str, object]:
        nodes = list(graph.get("nodes", []))
        process_symbols = {str(node.get("process_symbol")) for node in nodes}
        process_edges = [
            edge
            for edge in graph.get("edges", [])
            if str(edge.get("predecessor_process_symbol")) in process_symbols
            and str(edge.get("successor_process_symbol")) in process_symbols
        ]
        status_counts: dict[str, int] = defaultdict(int)
        total_effort = 0.0
        blocked_count = 0
        sensitivity_rows = []
        for node in nodes:
            status = str(node.get("status") or "unknown")
            status_counts[status] += 1
            blocker_summary = node.get("blocker_summary") or {}
            if int(blocker_summary.get("blocking_count") or 0) > 0:
                blocked_count += 1
            resource = node.get("resource_aware") or {}
            sensitivity = resource.get("max_makespan_sensitivity_hours")
            if sensitivity is not None:
                try:
                    sensitivity_value = float(sensitivity)
                except (TypeError, ValueError):
                    sensitivity_value = 0.0
                if sensitivity_value > 0:
                    sensitivity_rows.append(
                        {
                            "symbol": node.get("process_symbol"),
                            "name": node.get("name"),
                            "max_makespan_sensitivity_hours": (
                                self._clean_number(sensitivity_value)
                            ),
                        }
                    )
            for requirement in node.get("role_requirements") or []:
                total_effort += float(requirement.get("effort_hours") or 0)
        completion_at = self._latest_datetime(
            (node.get("resource_aware") or {}).get("ends_at")
            for node in nodes
        )
        return {
            "process_count": len(nodes),
            "edge_count": len(process_edges),
            "status_counts": dict(sorted(status_counts.items())),
            "blocked_process_count": blocked_count,
            "total_role_effort_hours": self._clean_number(total_effort),
            "projected_completion_at": (
                completion_at.isoformat() if completion_at is not None else None
            ),
            "makespan_sensitive_process_count": len(sensitivity_rows),
            "top_makespan_sensitivity": sorted(
                sensitivity_rows,
                key=lambda item: float(item["max_makespan_sensitivity_hours"]),
                reverse=True,
            )[:10],
            "converged": graph.get("converged"),
        }

    def _agent_graph_context(self, graph: dict[str, object]) -> dict[str, object]:
        scoped_predecessors: dict[str, list[str]] = defaultdict(list)
        scoped_successors: dict[str, list[str]] = defaultdict(list)
        process_symbols = {
            str(node.get("process_symbol")) for node in graph.get("nodes", [])
        }
        edges = []
        for edge in graph.get("edges", []):
            predecessor = edge.get("predecessor_process_symbol")
            successor = edge.get("successor_process_symbol")
            if (
                str(predecessor) not in process_symbols
                or str(successor) not in process_symbols
            ):
                continue
            if predecessor and successor:
                scoped_predecessors[str(successor)].append(str(predecessor))
                scoped_successors[str(predecessor)].append(str(successor))
            edges.append(
                {
                    "predecessor": predecessor,
                    "successor": successor,
                    "dependency_type": edge.get("dependency_type"),
                }
            )
        project_id = str(graph.get("project_id") or "")
        as_of = self._parse_datetime(graph.get("as_of"))
        process_ids = [
            str(node.get("process_id"))
            for node in graph.get("nodes", [])
            if node.get("process_id")
        ]
        topology = None
        if project_id and as_of is not None:
            topology = self._agent_direct_topology_symbols(
                project_id,
                as_of,
                set(process_ids),
            )
        nodes = []
        for node in graph.get("nodes", []):
            resource = node.get("resource_aware") or {}
            symbol = str(node.get("process_symbol"))
            process_id = str(node.get("process_id") or "")
            if topology is None:
                predecessors = sorted(scoped_predecessors.get(symbol, []))
                successors = sorted(scoped_successors.get(symbol, []))
            else:
                predecessors = sorted(topology["predecessors"].get(process_id, []))
                successors = sorted(topology["successors"].get(process_id, []))
            nodes.append(
                {
                    "process_id": node.get("process_id"),
                    "symbol": symbol,
                    "aliases": node.get("aliases") or [],
                    "name": node.get("name"),
                    "description": node.get("description") or "",
                    "process_type": node.get("process_type"),
                    "status": node.get("status"),
                    "computed_status": node.get("computed_status"),
                    "blocker_summary": node.get("blocker_summary"),
                    "predecessors": predecessors,
                    "successors": successors,
                    "role_requirements": node.get("role_requirements") or [],
                    "assumption_note": node.get("assumption_note"),
                    "earliest_start_at": node.get("earliest_start_at"),
                    "started_at": node.get("started_at"),
                    "finished_at": node.get("finished_at"),
                    "schedule": {
                        "starts_at": resource.get("starts_at"),
                        "ends_at": resource.get("ends_at"),
                        "schedule_window_starts_at": resource.get(
                            "schedule_window_starts_at"
                        ),
                        "schedule_window_ends_at": resource.get(
                            "schedule_window_ends_at"
                        ),
                        "schedule_buffer_hours": resource.get(
                            "schedule_buffer_hours"
                        ),
                        "schedule_elapsed_hours": resource.get(
                            "schedule_elapsed_hours"
                        ),
                        "inferred_duration_hours": resource.get(
                            "inferred_duration_hours"
                        ),
                        "role_sensitivity": resource.get("role_sensitivity") or [],
                        "max_makespan_sensitivity_hours": resource.get(
                            "max_makespan_sensitivity_hours"
                        ),
                        "sensitivity_label": resource.get("sensitivity_label"),
                        "allocation_state": resource.get("allocation_state"),
                    },
                }
            )
        return {"nodes": nodes, "edges": edges}

    def _agent_direct_topology_symbols(
        self,
        project_id: str,
        as_of: dt.datetime,
        process_ids: set[str],
    ) -> dict[str, dict[str, list[str]]]:
        active_ids = set(self._repository.active_process_ids_as_of(project_id, as_of))
        processes = getattr(self._repository, "processes", {})
        predecessors: dict[str, set[str]] = defaultdict(set)
        successors: dict[str, set[str]] = defaultdict(set)
        for successor_id in active_ids:
            revision = self._repository.selected_revision_as_of(
                project_id,
                successor_id,
                as_of,
            )
            if revision is None:
                continue
            for predecessor_id in revision.dependencies:
                if predecessor_id not in active_ids:
                    continue
                predecessor_symbol = getattr(
                    processes.get(predecessor_id),
                    "symbol",
                    predecessor_id,
                )
                successor_symbol = getattr(
                    processes.get(successor_id),
                    "symbol",
                    successor_id,
                )
                if successor_id in process_ids:
                    predecessors[successor_id].add(str(predecessor_symbol))
                if predecessor_id in process_ids:
                    successors[predecessor_id].add(str(successor_symbol))
        return {
            "predecessors": {
                process_id: sorted(symbols)
                for process_id, symbols in predecessors.items()
            },
            "successors": {
                process_id: sorted(symbols)
                for process_id, symbols in successors.items()
            },
        }

    def _agent_schedule_context(self, graph: dict[str, object]) -> dict[str, object]:
        rows = []
        for node in graph.get("nodes", []):
            resource = node.get("resource_aware") or {}
            dependency = node.get("dependency_only") or {}
            planned_start_at = resource.get("starts_at") or dependency.get("es_at")
            planned_finish_at = resource.get("ends_at") or dependency.get("ef_at")
            planned_finish_at = self._pm_schedule_fallback_finish_at(
                planned_start_at,
                planned_finish_at,
                node,
            )
            rows.append(
                {
                    "symbol": node.get("process_symbol"),
                    "name": node.get("name"),
                    "status": node.get("status"),
                    "computed_status": node.get("computed_status"),
                    "blocker_summary": node.get("blocker_summary"),
                    "planned_start_at": planned_start_at,
                    "planned_finish_at": planned_finish_at,
                    "schedule_window_starts_at": resource.get(
                        "schedule_window_starts_at"
                    )
                    or planned_start_at,
                    "schedule_window_ends_at": resource.get("schedule_window_ends_at")
                    or planned_finish_at,
                    "schedule_buffer_hours": resource.get("schedule_buffer_hours"),
                    "schedule_elapsed_hours": resource.get("schedule_elapsed_hours"),
                    "inferred_duration_hours": resource.get("inferred_duration_hours")
                    or self._pm_role_requirement_duration_hours(node),
                    "role_sensitivity": resource.get("role_sensitivity") or [],
                    "max_makespan_sensitivity_hours": resource.get(
                        "max_makespan_sensitivity_hours"
                    ),
                    "sensitivity_label": resource.get("sensitivity_label"),
                    "allocation_state": resource.get("allocation_state"),
                }
            )
        completion_at = self._latest_datetime(
            row.get("planned_finish_at") for row in rows
        )
        return {
            "basis": graph.get("schedule_basis"),
            "converged": graph.get("converged"),
            "completion_at": (
                completion_at.isoformat() if completion_at is not None else None
            ),
            "processes": rows,
        }

    def _pm_schedule_fallback_finish_at(
        self,
        planned_start_at: object,
        planned_finish_at: object,
        node: dict[str, object],
    ) -> object:
        start = self._parse_datetime(planned_start_at)
        finish = self._parse_datetime(planned_finish_at)
        if start is None:
            return planned_finish_at
        inferred_hours = self._pm_role_requirement_duration_hours(node)
        if inferred_hours is None:
            return planned_finish_at
        if finish is None or finish <= start:
            return (start + dt.timedelta(hours=inferred_hours)).isoformat()
        return planned_finish_at

    def _pm_role_requirement_duration_hours(
        self,
        node: dict[str, object],
    ) -> float | None:
        requirement_hours = []
        for requirement in node.get("role_requirements") or []:
            try:
                hours = float(requirement.get("effort_hours"))
            except (TypeError, ValueError):
                continue
            try:
                resource_count = int(requirement.get("required_resource_count") or 1)
            except (TypeError, ValueError):
                resource_count = 1
            if hours > 0:
                requirement_hours.append(hours / max(resource_count, 1))
        if not requirement_hours:
            return None
        return self._clean_number(max(requirement_hours))

    def _agent_slippage_context(
        self,
        snapshots: list[object],
        limit: int,
    ) -> dict[str, object]:
        history = list(snapshots)[-limit:]
        timeline = [
            {
                "commit_datetime": snapshot.get("committed_at"),
                "estimated_done_datetime": snapshot.get("completion_at"),
            }
            for snapshot in history
        ]
        latest = history[-1] if history else None
        previous = history[-2] if len(history) > 1 else None
        change_hours = None
        if latest is not None and previous is not None:
            latest_completion = self._parse_datetime(latest.get("completion_at"))
            previous_completion = self._parse_datetime(previous.get("completion_at"))
            if latest_completion is not None and previous_completion is not None:
                change_hours = self._clean_number(
                    (latest_completion - previous_completion).total_seconds() / 3600
                )
        return {
            "snapshot_count": len(snapshots),
            "latest": latest,
            "previous": previous,
            "completion_change_hours": change_hours,
            "timeline": timeline,
            "history": history,
        }

    def _agent_role_priority_context(
        self,
        graph: dict[str, object],
        now: dt.datetime,
        terminal_symbols: list[str],
    ) -> list[dict[str, object]]:
        role_rows: dict[str, list[dict[str, object]]] = defaultdict(list)
        role_names = {
            str(role["role_id"]): str(role.get("name", role["role_id"]))
            for role in getattr(self._repository, "roles", {}).values()
            if role["project_id"] == graph.get("project_id")
        }
        for node, priority in self._agent_priority_nodes(
            graph,
            now,
            terminal_symbols,
        ):
            for requirement in node.get("role_requirements") or []:
                role_id = requirement.get("role_id")
                if not role_id:
                    continue
                role_rows[str(role_id)].append(
                    {
                        **priority,
                        "effort_hours": self._clean_number(
                            requirement.get("effort_hours") or 0
                        ),
                        "status": node.get("status"),
                        "computed_status": node.get("computed_status"),
                        "blocking_count": (
                            (node.get("blocker_summary") or {}).get("blocking_count")
                            or 0
                        ),
                    }
                )
        return [
            {
                "role_id": role_id,
                "role_name": role_names.get(role_id, role_id),
                "processes": self._sort_agent_priority_rows(rows),
            }
            for role_id, rows in sorted(role_rows.items())
        ]

    def _agent_resource_priority_context(
        self,
        graph: dict[str, object],
        now: dt.datetime,
        terminal_symbols: list[str],
    ) -> list[dict[str, object]]:
        resource_rows: dict[str, list[dict[str, object]]] = defaultdict(list)
        resource_names = {
            str(resource["resource_id"]): str(
                resource.get("name", resource["resource_id"])
            )
            for resource in getattr(self._repository, "resources", {}).values()
            if resource["project_id"] == graph.get("project_id")
        }
        priority_by_process = {
            str(node["process_id"]): (node, priority)
            for node, priority in self._agent_priority_nodes(
                graph,
                now,
                terminal_symbols,
            )
            if node.get("process_id")
        }
        assignments: dict[tuple[str, str], dict[str, object]] = {}
        for slice_data in graph.get("allocation_slices", []):
            process_id = slice_data.get("process_id")
            resource_id = slice_data.get("resource_id")
            if process_id not in priority_by_process or not resource_id:
                continue
            node, priority = priority_by_process[str(process_id)]
            key = (str(resource_id), str(process_id))
            assignment = assignments.setdefault(
                key,
                {
                    **priority,
                    "effort_hours": 0.0,
                    "role_ids": set(),
                    "status": node.get("status"),
                    "computed_status": node.get("computed_status"),
                    "blocking_count": (
                        (node.get("blocker_summary") or {}).get("blocking_count")
                        or 0
                    ),
                },
            )
            assignment["effort_hours"] = float(assignment["effort_hours"]) + float(
                slice_data.get("effort_hours") or 0
            )
            role_id = slice_data.get("role_id")
            if role_id:
                assignment["role_ids"].add(str(role_id))

        for process_id, (node, priority) in priority_by_process.items():
            for requirement in node.get("role_requirements") or []:
                role_id = requirement.get("role_id")
                if not role_id:
                    continue
                active_pins = [
                    pin
                    for pin in requirement.get("pins", []) or []
                    if isinstance(pin, dict)
                    and pin.get("status") == "pinned_started"
                ]
                for pin in active_pins:
                    resource_id = pin.get("resource_id")
                    if not resource_id:
                        continue
                    key = (str(resource_id), process_id)
                    assignment = assignments.setdefault(
                        key,
                        {
                            **priority,
                            "effort_hours": float(
                                requirement.get("effort_hours") or 0
                            ),
                            "role_ids": set(),
                            "status": node.get("status"),
                            "computed_status": node.get("computed_status"),
                            "blocking_count": (
                                (node.get("blocker_summary") or {}).get(
                                    "blocking_count",
                                )
                                or 0
                            ),
                        },
                    )
                    assignment["role_ids"].add(str(role_id))
                    assignment["active_pin"] = True
                    assignment["pin_started_at"] = pin.get("pinned_at")

        for (resource_id, _process_id), assignment in assignments.items():
            role_ids = assignment.pop("role_ids")
            resource_rows[resource_id].append(
                {
                    **assignment,
                    "effort_hours": self._clean_number(assignment["effort_hours"]),
                    "role_ids": sorted(role_ids),
                }
            )
        return [
            {
                "resource_id": resource_id,
                "resource_name": resource_names.get(resource_id, resource_id),
                "processes": self._sort_agent_priority_rows(rows),
            }
            for resource_id, rows in sorted(resource_rows.items())
        ]

    def _agent_priority_nodes(
        self,
        graph: dict[str, object],
        now: dt.datetime,
        terminal_symbols: list[str],
    ) -> list[tuple[dict[str, object], dict[str, object]]]:
        scoped_symbols = set(self._agent_ancestor_scope_symbols(graph, terminal_symbols))
        rows = []
        for node in graph.get("nodes", []):
            symbol = node.get("process_symbol")
            if scoped_symbols and symbol not in scoped_symbols:
                continue
            status = str(node.get("computed_status") or node.get("status") or "")
            if status in {"done", "finished", "canceled"}:
                continue
            dependency = node.get("dependency_only") or {}
            resource = node.get("resource_aware") or {}
            planned_start_at = self._parse_datetime(
                resource.get("starts_at") or dependency.get("es_at")
            )
            planned_finish_at = self._parse_datetime(
                resource.get("ends_at") or dependency.get("ef_at")
            )
            if planned_start_at is None or planned_finish_at is None:
                continue
            time_until_start = planned_start_at - now
            if status in {"started", "due", "early_start"}:
                priority, priority_rank = "P0", 0
            elif time_until_start < dt.timedelta(days=3):
                priority, priority_rank = "P0", 0
            elif time_until_start < dt.timedelta(days=7):
                priority, priority_rank = "P1", 1
            elif time_until_start < dt.timedelta(days=14):
                priority, priority_rank = "P2", 2
            else:
                priority, priority_rank = "P3", 3
            rows.append(
                (
                    node,
                    {
                        "priority": priority,
                        "priority_rank": priority_rank,
                        "process_id": node.get("process_id"),
                        "process_symbol": symbol,
                        "process_name": node.get("name"),
                        "planned_start_at": planned_start_at.isoformat(),
                        "planned_finish_at": planned_finish_at.isoformat(),
                        "schedule_window_starts_at": resource.get(
                            "schedule_window_starts_at"
                        ),
                        "schedule_window_ends_at": resource.get(
                            "schedule_window_ends_at"
                        ),
                        "schedule_buffer_hours": resource.get(
                            "schedule_buffer_hours"
                        ),
                        "max_makespan_sensitivity_hours": resource.get(
                            "max_makespan_sensitivity_hours"
                        ),
                        "sensitivity_label": resource.get("sensitivity_label"),
                        "hours_until_planned_start": self._clean_number(
                            (planned_start_at - now).total_seconds() / 3600
                        ),
                        "hours_until_planned_finish": self._clean_number(
                            (planned_finish_at - now).total_seconds() / 3600
                        ),
                    },
                )
            )
        return rows

    def _agent_ancestor_scope_symbols(
        self,
        graph: dict[str, object],
        terminal_symbols: list[str],
    ) -> list[str]:
        if not terminal_symbols:
            return [str(node.get("process_symbol")) for node in graph.get("nodes", [])]
        selected = {symbol for symbol in terminal_symbols if symbol}
        predecessors: dict[str, set[str]] = defaultdict(set)
        for edge in graph.get("edges", []):
            predecessor = edge.get("predecessor_process_symbol")
            successor = edge.get("successor_process_symbol")
            if predecessor and successor:
                predecessors[str(successor)].add(str(predecessor))
        stack = list(selected)
        while stack:
            current = stack.pop()
            for predecessor in predecessors.get(current, set()):
                if predecessor in selected:
                    continue
                selected.add(predecessor)
                stack.append(predecessor)
        return [
            str(node.get("process_symbol"))
            for node in graph.get("nodes", [])
            if node.get("process_symbol") in selected
        ]

    def _sort_agent_priority_rows(
        self,
        rows: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        return sorted(
            rows,
            key=lambda row: (
                row["priority_rank"],
                row.get(
                    "hours_until_planned_start",
                    row.get("hours_until_planned_finish", 0),
                ),
                str(row.get("process_symbol") or ""),
            ),
        )

    def _critical_path_symbols(self, graph: dict[str, object]) -> list[str]:
        symbols_by_id = {
            node.get("process_id"): node.get("process_symbol")
            for node in graph.get("nodes", [])
        }
        return [
            str(symbols_by_id.get(process_id, process_id))
            for process_id in graph.get("critical_path_process_ids", [])
        ]

    def _latest_datetime(self, values) -> dt.datetime | None:
        parsed = [
            item
            for item in (self._parse_datetime(value) for value in values)
            if item is not None
        ]
        return max(parsed, default=None)

    def _capacity_data(self, query: QueryResourceCapacity) -> dict[str, object]:
        self._validate_resource_filters(
            query.project_id,
            resource_ids=query.resource_ids,
            role_ids=query.role_ids,
            error_for_unknown=False,
        )
        buckets = self._expanded_capacity(
            query.project_id,
            query.horizon_starts_at,
            query.horizon_ends_at,
            query.resource_ids,
            query.role_ids,
        )
        self._overlay_allocations_on_capacity_buckets(
            query,
            buckets,
        )
        return {
            "project_id": query.project_id,
            "as_of": query.as_of.isoformat(),
            "horizon_starts_at": query.horizon_starts_at.isoformat(),
            "horizon_ends_at": query.horizon_ends_at.isoformat(),
            "planning_granularity": self._enum_value(query.planning_granularity),
            "buckets": [self._bucket_json(bucket) for bucket in buckets],
        }

    def _overlay_allocations_on_capacity_buckets(
        self,
        query: QueryResourceCapacity,
        buckets: list[dict[str, Any]],
    ) -> None:
        for bucket in buckets:
            bucket["allocated_hours"] = 0.0
            bucket["remaining_hours"] = bucket["capacity_hours"]
        if not buckets:
            return

        schedule_query = QueryResourceSchedule(
            project_id=query.project_id,
            as_of=query.as_of,
            now=query.as_of,
            planning_granularity=query.planning_granularity,
            include_allocation_slices=True,
        )
        try:
            schedule = self._compute_resource_schedule(
                schedule_query,
                include_allocation_slices=True,
            )
        except ServiceValidationError:
            return

        slices = [self._parse_slice(item) for item in schedule["allocation_slices"]]
        for slice_data in slices:
            for bucket in buckets:
                if (
                    bucket["resource_id"] != slice_data["resource_id"]
                    or bucket["starts_at"] >= slice_data["ends_at"]
                    or bucket["ends_at"] <= slice_data["starts_at"]
                ):
                    continue
                overlap = self._overlap_hours(
                    bucket["starts_at"],
                    bucket["ends_at"],
                    slice_data["starts_at"],
                    slice_data["ends_at"],
                )
                slice_hours = self._overlap_hours(
                    slice_data["starts_at"],
                    slice_data["ends_at"],
                    slice_data["starts_at"],
                    slice_data["ends_at"],
                )
                if slice_hours <= 0:
                    continue
                allocated = float(slice_data["capacity_hours"]) * overlap / slice_hours
                bucket["allocated_hours"] += allocated
                bucket["remaining_hours"] -= allocated

    def _resource_schedule_data(
        self,
        query,
    ) -> tuple[dict[str, object], list[Warning]]:
        schedule = self._compute_resource_schedule(
            query,
            include_allocation_slices=getattr(query, "include_allocation_slices", False),
        )
        include_slices = getattr(query, "include_allocation_slices", False)
        data = dict(schedule)
        data["as_of"] = query.as_of.isoformat()
        data["now"] = query.now.isoformat()
        warnings = self._resource_warnings(data, query.max_iterations)
        if not include_slices:
            data["allocation_slices"] = []
        data.pop("horizon_starts_at", None)
        data.pop("horizon_ends_at", None)
        return data, warnings

    def _compute_resource_schedule(
        self,
        query,
        *,
        include_allocation_slices: bool = True,
    ) -> dict[str, object]:
        key = self._resource_schedule_cache_key(query)

        def factory() -> dict[str, object]:
            scheduler_input = self._resource_schedule_input(
                query,
                include_allocation_slices=True,
            )
            try:
                if self._resource_scheduler is not None:
                    schedule = self._resource_scheduler(scheduler_input)
                else:
                    schedule = compute_resource_schedule(scheduler_input)
            except ValueError as exc:
                raise ServiceValidationError(
                    code="resource_schedule_unsatisfiable",
                    message=str(exc),
                ) from exc
            return self._normalize_schedule_dict(schedule)

        schedule = self._cached_value(
            self._resource_schedule_cache,
            key,
            factory,
            copy_result=True,
        )
        if not include_allocation_slices:
            schedule["allocation_slices"] = []
        return schedule

    def _resource_schedule_input(
        self,
        query,
        *,
        include_allocation_slices: bool,
    ) -> dict[str, object]:
        project = self._repository.get_project(query.project_id)
        active_process_ids = self._repository.active_process_ids_as_of(
            query.project_id,
            query.as_of,
        )
        scope = getattr(query, "scope", None)
        if isinstance(query, QueryCosts):
            scope = None
        if scope is not None:
            scoped_ids, _scope_data, _target_process_id = (
                self._repository.process_ids_for_scope(
                    query.project_id,
                    query.as_of,
                    scope,
                )
            )
            active_process_ids = [
                process_id
                for process_id in active_process_ids
                if process_id in scoped_ids
            ]
        selected_process_ids = set(active_process_ids)
        processes = []
        requirements = []
        process_records = getattr(self._repository, "processes", {})
        pin_summaries = {
            process_id: self._pin_summary_for_process(
                query.project_id,
                process_id,
                query.as_of,
            )
            for process_id in active_process_ids
        }
        for process_id in active_process_ids:
            revision = self._repository.selected_revision_as_of(
                query.project_id,
                process_id,
                query.as_of,
            )
            if revision is None:
                continue
            process = process_records.get(process_id)
            pin_summary = pin_summaries.get(process_id, {})
            facts = self._process_completedness_facts(
                query.project_id,
                process_id,
                query.as_of,
            )
            status = str(facts.get("status") or "waiting")
            process_started_at = facts.get("started_at")
            process_finished_at = facts.get("finished_at")
            remaining_ready_at = None
            if process_started_at is None and process_finished_at is None:
                remaining_ready_at = max(query.now, query.as_of)
            dependencies = [
                dependency
                for dependency in revision.dependencies
                if dependency in selected_process_ids
            ]
            process_requirements = []
            for index, requirement in enumerate(revision.role_requirements):
                requirement_id = (
                    requirement.requirement_id
                    or f"{process_id}-requirement-{index + 1}"
                )
                pin_requirement = pin_summary.get("requirements", {}).get(
                    requirement_id,
                    {},
                )
                preferred_resources = list(
                    pin_requirement.get("preferred_resource_ids") or []
                )
                process_requirements.append(
                    {
                        "requirement_id": requirement_id,
                        "project_id": query.project_id,
                        "process_id": process_id,
                        "role_id": requirement.role_id,
                        "effort_hours": requirement.effort_hours,
                        "min_allocation_hours_per_day": (
                            requirement.min_allocation_hours_per_day
                        ),
                        "max_allocation_hours_per_day": (
                            requirement.max_allocation_hours_per_day
                        ),
                        "required_resource_count": requirement.required_resource_count,
                        "allocation_policy": self._enum_value(
                            requirement.allocation_policy,
                        ),
                        "preferred_resource_ids": preferred_resources,
                    }
                )
            if process_requirements and remaining_ready_at is not None:
                remaining_ready_at = max(
                    remaining_ready_at,
                    pin_summary.get("remaining_ready_at", remaining_ready_at),
                )
            processes.append(
                {
                    "process_id": process_id,
                    "name": revision.name,
                    "description": revision.description,
                    "process_type": (
                        getattr(process, "process_type", "standard")
                        if process is not None
                        else "standard"
                    ),
                    "dependencies": dependencies,
                    "duration_business_days": revision.duration_business_days,
                    "derived_status": status,
                    "started_at": process_started_at,
                    "finished_at": process_finished_at,
                    "remaining_ready_at": remaining_ready_at,
                    "earliest_start_at": revision.earliest_start_at,
                    "start_at_earliest": revision.start_at_earliest,
                    "delay_after_dependencies_business_days": (
                        revision.delay_after_dependencies_business_days
                    ),
                }
            )
            requirements.extend(process_requirements)

        roles = [
            dict(role)
            for role in getattr(self._repository, "roles", {}).values()
            if role["project_id"] == query.project_id
        ]
        resources = [
            dict(resource)
            for resource in getattr(self._repository, "resources", {}).values()
            if resource["project_id"] == query.project_id
        ]
        calendars = [
            dict(calendar)
            for calendar in getattr(self._repository, "calendars", {}).values()
            if calendar["project_id"] == query.project_id
        ]
        blockers = [
            {
                "blocker_id": blocker.blocker_id,
                "project_id": blocker.project_id,
                "process_id": blocker.process_id,
                "severity": "blocking",
                "created_at": blocker.created_at or blocker.opened_at,
                "resolved_at": blocker.resolved_at,
            }
            for blocker in self._repository.list_blockers_as_of(
                query.project_id,
                query.as_of,
                True,
            )
        ]
        horizon_starts_at, horizon_ends_at = self._resource_schedule_window(
            query,
            scope,
        )
        fixed_allocation_slices: list[dict[str, object]] = []
        capacity_holds = self._process_role_pin_capacity_holds(
            project_id=query.project_id,
            as_of=query.as_of,
            now=query.now,
        )
        fixed_role_completions = self._process_role_pin_completions(
            project_id=query.project_id,
            as_of=query.as_of,
            now=query.now,
            selected_process_ids=selected_process_ids,
        )
        return {
            "project_id": query.project_id,
            "project_start_at": project.start_at,
            "as_of": query.as_of,
            "now": query.now,
            "processes": processes,
            "dependencies": [
                {
                    "predecessor_process_id": dependency_id,
                    "successor_process_id": process["process_id"],
                }
                for process in processes
                for dependency_id in process["dependencies"]
            ],
            "role_requirements": requirements,
            "fixed_allocation_slices": fixed_allocation_slices,
            "capacity_holds": capacity_holds,
            "fixed_role_completions": fixed_role_completions,
            "roles": roles,
            "resources": resources,
            "calendars": calendars,
            "blockers": blockers,
            "options": {
                "horizon_starts_at": horizon_starts_at,
                "horizon_ends_at": horizon_ends_at,
                "planning_granularity": self._enum_value(query.planning_granularity),
                "max_iterations": query.max_iterations,
                "convergence_tolerance_hours": query.convergence_tolerance_hours,
                "resource_schedule_backend": query.resource_schedule_backend,
                "resource_schedule_mcts_c_puct": query.resource_schedule_mcts_c_puct,
                "resource_schedule_mcts_max_actions": (
                    query.resource_schedule_mcts_max_actions
                ),
                "include_resource_sensitivity": getattr(
                    query,
                    "include_resource_sensitivity",
                    False,
                ),
                "resource_schedule_sensitivity_backend": getattr(
                    query,
                    "resource_schedule_sensitivity_backend",
                    None,
                ),
                "resource_schedule_sensitivity_workers": getattr(
                    query,
                    "resource_schedule_sensitivity_workers",
                    None,
                ),
                "resource_schedule_sensitivity_process_pool": getattr(
                    query,
                    "resource_schedule_sensitivity_process_pool",
                    True,
                ),
                "blocked_policy": "include_normally",
                "include_allocation_slices": include_allocation_slices,
            },
        }

    def _resource_schedule_window(
        self,
        query,
        scope: dict[str, object] | None,
    ) -> tuple[dt.datetime, dt.datetime]:
        return self._snapshot_horizon(query.project_id, query.as_of, scope)

    def _active_process_role_pins(
        self,
        project_id: str,
        as_of: dt.datetime,
        *,
        resource_id: str | None = None,
    ) -> list[ProcessRolePinRecord]:
        return [
            pin
            for pin in self._repository.list_process_role_pins(
                project_id,
                as_of=as_of,
                resource_id=resource_id,
                include_done=False,
            )
            if pin.status == "pinned_started"
        ]

    def _process_role_pin_capacity_holds(
        self,
        *,
        project_id: str,
        as_of: dt.datetime,
        now: dt.datetime,
    ) -> list[dict[str, object]]:
        holds = []
        pins = self._active_process_role_pins(project_id, as_of)
        latest_finish_by_resource = {
            resource_id: max(
                max(pin.forecast_finish_at, now)
                for pin in pins
                if pin.resource_id == resource_id
            )
            for resource_id in {pin.resource_id for pin in pins}
        }
        for resource_id, finish_at in latest_finish_by_resource.items():
            starts_at = max(
                now,
                as_of,
                self._earliest_pin_start_for_resource(pins, resource_id),
            )
            if starts_at >= finish_at:
                continue
            holds.append(
                {
                    "hold_id": f"process-role-pins:{resource_id}",
                    "resource_id": resource_id,
                    "starts_at": starts_at,
                    "ends_at": finish_at,
                    "source": "process_role_pin",
                }
            )
        return holds

    def _earliest_pin_start_for_resource(
        self,
        pins: list[ProcessRolePinRecord],
        resource_id: str,
    ) -> dt.datetime:
        return min(
            pin.pinned_at
            for pin in pins
            if pin.resource_id == resource_id
        )

    def _process_role_pin_completions(
        self,
        *,
        project_id: str,
        as_of: dt.datetime,
        now: dt.datetime,
        selected_process_ids: set[str],
    ) -> list[dict[str, object]]:
        completions = []
        for pin in self._repository.list_process_role_pins(
            project_id,
            as_of=as_of,
            include_done=True,
        ):
            if pin.process_id not in selected_process_ids:
                continue
            completions.append(
                {
                    "process_id": pin.process_id,
                    "requirement_id": self._pin_requirement_id(pin),
                    "role_id": pin.role_id,
                    "resource_id": pin.resource_id,
                    "starts_at": pin.pinned_at,
                    "finish_at": (
                        pin.verified_done_at
                        if pin.verified_done_at is not None
                        else max(pin.forecast_finish_at, now)
                    ),
                    "source": "process_role_pin",
                    "pin_id": pin.pin_id,
                }
            )
        return completions

    def _pin_summary_for_process(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime,
    ) -> dict[str, object]:
        pins = self._repository.list_process_role_pins(
            project_id,
            as_of=as_of,
            process_id=process_id,
            include_done=True,
        )
        if not pins:
            started_at = None
        else:
            started_at = min(pin.pinned_at for pin in pins)
        latest_activity_at = max((pin.pinned_at for pin in pins), default=as_of)
        revision = self._repository.selected_revision_as_of(
            project_id,
            process_id,
            as_of,
        )
        expected_requirement_ids: set[str] = set()
        if revision is not None:
            for index, requirement in enumerate(revision.role_requirements):
                requirement_id = (
                    requirement.requirement_id
                    or f"{process_id}-requirement-{index + 1}"
                )
                expected_requirement_ids.add(requirement_id)
        by_requirement: dict[str, dict[str, object]] = {}
        verified_finished_by_requirement: dict[str, dt.datetime] = {}
        for pin in pins:
            requirement_id = self._pin_requirement_id(pin)
            done = (
                pin.status == "pinned_finished"
                and pin.verified_done_at is not None
                and pin.verified_done_at <= as_of
            )
            row = by_requirement.setdefault(
                requirement_id,
                {
                    "preferred_resource_ids": [],
                    "active_resource_ids": [],
                    "done_resource_ids": [],
                    "pins": [],
                },
            )
            if done:
                row["done_resource_ids"] = [
                    *list(row["done_resource_ids"]),
                    pin.resource_id,
                ]
                verified_finished_by_requirement[requirement_id] = pin.verified_done_at
            preferred = list(row["preferred_resource_ids"])
            if pin.resource_id in preferred:
                preferred.remove(pin.resource_id)
            preferred.insert(0, pin.resource_id)
            row["preferred_resource_ids"] = preferred
            if not done:
                row["active_resource_ids"] = [
                    *list(row["active_resource_ids"]),
                    pin.resource_id,
                ]
            row["pins"] = [
                *list(row["pins"]),
                {
                    "pin_id": pin.pin_id,
                    "project_id": pin.project_id,
                    "process_id": pin.process_id,
                    "requirement_id": requirement_id,
                    "role_id": pin.role_id,
                    "resource_id": pin.resource_id,
                    "pinned_at": pin.pinned_at.isoformat(),
                    "pinned_started_at": pin.pinned_at.isoformat(),
                    "forecast_finish_at": pin.forecast_finish_at.isoformat(),
                    "verified_done_at": (
                        pin.verified_done_at.isoformat()
                        if pin.verified_done_at is not None
                        else None
                    ),
                    "verified_finished_at": (
                        pin.verified_done_at.isoformat()
                        if pin.verified_done_at is not None
                        else None
                    ),
                    "status": pin.status,
                    "created_at": pin.created_at.isoformat(),
                    "updated_at": pin.updated_at.isoformat(),
                    "note": pin.note,
                    "active": not done,
                    "overdue": (
                        pin.status == "pinned_started"
                        and pin.forecast_finish_at < as_of
                    ),
                    "due": (
                        pin.status == "pinned_started"
                        and pin.verified_done_at is None
                        and pin.forecast_finish_at >= as_of
                    ),
                },
            ]
            latest_activity_at = max(latest_activity_at, pin.pinned_at)
        all_role_requirements_finished = bool(expected_requirement_ids) and (
            expected_requirement_ids <= set(verified_finished_by_requirement)
        )
        finished_at = (
            max(verified_finished_by_requirement.values())
            if all_role_requirements_finished
            else None
        )
        return {
            "started_at": started_at,
            "finished_at": finished_at,
            "has_due_process_role": any(
                bool(pin.get("due"))
                for requirement in by_requirement.values()
                for pin in list(requirement.get("pins") or [])
                if isinstance(pin, dict)
            ),
            "all_role_requirements_finished": all_role_requirements_finished,
            "remaining_ready_at": max(as_of, latest_activity_at),
            "requirements": by_requirement,
        }

    def _pin_requirement_id(
        self,
        pin: ProcessRolePinRecord,
    ) -> str:
        if pin.requirement_id is not None:
            return pin.requirement_id
        revision = self._repository.selected_revision_as_of(
            pin.project_id,
            pin.process_id,
            pin.pinned_at,
        )
        if revision is None:
            return f"{pin.process_id}:{pin.role_id}"
        for index, requirement in enumerate(revision.role_requirements):
            if requirement.role_id != pin.role_id:
                continue
            return (
                requirement.requirement_id
                or f"{pin.process_id}-requirement-{index + 1}"
            )
        return f"{pin.process_id}:{pin.role_id}"

    def _normalize_schedule_dict(self, schedule: dict[str, Any]) -> dict[str, object]:
        data = self._json_ready(dict(schedule))
        data.setdefault("allocation_slices", [])
        data.setdefault("processes", [])
        data.setdefault("critical_path_process_ids", [])
        data.setdefault("converged", True)
        data.setdefault("iteration_count", 1)
        data.setdefault(
            "convergence",
            {
                "converged": data["converged"],
                "iteration_count": data["iteration_count"],
                "max_iterations": 20,
                "tolerance_hours": 0,
                "changed_process_ids": [],
                "reason_changes": [],
                "allocation_fingerprint_changed": False,
            },
        )
        self._attach_process_facts_to_schedule(data)
        self._attach_schedule_diagnostic_defaults(data)
        return data

    def _attach_schedule_diagnostic_defaults(self, data: dict[str, object]) -> None:
        for row in data.get("processes", []):
            if not isinstance(row, dict):
                continue
            row.setdefault("allocation_diagnostic", None)

    def _attach_process_facts_to_schedule(self, data: dict[str, object]) -> None:
        processes = getattr(self._repository, "processes", {})
        project_id = str(data.get("project_id") or "")
        as_of = self._parse_datetime(data.get("as_of")) if data.get("as_of") else None
        for row in data.get("processes", []):
            if not isinstance(row, dict):
                continue
            process = processes.get(row.get("process_id"))
            if process is None:
                continue
            existing_start = (
                self._parse_datetime(row["starts_at"]) if row.get("starts_at") else None
            )
            facts = (
                self._process_completedness_facts(project_id, process.process_id, as_of)
                if project_id and as_of is not None
                else {}
            )
            row["status"] = str(facts.get("status") or "waiting")
            actual_start = facts.get("started_at")
            if (
                actual_start is not None
                and facts.get("normal_dependencies_finished")
                and actual_start <= as_of
                and (existing_start is None or actual_start < existing_start)
            ):
                row["starts_at"] = actual_start.isoformat()
            row["started_at"] = (
                actual_start.isoformat()
                if actual_start is not None
                and as_of is not None
                and actual_start <= as_of
                else None
            )
            finished_at = facts.get("finished_at")
            row["finished_at"] = (
                finished_at.isoformat() if finished_at is not None else None
            )
    def _process_actual_start_floor(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime | None,
    ) -> dt.datetime | None:
        if not project_id or as_of is None:
            return None
        project = self._repository.get_project(project_id)
        revision = self._repository.selected_revision_as_of(
            project_id,
            process_id,
            as_of,
        )
        if revision is None:
            return None
        processes = getattr(self._repository, "processes", {})
        floors = [project.start_at]
        for dependency_id in revision.dependencies:
            dependency = processes.get(dependency_id)
            if dependency is None:
                return None
            facts = self._process_completedness_facts(
                project_id,
                dependency_id,
                as_of,
            )
            if not facts["is_finished"]:
                return None
            finished_at = facts.get("finished_at")
            if finished_at is not None:
                floors.append(finished_at)
        return max(floors)

    def _json_ready(self, value):
        if isinstance(value, dt.datetime):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if hasattr(value, "model_dump"):
            return self._json_ready(value.model_dump(mode="json"))
        if isinstance(value, dict):
            return {key: self._json_ready(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self._json_ready(item) for item in value]
        return self._enum_value(value)

    def _expanded_capacity(
        self,
        project_id: str,
        horizon_starts_at: dt.datetime,
        horizon_ends_at: dt.datetime,
        resource_ids: list[str] | None,
        role_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        expander = getattr(self._repository, "expand_capacity_buckets", None)
        if expander is None:
            return []
        return expander(
            project_id,
            horizon_starts_at,
            horizon_ends_at,
            resource_ids,
            role_ids,
        )

    def _utilization_data(self, query: QueryUtilization):
        schedule = self._compute_resource_schedule(
            query,
            include_allocation_slices=True,
        )
        warnings = self._resource_warnings(schedule, query.max_iterations)
        horizon_starts_at = self._parse_datetime(schedule["horizon_starts_at"])
        horizon_ends_at = self._parse_datetime(schedule["horizon_ends_at"])
        buckets = self._expanded_capacity(
            query.project_id,
            horizon_starts_at,
            horizon_ends_at,
            None,
            None,
        )
        for bucket in buckets:
            bucket["allocated_hours"] = 0.0
            bucket["remaining_hours"] = bucket["capacity_hours"]
        slices = [self._parse_slice(item) for item in schedule["allocation_slices"]]
        for slice_data in slices:
            for bucket in buckets:
                if (
                    bucket["resource_id"] == slice_data["resource_id"]
                    and bucket["starts_at"] < slice_data["ends_at"]
                    and bucket["ends_at"] > slice_data["starts_at"]
                ):
                    overlap = self._overlap_hours(
                        bucket["starts_at"],
                        bucket["ends_at"],
                        slice_data["starts_at"],
                        slice_data["ends_at"],
                    )
                    slice_hours = self._overlap_hours(
                        slice_data["starts_at"],
                        slice_data["ends_at"],
                        slice_data["starts_at"],
                        slice_data["ends_at"],
                    )
                    if slice_hours <= 0:
                        continue
                    capacity = float(slice_data["capacity_hours"]) * overlap / slice_hours
                    bucket["allocated_hours"] += capacity
                    bucket["remaining_hours"] -= capacity
        by_resource = []
        for resource_id in sorted({bucket["resource_id"] for bucket in buckets}):
            resource_buckets = [
                bucket for bucket in buckets if bucket["resource_id"] == resource_id
            ]
            capacity = sum(bucket["capacity_hours"] for bucket in resource_buckets)
            allocated = sum(bucket["allocated_hours"] for bucket in resource_buckets)
            available = sum(bucket["available_hours"] for bucket in resource_buckets)
            by_resource.append(
                {
                    "resource_id": resource_id,
                    "capacity_hours": self._clean_number(capacity),
                    "available_hours": self._clean_number(available),
                    "allocated_hours": self._clean_number(allocated),
                    "remaining_hours": self._clean_number(capacity - allocated),
                    "utilization_ratio": self._clean_number(
                        allocated / capacity if capacity else 0,
                    ),
                }
            )
        scoped_ids = None
        if getattr(query, "scope", None) is not None:
            scoped_ids, _scope_data, _target_process_id = (
                self._repository.process_ids_for_scope(
                    query.project_id,
                    query.as_of,
                    query.scope,
                )
            )
        demanded_by_role = self._demanded_effort_by_role(
            query.project_id,
            query.as_of,
            scoped_ids,
        )
        fulfilled_by_role: dict[str, float] = defaultdict(float)
        for slice_data in slices:
            fulfilled_by_role[slice_data["role_id"]] += float(slice_data["effort_hours"])
        by_role = []
        for role_id in sorted(
            set(demanded_by_role) | set(fulfilled_by_role),
            key=lambda item: (-fulfilled_by_role.get(item, 0), item),
        ):
            demanded = demanded_by_role.get(role_id, 0)
            fulfilled = fulfilled_by_role.get(role_id, 0)
            by_role.append(
                {
                    "role_id": role_id,
                    "demanded_effort_hours": self._clean_number(demanded),
                    "fulfilled_effort_hours": self._clean_number(fulfilled),
                }
            )
        time_series = []
        overallocated = []
        for bucket in buckets:
            allocated = bucket["allocated_hours"]
            if allocated <= 0 and bucket["capacity_hours"] <= 0:
                continue
            role_ids = sorted(
                {
                    slice_data["role_id"]
                    for slice_data in slices
                    if slice_data["resource_id"] == bucket["resource_id"]
                    and bucket["starts_at"] < slice_data["ends_at"]
                    and bucket["ends_at"] > slice_data["starts_at"]
                }
            )
            time_series.append(
                {
                    "starts_at": bucket["starts_at"].isoformat(),
                    "ends_at": bucket["ends_at"].isoformat(),
                    "resource_id": bucket["resource_id"],
                    "role_ids": role_ids,
                    "capacity_hours": self._clean_number(bucket["capacity_hours"]),
                    "allocated_hours": self._clean_number(allocated),
                    "utilization_ratio": self._clean_number(
                        allocated / bucket["capacity_hours"]
                        if bucket["capacity_hours"]
                        else 0,
                    ),
                }
            )
            if allocated > bucket["capacity_hours"] + 0.0001:
                overallocated.append(self._bucket_json(bucket))
        return (
            {
                "project_id": query.project_id,
                "as_of": query.as_of.isoformat(),
                "planning_granularity": self._enum_value(query.planning_granularity),
                "by_resource": by_resource,
                "by_role": by_role,
                "time_series": time_series,
                "overallocated_buckets": overallocated,
            },
            warnings,
        )

    def _demanded_effort_by_role(
        self,
        project_id: str,
        as_of: dt.datetime,
        process_ids: set[str] | None = None,
    ) -> dict[str, float]:
        demanded: dict[str, float] = defaultdict(float)
        active_ids = self._repository.active_process_ids_as_of(
            project_id,
            as_of,
        )
        for process_id in active_ids:
            if process_ids is not None and process_id not in process_ids:
                continue
            revision = self._repository.selected_revision_as_of(
                project_id,
                process_id,
                as_of,
            )
            if revision is None:
                continue
            for requirement in revision.role_requirements:
                demanded[requirement.role_id] += float(requirement.effort_hours)
        return demanded

    def _cost_data(self, query: QueryCosts):
        self._validate_resource_filters(
            query.project_id,
            resource_ids=query.resource_ids,
            role_ids=query.role_ids,
            error_for_unknown=True,
        )
        schedule = self._compute_resource_schedule(
            query,
            include_allocation_slices=True,
        )
        warnings = self._resource_warnings(schedule, query.max_iterations)
        horizon_starts_at = self._parse_datetime(schedule["horizon_starts_at"])
        horizon_ends_at = self._parse_datetime(schedule["horizon_ends_at"])
        scoped_ids, _scope_data, _target_id = self._cost_scope_process_ids(query)
        resource_filter = set(query.resource_ids) if query.resource_ids else None
        role_filter = set(query.role_ids) if query.role_ids else None
        slices = []
        for item in schedule["allocation_slices"]:
            slice_data = self._parse_slice(item)
            if slice_data["process_id"] not in scoped_ids:
                continue
            if resource_filter is not None and slice_data["resource_id"] not in resource_filter:
                continue
            if role_filter is not None and slice_data["role_id"] not in role_filter:
                continue
            clipped = self._clip_slice(
                slice_data,
                horizon_starts_at,
                horizon_ends_at,
            )
            if clipped is not None:
                slices.append(clipped)
        resources = getattr(self._repository, "resources", {})
        project = self._repository.get_project(query.project_id)
        currency = query.currency or project.default_currency
        if currency != project.default_currency:
            raise ServiceValidationError(
                code="project_currency_mismatch",
                message="Cost query currency must match the project default currency.",
                field_path="currency",
                details={
                    "project_default_currency": project.default_currency,
                    "requested_currency": currency,
                },
            )
        currency_resources = self._currency_relevant_resources(query, slices)
        resource_currencies = {
            resource_id: resources[resource_id]["cost_currency"]
            for resource_id in currency_resources
            if resource_id in resources
        }
        if any(value != currency for value in resource_currencies.values()):
            raise ServiceValidationError(
                code="mixed_currency",
                message="Cost query cannot mix currencies.",
                details={
                    "requested_currency": currency,
                    "resource_currencies": resource_currencies,
                },
            )
        group_by = {self._enum_value(value) for value in query.group_by}
        cost_entries = self._cost_entries_for_slices(
            slices,
            currency,
            query,
            horizon_starts_at,
            horizon_ends_at,
        )
        total_cost = sum((entry["cost"] for entry in cost_entries), Decimal("0"))
        by_resource = []
        if "resource" in group_by:
            for resource_id in sorted({entry["resource_id"] for entry in cost_entries}):
                entries = [entry for entry in cost_entries if entry["resource_id"] == resource_id]
                by_resource.append(
                    {
                        "resource_id": resource_id,
                        "cost_unit": resources[resource_id]["cost_unit"],
                        "allocated_hours": self._clean_number(
                            sum(entry["hours"] for entry in entries),
                        ),
                        "currency": currency,
                        "cost_amount": self._money(sum(entry["cost"] for entry in entries)),
                    }
                )
        by_process = []
        if "process" in group_by:
            for process_id in sorted({entry["process_id"] for entry in cost_entries}):
                entries = [entry for entry in cost_entries if entry["process_id"] == process_id]
                by_process.append(
                    {
                        "process_id": process_id,
                        "allocated_hours": self._clean_number(
                            sum(entry["hours"] for entry in entries),
                        ),
                        "currency": currency,
                        "cost_amount": self._money(sum(entry["cost"] for entry in entries)),
                    }
                )
        by_role = []
        if "role" in group_by:
            for role_id in sorted({entry["role_id"] for entry in cost_entries}):
                entries = [entry for entry in cost_entries if entry["role_id"] == role_id]
                by_role.append(
                    {
                        "role_id": role_id,
                        "allocated_hours": self._clean_number(
                            sum(entry["hours"] for entry in entries),
                        ),
                        "currency": currency,
                        "cost_amount": self._money(sum(entry["cost"] for entry in entries)),
                    }
                )
        time_series = []
        if "time" in group_by:
            grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
            for entry in cost_entries:
                key = (
                    entry["starts_at"],
                    entry["ends_at"],
                    entry["resource_id"] if "resource" in group_by else None,
                    entry["process_id"] if "process" in group_by else None,
                    entry["role_id"] if "role" in group_by else None,
                )
                row = grouped.setdefault(
                    key,
                    {
                        "starts_at": entry["starts_at"],
                        "ends_at": entry["ends_at"],
                        "resource_id": key[2],
                        "process_id": key[3],
                        "role_id": key[4],
                        "allocated_hours": 0.0,
                        "currency": currency,
                        "cost": Decimal("0"),
                    },
                )
                row["allocated_hours"] += entry["hours"]
                row["cost"] += entry["cost"]
            for key in sorted(
                grouped,
                key=lambda item: (
                    item[2] or "",
                    item[3] or "",
                    item[4] or "",
                    item[0],
                    item[1],
                ),
            ):
                row = grouped[key]
                time_series.append(
                    {
                        "starts_at": row["starts_at"].isoformat(),
                        "ends_at": row["ends_at"].isoformat(),
                        "resource_id": row["resource_id"],
                        "process_id": row["process_id"],
                        "role_id": row["role_id"],
                        "allocated_hours": self._clean_number(row["allocated_hours"]),
                        "currency": currency,
                        "cost_amount": self._money(row["cost"]),
                    }
                )
        return (
            {
                "project_id": query.project_id,
                "as_of": query.as_of.isoformat(),
                "currency": currency,
                "total_cost": self._money(total_cost),
                "by_resource": by_resource,
                "by_process": by_process,
                "by_role": by_role,
                "time_series": time_series,
            },
            warnings,
        )

    def _resource_warnings(
        self,
        schedule: dict[str, object],
        max_iterations: int,
    ) -> list[Warning]:
        warnings = [
            self._warning_from_schedule(item, max_iterations)
            for item in schedule.pop("warnings", []) or []
        ]
        if not schedule.get("converged", True) and not any(
            warning.code == "max_iterations_reached" for warning in warnings
        ):
            warnings.append(
                Warning(
                    code="max_iterations_reached",
                    message="Resource schedule did not converge.",
                    severity=WarningSeverity.WARNING,
                    details={"max_iterations": max_iterations},
                )
            )
        return warnings

    def _warning_from_schedule(
        self,
        item: dict[str, object] | Warning,
        max_iterations: int,
    ) -> Warning:
        if isinstance(item, Warning):
            if item.code == "max_iterations_reached":
                return Warning(
                    code=item.code,
                    message="Resource schedule did not converge.",
                    severity=item.severity,
                    details=item.details or {"max_iterations": max_iterations},
                )
            return item
        code = str(item.get("code", "resource_schedule_warning"))
        details = dict(item.get("details") or {})
        if code == "max_iterations_reached":
            details.setdefault("max_iterations", max_iterations)
            return Warning(
                code=code,
                message="Resource schedule did not converge.",
                severity=WarningSeverity.WARNING,
                details=details,
            )
        return Warning(
            code=code,
            message=str(item.get("message", code.replace("_", " "))),
            severity=item.get("severity", WarningSeverity.WARNING),
            details=details,
        )

    def _cost_scope_process_ids(self, query: QueryCosts):
        scope = query.scope
        if query.target_process_id is not None:
            scope = type(
                "Scope",
                (),
                {
                    "type": "target_process",
                    "process_id": query.target_process_id,
                },
            )()
        if query.target_process_symbol is not None:
            scope = type(
                "Scope",
                (),
                {
                    "type": "target_process",
                    "process_id": None,
                    "process_symbol": query.target_process_symbol,
                },
            )()
        return self._repository.process_ids_for_scope(
            query.project_id,
            query.as_of,
            scope,
        )

    def _cost_entries_for_slices(
        self,
        slices: list[dict[str, Any]],
        currency: str,
        query: QueryCosts,
        horizon_starts_at: dt.datetime,
        horizon_ends_at: dt.datetime,
    ) -> list[dict[str, Any]]:
        resources = getattr(self._repository, "resources", {})
        entries: list[dict[str, Any]] = []
        atoms = self._cost_atoms_for_slices(
            slices,
            query,
            horizon_starts_at,
            horizon_ends_at,
        )
        slices_by_resource: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for slice_data in atoms:
            slices_by_resource[slice_data["resource_id"]].append(slice_data)
        for resource_id, resource_slices in slices_by_resource.items():
            resource = resources[resource_id]
            cost_unit = resource["cost_unit"]
            rate = Decimal(str(resource["cost_rate"]))
            total_hours = sum(float(item["effort_hours"]) for item in resource_slices)
            if total_hours <= 0:
                continue
            if cost_unit == "hour":
                for item in resource_slices:
                    entries.append(
                        self._cost_entry(item, Decimal(str(item["effort_hours"])) * rate)
                    )
                continue
            if cost_unit == "day":
                entries.extend(
                    self._prorated_period_cost_entries(
                        resource_slices,
                        rate,
                        period_key="local_date",
                    )
                )
                continue
            if cost_unit == "week":
                entries.extend(
                    self._prorated_period_cost_entries(
                        resource_slices,
                        rate,
                        period_key="local_week",
                    )
                )
                continue
            if cost_unit == "fixed":
                group_hours = sum(float(item["effort_hours"]) for item in resource_slices)
                if group_hours <= 0:
                    continue
                for item in resource_slices:
                    share = Decimal(str(item["effort_hours"])) / Decimal(str(group_hours))
                    entries.append(self._cost_entry(item, rate * share))
        for entry in entries:
            entry["currency"] = currency
        return entries

    def _cost_atoms_for_slices(
        self,
        slices: list[dict[str, Any]],
        query: QueryCosts,
        horizon_starts_at: dt.datetime,
        horizon_ends_at: dt.datetime,
    ) -> list[dict[str, Any]]:
        if not slices:
            return []
        buckets = self._expanded_capacity(
            query.project_id,
            horizon_starts_at,
            horizon_ends_at,
            sorted({slice_data["resource_id"] for slice_data in slices}),
            None,
        )
        buckets_by_resource: dict[str, list[dict[str, Any]]] = defaultdict(list)
        period_capacity: dict[tuple[str, str, str], float] = defaultdict(float)
        for bucket in buckets:
            buckets_by_resource[bucket["resource_id"]].append(bucket)
            period_capacity[
                (bucket["resource_id"], "local_date", bucket["local_date"])
            ] += float(bucket["capacity_hours"])
            period_capacity[
                (bucket["resource_id"], "local_week", bucket["local_week"])
            ] += float(bucket["capacity_hours"])
        atoms = []
        for slice_data in slices:
            matched = False
            for bucket in buckets_by_resource.get(slice_data["resource_id"], []):
                if (
                    bucket["starts_at"] >= slice_data["ends_at"]
                    or bucket["ends_at"] <= slice_data["starts_at"]
                ):
                    continue
                atom = self._cost_atom_for_overlap(slice_data, bucket)
                if atom is not None:
                    atom["local_date_capacity_hours"] = period_capacity[
                        (atom["resource_id"], "local_date", atom["local_date"])
                    ]
                    atom["local_week_capacity_hours"] = period_capacity[
                        (atom["resource_id"], "local_week", atom["local_week"])
                    ]
                    atoms.append(atom)
                    matched = True
            if not matched:
                atoms.extend(self._fallback_cost_atoms(slice_data))
        return atoms

    def _cost_atom_for_overlap(
        self,
        slice_data: dict[str, Any],
        bucket: dict[str, Any],
    ) -> dict[str, Any] | None:
        starts_at = max(slice_data["starts_at"], bucket["starts_at"])
        ends_at = min(slice_data["ends_at"], bucket["ends_at"])
        if ends_at <= starts_at:
            return None
        slice_hours = self._overlap_hours(
            slice_data["starts_at"],
            slice_data["ends_at"],
            slice_data["starts_at"],
            slice_data["ends_at"],
        )
        bucket_hours = self._overlap_hours(
            bucket["starts_at"],
            bucket["ends_at"],
            bucket["starts_at"],
            bucket["ends_at"],
        )
        overlap_hours = self._overlap_hours(
            starts_at,
            ends_at,
            starts_at,
            ends_at,
        )
        if slice_hours <= 0 or bucket_hours <= 0:
            return None
        atom = dict(slice_data)
        atom["starts_at"] = starts_at
        atom["ends_at"] = ends_at
        atom["effort_hours"] = float(slice_data["effort_hours"]) * overlap_hours / slice_hours
        atom["capacity_hours"] = float(bucket["capacity_hours"]) * overlap_hours / bucket_hours
        atom["local_date"] = bucket["local_date"]
        atom["local_week"] = bucket["local_week"]
        return atom

    def _fallback_cost_atoms(self, slice_data: dict[str, Any]) -> list[dict[str, Any]]:
        resources = getattr(self._repository, "resources", {})
        calendars = getattr(self._repository, "calendars", {})
        resource = resources.get(slice_data["resource_id"], {})
        calendar = calendars.get(resource.get("calendar_id"), {})
        timezone_name = calendar.get("timezone", "UTC")
        timezone = ZoneInfo(str(timezone_name))
        atoms = []
        for atom in self._split_slice_to_hours(slice_data):
            local_start = atom["starts_at"].astimezone(timezone)
            iso = local_start.isocalendar()
            atom["local_date"] = local_start.date().isoformat()
            atom["local_week"] = f"{iso.year}-W{iso.week:02d}"
            atom["capacity_hours"] = atom["effort_hours"]
            atom["local_date_capacity_hours"] = atom["effort_hours"]
            atom["local_week_capacity_hours"] = atom["effort_hours"]
            atoms.append(atom)
        return atoms

    def _prorated_period_cost_entries(
        self,
        slices: list[dict[str, Any]],
        rate: Decimal,
        *,
        period_key: str,
    ) -> list[dict[str, Any]]:
        entries = []
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in slices:
            groups[str(item[period_key])].append(item)
        for grouped_slices in groups.values():
            capacity_key = f"{period_key}_capacity_hours"
            group_capacity = float(
                grouped_slices[0].get(
                    capacity_key,
                    sum(float(item["capacity_hours"]) for item in grouped_slices),
                )
            )
            if group_capacity <= 0:
                continue
            for item in grouped_slices:
                share = Decimal(str(item["effort_hours"])) / Decimal(str(group_capacity))
                entries.append(self._cost_entry(item, rate * share))
        return entries

    def _cost_entry(
        self,
        slice_data: dict[str, Any],
        cost: Decimal,
    ) -> dict[str, Any]:
        return {
            "starts_at": slice_data["starts_at"],
            "ends_at": slice_data["ends_at"],
            "resource_id": slice_data["resource_id"],
            "process_id": slice_data["process_id"],
            "role_id": slice_data["role_id"],
            "hours": float(slice_data["effort_hours"]),
            "cost": cost,
        }

    def _currency_relevant_resources(
        self,
        query: QueryCosts,
        slices: list[dict[str, Any]],
    ) -> set[str]:
        resources = getattr(self._repository, "resources", {})
        if query.resource_ids is not None:
            return set(query.resource_ids)
        role_ids = set(query.role_ids or [])
        if not role_ids:
            role_ids = {slice_data["role_id"] for slice_data in slices}
        relevant = {slice_data["resource_id"] for slice_data in slices}
        for resource_id, resource in resources.items():
            if resource["project_id"] != query.project_id or not resource["active"]:
                continue
            if role_ids.intersection(resource["role_ids"]):
                relevant.add(resource_id)
        return relevant

    def _validate_resource_filters(
        self,
        project_id: str,
        *,
        resource_ids: list[str] | None,
        role_ids: list[str] | None,
        error_for_unknown: bool,
    ) -> None:
        resources = getattr(self._repository, "resources", {})
        roles = getattr(self._repository, "roles", {})
        for resource_id in resource_ids or []:
            resource = resources.get(resource_id)
            if resource is None or resource["project_id"] != project_id:
                if error_for_unknown:
                    raise ServiceValidationError(
                        code="not_found",
                        message="Resource filter id was not found.",
                        details={
                            "entity_type": "resource",
                            "entity_id": resource_id,
                            "field": "resource_ids",
                        },
                    )
                raise ServiceValidationError(
                    code="resource_not_found",
                    message=f"Resource {resource_id!r} does not exist.",
                    entity_id=resource_id,
                )
        for role_id in role_ids or []:
            role = roles.get(role_id)
            if role is None or role["project_id"] != project_id:
                if error_for_unknown:
                    raise ServiceValidationError(
                        code="not_found",
                        message="Role filter id was not found.",
                        details={
                            "entity_type": "role",
                            "entity_id": role_id,
                            "field": "role_ids",
                        },
                    )
                raise ServiceValidationError(
                    code="role_not_found",
                    message=f"Role {role_id!r} does not exist.",
                    entity_id=role_id,
                )

    def _parse_slice(self, item: dict[str, Any]) -> dict[str, Any]:
        data = dict(item)
        if isinstance(data["starts_at"], str):
            data["starts_at"] = dt.datetime.fromisoformat(data["starts_at"])
        if isinstance(data["ends_at"], str):
            data["ends_at"] = dt.datetime.fromisoformat(data["ends_at"])
        data["effort_hours"] = float(data["effort_hours"])
        data["capacity_hours"] = float(data.get("capacity_hours", data["effort_hours"]))
        return data

    def _clip_slice(
        self,
        slice_data: dict[str, Any],
        starts_at: dt.datetime,
        ends_at: dt.datetime,
    ) -> dict[str, Any] | None:
        clipped_start = max(slice_data["starts_at"], starts_at)
        clipped_end = min(slice_data["ends_at"], ends_at)
        if clipped_end <= clipped_start:
            return None
        clipped_start = clipped_start.astimezone(starts_at.tzinfo)
        clipped_end = clipped_end.astimezone(starts_at.tzinfo)
        original_hours = (
            slice_data["ends_at"] - slice_data["starts_at"]
        ).total_seconds() / 3600
        clipped_hours = (clipped_end - clipped_start).total_seconds() / 3600
        effort = float(slice_data["effort_hours"]) * clipped_hours / original_hours
        clipped = dict(slice_data)
        clipped["starts_at"] = clipped_start
        clipped["ends_at"] = clipped_end
        clipped["effort_hours"] = effort
        clipped["capacity_hours"] = effort
        return clipped

    def _split_slice_to_hours(
        self,
        slice_data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        parts = []
        current = slice_data["starts_at"]
        while current < slice_data["ends_at"]:
            next_at = min(current + dt.timedelta(hours=1), slice_data["ends_at"])
            hours = (next_at - current).total_seconds() / 3600
            part = dict(slice_data)
            part["starts_at"] = current
            part["ends_at"] = next_at
            part["effort_hours"] = hours
            part["capacity_hours"] = hours
            parts.append(part)
            current = next_at
        return parts

    def _overlap_hours(
        self,
        first_start: dt.datetime,
        first_end: dt.datetime,
        second_start: dt.datetime,
        second_end: dt.datetime,
    ) -> float:
        starts_at = max(first_start, second_start)
        ends_at = min(first_end, second_end)
        if ends_at <= starts_at:
            return 0
        return (ends_at - starts_at).total_seconds() / 3600

    def _bucket_json(self, bucket: dict[str, Any]) -> dict[str, object]:
        return {
            "resource_id": bucket["resource_id"],
            "calendar_id": bucket["calendar_id"],
            "starts_at": bucket["starts_at"].isoformat(),
            "ends_at": bucket["ends_at"].isoformat(),
            "capacity_hours": self._clean_number(bucket["capacity_hours"]),
            "available_hours": self._clean_number(bucket["available_hours"]),
            "allocated_hours": self._clean_number(bucket["allocated_hours"]),
            "remaining_hours": self._clean_number(bucket["remaining_hours"]),
            "role_ids": bucket["role_ids"],
            "local_date": bucket["local_date"],
            "local_week": bucket["local_week"],
        }

    def _enum_value(self, value):
        return getattr(value, "value", value)

    def _role_requirements_json(
        self,
        requirements,
        *,
        process_id: str | None = None,
        pin_summary: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        output = []
        pin_requirements = (
            pin_summary.get("requirements", {}) if pin_summary is not None else {}
        )
        for index, requirement in enumerate(requirements):
            requirement_id = requirement.requirement_id
            resolved_requirement_id = requirement_id or (
                f"{process_id}-requirement-{index + 1}" if process_id else None
            )
            pin_row = (
                pin_requirements.get(resolved_requirement_id, {})
                if resolved_requirement_id is not None
                else {}
            )
            pins = list(pin_row.get("pins") or [])
            active_pin_resource_ids = list(pin_row.get("active_resource_ids") or [])
            recent_pin_resource_ids = list(
                dict.fromkeys(
                    [
                        *active_pin_resource_ids,
                        *list(pin_row.get("done_resource_ids") or []),
                        *list(pin_row.get("preferred_resource_ids") or []),
                    ]
                )
            )
            pin_status = "planned"
            active_pins = [
                pin
                for pin in pins
                if isinstance(pin, dict) and pin.get("status") == "pinned_started"
            ]
            if active_pins:
                pin_status = (
                    "due"
                    if any(bool(pin.get("due")) for pin in active_pins)
                    else "pinned_started"
                )
            elif pin_row.get("done_resource_ids"):
                pin_status = "pinned_finished"
            item = {
                "requirement_id": resolved_requirement_id,
                "role_id": requirement.role_id,
                "effort_hours": self._clean_number(requirement.effort_hours),
                "pin_status": pin_status,
                "active_pinned_resource_ids": active_pin_resource_ids,
                "recent_pinned_resource_ids": recent_pin_resource_ids,
                "pins": pins,
                "required_resource_count": requirement.required_resource_count,
                "allocation_policy": self._enum_value(requirement.allocation_policy),
                "min_allocation_hours_per_day": (
                    self._clean_number(requirement.min_allocation_hours_per_day)
                    if requirement.min_allocation_hours_per_day is not None
                    else None
                ),
                "max_allocation_hours_per_day": (
                    self._clean_number(requirement.max_allocation_hours_per_day)
                    if requirement.max_allocation_hours_per_day is not None
                    else None
                ),
            }
            if pin_row.get("teammate_work_plan_forecasts"):
                item["teammate_work_plan_forecasts"] = list(
                    pin_row.get("teammate_work_plan_forecasts") or []
                )
            output.append(item)
        return output

    def _clean_number(self, value):
        value = float(value)
        if abs(value - round(value)) < 0.000001:
            return int(round(value))
        return round(value, 6)

    def _money(self, value: Decimal) -> str:
        return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def _handle_batch_update_process_graph(
        self,
        envelope: CommandEnvelope,
        command: BatchUpdateProcessGraph,
    ) -> CommandResult:
        staged = self._repository.clone()
        operation_results: list[BatchOperationResult] = []
        entity_ids: dict[str, object] = {"operation_ids": operation_results}
        resource_ids: list[str] = []
        process_ids: list[str] = []
        revision_ids: list[str] = []
        requirement_ids: list[str] = []
        edge_ids: list[str] = []
        candidate_dependencies: dict[str, list[str]] = {}
        candidate_requirements: dict[str, list[Any]] = {}
        original_requirements: dict[str, list[Any]] = {}
        requirement_process_by_op: dict[int, str] = {}
        dependency_successors_changed: set[str] = set()
        candidate_edge_ids: dict[tuple[str, str, str], str] = dict(
            getattr(staged, "dependency_edge_ids", {})
        )
        active_process_ids = staged.active_process_ids_as_of(
            command.project_id,
            command.edit_at,
        )

        def revision_for(process_id: str):
            revision = staged.selected_revision_as_of(
                command.project_id,
                process_id,
                command.edit_at,
            )
            if revision is None:
                raise ServiceValidationError(
                    code="process_not_found",
                    message=f"Process {process_id!r} does not have an active revision.",
                    entity_id=process_id,
                )
            return revision

        def deps_for(process_id: str) -> list[str]:
            if process_id not in candidate_dependencies:
                candidate_dependencies[process_id] = list(revision_for(process_id).dependencies)
            return candidate_dependencies[process_id]

        def reqs_for(process_id: str) -> list[Any]:
            if process_id not in candidate_requirements:
                original = list(revision_for(process_id).role_requirements)
                original_requirements[process_id] = original
                candidate_requirements[process_id] = list(original)
            return candidate_requirements[process_id]

        def validation_error(index: int, field: str, message: str, issue_type: str):
            raise ServiceValidationError(
                code="validation_error",
                message=message,
                validation_errors=[
                    ValidationIssue(
                        loc=["command", "operations", index, field],
                        msg=message,
                        type=issue_type,
                        ctx={},
                    )
                ],
            )

        def resolve_identity(index: int, operation, prefix: str = "") -> str:
            process_id = getattr(operation, f"{prefix}process_id", None)
            process_symbol = getattr(operation, f"{prefix}process_symbol", None)
            try:
                return self._resolve_process_id_in(
                    staged,
                    project_id=command.project_id,
                    process_id=process_id,
                    process_symbol=process_symbol,
                )
            except ServiceValidationError as exc:
                if exc.code in {"process_not_found", "not_found"}:
                    validation_error(
                        index,
                        f"{prefix}process_id",
                        "Process reference was not found.",
                        "process_reference",
                    )
                raise

        pending_added_requirements: dict[tuple[str, str], int] = {}
        pending_removed_requirements: dict[tuple[str, str], tuple[int, Any]] = {}
        for index, operation in enumerate(command.operations):
            operation_id = operation.operation_id or f"operation-{index + 1}"
            if isinstance(operation, AddDependencyOperation):
                predecessor_id = resolve_identity(index, operation, "predecessor_")
                successor_id = resolve_identity(index, operation, "successor_")
                if predecessor_id == successor_id:
                    validation_error(
                        index,
                        "successor_process_id",
                        "A process cannot depend on itself.",
                        "self_dependency",
                    )
                blocker = self._blocker_for_resolver_dependency(
                    project_id=command.project_id,
                    predecessor_id=predecessor_id,
                    as_of=command.edit_at,
                )
                if blocker is not None and blocker.process_id != successor_id:
                    validation_error(
                        index,
                        "successor_process_id",
                        "A blocker resolver dependency cannot be shared across processes.",
                        "blocker_resolver_dependency_shared",
                    )
                deps = deps_for(successor_id)
                key = (command.project_id, predecessor_id, successor_id)
                existing_edge_id = candidate_edge_ids.get(
                    key,
                    f"edge-{predecessor_id}-{successor_id}",
                )
                requested_edge_id = operation.edge_id or existing_edge_id
                for edge_key, edge_id in candidate_edge_ids.items():
                    if edge_id == requested_edge_id and edge_key != key:
                        validation_error(
                            index,
                            "edge_id",
                            "Dependency edge id is already used.",
                            "edge_id_collision",
                        )
                if predecessor_id in deps:
                    edge_ids.append(existing_edge_id)
                    operation_results.append(
                        BatchOperationResult(
                            operation_index=index,
                            operation_id=operation_id,
                            action=operation.action,
                            status="no_op",
                            edge_ids=[existing_edge_id],
                            matched_ids=MatchedIds(edge_ids=[existing_edge_id]),
                            no_op_reason="dependency_already_present",
                        )
                    )
                    continue
                deps.append(predecessor_id)
                candidate_edge_ids[key] = requested_edge_id
                edge_ids.append(requested_edge_id)
                dependency_successors_changed.add(successor_id)
                operation_results.append(
                    BatchOperationResult(
                        operation_index=index,
                        operation_id=operation_id,
                        action=operation.action,
                        status="applied",
                        edge_ids=[requested_edge_id],
                        created_ids=CreatedIds(edge_ids=[requested_edge_id]),
                    )
                )
                continue
            if isinstance(operation, RemoveDependencyOperation):
                if operation.edge_id is not None:
                    matches = [
                        key
                        for key, value in candidate_edge_ids.items()
                        if value == operation.edge_id and key[0] == command.project_id
                    ]
                    if not matches:
                        raise ServiceValidationError(
                            code="not_found",
                            message="Dependency edge id was not found.",
                            entity_id=operation.edge_id,
                        )
                    _project_id, predecessor_id, successor_id = matches[0]
                else:
                    predecessor_id = resolve_identity(index, operation, "predecessor_")
                    successor_id = resolve_identity(index, operation, "successor_")
                deps = deps_for(successor_id)
                key = (command.project_id, predecessor_id, successor_id)
                removed_edge_id = candidate_edge_ids.get(
                    key,
                    f"edge-{predecessor_id}-{successor_id}",
                )
                if predecessor_id not in deps:
                    operation_results.append(
                        BatchOperationResult(
                            operation_index=index,
                            operation_id=operation_id,
                            action=operation.action,
                            status="no_op",
                            edge_ids=[],
                            no_op_reason="dependency_already_absent",
                        )
                    )
                    continue
                if self._is_blocker_reference_dependency(
                    project_id=command.project_id,
                    successor_id=successor_id,
                    predecessor_id=predecessor_id,
                    as_of=command.edit_at,
                ):
                    operation_results.append(
                        BatchOperationResult(
                            operation_index=index,
                            operation_id=operation_id,
                            action=operation.action,
                            status="no_op",
                            edge_ids=[removed_edge_id],
                            matched_ids=MatchedIds(edge_ids=[removed_edge_id]),
                            no_op_reason="blocker_reference_dependency_required",
                        )
                    )
                    continue
                deps.remove(predecessor_id)
                candidate_edge_ids.pop(key, None)
                edge_ids.append(removed_edge_id)
                dependency_successors_changed.add(successor_id)
                operation_results.append(
                    BatchOperationResult(
                        operation_index=index,
                        operation_id=operation_id,
                        action=operation.action,
                        status="applied",
                        edge_ids=[removed_edge_id],
                        removed_ids={"edge_ids": [removed_edge_id]},
                    )
                )
                continue
            if isinstance(operation, AddRoleRequirementOperation):
                process_id = resolve_identity(index, operation)
                staged._validate_active_role_requirements(
                    command.project_id,
                    [operation.requirement],
                )
                req_id = operation.requirement.requirement_id or new_id()
                req = operation.requirement.model_copy(
                    update={"requirement_id": req_id}
                )
                reqs = reqs_for(process_id)
                if not self._is_default_missing_process_requirement(process_id, req):
                    reqs[:] = [
                        item
                        for item in reqs
                        if not self._is_default_missing_process_requirement(
                            process_id,
                            item,
                        )
                    ]
                existing = next(
                    (item for item in reqs if item.requirement_id == req_id),
                    None,
                )
                key = (process_id, req_id)
                if existing is not None:
                    if not self._role_requirement_equal(existing, req):
                        validation_error(
                            index,
                            "requirement",
                            "Requirement id already exists with different fields.",
                            "requirement_conflict",
                        )
                    operation_results.append(
                        BatchOperationResult(
                            operation_index=index,
                            operation_id=operation_id,
                            action=operation.action,
                            status="no_op",
                            requirement_ids=[req_id],
                            matched_ids=MatchedIds(requirement_ids=[req_id]),
                            no_op_reason="requirement_already_present",
                        )
                    )
                    requirement_process_by_op[index] = process_id
                    requirement_ids.append(req_id)
                    continue
                removed = pending_removed_requirements.get(key)
                if removed is not None:
                    remove_index, original = removed
                    if not self._role_requirement_equal(original, req):
                        validation_error(
                            index,
                            "requirement",
                            "Removed requirement cannot be re-added with changes.",
                            "requirement_conflict",
                        )
                    reqs.append(original)
                    for op_result in operation_results:
                        if op_result.operation_index == remove_index:
                            op_result.status = "validated_only"
                            op_result.validation_reason = "candidate_remove_then_readd"
                    operation_results.append(
                        BatchOperationResult(
                            operation_index=index,
                            operation_id=operation_id,
                            action=operation.action,
                            status="validated_only",
                            revision_id=None,
                            requirement_ids=[req_id],
                            validation_reason="candidate_remove_then_readd",
                        )
                    )
                    requirement_process_by_op[index] = process_id
                    requirement_ids.append(req_id)
                    continue
                reqs.append(req)
                pending_added_requirements[key] = index
                operation_results.append(
                    BatchOperationResult(
                        operation_index=index,
                        operation_id=operation_id,
                        action=operation.action,
                        status="applied",
                        requirement_ids=[req_id],
                        created_ids=CreatedIds(requirement_ids=[req_id]),
                    )
                )
                requirement_process_by_op[index] = process_id
                requirement_ids.append(req_id)
                continue
            if isinstance(operation, RemoveRoleRequirementOperation):
                process_id = resolve_identity(index, operation)
                req_id = operation.requirement_id
                reqs = reqs_for(process_id)
                existing = next(
                    (item for item in reqs if item.requirement_id == req_id),
                    None,
                )
                if existing is None:
                    operation_results.append(
                        BatchOperationResult(
                            operation_index=index,
                            operation_id=operation_id,
                            action=operation.action,
                            status="no_op",
                            revision_id=None,
                            requirement_ids=[req_id],
                            no_op_reason="requirement_already_absent",
                        )
                    )
                    requirement_process_by_op[index] = process_id
                    continue
                reqs.remove(existing)
                key = (process_id, req_id)
                added_index = pending_added_requirements.get(key)
                if added_index is not None:
                    for op_result in operation_results:
                        if op_result.operation_index == added_index:
                            op_result.status = "validated_only"
                            op_result.validation_reason = "candidate_add_then_remove"
                            op_result.candidate_only_ids.requirement_ids.append(req_id)
                    operation_results.append(
                        BatchOperationResult(
                            operation_index=index,
                            operation_id=operation_id,
                            action=operation.action,
                            status="validated_only",
                            requirement_ids=[req_id],
                            candidate_only_ids={"requirement_ids": [req_id]},
                            validation_reason="candidate_add_then_remove",
                        )
                    )
                    requirement_process_by_op[index] = process_id
                    continue
                pending_removed_requirements[key] = (index, existing)
                operation_results.append(
                    BatchOperationResult(
                        operation_index=index,
                        operation_id=operation_id,
                        action=operation.action,
                        status="applied",
                        requirement_ids=[req_id],
                        removed_ids={"requirement_ids": [req_id]},
                    )
                )
                requirement_process_by_op[index] = process_id
                requirement_ids.append(req_id)
                continue
            if isinstance(operation, UpsertResourceOperation):
                self._validate_resource_role_ids(
                    role_ids=operation.resource.role_ids,
                    active=operation.resource.active,
                )
                cost_currency = self._resource_project_currency(
                    staged,
                    command.project_id,
                    operation.resource.cost_currency,
                )
                self._validate_batch_resource_references(
                    staged,
                    command.project_id,
                    operation,
                    index,
                )
                calendar_id = self._resource_calendar_for_upsert(
                    staged,
                    command.project_id,
                    resource_type=operation.resource.resource_type,
                    calendar_id=operation.resource.calendar_id,
                )
                resource_preexisted = self._resource_exists(
                    staged,
                    command.project_id,
                    operation.resource.resource_id,
                )
                resource_already_equivalent = self._resource_equivalent(
                    staged,
                    command.project_id,
                    operation.resource,
                )
                if resource_already_equivalent:
                    resource_id = operation.resource.resource_id
                else:
                    resource_id = staged.upsert_resource(
                        project_id=command.project_id,
                        resource_id=operation.resource.resource_id,
                        name=operation.resource.name,
                        resource_type=operation.resource.resource_type,
                        role_ids=operation.resource.role_ids,
                        calendar_id=calendar_id,
                        available_from_at=operation.resource.available_from_at,
                        available_until_at=operation.resource.available_until_at,
                        cost_rate=operation.resource.cost_rate,
                        cost_unit=operation.resource.cost_unit,
                        cost_currency=cost_currency,
                        holidays=operation.resource.holidays,
                        calendar_overrides=operation.resource.calendar_overrides,
                        active=operation.resource.active,
                    )
                resource_ids.append(resource_id)
                created_ids = CreatedIds()
                matched_ids = MatchedIds()
                if resource_preexisted:
                    matched_ids.resource_ids.append(resource_id)
                else:
                    created_ids.resource_ids.append(resource_id)
                operation_results.append(
                    BatchOperationResult(
                        operation_index=index,
                        operation_id=operation_id,
                        action=operation.action,
                        status=(
                            "no_op" if resource_already_equivalent else "applied"
                        ),
                        created_ids=created_ids,
                        matched_ids=matched_ids,
                        no_op_reason=(
                            "resource_already_equivalent"
                            if resource_already_equivalent
                            else None
                        ),
                    )
                )
                continue
            if isinstance(operation, SetResourceRolesOperation):
                self._validate_batch_set_resource_references(
                    staged,
                    command.project_id,
                    operation.resource_id,
                    role_ids=operation.role_ids,
                    calendar_id=None,
                    index=index,
                )
                resource_roles_already_set = self._resource_roles_equal(
                    staged,
                    command.project_id,
                    operation.resource_id,
                    operation.role_ids,
                )
                staged.set_resource_roles(
                    project_id=command.project_id,
                    resource_id=operation.resource_id,
                    role_ids=operation.role_ids,
                )
                resource_ids.append(operation.resource_id)
                operation_results.append(
                    BatchOperationResult(
                        operation_index=index,
                        operation_id=operation_id,
                        action=operation.action,
                        status=(
                            "no_op" if resource_roles_already_set else "applied"
                        ),
                        matched_ids=MatchedIds(
                            resource_ids=[operation.resource_id],
                        ),
                        no_op_reason=(
                            "resource_roles_already_set"
                            if resource_roles_already_set
                            else None
                        ),
                    )
                )
                continue
            if isinstance(operation, SetResourceCalendarOperation):
                self._validate_batch_set_resource_references(
                    staged,
                    command.project_id,
                    operation.resource_id,
                    role_ids=None,
                    calendar_id=operation.calendar_id,
                    index=index,
                )
                resource_calendar_already_set = self._resource_calendar_equal(
                    staged,
                    command.project_id,
                    operation.resource_id,
                    operation.calendar_id,
                )
                staged.set_resource_calendar(
                    project_id=command.project_id,
                    resource_id=operation.resource_id,
                    calendar_id=operation.calendar_id,
                )
                resource_ids.append(operation.resource_id)
                operation_results.append(
                    BatchOperationResult(
                        operation_index=index,
                        operation_id=operation_id,
                        action=operation.action,
                        status=(
                            "no_op" if resource_calendar_already_set else "applied"
                        ),
                        matched_ids=MatchedIds(
                            resource_ids=[operation.resource_id],
                            calendar_ids=[operation.calendar_id],
                        ),
                        no_op_reason=(
                            "resource_calendar_already_set"
                            if resource_calendar_already_set
                            else None
                        ),
                    )
                )
                continue
            raise ServiceValidationError(
                code="unsupported_command",
                message=f"Unsupported batch operation type: {type(operation)!r}",
            )
        self._validate_candidate_dependency_graph(
            staged,
            command.project_id,
            command.edit_at,
            active_process_ids,
            candidate_dependencies,
            command.operations,
        )
        for successor_id in sorted(dependency_successors_changed):
            revision = revision_for(successor_id)
            deps = candidate_dependencies[successor_id]
            if list(revision.dependencies) == deps:
                continue
            staged.revisions_by_process[successor_id].append(
                revision.model_copy(
                    update={
                        "revision_id": new_id(),
                        "effective_at": command.edit_at,
                        "dependencies": list(dict.fromkeys(deps)),
                    }
                )
            )
            process_ids.append(successor_id)
        for process_id, reqs in candidate_requirements.items():
            original = original_requirements[process_id]
            if self._role_requirement_lists_equal(original, reqs):
                final_revision_id = revision_for(process_id).revision_id
            else:
                revision = revision_for(process_id)
                final_revision_id = new_id()
                staged.revisions_by_process[process_id].append(
                    revision.model_copy(
                        update={
                            "revision_id": final_revision_id,
                            "effective_at": command.edit_at,
                            "role_requirements": list(reqs),
                        }
                    )
                )
                process_ids.append(process_id)
                revision_ids.append(final_revision_id)
                for requirement in reqs:
                    if requirement.requirement_id is not None:
                        staged.role_requirements[requirement.requirement_id] = requirement
                final_requirement_ids = {
                    requirement.requirement_id
                    for requirement in reqs
                    if requirement.requirement_id is not None
                }
                for requirement in original:
                    if (
                        requirement.requirement_id is not None
                        and requirement.requirement_id not in final_requirement_ids
                    ):
                        staged.role_requirements.pop(requirement.requirement_id, None)
            for op_index, op_process_id in requirement_process_by_op.items():
                if op_process_id != process_id:
                    continue
                operation_results[op_index].revision_id = final_revision_id
                operation_results[op_index].matched_ids.revision_ids = [
                    final_revision_id
                ]
        staged.dependency_edge_ids = candidate_edge_ids
        self._repository.replace_with(staged)
        valid_requirement_ids = {
            requirement.requirement_id
            for reqs in candidate_requirements.values()
            for requirement in reqs
            if requirement.requirement_id is not None
        }
        for reqs in original_requirements.values():
            valid_requirement_ids.update(
                requirement.requirement_id
                for requirement in reqs
                if requirement.requirement_id is not None
            )
        requirement_ids = [
            requirement_id
            for requirement_id in requirement_ids
            if requirement_id in valid_requirement_ids
        ]
        entity_ids["process_ids"] = list(dict.fromkeys(process_ids))
        entity_ids["revision_ids"] = list(dict.fromkeys(revision_ids))
        entity_ids["requirement_ids"] = list(dict.fromkeys(requirement_ids))
        entity_ids["edge_ids"] = list(dict.fromkeys(edge_ids))
        if resource_ids:
            entity_ids["resource_ids"] = list(dict.fromkeys(resource_ids))
        entity_ids["operation_ids"] = [
            operation_result.model_dump(mode="json")
            for operation_result in operation_results
        ]
        return CommandResult(command_id=envelope.command_id, entity_ids=entity_ids)

    def _resolve_process_id(
        self,
        *,
        project_id: str,
        process_id: str | None,
        process_symbol: str | None,
    ) -> str:
        if process_id is not None:
            return process_id
        if process_symbol is None:
            raise ServiceValidationError(
                code="validation_error",
                message="Exactly one of process_id or process_symbol is required.",
                field_path="process_id",
            )
        resolver = getattr(self._repository, "resolve_process_id", None)
        if resolver is not None:
            return resolver(project_id, process_symbol)
        processes = getattr(self._repository, "processes", None)
        process_ids_by_project = getattr(self._repository, "process_ids_by_project", None)
        if isinstance(processes, dict) and process_ids_by_project is not None:
            matches = [
                candidate_id
                for candidate_id in process_ids_by_project.get(project_id, [])
                if getattr(processes[candidate_id], "symbol", None) == process_symbol
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ServiceValidationError(
                    code="ambiguous_process_symbol",
                    message="Process symbol resolves to multiple active processes.",
                    field_path="process_symbol",
                    entity_id=process_symbol,
                )
            raise ServiceValidationError(
                code="process_not_found",
                message=f"Process symbol {process_symbol!r} does not exist.",
                field_path="process_symbol",
                entity_id=process_symbol,
            )
        raise ServiceValidationError(
            code="unsupported_command",
            message="Repository does not support process symbol resolution.",
            field_path="process_symbol",
        )

    def _resolve_process_id_in(
        self,
        repository,
        *,
        project_id: str,
        process_id: str | None,
        process_symbol: str | None,
    ) -> str:
        if process_id is not None:
            repository.selected_revision_as_of(
                project_id,
                process_id,
                dt.datetime.max.replace(tzinfo=dt.UTC),
            )
            return process_id
        if process_symbol is None:
            raise ServiceValidationError(
                code="validation_error",
                message="Exactly one process identity is required.",
                field_path="process_id",
            )
        return repository.resolve_process_id(project_id, process_symbol)

    def _role_requirement_equal(self, left, right) -> bool:
        return left.model_dump(mode="json") == right.model_dump(mode="json")

    def _role_requirement_lists_equal(self, left: list[Any], right: list[Any]) -> bool:
        return [item.model_dump(mode="json") for item in left] == [
            item.model_dump(mode="json") for item in right
        ]

    def _is_default_missing_process_requirement(self, process_id: str, requirement) -> bool:
        role_id = getattr(requirement, "role_id", None)
        role = getattr(self._repository, "roles", {}).get(role_id)
        return (
            role is not None
            and role.get("name") == "Josh"
            and getattr(requirement, "requirement_id", None) == f"{process_id}-{role_id}"
        )

    def _validate_batch_resource_references(
        self,
        repository,
        project_id: str,
        operation: UpsertResourceOperation,
        index: int,
    ) -> None:
        self._validate_batch_set_resource_references(
            repository,
            project_id,
            operation.resource.resource_id,
            role_ids=operation.resource.role_ids,
            calendar_id=operation.resource.calendar_id,
            calendar_ids=[
                override.calendar_id
                for override in operation.resource.calendar_overrides
            ],
            index=index,
            resource_field=True,
        )

    def _validate_batch_set_resource_references(
        self,
        repository,
        project_id: str,
        resource_id: str | None,
        *,
        role_ids: list[str] | None,
        calendar_id: str | None,
        index: int,
        resource_field: bool = False,
        calendar_ids: list[str] | None = None,
    ) -> None:
        roles = getattr(repository, "roles", {})
        calendars = getattr(repository, "calendars", {})
        resources = getattr(repository, "resources", {})
        loc_prefix = ["command", "operations", index]
        if resource_field:
            loc_prefix.append("resource")
        elif resource_id is not None and resource_id not in resources:
            raise ServiceValidationError(
                code="validation_error",
                message="Resource id was not found.",
                validation_errors=[
                    ValidationIssue(
                        loc=[*loc_prefix, "resource_id"],
                        msg="Resource id was not found.",
                        type="resource_reference",
                        ctx={},
                    )
                ],
            )
        for role_id in role_ids or []:
            role = roles.get(role_id)
            if role is None or role["project_id"] != project_id or not role["active"]:
                raise ServiceValidationError(
                    code="validation_error",
                    message="Role id was not found.",
                    validation_errors=[
                        ValidationIssue(
                            loc=[*loc_prefix, "role_ids"],
                            msg="Role id was not found.",
                            type="role_reference",
                            ctx={},
                        )
                    ],
                )
        for calendar_field, candidate_calendar_id in [
            ("calendar_id", calendar_id),
            *[
                ("calendar_overrides", override_calendar_id)
                for override_calendar_id in calendar_ids or []
            ],
        ]:
            if candidate_calendar_id is None:
                continue
            calendar = calendars.get(candidate_calendar_id)
            if (
                calendar is None
                or calendar["project_id"] != project_id
                or not calendar["active"]
            ):
                raise ServiceValidationError(
                    code="validation_error",
                    message="Calendar id was not found.",
                    validation_errors=[
                        ValidationIssue(
                            loc=[*loc_prefix, calendar_field],
                            msg="Calendar id was not found.",
                            type="calendar_reference",
                            ctx={},
                        )
                    ],
                )

    def _validate_candidate_dependency_graph(
        self,
        repository,
        project_id: str,
        edit_at: dt.datetime,
        active_process_ids: list[str],
        candidate_dependencies: dict[str, list[str]],
        operations: list[Any],
    ) -> None:
        graph = nx.DiGraph()
        graph.add_nodes_from(active_process_ids)
        revisions = {}
        for process_id in active_process_ids:
            revision = repository.selected_revision_as_of(project_id, process_id, edit_at)
            if revision is not None:
                revisions[process_id] = revision
        for process_id in active_process_ids:
            deps = candidate_dependencies.get(
                process_id,
                list(revisions[process_id].dependencies),
            )
            for dependency_id in deps:
                if dependency_id in active_process_ids:
                    graph.add_edge(dependency_id, process_id)
        if nx.is_directed_acyclic_graph(graph):
            return
        for index, operation in enumerate(operations):
            if not isinstance(operation, AddDependencyOperation):
                continue
            predecessor_id = self._resolve_process_id_in(
                repository,
                project_id=project_id,
                process_id=operation.predecessor_process_id,
                process_symbol=operation.predecessor_process_symbol,
            )
            successor_id = self._resolve_process_id_in(
                repository,
                project_id=project_id,
                process_id=operation.successor_process_id,
                process_symbol=operation.successor_process_symbol,
            )
            graph_without_edge = graph.copy()
            graph_without_edge.remove_edge(predecessor_id, successor_id)
            if nx.has_path(graph_without_edge, successor_id, predecessor_id):
                cycle_ids = nx.shortest_path(
                    graph_without_edge,
                    successor_id,
                    predecessor_id,
                ) + [successor_id]
                processes = getattr(repository, "processes", {})
                cycle_symbols = [
                    getattr(processes.get(process_id), "symbol", process_id)
                    for process_id in cycle_ids
                ]
                predecessor = processes.get(predecessor_id)
                successor = processes.get(successor_id)
                raise ServiceValidationError(
                    code="dependency_cycle",
                    message="Dependency update would create a process cycle.",
                    details={
                        "operation_index": index,
                        "edge": {
                            "predecessor_symbol": getattr(
                                predecessor,
                                "symbol",
                                predecessor_id,
                            ),
                            "successor_symbol": getattr(
                                successor,
                                "symbol",
                                successor_id,
                            ),
                        },
                        "cycle_process_ids": cycle_ids,
                        "cycle_process_symbols": cycle_symbols,
                    },
                )
        raise ServiceValidationError(
            code="dependency_cycle",
            message="Dependency update would create a process cycle.",
            details={},
        )

    def _validate_resource_role_ids(
        self,
        *,
        role_ids: list[str],
        active: bool,
    ) -> None:
        if any(not role_id for role_id in role_ids):
            raise ServiceValidationError(
                code="validation_error",
                message="role_ids must contain non-empty strings.",
                field_path="role_ids",
            )
        if len(role_ids) != len(set(role_ids)):
            raise ServiceValidationError(
                code="validation_error",
                message="role_ids must be unique.",
                field_path="role_ids",
            )
        if active and not role_ids:
            raise ServiceValidationError(
                code="validation_error",
                message="Active resources require at least one role_id.",
                field_path="role_ids",
            )

    def _resource_exists(
        self,
        repository: ProjectRepository,
        project_id: str,
        resource_id: str | None,
    ) -> bool:
        if resource_id is None:
            return False
        resources = getattr(repository, "resources", None)
        if not isinstance(resources, dict):
            return False
        resource = resources.get(resource_id)
        return isinstance(resource, dict) and resource.get("project_id") == project_id

    def _resource_equivalent(
        self,
        repository: ProjectRepository,
        project_id: str,
        resource,
    ) -> bool:
        resource_id = resource.resource_id
        if resource_id is None:
            return False
        resources = getattr(repository, "resources", None)
        if not isinstance(resources, dict):
            return False
        existing = resources.get(resource_id)
        if not isinstance(existing, dict) or existing.get("project_id") != project_id:
            return False
        default_currency = repository.get_project(project_id).default_currency
        cost_currency = resource.cost_currency or default_currency
        cost_unit = getattr(resource.cost_unit, "value", resource.cost_unit)
        if any(holiday.holiday_id is None for holiday in resource.holidays):
            return False
        holiday_records = [holiday.model_dump() for holiday in resource.holidays]
        calendar_override_records = [
            override.model_dump()
            for override in resource.calendar_overrides
        ]
        expected_calendar_id = resource.calendar_id
        if expected_calendar_id is None and resource.resource_type == "external":
            expected_calendar_id = self._external_default_calendar_id(project_id)
        return (
            existing.get("name") == resource.name
            and existing.get("resource_type", "internal") == resource.resource_type
            and existing.get("role_ids") == resource.role_ids
            and existing.get("calendar_id") == expected_calendar_id
            and existing.get("available_from_at") == resource.available_from_at
            and existing.get("available_until_at") == resource.available_until_at
            and existing.get("cost_rate") == str(resource.cost_rate)
            and existing.get("cost_unit") == cost_unit
            and existing.get("cost_currency") == cost_currency
            and existing.get("holidays", []) == holiday_records
            and existing.get("calendar_overrides", []) == calendar_override_records
            and existing.get("active") == resource.active
        )

    def _resource_project_currency(
        self,
        repository: ProjectRepository,
        project_id: str,
        cost_currency: str | None,
    ) -> str:
        default_currency = repository.get_project(project_id).default_currency
        if cost_currency is not None and cost_currency != default_currency:
            raise ServiceValidationError(
                code="resource_currency_mismatch",
                message="Resource currency must match the project default currency.",
                field_path="cost_currency",
                details={
                    "project_default_currency": default_currency,
                    "resource_cost_currency": cost_currency,
                },
            )
        return default_currency

    def _resource_calendar_for_upsert(
        self,
        repository,
        project_id: str,
        *,
        resource_type: str,
        calendar_id: str | None,
    ) -> str:
        if calendar_id:
            return calendar_id
        if resource_type != "external":
            raise ServiceValidationError(
                code="validation_error",
                message="calendar_id is required for internal resources.",
                field_path="calendar_id",
            )
        return self._ensure_external_default_calendar(repository, project_id)

    def _ensure_external_default_calendar(self, repository, project_id: str) -> str:
        repository.get_project(project_id)
        calendar_id = self._external_default_calendar_id(project_id)
        calendars = getattr(repository, "calendars", {})
        existing = calendars.get(calendar_id)
        if (
            isinstance(existing, dict)
            and existing.get("project_id") == project_id
            and existing.get("active", True)
        ):
            return calendar_id
        repository.upsert_resource_calendar(
            project_id=project_id,
            calendar_id=calendar_id,
            name="External default",
            timezone="UTC",
            weekly_windows=[
                CalendarWeeklyWindowCommand(
                    weekday=weekday,
                    start_local_time="09:00",
                    end_local_time="17:00",
                    capacity_hours=8,
                )
                for weekday in range(5)
            ],
            active=True,
        )
        return calendar_id

    def _external_default_calendar_id(self, project_id: str) -> str:
        return f"calendar-{symbolify(project_id)}-external-default"

    def _resource_roles_equal(
        self,
        repository: ProjectRepository,
        project_id: str,
        resource_id: str,
        role_ids: list[str],
    ) -> bool:
        resources = getattr(repository, "resources", None)
        if not isinstance(resources, dict):
            return False
        resource = resources.get(resource_id)
        return (
            isinstance(resource, dict)
            and resource.get("project_id") == project_id
            and resource.get("role_ids") == role_ids
        )

    def _resource_calendar_equal(
        self,
        repository: ProjectRepository,
        project_id: str,
        resource_id: str,
        calendar_id: str,
    ) -> bool:
        resources = getattr(repository, "resources", None)
        if not isinstance(resources, dict):
            return False
        resource = resources.get(resource_id)
        return (
            isinstance(resource, dict)
            and resource.get("project_id") == project_id
            and resource.get("calendar_id") == calendar_id
        )

    def _load_command_replay_cache(
        self,
    ) -> dict[object, dict[str, CommandResult | CommandErrorResult]]:
        loader = getattr(self._repository, "load_command_replay_cache", None)
        if callable(loader):
            return loader()
        return {}

    def _persist_command_replay_cache(self) -> None:
        persister = getattr(self._repository, "replace_command_replay_cache", None)
        if callable(persister):
            persister(self._command_replay_cache)

    def _repository_call(self, method_name: str, **kwargs):
        return self._repository_call_on(self._repository, method_name, **kwargs)

    def _repository_call_on(self, repository, method_name: str, **kwargs):
        method = getattr(repository, method_name, None)
        if method is None:
            raise ServiceValidationError(
                code="unsupported_command",
                message=f"Repository does not support {method_name}.",
            )
        signature = inspect.signature(method)
        accepted_kwargs = {
            key: value for key, value in kwargs.items() if key in signature.parameters
        }
        return method(**accepted_kwargs)

    def _validate_required_roles_transition(
        self,
        envelope: CommandEnvelope,
    ) -> list[Warning] | CommandErrorResult:
        command = envelope.command
        if not isinstance(command, UpsertProcessRevision):
            return []

        legacy_used = bool(command.required_roles)
        role_requirements_used = bool(command.role_requirements)
        if legacy_used and role_requirements_used:
            return self._command_validation_error(
                envelope,
                loc=["command", "required_roles"],
                msg="required_roles and role_requirements are mutually exclusive.",
                issue_type="mutually_exclusive",
            )

        mode = self._config.required_roles_transition_mode
        if mode == RequiredRolesTransitionMode.REQUIRE_ROLE_REQUIREMENTS and legacy_used:
            return self._command_validation_error(
                envelope,
                loc=["command", "required_roles"],
                msg="required_roles is not accepted in require_role_requirements mode.",
                issue_type="legacy_required_roles_forbidden",
            )

        if mode == RequiredRolesTransitionMode.DUAL_WRITE_WARN and legacy_used:
            return [
                Warning(
                    code="legacy_required_roles",
                    message="required_roles was accepted in transition mode.",
                    severity=WarningSeverity.WARNING,
                    details={"mode": mode.value},
                )
            ]

        return []

    def _command_validation_error(
        self,
        envelope: CommandEnvelope,
        *,
        loc: list[str | int],
        msg: str,
        issue_type: str,
    ) -> CommandErrorResult:
        return CommandErrorResult(
            command_id=envelope.command_id,
            error=Error(
                code="validation_error",
                message="Payload validation failed.",
                details={},
                validation_errors=[
                    ValidationIssue(
                        loc=loc,
                        msg=msg,
                        type=issue_type,
                        ctx={},
                    )
                ],
            ),
        )
