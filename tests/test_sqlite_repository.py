import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import pytest

from projdash.service import bootstrap
from projdash.service.commands import CommandEnvelope
from projdash.service.errors import ServiceValidationError
from projdash.service.ladybug_repository import LadybugProjectRepository
from projdash.service.models import (
    CalendarWeeklyWindowCommand,
    CostUnit,
    RoleRequirementCommand,
)
from projdash.service.queries import QueryEnvelope
from projdash.service.results import CommandResult
from projdash.service.service import ProjectService
from projdash.service.sqlite_repository import (
    SQLiteProjectRepository,
    migrate_ladybug_to_sqlite,
)

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
            staked_resource_ids=[],
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


def test_bootstrap_auto_storage_follows_suffix_and_rejects_mismatch(tmp_path: Path):
    assert bootstrap._resolve_storage("auto", tmp_path / "project.lbug") == "ladybug"
    assert bootstrap._resolve_storage("auto", tmp_path / "project.sqlite") == "sqlite"
    with pytest.raises(ValueError, match="SQLite storage at a .lbug path"):
        bootstrap._resolve_storage("sqlite", tmp_path / "project.lbug")
    with pytest.raises(ValueError, match="Ladybug storage at a SQLite path"):
        bootstrap._resolve_storage("ladybug", tmp_path / "project.sqlite")


def test_migrate_ladybug_to_sqlite_leaves_source_readable(tmp_path: Path):
    pytest.importorskip("real_ladybug")
    source_path = tmp_path / "source.lbug"
    target_path = tmp_path / "target.sqlite"
    source = LadybugProjectRepository(source_path)
    staged = _seed_repository(source.clone())
    source.replace_with(staged)
    _close_ladybug(source)

    summary = migrate_ladybug_to_sqlite(source_path, target_path)

    assert source_path.exists()
    assert target_path.exists()
    assert summary["projects"] == 1
    assert summary["processes"] == 1

    reopened_source = LadybugProjectRepository(source_path)
    target = SQLiteProjectRepository(target_path)
    try:
        assert reopened_source.get_project("project-sqlite").name == "SQLite Project"
        assert target.get_project("project-sqlite").name == "SQLite Project"
        assert target.resources["resource-ada"]["calendar_id"] == "calendar-default"
    finally:
        _close_ladybug(reopened_source)
        target.close()


def test_migrate_ladybug_to_sqlite_skips_existing_target(tmp_path: Path):
    pytest.importorskip("real_ladybug")
    source_path = tmp_path / "source.lbug"
    target_path = tmp_path / "target.sqlite"
    source = LadybugProjectRepository(source_path)
    source.replace_with(_seed_repository(source.clone()))
    _close_ladybug(source)
    target_path.write_bytes(b"already here")

    summary = migrate_ladybug_to_sqlite(source_path, target_path)

    assert summary.migrated is False
    assert summary.skipped_reason == "target_not_empty"
    assert target_path.read_bytes() == b"already here"


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
    _handle_service(
        service,
        {
            "action": "finish_slack_run",
            "project_id": project_id,
            "run_id": run_id,
            "status": "succeeded",
            "finished_at": NOW.isoformat(),
            "collected_message_count": 3,
            "draft_outbox_ids": [outbox_id],
            "result_json": {"summary": "Prepared one draft."},
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
        assert runs[0]["draft_outbox_ids"] == [outbox_id]

        outbox = _query_service(
            reopened_service,
            {
                "action": "query_slack_outbox",
                "project_id": project_id,
                "statuses": ["draft"],
            },
        )["outbox"]
        assert outbox[0]["outbox_id"] == outbox_id
        assert outbox[0]["generated_body"] == "Please confirm Slack status."
    finally:
        reopened.close()


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
        role_ids=["role-eng"],
        calendar_id="calendar-default",
        available_from_at=NOW,
        cost_rate="100",
        cost_unit=CostUnit.HOUR,
    )
    repository.upsert_process_revision(
        project_id=project_id,
        process_id="process-build",
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
        staked_resource_ids=[],
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


def _close_ladybug(repository: LadybugProjectRepository) -> None:
    if not repository._conn.is_closed():
        repository._conn.close()
    if not repository._db.is_closed():
        repository._db.close()
