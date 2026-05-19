import datetime as dt
from pathlib import Path
from typing import Any

from projdash.service.commands import CommandEnvelope
from projdash.service.ladybug_repository import LadybugProjectRepository
from projdash.service.queries import QueryEnvelope
from projdash.service.repository import InMemoryProjectRepository
from projdash.service.service import ProjectService
from projdash.service.slack_crypto import decrypt_slack_bot_token, encrypt_slack_bot_token

UTC = dt.UTC


def _at(day: int, hour: int = 9) -> dt.datetime:
    return dt.datetime(2026, 5, day, hour, tzinfo=UTC)


def _iso(day: int, hour: int = 9) -> str:
    return _at(day, hour).isoformat()


def _json_iso(day: int, hour: int = 9) -> str:
    return _iso(day, hour).replace("+00:00", "Z")


def _weekday_windows() -> list[dict[str, object]]:
    return [
        {
            "window_id": f"weekday-{weekday}",
            "weekday": weekday,
            "start_local_time": "09:00",
            "end_local_time": "17:00",
            "capacity_hours": 8,
        }
        for weekday in range(5)
    ]


def _handle(service: ProjectService, command: dict[str, Any]):
    result = service.handle_command(CommandEnvelope.model_validate({"command": command}))
    assert result.ok is True, getattr(result, "error", None)
    return result


def _query(service: ProjectService, query: dict[str, Any]):
    result = service.handle_query(QueryEnvelope.model_validate({"query": query}))
    assert result.ok is True, getattr(result, "error", None)
    return result.data


def _close(repository: LadybugProjectRepository) -> None:
    if not repository._conn.is_closed():
        repository._conn.close()
    if not repository._db.is_closed():
        repository._db.close()


def _create_project(service: ProjectService, name: str = "Slack Project") -> str:
    return _handle(
        service,
        {
            "action": "create_project",
            "name": name,
            "start_at": _iso(13),
        },
    ).entity_ids["project_id"]


def _create_resource(service: ProjectService, project_id: str) -> str:
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "name": "Engineer",
        },
    ).entity_ids["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "name": "Weekdays",
            "timezone": "UTC",
            "weekly_windows": _weekday_windows(),
        },
    ).entity_ids["calendar_id"]
    return _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "name": "Ada",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13),
            "cost_rate": "100",
            "cost_unit": "hour",
        },
    ).entity_ids["resource_id"]


def test_slack_config_defaults_to_disabled_and_can_be_upserted():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)

    default_data = _query(
        service,
        {"action": "query_slack_project_config", "project_id": project_id},
    )

    assert default_data["config"] == {
        "project_id": project_id,
        "enabled": False,
        "workspace_id": None,
        "workspace_name": None,
        "bot_token_secret_ref": None,
        "signing_secret_ref": None,
        "default_channel_id": None,
        "continuity_note": None,
        "continuity_updated_at": None,
        "updated_at": None,
        "has_encrypted_bot_token": False,
        "encrypted_bot_token_updated_at": None,
    }
    assert default_data["resource_mappings"] == []
    assert default_data["collection_cursors"] == []

    _handle(
        service,
        {
            "action": "upsert_slack_project_config",
            "project_id": project_id,
            "enabled": True,
            "workspace_id": "T123",
            "workspace_name": "Example",
            "bot_token_secret_ref": "secret/slack/bot",
            "signing_secret_ref": "secret/slack/signing",
            "default_channel_id": "C123",
            "updated_at": _iso(14),
        },
    )

    data = _query(
        service,
        {"action": "query_slack_project_config", "project_id": project_id},
    )
    assert data["config"]["enabled"] is True
    assert data["config"]["workspace_id"] == "T123"
    assert data["config"]["updated_at"] == _json_iso(14)

    _handle(
        service,
        {
            "action": "update_slack_continuity_note",
            "project_id": project_id,
            "continuity_note": "Check Ada by tomorrow morning.",
            "updated_at": _iso(15),
        },
    )
    updated = _query(
        service,
        {"action": "query_slack_project_config", "project_id": project_id},
    )
    assert updated["config"]["continuity_note"] == "Check Ada by tomorrow morning."
    assert updated["config"]["continuity_updated_at"] == _json_iso(15)


