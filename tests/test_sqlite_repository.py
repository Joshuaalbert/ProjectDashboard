import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from projdash.service import bootstrap
from projdash.service.commands import CommandEnvelope
from projdash.service.errors import ServiceValidationError
from projdash.service.models import (
    CalendarWeeklyWindowCommand,
    CostUnit,
    ProcessRevisionRecord,
    RoleRequirementCommand,
)
from projdash.service.queries import QueryEnvelope
from projdash.service.results import CommandResult
from projdash.service.service import ProjectService
from projdash.service.sqlite_repository import SQLiteProjectRepository

NOW = dt.datetime(2026, 1, 15, 9, 0, tzinfo=dt.UTC)


def test_sqlite_repository_persists_projection_and_command_replay(tmp_path: Path):
    db_path = tmp_path / "project.sqlite"
    repository = SQLiteProjectRepository(db_path)
    staged = _seed_repository(repository.clone())
    repository.replace_with(staged)
    command_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    repository.replace_command_replay_cache(
        {
            command_id: {
                "payload-hash": CommandResult(
                    command_id=command_id,
                    entity_ids={"project_id": "project-sqlite"},
                )
            }
        }
    )
    repository.close()

    reopened = SQLiteProjectRepository(db_path)
    try:
        project = reopened.get_project("project-sqlite")
        assert project.name == "SQLite Project"
        assert reopened.roles["role-eng"]["name"] == "Engineer"
        assert reopened.resources["resource-ada"]["role_ids"] == ["role-eng"]
        assert reopened.processes["process-build"].symbol == "process-build"
        assert reopened.list_blockers("project-sqlite", include_resolved=True)[
            0
        ].description == "Waiting for vendor"
        replay_cache = reopened.load_command_replay_cache()
        assert replay_cache[command_id]["payload-hash"].entity_ids == {
            "project_id": "project-sqlite"
        }
    finally:
        reopened.close()


def test_sqlite_repository_persists_direct_repository_mutations(tmp_path: Path):
    db_path = tmp_path / "direct.sqlite"
    repository = SQLiteProjectRepository(db_path)
    repository.create_project(
        name="Direct SQLite Project",
        start_at=NOW,
        default_currency="EUR",
        project_id="project-direct",
    )
    repository.create_role(
        project_id="project-direct",
        name="Reviewer",
        role_id="role-reviewer",
    )
    repository.close()

    reopened = SQLiteProjectRepository(db_path)
    try:
        assert reopened.get_project("project-direct").default_currency == "EUR"
        assert reopened.roles["role-reviewer"]["name"] == "Reviewer"
    finally:
        reopened.close()


def test_sqlite_repository_round_trips_process_evidence_line_items(tmp_path: Path):
    db_path = tmp_path / "process-evidence.sqlite"
    repository = SQLiteProjectRepository(db_path)
    service = ProjectService(repository)
    try:
        project_id = _handle_service(
            service,
            {
                "action": "create_project",
                "name": "Evidence SQLite Project",
                "start_at": NOW.isoformat(),
            },
        ).entity_ids["project_id"]
        process_id = _handle_service(
            service,
            {
                "action": "upsert_process_revision",
                "project_id": project_id,
                "process_id": "process-build",
                "name": "Build",
                "effective_at": NOW.isoformat(),
                "duration_business_days": 1,
            },
        ).entity_ids["process_id"]
        _handle_service(
            service,
            {
                "action": "upsert_process_evidence_line_item",
                "project_id": project_id,
                "process_id": process_id,
                "line_item": "done_criteria",
                "last_evidence_at": NOW.isoformat(),
                "evidence_note": "Done criteria confirmed.",
                "evidence_source": "meeting-notes",
                "updated_at": NOW.isoformat(),
            },
        )
    finally:
        repository.close()

    reopened = SQLiteProjectRepository(db_path)
    reopened_service = ProjectService(reopened)
    try:
        rows = _query_service(
            reopened_service,
            {
                "action": "query_process_evidence_line_items",
                "project_id": project_id,
                "process_id": process_id,
            },
        )["line_items"]
        assert rows[0]["line_item"] == "done_criteria"
        assert rows[0]["last_evidence_at"] == NOW.isoformat()
        assert rows[0]["evidence_note"] == "Done criteria confirmed."
        assert rows[0]["evidence_source"] == "meeting-notes"
    finally:
        reopened.close()


def test_sqlite_repository_round_trips_process_role_pins(tmp_path: Path):
    db_path = tmp_path / "process-role-pin.sqlite"
    repository = SQLiteProjectRepository(db_path)
    service = ProjectService(repository)
    release_at = NOW + dt.timedelta(hours=4)
    release_json = release_at.isoformat().replace("+00:00", "Z")
    now_json = NOW.isoformat().replace("+00:00", "Z")
    try:
        project_id = _handle_service(
            service,
            {
                "action": "create_project",
                "name": "Work Plan SQLite Project",
                "start_at": NOW.isoformat(),
            },
        ).entity_ids["project_id"]
        role_id = _handle_service(
            service,
            {
                "action": "create_role",
                "project_id": project_id,
                "role_id": "role-engineer",
                "name": "Engineer",
            },
        ).entity_ids["role_id"]
        calendar_id = _handle_service(
            service,
            {
                "action": "upsert_resource_calendar",
                "project_id": project_id,
                "calendar_id": "calendar-default",
                "name": "Default",
                "timezone": "UTC",
                "weekly_windows": [
                    {
                        "window_id": "window-thu",
                        "weekday": 3,
                        "start_local_time": "09:00",
                        "end_local_time": "17:00",
                        "capacity_hours": 8,
                    }
                ],
            },
        ).entity_ids["calendar_id"]
        resource_id = _handle_service(
            service,
            {
                "action": "upsert_resource",
                "project_id": project_id,
                "resource_id": "resource-ada",
                "name": "Ada",
                "role_ids": [role_id],
                "calendar_id": calendar_id,
                "available_from_at": NOW.isoformat(),
                "cost_rate": "100",
                "cost_unit": "hour",
            },
        ).entity_ids["resource_id"]
        process_id = _handle_service(
            service,
            {
                "action": "upsert_process_revision",
                "project_id": project_id,
                "process_id": "process-build",
                "name": "Build",
                "effective_at": NOW.isoformat(),
                "duration_business_days": 1,
                "role_requirements": [
                    {
                        "requirement_id": "req-build-eng",
                        "role_id": role_id,
                        "effort_hours": 4,
                    }
                ],
            },
        ).entity_ids["process_id"]
        result = _handle_service(
            service,
            {
                "action": "upsert_process_role_pin",
                "project_id": project_id,
                "pin_id": "pin-ada-build",
                "process_id": process_id,
                "requirement_id": "req-build-eng",
                "role_id": role_id,
                "resource_id": resource_id,
                "pinned_at": NOW.isoformat(),
                "forecast_finish_at": release_at.isoformat(),
                "updated_at": NOW.isoformat(),
            },
        )
        evidence_result = _handle_service(
            service,
            {
                "action": "upsert_resource_evidence_line_item",
                "project_id": project_id,
                "resource_id": resource_id,
                "line_item": "resource_pinned_knowledge_complete",
                "last_modified_at": NOW.isoformat(),
                "last_evidence_at": NOW.isoformat(),
                "evidence_note": "Ada said this is her complete pinned plate.",
                "updated_at": NOW.isoformat(),
            },
        )
    finally:
        repository.close()

    reopened = SQLiteProjectRepository(db_path)
    reopened_service = ProjectService(reopened)
    try:
        pins = _query_service(
            reopened_service,
            {
                "action": "query_process_role_pins",
                "project_id": project_id,
                "as_of": (NOW + dt.timedelta(hours=1)).isoformat(),
                "resource_id": resource_id,
            },
        )["pins"]
        evidence = _query_service(
            reopened_service,
            {
                "action": "query_resource_evidence_line_items",
                "project_id": project_id,
                "resource_id": resource_id,
            },
        )["line_items"]

        assert result.entity_ids["pin_id"] == "pin-ada-build"
        assert pins[0]["pin_id"] == "pin-ada-build"
        assert pins[0]["forecast_finish_at"] == release_json
        assert evidence[0]["evidence_line_id"] == evidence_result.entity_ids[
            "evidence_line_id"
        ]
        assert evidence[0]["line_item"] == "resource_pinned_knowledge_complete"
        assert evidence[0]["last_evidence_at"] == now_json
    finally:
        reopened.close()


