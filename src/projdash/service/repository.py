"""Repository protocol and deterministic in-memory implementation."""

from __future__ import annotations

import copy
import datetime as dt
import math
from collections import defaultdict
from typing import Any, Protocol

import networkx as nx

from projdash.engine.calendar import expand_resource_calendar
from projdash.engine.schedule import ProcessScheduleInput, ProjectScheduleInput
from projdash.service.errors import ServiceValidationError, ValidationIssue
from projdash.service.identifiers import new_id, symbolify
from projdash.service.models import (
    BlockerRecord,
    CalendarWeeklyWindowCommand,
    CostUnit,
    ProcessRecord,
    ProcessRevisionRecord,
    ProcessStatus,
    ProjectRecord,
    RoleRequirementCommand,
)


class RetiredProcessRecord(ProcessRecord):
    """Process projection with soft-retirement audit fields."""

    is_active: bool = False
    retired_at: dt.datetime

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        if kwargs.get("mode") == "json":
            for field in ("retired_at", "finished_at"):
                if isinstance(data.get(field), str) and data[field].endswith("Z"):
                    data[field] = f"{data[field][:-1]}+00:00"
        return data


class RecordDict(dict):
    """Dictionary row with a Pydantic-like dump method for tests."""

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return copy.deepcopy(dict(self))


class ProjectRepository(Protocol):
    """Persistence contract used by the service layer."""

    def create_project(
        self,
        name: str,
        start_at: dt.datetime,
        default_currency: str = "USD",
    ) -> ProjectRecord:
        """Create and persist a project."""

    def create_role(
        self,
        project_id: str,
        name: str,
        role_id: str | None = None,
    ) -> str:
        """Create and persist a project role."""

    def get_project(self, project_id: str) -> ProjectRecord:
        """Return a project by id."""

    def upsert_process_revision(
        self,
        project_id: str,
        process_id: str | None,
        name: str,
        effective_at: dt.datetime,
        duration_business_days: int,
        dependencies: list[str],
        due_at: dt.datetime | None,
        earliest_start_at: dt.datetime | None,
        start_at_earliest: bool,
        delay_after_dependencies_business_days: int,
        required_roles: dict[str, float],
        role_requirements: list[RoleRequirementCommand],
        assumption_note: str | None,
    ) -> tuple[ProcessRecord, ProcessRevisionRecord]:
        """Create a process if needed and append a planning revision."""

    def set_process_status(
        self,
        project_id: str,
        process_id: str,
        status: ProcessStatus,
        edit_at: dt.datetime,
        finished_at: dt.datetime | None = None,
    ) -> tuple[ProcessRecord, str]:
        """Set explicit process status."""

    def resolve_process_id(self, project_id: str, process_symbol: str) -> str:
        """Resolve a project-scoped process symbol or alias to a process id."""

    def upsert_resource_calendar(
        self,
        project_id: str,
        calendar_id: str | None,
        name: str,
        timezone: str,
        weekly_windows: list[CalendarWeeklyWindowCommand],
        active: bool = True,
    ) -> str:
        """Create or replace a resource calendar."""

    def set_calendar_active(
        self,
        project_id: str,
        calendar_id: str,
        active: bool,
        force: bool = False,
    ) -> None:
        """Set a resource calendar active flag."""

    def add_calendar_exception(
        self,
        project_id: str,
        calendar_id: str,
        starts_at: dt.datetime,
        ends_at: dt.datetime,
        capacity_hours: float,
        exception_id: str | None = None,
        reason: str | None = None,
    ) -> str:
        """Add a resource calendar exception."""

    def remove_calendar_exception(
        self,
        project_id: str,
        calendar_id: str,
        exception_id: str,
    ) -> str:
        """Remove a resource calendar exception."""

    def upsert_resource(
        self,
        project_id: str,
        resource_id: str | None,
        name: str,
        role_ids: list[str],
        calendar_id: str,
        available_from_at: dt.datetime,
        cost_rate: Any,
        cost_unit: CostUnit,
        cost_currency: str | None = None,
        available_until_at: dt.datetime | None = None,
        active: bool = True,
    ) -> str:
        """Create or replace a resource."""

    def set_resource_active(
        self,
        project_id: str,
        resource_id: str,
        active: bool,
    ) -> None:
        """Set a resource active flag."""

    def set_resource_roles(
        self,
        project_id: str,
        resource_id: str,
        role_ids: list[str],
    ) -> None:
        """Replace a resource's role ids."""

    def set_resource_calendar(
        self,
        project_id: str,
        resource_id: str,
        calendar_id: str,
    ) -> None:
        """Replace a resource's calendar assignment."""

    def deactivate_role(
        self,
        project_id: str,
        role_id: str,
        force: bool = False,
    ) -> None:
        """Deactivate a role."""

    def add_blocker(
        self,
        project_id: str,
        process_id: str,
        description: str,
        opened_at: dt.datetime,
        blocker_id: str | None = None,
        details: str | None = None,
        severity: str | None = None,
    ) -> BlockerRecord:
        """Add a process blocker."""

    def resolve_blocker(
        self,
        project_id: str,
        blocker_id: str,
        resolved_at: dt.datetime,
    ) -> BlockerRecord:
        """Resolve a process blocker."""

    def list_blockers(
        self,
        project_id: str,
        include_resolved: bool = False,
    ) -> list[BlockerRecord]:
        """List project blockers."""

    def get_project_schedule_input(
        self,
        project_id: str,
        as_of: dt.datetime,
    ) -> ProjectScheduleInput:
        """Return the scheduling read model for a project."""

    def clone(self) -> ProjectRepository:
        """Return a deep copy for transactional batch application."""

    def replace_with(self, other: ProjectRepository) -> None:
        """Replace this repository's state with another repository's state."""