def test_slack_encrypted_token_storage_round_trips_without_plaintext():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    encrypted = encrypt_slack_bot_token(
        "xoxb-secret-token",
        "correct horse battery staple",
        salt=b"0123456789abcdef",
        kdf_iterations=1_200,
    )

    _handle(
        service,
        {
            "action": "store_slack_bot_token",
            "project_id": project_id,
            **encrypted,
            "updated_at": _iso(14),
        },
    )

    config = _query(
        service,
        {"action": "query_slack_project_config", "project_id": project_id},
    )
    assert config["config"]["has_encrypted_bot_token"] is True
    assert config["config"]["encrypted_bot_token_updated_at"] == _json_iso(14)
    assert "xoxb-secret-token" not in str(config)

    token = _query(
        service,
        {"action": "query_slack_bot_token", "project_id": project_id},
    )["encrypted_token"]
    assert token["ciphertext"] != "xoxb-secret-token"
    assert "xoxb-secret-token" not in str(token)
    assert decrypt_slack_bot_token(token, "correct horse battery staple") == (
        "xoxb-secret-token"
    )

    _handle(
        service,
        {
            "action": "clear_slack_bot_token",
            "project_id": project_id,
            "cleared_at": _iso(15),
        },
    )
    assert _query(
        service,
        {"action": "query_slack_bot_token", "project_id": project_id},
    )["encrypted_token"] is None


def test_slack_run_records_enforce_one_active_run_per_project():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)

    run_id = _handle(
        service,
        {
            "action": "start_slack_run",
            "project_id": project_id,
            "run_id": "run-1",
            "trigger": "ui",
            "codex_model": "gpt-5-codex",
            "started_at": _iso(14),
        },
    ).entity_ids["run_id"]
    assert run_id == "run-1"

    duplicate = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "start_slack_run",
                    "project_id": project_id,
                    "run_id": "run-2",
                    "started_at": _iso(14, 10),
                }
            }
        )
    )
    assert duplicate.ok is False
    assert duplicate.error.code == "slack_run_already_active"

    _handle(
        service,
        {
            "action": "finish_slack_run",
            "project_id": project_id,
            "run_id": run_id,
            "status": "no_new_data",
            "finished_at": _iso(14, 11),
            "collected_message_count": 0,
            "result_json": {"summary": "No new Slack evidence."},
        },
    )
    _handle(
        service,
        {
            "action": "start_slack_run",
            "project_id": project_id,
            "run_id": "run-2",
            "started_at": _iso(15),
        },
    )

    runs = _query(
        service,
        {"action": "query_slack_runs", "project_id": project_id},
    )["runs"]
    assert [run["run_id"] for run in runs] == ["run-2", "run-1"]
    assert runs[1]["status"] == "no_new_data"


def test_resource_slack_mapping_validates_project_ownership_and_can_clear():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    other_project_id = _create_project(service, "Other")
    resource_id = _create_resource(service, project_id)

    cross_project = service.handle_command(
        CommandEnvelope.model_validate(
            {
                "command": {
                    "action": "set_resource_slack_user",
                    "project_id": other_project_id,
                    "resource_id": resource_id,
                    "slack_user_id": "U123",
                    "updated_at": _iso(14),
                }
            }
        )
    )
    assert cross_project.ok is False
    assert cross_project.error.code == "cross_project_resource"

    _handle(
        service,
        {
            "action": "set_resource_slack_user",
            "project_id": project_id,
            "resource_id": resource_id,
            "slack_user_id": "U123",
            "display_name": "Ada Lovelace",
            "updated_at": _iso(14),
        },
    )
    _handle(
        service,
        {
            "action": "set_resource_slack_user",
            "project_id": project_id,
            "resource_id": resource_id,
            "slack_user_id": None,
            "active": False,
            "updated_at": _iso(15),
        },
    )

    data = _query(
        service,
        {"action": "query_slack_project_config", "project_id": project_id},
    )
    assert data["resource_mappings"] == [
        {
            "project_id": project_id,
            "resource_id": resource_id,
            "slack_user_id": None,
            "display_name": None,
            "active": False,
            "updated_at": _json_iso(15),
        }
    ]