def test_sqlite_repository_repairs_processes_missing_role_requirements(
    tmp_path: Path,
):
    db_path = tmp_path / "missing-role-repair.sqlite"
    repository = SQLiteProjectRepository(db_path)
    repository.create_project(
        name="Missing Role Repair",
        start_at=NOW,
        project_id="project-repair",
    )
    repository.close()
    with sqlite3.connect(db_path) as conn:
        _insert_raw_entity(
            conn,
            "process",
            "process-missing-role",
            "project-repair",
            {
                "process_id": "process-missing-role",
                "project_id": "project-repair",
                "symbol": "process-missing-role",
                "process_type": "standard",
            },
        )
        _insert_raw_entity(
            conn,
            "revision",
            "revision-missing-role",
            "project-repair",
            {
                "revision_id": "revision-missing-role",
                "process_id": "process-missing-role",
                "project_id": "project-repair",
                "effective_at": NOW.isoformat(),
                "name": "Missing role",
                "description": "",
                "duration_business_days": 1,
                "dependencies": [],
                "earliest_start_at": None,
                "start_at_earliest": False,
                "delay_after_dependencies_business_days": 0,
                "required_roles": {},
                "role_requirements": [],
                "assumption_note": None,
            },
            bypass_revision_role_trigger=True,
        )

    reopened = SQLiteProjectRepository(db_path)
    try:
        revision = reopened.revisions_by_process["process-missing-role"][-1]
        assert reopened.roles["role_res_josh"]["name"] == "Josh"
        assert revision.role_requirements[0].role_id == "role_res_josh"
        assert revision.role_requirements[0].effort_hours == 1
    finally:
        reopened.close()


def test_sqlite_repository_repairs_multi_role_process_to_best_exact_resource_role(
    tmp_path: Path,
):
    db_path = tmp_path / "multi-role-repair.sqlite"
    repository = SQLiteProjectRepository(db_path)
    repository.create_project(
        name="Multi Role Repair",
        start_at=NOW,
        project_id="project-repair",
    )
    repository.create_role(
        project_id="project-repair",
        role_id="role-ops",
        name="Ops",
    )
    repository.create_role(
        project_id="project-repair",
        role_id="role-eng",
        name="Engineering",
    )
    repository.upsert_resource_calendar(
        project_id="project-repair",
        calendar_id="calendar-default",
        name="Default",
        timezone="UTC",
        weekly_windows=[
            CalendarWeeklyWindowCommand(
                window_id="weekday-thu",
                weekday=3,
                start_local_time="09:00",
                end_local_time="17:00",
                capacity_hours=8,
            )
        ],
        active=True,
    )
    repository.upsert_resource(
        project_id="project-repair",
        resource_id="res_scott",
        name="Scott",
        resource_type="internal",
        role_ids=["role-ops"],
        calendar_id="calendar-default",
        available_from_at=NOW,
        cost_rate=100,
        cost_unit=CostUnit.HOUR,
    )
    repository.upsert_resource(
        project_id="project-repair",
        resource_id="res_ada",
        name="Ada",
        resource_type="internal",
        role_ids=["role-eng"],
        calendar_id="calendar-default",
        available_from_at=NOW,
        cost_rate=100,
        cost_unit=CostUnit.HOUR,
    )
    repository.close()
    with sqlite3.connect(db_path) as conn:
        _insert_raw_entity(
            conn,
            "process",
            "process-multi-role",
            "project-repair",
            {
                "process_id": "process-multi-role",
                "project_id": "project-repair",
                "symbol": "process-multi-role",
                "process_type": "standard",
            },
        )
        _insert_raw_entity(
            conn,
            "revision",
            "revision-multi-role",
            "project-repair",
            {
                "revision_id": "revision-multi-role",
                "process_id": "process-multi-role",
                "project_id": "project-repair",
                "effective_at": NOW.isoformat(),
                "name": "Multi role",
                "description": "",
                "duration_business_days": 1,
                "dependencies": [],
                "earliest_start_at": None,
                "start_at_earliest": False,
                "delay_after_dependencies_business_days": 0,
                "required_roles": {},
                "role_requirements": [
                    {
                        "requirement_id": "req-ops",
                        "role_id": "role-ops",
                        "effort_hours": 2,
                    },
                    {
                        "requirement_id": "req-eng",
                        "role_id": "role-eng",
                        "effort_hours": 6,
                    },
                ],
                "assumption_note": None,
            },
            bypass_revision_role_trigger=True,
        )

    reopened = SQLiteProjectRepository(db_path)
    try:
        revision = reopened.revisions_by_process["process-multi-role"][-1]
        assert len(revision.role_requirements) == 1
        assert revision.role_requirements[0].role_id == "role_res_ada"
        assert revision.role_requirements[0].effort_hours == 8
        assert reopened.roles["role_res_ada"]["name"] == "Exact assignment: Ada"
        assert "role_res_ada" in reopened.resources["res_ada"]["role_ids"]
    finally:
        reopened.close()


