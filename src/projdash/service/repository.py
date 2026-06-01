"""Repository protocol and deterministic in-memory implementation."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
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
    MilestoneRecord,
    PMCommunicationEvidenceRecord,
    ProcessEvidenceLineItemRecord,
    ProcessRecord,
    ProcessRevisionRecord,
    ProcessRolePinRecord,
    ProcessStatus,
    ProjectRecord,
    ResourceCalendarOverrideCommand,
    ResourceEvidenceLineItemRecord,
    ResourceHolidayCommand,
    RoleRequirementCommand,
    ScheduleSnapshotRecord,
    SlackCollectionCursorRecord,
    SlackEncryptedTokenRecord,
    SlackOutboxMessageCommand,
    SlackOutboxRecord,
    SlackOutboxStatus,
    SlackProjectConfigRecord,
    SlackResourceMappingRecord,
    SlackRunRecord,
    SlackRunStatus,
)

LEGACY_PROCESS_EVIDENCE_LINE_ITEMS = frozenset(
    {
        "finished",
        "historically_staked_resources",
        "pinned_resources",
        "planned_resources_uptodate_on_process",
        "role_requirements",
        "staked_resources",
    }
)


def slack_outbox_target_key(
    message: SlackOutboxMessageCommand | SlackOutboxRecord,
    project_id: str | None = None,
) -> tuple[str, str, str, str]:
    """Return the durable dedupe key for a Slack outbox target."""
    target_type = getattr(message, "target_type", None) or "dm"
    target_id = (
        getattr(message, "slack_channel_id", None)
        if target_type == "channel"
        else getattr(message, "slack_user_id", None)
    )
    if not target_id:
        raise ServiceValidationError(
            code="slack_outbox_target_missing",
            message="Slack outbox target id is missing.",
            entity_id=getattr(message, "outbox_id", None),
        )
    return (
        str(project_id or getattr(message, "project_id", "")),
        str(target_type),
        str(target_id),
        str(message.content_hash),
    )


def _outbox_body_hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _pm_claim_key(claim) -> tuple[object, ...]:
    return (
        claim.evidence_type,
        claim.resource_id,
        claim.process_id,
        claim.process_symbol,
        claim.obligation_id,
        claim.content_hash,
    )


def _merge_pm_evidence_claims(existing: list, incoming: list) -> list:
    output = list(existing)
    seen = {_pm_claim_key(claim) for claim in output}
    for claim in incoming:
        key = _pm_claim_key(claim)
        if key in seen:
            continue
        output.append(claim)
        seen.add(key)
    return output


DEFAULT_MISSING_PROCESS_ROLE_ID = "role_res_josh"
DEFAULT_MISSING_PROCESS_ROLE_NAME = "Josh"
DEFAULT_MISSING_PROCESS_RESOURCE_ID = "res_josh"
DEFAULT_MISSING_PROCESS_RESOURCE_NAME = "Josh"


class RetiredProcessRecord(ProcessRecord):
    """Process projection with soft-retirement audit fields."""

    is_active: bool = False
    retired_at: dt.datetime

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        if kwargs.get("mode") == "json":
            for field in ("retired_at",):
                if isinstance(data.get(field), str) and data[field].endswith("Z"):
                    data[field] = f"{data[field][:-1]}+00:00"
        return data


class RecordDict(dict):
    """Dictionary row with a Pydantic-like dump method for tests."""

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return copy.deepcopy(dict(self))


def _scope_get(scope: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(scope, dict):
        return scope.get(field_name, default)
    return getattr(scope, field_name, default)


class ProjectRepository(Protocol):
    """Persistence contract used by the service layer."""

    def create_project(
        self,
        name: str,
        start_at: dt.datetime,
        default_currency: str = "USD",
        project_id: str | None = None,
    ) -> ProjectRecord:
        """Create and persist a project."""

    def list_projects(self) -> list[ProjectRecord]:
        """Return all projects."""

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        start_at: dt.datetime | None = None,
        default_currency: str | None = None,
    ) -> ProjectRecord:
        """Update mutable project metadata."""

    def delete_project(self, project_id: str) -> None:
        """Delete a project and all project-owned data."""

    def create_role(
        self,
        project_id: str,
        name: str,
        role_id: str | None = None,
    ) -> str:
        """Create and persist a project role."""

    def rename_role(
        self,
        project_id: str,
        role_id: str,
        name: str,
    ) -> str:
        """Rename a project role."""

    def get_project(self, project_id: str) -> ProjectRecord:
        """Return a project by id."""

    def upsert_process_revision(
        self,
        project_id: str,
        process_id: str | None,
        process_type: str,
        name: str,
        description: str,
        effective_at: dt.datetime,
        duration_business_days: int,
        dependencies: list[str],
        earliest_start_at: dt.datetime | None,
        start_at_earliest: bool,
        delay_after_dependencies_business_days: int,
        required_roles: dict[str, float],
        role_requirements: list[RoleRequirementCommand],
        assumption_note: str | None,
    ) -> tuple[ProcessRecord, ProcessRevisionRecord]:
        """Create a process if needed and append a planning revision."""

    def delete_process(
        self,
        project_id: str,
        process_id: str,
        edit_at: dt.datetime,
    ) -> dict[str, Any]:
        """Delete a process and remove graph facts that reference it."""

    def set_process_status(
        self,
        project_id: str,
        process_id: str,
        status: ProcessStatus,
        edit_at: dt.datetime,
    ) -> tuple[ProcessRecord, str]:
        """Validate a lifecycle assertion without storing process state."""

    def upsert_process_role_pin(
        self,
        pin: ProcessRolePinRecord,
    ) -> ProcessRolePinRecord:
        """Create or update a process-role pin."""

    def delete_process_role_pin(
        self,
        project_id: str,
        pin_id: str,
    ) -> None:
        """Delete a process-role pin."""

    def list_process_role_pins(
        self,
        project_id: str,
        as_of: dt.datetime | None = None,
        process_id: str | None = None,
        resource_id: str | None = None,
        include_done: bool = True,
    ) -> list[ProcessRolePinRecord]:
        """List process-role pins."""

    def delete_invalid_process_role_pins(
        self,
        *,
        project_id: str | None = None,
        process_id: str | None = None,
        edit_at: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Delete process-role pins that cannot be valid at the edit time."""

    def resolve_process_id(self, project_id: str, process_symbol: str) -> str:
        """Resolve a project-scoped process symbol or alias to a process id."""

    def upsert_milestone(
        self,
        milestone: MilestoneRecord,
    ) -> MilestoneRecord:
        """Create or update a named milestone subset."""

    def set_milestone_active(
        self,
        project_id: str,
        milestone_id: str,
        active: bool,
        updated_at: dt.datetime,
    ) -> MilestoneRecord:
        """Activate or deactivate a milestone."""

    def list_milestones(
        self,
        project_id: str,
        include_inactive: bool = False,
    ) -> list[MilestoneRecord]:
        """List project milestones."""

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
        resource_type: str,
        role_ids: list[str],
        calendar_id: str,
        available_from_at: dt.datetime,
        cost_rate: Any,
        cost_unit: CostUnit,
        cost_currency: str | None = None,
        available_until_at: dt.datetime | None = None,
        holidays: list[ResourceHolidayCommand] | None = None,
        calendar_overrides: list[ResourceCalendarOverrideCommand] | None = None,
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

    def upsert_slack_project_config(
        self,
        config: SlackProjectConfigRecord,
    ) -> SlackProjectConfigRecord:
        """Create or update optional Slack settings for a project."""

    def get_slack_project_config(
        self,
        project_id: str,
    ) -> SlackProjectConfigRecord:
        """Return Slack settings or a disabled default when absent."""

    def set_resource_slack_user(
        self,
        mapping: SlackResourceMappingRecord,
    ) -> SlackResourceMappingRecord:
        """Set or clear a resource's Slack user mapping."""

    def list_resource_slack_mappings(
        self,
        project_id: str,
    ) -> list[SlackResourceMappingRecord]:
        """List project Slack resource mappings."""

    def record_slack_collection_cursor(
        self,
        cursor: SlackCollectionCursorRecord,
    ) -> SlackCollectionCursorRecord:
        """Create or update a Slack collection cursor."""

    def list_slack_collection_cursors(
        self,
        project_id: str,
    ) -> list[SlackCollectionCursorRecord]:
        """List project Slack collection cursors."""

    def store_slack_bot_token(
        self,
        token: SlackEncryptedTokenRecord,
    ) -> SlackEncryptedTokenRecord:
        """Store an encrypted UI-managed Slack bot token blob."""

    def get_slack_bot_token(
        self,
        project_id: str,
    ) -> SlackEncryptedTokenRecord | None:
        """Return encrypted UI-managed Slack bot token blob when present."""

    def clear_slack_bot_token(
        self,
        project_id: str,
    ) -> None:
        """Remove encrypted UI-managed Slack bot token blob."""

    def start_slack_run(
        self,
        run: SlackRunRecord,
    ) -> SlackRunRecord:
        """Create one active Slack background run for a project."""

    def finish_slack_run(
        self,
        project_id: str,
        run_id: str,
        *,
        status: SlackRunStatus,
        finished_at: dt.datetime,
        collected_message_count: int = 0,
        draft_outbox_ids: list[str] | None = None,
        result_json: dict[str, Any] | None = None,
        error_text: str | None = None,
    ) -> SlackRunRecord:
        """Finish a Slack background run."""

    def list_slack_runs(
        self,
        project_id: str,
        statuses: list[SlackRunStatus] | None = None,
        limit: int | None = None,
    ) -> list[SlackRunRecord]:
        """List Slack background runs."""

    def create_slack_outbox_messages(
        self,
        project_id: str,
        messages: list[SlackOutboxMessageCommand],
    ) -> dict[str, list[str]]:
        """Create deduplicated Slack outbox rows."""

    def mark_slack_outbox_sent(
        self,
        project_id: str,
        outbox_id: str,
        sent_at: dt.datetime,
        slack_channel_id: str,
        slack_message_ts: str,
        run_id: str | None = None,
    ) -> SlackOutboxRecord:
        """Mark a Slack outbox row sent."""

    def mark_slack_outbox_failed(
        self,
        project_id: str,
        outbox_id: str,
        failed_at: dt.datetime,
        error_text: str,
        run_id: str | None = None,
    ) -> SlackOutboxRecord:
        """Mark a Slack outbox row failed."""

    def update_slack_outbox_body(
        self,
        project_id: str,
        outbox_id: str,
        body: str,
        updated_at: dt.datetime,
        run_id: str | None = None,
    ) -> SlackOutboxRecord:
        """Update an editable draft Slack outbox row."""

    def mark_slack_outbox_skipped(
        self,
        project_id: str,
        outbox_id: str,
        skipped_at: dt.datetime,
        reason: str | None = None,
        run_id: str | None = None,
    ) -> SlackOutboxRecord:
        """Mark a Slack outbox row skipped."""

    def list_slack_outbox(
        self,
        project_id: str,
        statuses: list[SlackOutboxStatus],
        limit: int | None = None,
    ) -> list[SlackOutboxRecord]:
        """List Slack outbox rows matching statuses."""

    def record_pm_communication_evidence(
        self,
        evidence: PMCommunicationEvidenceRecord,
    ) -> PMCommunicationEvidenceRecord:
        """Persist proof that a PM communication protocol item was sent."""

    def list_pm_communication_evidence(
        self,
        project_id: str,
    ) -> list[PMCommunicationEvidenceRecord]:
        """List PM communication evidence for a project."""

    def upsert_process_evidence_line_item(
        self,
        record: ProcessEvidenceLineItemRecord,
    ) -> ProcessEvidenceLineItemRecord:
        """Create or update PM evidence recency for a process line item."""

    def list_process_evidence_line_items(
        self,
        project_id: str,
        process_id: str | None = None,
        line_items: list[str] | None = None,
    ) -> list[ProcessEvidenceLineItemRecord]:
        """List PM evidence recency rows for process line items."""

    def upsert_resource_evidence_line_item(
        self,
        record: ResourceEvidenceLineItemRecord,
    ) -> ResourceEvidenceLineItemRecord:
        """Create or update PM evidence recency for a resource line item."""

    def list_resource_evidence_line_items(
        self,
        project_id: str,
        resource_id: str | None = None,
        line_items: list[str] | None = None,
    ) -> list[ResourceEvidenceLineItemRecord]:
        """List PM evidence recency rows for resource line items."""

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
        resolution_owner_resource_id: str | None = None,
    ) -> BlockerRecord:
        """Add a process blocker."""

    def resolve_blocker(
        self,
        project_id: str,
        blocker_id: str,
        resolved_at: dt.datetime,
        resolution: str | None = None,
        resolution_owner_resource_id: str | None = None,
    ) -> BlockerRecord:
        """Resolve a process blocker."""

    def set_blocker_resolution_owner(
        self,
        project_id: str,
        blocker_id: str,
        resolution_owner_resource_id: str | None,
    ) -> BlockerRecord:
        """Set or clear the resource responsible for resolving a blocker."""

    def reopen_blocker(
        self,
        project_id: str,
        blocker_id: str,
    ) -> BlockerRecord:
        """Clear a blocker resolution."""

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

    def record_schedule_snapshot(
        self,
        snapshot: ScheduleSnapshotRecord,
    ) -> ScheduleSnapshotRecord:
        """Persist a committed schedule snapshot."""

    def schedule_snapshots_as_of(
        self,
        project_id: str,
        as_of: dt.datetime,
        terminal_process_symbols: list[str] | None = None,
    ) -> list[ScheduleSnapshotRecord]:
        """Return committed schedule snapshots visible at a time."""

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
        self.process_role_pins: dict[str, ProcessRolePinRecord] = {}
        self.process_role_pin_ids_by_project: dict[str, list[str]] = defaultdict(list)
        self.roles: dict[str, dict[str, Any]] = {}
        self.role_ids_by_project: dict[str, list[str]] = defaultdict(list)
        self.resources: dict[str, dict[str, Any]] = {}
        self.resource_ids_by_project: dict[str, list[str]] = defaultdict(list)
        self.calendars: dict[str, dict[str, Any]] = {}
        self.calendar_ids_by_project: dict[str, list[str]] = defaultdict(list)
        self.retired_processes: dict[str, dict[str, Any]] = {}
        self.process_aliases: dict[str, dict[str, str]] = defaultdict(dict)
        self.process_alias_sources: dict[str, dict[str, str]] = defaultdict(dict)
        self.dependency_edge_ids: dict[tuple[str, str, str], str] = {}
        self.schedule_snapshots: list[ScheduleSnapshotRecord] = []
        self.milestones: dict[str, MilestoneRecord] = {}
        self.milestone_ids_by_project: dict[str, list[str]] = defaultdict(list)
        self.slack_project_configs: dict[str, SlackProjectConfigRecord] = {}
        self.slack_resource_mappings: dict[
            tuple[str, str],
            SlackResourceMappingRecord,
        ] = {}
        self.slack_collection_cursors: dict[
            tuple[str, str],
            SlackCollectionCursorRecord,
        ] = {}
        self.slack_encrypted_tokens: dict[str, SlackEncryptedTokenRecord] = {}
        self.slack_runs: dict[str, SlackRunRecord] = {}
        self.slack_run_ids_by_project: dict[str, list[str]] = defaultdict(list)
        self.slack_outbox: dict[str, SlackOutboxRecord] = {}
        self.slack_outbox_ids_by_project: dict[str, list[str]] = defaultdict(list)
        self.slack_outbox_dedupe: dict[tuple[str, str, str, str], str] = {}
        self.pm_communication_evidence: dict[str, PMCommunicationEvidenceRecord] = {}
        self.pm_communication_evidence_ids_by_project: dict[
            str,
            list[str],
        ] = defaultdict(list)
        self.process_evidence_line_items: dict[str, ProcessEvidenceLineItemRecord] = {}
        self.process_evidence_line_item_ids_by_project: dict[
            str,
            list[str],
        ] = defaultdict(list)
        self.resource_evidence_line_items: dict[str, ResourceEvidenceLineItemRecord] = {}
        self.resource_evidence_line_item_ids_by_project: dict[
            str,
            list[str],
        ] = defaultdict(list)

    def create_project(
        self,
        name: str,
        start_at: dt.datetime,
        default_currency: str = "USD",
        project_id: str | None = None,
    ) -> ProjectRecord:
        resolved_project_id = project_id or new_id()
        if resolved_project_id in self.projects:
            raise ServiceValidationError(
                code="project_conflict",
                message="Project id already exists.",
                entity_id=resolved_project_id,
            )
        project = ProjectRecord(
            project_id=resolved_project_id,
            name=name,
            start_at=start_at,
            default_currency=default_currency,
        )
        self.projects[project.project_id] = project
        return project

    def list_projects(self) -> list[ProjectRecord]:
        """Return all projects sorted for stable UI selection."""
        return sorted(
            self.projects.values(),
            key=lambda project: (project.name.casefold(), project.project_id),
        )

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        start_at: dt.datetime | None = None,
        default_currency: str | None = None,
    ) -> ProjectRecord:
        """Update mutable project metadata."""
        project = self.get_project(project_id)
        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = name
        if start_at is not None:
            updates["start_at"] = start_at
        if default_currency is not None:
            updates["default_currency"] = default_currency
        updated = project.model_copy(update=updates)
        self.projects[project_id] = updated
        if default_currency is not None:
            self._set_project_resource_currency(project_id, default_currency)
        return updated

    def delete_project(self, project_id: str) -> None:
        """Delete a project and every project-owned fact."""
        self.get_project(project_id)
        process_ids = list(self.process_ids_by_project.get(project_id, []))
        role_ids = list(self.role_ids_by_project.get(project_id, []))
        resource_ids = list(self.resource_ids_by_project.get(project_id, []))
        calendar_ids = list(self.calendar_ids_by_project.get(project_id, []))
        blocker_ids = list(self.blocker_ids_by_project.get(project_id, []))
        milestone_ids = list(self.milestone_ids_by_project.get(project_id, []))

        for process_id in process_ids:
            self.processes.pop(process_id, None)
            for pin_id in list(self.process_role_pin_ids_by_project.get(project_id, [])):
                pin = self.process_role_pins.get(pin_id)
                if pin is not None and pin.process_id == process_id:
                    self.process_role_pins.pop(pin_id, None)
            for revision in self.revisions_by_process.pop(process_id, []):
                for requirement in revision.role_requirements:
                    if requirement.requirement_id is not None:
                        self.role_requirements.pop(requirement.requirement_id, None)
            self.retired_processes.pop(process_id, None)
        for role_id in role_ids:
            self.roles.pop(role_id, None)
        for resource_id in resource_ids:
            self.resources.pop(resource_id, None)
        for calendar_id in calendar_ids:
            self.calendars.pop(calendar_id, None)
        for blocker_id in blocker_ids:
            self.blockers.pop(blocker_id, None)
        for milestone_id in milestone_ids:
            self.milestones.pop(milestone_id, None)

        self.projects.pop(project_id, None)
        self.process_ids_by_project.pop(project_id, None)
        self.role_ids_by_project.pop(project_id, None)
        self.resource_ids_by_project.pop(project_id, None)
        self.calendar_ids_by_project.pop(project_id, None)
        self.blocker_ids_by_project.pop(project_id, None)
        self.milestone_ids_by_project.pop(project_id, None)
        self.process_aliases.pop(project_id, None)
        self.process_alias_sources.pop(project_id, None)
        self.process_role_pin_ids_by_project.pop(project_id, None)
        self.dependency_edge_ids = {
            key: edge_id
            for key, edge_id in self.dependency_edge_ids.items()
            if key[0] != project_id
        }
        self.role_requirements = {
            requirement.requirement_id: requirement
            for revisions in self.revisions_by_process.values()
            for revision in revisions
            for requirement in revision.role_requirements
            if requirement.requirement_id is not None
        }
        self.slack_project_configs.pop(project_id, None)
        self.slack_encrypted_tokens.pop(project_id, None)
        self.slack_resource_mappings = {
            key: mapping
            for key, mapping in self.slack_resource_mappings.items()
            if key[0] != project_id
        }
        self.slack_collection_cursors = {
            key: cursor
            for key, cursor in self.slack_collection_cursors.items()
            if key[0] != project_id
        }
        for outbox_id in self.slack_outbox_ids_by_project.pop(project_id, []):
            self.slack_outbox.pop(outbox_id, None)
        for run_id in self.slack_run_ids_by_project.pop(project_id, []):
            self.slack_runs.pop(run_id, None)
        self.slack_outbox_dedupe = {
            key: outbox_id
            for key, outbox_id in self.slack_outbox_dedupe.items()
            if key[0] != project_id
        }
        for evidence_id in self.pm_communication_evidence_ids_by_project.pop(
            project_id,
            [],
        ):
            self.pm_communication_evidence.pop(evidence_id, None)
        for evidence_line_id in self.process_evidence_line_item_ids_by_project.pop(
            project_id,
            [],
        ):
            self.process_evidence_line_items.pop(evidence_line_id, None)
        for evidence_line_id in self.resource_evidence_line_item_ids_by_project.pop(
            project_id,
            [],
        ):
            self.resource_evidence_line_items.pop(evidence_line_id, None)

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

    def rename_role(
        self,
        project_id: str,
        role_id: str,
        name: str,
    ) -> str:
        role = self._get_role(project_id, role_id)
        for existing_id in self.role_ids_by_project[project_id]:
            existing = self.roles[existing_id]
            if (
                existing_id != role_id
                and existing["active"]
                and existing["name"] == name
            ):
                raise ServiceValidationError(
                    code="duplicate_role_name",
                    message="Active role names must be unique within a project.",
                    field_path="name",
                )
        role["name"] = name
        return role_id

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
        process_type: str,
        name: str,
        description: str,
        effective_at: dt.datetime,
        duration_business_days: int,
        dependencies: list[str],
        earliest_start_at: dt.datetime | None,
        start_at_earliest: bool,
        delay_after_dependencies_business_days: int,
        required_roles: dict[str, float],
        role_requirements: list[RoleRequirementCommand],
        assumption_note: str | None,
    ) -> tuple[ProcessRecord, ProcessRevisionRecord]:
        self.get_project(project_id)
        if len(role_requirements) > 1:
            raise ServiceValidationError(
                code="process_role_requirement_count_invalid",
                message="Active processes must define exactly one role requirement.",
                field_path="role_requirements",
            )

        if process_id is None:
            process = ProcessRecord(
                process_id=new_id(),
                project_id=project_id,
                symbol=self._unique_symbol(project_id, self._symbol_from_name(name)),
                process_type=process_type,
            )
            self.processes[process.process_id] = process
            self.process_ids_by_project[project_id].append(process.process_id)
        elif process_id in self.processes:
            process = self._get_process(project_id, process_id)
            if process.process_type != process_type:
                process = process.model_copy(update={"process_type": process_type})
                self.processes[process.process_id] = process
        else:
            process = ProcessRecord(
                process_id=process_id,
                project_id=project_id,
                symbol=self._unique_symbol(project_id, process_id),
                process_type=process_type,
            )
            self.processes[process.process_id] = process
            self.process_ids_by_project[project_id].append(process.process_id)

        role_requirements = self._role_requirements_or_default(
            project_id,
            process.process_id,
            role_requirements,
        )
        self._validate_active_role_requirements(project_id, role_requirements)

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
            description=description,
            duration_business_days=duration_business_days,
            dependencies=dependencies,
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
        self.delete_invalid_process_role_pins(
            project_id=project_id,
            process_id=process.process_id,
            edit_at=effective_at,
        )
        return process, revision

    def delete_process(
        self,
        project_id: str,
        process_id: str,
        edit_at: dt.datetime,
    ) -> dict[str, Any]:
        """Delete a process and remove graph facts that reference it."""
        self.get_project(project_id)
        self._get_process(project_id, process_id)

        process_ids_to_delete: list[str] = []
        process_id_set: set[str] = set()
        blocker_ids_to_delete: list[str] = []
        blocker_id_set: set[str] = set()

        def project_blockers() -> list[BlockerRecord]:
            return [
                self.blockers[blocker_id]
                for blocker_id in self.blocker_ids_by_project.get(project_id, [])
                if blocker_id in self.blockers
            ]

        def resolver_process_id(blocker: BlockerRecord) -> str | None:
            resolver_symbol = self._blocker_resolver_symbol(blocker.blocker_id)
            try:
                return self.resolve_process_id(project_id, resolver_symbol)
            except ServiceValidationError as exc:
                if exc.code != "not_found":
                    raise
                return None

        def remaining_successors(
            predecessor_id: str | None,
            excluding_process_ids: set[str],
        ) -> list[str]:
            if predecessor_id is None:
                return []
            successors = []
            for candidate_id in self.process_ids_by_project.get(project_id, []):
                if candidate_id in excluding_process_ids:
                    continue
                revision = self._latest_revision_as_of(candidate_id, edit_at)
                if revision is None:
                    continue
                if predecessor_id in revision.dependencies:
                    successors.append(candidate_id)
            return sorted(successors)

        def collect_blocker(blocker_id: str) -> None:
            blocker = self.blockers.get(blocker_id)
            if blocker is None or blocker.project_id != project_id:
                return
            if blocker_id not in blocker_id_set:
                blocker_id_set.add(blocker_id)
                blocker_ids_to_delete.append(blocker_id)
            resolver_id = resolver_process_id(blocker)
            if resolver_id is not None and resolver_id not in process_id_set:
                collect_process(resolver_id)

        def collect_process(candidate_id: str) -> None:
            self._get_process(project_id, candidate_id)
            if candidate_id in process_id_set:
                return
            process_id_set.add(candidate_id)
            process_ids_to_delete.append(candidate_id)

        collect_process(process_id)

        changed = True
        while changed:
            changed = False
            for blocker in project_blockers():
                if blocker.blocker_id in blocker_id_set:
                    continue
                resolver_id = resolver_process_id(blocker)
                if resolver_id in process_id_set:
                    collect_blocker(blocker.blocker_id)
                    changed = True
                    continue
                if blocker.process_id not in process_id_set:
                    continue
                if not remaining_successors(resolver_id, process_id_set):
                    collect_blocker(blocker.blocker_id)
                    changed = True

        for blocker in project_blockers():
            if blocker.blocker_id in blocker_id_set:
                continue
            if blocker.process_id not in process_id_set:
                continue
            successors = remaining_successors(
                resolver_process_id(blocker),
                process_id_set,
            )
            if successors:
                self.blockers[blocker.blocker_id] = blocker.model_copy(
                    update={"process_id": successors[0]}
                )

        removed_dependency_pairs: set[tuple[str, str]] = set()
        for candidate_id, revisions in list(self.revisions_by_process.items()):
            for revision in revisions:
                if candidate_id in process_id_set:
                    for dependency_id in revision.dependencies:
                        removed_dependency_pairs.add((dependency_id, candidate_id))
                    continue
                for dependency_id in revision.dependencies:
                    if dependency_id in process_id_set:
                        removed_dependency_pairs.add((dependency_id, candidate_id))

        for candidate_id, revisions in list(self.revisions_by_process.items()):
            if candidate_id in process_id_set:
                continue
            cleaned_revisions = []
            for revision in revisions:
                dependencies = [
                    dependency_id
                    for dependency_id in revision.dependencies
                    if dependency_id not in process_id_set
                ]
                if dependencies != revision.dependencies:
                    revision = revision.model_copy(update={"dependencies": dependencies})
                cleaned_revisions.append(revision)
            self.revisions_by_process[candidate_id] = cleaned_revisions

        deleted_symbols: set[str] = set()
        for candidate_id in process_ids_to_delete:
            process = self.processes.pop(candidate_id, None)
            if process is not None:
                deleted_symbols.add(process.symbol)
            for revision in self.revisions_by_process.pop(candidate_id, []):
                for requirement in revision.role_requirements:
                    if requirement.requirement_id is not None:
                        self.role_requirements.pop(requirement.requirement_id, None)
            self.retired_processes.pop(candidate_id, None)

        self.process_ids_by_project[project_id] = [
            candidate_id
            for candidate_id in self.process_ids_by_project.get(project_id, [])
            if candidate_id not in process_id_set
        ]

        deleted_aliases: set[str] = set()
        aliases = self.process_aliases.get(project_id, {})
        alias_sources = self.process_alias_sources.get(project_id, {})
        for alias, target_id in list(aliases.items()):
            if target_id in process_id_set:
                deleted_aliases.add(alias)
                aliases.pop(alias, None)
                alias_sources.pop(alias, None)

        for blocker_id in blocker_ids_to_delete:
            self.blockers.pop(blocker_id, None)
        self.blocker_ids_by_project[project_id] = [
            blocker_id
            for blocker_id in self.blocker_ids_by_project.get(project_id, [])
            if blocker_id not in blocker_id_set
        ]

        self.dependency_edge_ids = {
            key: edge_id
            for key, edge_id in self.dependency_edge_ids.items()
            if key[0] != project_id
            or (key[1] not in process_id_set and key[2] not in process_id_set)
        }

        pin_ids_to_delete = [
            pin_id
            for pin_id in self.process_role_pin_ids_by_project.get(project_id, [])
            if (
                pin_id in self.process_role_pins
                and self.process_role_pins[pin_id].process_id in process_id_set
            )
        ]
        for pin_id in pin_ids_to_delete:
            self.process_role_pins.pop(pin_id, None)
        self.process_role_pin_ids_by_project[project_id] = [
            pin_id
            for pin_id in self.process_role_pin_ids_by_project.get(project_id, [])
            if pin_id not in set(pin_ids_to_delete)
        ]

        evidence_line_ids_to_delete = [
            evidence_line_id
            for evidence_line_id in self.process_evidence_line_item_ids_by_project.get(
                project_id,
                [],
            )
            if (
                evidence_line_id in self.process_evidence_line_items
                and self.process_evidence_line_items[evidence_line_id].process_id
                in process_id_set
            )
        ]
        for evidence_line_id in evidence_line_ids_to_delete:
            self.process_evidence_line_items.pop(evidence_line_id, None)
        self.process_evidence_line_item_ids_by_project[project_id] = [
            evidence_line_id
            for evidence_line_id in self.process_evidence_line_item_ids_by_project.get(
                project_id,
                [],
            )
            if evidence_line_id not in set(evidence_line_ids_to_delete)
        ]

        deleted_symbol_names = deleted_symbols | deleted_aliases
        for milestone_id in self.milestone_ids_by_project.get(project_id, []):
            milestone = self.milestones.get(milestone_id)
            if milestone is None:
                continue
            process_symbols = [
                symbol
                for symbol in milestone.process_symbols
                if symbol not in deleted_symbol_names
            ]
            if process_symbols != milestone.process_symbols:
                self.milestones[milestone_id] = milestone.model_copy(
                    update={"process_symbols": process_symbols, "updated_at": edit_at}
                )

        self.role_requirements = {
            requirement.requirement_id: requirement
            for revisions in self.revisions_by_process.values()
            for revision in revisions
            for requirement in revision.role_requirements
            if requirement.requirement_id is not None
        }

        return {
            "process_id": process_id,
            "deleted_process_ids": process_ids_to_delete,
            "deleted_blocker_ids": blocker_ids_to_delete,
            "removed_dependency_count": len(removed_dependency_pairs),
            "removed_dependencies": [
                {
                    "predecessor_process_id": predecessor_id,
                    "successor_process_id": successor_id,
                }
                for predecessor_id, successor_id in sorted(removed_dependency_pairs)
            ],
            "deleted_process_role_pin_ids": pin_ids_to_delete,
            "deleted_evidence_line_ids": evidence_line_ids_to_delete,
        }

    def set_process_status(
        self,
        project_id: str,
        process_id: str,
        status: ProcessStatus,
        edit_at: dt.datetime,
    ) -> tuple[ProcessRecord, str]:
        process = self._get_process(project_id, process_id)
        if status in {ProcessStatus.PAUSED, ProcessStatus.CANCELED}:
            raise ServiceValidationError(
                code="process_state_not_stored",
                message=(
                    "Process lifecycle state is derived from process-role pins; "
                    "paused and canceled are not stored process states."
                ),
                field_path="status",
                entity_id=process_id,
            )
        if status == ProcessStatus.PLANNED and self.list_process_role_pins(
            project_id,
            process_id=process_id,
            include_done=True,
        ):
            raise ServiceValidationError(
                code="started_state_derived_from_pins",
                message=(
                    "Started state is derived from process-role pins. "
                    "Delete process-role pins before returning a process to planned."
                ),
                entity_id=process_id,
            )
        if status in {ProcessStatus.IN_PROGRESS, ProcessStatus.PAUSED}:
            self._validate_process_has_started_pin(
                project_id=project_id,
                process_id=process_id,
                edit_at=edit_at,
            )
        if status == ProcessStatus.DONE:
            self._validate_process_role_pins_done_for_done(
                project_id=project_id,
                process_id=process_id,
                edit_at=edit_at,
            )
        return process, new_id()

    def upsert_process_role_pin(
        self,
        pin: ProcessRolePinRecord,
    ) -> ProcessRolePinRecord:
        self.get_project(pin.project_id)
        self._get_process(pin.project_id, pin.process_id)
        self._validate_process_role_pin(pin)
        existing = self.process_role_pins.get(pin.pin_id)
        if existing is not None and existing.project_id != pin.project_id:
            raise ServiceValidationError(
                code="cross_project_process_role_pin",
                message="Process-role pin belongs to another project.",
                entity_id=pin.pin_id,
            )
        if existing is None and pin.pin_id not in self.process_role_pin_ids_by_project[
            pin.project_id
        ]:
            self.process_role_pin_ids_by_project[pin.project_id].append(pin.pin_id)
        created_at = existing.created_at if existing is not None else pin.created_at
        normalized = pin.model_copy(update={"created_at": created_at})
        self.process_role_pins[normalized.pin_id] = normalized
        return normalized

    def delete_process_role_pin(
        self,
        project_id: str,
        pin_id: str,
    ) -> None:
        pin = self.process_role_pins.get(pin_id)
        if pin is None:
            raise ServiceValidationError(
                code="process_role_pin_not_found",
                message=f"Process-role pin {pin_id!r} does not exist.",
                entity_id=pin_id,
            )
        if pin.project_id != project_id:
            raise ServiceValidationError(
                code="cross_project_process_role_pin",
                message="Process-role pin does not belong to the requested project.",
                entity_id=pin_id,
            )
        self.process_role_pins.pop(pin_id, None)
        if pin_id in self.process_role_pin_ids_by_project.get(project_id, []):
            self.process_role_pin_ids_by_project[project_id].remove(pin_id)
        self._sync_process_lifecycle_after_pin_delete(project_id, pin.process_id, pin)

    def list_process_role_pins(
        self,
        project_id: str,
        as_of: dt.datetime | None = None,
        process_id: str | None = None,
        resource_id: str | None = None,
        include_done: bool = True,
    ) -> list[ProcessRolePinRecord]:
        self.get_project(project_id)
        rows = [
            self.process_role_pins[pin_id]
            for pin_id in self.process_role_pin_ids_by_project.get(project_id, [])
            if pin_id in self.process_role_pins
        ]
        if as_of is not None:
            rows = [row for row in rows if row.pinned_at <= as_of]
        if process_id is not None:
            rows = [row for row in rows if row.process_id == process_id]
        if resource_id is not None:
            rows = [row for row in rows if row.resource_id == resource_id]
        if not include_done:
            rows = [row for row in rows if row.status != "pinned_finished"]
        return sorted(
            rows,
            key=lambda row: (
                row.pinned_at,
                row.forecast_finish_at,
                row.resource_id,
                row.process_id,
                row.role_id,
                row.pin_id,
            ),
        )

    def delete_invalid_process_role_pins(
        self,
        *,
        project_id: str | None = None,
        process_id: str | None = None,
        edit_at: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Delete future or process-role-orphaned pins."""
        effective_at = edit_at or dt.datetime.now(dt.UTC)
        project_ids = [project_id] if project_id is not None else sorted(self.projects)
        deleted_pin_ids: list[str] = []
        deleted_pin_reasons: dict[str, str] = {}
        for current_project_id in project_ids:
            self.get_project(current_project_id)
            for pin_id in list(
                self.process_role_pin_ids_by_project.get(current_project_id, [])
            ):
                pin = self.process_role_pins.get(pin_id)
                if pin is None:
                    continue
                if process_id is not None and pin.process_id != process_id:
                    continue
                reason = self._invalid_process_role_pin_reason(pin, effective_at)
                if reason is None:
                    continue
                self.process_role_pins.pop(pin_id, None)
                if pin_id in self.process_role_pin_ids_by_project.get(
                    current_project_id,
                    [],
                ):
                    self.process_role_pin_ids_by_project[current_project_id].remove(
                        pin_id,
                    )
                self._sync_process_lifecycle_after_pin_delete(
                    current_project_id,
                    pin.process_id,
                    pin,
                )
                deleted_pin_ids.append(pin_id)
                deleted_pin_reasons[pin_id] = reason
        return {
            "deleted_pin_ids": deleted_pin_ids,
            "deleted_pin_count": len(deleted_pin_ids),
            "deleted_pin_reasons": deleted_pin_reasons,
        }

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

    def upsert_milestone(self, milestone: MilestoneRecord) -> MilestoneRecord:
        self.get_project(milestone.project_id)
        if milestone.milestone_id not in self.milestones:
            self.milestone_ids_by_project[milestone.project_id].append(
                milestone.milestone_id,
            )
        else:
            existing = self.milestones[milestone.milestone_id]
            if existing.project_id != milestone.project_id:
                raise ServiceValidationError(
                    code="milestone_project_conflict",
                    message="Milestone id already belongs to another project.",
                    entity_id=milestone.milestone_id,
                )
        self.milestones[milestone.milestone_id] = milestone
        return milestone

    def set_milestone_active(
        self,
        project_id: str,
        milestone_id: str,
        active: bool,
        updated_at: dt.datetime,
    ) -> MilestoneRecord:
        milestone = self._get_milestone(project_id, milestone_id)
        updated = milestone.model_copy(
            update={
                "active": active,
                "updated_at": updated_at,
            }
        )
        self.milestones[milestone_id] = updated
        return updated

    def list_milestones(
        self,
        project_id: str,
        include_inactive: bool = False,
    ) -> list[MilestoneRecord]:
        self.get_project(project_id)
        output = [
            self.milestones[milestone_id]
            for milestone_id in self.milestone_ids_by_project.get(project_id, [])
            if milestone_id in self.milestones
            and (include_inactive or self.milestones[milestone_id].active)
        ]
        return sorted(output, key=lambda item: (item.name.casefold(), item.milestone_id))

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
        resource_type: str,
        role_ids: list[str],
        calendar_id: str,
        available_from_at: dt.datetime,
        cost_rate: Any,
        cost_unit: CostUnit,
        cost_currency: str | None = None,
        available_until_at: dt.datetime | None = None,
        holidays: list[ResourceHolidayCommand] | None = None,
        calendar_overrides: list[ResourceCalendarOverrideCommand] | None = None,
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
        calendar_override_records = self._validated_calendar_overrides(
            project_id,
            calendar_overrides or [],
            active=active,
        )
        holiday_records = []
        seen_holiday_ids: set[str] = set()
        for holiday in holidays or []:
            holiday_id = holiday.holiday_id or new_id()
            if holiday_id in seen_holiday_ids:
                raise ServiceValidationError(
                    code="duplicate_resource_holiday",
                    message="Resource holiday ids must be unique.",
                    entity_id=holiday_id,
                    field_path="holidays",
                )
            seen_holiday_ids.add(holiday_id)
            holiday_records.append(
                {
                    "holiday_id": holiday_id,
                    "starts_at": holiday.starts_at,
                    "ends_at": holiday.ends_at,
                    "reason": holiday.reason,
                }
            )
        project = self.get_project(project_id)
        self.resources[resolved_resource_id] = RecordDict({
            "resource_id": resolved_resource_id,
            "project_id": project_id,
            "name": name,
            "resource_type": resource_type,
            "role_ids": list(role_ids),
            "calendar_id": calendar_id,
            "available_from_at": available_from_at,
            "available_until_at": available_until_at,
            "cost_rate": str(cost_rate),
            "cost_unit": getattr(cost_unit, "value", cost_unit),
            "cost_currency": cost_currency or project.default_currency,
            "holidays": holiday_records,
            "calendar_overrides": calendar_override_records,
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

    def upsert_slack_project_config(
        self,
        config: SlackProjectConfigRecord,
    ) -> SlackProjectConfigRecord:
        self.get_project(config.project_id)
        self.slack_project_configs[config.project_id] = config
        return config

    def get_slack_project_config(
        self,
        project_id: str,
    ) -> SlackProjectConfigRecord:
        self.get_project(project_id)
        return self.slack_project_configs.get(
            project_id,
            SlackProjectConfigRecord(project_id=project_id),
        )

    def set_resource_slack_user(
        self,
        mapping: SlackResourceMappingRecord,
    ) -> SlackResourceMappingRecord:
        resource = self._get_resource(mapping.project_id, mapping.resource_id)
        if not mapping.active:
            mapping = mapping.model_copy(
                update={"slack_user_id": None, "display_name": None},
            )
            resource["resource_type"] = "external"
        elif mapping.slack_user_id:
            resource["resource_type"] = "internal"
        self.slack_resource_mappings[(mapping.project_id, mapping.resource_id)] = (
            mapping
        )
        return mapping

    def list_resource_slack_mappings(
        self,
        project_id: str,
    ) -> list[SlackResourceMappingRecord]:
        self.get_project(project_id)
        return sorted(
            [
                mapping
                for mapping in self.slack_resource_mappings.values()
                if mapping.project_id == project_id
            ],
            key=lambda mapping: mapping.resource_id,
        )

    def record_slack_collection_cursor(
        self,
        cursor: SlackCollectionCursorRecord,
    ) -> SlackCollectionCursorRecord:
        self.get_project(cursor.project_id)
        self.slack_collection_cursors[(cursor.project_id, cursor.conversation_id)] = (
            cursor
        )
        return cursor

    def list_slack_collection_cursors(
        self,
        project_id: str,
    ) -> list[SlackCollectionCursorRecord]:
        self.get_project(project_id)
        return sorted(
            [
                cursor
                for cursor in self.slack_collection_cursors.values()
                if cursor.project_id == project_id
            ],
            key=lambda cursor: (cursor.conversation_type, cursor.conversation_id),
        )

    def store_slack_bot_token(
        self,
        token: SlackEncryptedTokenRecord,
    ) -> SlackEncryptedTokenRecord:
        self.get_project(token.project_id)
        existing = self.slack_encrypted_tokens.get(token.project_id)
        if existing is not None:
            token = token.model_copy(update={"created_at": existing.created_at})
        self.slack_encrypted_tokens[token.project_id] = token
        return token

    def get_slack_bot_token(
        self,
        project_id: str,
    ) -> SlackEncryptedTokenRecord | None:
        self.get_project(project_id)
        return self.slack_encrypted_tokens.get(project_id)

    def clear_slack_bot_token(
        self,
        project_id: str,
    ) -> None:
        self.get_project(project_id)
        self.slack_encrypted_tokens.pop(project_id, None)

    def start_slack_run(
        self,
        run: SlackRunRecord,
    ) -> SlackRunRecord:
        self.get_project(run.project_id)
        for existing_id in self.slack_run_ids_by_project.get(run.project_id, []):
            existing = self.slack_runs[existing_id]
            status = (
                existing.status
                if isinstance(existing.status, SlackRunStatus)
                else SlackRunStatus(existing.status)
            )
            if status.is_active:
                raise ServiceValidationError(
                    code="slack_run_already_active",
                    message="A Slack run is already active for this project.",
                    entity_id=existing.run_id,
                )
        if run.run_id in self.slack_runs:
            raise ServiceValidationError(
                code="slack_run_conflict",
                message=f"Slack run {run.run_id!r} already exists.",
                entity_id=run.run_id,
            )
        self.slack_runs[run.run_id] = run
        self.slack_run_ids_by_project[run.project_id].append(run.run_id)
        return run

    def finish_slack_run(
        self,
        project_id: str,
        run_id: str,
        *,
        status: SlackRunStatus,
        finished_at: dt.datetime,
        collected_message_count: int = 0,
        draft_outbox_ids: list[str] | None = None,
        result_json: dict[str, Any] | None = None,
        error_text: str | None = None,
    ) -> SlackRunRecord:
        if status.is_active:
            raise ServiceValidationError(
                code="slack_run_terminal_status_required",
                message="Slack run finish status must be terminal.",
                entity_id=run_id,
            )
        run = self._get_slack_run(project_id, run_id)
        if finished_at < run.started_at:
            raise ServiceValidationError(
                code="slack_run_finished_before_start",
                message="Slack run finished_at must be no earlier than started_at.",
                entity_id=run_id,
            )
        for outbox_id in draft_outbox_ids or []:
            self._get_slack_outbox(project_id, outbox_id)
        updated = run.model_copy(
            update={
                "status": status,
                "finished_at": finished_at,
                "updated_at": finished_at,
                "collected_message_count": collected_message_count,
                "draft_outbox_ids": list(draft_outbox_ids or []),
                "result_json": result_json,
                "error_text": error_text,
            },
        )
        self.slack_runs[run_id] = updated
        return updated

    def list_slack_runs(
        self,
        project_id: str,
        statuses: list[SlackRunStatus] | None = None,
        limit: int | None = None,
    ) -> list[SlackRunRecord]:
        self.get_project(project_id)
        status_values = (
            {getattr(status, "value", status) for status in statuses}
            if statuses is not None
            else None
        )
        rows = [
            self.slack_runs[run_id]
            for run_id in self.slack_run_ids_by_project.get(project_id, [])
            if status_values is None
            or getattr(self.slack_runs[run_id].status, "value", self.slack_runs[run_id].status)
            in status_values
        ]
        rows.sort(key=lambda row: (row.started_at, row.run_id), reverse=True)
        if limit is not None:
            return rows[:limit]
        return rows

    def _sync_process_lifecycle_after_pin_delete(
        self,
        project_id: str,
        process_id: str,
        deleted_pin: ProcessRolePinRecord,
    ) -> None:
        del deleted_pin
        self._get_process(project_id, process_id)

    def _process_pin_finished_at_if_all_requirements(
        self,
        project_id: str,
        process_id: str,
        pins: list[ProcessRolePinRecord],
        deleted_pin: ProcessRolePinRecord,
    ) -> dt.datetime | None:
        as_of = max(
            [
                deleted_pin.updated_at,
                deleted_pin.pinned_at,
                *[pin.updated_at for pin in pins],
                *[
                    pin.verified_done_at
                    for pin in pins
                    if pin.verified_done_at is not None
                ],
            ]
        )
        revision = self._latest_revision_as_of(process_id, as_of)
        if revision is None or not revision.role_requirements:
            return None
        verified_by_requirement: dict[str, dt.datetime] = {}
        for pin in pins:
            if (
                pin.status != "pinned_finished"
                or pin.verified_done_at is None
                or pin.verified_done_at > as_of
            ):
                continue
            verified_by_requirement[self._pin_requirement_id(pin)] = pin.verified_done_at
        for index, requirement in enumerate(revision.role_requirements):
            requirement_id = (
                requirement.requirement_id
                or self._synthetic_requirement_id(process_id, index)
            )
            if requirement_id not in verified_by_requirement:
                return None
        return max(verified_by_requirement.values(), default=None)

    def _process_pin_finished_at_as_of(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime,
    ) -> dt.datetime | None:
        revision = self._latest_revision_as_of(process_id, as_of)
        if revision is None or not revision.role_requirements:
            return None
        verified_by_requirement: dict[str, dt.datetime] = {}
        for pin in self.list_process_role_pins(
            project_id,
            as_of=as_of,
            process_id=process_id,
            include_done=True,
        ):
            if (
                pin.status != "pinned_finished"
                or pin.verified_done_at is None
                or pin.verified_done_at > as_of
            ):
                continue
            verified_by_requirement[self._pin_requirement_id(pin)] = pin.verified_done_at
        for index, requirement in enumerate(revision.role_requirements):
            requirement_id = (
                requirement.requirement_id
                or self._synthetic_requirement_id(process_id, index)
            )
            if requirement_id not in verified_by_requirement:
                return None
        return max(verified_by_requirement.values(), default=None)

    def create_slack_outbox_messages(
        self,
        project_id: str,
        messages: list[SlackOutboxMessageCommand],
    ) -> dict[str, list[str]]:
        self.get_project(project_id)
        created_ids: list[str] = []
        matched_ids: list[str] = []
        skipped_ids: list[str] = []
        for message in messages:
            if message.resource_id is not None:
                self._get_resource(project_id, message.resource_id)
            dedupe_key = slack_outbox_target_key(message, project_id)
            existing_id = self.slack_outbox_dedupe.get(dedupe_key)
            if existing_id is not None:
                existing = self.slack_outbox[existing_id]
                if existing.status == SlackOutboxStatus.DRAFT:
                    merged_claims = _merge_pm_evidence_claims(
                        list(existing.pm_evidence_claims),
                        list(message.pm_evidence_claims),
                    )
                    if merged_claims != list(existing.pm_evidence_claims):
                        self.slack_outbox[existing_id] = existing.model_copy(
                            update={
                                "pm_evidence_claims": merged_claims,
                                "run_id": message.run_id or existing.run_id,
                                "updated_at": message.created_at,
                            }
                        )
                        matched_ids.append(existing_id)
                        continue
                    matched_ids.append(existing_id)
                    skipped_ids.append(existing_id)
                    continue
            outbox_id = new_id()
            record = SlackOutboxRecord(
                outbox_id=outbox_id,
                project_id=project_id,
                # Creation always stages a draft. Delivery status is only set by
                # mark_slack_outbox_sent, which also stores Slack's channel and ts
                # needed for PM evidence audit.
                status=SlackOutboxStatus.DRAFT,
                target_type=message.target_type,
                resource_id=message.resource_id,
                slack_user_id=message.slack_user_id,
                slack_channel_id=message.slack_channel_id,
                body=message.body,
                blocks=message.blocks,
                generated_body=message.generated_body or message.body,
                content_hash=message.content_hash,
                run_id=message.run_id,
                created_at=message.created_at,
                updated_at=message.created_at,
                pm_evidence_claims=message.pm_evidence_claims,
            )
            self.slack_outbox[outbox_id] = record
            self.slack_outbox_ids_by_project[project_id].append(outbox_id)
            self.slack_outbox_dedupe[dedupe_key] = outbox_id
            created_ids.append(outbox_id)
        return {
            "created_outbox_ids": created_ids,
            "matched_outbox_ids": matched_ids,
            "skipped_outbox_ids": skipped_ids,
        }

    def mark_slack_outbox_sent(
        self,
        project_id: str,
        outbox_id: str,
        sent_at: dt.datetime,
        slack_channel_id: str,
        slack_message_ts: str,
        run_id: str | None = None,
    ) -> SlackOutboxRecord:
        record = self._get_slack_outbox(project_id, outbox_id)
        updated = record.model_copy(
            update={
                "status": SlackOutboxStatus.SENT,
                "sent_at": sent_at,
                "failed_at": None,
                "slack_channel_id": slack_channel_id,
                "slack_message_ts": slack_message_ts,
                "error_text": None,
                "run_id": run_id or record.run_id,
                "updated_at": sent_at,
            },
        )
        self.slack_outbox[outbox_id] = updated
        return updated

    def mark_slack_outbox_failed(
        self,
        project_id: str,
        outbox_id: str,
        failed_at: dt.datetime,
        error_text: str,
        run_id: str | None = None,
    ) -> SlackOutboxRecord:
        record = self._get_slack_outbox(project_id, outbox_id)
        updated = record.model_copy(
            update={
                "status": SlackOutboxStatus.FAILED,
                "failed_at": failed_at,
                "error_text": error_text,
                "run_id": run_id or record.run_id,
                "updated_at": failed_at,
            },
        )
        self.slack_outbox[outbox_id] = updated
        return updated

    def update_slack_outbox_body(
        self,
        project_id: str,
        outbox_id: str,
        body: str,
        updated_at: dt.datetime,
        run_id: str | None = None,
    ) -> SlackOutboxRecord:
        record = self._get_slack_outbox(project_id, outbox_id)
        if record.status != SlackOutboxStatus.DRAFT:
            raise ServiceValidationError(
                code="slack_outbox_not_editable",
                message="Only draft Slack outbox rows can be edited.",
                entity_id=outbox_id,
            )
        updated = record.model_copy(
            update={
                "body": body,
                "blocks": [],
                "content_hash": _outbox_body_hash(body),
                # Editing in the UI changes the plain body only; generated Block Kit
                # and evidence claims must not survive as stale proof of the edit.
                "pm_evidence_claims": [],
                "edited_at": updated_at,
                "updated_at": updated_at,
                "run_id": run_id or record.run_id,
            },
        )
        self.slack_outbox[outbox_id] = updated
        self.slack_outbox_dedupe.pop(slack_outbox_target_key(record, project_id), None)
        self.slack_outbox_dedupe[slack_outbox_target_key(updated, project_id)] = (
            outbox_id
        )
        return updated

    def mark_slack_outbox_skipped(
        self,
        project_id: str,
        outbox_id: str,
        skipped_at: dt.datetime,
        reason: str | None = None,
        run_id: str | None = None,
    ) -> SlackOutboxRecord:
        record = self._get_slack_outbox(project_id, outbox_id)
        updated = record.model_copy(
            update={
                "status": SlackOutboxStatus.SKIPPED,
                "skipped_at": skipped_at,
                "skip_reason": reason,
                "failed_at": None,
                "error_text": None,
                "run_id": run_id or record.run_id,
                "updated_at": skipped_at,
            },
        )
        self.slack_outbox[outbox_id] = updated
        return updated

    def list_slack_outbox(
        self,
        project_id: str,
        statuses: list[SlackOutboxStatus],
        limit: int | None = None,
    ) -> list[SlackOutboxRecord]:
        self.get_project(project_id)
        status_values = {getattr(status, "value", status) for status in statuses}
        rows = [
            self.slack_outbox[outbox_id]
            for outbox_id in self.slack_outbox_ids_by_project.get(project_id, [])
            if self.slack_outbox[outbox_id].status.value in status_values
        ]
        rows.sort(key=lambda row: (row.created_at, row.outbox_id))
        if limit is not None:
            return rows[:limit]
        return rows

    def record_pm_communication_evidence(
        self,
        evidence: PMCommunicationEvidenceRecord,
    ) -> PMCommunicationEvidenceRecord:
        self.get_project(evidence.project_id)
        self._get_slack_outbox(evidence.project_id, evidence.outbox_id)
        if (
            evidence.evidence_id not in self.pm_communication_evidence
            and evidence.evidence_id
            not in self.pm_communication_evidence_ids_by_project[evidence.project_id]
        ):
            self.pm_communication_evidence_ids_by_project[evidence.project_id].append(
                evidence.evidence_id,
            )
        self.pm_communication_evidence[evidence.evidence_id] = evidence
        return evidence

    def list_pm_communication_evidence(
        self,
        project_id: str,
    ) -> list[PMCommunicationEvidenceRecord]:
        self.get_project(project_id)
        return sorted(
            [
                self.pm_communication_evidence[evidence_id]
                for evidence_id in self.pm_communication_evidence_ids_by_project.get(
                    project_id,
                    [],
                )
                if evidence_id in self.pm_communication_evidence
            ],
            key=lambda row: (row.communicated_at, row.evidence_id),
        )

    def upsert_process_evidence_line_item(
        self,
        record: ProcessEvidenceLineItemRecord,
    ) -> ProcessEvidenceLineItemRecord:
        self.get_project(record.project_id)
        process = self._get_process(record.project_id, record.process_id)
        if process.symbol != record.process_symbol:
            record = record.model_copy(update={"process_symbol": process.symbol})
        existing = self.process_evidence_line_items.get(record.evidence_line_id)
        if existing is not None:
            if existing.project_id != record.project_id:
                raise ServiceValidationError(
                    code="cross_project_process_evidence",
                    message="Process evidence line item belongs to another project.",
                    entity_id=record.evidence_line_id,
                )
            record = record.model_copy(update={"created_at": existing.created_at})
        elif (
            record.evidence_line_id
            not in self.process_evidence_line_item_ids_by_project[record.project_id]
        ):
            self.process_evidence_line_item_ids_by_project[record.project_id].append(
                record.evidence_line_id,
            )
        self.process_evidence_line_items[record.evidence_line_id] = record
        return record

    def list_process_evidence_line_items(
        self,
        project_id: str,
        process_id: str | None = None,
        line_items: list[str] | None = None,
    ) -> list[ProcessEvidenceLineItemRecord]:
        self.get_project(project_id)
        line_filter = set(line_items or [])
        rows = [
            self.process_evidence_line_items[evidence_line_id]
            for evidence_line_id in self.process_evidence_line_item_ids_by_project.get(
                project_id,
                [],
            )
            if evidence_line_id in self.process_evidence_line_items
        ]
        if process_id is not None:
            self._get_process(project_id, process_id)
            rows = [row for row in rows if row.process_id == process_id]
        if line_filter:
            rows = [row for row in rows if row.line_item in line_filter]
        return sorted(
            rows,
            key=lambda row: (row.process_symbol, row.line_item, row.evidence_line_id),
        )

    def delete_process_evidence_line_items(
        self,
        *,
        line_items: set[str] | frozenset[str],
    ) -> dict[str, Any]:
        deleted_ids = []
        for evidence_line_id, row in list(self.process_evidence_line_items.items()):
            if row.line_item not in line_items:
                continue
            deleted_ids.append(evidence_line_id)
            self.process_evidence_line_items.pop(evidence_line_id, None)
            ids = self.process_evidence_line_item_ids_by_project.get(row.project_id, [])
            self.process_evidence_line_item_ids_by_project[row.project_id] = [
                item for item in ids if item != evidence_line_id
            ]
        return {"deleted_evidence_line_ids": sorted(deleted_ids)}

    def upsert_resource_evidence_line_item(
        self,
        record: ResourceEvidenceLineItemRecord,
    ) -> ResourceEvidenceLineItemRecord:
        self.get_project(record.project_id)
        self._get_resource(record.project_id, record.resource_id)
        existing = self.resource_evidence_line_items.get(record.evidence_line_id)
        if existing is not None:
            if existing.project_id != record.project_id:
                raise ServiceValidationError(
                    code="cross_project_resource_evidence",
                    message="Resource evidence line item belongs to another project.",
                    entity_id=record.evidence_line_id,
                )
            record = record.model_copy(update={"created_at": existing.created_at})
        elif (
            record.evidence_line_id
            not in self.resource_evidence_line_item_ids_by_project[record.project_id]
        ):
            self.resource_evidence_line_item_ids_by_project[record.project_id].append(
                record.evidence_line_id,
            )
        self.resource_evidence_line_items[record.evidence_line_id] = record
        return record

    def list_resource_evidence_line_items(
        self,
        project_id: str,
        resource_id: str | None = None,
        line_items: list[str] | None = None,
    ) -> list[ResourceEvidenceLineItemRecord]:
        self.get_project(project_id)
        line_filter = set(line_items or [])
        rows = [
            self.resource_evidence_line_items[evidence_line_id]
            for evidence_line_id in self.resource_evidence_line_item_ids_by_project.get(
                project_id,
                [],
            )
            if evidence_line_id in self.resource_evidence_line_items
        ]
        if resource_id is not None:
            self._get_resource(project_id, resource_id)
            rows = [row for row in rows if row.resource_id == resource_id]
        if line_filter:
            rows = [row for row in rows if row.line_item in line_filter]
        return sorted(rows, key=lambda row: (row.resource_id, row.line_item))

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
        resolution_owner_resource_id: str | None = None,
    ) -> BlockerRecord:
        self._get_process(project_id, process_id)
        if resolution_owner_resource_id is not None:
            self._get_resource(project_id, resolution_owner_resource_id)
        resolved_blocker_id = blocker_id or self._unique_blocker_id(
            project_id,
            description,
        )
        existing = self.blockers.get(resolved_blocker_id)
        if existing is not None:
            if existing.project_id != project_id:
                raise ServiceValidationError(
                    code="cross_project_blocker",
                    message="Blocker id belongs to another project.",
                    entity_id=resolved_blocker_id,
                )
            if existing.process_id != process_id:
                raise ServiceValidationError(
                    code="blocker_process_reference_conflict",
                    message="A blocker can reference exactly one process.",
                    field_path="process_id",
                    entity_id=resolved_blocker_id,
                    details={
                        "existing_process_id": existing.process_id,
                        "requested_process_id": process_id,
                    },
                )
            return existing
        blocker = BlockerRecord(
            blocker_id=resolved_blocker_id,
            project_id=project_id,
            process_id=process_id,
            description=description,
            opened_at=opened_at,
            summary=description,
            details=details,
            severity=severity or "blocking",
            created_at=opened_at,
            resolution_owner_resource_id=resolution_owner_resource_id,
        )
        self.blockers[blocker.blocker_id] = blocker
        if blocker.blocker_id not in self.blocker_ids_by_project[project_id]:
            self.blocker_ids_by_project[project_id].append(blocker.blocker_id)
        return blocker

    def resolve_blocker(
        self,
        project_id: str,
        blocker_id: str,
        resolved_at: dt.datetime,
        resolution: str | None = None,
        resolution_owner_resource_id: str | None = None,
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
        if resolution_owner_resource_id is not None:
            self._get_resource(project_id, resolution_owner_resource_id)
        if blocker.resolved_at is not None:
            desired_owner = (
                resolution_owner_resource_id
                if resolution_owner_resource_id is not None
                else blocker.resolution_owner_resource_id
            )
            if (
                blocker.resolved_at == resolved_at
                and blocker.resolution == resolution
                and blocker.resolution_owner_resource_id == desired_owner
            ):
                return blocker
            raise ServiceValidationError(
                code="idempotency_conflict",
                message="Blocker was already resolved with different values.",
                entity_id=blocker_id,
            )
        update = {"resolved_at": resolved_at, "resolution": resolution}
        if resolution_owner_resource_id is not None:
            update["resolution_owner_resource_id"] = resolution_owner_resource_id
        updated = blocker.model_copy(
            update=update,
        )
        self.blockers[blocker_id] = updated
        return updated

    def set_blocker_resolution_owner(
        self,
        project_id: str,
        blocker_id: str,
        resolution_owner_resource_id: str | None,
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
        if resolution_owner_resource_id is not None:
            self._get_resource(project_id, resolution_owner_resource_id)
        if blocker.resolution_owner_resource_id == resolution_owner_resource_id:
            return blocker
        updated = blocker.model_copy(
            update={"resolution_owner_resource_id": resolution_owner_resource_id},
        )
        self.blockers[blocker_id] = updated
        return updated

    def reopen_blocker(
        self,
        project_id: str,
        blocker_id: str,
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
        if blocker.resolved_at is None and blocker.resolution is None:
            return blocker
        updated = blocker.model_copy(
            update={"resolved_at": None, "resolution": None},
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
            ]
            pinned_started_at = self._process_pin_started_at(
                project_id,
                process_id,
                as_of,
            )
            pinned_finished_at = self._process_pin_finished_at_as_of(
                project_id,
                process_id,
                as_of,
            )
            processes.append(
                ProcessScheduleInput(
                    process_id=process_id,
                    name=revision.name,
                    description=revision.description,
                    dependencies=tuple(revision.dependencies),
                    duration_business_days=revision.duration_business_days,
                    derived_status=(
                        "finished"
                        if pinned_finished_at is not None
                        else "started"
                        if pinned_started_at is not None
                        else "planned"
                    ),
                    process_type=process.process_type,
                    pin_started_at=pinned_started_at,
                    pin_finished_at=pinned_finished_at,
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
        self._set_project_resource_currency(project_id, default_currency)

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

    def record_schedule_snapshot(
        self,
        snapshot: ScheduleSnapshotRecord,
    ) -> ScheduleSnapshotRecord:
        self.get_project(snapshot.project_id)
        for existing in self.schedule_snapshots:
            if existing.snapshot_id == snapshot.snapshot_id:
                return existing
        self.schedule_snapshots.append(snapshot)
        self.schedule_snapshots.sort(
            key=lambda item: (
                item.project_id,
                item.committed_at,
                tuple(item.terminal_process_symbols),
                item.snapshot_id,
            )
        )
        return snapshot

    def schedule_snapshots_as_of(
        self,
        project_id: str,
        as_of: dt.datetime,
        terminal_process_symbols: list[str] | None = None,
    ) -> list[ScheduleSnapshotRecord]:
        self.get_project(project_id)
        terminal_filter = tuple(sorted(terminal_process_symbols or []))
        rows = []
        for snapshot in self.schedule_snapshots:
            if snapshot.project_id != project_id:
                continue
            if snapshot.committed_at > as_of:
                continue
            if tuple(snapshot.terminal_process_symbols) != terminal_filter:
                continue
            rows.append(snapshot)
        return rows

    def process_ids_for_scope(
        self,
        project_id: str,
        as_of: dt.datetime,
        scope: Any,
    ) -> tuple[set[str], dict[str, Any], str | None]:
        scope_type = _scope_get(scope, "type", "project")
        if scope is None or scope_type == "project":
            return (
                set(self.active_process_ids_as_of(project_id, as_of)),
                {"type": "project"},
                None,
            )
        if scope_type == "target_process":
            process_id = _scope_get(scope, "process_id")
            if process_id is None:
                process_id = self.resolve_process_id(
                    project_id,
                    _scope_get(scope, "process_symbol"),
                )
            else:
                self._get_process(project_id, process_id)
            return (
                {process_id},
                {"type": "target_process", "process_id": process_id},
                process_id,
            )
        root_symbols = _scope_get(scope, "root_process_symbols")
        roots = [self.resolve_process_id(project_id, symbol) for symbol in root_symbols]
        graph = self._active_dependency_graph(project_id, as_of)
        selected: set[str] = set()
        raw_direction = _scope_get(scope, "direction")
        direction = getattr(raw_direction, "value", raw_direction)
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
        process_ids: list[str],
        edit_at: dt.datetime,
        processes: list[Any],
        dependencies: list[Any],
        root_symbols: list[str] | None,
        leaf_symbols: list[str] | None,
        command_id: str,
        preserve_parent_symbol_as_alias: bool = True,
        parent_alias_target_symbol: str | None = None,
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
        root_symbols = list(root_symbols or [])
        leaf_symbols = list(leaf_symbols or [])
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
        inferred_root_symbols = [
            symbol for symbol in child_symbols if child_graph.in_degree(symbol) == 0
        ]
        inferred_leaf_symbols = [
            symbol for symbol in child_symbols if child_graph.out_degree(symbol) == 0
        ]
        if root_symbols:
            if set(root_symbols) != set(inferred_root_symbols):
                self._raise_validation(
                    ["command", "root_symbols"],
                    "Root symbols must match the child graph's topological roots.",
                    "root_symbols_mismatch",
                    {
                        "inferred_root_symbols": inferred_root_symbols,
                        "supplied_root_symbols": list(root_symbols),
                    },
                )
        else:
            root_symbols = inferred_root_symbols
        if leaf_symbols:
            if set(leaf_symbols) != set(inferred_leaf_symbols):
                self._raise_validation(
                    ["command", "leaf_symbols"],
                    "Leaf symbols must match the child graph's topological leaves.",
                    "leaf_symbols_mismatch",
                    {
                        "inferred_leaf_symbols": inferred_leaf_symbols,
                        "supplied_leaf_symbols": list(leaf_symbols),
                    },
                )
        else:
            leaf_symbols = inferred_leaf_symbols
        child_ids: dict[str, str] = {}
        incoming, outgoing = self._external_edges(
            project_id,
            edit_at,
            selected_process_ids,
        )
        incoming = {
            process_id
            for process_id in incoming
            if getattr(
                self.processes.get(process_id),
                "process_type",
                "standard",
            )
            != "blocker"
        }
        for child in processes:
            duration_days = math.ceil(float(child.duration_hours) / 8)
            process, _revision = self.upsert_process_revision(
                project_id=project_id,
                process_id=f"process-{child.process_symbol}",
                process_type="standard",
                name=child.name,
                description=child.description,
                effective_at=edit_at,
                duration_business_days=duration_days,
                dependencies=[],
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
                dep for dep in revision.dependencies if dep not in selected_process_ids
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
                "replace_process_with_subgraph",
                list(child_ids.values()),
            )
            for process_id in ordered_process_ids
        ]
        alias_process_id = None
        if preserve_parent_symbol_as_alias:
            target_symbol = parent_alias_target_symbol or processes[0].process_symbol
            alias_process_id = child_ids[target_symbol]
            for process_id in ordered_process_ids:
                retired_process = self.processes[process_id]
                self.process_aliases[project_id][retired_process.symbol] = (
                    alias_process_id
                )
                self.process_alias_sources[project_id][retired_process.symbol] = (
                    "retirement"
                )
                for alias, target_id in list(self.process_aliases[project_id].items()):
                    if target_id == process_id and (
                        self.process_alias_sources[project_id].get(alias) == "rename"
                    ):
                        self.process_aliases[project_id][alias] = alias_process_id
                        self.process_alias_sources[project_id][alias] = "retirement"
        blocker_cleanup = self.delete_orphaned_blocker_processes(
            project_id=project_id,
            edit_at=edit_at,
        )
        self._validate_active_dependency_graph_acyclic(
            project_id,
            edit_at,
            "process_symbols",
        )
        return {
            "process_ids": list(child_ids.values()),
            "retired_process_ids": ordered_process_ids,
            "retirement_event_ids": retirement_event_ids,
            "edge_ids": list(dict.fromkeys(edge_ids)),
            "retired_edge_ids": list(dict.fromkeys(retired_edge_ids)),
            "deleted_blocker_process_ids": blocker_cleanup["deleted_process_ids"],
            "deleted_blocker_ids": blocker_cleanup["deleted_blocker_ids"],
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
        process_symbol = new_process.process_symbol or self._unique_symbol(
            project_id,
            self._symbol_from_name(new_process.name),
        )
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
            process_symbol,
            owning_process_id=None,
        )
        incoming, outgoing = self._external_edges(
            project_id,
            edit_at,
            selected_process_ids,
        )
        incoming = {
            process_id
            for process_id in incoming
            if getattr(
                self.processes.get(process_id),
                "process_type",
                "standard",
            )
            != "blocker"
        }
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
            role_requirement_process_ids = [
                revision.process_id
                for revision in selected_revisions
                if self._non_default_role_requirements(revision)
            ]
            legacy_required_role_process_ids = [
                revision.process_id
                for revision in selected_revisions
                if revision.required_roles
            ]
            if role_requirement_process_ids and legacy_required_role_process_ids:
                self._raise_validation(
                    ["command", "new_process", "role_requirements"],
                    (
                        "Cannot infer collapsed role usage from mixed "
                        "role_requirements and legacy required_roles."
                    ),
                    "collapse_mixed_role_requirement_modes",
                    {
                        "role_requirement_process_ids": role_requirement_process_ids,
                        "legacy_required_role_process_ids": (
                            legacy_required_role_process_ids
                        ),
                    },
                )
            role_requirements = self._merged_role_requirements(selected_revisions)
            requirement_ids = [
                requirement.requirement_id
                for requirement in role_requirements
                if requirement.requirement_id is not None
            ]
        required_roles = dict(getattr(new_process, "required_roles", {}) or {})
        if not required_roles and not role_requirements:
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
            process_id=f"process-{process_symbol}",
            process_type="standard",
            name=new_process.name,
            description=new_process.description,
            effective_at=edit_at,
            duration_business_days=duration_days,
            dependencies=sorted(incoming),
            earliest_start_at=new_process.earliest_start_at,
            start_at_earliest=False,
            delay_after_dependencies_business_days=0,
            required_roles=required_roles,
            role_requirements=role_requirements,
            assumption_note=None,
        )
        replacement = replacement.model_copy(
            update={
                "symbol": process_symbol,
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
        blocker_cleanup = self.delete_orphaned_blocker_processes(
            project_id=project_id,
            edit_at=edit_at,
        )
        self._validate_active_dependency_graph_acyclic(
            project_id,
            edit_at,
            "process_symbols",
        )
        return {
            "process_id": replacement.process_id,
            "retired_process_ids": ordered_process_ids,
            "retirement_event_ids": retirement_event_ids,
            "edge_ids": list(dict.fromkeys(edge_ids)),
            "retired_edge_ids": list(dict.fromkeys(retired_edge_ids)),
            "deleted_blocker_process_ids": blocker_cleanup["deleted_process_ids"],
            "deleted_blocker_ids": blocker_cleanup["deleted_blocker_ids"],
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
            for calendar, segment_starts_at, segment_ends_at in (
                self._resource_calendar_segments(
                    resource,
                    horizon_starts_at,
                    horizon_ends_at,
                )
            ):
                if calendar is None or not calendar["active"]:
                    continue
                buckets.extend(
                    self._expand_resource_calendar(
                        resource,
                        calendar,
                        segment_starts_at,
                        segment_ends_at,
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
        other._validate_process_role_definition_invariants()
        other._validate_no_orphaned_blocker_processes()
        self.projects = other.projects
        self.processes = other.processes
        self.process_ids_by_project = other.process_ids_by_project
        self.revisions_by_process = other.revisions_by_process
        self.blockers = other.blockers
        self.blocker_ids_by_project = other.blocker_ids_by_project
        self.process_role_pins = other.process_role_pins
        self.process_role_pin_ids_by_project = other.process_role_pin_ids_by_project
        self.roles = other.roles
        self.role_ids_by_project = other.role_ids_by_project
        self.role_requirements = other.role_requirements
        self.resources = other.resources
        self.resource_ids_by_project = other.resource_ids_by_project
        self.calendars = other.calendars
        self.calendar_ids_by_project = other.calendar_ids_by_project
        self.retired_processes = other.retired_processes
        self.process_aliases = other.process_aliases
        self.process_alias_sources = other.process_alias_sources
        self.dependency_edge_ids = other.dependency_edge_ids
        self.schedule_snapshots = other.schedule_snapshots
        self.milestones = other.milestones
        self.milestone_ids_by_project = other.milestone_ids_by_project
        self.slack_project_configs = other.slack_project_configs
        self.slack_resource_mappings = other.slack_resource_mappings
        self.slack_collection_cursors = other.slack_collection_cursors
        self.slack_encrypted_tokens = other.slack_encrypted_tokens
        self.slack_runs = other.slack_runs
        self.slack_run_ids_by_project = other.slack_run_ids_by_project
        self.slack_outbox = other.slack_outbox
        self.slack_outbox_ids_by_project = other.slack_outbox_ids_by_project
        self.slack_outbox_dedupe = other.slack_outbox_dedupe
        self.pm_communication_evidence = other.pm_communication_evidence
        self.pm_communication_evidence_ids_by_project = (
            other.pm_communication_evidence_ids_by_project
        )
        self.process_evidence_line_items = other.process_evidence_line_items
        self.process_evidence_line_item_ids_by_project = (
            other.process_evidence_line_item_ids_by_project
        )
        self.resource_evidence_line_items = other.resource_evidence_line_items
        self.resource_evidence_line_item_ids_by_project = (
            other.resource_evidence_line_item_ids_by_project
        )

    def ensure_default_process_roles_for_missing_requirements(
        self,
        *,
        project_id: str | None = None,
        edit_at: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Repair legacy process revisions that have no role requirement."""
        _ = edit_at or dt.datetime.now(dt.UTC)
        project_ids = [project_id] if project_id is not None else sorted(self.projects)
        updated_process_ids: list[str] = []
        selected_role_ids: dict[str, str] = {}
        for current_project_id in project_ids:
            self.get_project(current_project_id)
            for process_id in list(self.process_ids_by_project.get(current_project_id, [])):
                revisions = list(self.revisions_by_process.get(process_id, []))
                if not revisions:
                    continue
                role_id: str | None = None
                changed = False
                repaired_revisions = []
                for revision in revisions:
                    if revision.role_requirements:
                        repaired_revisions.append(revision)
                        continue
                    if role_id is None:
                        role_id = self._ensure_default_missing_process_role(
                            current_project_id
                        )
                    role_requirements = [
                        RoleRequirementCommand(
                            requirement_id=f"{process_id}-{role_id}",
                            role_id=role_id,
                            effort_hours=1,
                        )
                    ]
                    repaired_revisions.append(
                        revision.model_copy(update={"role_requirements": role_requirements})
                    )
                    changed = True
                if changed:
                    self.revisions_by_process[process_id] = repaired_revisions
                    updated_process_ids.append(process_id)
                    if role_id is not None:
                        selected_role_ids[process_id] = role_id
        if updated_process_ids:
            self._rebuild_role_requirement_index()
        return {
            "updated_process_ids": updated_process_ids,
            "updated_process_count": len(updated_process_ids),
            "default_role_id": DEFAULT_MISSING_PROCESS_ROLE_ID,
            "selected_role_ids": selected_role_ids,
        }

    def normalize_process_role_requirements_to_single(
        self,
        *,
        project_id: str | None = None,
        edit_at: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Repair legacy process revisions that contain multiple role requirements."""
        _ = edit_at or dt.datetime.now(dt.UTC)
        project_ids = [project_id] if project_id is not None else sorted(self.projects)
        updated_process_ids: list[str] = []
        selected_role_ids: dict[str, str] = {}
        for current_project_id in project_ids:
            self.get_project(current_project_id)
            for process_id in list(self.process_ids_by_project.get(current_project_id, [])):
                revisions = list(self.revisions_by_process.get(process_id, []))
                if not revisions:
                    continue
                changed = False
                repaired_revisions = []
                for revision in revisions:
                    if len(revision.role_requirements) <= 1:
                        repaired_revisions.append(revision)
                        continue
                    requirement = self._single_role_requirement_from_legacy_requirements(
                        current_project_id,
                        process_id,
                        revision.role_requirements,
                    )
                    repaired_revisions.append(
                        revision.model_copy(update={"role_requirements": [requirement]})
                    )
                    self._rewrite_process_pins_for_single_requirement(
                        current_project_id,
                        process_id,
                        requirement,
                    )
                    selected_role_ids[process_id] = requirement.role_id
                    changed = True
                if not changed:
                    continue
                self.revisions_by_process[process_id] = repaired_revisions
                updated_process_ids.append(process_id)
        if updated_process_ids:
            self._rebuild_role_requirement_index()
        return {
            "updated_process_ids": updated_process_ids,
            "updated_process_count": len(updated_process_ids),
            "selected_role_ids": selected_role_ids,
        }

    def delete_orphaned_blocker_processes(
        self,
        *,
        project_id: str | None = None,
        edit_at: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Delete active blocker resolver processes that have no active children."""
        effective_at = edit_at or dt.datetime.now(dt.UTC)
        project_ids = [project_id] if project_id is not None else sorted(self.projects)
        deleted_process_ids: list[str] = []
        deleted_blocker_ids: list[str] = []
        changed = True
        while changed:
            changed = False
            for current_project_id in project_ids:
                self.get_project(current_project_id)
                for process_id in self._orphaned_blocker_process_ids(
                    current_project_id,
                    effective_at,
                ):
                    if process_id not in self.processes:
                        continue
                    result = self.delete_process(
                        current_project_id,
                        process_id,
                        effective_at,
                    )
                    deleted_process_ids.extend(result.get("deleted_process_ids", []))
                    deleted_blocker_ids.extend(result.get("deleted_blocker_ids", []))
                    changed = True
        return {
            "deleted_process_ids": list(dict.fromkeys(deleted_process_ids)),
            "deleted_blocker_ids": list(dict.fromkeys(deleted_blocker_ids)),
        }

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
            process_type=process.process_type,
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

    def _blocker_resolver_symbol(self, blocker_id: str) -> str:
        stem = blocker_id.removeprefix("blocker-")
        stem = self._slugify_identifier_stem(stem) or self._slugify_identifier_stem(
            blocker_id,
        )
        return f"resolve-{stem}"

    def _slugify_identifier_stem(self, value: str) -> str:
        slug = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
        while "--" in slug:
            slug = slug.replace("--", "-")
        return slug.strip("-") or "blocker"

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
        requirements = [
            requirement
            for revision in revisions
            for requirement in self._non_default_role_requirements(revision)
        ]
        if not requirements:
            return []
        project_id = revisions[0].project_id
        self._merged_role_requirements_by_role(revisions)
        return [
            self._single_role_requirement_from_legacy_requirements(
                project_id,
                "collapse",
                requirements,
                requirement_id_prefix="req-collapse",
            )
        ]

    def _merged_role_requirements_by_role(
        self,
        revisions: list[ProcessRevisionRecord],
    ) -> list[RoleRequirementCommand]:
        grouped: dict[str, list[RoleRequirementCommand]] = defaultdict(list)
        process_ids_by_role: dict[str, list[str]] = defaultdict(list)
        requirement_ids_by_role: dict[str, list[str]] = defaultdict(list)
        for revision in revisions:
            for requirement in self._non_default_role_requirements(revision):
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
                            "role_id": role_id,
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

    def _non_default_role_requirements(
        self,
        revision: ProcessRevisionRecord,
    ) -> list[RoleRequirementCommand]:
        return [
            requirement
            for requirement in revision.role_requirements
            if not self._is_default_missing_process_requirement(
                revision.process_id,
                requirement,
            )
        ]

    def _is_default_missing_process_requirement(
        self,
        process_id: str,
        requirement: RoleRequirementCommand,
    ) -> bool:
        role_id = requirement.role_id
        role = self.roles.get(role_id)
        return (
            role is not None
            and role.get("name") == DEFAULT_MISSING_PROCESS_ROLE_NAME
            and requirement.requirement_id == f"{process_id}-{role_id}"
        )

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

    def _resource_calendar_segments(
        self,
        resource: dict[str, Any],
        starts_at: dt.datetime,
        ends_at: dt.datetime,
    ) -> list[tuple[dict[str, Any] | None, dt.datetime, dt.datetime]]:
        default_calendar = self.calendars.get(resource["calendar_id"])
        segments: list[tuple[dict[str, Any] | None, dt.datetime, dt.datetime]] = []
        cursor = starts_at
        for override in sorted(
            resource.get("calendar_overrides", []),
            key=lambda rule: rule["starts_at"],
        ):
            override_starts_at = max(override["starts_at"], starts_at)
            override_ends_at = min(override.get("ends_at") or ends_at, ends_at)
            if override_ends_at <= starts_at or override_starts_at >= ends_at:
                continue
            if override_starts_at > cursor:
                segments.append((default_calendar, cursor, override_starts_at))
            segments.append(
                (
                    self.calendars.get(override["calendar_id"]),
                    max(cursor, override_starts_at),
                    override_ends_at,
                )
            )
            cursor = max(cursor, override_ends_at)
        if cursor < ends_at:
            segments.append((default_calendar, cursor, ends_at))
        return [
            (calendar, segment_starts_at, segment_ends_at)
            for calendar, segment_starts_at, segment_ends_at in segments
            if segment_ends_at > segment_starts_at
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

    def _get_milestone(self, project_id: str, milestone_id: str) -> MilestoneRecord:
        if milestone_id not in self.milestones:
            raise ServiceValidationError(
                code="milestone_not_found",
                message=f"Milestone {milestone_id!r} does not exist.",
                entity_id=milestone_id,
            )
        milestone = self.milestones[milestone_id]
        if milestone.project_id != project_id:
            raise ServiceValidationError(
                code="cross_project_milestone",
                message="Milestone does not belong to the requested project.",
                entity_id=milestone_id,
            )
        return milestone

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

    def _get_slack_outbox(
        self,
        project_id: str,
        outbox_id: str,
    ) -> SlackOutboxRecord:
        if outbox_id not in self.slack_outbox:
            raise ServiceValidationError(
                code="slack_outbox_not_found",
                message=f"Slack outbox row {outbox_id!r} does not exist.",
                entity_id=outbox_id,
            )
        record = self.slack_outbox[outbox_id]
        if record.project_id != project_id:
            raise ServiceValidationError(
                code="cross_project_slack_outbox",
                message="Slack outbox row does not belong to the requested project.",
                entity_id=outbox_id,
            )
        return record

    def _get_slack_run(
        self,
        project_id: str,
        run_id: str,
    ) -> SlackRunRecord:
        if run_id not in self.slack_runs:
            raise ServiceValidationError(
                code="slack_run_not_found",
                message=f"Slack run {run_id!r} does not exist.",
                entity_id=run_id,
            )
        record = self.slack_runs[run_id]
        if record.project_id != project_id:
            raise ServiceValidationError(
                code="cross_project_slack_run",
                message="Slack run does not belong to the requested project.",
                entity_id=run_id,
            )
        return record

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
        if len(role_requirements) != 1:
            raise ServiceValidationError(
                code="process_role_requirement_count_invalid",
                message="Active processes must define exactly one role requirement.",
                field_path="role_requirements",
            )
        for requirement in role_requirements:
            role = self._get_role(project_id, requirement.role_id)
            if not role["active"]:
                raise ServiceValidationError(
                    code="inactive_role",
                    message="Inactive roles cannot be used by new requirements.",
                    entity_id=requirement.role_id,
                )

    def _role_requirements_or_default(
        self,
        project_id: str,
        process_id: str,
        role_requirements: list[RoleRequirementCommand],
    ) -> list[RoleRequirementCommand]:
        if role_requirements:
            return role_requirements
        role_id = self._ensure_default_missing_process_role(project_id)
        return [
            RoleRequirementCommand(
                requirement_id=f"{process_id}-{role_id}",
                role_id=role_id,
                effort_hours=1,
            )
        ]

    def _ensure_default_missing_process_role(self, project_id: str) -> str:
        project = self.get_project(project_id)
        role_id = self._project_scoped_role_id(
            project_id,
            DEFAULT_MISSING_PROCESS_ROLE_ID,
        )
        role = self.roles.get(role_id)
        if role is not None:
            role["active"] = True
        else:
            self.roles[role_id] = {
                "role_id": role_id,
                "project_id": project_id,
                "name": DEFAULT_MISSING_PROCESS_ROLE_NAME,
                "active": True,
            }
            if role_id not in self.role_ids_by_project[project_id]:
                self.role_ids_by_project[project_id].append(role_id)
        self._ensure_default_missing_process_resource(
            project_id,
            project.start_at,
            role_id,
        )
        return role_id

    def _ensure_default_missing_process_resource(
        self,
        project_id: str,
        available_from_at: dt.datetime,
        role_id: str,
    ) -> None:
        if any(
            resource.get("active", True)
            and role_id in set(resource.get("role_ids") or [])
            for resource in self.resources.values()
            if resource.get("project_id") == project_id
        ):
            return
        for resource_id in self.resource_ids_by_project.get(project_id, []):
            resource = self.resources.get(resource_id)
            if (
                resource is not None
                and resource.get("active", True)
                and resource.get("name") == DEFAULT_MISSING_PROCESS_RESOURCE_NAME
            ):
                self.set_resource_roles(
                    project_id,
                    resource_id,
                    list(
                        dict.fromkeys(
                            [
                                *list(resource.get("role_ids") or []),
                                role_id,
                            ]
                        )
                    ),
                )
                return
        calendar_id = f"calendar-{symbolify(project_id)}-josh-default"
        if calendar_id not in self.calendars:
            self.upsert_resource_calendar(
                project_id=project_id,
                calendar_id=calendar_id,
                name="Josh default",
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
        resource_id = DEFAULT_MISSING_PROCESS_RESOURCE_ID
        existing = self.resources.get(resource_id)
        if existing is not None and existing.get("project_id") != project_id:
            resource_id = f"res_{symbolify(project_id)}_josh"
        self.upsert_resource(
            project_id=project_id,
            resource_id=resource_id,
            name=DEFAULT_MISSING_PROCESS_RESOURCE_NAME,
            resource_type="external",
            role_ids=[role_id],
            calendar_id=calendar_id,
            available_from_at=available_from_at,
            cost_rate=0,
            cost_unit=CostUnit.HOUR,
            active=True,
        )

    def _single_role_requirement_from_legacy_requirements(
        self,
        project_id: str,
        process_id: str,
        role_requirements: list[RoleRequirementCommand],
        *,
        requirement_id_prefix: str | None = None,
    ) -> RoleRequirementCommand:
        total_effort = sum(int(requirement.effort_hours) for requirement in role_requirements)
        resource_id = self._best_resource_for_role_requirements(
            project_id,
            role_requirements,
        )
        exact_role_id = self._ensure_resource_exact_role(project_id, resource_id)
        requirement_id = f"{requirement_id_prefix or process_id}-{exact_role_id}"
        return RoleRequirementCommand(
            requirement_id=requirement_id,
            role_id=exact_role_id,
            effort_hours=max(total_effort, 1),
        )

    def _best_resource_for_role_requirements(
        self,
        project_id: str,
        role_requirements: list[RoleRequirementCommand],
    ) -> str:
        scored: list[tuple[int, str]] = []
        for resource_id in self.resource_ids_by_project.get(project_id, []):
            resource = self.resources.get(resource_id)
            if resource is None or not resource.get("active", True):
                continue
            resource_roles = {str(role_id) for role_id in resource.get("role_ids", [])}
            score = sum(
                int(requirement.effort_hours)
                for requirement in role_requirements
                if requirement.role_id in resource_roles
            )
            if score > 0:
                scored.append((score, resource_id))
        if scored:
            return sorted(scored, key=lambda item: (-item[0], item[1]))[0][1]
        self._ensure_default_missing_process_role(project_id)
        return self._default_missing_process_resource_id(project_id)

    def _default_missing_process_resource_id(self, project_id: str) -> str:
        role_id = self._project_scoped_role_id(
            project_id,
            DEFAULT_MISSING_PROCESS_ROLE_ID,
        )
        candidates = [
            resource_id
            for resource_id in self.resource_ids_by_project.get(project_id, [])
            if self.resources[resource_id].get("active", True)
            and role_id
            in {
                str(resource_role_id)
                for resource_role_id in self.resources[resource_id].get("role_ids", [])
            }
        ]
        if DEFAULT_MISSING_PROCESS_RESOURCE_ID in candidates:
            return DEFAULT_MISSING_PROCESS_RESOURCE_ID
        if candidates:
            return sorted(candidates)[0]
        raise ServiceValidationError(
            code="default_missing_process_resource_unavailable",
            message="Default missing-process resource could not be created.",
            entity_id=project_id,
        )

    def _ensure_resource_exact_role(self, project_id: str, resource_id: str) -> str:
        resource = self._get_resource(project_id, resource_id)
        role_id = self._project_scoped_role_id(project_id, f"role_{resource_id}")
        existing = self.roles.get(role_id)
        if existing is not None:
            existing["active"] = True
        else:
            self.roles[role_id] = {
                "role_id": role_id,
                "project_id": project_id,
                "name": f"Exact assignment: {resource.get('name') or resource_id}",
                "active": True,
            }
            self.role_ids_by_project[project_id].append(role_id)
        role_ids = list(resource.get("role_ids", []) or [])
        if role_id not in role_ids:
            resource["role_ids"] = [*role_ids, role_id]
        return role_id

    def _project_scoped_role_id(self, project_id: str, base_role_id: str) -> str:
        existing = self.roles.get(base_role_id)
        if existing is None or existing["project_id"] == project_id:
            return base_role_id
        stem = f"role_{symbolify(project_id)}_{base_role_id.removeprefix('role_')}"
        candidate = stem
        suffix = 2
        while True:
            existing = self.roles.get(candidate)
            if existing is None or existing["project_id"] == project_id:
                return candidate
            candidate = f"{stem}_{suffix}"
            suffix += 1

    def _rewrite_process_pins_for_single_requirement(
        self,
        project_id: str,
        process_id: str,
        requirement: RoleRequirementCommand,
    ) -> None:
        for pin_id in list(self.process_role_pin_ids_by_project.get(project_id, [])):
            pin = self.process_role_pins.get(pin_id)
            if pin is None or pin.process_id != process_id:
                continue
            if requirement.role_id not in {
                str(role_id)
                for role_id in self.resources.get(pin.resource_id, {}).get("role_ids", [])
            }:
                continue
            self.process_role_pins[pin_id] = pin.model_copy(
                update={
                    "requirement_id": requirement.requirement_id,
                    "role_id": requirement.role_id,
                }
            )

    def _rebuild_role_requirement_index(self) -> None:
        self.role_requirements = {
            requirement.requirement_id: requirement
            for revisions in self.revisions_by_process.values()
            for revision in revisions
            for requirement in revision.role_requirements
            if requirement.requirement_id is not None
        }

    def _validate_process_role_definition_invariants(self) -> None:
        for project_id in sorted(self.projects):
            for process_id in self.process_ids_by_project.get(project_id, []):
                for revision in self.revisions_by_process.get(process_id, []):
                    if not revision.role_requirements:
                        raise ServiceValidationError(
                            code="process_role_requirements_missing",
                            message="Processes must define exactly one role requirement.",
                            entity_id=process_id,
                            details={"revision_id": revision.revision_id},
                        )
                    if len(revision.role_requirements) != 1:
                        raise ServiceValidationError(
                            code="process_role_requirement_count_invalid",
                            message="Processes must define exactly one role requirement.",
                            entity_id=process_id,
                            details={"revision_id": revision.revision_id},
                        )
                    for index, requirement in enumerate(revision.role_requirements):
                        if requirement.effort_hours <= 0:
                            raise ServiceValidationError(
                                code="process_role_effort_nonpositive",
                                message="Process-role effort_hours must be positive.",
                                field_path=f"role_requirements.{index}.effort_hours",
                                entity_id=process_id,
                                details={"revision_id": revision.revision_id},
                            )

    def _validate_no_orphaned_blocker_processes(self) -> None:
        as_of = dt.datetime.max.replace(tzinfo=dt.UTC)
        for project_id in sorted(self.projects):
            orphaned = self._orphaned_blocker_process_ids(project_id, as_of)
            if orphaned:
                raise ServiceValidationError(
                    code="orphaned_blocker_resolver_process",
                    message="Blocker resolver processes must have a child process.",
                    details={"process_ids": orphaned},
                )

    def _orphaned_blocker_process_ids(
        self,
        project_id: str,
        as_of: dt.datetime,
    ) -> list[str]:
        active_ids = set(self.active_process_ids_as_of(project_id, as_of))
        successors_by_predecessor: dict[str, set[str]] = {
            process_id: set() for process_id in active_ids
        }
        for process_id in active_ids:
            revision = self._latest_revision_as_of(process_id, as_of)
            if revision is None:
                continue
            for dependency_id in revision.dependencies:
                if dependency_id in active_ids:
                    successors_by_predecessor.setdefault(dependency_id, set()).add(
                        process_id
                    )
        return sorted(
            process_id
            for process_id in active_ids
            if self.processes[process_id].process_type == "blocker"
            and not successors_by_predecessor.get(process_id)
        )

    def _validate_process_role_pin(
        self,
        pin: ProcessRolePinRecord,
    ) -> None:
        process = self._get_process(pin.project_id, pin.process_id)
        if pin.pinned_at > pin.updated_at:
            raise ServiceValidationError(
                code="validation_error",
                message="pinned_at must be no later than updated_at.",
                field_path="pinned_at",
            )
        self._get_role(pin.project_id, pin.role_id)
        resource = self._get_resource(pin.project_id, pin.resource_id)
        if not resource.get("active", True):
            raise ServiceValidationError(
                code="inactive_pinned_resource",
                message="Inactive resources cannot be pinned to process-roles.",
                entity_id=pin.resource_id,
            )
        if pin.role_id not in {str(value) for value in resource.get("role_ids", [])}:
            raise ServiceValidationError(
                code="pinned_resource_role_mismatch",
                message="A resource can only be pinned to roles it can perform.",
                entity_id=pin.resource_id,
            )
        revision = self._latest_revision_as_of(process.process_id, pin.pinned_at)
        if revision is None:
            raise ServiceValidationError(
                code="process_revision_not_found",
                message="Process-role pins require a process revision at pinned_at.",
                entity_id=process.process_id,
            )
        matching = []
        for index, requirement in enumerate(revision.role_requirements):
            requirement_id = requirement.requirement_id or self._synthetic_requirement_id(
                process.process_id,
                index,
            )
            if pin.requirement_id is not None and pin.requirement_id != requirement_id:
                continue
            if requirement.role_id == pin.role_id:
                matching.append(requirement_id)
        if not matching:
            raise ServiceValidationError(
                code="pin_requirement_not_found",
                message="Process-role pin must reference an existing process-role.",
                entity_id=pin.process_id,
            )
        if pin.requirement_id is None and len(matching) > 1:
            raise ServiceValidationError(
                code="ambiguous_pin_requirement",
                message=(
                    "requirement_id is required when a process has multiple "
                    "requirements for the same role."
                ),
                field_path="requirement_id",
            )
        resolved_requirement_id = pin.requirement_id or matching[0]
        for existing in self.process_role_pins.values():
            if existing.pin_id == pin.pin_id:
                continue
            if existing.project_id != pin.project_id:
                continue
            if existing.status == "pinned_finished":
                continue
            existing_requirement_id = existing.requirement_id
            if existing_requirement_id is None:
                existing_requirement_id = self._pin_requirement_id(existing)
            if (
                existing.process_id == pin.process_id
                and existing_requirement_id == resolved_requirement_id
            ):
                raise ServiceValidationError(
                    code="process_role_already_pinned",
                    message="A process-role can only have one active pin.",
                    entity_id=existing.pin_id,
                )

    def _invalid_process_role_pin_reason(
        self,
        pin: ProcessRolePinRecord,
        as_of: dt.datetime,
    ) -> str | None:
        if pin.pinned_at > as_of:
            return "future_pinned_at"
        if pin.process_id not in self.processes:
            return "missing_process"
        if not self._is_process_active_as_of(pin.process_id, as_of):
            return "inactive_process"
        revision = self._latest_revision_as_of(pin.process_id, as_of)
        if revision is None:
            return "missing_process_revision"
        matches = []
        for index, requirement in enumerate(revision.role_requirements):
            requirement_id = requirement.requirement_id or self._synthetic_requirement_id(
                pin.process_id,
                index,
            )
            if pin.requirement_id is not None and pin.requirement_id != requirement_id:
                continue
            if requirement.role_id == pin.role_id:
                matches.append(requirement_id)
        if pin.requirement_id is None and len(matches) > 1:
            return "ambiguous_current_process_role"
        if not matches:
            return "missing_current_process_role"
        return None

    def _pin_requirement_id(self, pin: ProcessRolePinRecord) -> str:
        if pin.requirement_id is not None:
            return pin.requirement_id
        revision = self._latest_revision_as_of(pin.process_id, pin.pinned_at)
        if revision is None:
            return f"{pin.process_id}:{pin.role_id}"
        for index, requirement in enumerate(revision.role_requirements):
            if requirement.role_id != pin.role_id:
                continue
            return requirement.requirement_id or self._synthetic_requirement_id(
                pin.process_id,
                index,
            )
        return f"{pin.process_id}:{pin.role_id}"

    @staticmethod
    def _synthetic_requirement_id(process_id: str, index: int) -> str:
        return f"{process_id}-requirement-{index + 1}"

    def _validate_process_has_started_pin(
        self,
        *,
        project_id: str,
        process_id: str,
        edit_at: dt.datetime,
    ) -> None:
        if any(
            pin.pinned_at <= edit_at
            for pin in self.list_process_role_pins(
                project_id,
                as_of=edit_at,
                process_id=process_id,
                include_done=True,
            )
        ):
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

    def _process_pin_started_at(
        self,
        project_id: str,
        process_id: str,
        as_of: dt.datetime,
    ) -> dt.datetime | None:
        return min(
            (
                pin.pinned_at
                for pin in self.list_process_role_pins(
                    project_id,
                    as_of=as_of,
                    process_id=process_id,
                    include_done=True,
                )
            ),
            default=None,
        )

    def _validate_process_role_pins_done_for_done(
        self,
        *,
        project_id: str,
        process_id: str,
        edit_at: dt.datetime,
    ) -> dt.datetime | None:
        revision = self._latest_revision_as_of(process_id, edit_at)
        if revision is None or not revision.role_requirements:
            return None
        verified_by_requirement: dict[str, dt.datetime] = {}
        for pin in self.list_process_role_pins(
            project_id,
            as_of=edit_at,
            process_id=process_id,
            include_done=True,
        ):
            if (
                pin.status != "pinned_finished"
                or pin.verified_done_at is None
                or pin.verified_done_at > edit_at
            ):
                continue
            verified_by_requirement[self._pin_requirement_id(pin)] = pin.verified_done_at
        missing = []
        for index, requirement in enumerate(revision.role_requirements):
            requirement_id = (
                requirement.requirement_id
                or self._synthetic_requirement_id(process_id, index)
            )
            if requirement_id in verified_by_requirement:
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
        return max(verified_by_requirement.values(), default=None)

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

    def _validated_calendar_overrides(
        self,
        project_id: str,
        overrides: list[ResourceCalendarOverrideCommand],
        *,
        active: bool,
    ) -> list[dict[str, Any]]:
        records = []
        seen_ids: set[str] = set()
        for override in overrides:
            rule_id = override.rule_id or new_id()
            if rule_id in seen_ids:
                raise ServiceValidationError(
                    code="duplicate_calendar_override",
                    message="Resource calendar override rule ids must be unique.",
                    entity_id=rule_id,
                    field_path="calendar_overrides",
                )
            seen_ids.add(rule_id)
            calendar = self._get_calendar(project_id, override.calendar_id)
            if active and not calendar["active"]:
                raise ServiceValidationError(
                    code="inactive_calendar",
                    message=(
                        "Inactive calendars cannot be assigned to active resources."
                    ),
                    entity_id=override.calendar_id,
                    field_path="calendar_overrides",
                )
            records.append(
                {
                    "rule_id": rule_id,
                    "calendar_id": override.calendar_id,
                    "starts_at": override.starts_at,
                    "ends_at": override.ends_at,
                    "reason": override.reason,
                }
            )
        self._validate_calendar_override_intervals(records)
        return sorted(records, key=lambda rule: rule["starts_at"])

    def _validate_calendar_override_intervals(
        self,
        overrides: list[dict[str, Any]],
    ) -> None:
        previous_end: dt.datetime | None = None
        for index, override in enumerate(
            sorted(overrides, key=lambda rule: rule["starts_at"]),
        ):
            starts_at = override["starts_at"]
            if previous_end is None:
                if index > 0:
                    raise ServiceValidationError(
                        code="calendar_override_overlap",
                        message="Resource calendar overrides must not overlap.",
                        field_path="calendar_overrides",
                    )
            elif starts_at < previous_end:
                raise ServiceValidationError(
                    code="calendar_override_overlap",
                    message="Resource calendar overrides must not overlap.",
                    field_path="calendar_overrides",
                )
            previous_end = override.get("ends_at")

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
            and (
                self.resources[resource_id]["calendar_id"] == calendar_id
                or any(
                    override["calendar_id"] == calendar_id
                    for override in self.resources[resource_id].get(
                        "calendar_overrides",
                        [],
                    )
                )
            )
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
            if revision.effective_at > as_of:
                continue
            if latest is None or revision.effective_at >= latest.effective_at:
                latest = revision
        return latest

    def _unique_symbol(self, project_id: str, base_symbol: str) -> str:
        existing = {
            self.processes[process_id].symbol
            for process_id in self.process_ids_by_project[project_id]
        }
        active_as_of = dt.datetime.max.replace(tzinfo=dt.UTC)
        existing.update(
            alias
            for alias, process_id in self.process_aliases.get(project_id, {}).items()
            if self._is_process_active_as_of(process_id, active_as_of)
        )
        if base_symbol not in existing:
            return base_symbol

        suffix = 1
        while f"{base_symbol}{suffix}" in existing:
            suffix += 1
        return f"{base_symbol}{suffix}"

    def _set_project_resource_currency(
        self,
        project_id: str,
        default_currency: str,
    ) -> None:
        for resource_id in self.resource_ids_by_project.get(project_id, []):
            if resource_id in self.resources:
                self.resources[resource_id]["cost_currency"] = default_currency

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
        validation_times = self._revision_validation_times(project_id, candidate)
        for as_of in validation_times:
            graph = nx.DiGraph()
            graph.add_nodes_from(self.process_ids_by_project[project_id])
            for process_id in self.process_ids_by_project[project_id]:
                if process_id == candidate.process_id:
                    revision = candidate
                else:
                    revision = self._latest_revision_as_of(process_id, as_of)
                if revision is None:
                    continue
                for dependency_id in revision.dependencies:
                    graph.add_edge(dependency_id, process_id)

            if nx.is_directed_acyclic_graph(graph):
                continue
            cycle = [
                {
                    "predecessor_process_id": predecessor,
                    "successor_process_id": successor,
                }
                for predecessor, successor in nx.find_cycle(graph)
            ]
            raise ServiceValidationError(
                code="dependency_cycle",
                message="Adding this process revision would create a dependency cycle.",
                field_path="dependencies",
                entity_id=candidate.process_id,
                details={"as_of": as_of.isoformat(), "cycle": cycle},
            )

    def _revision_validation_times(
        self,
        project_id: str,
        candidate: ProcessRevisionRecord,
    ) -> list[dt.datetime]:
        next_candidate_revision_at = min(
            (
                revision.effective_at
                for revision in self.revisions_by_process.get(
                    candidate.process_id,
                    [],
                )
                if revision.effective_at > candidate.effective_at
            ),
            default=None,
        )
        times = {candidate.effective_at}
        for process_id in self.process_ids_by_project[project_id]:
            for revision in self.revisions_by_process.get(process_id, []):
                if revision.effective_at < candidate.effective_at:
                    continue
                if (
                    next_candidate_revision_at is not None
                    and revision.effective_at >= next_candidate_revision_at
                ):
                    continue
                times.add(revision.effective_at)
        return sorted(times)

    def _validate_active_dependency_graph_acyclic(
        self,
        project_id: str,
        as_of: dt.datetime,
        field_path: str,
    ) -> None:
        graph = self._active_dependency_graph(project_id, as_of)
        if nx.is_directed_acyclic_graph(graph):
            return
        cycle = [
            {"predecessor_process_id": predecessor, "successor_process_id": successor}
            for predecessor, successor in nx.find_cycle(graph)
        ]
        raise ServiceValidationError(
            code="dependency_cycle",
            message="Graph rewrite would create a dependency cycle.",
            field_path=field_path,
            details={"cycle": cycle},
        )