def test_slack_cursors_and_outbox_dedupe_and_status_transitions():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    resource_id = _create_resource(service, project_id)

    _handle(
        service,
        {
            "action": "record_slack_collection_cursor",
            "project_id": project_id,
            "conversation_id": "C123",
            "conversation_type": "channel",
            "conversation_name": "proj-alpha",
            "latest_collected_ts": "1715600000.000100",
            "last_run_id": "run-1",
            "last_run_status": "success",
            "updated_at": _iso(14),
            "rate_limited_until_at": _iso(14, 10),
        },
    )

    first = _handle(
        service,
        {
            "action": "create_slack_outbox_messages",
            "project_id": project_id,
            "messages": [
                {
                    "resource_id": resource_id,
                    "slack_user_id": "U123",
                    "body": "Please post a status update.",
                    "content_hash": "sha256:abc",
                    "run_id": "run-1",
                    "created_at": _iso(14),
                }
            ],
        },
    )
    outbox_id = first.entity_ids["created_outbox_ids"][0]

    replay = _handle(
        service,
        {
            "action": "create_slack_outbox_messages",
            "project_id": project_id,
            "messages": [
                {
                    "resource_id": resource_id,
                    "slack_user_id": "U123",
                    "body": "Please post a status update.",
                    "content_hash": "sha256:abc",
                    "run_id": "run-1",
                    "created_at": _iso(14),
                }
            ],
        },
    )
    assert replay.entity_ids["created_outbox_ids"] == []
    assert replay.entity_ids["matched_outbox_ids"] == [outbox_id]
    assert replay.entity_ids["skipped_outbox_ids"] == [outbox_id]

    pending = _query(
        service,
        {"action": "query_pending_slack_outbox", "project_id": project_id},
    )
    assert [row["outbox_id"] for row in pending["outbox"]] == [outbox_id]
    assert pending["outbox"][0]["status"] == "draft"
    assert pending["outbox"][0]["generated_body"] == "Please post a status update."

    _handle(
        service,
        {
            "action": "update_slack_outbox_body",
            "project_id": project_id,
            "outbox_id": outbox_id,
            "body": "Edited status prompt.",
            "updated_at": _iso(14, 10),
        },
    )
    edited = _query(
        service,
        {
            "action": "query_slack_outbox",
            "project_id": project_id,
            "statuses": ["draft"],
        },
    )["outbox"][0]
    assert edited["body"] == "Edited status prompt."
    assert edited["generated_body"] == "Please post a status update."
    assert edited["edited_at"] == _json_iso(14, 10)

    _handle(
        service,
        {
            "action": "mark_slack_outbox_failed",
            "project_id": project_id,
            "outbox_id": outbox_id,
            "failed_at": _iso(14, 11),
            "error_text": "Slack API timeout",
            "run_id": "run-1",
        },
    )
    assert _query(
        service,
        {
            "action": "query_pending_slack_outbox",
            "project_id": project_id,
            "statuses": ["draft"],
        },
    )["outbox"] == []

    _handle(
        service,
        {
            "action": "mark_slack_outbox_sent",
            "project_id": project_id,
            "outbox_id": outbox_id,
            "sent_at": _iso(14, 12),
            "slack_channel_id": "D123",
            "slack_message_ts": "1715600100.000200",
            "run_id": "run-2",
        },
    )
    sent = _query(
        service,
        {
            "action": "query_pending_slack_outbox",
            "project_id": project_id,
            "statuses": ["sent"],
        },
    )["outbox"]
    assert sent[0]["sent_at"] == _json_iso(14, 12)
    assert sent[0]["error_text"] is None

    second = _handle(
        service,
        {
            "action": "create_slack_outbox_messages",
            "project_id": project_id,
            "messages": [
                {
                    "resource_id": resource_id,
                    "slack_user_id": "U123",
                    "body": "No longer needed.",
                    "content_hash": "sha256:def",
                    "run_id": "run-3",
                    "created_at": _iso(15),
                }
            ],
        },
    ).entity_ids["created_outbox_ids"][0]
    _handle(
        service,
        {
            "action": "mark_slack_outbox_skipped",
            "project_id": project_id,
            "outbox_id": second,
            "skipped_at": _iso(15, 10),
            "reason": "Operator chose not to send.",
        },
    )
    skipped = _query(
        service,
        {
            "action": "query_slack_outbox",
            "project_id": project_id,
            "statuses": ["skipped"],
        },
    )["outbox"][0]
    assert skipped["skipped_at"] == _json_iso(15, 10)
    assert skipped["skip_reason"] == "Operator chose not to send."

    config = _query(
        service,
        {"action": "query_slack_project_config", "project_id": project_id},
    )
    assert config["collection_cursors"][0]["conversation_id"] == "C123"


