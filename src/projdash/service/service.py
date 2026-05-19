"""Validated service facade for commands and queries."""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
import threading
from collections import defaultdict
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

import networkx as nx

from projdash.engine.resource_schedule import compute_resource_schedule
from projdash.engine.schedule import ProjectScheduleInput, compute_schedule
from projdash.service.commands import (
    AddBlocker,
    AddCalendarException,
    AddDependencyOperation,
    AddProcessAliases,
    AddRoleRequirementOperation,
    BatchCommandEnvelope,
    BatchUpdateProcessGraph,
    CollapseSubgraph,
    CommandEnvelope,
    CommitProjectState,
    CreateProject,
    CreateRole,
    DeactivateRole,
    DeleteProject,
    RemoveCalendarException,
    RemoveDependencyOperation,
    RemoveRoleRequirementOperation,
    RenameProcess,
    RenameRole,
    ReplaceProcessWithSubgraph,
    ResolveBlocker,
    SetCalendarActive,
    SetProcessStatus,
    SetProjectDefaultCurrency,
    SetResourceActive,
    SetResourceCalendar,
    SetResourceCalendarOperation,
    SetResourceRoles,
    SetResourceRolesOperation,
    UpdateProject,
    UpsertProcessRevision,
    UpsertResource,
    UpsertResourceCalendar,
    UpsertResourceOperation,
)
from projdash.service.errors import Error, ServiceValidationError, ValidationIssue
from projdash.service.identifiers import new_id
from projdash.service.models import (
    RequiredRolesTransitionMode,
    ScheduleBasis,
    ScheduleSnapshotRecord,
    ServiceConfig,
    WarningSeverity,
)
from projdash.service.queries import (
    GetProject,
    QueryAgentContext,
    QueryBlockers,
    QueryCosts,
    QueryCriticalPath,
    QueryEnvelope,
    QueryProcessGraph,
    QueryProjectCatalog,
    QueryProjects,
    QueryResourceCapacity,
    QueryResourceSchedule,
    QuerySchedule,
    QueryScheduleSnapshots,
    QueryUtilization,
)
from projdash.service.repository import ProjectRepository
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
    ) -> None:
        self._repository = repository
        self._config = ServiceConfig(
            required_roles_transition_mode=required_roles_transition_mode,
        )
        self._resource_scheduler = resource_scheduler
        self._command_lock = threading.RLock()
        self._command_replay_cache: dict[
            object,
            dict[str, CommandResult | CommandErrorResult],
        ] = self._load_command_replay_cache()

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
            process, lifecycle_event_id = self._repository.set_process_status(
                project_id=command.project_id,
                process_id=process_id,
                status=command.status,
                edit_at=command.edit_at,
                started_at=command.started_at,
                finished_at=command.finished_at,
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
            resource_id = self._repository.upsert_resource(
                project_id=command.project_id,
                resource_id=command.resource_id,
                name=command.name,
                role_ids=command.role_ids,
                calendar_id=command.calendar_id,
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
                severity=command.severity,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"blocker_id": blocker.blocker_id},
            )
        if isinstance(command, ResolveBlocker):
            blocker = self._repository.resolve_blocker(
                project_id=command.project_id,
                blocker_id=command.blocker_id,
                resolved_at=command.resolved_at,
                resolution=command.resolution,
            )
            return CommandResult(
                command_id=envelope.command_id,
                entity_ids={"blocker_id": blocker.blocker_id},
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

    def _schedule_input_for_scope(
        self,
        project_id: str,
        as_of: dt.datetime,
        scope: Any,
    ) -> ProjectScheduleInput:
        schedule_input = self._repository.get_project_schedule_input(project_id, as_of)
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

    def _schedule(self, project_id, as_of, now, scope=None):
        schedule_input = self._schedule_input_for_scope(project_id, as_of, scope)
        return compute_schedule(schedule_input, now)

    def _commit_project_state(
        self,
        envelope: CommandEnvelope,
        command: CommitProjectState,
    ) -> ScheduleSnapshotRecord:
        terminal_symbols = sorted(command.terminal_process_symbols)
        scope = self._terminal_scope_data(terminal_symbols)
        schedule_query = QueryResourceSchedule(
            project_id=command.project_id,
            as_of=command.committed_at,
            now=command.committed_at,
            scope=scope,
            include_allocation_slices=False,
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
            note=command.note,
        )
        recorder = getattr(self._repository, "record_schedule_snapshot", None)
        if recorder is None:
            raise ServiceValidationError(
                code="unsupported_repository",
                message="Repository does not support schedule snapshots.",
            )
        return recorder(snapshot)

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
        projection = compute_schedule(schedule_input, as_of)
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
        resource_schedule = None
        resource_by_process: dict[str, dict[str, object]] = {}
        resource_warnings: list[Warning] = []
        if query.include_resource_fields:
            resource_schedule, resource_warnings = self._resource_schedule_data(query)
            resource_by_process = {
                row["process_id"]: row for row in resource_schedule["processes"]
            }
        nodes = []
        resource_cp = (
            set(resource_schedule["critical_path_process_ids"])
            if resource_schedule is not None
            else set()
        )
        for node in graph["nodes"]:
            node = dict(node)
            if resource_schedule is None:
                node["resource_aware"] = None
            else:
                row = resource_by_process.get(node["process_id"])
                node["resource_aware"] = None
                if row is not None:
                    resource_es_at = self._parse_datetime(
                        row.get("resource_es_at") or row.get("starts_at")
                    )
                    resource_ls_at = self._parse_datetime(row.get("resource_ls_at"))
                    resource_lf_at = self._parse_datetime(row.get("resource_lf_at"))
                    allocation_state = str(row["allocation_state"])
                    inferred_duration_hours = row.get("inferred_duration_hours")
                    node["inferred_duration_hours"] = inferred_duration_hours
                    node["computed_status"] = self._resource_allocation_status(
                        explicit_status=str(node["status"]),
                        allocation_state=allocation_state,
                        is_blocked=bool(node["blocker_summary"].get("blocking_count")),
                        fallback_status=str(node["computed_status"]),
                    )
                    if resource_es_at is not None and resource_ls_at is not None:
                        node["computed_status"] = self._resource_graph_computed_status(
                            explicit_status=str(node["status"]),
                            allocation_state=allocation_state,
                            earliest_start_at=resource_es_at,
                            latest_start_at=resource_ls_at,
                            now=query.now,
                            is_blocked=bool(
                                node["blocker_summary"].get("blocking_count")
                            ),
                        )
                        node["work_now_window"] = {
                            "starts_at": resource_es_at.isoformat(),
                            "ends_at": resource_ls_at.isoformat(),
                            "active": (
                                node["computed_status"] == "work_now"
                                and resource_es_at <= query.now < resource_ls_at
                            ),
                        }
                    if resource_ls_at is not None and resource_lf_at is not None:
                        node["late_risk_window"] = {
                            "starts_at": resource_ls_at.isoformat(),
                            "ends_at": resource_lf_at.isoformat(),
                            "active": (
                                node["computed_status"] in {"late_risk", "blocked"}
                                and node["status"] not in {"done", "canceled"}
                                and query.now >= resource_ls_at
                            ),
                        }
                    node["resource_aware"] = {
                        "ready_at": row["ready_at"],
                        "starts_at": row["starts_at"],
                        "ends_at": row["ends_at"],
                        "es_at": row.get("resource_es_at"),
                        "ef_at": row.get("resource_ef_at"),
                        "ls_at": row.get("resource_ls_at"),
                        "lf_at": row.get("resource_lf_at"),
                        "inferred_duration_hours": inferred_duration_hours,
                        "resource_delay_hours": row["resource_delay_hours"],
                        "slack_hours": row.get("resource_slack_hours"),
                        "criticality_label": (
                            "critical"
                            if node["process_id"] in resource_cp
                            else "non_critical"
                        ),
                        "allocation_state": row["allocation_state"],
                        "allocation_diagnostic": row.get("allocation_diagnostic"),
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
            "edges": graph["edges"],
            "critical_path_process_ids": (
                resource_schedule["critical_path_process_ids"]
                if resource_schedule is not None
                else graph["critical_path_process_ids"]
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
        schedule_input = self._schedule_input_for_scope(
            project_id,
            as_of,
            scope,
        )
        projection = compute_schedule(schedule_input, now)
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
        nodes = []
        edges = []
        for row in projection.rows:
            process = processes.get(row.process_id)
            revision = self._repository.selected_revision_as_of(
                project_id,
                row.process_id,
                as_of,
            )
            started_at = getattr(process, "started_at", None)
            finished_at = getattr(process, "finished_at", None)
            process_symbol = getattr(process, "symbol", row.process_id)
            computed_status = self._graph_computed_status(
                row.explicit_status,
                row.computed_status.value,
                row.earliest_start_at,
                row.latest_start_at,
                now,
                bool(blocker_summary.get(row.process_id, {}).get("blocking_count")),
            )
            duration_hours = input_by_id[row.process_id].duration_business_days * 8
            latest_start_at = row.latest_start_at
            latest_finish_at = row.latest_finish_at
            work_active = (
                computed_status == "work_now"
                and row.earliest_start_at <= now < latest_start_at
            )
            late_active = (
                computed_status in {"late_risk", "blocked"}
                and row.explicit_status not in {"done", "canceled"}
                and now >= latest_start_at
            )
            required_roles = dict(getattr(revision, "required_roles", {}) or {})
            role_requirements = self._role_requirements_json(
                list(getattr(revision, "role_requirements", []) or [])
            )
            node = {
                "process_id": row.process_id,
                "process_symbol": process_symbol,
                "aliases": sorted(aliases_by_process.get(row.process_id, [])),
                "name": row.name,
                "description": revision.description if revision else "",
                "duration_hours": duration_hours,
                "inferred_duration_hours": None,
                "earliest_start_at": (
                    input_by_id[row.process_id].earliest_start_at.isoformat()
                    if input_by_id[row.process_id].earliest_start_at
                    else None
                ),
                "status": row.explicit_status,
                "started_at": started_at.isoformat() if started_at else None,
                "finished_at": finished_at.isoformat() if finished_at else None,
                "computed_status": computed_status,
                "blocker_summary": blocker_summary.get(
                    row.process_id,
                    {
                        "unresolved_count": 0,
                        "blocking_count": 0,
                        "blocker_ids": [],
                    },
                ),
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

    def _graph_computed_status(
        self,
        explicit_status: str,
        schedule_status: str,
        earliest_start_at: dt.datetime,
        latest_start_at: dt.datetime,
        now: dt.datetime,
        is_blocked: bool,
    ) -> str:
        if explicit_status == "done":
            return "complete"
        if explicit_status == "canceled":
            return "canceled"
        if is_blocked or schedule_status == "blocked":
            return "blocked"
        if now >= latest_start_at:
            return "late_risk"
        if earliest_start_at <= now < latest_start_at:
            return "work_now"
        if schedule_status == "late":
            return "late_risk"
        return "ready"

    def _resource_graph_computed_status(
        self,
        explicit_status: str,
        allocation_state: str,
        earliest_start_at: dt.datetime,
        latest_start_at: dt.datetime,
        now: dt.datetime,
        is_blocked: bool,
    ) -> str:
        if explicit_status == "done":
            return "complete"
        if explicit_status == "canceled":
            return "canceled"
        if is_blocked:
            return "blocked"
        if now >= latest_start_at:
            return "late_risk"
        if earliest_start_at <= now < latest_start_at:
            return "work_now"
        return "ready"

    def _resource_allocation_status(
        self,
        *,
        explicit_status: str,
        allocation_state: str,
        is_blocked: bool,
        fallback_status: str,
    ) -> str:
        if explicit_status == "done":
            return "complete"
        if explicit_status == "canceled":
            return "canceled"
        if is_blocked:
            return "blocked"
        return fallback_status

    def _blocker_summary_by_process(
        self,
        project_id: str,
        as_of: dt.datetime,
    ) -> dict[str, dict[str, object]]:
        list_as_of = getattr(self._repository, "list_blockers_as_of", None)
        blockers = (
            list_as_of(project_id, as_of, False)
            if list_as_of is not None
            else self._repository.list_blockers(project_id, False)
        )
        processes = getattr(self._repository, "processes", {})
        active_ids = set(
            self._repository.active_process_ids_as_of(project_id, as_of)
            if hasattr(self._repository, "active_process_ids_as_of")
            else []
        )
        summary: dict[str, dict[str, object]] = {}
        for blocker in blockers:
            severity = self._enum_value(blocker.severity)
            process = processes.get(blocker.process_id)
            if blocker.process_id not in active_ids:
                continue
            if self._enum_value(getattr(process, "status", None)) in {
                "done",
                "canceled",
            }:
                continue
            row = summary.setdefault(
                blocker.process_id,
                {"unresolved_count": 0, "blocking_count": 0, "blocker_ids": []},
            )
            row["unresolved_count"] += 1
            if severity == "blocking":
                row["blocking_count"] += 1
                row["blocker_ids"].append(blocker.blocker_id)
        return summary

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
            created_at = blocker.created_at or blocker.opened_at
            resolved_at = blocker.resolved_at
            is_resolved = resolved_at is not None and resolved_at <= query.as_of
            process = processes.get(blocker.process_id)
            is_blocking = (
                not is_resolved
                and created_at <= query.as_of
                and self._enum_value(blocker.severity) == "blocking"
                and blocker.process_id in active_ids
                and self._enum_value(getattr(process, "status", None))
                not in {"done", "canceled"}
            )
            rows.append(
                {
                    "blocker_id": blocker.blocker_id,
                    "project_id": blocker.project_id,
                    "process_id": blocker.process_id,
                    "process_symbol": getattr(process, "symbol", blocker.process_id),
                    "summary": blocker.summary or blocker.description,
                    "details": blocker.details,
                    "severity": self._enum_value(blocker.severity),
                    "created_at": created_at.isoformat(),
                    "resolved_at": resolved_at.isoformat() if resolved_at else None,
                    "resolution": blocker.resolution,
                    "is_resolved_as_of": is_resolved,
                    "is_blocking_as_of": is_blocking,
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

    def _schedule_snapshot_data(
        self,
        query: QueryScheduleSnapshots,
    ) -> dict[str, object]:
        snapshots = self._repository.schedule_snapshots_as_of(
            query.project_id,
            query.as_of,
            query.terminal_process_symbols,
        )
        return {
            "project_id": query.project_id,
            "as_of": query.as_of.isoformat(),
            "terminal_process_symbols": query.terminal_process_symbols,
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
                "blockers": blockers["blockers"],
                "available_queries": [
                    "query_process_graph",
                    "query_resource_schedule",
                    "query_schedule_snapshots",
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

    def _agent_snapshot_sort_key(self, snapshot: object) -> tuple[dt.datetime, str]:
        committed_at = self._parse_datetime(snapshot.get("committed_at"))
        if committed_at is None:
            committed_at = dt.datetime.min.replace(tzinfo=dt.UTC)
        return committed_at.astimezone(dt.UTC), str(snapshot["snapshot_id"])

    def _agent_context_summary(self, graph: dict[str, object]) -> dict[str, object]:
        nodes = list(graph.get("nodes", []))
        status_counts: dict[str, int] = defaultdict(int)
        total_effort = 0.0
        blocked_count = 0
        for node in nodes:
            status = str(node.get("status") or "unknown")
            status_counts[status] += 1
            blocker_summary = node.get("blocker_summary") or {}
            if int(blocker_summary.get("blocking_count") or 0) > 0:
                blocked_count += 1
            for requirement in node.get("role_requirements") or []:
                total_effort += float(requirement.get("effort_hours") or 0)
        completion_at = self._latest_datetime(
            (node.get("resource_aware") or {}).get("ends_at")
            for node in nodes
        )
        return {
            "process_count": len(nodes),
            "edge_count": len(graph.get("edges", [])),
            "status_counts": dict(sorted(status_counts.items())),
            "blocked_process_count": blocked_count,
            "total_role_effort_hours": self._clean_number(total_effort),
            "projected_completion_at": (
                completion_at.isoformat() if completion_at is not None else None
            ),
            "critical_path": self._critical_path_symbols(graph),
            "converged": graph.get("converged"),
        }

    def _agent_graph_context(self, graph: dict[str, object]) -> dict[str, object]:
        predecessors: dict[str, list[str]] = defaultdict(list)
        successors: dict[str, list[str]] = defaultdict(list)
        edges = []
        for edge in graph.get("edges", []):
            predecessor = edge.get("predecessor_process_symbol")
            successor = edge.get("successor_process_symbol")
            if predecessor and successor:
                predecessors[str(successor)].append(str(predecessor))
                successors[str(predecessor)].append(str(successor))
            edges.append(
                {
                    "predecessor": predecessor,
                    "successor": successor,
                    "dependency_type": edge.get("dependency_type"),
                }
            )
        nodes = []
        for node in graph.get("nodes", []):
            resource = node.get("resource_aware") or {}
            symbol = str(node.get("process_symbol"))
            nodes.append(
                {
                    "process_id": node.get("process_id"),
                    "symbol": symbol,
                    "aliases": node.get("aliases") or [],
                    "name": node.get("name"),
                    "description": node.get("description") or "",
                    "status": node.get("status"),
                    "computed_status": node.get("computed_status"),
                    "predecessors": sorted(predecessors.get(symbol, [])),
                    "successors": sorted(successors.get(symbol, [])),
                    "role_requirements": node.get("role_requirements") or [],
                    "earliest_start_at": node.get("earliest_start_at"),
                    "started_at": node.get("started_at"),
                    "finished_at": node.get("finished_at"),
                    "blocker_summary": node.get("blocker_summary"),
                    "schedule": {
                        "starts_at": resource.get("starts_at"),
                        "ends_at": resource.get("ends_at"),
                        "inferred_duration_hours": resource.get(
                            "inferred_duration_hours"
                        ),
                        "slack_hours": resource.get("slack_hours"),
                        "criticality_label": resource.get("criticality_label"),
                        "allocation_state": resource.get("allocation_state"),
                    },
                }
            )
        return {"nodes": nodes, "edges": edges}

    def _agent_schedule_context(self, graph: dict[str, object]) -> dict[str, object]:
        critical_ids = set(graph.get("critical_path_process_ids") or [])
        rows = []
        for node in graph.get("nodes", []):
            resource = node.get("resource_aware") or {}
            dependency = node.get("dependency_only") or {}
            rows.append(
                {
                    "symbol": node.get("process_symbol"),
                    "name": node.get("name"),
                    "status": node.get("status"),
                    "computed_status": node.get("computed_status"),
                    "es_at": resource.get("es_at") or dependency.get("es_at"),
                    "ef_at": resource.get("ef_at") or dependency.get("ef_at"),
                    "ls_at": resource.get("ls_at") or dependency.get("ls_at"),
                    "lf_at": resource.get("lf_at") or dependency.get("lf_at"),
                    "starts_at": resource.get("starts_at"),
                    "ends_at": resource.get("ends_at"),
                    "inferred_duration_hours": resource.get(
                        "inferred_duration_hours"
                    ),
                    "slack_hours": resource.get("slack_hours"),
                    "critical": node.get("process_id") in critical_ids,
                    "allocation_state": resource.get("allocation_state"),
                }
            )
        completion_at = self._latest_datetime(row.get("ends_at") for row in rows)
        return {
            "basis": graph.get("schedule_basis"),
            "converged": graph.get("converged"),
            "completion_at": (
                completion_at.isoformat() if completion_at is not None else None
            ),
            "critical_path": self._critical_path_symbols(graph),
            "processes": rows,
        }

    def _agent_slippage_context(
        self,
        snapshots: list[object],
        limit: int,
    ) -> dict[str, object]:
        history = list(snapshots)[-limit:]
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
            if node.get("status") in {"done", "canceled"}:
                continue
            dependency = node.get("dependency_only") or {}
            resource = node.get("resource_aware") or {}
            es_at = self._parse_datetime(resource.get("es_at") or resource.get("starts_at"))
            ls_at = self._parse_datetime(resource.get("ls_at"))
            lf_at = self._parse_datetime(resource.get("lf_at"))
            if es_at is None:
                es_at = self._parse_datetime(dependency.get("es_at"))
            if ls_at is None:
                ls_at = self._parse_datetime(dependency.get("ls_at"))
            if lf_at is None:
                lf_at = self._parse_datetime(dependency.get("lf_at"))
            if es_at is None or ls_at is None or lf_at is None:
                continue
            if now >= lf_at:
                priority, priority_rank = "P0", 0
            elif now >= ls_at:
                priority, priority_rank = "P1", 1
            elif now >= es_at:
                priority, priority_rank = "P2", 2
            else:
                priority, priority_rank = "P3", 3
            rows.append(
                (
                    node,
                    {
                        "priority": priority,
                        "priority_rank": priority_rank,
                        "process_symbol": symbol,
                        "process_name": node.get("name"),
                        "es_at": es_at.isoformat(),
                        "ls_at": ls_at.isoformat(),
                        "lf_at": lf_at.isoformat(),
                        "hours_until_ls": self._clean_number(
                            (ls_at - now).total_seconds() / 3600
                        ),
                        "hours_until_lf": self._clean_number(
                            (lf_at - now).total_seconds() / 3600
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
                row.get("hours_until_ls", row["hours_until_lf"]),
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
        scheduler_input = self._resource_schedule_input(
            query,
            include_allocation_slices=include_allocation_slices,
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
        for process_id in active_process_ids:
            revision = self._repository.selected_revision_as_of(
                query.project_id,
                process_id,
                query.as_of,
            )
            if revision is None:
                continue
            process = process_records.get(process_id)
            dependencies = [
                dependency
                for dependency in revision.dependencies
                if dependency in selected_process_ids
            ]
            processes.append(
                {
                    "process_id": process_id,
                    "name": revision.name,
                    "description": revision.description,
                    "dependencies": dependencies,
                    "duration_business_days": revision.duration_business_days,
                    "explicit_status": (
                        self._enum_value(process.status)
                        if process is not None
                        else "planned"
                    ),
                    "started_at": (
                        getattr(process, "started_at", None)
                        if process is not None
                        else None
                    ),
                    "finished_at": (
                        getattr(process, "finished_at", None)
                        if process is not None
                        else None
                    ),
                    "earliest_start_at": revision.earliest_start_at,
                    "start_at_earliest": revision.start_at_earliest,
                    "delay_after_dependencies_business_days": (
                        revision.delay_after_dependencies_business_days
                    ),
                }
            )
            for index, requirement in enumerate(revision.role_requirements):
                requirement_id = (
                    requirement.requirement_id
                    or f"{process_id}-requirement-{index + 1}"
                )
                requirements.append(
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
                    }
                )

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
                "severity": self._enum_value(blocker.severity),
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
        for row in data.get("processes", []):
            if not isinstance(row, dict):
                continue
            process = processes.get(row.get("process_id"))
            if process is None:
                continue
            row["status"] = self._enum_value(process.status)
            row["started_at"] = (
                process.started_at.isoformat() if process.started_at else None
            )
            row["finished_at"] = (
                process.finished_at.isoformat() if process.finished_at else None
            )

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

    def _role_requirements_json(self, requirements) -> list[dict[str, object]]:
        return [
            {
                "requirement_id": requirement.requirement_id,
                "role_id": requirement.role_id,
                "effort_hours": self._clean_number(requirement.effort_hours),
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
            for requirement in requirements
        ]

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
                        role_ids=operation.resource.role_ids,
                        calendar_id=operation.resource.calendar_id,
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
        return (
            existing.get("name") == resource.name
            and existing.get("role_ids") == resource.role_ids
            and existing.get("calendar_id") == resource.calendar_id
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