def test_sqlite_repository_repairs_exact_resource_role_cross_project_collision(
    tmp_path: Path,
):
    db_path = tmp_path / "multi-role-cross-project-repair.sqlite"
    repository = SQLiteProjectRepository(db_path)
    repository.create_project(
        name="Project A",
        start_at=NOW,
        project_id="project-a",
    )
    repository.create_role(
        project_id="project-a",
        role_id="role_res_ada",
        name="Exact assignment: Ada",
    )
    repository.create_project(
        name="Project B",
        start_at=NOW,
        project_id="project-b",
    )
    repository.create_role(
        project_id="project-b",
        role_id="role-ops",
        name="Ops",
    )
    repository.create_role(
        project_id="project-b",
        role_id="role-eng",
        name="Engineering",
    )
    repository.upsert_resource_calendar(
        project_id="project-b",
        calendar_id="calendar-default-b",
        name="Default B",
        timezone="UTC",
        weekly_windows=[
            CalendarWeeklyWindowCommand(
                window_id="weekday-thu-b",
                weekday=3,
                start_local_time="09:00",
                end_local_time="17:00",
                capacity_hours=8,
            )
        ],
        active=True,
    )
    repository.upsert_resource(
        project_id="project-b",
        resource_id="res_scott",
        name="Scott",
        resource_type="internal",
        role_ids=["role-ops"],
        calendar_id="calendar-default-b",
        available_from_at=NOW,
        cost_rate=100,
        cost_unit=CostUnit.HOUR,
    )
    repository.upsert_resource(
        project_id="project-b",
        resource_id="res_ada",
        name="Ada",
        resource_type="internal",
        role_ids=["role-eng"],
        calendar_id="calendar-default-b",
        available_from_at=NOW,
        cost_rate=100,
        cost_unit=CostUnit.HOUR,
    )
    repository.close()
    with sqlite3.connect(db_path) as conn:
        _insert_raw_entity(
            conn,
            "process",
            "process-multi-role-b",
            "project-b",
            {
                "process_id": "process-multi-role-b",
                "project_id": "project-b",
                "symbol": "process-multi-role-b",
                "process_type": "standard",
            },
        )
        _insert_raw_entity(
            conn,
            "revision",
            "revision-multi-role-b",
            "project-b",
            {
                "revision_id": "revision-multi-role-b",
                "process_id": "process-multi-role-b",
                "project_id": "project-b",
                "effective_at": NOW.isoformat(),
                "name": "Multi role B",
                "description": "",
                "duration_business_days": 1,
                "dependencies": [],
                "earliest_start_at": None,
                "start_at_earliest": False,
                "delay_after_dependencies_business_days": 0,
                "required_roles": {},
                "role_requirements": [
                    {
                        "requirement_id": "req-ops-b",
                        "role_id": "role-ops",
                        "effort_hours": 2,
                    },
                    {
                        "requirement_id": "req-eng-b",
                        "role_id": "role-eng",
                        "effort_hours": 6,
                    },
                ],
                "assumption_note": None,
            },
            bypass_revision_role_trigger=True,
        )

    reopened = SQLiteProjectRepository(db_path)
    try:
        revision = reopened.revisions_by_process["process-multi-role-b"][-1]
        assert len(revision.role_requirements) == 1
        assert revision.role_requirements[0].role_id == "role_PB_res_ada"
        assert revision.role_requirements[0].effort_hours == 8
        assert reopened.roles["role_res_ada"]["project_id"] == "project-a"
        assert reopened.roles["role_PB_res_ada"]["project_id"] == "project-b"
        assert reopened.roles["role_PB_res_ada"]["name"] == "Exact assignment: Ada"
        assert "role_PB_res_ada" in reopened.resources["res_ada"]["role_ids"]
    finally:
        reopened.close()


def test_sqlite_repository_repairs_every_revision_to_exactly_one_role(
    tmp_path: Path,
):
    db_path = tmp_path / "all-revisions-role-repair.sqlite"
    repository = SQLiteProjectRepository(db_path)
    repository.create_project(
        name="All Revision Role Repair",
        start_at=NOW,
        project_id="project-repair",
    )
    repository.create_role(
        project_id="project-repair",
        role_id="role-ops",
        name="Ops",
    )
    repository.create_role(
        project_id="project-repair",
        role_id="role-eng",
        name="Engineering",
    )
    repository.upsert_resource_calendar(
        project_id="project-repair",
        calendar_id="calendar-default",
        name="Default",
        timezone="UTC",
        weekly_windows=[
            CalendarWeeklyWindowCommand(
                window_id="weekday-thu",
                weekday=3,
                start_local_time="09:00",
                end_local_time="17:00",
                capacity_hours=8,
            )
        ],
        active=True,
    )
    repository.upsert_resource(
        project_id="project-repair",
        resource_id="res_scott",
        name="Scott",
        resource_type="internal",
        role_ids=["role-ops"],
        calendar_id="calendar-default",
        available_from_at=NOW,
        cost_rate=100,
        cost_unit=CostUnit.HOUR,
    )
    repository.upsert_resource(
        project_id="project-repair",
        resource_id="res_ada",
        name="Ada",
        resource_type="internal",
        role_ids=["role-eng"],
        calendar_id="calendar-default",
        available_from_at=NOW,
        cost_rate=100,
        cost_unit=CostUnit.HOUR,
    )
    repository.close()
    with sqlite3.connect(db_path) as conn:
        _insert_raw_entity(
            conn,
            "process",
            "process-repair-all-revisions",
            "project-repair",
            {
                "process_id": "process-repair-all-revisions",
                "project_id": "project-repair",
                "symbol": "repair-all-revisions",
                "process_type": "standard",
            },
        )
        for revision_id, offset_hours, role_requirements in (
            ("revision-missing-role", 0, []),
            (
                "revision-multi-role",
                1,
                [
                    {
                        "requirement_id": "req-ops",
                        "role_id": "role-ops",
                        "effort_hours": 2,
                    },
                    {
                        "requirement_id": "req-eng",
                        "role_id": "role-eng",
                        "effort_hours": 6,
                    },
                ],
            ),
            (
                "revision-single-role",
                2,
                [
                    {
                        "requirement_id": "req-single-eng",
                        "role_id": "role-eng",
                        "effort_hours": 4,
                    }
                ],
            ),
        ):
            _insert_raw_entity(
                conn,
                "revision",
                revision_id,
                "project-repair",
                {
                    "revision_id": revision_id,
                    "process_id": "process-repair-all-revisions",
                    "project_id": "project-repair",
                    "effective_at": (
                        NOW + dt.timedelta(hours=offset_hours)
                    ).isoformat(),
                    "name": "Repair all revisions",
                    "description": "Done means engineering repair is complete.",
                    "duration_business_days": 1,
                    "dependencies": [],
                    "earliest_start_at": None,
                    "start_at_earliest": False,
                    "delay_after_dependencies_business_days": 0,
                    "required_roles": {},
                    "role_requirements": role_requirements,
                    "assumption_note": None,
                },
                bypass_revision_role_trigger=len(role_requirements) != 1,
            )

    reopened = SQLiteProjectRepository(db_path)
    try:
        revisions = reopened.revisions_by_process["process-repair-all-revisions"]
        requirements_by_revision = {
            revision.revision_id: revision.role_requirements[0]
            for revision in revisions
        }

        assert len(revisions) == 3
        assert all(len(revision.role_requirements) == 1 for revision in revisions)
        assert requirements_by_revision["revision-missing-role"].role_id == (
            "role_res_josh"
        )
        assert requirements_by_revision["revision-missing-role"].effort_hours == 1
        assert requirements_by_revision["revision-multi-role"].role_id == (
            "role_res_ada"
        )
        assert requirements_by_revision["revision-multi-role"].effort_hours == 8
        assert requirements_by_revision["revision-single-role"].role_id == "role-eng"
        assert requirements_by_revision["revision-single-role"].effort_hours == 4
    finally:
        reopened.close()

    with sqlite3.connect(db_path) as conn:
        stored_revision_payloads = [
            json.loads(row[0])["data"]
            for row in conn.execute(
                """
                SELECT payload_json
                FROM repository_entity
                WHERE kind = 'revision'
                  AND project_id = 'project-repair'
                ORDER BY entity_id
                """
            )
        ]

    assert len(stored_revision_payloads) == 3
    assert all(
        len(payload["role_requirements"]) == 1
        for payload in stored_revision_payloads
    )