def test_slack_state_round_trips_through_ladybugdb(tmp_path: Path):
    db_path = tmp_path / "slack.lbug"
    repository = LadybugProjectRepository(db_path)
    service = ProjectService(repository)
    project_id = _create_project(service)
    resource_id = _create_resource(service, project_id)

    _handle(
        service,
        {
            "action": "upsert_slack_project_config",
            "project_id": project_id,
            "enabled": False,
            "workspace_id": "T123",
            "updated_at": _iso(14),
        },
    )
    encrypted = encrypt_slack_bot_token(
        "xoxb-db-token",
        "roundtrip passphrase",
        salt=b"fedcba9876543210",
        kdf_iterations=1_200,
    )
    _handle(
        service,
        {
            "action": "store_slack_bot_token",
            "project_id": project_id,
            **encrypted,
            "updated_at": _iso(14, 1),
        },
    )
    _handle(
        service,
        {
            "action": "start_slack_run",
            "project_id": project_id,
            "run_id": "run-1",
            "started_at": _iso(14, 2),
        },
    )
    _handle(
        service,
        {
            "action": "set_resource_slack_user",
            "project_id": project_id,
            "resource_id": resource_id,
            "slack_user_id": "U123",
            "updated_at": _iso(14),
        },
    )
    _handle(
        service,
        {
            "action": "record_slack_collection_cursor",
            "project_id": project_id,
            "conversation_id": "C123",
            "conversation_type": "channel",
            "conversation_name": "proj-alpha",
            "latest_collected_ts": "1715600000.000100",
            "last_run_id": "run-1",
            "last_run_status": "success",
            "updated_at": _iso(14),
        },
    )
    outbox_id = _handle(
        service,
        {
            "action": "create_slack_outbox_messages",
            "project_id": project_id,
            "messages": [
                {
                    "resource_id": resource_id,
                    "slack_user_id": "U123",
                    "body": "Please post a status update.",
                    "generated_body": "Generated status request.",
                    "content_hash": "sha256:abc",
                    "run_id": "run-1",
                    "created_at": _iso(14),
                }
            ],
        },
    ).entity_ids["created_outbox_ids"][0]
    _handle(
        service,
        {
            "action": "update_slack_outbox_body",
            "project_id": project_id,
            "outbox_id": outbox_id,
            "body": "Edited durable status update.",
            "updated_at": _iso(14, 3),
        },
    )
    _handle(
        service,
        {
            "action": "finish_slack_run",
            "project_id": project_id,
            "run_id": "run-1",
            "status": "succeeded",
            "finished_at": _iso(14, 4),
            "collected_message_count": 3,
            "draft_outbox_ids": [outbox_id],
            "result_json": {"draft_count": 1},
        },
    )

    _close(repository)
    reopened = LadybugProjectRepository(db_path)
    try:
        reopened_service = ProjectService(reopened)
        config = _query(
            reopened_service,
            {"action": "query_slack_project_config", "project_id": project_id},
        )
        assert config["config"]["workspace_id"] == "T123"
        assert config["config"]["has_encrypted_bot_token"] is True
        token = _query(
            reopened_service,
            {"action": "query_slack_bot_token", "project_id": project_id},
        )["encrypted_token"]
        assert decrypt_slack_bot_token(token, "roundtrip passphrase") == "xoxb-db-token"
        assert config["resource_mappings"][0]["slack_user_id"] == "U123"
        assert config["collection_cursors"][0]["latest_collected_ts"] == (
            "1715600000.000100"
        )
        runs = _query(
            reopened_service,
            {"action": "query_slack_runs", "project_id": project_id},
        )["runs"]
        assert runs[0]["run_id"] == "run-1"
        assert runs[0]["status"] == "succeeded"
        assert runs[0]["result_json"] == {"draft_count": 1}

        outbox = _query(
            reopened_service,
            {"action": "query_pending_slack_outbox", "project_id": project_id},
        )["outbox"]
        assert [row["outbox_id"] for row in outbox] == [outbox_id]
        assert outbox[0]["content_hash"] == "sha256:abc"
        assert outbox[0]["body"] == "Edited durable status update."
        assert outbox[0]["generated_body"] == "Generated status request."
        assert outbox[0]["edited_at"] == _json_iso(14, 3)
    finally:
        _close(reopened)