class InMemoryProjectRepository:
    """Deterministic repository used by tests and early service development."""

    def __init__(self) -> None:
        self.projects: dict[str, ProjectRecord] = {}
        self.processes: dict[str, ProcessRecord] = {}
        self.process_ids_by_project: dict[str, list[str]] = defaultdict(list)
        self.revisions_by_process: dict[str, list[ProcessRevisionRecord]] = defaultdict(list)
        self.blockers: dict[str, BlockerRecord] = {}
        self.blocker_ids_by_project: dict[str, list[str]] = defaultdict(list)
        self.role_requirements: dict[str, RoleRequirementCommand] = {}
        self.roles: dict[str, dict[str, Any]] = {}
        self.role_ids_by_project: dict[str, list[str]] = defaultdict(list)
        self.resources: dict[str, dict[str, Any]] = {}
        self.resource_ids_by_project: dict[str, list[str]] = defaultdict(list)
        self.calendars: dict[str, dict[str, Any]] = {}
        self.calendar_ids_by_project: dict[str, list[str]] = defaultdict(list)
        self.due_history_events: list[dict[str, Any]] = []
        self.project_due_at: dict[str, dt.datetime | None] = {}
        self.retired_processes: dict[str, dict[str, Any]] = {}
        self.process_aliases: dict[str, dict[str, str]] = defaultdict(dict)
        self.process_alias_sources: dict[str, dict[str, str]] = defaultdict(dict)
        self.dependency_edge_ids: dict[tuple[str, str, str], str] = {}

    def create_project(
        self,
        name: str,
        start_at: dt.datetime,
        default_currency: str = "USD",
    ) -> ProjectRecord:
        project = ProjectRecord(
            project_id=new_id(),
            name=name,
            start_at=start_at,
            default_currency=default_currency,
        )
        self.projects[project.project_id] = project
        return project

    def create_role(
        self,
        project_id: str,
        name: str,
        role_id: str | None = None,
    ) -> str:
        self.get_project(project_id)
        resolved_role_id = role_id or new_id()
        if resolved_role_id in self.roles:
            role = self._get_role(project_id, resolved_role_id)
            if role["name"] != name:
                raise ServiceValidationError(
                    code="role_conflict",
                    message="Role id already exists with different fields.",
                    entity_id=resolved_role_id,
                )
            return resolved_role_id
        for existing_id in self.role_ids_by_project[project_id]:
            role = self.roles[existing_id]
            if role["active"] and role["name"] == name:
                raise ServiceValidationError(
                    code="duplicate_role_name",
                    message="Active role names must be unique within a project.",
                    field_path="name",
                )
        self.roles[resolved_role_id] = {
            "role_id": resolved_role_id,
            "project_id": project_id,
            "name": name,
            "active": True,
        }
        self.role_ids_by_project[project_id].append(resolved_role_id)
        return resolved_role_id

    def get_project(self, project_id: str) -> ProjectRecord:
        if project_id not in self.projects:
            raise ServiceValidationError(
                code="project_not_found",
                message=f"Project {project_id!r} does not exist.",
                entity_id=project_id,
            )
        return self.projects[project_id]

    def upsert_process_revision(
        self,
        project_id: str,
        process_id: str | None,
        name: str,
        effective_at: dt.datetime,
        duration_business_days: int,
        dependencies: list[str],
        due_at: dt.datetime | None,
        earliest_start_at: dt.datetime | None,
        start_at_earliest: bool,
        delay_after_dependencies_business_days: int,
        required_roles: dict[str, float],
        role_requirements: list[RoleRequirementCommand],
        assumption_note: str | None,
    ) -> tuple[ProcessRecord, ProcessRevisionRecord]:
        self.get_project(project_id)

        self._validate_active_role_requirements(project_id, role_requirements)

        if process_id is None:
            process = ProcessRecord(
                process_id=new_id(),
                project_id=project_id,
                symbol=self._unique_symbol(project_id, self._symbol_from_name(name)),
            )
            self.processes[process.process_id] = process
            self.process_ids_by_project[project_id].append(process.process_id)
        elif process_id in self.processes:
            process = self._get_process(project_id, process_id)
        else:
            process = ProcessRecord(
                process_id=process_id,
                project_id=project_id,
                symbol=self._unique_symbol(project_id, process_id),
            )
            self.processes[process.process_id] = process
            self.process_ids_by_project[project_id].append(process.process_id)

        for dependency_id in dependencies:
            self._get_process(project_id, dependency_id)
            if dependency_id == process.process_id:
                raise ServiceValidationError(
                    code="self_dependency",
                    message="A process cannot depend on itself.",
                    field_path="dependencies",
                    entity_id=process.process_id,
                )

        revision = ProcessRevisionRecord(
            revision_id=new_id(),
            process_id=process.process_id,
            project_id=project_id,
            effective_at=effective_at,
            name=name,
            duration_business_days=duration_business_days,
            dependencies=dependencies,
            due_at=due_at,
            earliest_start_at=earliest_start_at,
            start_at_earliest=start_at_earliest,
            delay_after_dependencies_business_days=delay_after_dependencies_business_days,
            required_roles=required_roles,
            role_requirements=role_requirements,
            assumption_note=assumption_note,
        )
        self._validate_acyclic_after_revision(project_id, revision)
        self.revisions_by_process[process.process_id].append(revision)
        for requirement in role_requirements:
            if requirement.requirement_id is not None:
                self.role_requirements[requirement.requirement_id] = requirement
        if due_at is not None:
            self._record_due_history_event(
                project_id=project_id,
                process_id=process.process_id,
                mutation_action="upsert_process_revision",
                edit_at=effective_at,
                before_due_at=None,
                after_due_at=due_at,
                command_id="initial_revision",
            )
        return process, revision

    def set_process_status(
        self,
        project_id: str,
        process_id: str,
        status: ProcessStatus,
        edit_at: dt.datetime,
        finished_at: dt.datetime | None = None,
    ) -> tuple[ProcessRecord, str]:
        process = self._get_process(project_id, process_id)
        lifecycle_finished_at = self._resolve_lifecycle_finished_at(
            process=process,
            status=status,
            edit_at=edit_at,
            finished_at=finished_at,
        )
        updated = process.model_copy(
            update={"status": status, "finished_at": lifecycle_finished_at}
        )
        self.processes[process_id] = updated
        return updated, new_id()

    def resolve_process_id(self, project_id: str, process_symbol: str) -> str:
        self.get_project(project_id)
        alias_target = self.process_aliases.get(project_id, {}).get(process_symbol)
        if alias_target is not None and self._is_process_active_as_of(
            alias_target,
            dt.datetime.max.replace(tzinfo=dt.UTC),
        ):
            return alias_target
        matches = [
            process_id
            for process_id in self.process_ids_by_project.get(project_id, [])
            if self.processes[process_id].symbol == process_symbol
            and self._is_process_active_as_of(
                process_id,
                dt.datetime.max.replace(tzinfo=dt.UTC),
            )
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
            code="not_found",
            message=f"Process symbol {process_symbol!r} does not exist.",
            field_path="process_symbol",
            entity_id=process_symbol,
        )

    def rename_process(
        self,
        project_id: str,
        process_id: str,
        new_symbol: str,
        edit_at: dt.datetime,
        keep_old_symbol_as_alias: bool = True,
    ) -> str:
        process = self._get_process(project_id, process_id)
        self._validate_active_process_identity_available(
            project_id,
            new_symbol,
            owning_process_id=process_id,
        )
        old_symbol = process.symbol
        self.processes[process_id] = process.model_copy(update={"symbol": new_symbol})
        if keep_old_symbol_as_alias:
            self._add_process_alias(
                project_id,
                process_id,
                old_symbol,
                source="rename",
            )
        return process_id

    def add_process_aliases(
        self,
        project_id: str,
        process_id: str,
        aliases: list[str],
        edit_at: dt.datetime,
    ) -> str:
        self._get_process(project_id, process_id)
        for alias in aliases:
            self._add_process_alias(
                project_id,
                process_id,
                alias,
                source="manual",
            )
        return process_id

    def upsert_resource_calendar(
        self,
        project_id: str,
        calendar_id: str | None,
        name: str,
        timezone: str,
        weekly_windows: list[CalendarWeeklyWindowCommand],
        active: bool = True,
    ) -> str:
        self.get_project(project_id)
        resolved_calendar_id = calendar_id or new_id()
        existing = self.calendars.get(resolved_calendar_id)
        if existing is not None and existing["project_id"] != project_id:
            raise ServiceValidationError(
                code="cross_project_calendar",
                message="Calendar does not belong to the requested project.",
                entity_id=resolved_calendar_id,
            )
        for existing_id in self.calendar_ids_by_project.get(project_id, []):
            calendar = self.calendars[existing_id]
            if (
                existing_id != resolved_calendar_id
                and calendar["active"]
                and calendar["name"] == name
            ):
                raise ServiceValidationError(
                    code="duplicate_calendar_name",
                    message="Active calendar names must be unique within a project.",
                    field_path="name",
                )
        if existing is not None and active is False and self._calendar_in_use(
            project_id, resolved_calendar_id
        ):
            raise ServiceValidationError(
                code="calendar_in_use",
                message="Calendar is used by active resources.",
                entity_id=resolved_calendar_id,
            )
        windows = self._validated_weekly_windows(weekly_windows)
        exceptions = existing["exceptions"] if existing is not None else []
        self.calendars[resolved_calendar_id] = {
            "calendar_id": resolved_calendar_id,
            "project_id": project_id,
            "name": name,
            "timezone": timezone,
            "weekly_windows": windows,
            "exceptions": copy.deepcopy(exceptions),
            "active": active,
        }
        if resolved_calendar_id not in self.calendar_ids_by_project[project_id]:
            self.calendar_ids_by_project[project_id].append(resolved_calendar_id)
        return resolved_calendar_id

    def set_calendar_active(
        self,
        project_id: str,
        calendar_id: str,
        active: bool,
        force: bool = False,
    ) -> None:
        calendar = self._get_calendar(project_id, calendar_id)
        if not active and not force and self._calendar_in_use(project_id, calendar_id):
            raise ServiceValidationError(
                code="calendar_in_use",
                message="Calendar is used by active resources.",
                entity_id=calendar_id,
            )
        calendar["active"] = active

    def add_calendar_exception(
        self,
        project_id: str,
        calendar_id: str,
        starts_at: dt.datetime,
        ends_at: dt.datetime,
        capacity_hours: float,
        exception_id: str | None = None,
        reason: str | None = None,
    ) -> str:
        calendar = self._get_calendar(project_id, calendar_id)
        resolved_exception_id = exception_id or new_id()
        new_exception = {
            "exception_id": resolved_exception_id,
            "calendar_id": calendar_id,
            "project_id": project_id,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "capacity_hours": float(capacity_hours),
            "reason": reason,
        }
        for existing in calendar["exceptions"]:
            if existing["exception_id"] == resolved_exception_id:
                if self._equivalent_exception(existing, new_exception):
                    return resolved_exception_id
                raise ServiceValidationError(
                    code="calendar_exception_conflict",
                    message="Calendar exception id already exists with different fields.",
                    entity_id=resolved_exception_id,
                )
            if (
                starts_at < existing["ends_at"]
                and ends_at > existing["starts_at"]
                and float(existing["capacity_hours"]) != float(capacity_hours)
            ):
                raise ServiceValidationError(
                    code="calendar_exception_overlap",
                    message="Overlapping calendar exceptions must have the same capacity.",
                    entity_id=calendar_id,
                )
        calendar["exceptions"].append(new_exception)
        return resolved_exception_id

    def remove_calendar_exception(
        self,
        project_id: str,
        calendar_id: str,
        exception_id: str,
    ) -> str:
        calendar = self._get_calendar(project_id, calendar_id)
        calendar["exceptions"] = [
            exception
            for exception in calendar["exceptions"]
            if exception["exception_id"] != exception_id
        ]
        return exception_id

    def upsert_resource(
        self,
        project_id: str,
        resource_id: str | None,
        name: str,
        role_ids: list[str],
        calendar_id: str,
        available_from_at: dt.datetime,
        cost_rate: Any,
        cost_unit: CostUnit,
        cost_currency: str | None = None,
        available_until_at: dt.datetime | None = None,
        active: bool = True,
    ) -> str:
        self.get_project(project_id)
        resolved_resource_id = resource_id or new_id()
        existing = self.resources.get(resolved_resource_id)
        if existing is not None and existing["project_id"] != project_id:
            raise ServiceValidationError(
                code="cross_project_resource",
                message="Resource does not belong to the requested project.",
                entity_id=resolved_resource_id,
            )
        for existing_id in self.resource_ids_by_project.get(project_id, []):
            resource = self.resources[existing_id]
            if (
                existing_id != resolved_resource_id
                and resource["active"]
                and resource["name"] == name
            ):
                raise ServiceValidationError(
                    code="duplicate_resource_name",
                    message="Active resource names must be unique within a project.",
                    field_path="name",
                )
        self._validate_resource_assignment(
            project_id,
            role_ids=role_ids,
            calendar_id=calendar_id,
            active=active,
        )
        project = self.get_project(project_id)
        self.resources[resolved_resource_id] = RecordDict({
            "resource_id": resolved_resource_id,
            "project_id": project_id,
            "name": name,
            "role_ids": list(role_ids),
            "calendar_id": calendar_id,
            "available_from_at": available_from_at,
            "available_until_at": available_until_at,
            "cost_rate": str(cost_rate),
            "cost_unit": getattr(cost_unit, "value", cost_unit),
            "cost_currency": cost_currency or project.default_currency,
            "active": active,
        })
        if resolved_resource_id not in self.resource_ids_by_project[project_id]:
            self.resource_ids_by_project[project_id].append(resolved_resource_id)
        return resolved_resource_id

    def set_resource_active(
        self,
        project_id: str,
        resource_id: str,
        active: bool,
    ) -> None:
        resource = self._get_resource(project_id, resource_id)
        resource["active"] = active

    def set_resource_roles(
        self,
        project_id: str,
        resource_id: str,
        role_ids: list[str],
    ) -> None:
        resource = self._get_resource(project_id, resource_id)
        self._validate_resource_assignment(
            project_id,
            role_ids=role_ids,
            calendar_id=resource["calendar_id"],
            active=resource["active"],
        )
        resource["role_ids"] = list(role_ids)

    def set_resource_calendar(
        self,
        project_id: str,
        resource_id: str,
        calendar_id: str,
    ) -> None:
        resource = self._get_resource(project_id, resource_id)
        self._validate_resource_assignment(
            project_id,
            role_ids=resource["role_ids"],
            calendar_id=calendar_id,
            active=resource["active"],
        )
        resource["calendar_id"] = calendar_id

    def deactivate_role(
        self,
        project_id: str,
        role_id: str,
        force: bool = False,
    ) -> None:
        role = self._get_role(project_id, role_id)
        if not force and self._role_in_use(project_id, role_id):
            raise ServiceValidationError(
                code="role_in_use",
                message="Role is used by active resources or current revisions.",
                entity_id=role_id,
            )
        role["active"] = False

    def add_blocker(
        self,
        project_id: str,
        process_id: str,
        description: str,
        opened_at: dt.datetime,
        blocker_id: str | None = None,
        details: str | None = None,
        severity: str | None = None,
    ) -> BlockerRecord:
        self._get_process(project_id, process_id)
        blocker = BlockerRecord(
            blocker_id=blocker_id or self._unique_blocker_id(project_id, description),
            project_id=project_id,
            process_id=process_id,
            description=description,
            opened_at=opened_at,
            summary=description,
            details=details,
            severity=severity or "blocking",
            created_at=opened_at,
        )
        self.blockers[blocker.blocker_id] = blocker
        self.blocker_ids_by_project[project_id].append(blocker.blocker_id)
        return blocker

    def resolve_blocker(
        self,
        project_id: str,
        blocker_id: str,
        resolved_at: dt.datetime,
        resolution: str | None = None,
    ) -> BlockerRecord:
        if blocker_id not in self.blockers:
            raise ServiceValidationError(
                code="blocker_not_found",
                message=f"Blocker {blocker_id!r} does not exist.",
                entity_id=blocker_id,
            )
        blocker = self.blockers[blocker_id]
        if blocker.project_id != project_id:
            raise ServiceValidationError(
                code="cross_project_blocker",
                message="Blocker does not belong to the requested project.",
                entity_id=blocker_id,
            )
        if blocker.resolved_at is not None:
            if blocker.resolved_at == resolved_at and blocker.resolution == resolution:
                return blocker
            raise ServiceValidationError(
                code="idempotency_conflict",
                message="Blocker was already resolved with different values.",
                entity_id=blocker_id,
            )
        updated = blocker.model_copy(
            update={"resolved_at": resolved_at, "resolution": resolution},
        )
        self.blockers[blocker_id] = updated
        return updated

    def list_blockers(
        self,
        project_id: str,
        include_resolved: bool = False,
    ) -> list[BlockerRecord]:
        self.get_project(project_id)
        blockers = [
            self.blockers[blocker_id]
            for blocker_id in self.blocker_ids_by_project.get(project_id, [])
        ]
        if include_resolved:
            return blockers
        return [blocker for blocker in blockers if blocker.resolved_at is None]

    def get_project_schedule_input(
        self,
        project_id: str,
        as_of: dt.datetime,
    ) -> ProjectScheduleInput:
        project = self.get_project(project_id)
        processes = []
        blockers_as_of = self.list_blockers_as_of(project_id, as_of)
        for process_id in self.process_ids_by_project[project_id]:
            if not self._is_process_active_as_of(process_id, as_of):
                continue
            process = self.processes[process_id]
            revision = self._latest_revision_as_of(process_id, as_of)
            if revision is None:
                continue
            unresolved_blockers = [
                blocker
                for blocker in blockers_as_of
                if blocker.process_id == process_id
                and self._enum_value(blocker.severity) == "blocking"
                and process.status.value not in {"done", "canceled"}
            ]
            due_at = self.current_process_due_at(process_id, as_of)
            processes.append(
                ProcessScheduleInput(
                    process_id=process_id,
                    name=revision.name,
                    dependencies=tuple(revision.dependencies),
                    duration_business_days=revision.duration_business_days,
                    explicit_status=process.status.value,
                    due_at=due_at,
                    earliest_start_at=revision.earliest_start_at,
                    start_at_earliest=revision.start_at_earliest,
                    delay_after_dependencies_business_days=(
                        revision.delay_after_dependencies_business_days
                    ),
                    unresolved_blocker_count=len(unresolved_blockers),
                )
            )

        return ProjectScheduleInput(
            project_id=project.project_id,
            name=project.name,
            start_at=project.start_at,
            processes=tuple(processes),
        )

    def set_project_default_currency(
        self,
        project_id: str,
        default_currency: str,
    ) -> None:
        project = self.get_project(project_id)
        self.projects[project_id] = project.model_copy(
            update={"default_currency": default_currency},
        )

    def set_project_due_at(
        self,
        project_id: str,
        due_at: dt.datetime | None,
        edit_at: dt.datetime,
        command_id: str,
        mutation_action: str,
    ) -> str:
        self.get_project(project_id)
        before_due_at = self.current_project_due_at(project_id, edit_at)
        self.project_due_at[project_id] = due_at
        event_id = self._record_due_history_event(
            project_id=project_id,
            process_id=None,
            mutation_action=mutation_action,
            edit_at=edit_at,
            before_due_at=before_due_at,
            after_due_at=due_at,
            command_id=command_id,
        )
        derived_due_at = self.derived_project_due_at(project_id, edit_at, None)
        if derived_due_at is not None and mutation_action == "set_project_due_at":
            self._record_due_history_event(
                project_id=project_id,
                process_id=None,
                mutation_action="derived_project_due_at_changed",
                edit_at=edit_at,
                before_due_at=None,
                after_due_at=derived_due_at,
                command_id=command_id,
            )
        return event_id

    def set_process_due_at(
        self,
        project_id: str,
        process_id: str,
        due_at: dt.datetime | None,
        edit_at: dt.datetime,
        command_id: str,
    ) -> str:
        self._get_process(project_id, process_id)
        before_derived = self.derived_project_due_at(project_id, edit_at, None)
        before_due_at = self.current_process_due_at(process_id, edit_at)
        event_id = self._record_due_history_event(
            project_id=project_id,
            process_id=process_id,
            mutation_action="set_process_due_at",
            edit_at=edit_at,
            before_due_at=before_due_at,
            after_due_at=due_at,
            command_id=command_id,
        )
        after_derived = self.derived_project_due_at(project_id, edit_at, None)
        if before_derived != after_derived:
            self._record_due_history_event(
                project_id=project_id,
                process_id=None,
                mutation_action="derived_project_due_at_changed",
                edit_at=edit_at,
                before_due_at=before_derived,
                after_due_at=after_derived,
                command_id=command_id,
            )
        return event_id

    def current_project_due_at(
        self,
        project_id: str,
        as_of: dt.datetime,
    ) -> dt.datetime | None:
        self.get_project(project_id)
        current = None
        for event in self.due_history_events:
            if (
                event["project_id"] == project_id
                and event["process_id"] is None
                and event["mutation_action"]
                in {"set_project_due_at", "clear_project_due_at"}
                and event["edit_at"] <= as_of
            ):
                current = event["after_due_at"]
        return current

    def current_process_due_at(
        self,
        process_id: str,
        as_of: dt.datetime,
    ) -> dt.datetime | None:
        current = None
        revision = self._latest_revision_as_of(process_id, as_of)
        if revision is not None:
            current = revision.due_at
        for event in self.due_history_events:
            if (
                event["process_id"] == process_id
                and event["mutation_action"] == "set_process_due_at"
                and event["edit_at"] <= as_of
            ):
                current = event["after_due_at"]
        return current

    def due_history_as_of(
        self,
        project_id: str,
        as_of: dt.datetime,
    ) -> list[dict[str, Any]]:
        self.get_project(project_id)
        return [
            copy.deepcopy(event)
            for event in self.due_history_events
            if event["project_id"] == project_id and event["edit_at"] <= as_of
        ]

    def list_blockers_as_of(
        self,
        project_id: str,
        as_of: dt.datetime,
        include_resolved: bool = False,
    ) -> list[BlockerRecord]:
        self.get_project(project_id)
        rows = []
        for blocker_id in self.blocker_ids_by_project.get(project_id, []):
            blocker = self.blockers[blocker_id]
            created_at = blocker.created_at or blocker.opened_at
            if created_at > as_of:
                continue
            is_resolved = blocker.resolved_at is not None and blocker.resolved_at <= as_of
            if is_resolved and not include_resolved:
                continue
            rows.append(blocker)
        return rows

    def active_process_ids_as_of(
        self,
        project_id: str,
        as_of: dt.datetime,
    ) -> list[str]:
        self.get_project(project_id)
        return [
            process_id
            for process_id in self.process_ids_by_project[project_id]
            if self._latest_revision_as_of(process_id, as_of) is not None
            and self._is_process_active_as_of(process_id, as_of)
        ]

    def selected_revision_as_of(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime,
    ) -> ProcessRevisionRecord | None:
        self._get_process(project_id, process_id)
        return self._latest_revision_as_of(process_id, as_of)

    def derived_project_due_at(
        self,
        project_id: str,
        as_of: dt.datetime,
        process_ids: set[str] | None,
    ) -> dt.datetime | None:
        due_values = []
        selected_ids = process_ids or set(self.active_process_ids_as_of(project_id, as_of))
        for process_id in selected_ids:
            if not self._is_process_active_as_of(process_id, as_of):
                continue
            due_at = self.current_process_due_at(process_id, as_of)
            if due_at is not None:
                due_values.append(due_at)
        return max(due_values, default=None)

    def process_ids_for_scope(
        self,
        project_id: str,
        as_of: dt.datetime,
        scope: Any,
    ) -> tuple[set[str], dict[str, Any], str | None]:
        if scope is None or getattr(scope, "type", "project") == "project":
            return (
                set(self.active_process_ids_as_of(project_id, as_of)),
                {"type": "project"},
                None,
            )
        if getattr(scope, "type", None) == "target_process":
            process_id = getattr(scope, "process_id", None)
            if process_id is None:
                process_id = self.resolve_process_id(project_id, scope.process_symbol)
            else:
                self._get_process(project_id, process_id)
            return (
                {process_id},
                {"type": "target_process", "process_id": process_id},
                process_id,
            )
        root_symbols = scope.root_process_symbols
        roots = [self.resolve_process_id(project_id, symbol) for symbol in root_symbols]
        graph = self._active_dependency_graph(project_id, as_of)
        selected: set[str] = set()
        direction = getattr(scope.direction, "value", scope.direction)
        for root_id in roots:
            if direction in {"descendants", "ancestors_and_descendants"}:
                selected.add(root_id)
                selected.update(nx.descendants(graph, root_id))
            if direction in {"ancestors", "ancestors_and_descendants"}:
                selected.add(root_id)
                selected.update(nx.ancestors(graph, root_id))
        return (
            selected,
            {
                "type": "topo_filter",
                "root_process_symbols": list(root_symbols),
                "direction": direction,
            },
            None,
        )

    def replace_process_with_subgraph(
        self,
        project_id: str,
        process_id: str,
        edit_at: dt.datetime,
        processes: list[Any],
        dependencies: list[Any],
        root_symbols: list[str],
        leaf_symbols: list[str],
        command_id: str,
        preserve_parent_symbol_as_alias: bool = True,
        parent_alias_target_symbol: str | None = None,
    ) -> dict[str, Any]:
        parent = self._get_process(project_id, process_id)
        child_symbols = [child.process_symbol for child in processes]
        self._validate_unique_values(
            child_symbols,
            ["command", "processes"],
            "duplicate_child_symbol",
        )
        self._validate_unique_values(
            root_symbols,
            ["command", "root_symbols"],
            "duplicate_root_symbol",
        )
        self._validate_unique_values(
            leaf_symbols,
            ["command", "leaf_symbols"],
            "duplicate_leaf_symbol",
        )
        child_symbol_set = set(child_symbols)
        if not set(root_symbols).issubset(child_symbol_set):
            self._raise_validation(
                ["command", "root_symbols"],
                "Root symbols must name supplied child processes.",
                "invalid_root_symbols",
            )
        if not set(leaf_symbols).issubset(child_symbol_set):
            self._raise_validation(
                ["command", "leaf_symbols"],
                "Leaf symbols must name supplied child processes.",
                "invalid_leaf_symbols",
            )
        if parent_alias_target_symbol is not None and (
            parent_alias_target_symbol not in child_symbol_set
        ):
            self._raise_validation(
                ["command", "parent_alias_target_symbol"],
                "Parent alias target must name a supplied child.",
                "invalid_parent_alias_target_symbol",
            )
        for child in processes:
            self._validate_active_process_identity_available(
                project_id,
                child.process_symbol,
                owning_process_id=None,
            )
            for alias in child.aliases:
                self._validate_active_process_identity_available(
                    project_id,
                    alias,
                    owning_process_id=None,
                )
        dependency_pairs = []
        for index, item in enumerate(dependencies):
            if (
                item.predecessor_symbol not in child_symbol_set
                or item.successor_symbol not in child_symbol_set
            ):
                self._raise_validation(
                    ["command", "dependencies", index],
                    "Child dependency endpoints must name supplied child processes.",
                    "invalid_child_dependency",
                )
            dependency_pairs.append((item.predecessor_symbol, item.successor_symbol))
        child_graph = nx.DiGraph()
        child_graph.add_nodes_from(child_symbols)
        child_graph.add_edges_from(dependency_pairs)
        if not nx.is_directed_acyclic_graph(child_graph):
            self._raise_validation(
                ["command", "dependencies"],
                "Child dependency graph must be acyclic.",
                "dependency_cycle",
            )
        child_ids: dict[str, str] = {}
        incoming, outgoing = self._external_edges(project_id, edit_at, {process_id})
        for child in processes:
            duration_days = math.ceil(float(child.duration_hours) / 8)
            process, _revision = self.upsert_process_revision(
                project_id=project_id,
                process_id=f"process-{child.process_symbol}",
                name=child.name,
                effective_at=edit_at,
                duration_business_days=duration_days,
                dependencies=[],
                due_at=child.due_at,
                earliest_start_at=child.earliest_start_at,
                start_at_earliest=False,
                delay_after_dependencies_business_days=0,
                required_roles={},
                role_requirements=child.role_requirements,
                assumption_note=None,
            )
            self.processes[process.process_id] = process.model_copy(
                update={
                    "symbol": child.process_symbol,
                    "status": child.status,
                    "finished_at": child.finished_at,
                }
            )
            for alias in child.aliases:
                self._add_process_alias(
                    project_id,
                    process.process_id,
                    alias,
                    source="manual",
                )
            child_ids[child.process_symbol] = process.process_id
        edge_ids: list[str] = []
        for symbol, child_id in child_ids.items():
            deps = [
                child_ids[pred]
                for pred, succ in dependency_pairs
                if succ == symbol
            ]
            if symbol in root_symbols:
                deps.extend(sorted(incoming))
            revision = self.revisions_by_process[child_id][-1].model_copy(
                update={"dependencies": list(dict.fromkeys(deps))}
            )
            self.revisions_by_process[child_id][-1] = revision
            for predecessor_id in deps:
                edge_id = self._dependency_edge_id(
                    project_id,
                    predecessor_id,
                    child_id,
                )
                edge_ids.append(edge_id)
        for successor_id in outgoing:
            revision = self._latest_revision_as_of(successor_id, edit_at)
            if revision is None:
                continue
            dependencies_for_successor = [
                dep for dep in revision.dependencies if dep != process_id
            ]
            dependencies_for_successor.extend(child_ids[symbol] for symbol in leaf_symbols)
            self.revisions_by_process[successor_id].append(
                revision.model_copy(
                    update={
                        "revision_id": new_id(),
                        "effective_at": edit_at,
                        "dependencies": list(dict.fromkeys(dependencies_for_successor)),
                    }
                )
            )
            for leaf_symbol in leaf_symbols:
                edge_ids.append(
                    self._dependency_edge_id(
                        project_id,
                        child_ids[leaf_symbol],
                        successor_id,
                    )
                )
        retired_edge_ids = [
            self._dependency_edge_id(project_id, predecessor_id, process_id)
            for predecessor_id in incoming
        ] + [
            self._dependency_edge_id(project_id, process_id, successor_id)
            for successor_id in outgoing
        ]
        retirement_event_id = self._retire_process(
            process_id,
            edit_at,
            command_id,
            "replace_process_with_subgraph",
            list(child_ids.values()),
        )
        alias_process_id = None
        if preserve_parent_symbol_as_alias:
            target_symbol = parent_alias_target_symbol or processes[0].process_symbol
            alias_process_id = child_ids[target_symbol]
            self.process_aliases[project_id][parent.symbol] = alias_process_id
            self.process_alias_sources[project_id][parent.symbol] = "retirement"
            for alias, target_id in list(self.process_aliases[project_id].items()):
                if target_id == process_id and (
                    self.process_alias_sources[project_id].get(alias) == "rename"
                ):
                    self.process_aliases[project_id][alias] = alias_process_id
                    self.process_alias_sources[project_id][alias] = "retirement"
        return {
            "process_ids": list(child_ids.values()),
            "retired_process_ids": [process_id],
            "retirement_event_ids": [retirement_event_id],
            "edge_ids": list(dict.fromkeys(edge_ids)),
            "retired_edge_ids": list(dict.fromkeys(retired_edge_ids)),
            **({"alias_process_id": alias_process_id} if alias_process_id else {}),
        }

    def collapse_subgraph(
        self,
        project_id: str,
        process_ids: set[str] | list[str],
        edit_at: dt.datetime,
        new_process: Any,
        command_id: str,
    ) -> dict[str, Any]:
        ordered_process_ids = list(dict.fromkeys(process_ids))
        selected_process_ids = set(ordered_process_ids)
        active_graph = self._active_dependency_graph(project_id, edit_at)
        if not selected_process_ids:
            self._raise_validation(
                ["command", "process_symbols"],
                "At least one process is required.",
                "empty_process_selection",
            )
        if not selected_process_ids.issubset(set(active_graph.nodes)):
            self._raise_validation(
                ["command", "process_symbols"],
                "All selected processes must be active.",
                "process_reference",
            )
        if len(selected_process_ids) > 1:
            selected_graph = active_graph.subgraph(selected_process_ids).to_undirected()
            if not nx.is_connected(selected_graph):
                self._raise_validation(
                    ["command", "process_symbols"],
                    "Collapsed process selection must be connected.",
                    "disconnected_subgraph",
                )
        self._validate_active_process_identity_available(
            project_id,
            new_process.process_symbol,
            owning_process_id=None,
        )
        incoming, outgoing = self._external_edges(
            project_id,
            edit_at,
            selected_process_ids,
        )
        selected_revisions = [
            self._latest_revision_as_of(process_id, edit_at)
            for process_id in ordered_process_ids
        ]
        if any(revision is None for revision in selected_revisions):
            self._raise_validation(
                ["command", "process_symbols"],
                "All selected processes must have revisions.",
                "process_reference",
            )
        selected_revisions = [revision for revision in selected_revisions if revision]
        role_requirements = list(new_process.role_requirements)
        requirement_ids: list[str] = [
            requirement.requirement_id
            for requirement in role_requirements
            if requirement.requirement_id is not None
        ]
        if not role_requirements:
            role_requirements = self._merged_role_requirements(selected_revisions)
            requirement_ids = [
                requirement.requirement_id
                for requirement in role_requirements
                if requirement.requirement_id is not None
            ]
        required_roles = dict(getattr(new_process, "required_roles", {}) or {})
        if not required_roles:
            required_roles = self._merged_legacy_required_roles(selected_revisions)
        if new_process.duration_hours is None:
            duration_days = sum(
                revision.duration_business_days for revision in selected_revisions
            )
            duration_days = max(duration_days, 1)
        else:
            duration_days = math.ceil(float(new_process.duration_hours) / 8)
        replacement, _revision = self.upsert_process_revision(
            project_id=project_id,
            process_id=f"process-{new_process.process_symbol}",
            name=new_process.name,
            effective_at=edit_at,
            duration_business_days=duration_days,
            dependencies=sorted(incoming),
            due_at=new_process.due_at,
            earliest_start_at=new_process.earliest_start_at,
            start_at_earliest=False,
            delay_after_dependencies_business_days=0,
            required_roles=required_roles,
            role_requirements=role_requirements,
            assumption_note=None,
        )
        replacement = replacement.model_copy(
            update={
                "symbol": new_process.process_symbol,
                "status": new_process.status,
                "finished_at": new_process.finished_at,
            }
        )
        self.processes[replacement.process_id] = replacement
        for alias in new_process.aliases:
            self._add_process_alias(
                project_id,
                replacement.process_id,
                alias,
                source="manual",
            )
        edge_ids = [
            self._dependency_edge_id(project_id, predecessor_id, replacement.process_id)
            for predecessor_id in incoming
        ]
        for successor_id in outgoing:
            revision = self._latest_revision_as_of(successor_id, edit_at)
            if revision is None:
                continue
            dependencies = [
                dep for dep in revision.dependencies if dep not in selected_process_ids
            ]
            dependencies.append(replacement.process_id)
            self.revisions_by_process[successor_id].append(
                revision.model_copy(
                    update={
                        "revision_id": new_id(),
                        "effective_at": edit_at,
                        "dependencies": list(dict.fromkeys(dependencies)),
                    }
                )
            )
            edge_ids.append(
                self._dependency_edge_id(project_id, replacement.process_id, successor_id)
            )
        retired_edge_ids = []
        for predecessor_id, successor_id in active_graph.edges:
            if (
                predecessor_id in selected_process_ids
                or successor_id in selected_process_ids
            ):
                retired_edge_ids.append(
                    self._dependency_edge_id(project_id, predecessor_id, successor_id)
                )
        retirement_event_ids = [
            self._retire_process(
                process_id,
                edit_at,
                command_id,
                "collapse_subgraph",
                [replacement.process_id],
            )
            for process_id in ordered_process_ids
        ]
        return {
            "process_id": replacement.process_id,
            "retired_process_ids": ordered_process_ids,
            "retirement_event_ids": retirement_event_ids,
            "edge_ids": list(dict.fromkeys(edge_ids)),
            "retired_edge_ids": list(dict.fromkeys(retired_edge_ids)),
            "requirement_ids": requirement_ids,
        }

    def expand_capacity_buckets(
        self,
        project_id: str,
        horizon_starts_at: dt.datetime,
        horizon_ends_at: dt.datetime,
        resource_ids: list[str] | None = None,
        role_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        self.get_project(project_id)
        resource_filter = set(resource_ids or self.resource_ids_by_project[project_id])
        role_filter = set(role_ids) if role_ids is not None else None
        buckets: list[dict[str, Any]] = []
        for resource_id in self.resource_ids_by_project[project_id]:
            if resource_id not in resource_filter:
                continue
            resource = self.resources[resource_id]
            if not resource["active"]:
                continue
            if role_filter is not None and not role_filter.intersection(resource["role_ids"]):
                continue
            calendar = self.calendars.get(resource["calendar_id"])
            if calendar is None or not calendar["active"]:
                continue
            buckets.extend(
                self._expand_resource_calendar(
                    resource,
                    calendar,
                    horizon_starts_at,
                    horizon_ends_at,
                )
            )
        return sorted(
            buckets,
            key=lambda bucket: (
                bucket["starts_at"],
                bucket["ends_at"],
                bucket["resource_id"],
            ),
        )

    def clone(self) -> InMemoryProjectRepository:
        return copy.deepcopy(self)

    def replace_with(self, other: ProjectRepository) -> None:
        if not isinstance(other, InMemoryProjectRepository):
            raise TypeError("InMemoryProjectRepository can only replace with the same type")
        self.projects = other.projects
        self.processes = other.processes
        self.process_ids_by_project = other.process_ids_by_project
        self.revisions_by_process = other.revisions_by_process
        self.blockers = other.blockers
        self.blocker_ids_by_project = other.blocker_ids_by_project
        self.roles = other.roles
        self.role_ids_by_project = other.role_ids_by_project
        self.role_requirements = other.role_requirements
        self.resources = other.resources
        self.resource_ids_by_project = other.resource_ids_by_project
        self.calendars = other.calendars
        self.calendar_ids_by_project = other.calendar_ids_by_project
        self.due_history_events = other.due_history_events
        self.project_due_at = other.project_due_at
        self.retired_processes = other.retired_processes
        self.process_aliases = other.process_aliases
        self.process_alias_sources = other.process_alias_sources
        self.dependency_edge_ids = other.dependency_edge_ids

    def _record_due_history_event(
        self,
        *,
        project_id: str,
        process_id: str | None,
        mutation_action: str,
        edit_at: dt.datetime,
        before_due_at: dt.datetime | None,
        after_due_at: dt.datetime | None,
        command_id: str,
    ) -> str:
        event_id = (
            f"due-{mutation_action}-{len(self.due_history_events) + 1}"
        )
        self.due_history_events.append(
            {
                "event_id": event_id,
                "project_id": project_id,
                "process_id": process_id,
                "mutation_action": mutation_action,
                "edit_at": edit_at,
                "before_due_at": before_due_at,
                "after_due_at": after_due_at,
                "command_id": command_id,
            }
        )
        return event_id

    def _is_process_active_as_of(
        self,
        process_id: str,
        as_of: dt.datetime,
    ) -> bool:
        retirement = self.retired_processes.get(process_id)
        return retirement is None or retirement["retired_at"] > as_of

    def _active_dependency_graph(
        self,
        project_id: str,
        as_of: dt.datetime,
    ) -> nx.DiGraph:
        graph = nx.DiGraph()
        active_ids = self.active_process_ids_as_of(project_id, as_of)
        graph.add_nodes_from(active_ids)
        active_set = set(active_ids)
        for process_id in active_ids:
            revision = self._latest_revision_as_of(process_id, as_of)
            if revision is None:
                continue
            for dependency_id in revision.dependencies:
                if dependency_id in active_set:
                    graph.add_edge(dependency_id, process_id)
        return graph

    def _external_edges(
        self,
        project_id: str,
        as_of: dt.datetime,
        process_ids: set[str],
    ) -> tuple[set[str], set[str]]:
        incoming: set[str] = set()
        outgoing: set[str] = set()
        active_ids = set(self.active_process_ids_as_of(project_id, as_of))
        for candidate_id in active_ids:
            revision = self._latest_revision_as_of(candidate_id, as_of)
            if revision is None:
                continue
            dependencies = set(revision.dependencies)
            if candidate_id in process_ids:
                incoming.update(dependencies - process_ids)
            elif dependencies.intersection(process_ids):
                outgoing.add(candidate_id)
        return incoming, outgoing

    def _retire_process(
        self,
        process_id: str,
        retired_at: dt.datetime,
        command_id: str,
        reason: str,
        replacement_process_ids: list[str],
    ) -> str:
        event_id = f"retirement-{process_id}-{len(self.retired_processes) + 1}"
        self.retired_processes[process_id] = {
            "retirement_event_id": event_id,
            "process_id": process_id,
            "retired_at": retired_at,
            "retired_by_command_id": command_id,
            "retirement_reason": reason,
            "replacement_process_ids": list(replacement_process_ids),
        }
        process = self.processes[process_id]
        self.processes[process_id] = RetiredProcessRecord(
            process_id=process.process_id,
            project_id=process.project_id,
            symbol=process.symbol,
            status=process.status,
            finished_at=process.finished_at,
            retired_at=retired_at,
        )
        return event_id

    def _dependency_edge_id(
        self,
        project_id: str,
        predecessor_id: str,
        successor_id: str,
    ) -> str:
        key = (project_id, predecessor_id, successor_id)
        edge_id = self.dependency_edge_ids.get(key)
        if edge_id is None:
            edge_id = f"edge-{predecessor_id}-{successor_id}"
            self.dependency_edge_ids[key] = edge_id
        return edge_id

    def _raise_validation(
        self,
        loc: list[str | int],
        msg: str,
        issue_type: str,
        ctx: dict[str, Any] | None = None,
    ) -> None:
        raise ServiceValidationError(
            code="validation_error",
            message=msg,
            validation_errors=[
                ValidationIssue(
                    loc=loc,
                    msg=msg,
                    type=issue_type,
                    ctx=ctx or {},
                )
            ],
        )

    def _validate_unique_values(
        self,
        values: list[str],
        loc: list[str | int],
        issue_type: str,
    ) -> None:
        if len(values) != len(set(values)):
            self._raise_validation(
                loc,
                "Values must be unique.",
                issue_type,
            )

    def _merged_role_requirements(
        self,
        revisions: list[ProcessRevisionRecord],
    ) -> list[RoleRequirementCommand]:
        grouped: dict[str, list[RoleRequirementCommand]] = defaultdict(list)
        process_ids_by_role: dict[str, list[str]] = defaultdict(list)
        requirement_ids_by_role: dict[str, list[str]] = defaultdict(list)
        for revision in revisions:
            for requirement in revision.role_requirements:
                grouped[requirement.role_id].append(requirement)
                process_ids_by_role[requirement.role_id].append(revision.process_id)
                if requirement.requirement_id is not None:
                    requirement_ids_by_role[requirement.role_id].append(
                        requirement.requirement_id
                    )
        merged = []
        for role_id, requirements in grouped.items():
            for field in (
                "required_resource_count",
                "min_allocation_hours_per_day",
                "max_allocation_hours_per_day",
                "allocation_policy",
            ):
                values = [self._enum_value(getattr(item, field)) for item in requirements]
                if len(set(values)) > 1:
                    self._raise_validation(
                        ["command", "new_process", "role_requirements"],
                        "Collapsed role requirements have conflicting controls.",
                        "collapse_role_requirement_conflict",
                        {
                            "field": field,
                            "values": values,
                            "process_ids": process_ids_by_role[role_id],
                            "requirement_ids": requirement_ids_by_role[role_id],
                        },
                    )
            first = requirements[0]
            merged.append(
                RoleRequirementCommand(
                    requirement_id=f"req-collapse-{role_id}",
                    role_id=role_id,
                    effort_hours=sum(
                        float(requirement.effort_hours)
                        for requirement in requirements
                    ),
                    min_allocation_hours_per_day=(
                        first.min_allocation_hours_per_day
                    ),
                    max_allocation_hours_per_day=(
                        first.max_allocation_hours_per_day
                    ),
                    required_resource_count=first.required_resource_count,
                    allocation_policy=first.allocation_policy,
                )
            )
        return merged

    def _merged_legacy_required_roles(
        self,
        revisions: list[ProcessRevisionRecord],
    ) -> dict[str, float]:
        roles = sorted(
            {
                role
                for revision in revisions
                for role in revision.required_roles
            }
        )
        if not roles:
            return {}
        total_duration = sum(revision.duration_business_days for revision in revisions)
        if total_duration <= 0:
            self._raise_validation(
                ["command", "new_process", "required_roles"],
                "Cannot merge legacy required_roles from zero-duration subgraph.",
                "collapse_zero_duration_required_roles",
                {
                    "subgraph_cp_duration": total_duration,
                    "roles": roles,
                },
            )
        merged = {}
        for role in roles:
            weighted = sum(
                float(revision.required_roles.get(role, 0))
                * revision.duration_business_days
                for revision in revisions
            )
            merged[role] = weighted / total_duration
        return merged

    def _expand_resource_calendar(
        self,
        resource: dict[str, Any],
        calendar: dict[str, Any],
        starts_at: dt.datetime,
        ends_at: dt.datetime,
    ) -> list[dict[str, Any]]:
        return [
            {
                "resource_id": bucket.resource_id,
                "calendar_id": bucket.calendar_id,
                "starts_at": bucket.starts_at,
                "ends_at": bucket.ends_at,
                "capacity_hours": bucket.capacity_hours,
                "available_hours": bucket.available_hours,
                "allocated_hours": bucket.allocated_hours,
                "remaining_hours": bucket.remaining_hours,
                "role_ids": list(bucket.role_ids),
                "local_date": bucket.local_date,
                "local_week": bucket.local_week,
            }
            for bucket in expand_resource_calendar(
                calendar=calendar,
                resource=resource,
                horizon_starts_at=starts_at,
                horizon_ends_at=ends_at,
                planning_granularity="hour",
            )
        ]

    def _get_process(self, project_id: str, process_id: str) -> ProcessRecord:
        if process_id not in self.processes:
            raise ServiceValidationError(
                code="process_not_found",
                message=f"Process {process_id!r} does not exist.",
                entity_id=process_id,
            )
        process = self.processes[process_id]
        if process.project_id != project_id:
            raise ServiceValidationError(
                code="cross_project_process",
                message="Process does not belong to the requested project.",
                entity_id=process_id,
            )
        return process

    def _get_role(self, project_id: str, role_id: str) -> dict[str, Any]:
        if role_id not in self.roles:
            raise ServiceValidationError(
                code="role_not_found",
                message=f"Role {role_id!r} does not exist.",
                entity_id=role_id,
            )
        role = self.roles[role_id]
        if role["project_id"] != project_id:
            raise ServiceValidationError(
                code="cross_project_role",
                message="Role does not belong to the requested project.",
                entity_id=role_id,
            )
        return role

    def _get_calendar(self, project_id: str, calendar_id: str) -> dict[str, Any]:
        if calendar_id not in self.calendars:
            raise ServiceValidationError(
                code="calendar_not_found",
                message=f"Calendar {calendar_id!r} does not exist.",
                entity_id=calendar_id,
            )
        calendar = self.calendars[calendar_id]
        if calendar["project_id"] != project_id:
            raise ServiceValidationError(
                code="cross_project_calendar",
                message="Calendar does not belong to the requested project.",
                entity_id=calendar_id,
            )
        return calendar

    def _get_resource(self, project_id: str, resource_id: str) -> dict[str, Any]:
        if resource_id not in self.resources:
            raise ServiceValidationError(
                code="resource_not_found",
                message=f"Resource {resource_id!r} does not exist.",
                entity_id=resource_id,
            )
        resource = self.resources[resource_id]
        if resource["project_id"] != project_id:
            raise ServiceValidationError(
                code="cross_project_resource",
                message="Resource does not belong to the requested project.",
                entity_id=resource_id,
            )
        return resource

    def _active_process_symbols(self, project_id: str) -> dict[str, str]:
        as_of = dt.datetime.max.replace(tzinfo=dt.UTC)
        return {
            self.processes[process_id].symbol: process_id
            for process_id in self.process_ids_by_project.get(project_id, [])
            if self._is_process_active_as_of(process_id, as_of)
        }

    def _validate_active_process_identity_available(
        self,
        project_id: str,
        symbol: str,
        *,
        owning_process_id: str | None = None,
    ) -> None:
        active_symbols = self._active_process_symbols(project_id)
        if symbol in active_symbols and active_symbols[symbol] != owning_process_id:
            raise ServiceValidationError(
                code="validation_error",
                message="Process symbols and aliases must be unique.",
                field_path="process_symbol",
                validation_errors=[
                    ValidationIssue(
                        loc=["command", "process_symbol"],
                        msg="Process symbol collides with an active process.",
                        type="process_symbol_collision",
                        ctx={"symbol": symbol},
                    )
                ],
            )
        alias_target = self.process_aliases.get(project_id, {}).get(symbol)
        if (
            alias_target is not None
            and alias_target != owning_process_id
            and self._is_process_active_as_of(
                alias_target,
                dt.datetime.max.replace(tzinfo=dt.UTC),
            )
        ):
            raise ServiceValidationError(
                code="validation_error",
                message="Process symbols and aliases must be unique.",
                field_path="aliases",
                validation_errors=[
                    ValidationIssue(
                        loc=["command", "aliases"],
                        msg="Alias collides with an active process identity.",
                        type="process_alias_collision",
                        ctx={"symbol": symbol},
                    )
                ],
            )

    def _add_process_alias(
        self,
        project_id: str,
        process_id: str,
        alias: str,
        *,
        source: str,
    ) -> None:
        self._validate_active_process_identity_available(
            project_id,
            alias,
            owning_process_id=process_id,
        )
        self.process_aliases[project_id][alias] = process_id
        self.process_alias_sources[project_id][alias] = source

    def _validate_active_role_requirements(
        self,
        project_id: str,
        role_requirements: list[RoleRequirementCommand],
    ) -> None:
        for requirement in role_requirements:
            role = self._get_role(project_id, requirement.role_id)
            if not role["active"]:
                raise ServiceValidationError(
                    code="inactive_role",
                    message="Inactive roles cannot be used by new requirements.",
                    entity_id=requirement.role_id,
                )

    def _validate_resource_assignment(
        self,
        project_id: str,
        *,
        role_ids: list[str],
        calendar_id: str,
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
        for role_id in role_ids:
            role = self._get_role(project_id, role_id)
            if active and not role["active"]:
                raise ServiceValidationError(
                    code="inactive_role",
                    message="Inactive roles cannot be assigned to active resources.",
                    entity_id=role_id,
                )
        calendar = self._get_calendar(project_id, calendar_id)
        if active and not calendar["active"]:
            raise ServiceValidationError(
                code="inactive_calendar",
                message="Inactive calendars cannot be assigned to active resources.",
                entity_id=calendar_id,
            )

    def _resolve_lifecycle_finished_at(
        self,
        *,
        process: ProcessRecord,
        status: ProcessStatus,
        edit_at: dt.datetime,
        finished_at: dt.datetime | None,
    ) -> dt.datetime | None:
        if finished_at is not None and finished_at > edit_at:
            raise ServiceValidationError(
                code="validation_error",
                message="finished_at must be no later than edit_at.",
                field_path="finished_at",
            )
        if status == ProcessStatus.DONE:
            return finished_at or edit_at
        if status == ProcessStatus.CANCELED:
            if process.status == ProcessStatus.DONE:
                if finished_at is not None and finished_at != process.finished_at:
                    raise ServiceValidationError(
                        code="validation_error",
                        message=(
                            "Canceling a done process can only preserve the existing "
                            "finished_at value."
                        ),
                        field_path="finished_at",
                    )
                return process.finished_at
            if finished_at is not None:
                raise ServiceValidationError(
                    code="validation_error",
                    message="Canceling unfinished work must not set finished_at.",
                    field_path="finished_at",
                )
            return None
        if finished_at is not None:
            raise ServiceValidationError(
                code="validation_error",
                message="finished_at is only accepted when status is done.",
                field_path="finished_at",
            )
        return None

    def _validated_weekly_windows(
        self,
        weekly_windows: list[CalendarWeeklyWindowCommand],
    ) -> list[dict[str, Any]]:
        windows = []
        seen_window_ids: set[str] = set()
        intervals_by_weekday: dict[int, list[tuple[dt.time, dt.time]]] = defaultdict(list)
        for window in weekly_windows:
            window_id = window.window_id or new_id()
            if window_id in seen_window_ids:
                raise ServiceValidationError(
                    code="duplicate_calendar_window",
                    message="Calendar weekly window ids must be unique.",
                    entity_id=window_id,
                )
            seen_window_ids.add(window_id)
            starts = dt.time.fromisoformat(window.start_local_time)
            ends = dt.time.fromisoformat(window.end_local_time)
            for existing_start, existing_end in intervals_by_weekday[window.weekday]:
                if starts < existing_end and ends > existing_start:
                    raise ServiceValidationError(
                        code="calendar_window_overlap",
                        message="Weekly calendar windows cannot overlap.",
                        field_path="weekly_windows",
                    )
            intervals_by_weekday[window.weekday].append((starts, ends))
            windows.append(
                {
                    "window_id": window_id,
                    "weekday": window.weekday,
                    "start_local_time": window.start_local_time,
                    "end_local_time": window.end_local_time,
                    "capacity_hours": float(window.capacity_hours),
                }
            )
        return windows

    def _enum_value(self, value):
        return getattr(value, "value", value)

    def _role_in_use(self, project_id: str, role_id: str) -> bool:
        for resource_id in self.resource_ids_by_project.get(project_id, []):
            resource = self.resources[resource_id]
            if resource["active"] and role_id in resource["role_ids"]:
                return True
        for process_id in self.process_ids_by_project.get(project_id, []):
            revision = self._latest_revision_as_of(
                process_id,
                dt.datetime.max.replace(tzinfo=dt.UTC),
            )
            if revision is None:
                continue
            if any(
                requirement.role_id == role_id
                for requirement in revision.role_requirements
            ):
                return True
        return False

    def _calendar_in_use(self, project_id: str, calendar_id: str) -> bool:
        return any(
            self.resources[resource_id]["active"]
            and self.resources[resource_id]["calendar_id"] == calendar_id
            for resource_id in self.resource_ids_by_project.get(project_id, [])
        )

    def _equivalent_exception(
        self,
        existing: dict[str, Any],
        candidate: dict[str, Any],
    ) -> bool:
        return (
            existing["starts_at"] == candidate["starts_at"]
            and existing["ends_at"] == candidate["ends_at"]
            and float(existing["capacity_hours"]) == float(candidate["capacity_hours"])
            and existing.get("reason") == candidate.get("reason")
        )

    def _latest_revision_as_of(
        self,
        process_id: str,
        as_of: dt.datetime,
    ) -> ProcessRevisionRecord | None:
        latest = None
        for revision in self.revisions_by_process[process_id]:
            if revision.effective_at <= as_of:
                latest = revision
        return latest

    def _unique_symbol(self, project_id: str, base_symbol: str) -> str:
        existing = {
            self.processes[process_id].symbol
            for process_id in self.process_ids_by_project[project_id]
        }
        if base_symbol not in existing:
            return base_symbol

        suffix = 1
        while f"{base_symbol}{suffix}" in existing:
            suffix += 1
        return f"{base_symbol}{suffix}"

    def _symbol_from_name(self, name: str) -> str:
        slug = "".join(
            char.lower() if char.isalnum() else "-"
            for char in name.strip()
        )
        while "--" in slug:
            slug = slug.replace("--", "-")
        return slug.strip("-") or symbolify(name).lower()

    def _unique_blocker_id(self, project_id: str, summary: str) -> str:
        slug = "".join(
            char.lower() if char.isalnum() else "-"
            for char in summary.strip()
        )
        while "--" in slug:
            slug = slug.replace("--", "-")
        base_id = f"blocker-{slug.strip('-') or 'blocker'}"
        if base_id not in self.blockers:
            return base_id
        suffix = 1
        while f"{base_id}-{suffix}" in self.blockers:
            suffix += 1
        return f"{base_id}-{suffix}"

    def _validate_acyclic_after_revision(
        self,
        project_id: str,
        candidate: ProcessRevisionRecord,
    ) -> None:
        graph = nx.DiGraph()
        graph.add_nodes_from(self.process_ids_by_project[project_id])
        for process_id in self.process_ids_by_project[project_id]:
            if process_id == candidate.process_id:
                revision = candidate
            else:
                revision = self._latest_revision_as_of(process_id, candidate.effective_at)
            if revision is None:
                continue
            for dependency_id in revision.dependencies:
                graph.add_edge(dependency_id, process_id)

        if not nx.is_directed_acyclic_graph(graph):
            raise ServiceValidationError(
                code="dependency_cycle",
                message="Adding this process revision would create a dependency cycle.",
                field_path="dependencies",
                entity_id=candidate.process_id,
            )