def test_sqlite_repository_rejects_persisting_historical_multi_role_revision(
    tmp_path: Path,
):
    db_path = tmp_path / "reject-historical-multi-role.sqlite"
    repository = SQLiteProjectRepository(db_path)
    repository.create_project(
        name="Reject Historical Multi Role",
        start_at=NOW,
        project_id="project-reject",
    )
    repository.create_role(
        project_id="project-reject",
        role_id="role-ops",
        name="Ops",
    )
    repository.create_role(
        project_id="project-reject",
        role_id="role-eng",
        name="Engineering",
    )
    repository.upsert_process_revision(
        project_id="project-reject",
        process_id="process-valid",
        process_type="standard",
        name="Valid",
        description="Valid process.",
        effective_at=NOW + dt.timedelta(hours=1),
        duration_business_days=1,
        dependencies=[],
        earliest_start_at=None,
        start_at_earliest=False,
        delay_after_dependencies_business_days=0,
        required_roles={},
        role_requirements=[
            RoleRequirementCommand(
                requirement_id="req-valid",
                role_id="role-eng",
                effort_hours=4,
            )
        ],
        assumption_note=None,
    )
    staged = repository.clone()
    staged.revisions_by_process["process-valid"].insert(
        0,
        ProcessRevisionRecord.model_construct(
            revision_id="revision-invalid-history",
            process_id="process-valid",
            project_id="project-reject",
            effective_at=NOW,
            name="Invalid history",
            description="Invalid historical process revision.",
            duration_business_days=1,
            role_requirements=[
                RoleRequirementCommand(
                    requirement_id="req-ops-invalid",
                    role_id="role-ops",
                    effort_hours=2,
                ),
                RoleRequirementCommand(
                    requirement_id="req-eng-invalid",
                    role_id="role-eng",
                    effort_hours=6,
                ),
            ],
        ),
    )

    try:
        with pytest.raises(ServiceValidationError) as exc_info:
            repository.replace_with(staged)
        assert exc_info.value.code == "process_role_requirement_count_invalid"
        assert exc_info.value.details["revision_id"] == "revision-invalid-history"
    finally:
        repository.close()


def test_sqlite_schema_rejects_revision_rows_without_exactly_one_role(
    tmp_path: Path,
):
    db_path = tmp_path / "schema-rejects-invalid-revision-role-count.sqlite"
    repository = SQLiteProjectRepository(db_path)
    repository.create_project(
        name="Schema Rejects Invalid Revision Role Count",
        start_at=NOW,
        project_id="project-schema",
    )
    repository.close()

    base_revision = {
        "revision_id": "revision-invalid",
        "process_id": "process-invalid",
        "project_id": "project-schema",
        "effective_at": NOW.isoformat(),
        "name": "Invalid",
        "description": "Invalid revision role cardinality.",
        "duration_business_days": 1,
        "dependencies": [],
        "earliest_start_at": None,
        "start_at_earliest": False,
        "delay_after_dependencies_business_days": 0,
        "required_roles": {},
        "assumption_note": None,
    }

    with sqlite3.connect(db_path) as conn:
        for role_requirements in (
            [],
            [
                {
                    "requirement_id": "req-a",
                    "role_id": "role-a",
                    "effort_hours": 1,
                },
                {
                    "requirement_id": "req-b",
                    "role_id": "role-b",
                    "effort_hours": 1,
                },
            ],
        ):
            with pytest.raises(sqlite3.IntegrityError, match="exactly one item"):
                _insert_raw_entity(
                    conn,
                    "revision",
                    f"revision-invalid-{len(role_requirements)}",
                    "project-schema",
                    {
                        **base_revision,
                        "revision_id": f"revision-invalid-{len(role_requirements)}",
                        "role_requirements": role_requirements,
                    },
                )


def test_sqlite_repository_deletes_legacy_childless_blocker_processes(
    tmp_path: Path,
):
    db_path = tmp_path / "orphan-blocker-repair.sqlite"
    repository = SQLiteProjectRepository(db_path)
    repository.create_project(
        name="Orphan Blocker Repair",
        start_at=NOW,
        project_id="project-repair",
    )
    repository.close()
    with sqlite3.connect(db_path) as conn:
        _insert_raw_entity(
            conn,
            "process",
            "resolve-orphan",
            "project-repair",
            {
                "process_id": "resolve-orphan",
                "project_id": "project-repair",
                "symbol": "resolve-orphan",
                "process_type": "blocker",
            },
        )
        _insert_raw_entity(
            conn,
            "revision",
            "revision-resolve-orphan",
            "project-repair",
            {
                "revision_id": "revision-resolve-orphan",
                "process_id": "resolve-orphan",
                "project_id": "project-repair",
                "effective_at": NOW.isoformat(),
                "name": "Resolve orphan",
                "description": "Resolve blocker: orphan",
                "duration_business_days": 0,
                "dependencies": [],
                "earliest_start_at": None,
                "start_at_earliest": False,
                "delay_after_dependencies_business_days": 0,
                "required_roles": {},
                "role_requirements": [],
                "assumption_note": None,
            },
            bypass_revision_role_trigger=True,
        )

    reopened = SQLiteProjectRepository(db_path)
    try:
        assert "resolve-orphan" not in reopened.processes
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT kind, entity_id
                FROM repository_entity
                WHERE entity_id IN ('resolve-orphan', 'revision-resolve-orphan')
                """
            ).fetchall()
        assert rows == []
    finally:
        reopened.close()


def test_sqlite_repository_migrates_legacy_work_plan_rows_to_pins(tmp_path: Path):
    db_path = tmp_path / "legacy-work-plan.sqlite"
    repository = SQLiteProjectRepository(db_path)
    service = ProjectService(repository)
    release_at = NOW + dt.timedelta(hours=4)
    try:
        project_id = _handle_service(
            service,
            {
                "action": "create_project",
                "name": "Legacy Work Plan Migration",
                "start_at": NOW.isoformat(),
            },
        ).entity_ids["project_id"]
        role_id = _handle_service(
            service,
            {
                "action": "create_role",
                "project_id": project_id,
                "role_id": "role-engineer",
                "name": "Engineer",
            },
        ).entity_ids["role_id"]
        calendar_id = _handle_service(
            service,
            {
                "action": "upsert_resource_calendar",
                "project_id": project_id,
                "calendar_id": "calendar-default",
                "name": "Default",
                "timezone": "UTC",
                "weekly_windows": [
                    {
                        "window_id": "window-thu",
                        "weekday": 3,
                        "start_local_time": "09:00",
                        "end_local_time": "17:00",
                        "capacity_hours": 8,
                    }
                ],
            },
        ).entity_ids["calendar_id"]
        resource_id = _handle_service(
            service,
            {
                "action": "upsert_resource",
                "project_id": project_id,
                "resource_id": "resource-ada",
                "name": "Ada",
                "role_ids": [role_id],
                "calendar_id": calendar_id,
                "available_from_at": NOW.isoformat(),
                "cost_rate": "100",
                "cost_unit": "hour",
            },
        ).entity_ids["resource_id"]
        process_id = _handle_service(
            service,
            {
                "action": "upsert_process_revision",
                "project_id": project_id,
                "process_id": "process-build",
                "name": "Build",
                "effective_at": NOW.isoformat(),
                "duration_business_days": 1,
                "role_requirements": [
                    {
                        "requirement_id": "req-build-eng",
                        "role_id": role_id,
                        "effort_hours": 4,
                    }
                ],
            },
        ).entity_ids["process_id"]
    finally:
        repository.close()

    legacy_payload = {
        "data": {
            "work_plan_id": "work-plan-ada-build",
            "project_id": project_id,
            "resource_id": resource_id,
            "starts_at": NOW.isoformat(),
            "planning_release_at": release_at.isoformat(),
            "status": "accepted",
            "capacity_policy": "opaque",
            "forecasts": [
                {
                    "forecast_id": "forecast-build",
                    "project_id": project_id,
                    "work_plan_id": "work-plan-ada-build",
                    "process_id": process_id,
                    "requirement_id": "req-build-eng",
                    "role_id": role_id,
                    "forecast_finish_at": release_at.isoformat(),
                }
            ],
            "source": "legacy",
            "note": "Legacy opaque work plan.",
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO repository_entity(
                kind, entity_id, project_id, payload_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "teammate_work_plan",
                "work-plan-ada-build",
                project_id,
                json.dumps(legacy_payload, sort_keys=True),
                NOW.isoformat(),
            ),
        )

    reopened = SQLiteProjectRepository(db_path)
    reopened_service = ProjectService(reopened)
    try:
        pins = _query_service(
            reopened_service,
            {
                "action": "query_process_role_pins",
                "project_id": project_id,
                "as_of": (NOW + dt.timedelta(hours=1)).isoformat(),
                "resource_id": resource_id,
            },
        )["pins"]

        assert pins[0]["pin_id"] == "forecast-build"
        assert pins[0]["process_id"] == process_id
        assert pins[0]["requirement_id"] == "req-build-eng"
        assert pins[0]["resource_id"] == resource_id
    finally:
        reopened.close()


def test_sqlite_repository_migrates_legacy_stake_rows_to_forecasted_pins(
    tmp_path: Path,
):
    db_path = tmp_path / "legacy-stake-window.sqlite"
    repository = SQLiteProjectRepository(db_path)
    service = ProjectService(repository)
    release_at = NOW + dt.timedelta(hours=4)
    release_json = release_at.isoformat().replace("+00:00", "Z")
    try:
        project_id = _handle_service(
            service,
            {
                "action": "create_project",
                "name": "Legacy Stake Migration",
                "start_at": NOW.isoformat(),
            },
        ).entity_ids["project_id"]
        role_id = _handle_service(
            service,
            {
                "action": "create_role",
                "project_id": project_id,
                "role_id": "role-engineer",
                "name": "Engineer",
            },
        ).entity_ids["role_id"]
        calendar_id = _handle_service(
            service,
            {
                "action": "upsert_resource_calendar",
                "project_id": project_id,
                "calendar_id": "calendar-default",
                "name": "Default",
                "timezone": "UTC",
                "weekly_windows": [
                    {
                        "window_id": "window-thu",
                        "weekday": 3,
                        "start_local_time": "09:00",
                        "end_local_time": "17:00",
                        "capacity_hours": 8,
                    }
                ],
            },
        ).entity_ids["calendar_id"]
        resource_id = _handle_service(
            service,
            {
                "action": "upsert_resource",
                "project_id": project_id,
                "resource_id": "resource-ada",
                "name": "Ada",
                "role_ids": [role_id],
                "calendar_id": calendar_id,
                "available_from_at": NOW.isoformat(),
                "cost_rate": "100",
                "cost_unit": "hour",
            },
        ).entity_ids["resource_id"]
        process_id = _handle_service(
            service,
            {
                "action": "upsert_process_revision",
                "project_id": project_id,
                "process_id": "process-build",
                "name": "Build",
                "effective_at": NOW.isoformat(),
                "duration_business_days": 1,
                "role_requirements": [
                    {
                        "requirement_id": "req-build-eng",
                        "role_id": role_id,
                        "effort_hours": 4,
                    }
                ],
            },
        ).entity_ids["process_id"]
    finally:
        repository.close()

    legacy_payload = {
        "data": {
            "stake_id": "stake-ada-build",
            "project_id": project_id,
            "process_id": process_id,
            "requirement_id": "req-build-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "starts_at": NOW.isoformat(),
            "ends_at": None,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "note": "Legacy active focus.",
        }
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO repository_entity(
                kind, entity_id, project_id, payload_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "process_role_stake_window",
                "stake-ada-build",
                project_id,
                json.dumps(legacy_payload, sort_keys=True),
                NOW.isoformat(),
            ),
        )

    reopened = SQLiteProjectRepository(db_path)
    reopened_service = ProjectService(reopened)
    try:
        pins = _query_service(
            reopened_service,
            {
                "action": "query_process_role_pins",
                "project_id": project_id,
                "as_of": (NOW + dt.timedelta(hours=1)).isoformat(),
                "resource_id": resource_id,
            },
        )["pins"]

        assert pins[0]["pin_id"] == "stake-ada-build"
        assert pins[0]["pinned_at"] == NOW.isoformat().replace("+00:00", "Z")
        assert pins[0]["forecast_finish_at"] == release_json
        assert pins[0]["status"] == "pinned_started"
    finally:
        reopened.close()


def test_sqlite_repository_canonical_pin_overrides_same_id_legacy_stake_row(
    tmp_path: Path,
):
    db_path = tmp_path / "canonical-pin-overrides-legacy-stake.sqlite"
    repository = SQLiteProjectRepository(db_path)
    service = ProjectService(repository)
    forecast_at = NOW + dt.timedelta(hours=4)
    try:
        project_id = _handle_service(
            service,
            {
                "action": "create_project",
                "name": "Canonical Pin Precedence",
                "start_at": NOW.isoformat(),
            },
        ).entity_ids["project_id"]
        role_id = _handle_service(
            service,
            {
                "action": "create_role",
                "project_id": project_id,
                "role_id": "role-engineer",
                "name": "Engineer",
            },
        ).entity_ids["role_id"]
        calendar_id = _handle_service(
            service,
            {
                "action": "upsert_resource_calendar",
                "project_id": project_id,
                "calendar_id": "calendar-default",
                "name": "Default",
                "timezone": "UTC",
                "weekly_windows": [
                    {
                        "window_id": "window-thu",
                        "weekday": 3,
                        "start_local_time": "09:00",
                        "end_local_time": "17:00",
                        "capacity_hours": 8,
                    }
                ],
            },
        ).entity_ids["calendar_id"]
        resource_id = _handle_service(
            service,
            {
                "action": "upsert_resource",
                "project_id": project_id,
                "resource_id": "resource-ada",
                "name": "Ada",
                "role_ids": [role_id],
                "calendar_id": calendar_id,
                "available_from_at": NOW.isoformat(),
                "cost_rate": "100",
                "cost_unit": "hour",
            },
        ).entity_ids["resource_id"]
        process_id = _handle_service(
            service,
            {
                "action": "upsert_process_revision",
                "project_id": project_id,
                "process_id": "process-build",
                "name": "Build",
                "effective_at": NOW.isoformat(),
                "duration_business_days": 1,
                "role_requirements": [
                    {
                        "requirement_id": "req-build-eng",
                        "role_id": role_id,
                        "effort_hours": 4,
                    }
                ],
            },
        ).entity_ids["process_id"]
        _handle_service(
            service,
            {
                "action": "upsert_process_role_pin",
                "project_id": project_id,
                "pin_id": "stake-ada-build",
                "process_id": process_id,
                "requirement_id": "req-build-eng",
                "role_id": role_id,
                "resource_id": resource_id,
                "pinned_at": NOW.isoformat(),
                "forecast_finish_at": forecast_at.isoformat(),
                "updated_at": NOW.isoformat(),
                "note": "Canonical repaired pin.",
            },
        )
    finally:
        repository.close()

    legacy_payload = {
        "data": {
            "stake_id": "stake-ada-build",
            "project_id": project_id,
            "process_id": process_id,
            "requirement_id": "req-build-eng",
            "role_id": role_id,
            "resource_id": resource_id,
            "starts_at": NOW.isoformat(),
            "ends_at": (NOW + dt.timedelta(hours=2)).isoformat(),
            "created_at": NOW.isoformat(),
            "updated_at": (NOW + dt.timedelta(hours=2)).isoformat(),
            "note": "Legacy finished focus.",
        }
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO repository_entity(
                kind, entity_id, project_id, payload_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "process_role_stake_window",
                "stake-ada-build",
                project_id,
                json.dumps(legacy_payload, sort_keys=True),
                NOW.isoformat(),
            ),
        )

    reopened = SQLiteProjectRepository(db_path)
    reopened_service = ProjectService(reopened)
    try:
        pins = _query_service(
            reopened_service,
            {
                "action": "query_process_role_pins",
                "project_id": project_id,
                "as_of": (NOW + dt.timedelta(hours=3)).isoformat(),
                "resource_id": resource_id,
            },
        )["pins"]

        assert pins[0]["pin_id"] == "stake-ada-build"
        assert pins[0]["status"] == "pinned_started"
        assert pins[0]["forecast_finish_at"] == forecast_at.isoformat().replace(
            "+00:00",
            "Z",
        )
        assert pins[0]["verified_done_at"] is None
        assert pins[0]["note"] == "Canonical repaired pin."
    finally:
        reopened.close()


def test_sqlite_repository_persists_delete_process_graph_cleanup(tmp_path: Path):
    db_path = tmp_path / "delete-process.sqlite"
    repository = SQLiteProjectRepository(db_path)
    service = ProjectService(repository)
    try:
        project_id = _handle_service(
            service,
            {
                "action": "create_project",
                "name": "Delete SQLite Project",
                "start_at": NOW.isoformat(),
            },
        ).entity_ids["project_id"]
        parent_id = _handle_service(
            service,
            {
                "action": "upsert_process_revision",
                "project_id": project_id,
                "process_id": "process-parent",
                "name": "Parent",
                "effective_at": NOW.isoformat(),
                "duration_business_days": 1,
            },
        ).entity_ids["process_id"]
        child_id = _handle_service(
            service,
            {
                "action": "upsert_process_revision",
                "project_id": project_id,
                "process_id": "process-child",
                "name": "Child",
                "effective_at": NOW.isoformat(),
                "duration_business_days": 1,
                "dependencies": [parent_id],
            },
        ).entity_ids["process_id"]
        blocker_id = _handle_service(
            service,
            {
                "action": "add_blocker",
                "project_id": project_id,
                "process_id": parent_id,
                "blocker_id": "blocker-parent",
                "summary": "Parent blocker",
                "created_at": NOW.isoformat(),
            },
        ).entity_ids["blocker_id"]
        _handle_service(
            service,
            {
                "action": "delete_process",
                "project_id": project_id,
                "process_id": parent_id,
                "edit_at": NOW.isoformat(),
            },
        )
    finally:
        repository.close()

    reopened = SQLiteProjectRepository(db_path)
    reopened_service = ProjectService(reopened)
    try:
        graph = _query_service(
            reopened_service,
            {
                "action": "query_process_graph",
                "project_id": project_id,
                "as_of": NOW.isoformat(),
                "now": NOW.isoformat(),
            },
        )
        blockers = _query_service(
            reopened_service,
            {
                "action": "query_blockers",
                "project_id": project_id,
                "as_of": NOW.isoformat(),
                "include_resolved": True,
            },
        )

        assert {node["process_id"] for node in graph["nodes"]} == {child_id}
        assert graph["edges"] == []
        assert blockers["blockers"] == []
        assert blocker_id not in reopened.blockers
    finally:
        reopened.close()


def test_sqlite_rows_exclude_computed_fields_and_snapshots_are_explicit(
    tmp_path: Path,
):
    db_path = tmp_path / "computed-fields-and-snapshots.sqlite"
    repository = SQLiteProjectRepository(db_path)
    service = ProjectService(repository)
    try:
        project_id = _handle_service(
            service,
            {
                "action": "create_project",
                "name": "Computed Fields SQLite Project",
                "start_at": NOW.isoformat(),
            },
        ).entity_ids["project_id"]
        process_id = _handle_service(
            service,
            {
                "action": "upsert_process_revision",
                "project_id": project_id,
                "process_id": "process-build",
                "process_symbol": "build",
                "name": "Build",
                "effective_at": NOW.isoformat(),
                "duration_business_days": 1,
            },
        ).entity_ids["process_id"]

        graph = _query_service(
            service,
            {
                "action": "query_process_graph",
                "project_id": project_id,
                "as_of": NOW.isoformat(),
                "now": NOW.isoformat(),
                "include_resource_fields": True,
            },
        )
        assert graph["nodes"][0]["computed_status"] == "ready"
        assert graph["nodes"][0]["resource_aware"]["starts_at"] == NOW.isoformat()

        before_commit = _query_service(
            service,
            {
                "action": "query_schedule_snapshots",
                "project_id": project_id,
                "as_of": NOW.isoformat(),
            },
        )
        assert before_commit["snapshots"] == []

        _handle_service(
            service,
            {
                "action": "create_role",
                "project_id": project_id,
                "role_id": "role-extra",
                "name": "Extra",
            },
        )
        after_mutation = _query_service(
            service,
            {
                "action": "query_schedule_snapshots",
                "project_id": project_id,
                "as_of": NOW.isoformat(),
            },
        )
        assert after_mutation["snapshots"] == []

        _handle_service(
            service,
            {
                "action": "commit_project_state",
                "project_id": project_id,
                "committed_at": NOW.isoformat(),
                "terminal_process_symbols": ["build"],
                "note": "Explicit schedule commit",
            },
        )
    finally:
        repository.close()

    with sqlite3.connect(db_path) as connection:
        process_payload = json.loads(
            connection.execute(
                """
                SELECT payload_json
                FROM repository_entity
                WHERE kind = 'process' AND entity_id = ?
                """,
                (process_id,),
            ).fetchone()[0]
        )["data"]
        snapshot_payloads = [
            json.loads(row[0])["data"]
            for row in connection.execute(
                """
                SELECT payload_json
                FROM repository_entity
                WHERE kind = 'schedule_snapshot'
                """
            )
        ]

    for computed_field in (
        "computed_status",
        "status",
        "started_at",
        "finished_at",
        "dependency_only",
        "resource_aware",
    ):
        assert computed_field not in process_payload
    assert len(snapshot_payloads) == 1
    assert snapshot_payloads[0]["note"] == "Explicit schedule commit"

    reopened = SQLiteProjectRepository(db_path)
    reopened_service = ProjectService(reopened)
    try:
        snapshots = _query_service(
            reopened_service,
            {
                "action": "query_schedule_snapshots",
                "project_id": project_id,
                "as_of": NOW.isoformat(),
                "terminal_process_symbols": ["build"],
            },
        )["snapshots"]
        assert len(snapshots) == 1
        assert snapshots[0]["note"] == "Explicit schedule commit"
        assert snapshots[0]["terminal_process_symbols"] == ["build"]
    finally:
        reopened.close()


def test_sqlite_repository_reloads_across_independent_services(tmp_path: Path):
    db_path = tmp_path / "shared.sqlite"
    repository_a = SQLiteProjectRepository(db_path)
    repository_b = SQLiteProjectRepository(db_path)
    service_a = ProjectService(repository_a)
    service_b = ProjectService(repository_b)
    try:
        project_id = _handle_service(
            service_a,
            {
                "action": "create_project",
                "name": "Shared SQLite Project",
                "start_at": NOW.isoformat(),
            },
        ).entity_ids["project_id"]

        projects = _query_service(
            service_b,
            {"action": "query_projects"},
        )["projects"]
        assert [project["project_id"] for project in projects] == [project_id]

        role_id = _handle_service(
            service_b,
            {
                "action": "create_role",
                "project_id": project_id,
                "name": "Reviewer",
            },
        ).entity_ids["role_id"]
        catalog = _query_service(
            service_a,
            {"action": "query_project_catalog", "project_id": project_id},
        )
        assert [role["role_id"] for role in catalog["roles"]] == [role_id]
    finally:
        repository_a.close()
        repository_b.close()


def test_sqlite_repository_rejects_stale_staged_commit(tmp_path: Path):
    db_path = tmp_path / "stale.sqlite"
    repository_a = SQLiteProjectRepository(db_path)
    repository_b = SQLiteProjectRepository(db_path)
    try:
        stale_stage = repository_b.clone()
        repository_a.create_project(
            name="Committed elsewhere",
            start_at=NOW,
            project_id="project-elsewhere",
        )
        stale_stage.create_project(
            name="Stale staged project",
            start_at=NOW,
            project_id="project-stale",
        )

        with pytest.raises(ServiceValidationError, match="changed after this transaction"):
            repository_b.replace_with(stale_stage)

        assert [project.project_id for project in repository_b.list_projects()] == [
            "project-elsewhere"
        ]
    finally:
        repository_a.close()
        repository_b.close()


def test_sqlite_direct_validation_error_does_not_dirty_projection(tmp_path: Path):
    db_path = tmp_path / "direct-error.sqlite"
    repository = SQLiteProjectRepository(db_path)
    repository.create_project(
        name="Direct SQLite Project",
        start_at=NOW,
        project_id="project-direct-error",
    )

    with pytest.raises(ServiceValidationError):
        repository.upsert_process_revision(
            project_id="project-direct-error",
            process_id="process-invalid",
            process_type="standard",
            name="Invalid",
            description="Invalid dependency.",
            effective_at=NOW,
            duration_business_days=1,
            dependencies=["process-missing"],
            earliest_start_at=None,
            start_at_earliest=False,
            delay_after_dependencies_business_days=0,
            required_roles={},
            role_requirements=[],
            assumption_note=None,
        )

    try:
        assert "process-invalid" not in repository.processes
    finally:
        repository.close()

    reopened = SQLiteProjectRepository(db_path)
    try:
        assert "process-invalid" not in reopened.processes
    finally:
        reopened.close()


def test_bootstrap_auto_storage_resolves_to_sqlite_and_rejects_other_storage(
    tmp_path: Path,
):
    assert bootstrap._resolve_storage("auto", tmp_path / "project.sqlite") == "sqlite"
    assert bootstrap._resolve_storage("sqlite", tmp_path / "project.sqlite") == "sqlite"
    with pytest.raises(ValueError, match="only supports SQLite"):
        bootstrap._resolve_storage("unsupported", tmp_path / "project.sqlite")


def test_ui_service_storage_resolver_only_supports_sqlite(tmp_path: Path):
    from projdash.ui import service_client

    assert bootstrap._resolve_storage("auto", tmp_path / "project.sqlite") == "sqlite"
    assert service_client._resolve_storage("auto", str(tmp_path / "project.sqlite")) == "sqlite"
    assert service_client._resolve_storage("sqlite", str(tmp_path / "project.sqlite")) == "sqlite"
    with pytest.raises(ValueError, match="only supports SQLite"):
        service_client._resolve_storage("unsupported", str(tmp_path / "project.sqlite"))


def test_local_state_databases_and_secrets_are_gitignored():
    gitignore = Path(".gitignore").read_text().splitlines()

    for pattern in (
        "data/",
        ".streamlit/secrets.toml",
        "*.sqlite",
        "*.sqlite-shm",
        "*.sqlite-wal",
        "*.db",
        "*.db-shm",
        "*.db-wal",
    ):
        assert pattern in gitignore


def test_sqlite_repository_round_trips_slack_service_state(tmp_path: Path):
    db_path = tmp_path / "slack.sqlite"
    repository = SQLiteProjectRepository(db_path)
    service = ProjectService(repository)
    project_id = _handle_service(
        service,
        {
            "action": "create_project",
            "name": "Slack SQLite Project",
            "start_at": NOW.isoformat(),
        },
    ).entity_ids["project_id"]
    role_id = _handle_service(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "name": "Engineer",
        },
    ).entity_ids["role_id"]
    calendar_id = _handle_service(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "name": "Default",
            "timezone": "UTC",
            "weekly_windows": [
                {
                    "window_id": "window-mon",
                    "weekday": 0,
                    "start_local_time": "09:00",
                    "end_local_time": "17:00",
                    "capacity_hours": 8,
                }
            ],
        },
    ).entity_ids["calendar_id"]
    resource_id = _handle_service(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "name": "Ada",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": NOW.isoformat(),
            "cost_rate": "100",
            "cost_unit": "hour",
        },
    ).entity_ids["resource_id"]
    _handle_service(
        service,
        {
            "action": "upsert_slack_project_config",
            "project_id": project_id,
            "enabled": True,
            "workspace_id": "T123",
            "workspace_name": "Example",
            "default_channel_id": "C123",
            "updated_at": NOW.isoformat(),
        },
    )
    _handle_service(
        service,
        {
            "action": "set_resource_slack_user",
            "project_id": project_id,
            "resource_id": resource_id,
            "slack_user_id": "U123",
            "display_name": "Ada Lovelace",
            "updated_at": NOW.isoformat(),
        },
    )
    _handle_service(
        service,
        {
            "action": "record_slack_collection_cursor",
            "project_id": project_id,
            "conversation_id": "D123",
            "conversation_type": "im",
            "conversation_name": "Ada",
            "latest_collected_ts": "1715600000.000100",
            "last_run_id": "run-1",
            "last_run_status": "success",
            "updated_at": NOW.isoformat(),
        },
    )
    _handle_service(
        service,
        {
            "action": "store_slack_bot_token",
            "project_id": project_id,
            "ciphertext": "encrypted-token",
            "kdf": "pbkdf2_hmac_sha256",
            "kdf_salt": "salt",
            "kdf_iterations": 390000,
            "cipher": "fernet",
            "updated_at": NOW.isoformat(),
        },
    )
    run_id = _handle_service(
        service,
        {
            "action": "start_slack_run",
            "project_id": project_id,
            "run_id": "run-1",
            "trigger": "ui",
            "codex_model": "gpt-5-codex",
            "started_at": NOW.isoformat(),
        },
    ).entity_ids["run_id"]
    outbox_id = _handle_service(
        service,
        {
            "action": "create_slack_outbox_messages",
            "project_id": project_id,
            "messages": [
                {
                    "resource_id": resource_id,
                    "slack_user_id": "U123",
                    "body": "Please confirm Slack status.",
                    "content_hash": "sha256:sqlite",
                    "run_id": run_id,
                    "created_at": NOW.isoformat(),
                }
            ],
        },
    ).entity_ids["created_outbox_ids"][0]
    channel_outbox_id = _handle_service(
        service,
        {
            "action": "create_slack_outbox_messages",
            "project_id": project_id,
            "messages": [
                {
                    "target_type": "channel",
                    "slack_channel_id": "C123",
                    "body": "Team channel status request.",
                    "content_hash": "sha256:sqlite-channel",
                    "run_id": run_id,
                    "created_at": NOW.isoformat(),
                }
            ],
        },
    ).entity_ids["created_outbox_ids"][0]
    _handle_service(
        service,
        {
            "action": "finish_slack_run",
            "project_id": project_id,
            "run_id": run_id,
            "status": "succeeded",
            "finished_at": NOW.isoformat(),
            "collected_message_count": 3,
            "draft_outbox_ids": [outbox_id, channel_outbox_id],
            "result_json": {"summary": "Prepared two drafts."},
        },
    )
    repository.close()

    reopened = SQLiteProjectRepository(db_path)
    reopened_service = ProjectService(reopened)
    try:
        slack = _query_service(
            reopened_service,
            {"action": "query_slack_project_config", "project_id": project_id},
        )
        assert slack["config"]["enabled"] is True
        assert slack["config"]["has_encrypted_bot_token"] is True
        assert slack["resource_mappings"][0]["slack_user_id"] == "U123"
        assert slack["collection_cursors"][0]["latest_collected_ts"] == (
            "1715600000.000100"
        )

        token = _query_service(
            reopened_service,
            {"action": "query_slack_bot_token", "project_id": project_id},
        )["encrypted_token"]
        assert token["ciphertext"] == "encrypted-token"

        runs = _query_service(
            reopened_service,
            {"action": "query_slack_runs", "project_id": project_id},
        )["runs"]
        assert runs[0]["status"] == "succeeded"
        assert runs[0]["draft_outbox_ids"] == [outbox_id, channel_outbox_id]

        outbox = _query_service(
            reopened_service,
            {
                "action": "query_slack_outbox",
                "project_id": project_id,
                "statuses": ["draft"],
            },
        )["outbox"]
        outbox_by_id = {row["outbox_id"]: row for row in outbox}
        assert outbox_by_id[outbox_id]["generated_body"] == (
            "Please confirm Slack status."
        )
        assert outbox_by_id[channel_outbox_id]["target_type"] == "channel"
        assert outbox_by_id[channel_outbox_id]["slack_channel_id"] == "C123"
    finally:
        reopened.close()


def _insert_raw_entity(
    conn: sqlite3.Connection,
    kind: str,
    entity_id: str,
    project_id: str,
    data: dict[str, Any],
    *,
    bypass_revision_role_trigger: bool = False,
) -> None:
    if bypass_revision_role_trigger:
        _drop_revision_role_requirement_triggers(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO repository_entity(
            kind, entity_id, project_id, payload_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            kind,
            entity_id,
            project_id,
            json.dumps({"data": data}, sort_keys=True),
            NOW.isoformat(),
        ),
    )


def _drop_revision_role_requirement_triggers(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER IF EXISTS repository_revision_role_requirements_one_insert")
    conn.execute("DROP TRIGGER IF EXISTS repository_revision_role_requirements_one_update")


def _seed_repository(repository):
    project_id = "project-sqlite"
    repository.create_project(
        name="SQLite Project",
        start_at=NOW,
        default_currency="USD",
        project_id=project_id,
    )
    repository.create_role(project_id, "Engineer", role_id="role-eng")
    repository.upsert_resource_calendar(
        project_id=project_id,
        calendar_id="calendar-default",
        name="Default",
        timezone="UTC",
        weekly_windows=[
            CalendarWeeklyWindowCommand(
                window_id="window-mon",
                weekday=0,
                start_local_time="09:00",
                end_local_time="17:00",
                capacity_hours=8,
            )
        ],
    )
    repository.upsert_resource(
        project_id=project_id,
        resource_id="resource-ada",
        name="Ada",
        resource_type="internal",
        role_ids=["role-eng"],
        calendar_id="calendar-default",
        available_from_at=NOW,
        cost_rate="100",
        cost_unit=CostUnit.HOUR,
    )
    repository.upsert_process_revision(
        project_id=project_id,
        process_id="process-build",
        process_type="standard",
        name="Build",
        description="Build the first version.",
        effective_at=NOW,
        duration_business_days=2,
        dependencies=[],
        earliest_start_at=None,
        start_at_earliest=False,
        delay_after_dependencies_business_days=0,
        required_roles={},
        role_requirements=[
            RoleRequirementCommand(
                requirement_id="req-build-eng",
                role_id="role-eng",
                effort_hours=8,
            )
        ],
        assumption_note=None,
    )
    repository.add_blocker(
        project_id=project_id,
        process_id="process-build",
        description="Waiting for vendor",
        opened_at=NOW,
        blocker_id="blocker-vendor",
    )
    return repository


def _handle_service(service: ProjectService, command: dict[str, Any]):
    result = service.handle_command(CommandEnvelope.model_validate({"command": command}))
    assert result.ok is True, getattr(result, "error", None)
    return result


def _query_service(service: ProjectService, query: dict[str, Any]):
    result = service.handle_query(QueryEnvelope.model_validate({"query": query}))
    assert result.ok is True, getattr(result, "error", None)
    return result.data
