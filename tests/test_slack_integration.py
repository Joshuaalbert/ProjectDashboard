import datetime as dt
import hashlib
import json

import pytest
from pydantic import ValidationError

from projdash.integrations import slack_bot
from projdash.service.commands import CommandEnvelope
from projdash.service.queries import QueryEnvelope
from projdash.service.repository import InMemoryProjectRepository
from projdash.service.service import ProjectService

UTC = dt.UTC


def _at(hour: int = 9) -> dt.datetime:
    return dt.datetime(2026, 5, 19, hour, tzinfo=UTC)


class FakeService:
    def __init__(self, config=None, pending=None) -> None:
        self.config = config
        self.pending = list(pending or [])
        self.cursors = []
        self.created_messages = []
        self.sent = []
        self.failed = []
        self.continuity_notes = []
        self.pm_agent_context = (
            "# PM agent context\n\n"
            "- [evidence:slack-blocker] Is Ada blocked on credentials?\n"
        )
        self.pm_evidence_line_items = [
            {
                "evidence_line_id": "evidence:slack-blocker",
                "question": "Is Ada blocked on credentials?",
            }
        ]

    def query_slack_project_config(self, project_id):
        assert project_id == "project-a"
        return self.config

    def query_agent_context(self, project_id, as_of=None, now=None):
        assert project_id == "project-a"
        return {"summary": "Current project context", "as_of": as_of.isoformat()}

    def query_pm_markdown_context(self, project_id, as_of=None, now=None, **kwargs):
        assert project_id == "project-a"
        return {
            "markdown": self.pm_agent_context,
            "agent_context": {"summary": "Current project context"},
            "evidence_line_items": self.pm_evidence_line_items,
            "generated_at": now.isoformat(),
        }

    def query_slack_outbox(self, project_id, statuses=None, limit=None):
        assert project_id == "project-a"
        rows = list(self.pending)
        if statuses is not None:
            status_values = {str(status) for status in statuses}
            rows = [
                row
                for row in rows
                if str(row.get("status", "draft")) in status_values
            ]
        return {"outbox": rows[:limit] if limit is not None else rows}

    def record_slack_collection_cursor(self, **kwargs):
        self.cursors.append(kwargs)
        return {"ok": True}

    def create_slack_outbox_messages(self, **kwargs):
        self.created_messages.append(kwargs)
        self.pending.extend(
            {
                "outbox_id": f"outbox-{index}",
                "target_type": message.get("target_type", "dm"),
                "resource_id": message.get("resource_id"),
                "slack_user_id": message.get("slack_user_id"),
                "slack_channel_id": message.get("slack_channel_id"),
                "body": message["body"],
                "blocks": message.get("blocks", []),
                "content_hash": message.get("content_hash"),
                "run_id": message.get("run_id"),
                "status": "draft",
                "pm_evidence_claims": message.get("pm_evidence_claims", []),
            }
            for index, message in enumerate(kwargs["messages"], start=1)
        )
        return {"ok": True}

    def update_slack_continuity_note(self, **kwargs):
        self.continuity_notes.append(kwargs)
        return {"ok": True}

    def query_pending_slack_outbox(self, project_id):
        assert project_id == "project-a"
        return {"messages": list(self.pending)}

    def mark_slack_outbox_sent(self, **kwargs):
        self.sent.append(kwargs)
        return {"ok": True}

    def mark_slack_outbox_failed(self, **kwargs):
        self.failed.append(kwargs)
        return {"ok": True}


class FakeSlackClient:
    def __init__(self) -> None:
        self.posts = []
        self.opened_dms = []
        self.history_calls = []
        self.reply_calls = []
        self.auth_test_called = False
        self.conversations_list_calls = []

    def auth_test(self):
        self.auth_test_called = True
        return {"ok": True, "user_id": "UAPP"}

    def users_info(self, user):
        return {"ok": True, "user": {"id": user, "name": "teammate"}}

    def conversations_list(self, **kwargs):
        self.conversations_list_calls.append(kwargs)
        return {
            "ok": True,
            "channels": [
                {
                    "id": "C1",
                    "name": "general",
                    "is_channel": True,
                    "is_member": True,
                },
                {
                    "id": "C2",
                    "name": "outside",
                    "is_channel": True,
                    "is_member": False,
                },
                {"id": "D1", "is_im": True, "user": "U1"},
            ],
        }

    def users_list(self, **kwargs):
        return {
            "ok": True,
            "members": [
                {
                    "id": "U1",
                    "name": "ada",
                    "real_name": "Ada Lovelace",
                    "tz": "Europe/London",
                    "profile": {"display_name": "Ada"},
                },
                {
                    "id": "UAPP",
                    "name": "app",
                    "is_app_user": True,
                    "profile": {"display_name": "ProjDash"},
                },
                {
                    "id": "UDEL",
                    "name": "deleted",
                    "deleted": True,
                    "profile": {"display_name": "Deleted"},
                },
            ],
        }

    def conversations_history(self, **kwargs):
        self.history_calls.append(kwargs)
        channel = kwargs["channel"]
        if channel == "C1":
            return {
                "ok": True,
                "messages": [
                    {
                        "type": "message",
                        "user": "U1",
                        "text": "Need draft copy by Friday",
                        "ts": "1779181200.000100",
                        "thread_ts": "1779181200.000100",
                        "reply_count": 1,
                    }
                ],
            }
        if channel == "D1":
            return {
                "ok": True,
                "messages": [
                    {
                        "type": "message",
                        "user": "U1",
                        "text": "I am blocked on credentials",
                        "ts": "1779184800.000200",
                    }
                ],
            }
        raise AssertionError(f"unexpected history channel {channel}")

    def conversations_replies(self, **kwargs):
        self.reply_calls.append(kwargs)
        return {
            "ok": True,
            "messages": [
                {
                    "type": "message",
                    "user": "U1",
                    "text": "Need draft copy by Friday",
                    "ts": "1779181200.000100",
                    "thread_ts": "1779181200.000100",
                },
                {
                    "type": "message",
                    "user": "U2",
                    "text": "Copy review is queued",
                    "ts": "1779181800.000300",
                    "thread_ts": "1779181200.000100",
                },
            ],
        }

    def conversations_open(self, **kwargs):
        self.opened_dms.append(kwargs)
        return {"ok": True, "channel": {"id": f"D-{kwargs['users']}"}}

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1779189000.000400", "channel": kwargs["channel"]}


class FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class FakeSubprocessRunner:
    def __init__(self, stdout: str | list[str], returncode: int = 0) -> None:
        self.stdouts = list(stdout) if isinstance(stdout, list) else [stdout]
        self.returncode = returncode
        self.calls = []

    def run(self, args, *, input, text, capture_output, check, env=None):
        call_index = len(self.calls)
        self.calls.append(
            {
                "args": args,
                "input": input,
                "text": text,
                "capture_output": capture_output,
                "check": check,
                "env": env,
            }
        )
        stdout = self.stdouts[min(call_index, len(self.stdouts) - 1)]
        return FakeCompletedProcess(stdout, returncode=self.returncode)


def _config(enabled: bool = True):
    return {
        "project_id": "project-a",
        "enabled": enabled,
        "token_env_var": "SLACK_BOT_TOKEN",
        "start_at": _at().isoformat(),
        "channels": [{"channel_id": "C1", "name": "general"}],
        "resource_slack_map": [
            {
                "resource_id": "res-1",
                "teammate_id": "tm-1",
                "slack_user_id": "U1",
                "name": "Ada",
            }
        ],
        "conversation_cursors": {},
    }


def _continuity_note(
    summary: str = "Structured continuity handoff.",
    *,
    teammates: list[dict[str, str]] | None = None,
) -> dict:
    del teammates
    return {
        "schema_version": 3,
        "summary": summary,
        "generated_at": _at(10).isoformat(),
        "note": "Next run should check expected replies and current blockers.",
        "next_run_focus": ["Check expected replies and channel context requests."],
    }


def _simple_continuity_note(summary: str = "Simple continuity handoff.") -> dict:
    return {
        "schema_version": 3,
        "summary": summary,
        "generated_at": _at(10).isoformat(),
        "note": "Next run should check whether Ada's credential blocker moved.",
        "next_run_focus": ["Review Ada's blocker reply."],
    }


def _evidence_line_answers() -> list[dict]:
    return [
        {
            "evidence_line_id": "evidence:slack-blocker",
            "question": "Is Ada blocked on credentials?",
            "answer": "Yes",
            "reason": "Ada said she is blocked on credentials.",
            "outcome": "Credential access is blocking Ada's current task.",
            "update_intent": "Track blocker and ask for access update.",
            "source_keys": ["D1:1779184800.000200"],
        }
    ]


def _service_evidence_line_answers(
    service: ProjectService,
    project_id: str,
) -> list[dict]:
    context = _query(
        service,
        {
            "action": "query_pm_markdown_context",
            "project_id": project_id,
            "as_of": _at(10).isoformat(),
            "now": _at(10).isoformat(),
        },
    )
    return [
        {
            "evidence_line_id": item["evidence_line_id"],
            "question": item.get("question") or "",
            "answer": "No",
            "reason": "No new Slack or manual evidence addressed this line item.",
            "outcome": "No project state change.",
            "update_intent": "No evidence update.",
            "source_keys": [],
        }
        for item in context["evidence_line_items"]
    ]


def _reviewer_notes() -> list[dict]:
    return [
        {
            "reviewer": "pm-flow-review",
            "status": "approved",
            "note": "Evidence answers, message decisions, and continuity are aligned.",
            "required_changes": [],
        }
    ]


def _fake_message_ack_claims() -> tuple[list[dict], list[dict]]:
    assignment_hash = "sha256:" + hashlib.sha256(
        json.dumps(
            {"resource_id": "res-1", "assigned_processes": []},
            sort_keys=True,
            default=str,
        ).encode("utf-8"),
    ).hexdigest()
    dm_claims = [
        {
            "evidence_type": "resource_assignment_review",
            "resource_id": "res-1",
            "obligation_id": "resource_assignment_review:res-1",
            "content_hash": assignment_hash,
        },
        {
            "evidence_type": "message_receipt_ack",
            "resource_id": "res-1",
            "obligation_id": "message_receipt_ack:D1:1779184800.000200",
        },
    ]
    channel_claims = [
        {
            "evidence_type": "message_receipt_ack",
            "obligation_id": "message_receipt_ack:C1:1779181200.000100",
        },
        {
            "evidence_type": "message_receipt_ack",
            "obligation_id": "message_receipt_ack:C1:1779181800.000300",
        },
    ]
    return dm_claims, channel_claims


def _empty_assignment_review_blocks() -> list[dict]:
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Tasks for Ada"}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Current process work list for `res-1`",
                }
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "No current or upcoming process work.",
            },
        },
    ]


def _empty_assignment_review_markdown(extra: str) -> str:
    return (
        "# Tasks for Ada\n\n"
        "Current process work list for `res-1`\n\n"
        "No current or upcoming process work."
        f"\n\n{extra}"
    )


def _handle(service: ProjectService, command: dict):
    result = service.handle_command(CommandEnvelope.model_validate({"command": command}))
    assert result.ok is True, getattr(result, "error", None)
    return result


def _query(service: ProjectService, query: dict):
    result = service.handle_query(QueryEnvelope.model_validate({"query": query}))
    assert result.ok is True, getattr(result, "error", None)
    return result.data


def _seed_service_with_slack_config() -> tuple[ProjectService, str]:
    service = ProjectService(InMemoryProjectRepository())
    project_id = _handle(
        service,
        {
            "action": "create_project",
            "project_id": "project-a",
            "name": "Project A",
            "start_at": _at(8).isoformat(),
        },
    ).entity_ids["project_id"]
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-eng",
            "name": "Engineer",
        },
    ).entity_ids["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "cal-weekdays",
            "name": "Weekdays",
            "timezone": "UTC",
            "weekly_windows": [
                {
                    "window_id": f"weekday-{weekday}",
                    "weekday": weekday,
                    "start_local_time": "09:00",
                    "end_local_time": "17:00",
                    "capacity_hours": 8,
                }
                for weekday in range(5)
            ],
        },
    ).entity_ids["calendar_id"]
    resource_id = _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "res-1",
            "name": "Ada",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _at(8).isoformat(),
            "cost_rate": "100",
            "cost_unit": "hour",
        },
    ).entity_ids["resource_id"]
    _handle(
        service,
        {
            "action": "upsert_slack_project_config",
            "project_id": project_id,
            "enabled": True,
            "workspace_id": "T1",
            "workspace_name": "Test",
            "bot_token_secret_ref": "SLACK_BOT_TOKEN",
            "default_channel_id": "C1",
            "updated_at": _at(9).isoformat(),
        },
    )
    _handle(
        service,
        {
            "action": "set_resource_slack_user",
            "project_id": project_id,
            "resource_id": resource_id,
            "slack_user_id": "U1",
            "display_name": "Ada",
            "updated_at": _at(9).isoformat(),
        },
    )
    return service, project_id


@pytest.mark.parametrize("config", [None, _config(enabled=False)])
def test_run_once_noops_when_slack_config_is_missing_or_disabled(config, capsys, tmp_path):
    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=FakeService(config=config),
        slack_client_factory=lambda token: pytest.fail("Slack client should not be created"),
        subprocess_runner=FakeSubprocessRunner("{}"),
        now=_at(),
    )

    assert result.exit_code == 0
    assert result.noop is True
    assert "No enabled Slack config" in capsys.readouterr().out


def test_verify_fails_clearly_when_token_env_var_is_missing(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    result = slack_bot.verify(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        service=FakeService(config=_config()),
        slack_client_factory=lambda token: pytest.fail("Slack client should not be created"),
    )

    assert result.exit_code == 1
    assert "SLACK_BOT_TOKEN" in capsys.readouterr().err


def test_verify_accepts_token_override_without_env_config(monkeypatch, tmp_path):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    slack_client = FakeSlackClient()
    seen_tokens = []
    config = {**_config(), "token_env_var": None}

    result = slack_bot.verify(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        token_override="xoxb-ui",
        service=FakeService(config=config),
        slack_client_factory=lambda token: seen_tokens.append(token) or slack_client,
    )

    assert result.exit_code == 0
    assert seen_tokens == ["xoxb-ui"]
    assert slack_client.auth_test_called is True


def test_list_slack_users_filters_deleted_and_app_users(monkeypatch, tmp_path):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    slack_client = FakeSlackClient()

    users = slack_bot.list_slack_users(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        token_override="xoxb-ui",
        service=FakeService(config={**_config(), "token_env_var": None}),
        slack_client_factory=lambda token: slack_client,
    )

    assert [user.as_dict() for user in users] == [
        {
            "slack_user_id": "U1",
            "name": "ada",
            "real_name": "Ada Lovelace",
            "display_name": "Ada",
            "email": None,
            "timezone": "Europe/London",
            "deleted": False,
            "is_bot": False,
            "is_app_user": False,
        }
    ]


def test_run_once_collects_messages_invokes_codex_persists_and_sends(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    slack_client = FakeSlackClient()
    service = FakeService(config=_config())
    service.pending.append(
        {
            "outbox_id": "old-outbox",
            "resource_id": "res-1",
            "slack_user_id": "U1",
            "body": "Older pending draft.",
            "blocks": [],
            "run_id": "old-run",
            "status": "draft",
        }
    )
    dm_markdown = "# Credential blocker\n\nI saw the credential blocker and will track it."
    channel_markdown = (
        "Credentials are blocking Ada's current task. Please share the latest "
        "access update here."
    )
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "evidence_line_answers": _evidence_line_answers(),
                "reviewer_notes": _reviewer_notes(),
                "continuity_note": _simple_continuity_note(
                    "Next run should confirm Ada saw the blocker update.",
                ),
                "draft_messages": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                        "message_markdown": dm_markdown,
                        "reason": "Follow up on Slack blocker.",
                        "source_message_keys": ["D1:1779184800.000200"],
                    }
                ],
                "team_channel_draft_messages": [
                    {
                        "channel_id": "C1",
                        "channel_name": "general",
                        "message_markdown": channel_markdown,
                        "reason": "Team-visible coordination is needed.",
                        "source_message_keys": ["D1:1779184800.000200"],
                    }
                ],
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=service,
        slack_client_factory=lambda token: slack_client,
        subprocess_runner=runner,
        codex_bin="codex-test",
        now=_at(10),
        run_id="run-1",
    )

    run_dir = tmp_path / "data" / "project-a" / "unreconciled" / "slack" / "run-1"
    reconciled_dir = tmp_path / "data" / "project-a" / "reconciled" / "slack" / "run-1"
    assert result.exit_code == 0
    assert not run_dir.exists()
    assert (reconciled_dir / "raw_messages.jsonl").exists()
    assert (reconciled_dir / "pm_agent_context.md").read_text() == (
        service.pm_agent_context.strip()
    )
    assert (reconciled_dir / "unsent_outbox.json").exists()
    assert not (reconciled_dir / "outbound_history.json").exists()
    assert "Need draft copy by Friday" in (reconciled_dir / "messages.md").read_text()
    manifest = json.loads((reconciled_dir / "collection_manifest.json").read_text())
    assert manifest["project_id"] == "project-a"
    assert manifest["message_count"] == 3
    assert runner.calls[0]["args"][:2] == ["codex-test", "exec"]
    assert "--ask-for-approval" not in runner.calls[0]["args"]
    assert "-a" not in runner.calls[0]["args"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in runner.calls[0]["args"]
    assert "-C" in runner.calls[0]["args"]
    sandbox_index = runner.calls[0]["args"].index("-s")
    assert runner.calls[0]["args"][sandbox_index + 1] == "danger-full-access"
    assert "SLACK_BOT_TOKEN" not in runner.calls[0]["env"]
    assert "$projdash-pm-flow from" in runner.calls[0]["input"]
    assert "$projdash-project-manager only for" in runner.calls[0]["input"]
    assert "unsent_outbox.json" in runner.calls[0]["input"]
    assert "outbound_history.json" not in runner.calls[0]["input"]
    assert "do not mine past sent, failed, or skipped outbound messages" in (
        runner.calls[0]["input"]
    )
    assert "PM markdown context path:" in runner.calls[0]["input"]
    assert "Read these inputs first: pm_agent_context.md" in runner.calls[0]["input"]
    assert "Procedure:" in runner.calls[0]["input"]
    assert "Optional corrective cycle" in runner.calls[0]["input"]
    assert "evidence_line_answers" in runner.calls[0]["input"]
    assert "reviewer_notes" in runner.calls[0]["input"]
    assert "team_channel_draft_messages" in runner.calls[0]["input"]
    assert "Review: run a reviewer pass" in runner.calls[0]["input"]
    assert service.created_messages[0]["messages"][0]["body"] == dm_markdown
    assert service.created_messages[0]["messages"][0]["content_hash"].startswith(
        "sha256:"
    )
    assert service.created_messages[0]["messages"][0]["blocks"] == [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Credential blocker"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "I saw the credential blocker and will track it.",
            },
        },
    ]
    assert service.created_messages[0]["messages"][0][
        "content_hash"
    ] == slack_bot._message_content_hash(  # noqa: SLF001 - assert integration hashing.
        dm_markdown,
        service.created_messages[0]["messages"][0]["blocks"],
    )
    assert service.created_messages[0]["messages"][1]["target_type"] == "channel"
    assert service.created_messages[0]["messages"][1]["slack_channel_id"] == "C1"
    assert service.created_messages[0]["messages"][1]["blocks"] == [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    channel_markdown
                ),
            },
        }
    ]
    assert slack_client.opened_dms == [{"users": "U1"}]
    assert slack_client.posts == [
        {
            "channel": "D-U1",
            "text": dm_markdown,
            "blocks": service.created_messages[0]["messages"][0]["blocks"],
        },
        {
            "channel": "C1",
            "text": channel_markdown,
            "blocks": service.created_messages[0]["messages"][1]["blocks"],
        },
    ]
    assert [row["outbox_id"] for row in service.sent] == ["outbox-1", "outbox-2"]
    assert all(row["outbox_id"] != "old-outbox" for row in service.sent)
    assert service.failed == []
    assert {call["conversation_id"] for call in service.cursors} == {
        "C1",
        "C1:thread:1779181200.000100",
        "D1",
    }
    assert [call["channel"] for call in slack_client.history_calls] == ["C1", "D1"]
    assert slack_client.reply_calls[0]["channel"] == "C1"


def test_run_once_includes_manual_notes_and_archives_them_after_success(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    manual_source = (
        tmp_path
        / "data"
        / "project-a"
        / "unreconciled"
        / "manual_notes"
        / "ops"
        / "moc-start.md"
    )
    manual_source.parent.mkdir(parents=True)
    manual_source.write_text(
        "# Manual PM note\n\nJosh said the MOC start shifted to Thursday.\n",
        encoding="utf-8",
    )
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "evidence_line_answers": _evidence_line_answers(),
                "reviewer_notes": _reviewer_notes(),
                "continuity_note": _simple_continuity_note(
                    "Manual note was incorporated.",
                ),
                "draft_messages": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                        "message_markdown": _empty_assignment_review_markdown(
                            "I saw the manual note about the MOC start shift."
                        ),
                        "reason": "Manual note affects Ada's current work.",
                        "source_message_keys": ["manual_notes:ops/moc-start.md"],
                    }
                ],
                "team_channel_draft_messages": [],
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=FakeService(config=_config()),
        slack_client_factory=lambda token: FakeSlackClient(),
        subprocess_runner=runner,
        codex_bin="codex-test",
        prepare_only=True,
        now=_at(10),
        run_id="run-manual",
    )

    slack_reconciled_dir = (
        tmp_path / "data" / "project-a" / "reconciled" / "slack" / "run-manual"
    )
    manual_reconciled = (
        tmp_path
        / "data"
        / "project-a"
        / "reconciled"
        / "manual_notes"
        / "run-manual"
        / "ops"
        / "moc-start.md"
    )
    assert result.exit_code == 0
    assert result.data["manual_note_count"] == 1
    assert not manual_source.exists()
    assert manual_reconciled.read_text(encoding="utf-8").startswith(
        "# Manual PM note"
    )
    manual_notes_markdown = (slack_reconciled_dir / "manual_notes.md").read_text(
        encoding="utf-8",
    )
    assert "manual_notes:ops/moc-start.md" in manual_notes_markdown
    assert "MOC start shifted to Thursday" in manual_notes_markdown
    assert (
        slack_reconciled_dir / "manual_notes" / "ops" / "moc-start.md"
    ).read_text(encoding="utf-8").startswith("# Manual PM note")
    manifest = json.loads(
        (slack_reconciled_dir / "collection_manifest.json").read_text(
            encoding="utf-8",
        )
    )
    assert manifest["manual_note_count"] == 1
    assert manifest["manual_notes"][0]["note_key"] == "manual_notes:ops/moc-start.md"
    assert "manual_notes.md" in runner.calls[0]["input"]
    assert "manual_notes/" in runner.calls[0]["input"]


def test_run_once_keeps_manual_notes_unreconciled_when_codex_fails(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    manual_source = (
        tmp_path
        / "data"
        / "project-a"
        / "unreconciled"
        / "manual_notes"
        / "ops"
        / "moc-start.md"
    )
    manual_source.parent.mkdir(parents=True)
    manual_source.write_text("Manual note still needs reconciliation.\n", encoding="utf-8")
    runner = FakeSubprocessRunner("", returncode=1)

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=FakeService(config=_config()),
        slack_client_factory=lambda token: FakeSlackClient(),
        subprocess_runner=runner,
        codex_bin="codex-test",
        prepare_only=True,
        now=_at(10),
        run_id="run-manual-failed",
    )

    assert result.exit_code == 1
    assert manual_source.exists()
    assert not (
        tmp_path
        / "data"
        / "project-a"
        / "reconciled"
        / "manual_notes"
        / "run-manual-failed"
    ).exists()


def test_outbox_messages_do_not_rewrite_inline_numbered_lists_without_blocks():
    text = (
        "No newer reply is captured. Complete current Ada list needing correction: "
        "1 Internet stability and VPN: status planned, done means access works. "
        "2 Scotia iTRADE coverage extraction: status planned, done means data exports. "
        "3 Strategy research: status planned, done means options are summarized. "
        "Please reply with corrections, current blocker status, and any started items."
    )

    blocks = slack_bot._blocks_for_draft(text)  # noqa: SLF001

    assert blocks == [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        }
    ]


def test_outbox_messages_leave_simple_text_as_single_section_block():
    blocks = slack_bot._blocks_for_draft(  # noqa: SLF001
        "Please confirm whether access is working."
    )

    assert blocks == [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "Please confirm whether access is working.",
            },
        }
    ]


def test_outbox_messages_render_markdown_headings_and_lists():
    blocks = slack_bot._blocks_for_draft(  # noqa: SLF001
        "## Priority\n\n- Confirm access\n- Share blocker status"
    )

    assert blocks == [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Priority"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "- Confirm access\n- Share blocker status",
            },
        },
    ]


def test_run_once_retries_invalid_codex_json_with_correction_prompt(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    slack_client = FakeSlackClient()
    service = FakeService(config=_config())
    invalid_continuity = _continuity_note("Invalid first output.")
    valid_continuity = _continuity_note("Corrected output.")
    no_message = {
        "teammate_id": "tm-1",
        "slack_user_id": "U1",
        "reason": "No direct follow-up.",
        "source_message_keys": [],
    }
    runner = FakeSubprocessRunner(
        [
            json.dumps(
                {
                    "continuity_note": invalid_continuity,
                    "draft_messages": [],
                    "team_channel_draft_messages": [],
                    "no_message_decisions": [no_message],
                }
            ),
            json.dumps(
                {
                    "evidence_line_answers": _evidence_line_answers(),
                    "reviewer_notes": _reviewer_notes(),
                    "continuity_note": valid_continuity,
                    "draft_messages": [],
                    "team_channel_draft_messages": [],
                    "no_message_decisions": [no_message],
                }
            ),
        ]
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=service,
        slack_client_factory=lambda token: slack_client,
        subprocess_runner=runner,
        codex_bin="codex-test",
        prepare_only=True,
        now=_at(10),
        run_id="run-retry",
    )

    run_dir = tmp_path / "data" / "project-a" / "unreconciled" / "slack" / "run-retry"
    reconciled_dir = (
        tmp_path / "data" / "project-a" / "reconciled" / "slack" / "run-retry"
    )
    assert result.exit_code == 0
    assert not run_dir.exists()
    assert len(runner.calls) == 2
    assert "Correction attempt: 2 of 3" in runner.calls[1]["input"]
    assert "missing evidence line answers" in runner.calls[1]["input"]
    assert (reconciled_dir / "codex_invalid_output_attempt_1.txt").exists()
    stored_continuity = json.loads(service.continuity_notes[0]["continuity_note"])
    assert stored_continuity["summary"] == "Corrected output."


def test_run_once_recovers_valid_codex_artifact_when_stdout_validation_fails(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    service = FakeService(config=_config())
    slack_client = FakeSlackClient()
    no_message = {
        "teammate_id": "tm-1",
        "slack_user_id": "U1",
        "reason": "No useful follow-up.",
        "source_message_keys": [],
    }
    invalid_stdout = json.dumps(
        {
            "evidence_line_answers": [
                {
                    "evidence_line_id": "all-service-prepared-lines",
                    "question": "Were all service evidence lines reviewed?",
                    "answer": "No",
                    "reason": "No inbound Slack evidence.",
                    "outcome": None,
                    "update_intent": "No update.",
                    "source_keys": [],
                }
            ],
            "reviewer_notes": _reviewer_notes(),
            "continuity_note": _continuity_note("Invalid stdout."),
            "draft_messages": [],
            "team_channel_draft_messages": [],
            "no_message_decisions": [no_message],
        }
    )
    valid_output = {
        "evidence_line_answers": _evidence_line_answers(),
        "reviewer_notes": _reviewer_notes(),
        "continuity_note": _continuity_note("Recovered artifact."),
        "draft_messages": [],
        "team_channel_draft_messages": [],
        "no_message_decisions": [no_message],
    }
    artifact_path = (
        tmp_path
        / "data"
        / "project-a"
        / "reconciled"
        / "slack"
        / "run-artifact"
        / "pm_flow_result.final.json"
    )

    class ArtifactRunner(FakeSubprocessRunner):
        def run(self, args, *, input, text, capture_output, check, env=None):
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(json.dumps(valid_output), encoding="utf-8")
            return super().run(
                args,
                input=input,
                text=text,
                capture_output=capture_output,
                check=check,
                env=env,
            )

    runner = ArtifactRunner(invalid_stdout)

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=service,
        slack_client_factory=lambda token: slack_client,
        subprocess_runner=runner,
        codex_bin="codex-test",
        prepare_only=True,
        now=_at(10),
        run_id="run-artifact",
    )

    run_dir = (
        tmp_path / "data" / "project-a" / "unreconciled" / "slack" / "run-artifact"
    )
    reconciled_dir = (
        tmp_path / "data" / "project-a" / "reconciled" / "slack" / "run-artifact"
    )
    assert result.exit_code == 0
    assert len(runner.calls) == 1
    assert not (run_dir / "codex_invalid_output_attempt_1.txt").exists()
    assert not run_dir.exists()
    assert artifact_path.exists()
    assert (reconciled_dir / "codex_output.json").exists()
    stored_continuity = json.loads(service.continuity_notes[0]["continuity_note"])
    assert stored_continuity["summary"] == "Recovered artifact."


def test_run_once_prepare_only_uses_token_override_model_and_no_message_decisions(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    slack_client = FakeSlackClient()
    config = _config()
    config["resource_slack_map"] = [
        *config["resource_slack_map"],
        {
            "resource_id": "res-2",
            "teammate_id": "tm-2",
            "slack_user_id": "U2",
            "name": "Grace",
        },
    ]
    service = FakeService(config={**config, "token_env_var": None})
    seen_tokens = []
    runner = FakeSubprocessRunner(
        json.dumps(
                {
                    "summary": "Updated blocker status.",
                    "evidence_line_answers": _evidence_line_answers(),
                    "reviewer_notes": _reviewer_notes(),
                    "project_updates": ["Credential blocker remains active."],
                    "continuity_note": _continuity_note(
                    "Next run should check whether Ada replied today.",
                    teammates=[
                        {
                            "teammate_id": "tm-1",
                            "slack_user_id": "U1",
                            "resource_id": "res-1",
                            "name": "Ada",
                        },
                        {
                            "teammate_id": "tm-2",
                            "slack_user_id": "U2",
                            "resource_id": "res-2",
                            "name": "Grace",
                        },
                    ],
                ),
                "draft_messages": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                        "message_markdown": "Please send the credential update.",
                        "reason": "Credential blocker is relevant.",
                        "source_message_keys": ["D1:1779184800.000200"],
                        "pm_evidence_claims": [
                            {
                                "evidence_type": "project_update_notice",
                                "resource_id": "res-1",
                                "evidence_note": (
                                    "Ada is being told that the credential blocker "
                                    "status was updated."
                                ),
                            }
                        ],
                    }
                ],
                "no_message_decisions": [
                    {
                        "teammate_id": "tm-2",
                        "slack_user_id": "U2",
                        "reason": "No new relevant update.",
                        "source_message_keys": [],
                    }
                ],
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=service,
        token_override="xoxb-ui",
        slack_client_factory=lambda token: seen_tokens.append(token) or slack_client,
        subprocess_runner=runner,
        codex_bin="codex-test",
        codex_model="gpt-5-test",
        prepare_only=True,
        now=_at(10),
        run_id="run-1",
    )

    run_dir = tmp_path / "data" / "project-a" / "unreconciled" / "slack" / "run-1"
    reconciled_dir = tmp_path / "data" / "project-a" / "reconciled" / "slack" / "run-1"
    assert result.exit_code == 0
    assert not run_dir.exists()
    assert result.data["prepare_only"] is True
    assert result.data["no_message_count"] == 1
    assert result.data["send_result"] is None
    assert seen_tokens == ["xoxb-ui"]
    assert runner.calls[0]["args"][:2] == ["codex-test", "exec"]
    assert "--ask-for-approval" not in runner.calls[0]["args"]
    assert "-a" not in runner.calls[0]["args"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in runner.calls[0]["args"]
    sandbox_index = runner.calls[0]["args"].index("-s")
    assert runner.calls[0]["args"][sandbox_index + 1] == "danger-full-access"
    assert "--model" in runner.calls[0]["args"]
    model_index = runner.calls[0]["args"].index("--model")
    assert runner.calls[0]["args"][model_index + 1] == "gpt-5-test"
    assert json.loads((reconciled_dir / "codex_output.json").read_text())[
        "no_message_decisions"
    ][0]["reason"] == "No new relevant update."
    assert service.created_messages
    assert slack_client.posts == []
    assert service.sent == []


def test_run_once_invokes_codex_with_continuity_when_no_new_evidence(
    monkeypatch,
    tmp_path,
):
    class EmptySlackClient(FakeSlackClient):
        def conversations_history(self, **kwargs):
            self.history_calls.append(kwargs)
            return {"ok": True, "messages": []}

        def conversations_replies(self, **kwargs):
            raise AssertionError("No thread replies should be fetched without messages.")

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    service = FakeService(
        config={
            **_config(),
            "continuity_note": "Check whether Ada replied by noon.",
            "continuity_updated_at": _at(9).isoformat(),
        }
    )
    service.pm_evidence_line_items = [
        {
            "evidence_line_id": "evidence:no-new-slack",
            "question": "Is there new Slack evidence?",
        }
    ]
    slack_client = EmptySlackClient()
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "evidence_line_answers": [
                    {
                        "evidence_line_id": "evidence:no-new-slack",
                        "question": "Is there new Slack evidence?",
                        "answer": "No",
                        "reason": "No messages were collected in this run.",
                        "outcome": "No service update is needed.",
                        "update_intent": "No update.",
                        "source_keys": [],
                    }
                ],
                "reviewer_notes": _reviewer_notes(),
                "continuity_note": _simple_continuity_note(
                    "No new evidence; check Ada again this afternoon.",
                ),
                "draft_messages": [],
                "no_message_decisions": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                        "reason": "No new evidence required a message.",
                        "source_message_keys": [],
                    }
                ],
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=service,
        slack_client_factory=lambda token: slack_client,
        subprocess_runner=runner,
        now=_at(10),
        run_id="run-empty",
    )

    run_dir = (
        tmp_path / "data" / "project-a" / "unreconciled" / "slack" / "run-empty"
    )
    reconciled_dir = (
        tmp_path / "data" / "project-a" / "reconciled" / "slack" / "run-empty"
    )
    assert result.exit_code == 0
    assert not run_dir.exists()
    assert result.noop is False
    assert runner.calls
    prompt = runner.calls[0]["input"]
    assert "continuity_note.json" in prompt
    assert "Project id: project-a" in prompt
    assert f"ProjDash database path: {(tmp_path / 'project.sqlite').resolve()}" in prompt
    for phrase in [
        "PM markdown context path:",
        "Read these inputs first:",
        "Procedure:",
        "Read: use pm_agent_context.md as the source",
        "Prioritize line items marked with `*`",
        "Reconcile: inspect Slack and manual notes",
        "processes, dependencies, blockers, role requirements, pins, forecasts",
        "verified finishes, resources, calendars, or plan data",
        "validated ProjectDashboard service command/query envelopes",
        "Use exact `role_<resource_id>` roles",
        "use shared roles only when the evidence is genuinely shareable",
        "Answer evidence: answer every service-prepared evidence line item Yes/No",
        "including explicit no-change evidence",
        "Accepted source keys include",
        "`channel:timestamp`",
        "`manual_notes:path`",
        "`pm_agent_context.md:<line or section>`",
        "Update service: update `last_evidence` only for Yes answers",
        "Diff: if service state changed",
        "pm_agent_context.after_project_updates.md",
        "pm_agent_context.after_project_updates.diff",
        "Read the diff before drafting",
        "Optional corrective cycle",
        "pm_agent_context.after_adjustment_pass.md",
        "pm_agent_context.after_adjustment_pass.diff",
        "If no corrective cycle is needed, say so in reviewer_notes",
        "Draft: query the refreshed PM communication protocol",
        "Review: run a reviewer pass",
        "reviewer_notes",
        "Return JSON: update a concise continuity_note",
        "evidence_line_answers",
        "unsent_outbox.json",
        "do not mine past sent, failed, or skipped outbound messages",
        "pm_communication_protocol.json",
        "team_channel_draft_messages",
        "message_artifact",
        "service-rendered message_markdown",
        "Return only `message_markdown`",
        "Do not return `text`, `body`, `blocks`",
        "runner converts `message_markdown` into Slack Block Kit",
        "message_receipt_ack",
        "project_update_notice",
        "newline-separated lists",
        "Assume teammates do not have access to ProjDash",
        "Do not merely check in",
    ]:
        assert phrase in prompt
    assert "exactly one entry for each point 1 through 18" not in prompt
    assert "theory-of-mind extension" not in prompt
    assert "pm_signal_context.json" not in prompt
    assert "teammate_context.json" not in prompt
    assert not (reconciled_dir / "pm_signal_context.json").exists()
    assert not (reconciled_dir / "teammate_context.json").exists()
    assert json.loads((reconciled_dir / "collection_manifest.json").read_text())[
        "message_count"
    ] == 0
    continuity = json.loads((reconciled_dir / "continuity_note.json").read_text())
    assert continuity["previous_continuity_note"] == "Check whether Ada replied by noon."
    stored_continuity = json.loads(service.continuity_notes[0]["continuity_note"])
    assert stored_continuity["summary"] == (
        "No new evidence; check Ada again this afternoon."
    )
    assert stored_continuity["schema_version"] == 3
    assert stored_continuity["note"]
    assert "pm_assessment" not in stored_continuity
    assert result.data["evidence_line_answers"][0]["answer"] == "no"
    assert result.data["reviewer_notes"][0]["status"] == "approved"
    assert service.created_messages == []
    assert service.sent == []
    assert {call["conversation_id"] for call in service.cursors} == {"C1", "D1"}
    assert {call["conversation_id"] for call in service.cursors} == {"C1", "D1"}



def test_pm_flow_no_longer_emits_legacy_assessment_context():
    assert not hasattr(slack_bot, "_pm_signal_context")
    assert not hasattr(slack_bot, "_pm_assessment_inputs")

def test_codex_draft_rejects_internal_project_management_terms():
    with pytest.raises(ValidationError, match="self-contained"):
        slack_bot.CodexDraftOutput.model_validate(
            {
                "continuity_note": _continuity_note(
                    "Ada knows the clarification is due today.",
                ),
                "draft_messages": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                        "message_markdown": (
                            "This task is on the critical path and past LS."
                        ),
                        "reason": "Schedule risk.",
                        "source_message_keys": [],
                    }
                ],
                "no_message_decisions": [],
            }
        )


def test_codex_drafts_are_markdown_only():
    valid = slack_bot.CodexDraftOutput.model_validate(
        {
            "continuity_note": _continuity_note("Markdown-only draft."),
            "draft_messages": [
                {
                    "teammate_id": "tm-1",
                    "slack_user_id": "U1",
                    "message_markdown": "## Update\n\nPlease confirm access.",
                }
            ],
            "team_channel_draft_messages": [
                {
                    "channel_id": "C1",
                    "message_markdown": "## Team update\n\nNo shared action.",
                }
            ],
            "no_message_decisions": [],
        }
    )

    assert valid.draft_messages[0].message_markdown.startswith("## Update")
    with pytest.raises(ValidationError, match="Extra inputs"):
        slack_bot.CodexDraftOutput.model_validate(
            {
                "continuity_note": _continuity_note("Old output rejected."),
                "draft_messages": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                        "text": "Old free-form fallback text.",
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": "Old block.",
                                },
                            }
                        ],
                    }
                ],
                "no_message_decisions": [],
            }
        )


def test_codex_output_requires_evidence_line_yes_no_answer():
    with pytest.raises(ValidationError, match="answer must be Yes/No"):
        slack_bot.CodexDraftOutput.model_validate(
            {
                "evidence_line_answers": [
                    {
                        "evidence_line_id": "evidence:blocked",
                        "question": "Is Ada blocked?",
                        "answer": "maybe",
                        "reason": "Ambiguous answer should be rejected.",
                    }
                ],
                "reviewer_notes": _reviewer_notes(),
                "continuity_note": _simple_continuity_note(),
                "draft_messages": [],
                "no_message_decisions": [],
            }
        )


def test_codex_output_accepts_simplified_pm_flow_output():
    output = slack_bot.CodexDraftOutput.model_validate(
        {
            "evidence_line_answers": _evidence_line_answers(),
            "reviewer_notes": _reviewer_notes(),
            "project_updates": ["Recorded Ada's credential blocker."],
            "continuity_note": _simple_continuity_note(),
            "draft_messages": [],
            "team_channel_draft_messages": [],
            "no_message_decisions": [
                {
                    "teammate_id": "tm-1",
                    "slack_user_id": "U1",
                    "reason": "The channel draft covers the relevant update.",
                    "source_message_keys": ["D1:1779184800.000200"],
                }
            ],
        }
    )

    assert output.evidence_line_answers[0].answer == "yes"
    assert output.reviewer_notes[0].status == "approved"
    assert output.continuity_note.schema_version == 3


def test_codex_output_must_cover_service_prepared_evidence_lines():
    parsed_config = slack_bot._normalize_config(_config(), "project-a")
    output = slack_bot.CodexDraftOutput.model_validate(
        {
            "evidence_line_answers": _evidence_line_answers(),
            "reviewer_notes": _reviewer_notes(),
            "continuity_note": _simple_continuity_note(),
            "draft_messages": [],
            "no_message_decisions": [
                {
                    "teammate_id": "tm-1",
                    "slack_user_id": "U1",
                    "reason": "No follow-up needed.",
                    "source_message_keys": [],
                }
            ],
        }
    )

    with pytest.raises(slack_bot.IntegrationError, match="missing evidence line"):
        slack_bot._validate_codex_reconciliation_output(
            output,
            parsed_config,
            pm_agent_context={
                "evidence_line_items": [
                    {
                        "evidence_line_id": "evidence:slack-blocker",
                        "question": "Is Ada blocked on credentials?",
                    },
                    {
                        "evidence_line_id": "evidence:planned-schedule",
                        "question": "Is Ada's planned schedule correct?",
                    },
                ]
            },
        )


def test_codex_output_no_longer_requires_theory_of_mind_for_each_mapped_teammate():
    config = _config()
    config["resource_slack_map"] = [
        *config["resource_slack_map"],
        {
            "resource_id": "res-2",
            "teammate_id": "tm-2",
            "slack_user_id": "U2",
            "name": "Grace",
        },
    ]
    parsed_config = slack_bot._normalize_config(config, "project-a")
    output = slack_bot.CodexDraftOutput.model_validate(
        {
            "evidence_line_answers": _evidence_line_answers(),
            "reviewer_notes": _reviewer_notes(),
            "continuity_note": _simple_continuity_note(
                "No teammate theory-of-mind structure is required.",
            ),
            "draft_messages": [],
            "no_message_decisions": [
                {
                    "teammate_id": "tm-1",
                    "slack_user_id": "U1",
                    "reason": "No follow-up for Ada.",
                    "source_message_keys": [],
                },
                {
                    "teammate_id": "tm-2",
                    "slack_user_id": "U2",
                    "reason": "No follow-up for Grace.",
                    "source_message_keys": [],
                },
            ],
        }
    )

    slack_bot._validate_codex_reconciliation_output(output, parsed_config)


def test_codex_output_requires_due_pm_evidence_claims():
    parsed_config = slack_bot._normalize_config(_config(), "project-a")
    protocol = {
        "obligations": [
            {
                "obligation_id": "process_pre_start_3_day:res-1:process-a",
                "due": True,
                "evidence_type": "process_pre_start_3_day",
                "resource_id": "res-1",
                "process_id": "process-a",
                "process_symbol": "A",
                "required_evidence_types": [
                    "process_pre_start_3_day",
                    "process_full_update",
                ],
                "target_type": "dm",
                "slack_user_id": "U1",
                "content_hash": "sha256:process-a",
            }
        ]
    }
    missing = slack_bot.CodexDraftOutput.model_validate(
        {
            "continuity_note": _continuity_note("Protocol claim missing."),
            "draft_messages": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                        "message_markdown": (
                            "Task A starts soon; please confirm readiness."
                        ),
                        "reason": "Protocol obligation is due.",
                    "source_message_keys": [],
                }
            ],
            "no_message_decisions": [],
        }
    )
    with pytest.raises(slack_bot.IntegrationError, match="omitted required PM evidence"):
        slack_bot._validate_codex_reconciliation_output(
            missing,
            parsed_config,
            pm_protocol_context=protocol,
        )

    wrong_hash = slack_bot.CodexDraftOutput.model_validate(
        {
            "continuity_note": _continuity_note("Protocol claim has wrong hash."),
            "draft_messages": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                        "message_markdown": (
                            "Task A starts soon; please confirm readiness."
                        ),
                        "reason": "Protocol obligation is due.",
                    "source_message_keys": [],
                    "pm_evidence_claims": [
                        {
                            "evidence_type": "process_pre_start_3_day",
                            "resource_id": "res-1",
                            "process_id": "process-a",
                            "process_symbol": "A",
                            "obligation_id": (
                                "process_pre_start_3_day:res-1:process-a"
                            ),
                            "content_hash": "sha256:wrong",
                        },
                        {
                            "evidence_type": "process_full_update",
                            "resource_id": "res-1",
                            "process_id": "process-a",
                            "process_symbol": "A",
                            "obligation_id": (
                                "process_pre_start_3_day:res-1:process-a"
                            ),
                            "content_hash": "sha256:wrong",
                        },
                    ],
                }
            ],
            "no_message_decisions": [],
        }
    )
    with pytest.raises(slack_bot.IntegrationError, match="content hash"):
        slack_bot._validate_codex_reconciliation_output(
            wrong_hash,
            parsed_config,
            pm_protocol_context=protocol,
        )

    non_due_protocol = {
        "obligations": [
            {
                **protocol["obligations"][0],
                "due": False,
            }
        ]
    }
    with pytest.raises(slack_bot.IntegrationError, match="invalid PM evidence"):
        slack_bot._validate_codex_reconciliation_output(
            wrong_hash,
            parsed_config,
            pm_protocol_context=non_due_protocol,
        )

    valid = slack_bot.CodexDraftOutput.model_validate(
        {
            "continuity_note": _continuity_note("Protocol claim present."),
            "draft_messages": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                    "message_markdown": (
                        "Task A starts soon. Planned start is Friday; done means "
                        "the acceptance checklist is complete. Please confirm "
                        "readiness."
                    ),
                    "reason": "Protocol obligation is due.",
                    "source_message_keys": [],
                    "pm_evidence_claims": [
                        {
                            "evidence_type": "process_pre_start_3_day",
                            "resource_id": "res-1",
                            "process_id": "process-a",
                            "process_symbol": "A",
                            "obligation_id": (
                                "process_pre_start_3_day:res-1:process-a"
                            ),
                            "content_hash": "sha256:process-a",
                        },
                        {
                            "evidence_type": "process_full_update",
                            "resource_id": "res-1",
                            "process_id": "process-a",
                            "process_symbol": "A",
                            "obligation_id": (
                                "process_pre_start_3_day:res-1:process-a"
                            ),
                            "content_hash": "sha256:process-a",
                        },
                    ],
                }
            ],
            "no_message_decisions": [],
        }
    )
    slack_bot._validate_codex_reconciliation_output(
        valid,
        parsed_config,
        pm_protocol_context=protocol,
    )


def test_codex_output_requires_service_generated_message_artifact():
    parsed_config = slack_bot._normalize_config(_config(), "project-a")
    artifact_blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Tasks for Ada"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Needs attention*"}},
    ]
    protocol = {
        "obligations": [
            {
                "obligation_id": "resource_assignment_review:res-1",
                "due": True,
                "evidence_type": "resource_assignment_review",
                "resource_id": "res-1",
                "process_id": None,
                "process_symbol": None,
                "required_evidence_types": ["resource_assignment_review"],
                "target_type": "dm",
                "slack_user_id": "U1",
                "content_hash": "sha256:assignment-list",
                "message_artifact": {
                    "artifact_kind": "resource_assignment_list",
                    "rendered_by": "query_pm_communication_protocol",
                    "content_hash": "sha256:assignment-list",
                    "message_markdown": "Current process work list for Ada.",
                    "message_blocks": artifact_blocks,
                    "required_visible_text": "Tasks for Ada\n\n*Needs attention*",
                },
            }
        ]
    }
    missing_artifact = slack_bot.CodexDraftOutput.model_validate(
        {
            "continuity_note": _continuity_note("Artifact missing."),
            "draft_messages": [
                {
                    "teammate_id": "tm-1",
                    "slack_user_id": "U1",
                    "message_markdown": "Please review your assignment list.",
                    "pm_evidence_claims": [
                        {
                            "evidence_type": "resource_assignment_review",
                            "resource_id": "res-1",
                            "obligation_id": "resource_assignment_review:res-1",
                            "content_hash": "sha256:assignment-list",
                        }
                    ],
                }
            ],
            "no_message_decisions": [],
        }
    )

    with pytest.raises(slack_bot.IntegrationError, match="message artifact"):
        slack_bot._validate_codex_reconciliation_output(
            missing_artifact,
            parsed_config,
            pm_protocol_context=protocol,
        )

    valid = slack_bot.CodexDraftOutput.model_validate(
        {
            "continuity_note": _continuity_note("Artifact included."),
            "draft_messages": [
                {
                    "teammate_id": "tm-1",
                    "slack_user_id": "U1",
                    "message_markdown": "# Tasks for Ada\n\n*Needs attention*",
                    "pm_evidence_claims": [
                        {
                            "evidence_type": "resource_assignment_review",
                            "resource_id": "res-1",
                            "obligation_id": "resource_assignment_review:res-1",
                            "content_hash": "sha256:assignment-list",
                        }
                    ],
                }
            ],
            "no_message_decisions": [],
        }
    )
    slack_bot._validate_codex_reconciliation_output(
        valid,
        parsed_config,
        pm_protocol_context=protocol,
    )


def test_run_once_rejects_codex_draft_for_unmapped_slack_user(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    slack_client = FakeSlackClient()
    service = FakeService(config=_config())
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "continuity_note": _continuity_note(
                    "Invalid run should not persist because user is unmapped.",
                ),
                "draft_messages": [
                    {
                        "teammate_id": "outside",
                        "slack_user_id": "U999",
                        "message_markdown": "This should not be sent.",
                    }
                ]
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=service,
        slack_client_factory=lambda token: slack_client,
        subprocess_runner=runner,
        now=_at(10),
        run_id="run-1",
    )

    assert result.exit_code == 1
    assert "unmapped Slack user" in (result.message or "")
    assert len(runner.calls) == 3
    assert service.cursors == []
    assert service.created_messages == []
    assert slack_client.posts == []


def test_run_once_does_not_advance_cursors_when_codex_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    service = FakeService(config=_config())
    runner = FakeSubprocessRunner("", returncode=1)

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=service,
        slack_client_factory=lambda token: FakeSlackClient(),
        subprocess_runner=runner,
        now=_at(10),
        run_id="run-1",
    )

    assert result.exit_code == 1
    assert service.cursors == []
    assert service.created_messages == []


def test_run_once_does_not_advance_cursors_when_codex_omits_teammate_decisions(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    service = FakeService(config=_config())
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "summary": "Unable to reconcile because local file access failed.",
                "project_updates": [],
                "continuity_note": _continuity_note(
                    "This should not advance because teammate coverage is missing.",
                ),
                "draft_messages": [],
                "no_message_decisions": [],
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=service,
        slack_client_factory=lambda token: FakeSlackClient(),
        subprocess_runner=runner,
        now=_at(10),
        run_id="run-1",
    )

    assert result.exit_code == 1
    assert "omitted draft/no-message decisions" in (result.message or "")
    assert len(runner.calls) == 3
    assert service.cursors == []
    assert service.created_messages == []


def test_run_once_recreates_artifact_directory_if_child_agent_moves_evidence(
    monkeypatch,
    tmp_path,
):
    class MovingEvidenceRunner:
        def __init__(self, run_dir, reconciled_dir) -> None:
            self.run_dir = run_dir
            self.reconciled_dir = reconciled_dir
            self.calls = []

        def run(self, args, *, input, text, capture_output, check, env=None):
            self.calls.append(
                {
                    "args": args,
                    "input": input,
                    "text": text,
                    "capture_output": capture_output,
                    "check": check,
                    "env": env,
                }
            )
            for path in list(self.run_dir.iterdir()):
                path.rename(self.reconciled_dir / path.name)
            self.run_dir.rmdir()
            return FakeCompletedProcess(
                json.dumps(
                        {
                            "evidence_line_answers": _evidence_line_answers(),
                            "reviewer_notes": _reviewer_notes(),
                            "continuity_note": _continuity_note(
                                "Next run should keep checking normal schedule risk.",
                            ),
                        "draft_messages": [],
                        "no_message_decisions": [
                            {
                                "teammate_id": "tm-1",
                                "slack_user_id": "U1",
                                "reason": "No teammate follow-up needed.",
                                "source_message_keys": [],
                            }
                        ],
                    }
                )
            )

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    data_root = tmp_path / "data"
    run_dir = data_root / "project-a" / "unreconciled" / "slack" / "run-1"
    reconciled_dir = data_root / "project-a" / "reconciled" / "slack" / "run-1"
    runner = MovingEvidenceRunner(run_dir, reconciled_dir)

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=data_root,
        service=FakeService(config=_config()),
        slack_client_factory=lambda token: FakeSlackClient(),
        subprocess_runner=runner,
        now=_at(10),
        run_id="run-1",
    )

    assert result.exit_code == 0
    assert not run_dir.exists()
    assert (reconciled_dir / "codex_output.json").exists()
    assert "Treat the input folder as read-only" in runner.calls[0]["input"]


def test_run_once_dry_run_persists_but_does_not_post_or_mark_sent(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    slack_client = FakeSlackClient()
    service = FakeService(config=_config())
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "evidence_line_answers": _evidence_line_answers(),
                "reviewer_notes": _reviewer_notes(),
                "continuity_note": _continuity_note("Dry-run continuity note."),
                "draft_messages": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                        "message_markdown": "Dry run message.",
                        "reason": "Preview.",
                        "source_message_keys": [],
                    }
                ]
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=service,
        slack_client_factory=lambda token: slack_client,
        subprocess_runner=runner,
        dry_run_send=True,
        now=_at(10),
        run_id="run-1",
    )

    assert result.exit_code == 0
    assert service.created_messages
    assert slack_client.posts == []
    assert service.sent == []
    assert service.failed == []


def test_send_outbox_messages_sends_only_selected_rows(monkeypatch, tmp_path):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    slack_client = FakeSlackClient()
    service = FakeService(
        config={**_config(), "token_env_var": None},
        pending=[
            {
                "outbox_id": "outbox-send",
                "slack_user_id": "U1",
                "body": "Selected update.",
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "*Selected update.*"},
                    }
                ],
            },
            {
                "outbox_id": "outbox-skip",
                "slack_user_id": "U1",
                "body": "Unselected update.",
            },
        ],
    )

    result = slack_bot.send_outbox_messages(
        db_path=tmp_path / "project.sqlite",
        project_id="project-a",
        token_override="xoxb-ui",
        outbox_ids=["outbox-send"],
        service=service,
        slack_client_factory=lambda token: slack_client,
        now=_at(11),
    )

    assert result.exit_code == 0
    assert result.data["sent"] == 1
    assert result.data["skipped"] == 1
    assert result.data["failed"] == 0
    assert slack_client.posts == [
        {
            "channel": "D-U1",
            "text": "Selected update.",
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*Selected update.*"},
                }
            ],
        }
    ]
    assert service.sent[0]["outbox_id"] == "outbox-send"
    assert service.failed == []


def test_run_once_service_config_default_channel_restricts_channel_collection(
    monkeypatch,
    tmp_path,
):
    class ExtraInvitedChannelSlackClient(FakeSlackClient):
        def conversations_list(self, **kwargs):
            self.conversations_list_calls.append(kwargs)
            return {
                "ok": True,
                "channels": [
                    {"id": "C1", "name": "general", "is_channel": True, "is_member": True},
                    {"id": "C2", "name": "other", "is_channel": True, "is_member": True},
                    {"id": "D1", "is_im": True, "user": "U1"},
                ],
            }

        def conversations_history(self, **kwargs):
            if kwargs["channel"] == "C2":
                raise AssertionError("default_channel_id should exclude C2")
            return super().conversations_history(**kwargs)

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    service, project_id = _seed_service_with_slack_config()
    slack_client = ExtraInvitedChannelSlackClient()
    dm_claims, channel_claims = _fake_message_ack_claims()
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "evidence_line_answers": _service_evidence_line_answers(
                    service,
                    project_id,
                ),
                "continuity_note": _continuity_note(
                    "No follow-up needed; continue normal monitoring.",
                    teammates=[
                        {
                            "teammate_id": "res-1",
                            "slack_user_id": "U1",
                            "resource_id": "res-1",
                            "name": "Ada",
                        }
                    ],
                ),
                "draft_messages": [
                        {
                            "teammate_id": "res-1",
                            "slack_user_id": "U1",
                            "message_markdown": _empty_assignment_review_markdown(
                                "Received your update; no direct follow-up is needed."
                            ),
                            "reason": "Acknowledge DM and assignment-review protocol.",
                            "source_message_keys": [],
                            "pm_evidence_claims": dm_claims,
                    }
                ],
                "team_channel_draft_messages": [
                    {
                        "channel_id": "C1",
                        "channel_name": "general",
                        "message_markdown": (
                            "Received the channel context; no action is needed."
                        ),
                        "reason": "Acknowledge collected channel messages.",
                        "source_message_keys": [],
                        "pm_evidence_claims": channel_claims,
                    }
                ],
                "no_message_decisions": [],
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id=project_id,
        data_root=tmp_path / "data",
        service=service,
        slack_client_factory=lambda token: slack_client,
        subprocess_runner=runner,
        codex_bin="codex-test",
        now=_at(10),
        run_id="run-1",
    )

    assert result.exit_code == 0
    assert [call["channel"] for call in slack_client.history_calls] == ["C1", "D1"]


def test_run_once_works_against_service_command_and_query_envelopes(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    service, project_id = _seed_service_with_slack_config()
    slack_client = FakeSlackClient()
    dm_claims, channel_claims = _fake_message_ack_claims()
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "evidence_line_answers": _service_evidence_line_answers(
                    service,
                    project_id,
                ),
                "continuity_note": _continuity_note(
                    "Next run should check whether Ada sent an update.",
                    teammates=[
                        {
                            "teammate_id": "res-1",
                            "slack_user_id": "U1",
                            "resource_id": "res-1",
                            "name": "Ada",
                        }
                    ],
                ),
                "draft_messages": [
                    {
                        "teammate_id": "res-1",
                        "slack_user_id": "U1",
                        "message_markdown": _empty_assignment_review_markdown(
                            "Please update the credential blocker."
                        ),
                        "pm_evidence_claims": dm_claims,
                    }
                ],
                "team_channel_draft_messages": [
                    {
                        "channel_id": "C1",
                        "channel_name": "general",
                        "message_markdown": "Received the channel context; tracking it.",
                        "pm_evidence_claims": channel_claims,
                    }
                ],
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.sqlite",
        project_id=project_id,
        data_root=tmp_path / "data",
        service=service,
        slack_client_factory=lambda token: slack_client,
        subprocess_runner=runner,
        codex_bin="codex-test",
        now=_at(10),
        run_id="run-1",
    )

    assert result.exit_code == 0
    outbox = _query(
        service,
        {
            "action": "query_pending_slack_outbox",
            "project_id": project_id,
            "statuses": ["sent"],
        },
    )["outbox"]
    dm_outbox = next(row for row in outbox if row["target_type"] == "dm")
    assert dm_outbox["resource_id"] == "res-1"
    assert dm_outbox["body"].endswith("Please update the credential blocker.")
    assert "Current process work list for `res-1`" in dm_outbox["body"]
    assert dm_outbox["blocks"] == slack_bot._blocks_for_draft(  # noqa: SLF001
        _empty_assignment_review_markdown("Please update the credential blocker.")
    )
    assert dm_outbox["slack_channel_id"] == "D-U1"
    assert _query(
        service,
        {"action": "query_slack_project_config", "project_id": project_id},
    )["collection_cursors"]


def test_collect_messages_fetches_new_replies_for_known_old_threads():
    class OldThreadSlackClient:
        def __init__(self) -> None:
            self.history_calls = []
            self.reply_calls = []

        def conversations_history(self, **kwargs):
            self.history_calls.append(kwargs)
            return {"ok": True, "messages": []}

        def conversations_replies(self, **kwargs):
            self.reply_calls.append(kwargs)
            return {
                "ok": True,
                "messages": [
                    {
                        "type": "message",
                        "user": "U1",
                        "text": "New reply on old thread",
                        "ts": "1779189900.000400",
                        "thread_ts": "1779180000.000100",
                    }
                ],
            }

    client = OldThreadSlackClient()
    config = slack_bot._normalize_config(
        {
            **_config(),
            "conversation_cursors": {
                "C1": "1779189000.000000",
                "C1:thread:1779180000.000100": "1779189000.000000",
            },
        },
        "project-a",
    )

    collected = slack_bot._collect_messages(
        client,
        config,
        [{"id": "C1", "name": "general", "is_channel": True, "is_member": True}],
        _at(12),
    )

    assert [message["text"] for message in collected["messages"]] == [
        "New reply on old thread"
    ]
    assert client.reply_calls[0]["ts"] == "1779180000.000100"
    assert collected["cursors"]["C1:thread:1779180000.000100"] == "1779189900.000400"


def test_manifest_contains_slack_schema_fields_and_required_scopes(capsys):
    result = slack_bot.manifest(project_id="project-a", name="ProjDash Test Bot")

    payload = json.loads(capsys.readouterr().out)
    scopes = payload["oauth_config"]["scopes"]["bot"]
    assert result.exit_code == 0
    assert payload["display_information"]["name"] == "ProjDash Test Bot"
    assert payload["features"]["app_home"]["messages_tab_enabled"] is True
    assert (
        payload["features"]["app_home"]["messages_tab_read_only_enabled"]
        is False
    )
    assert "_metadata" not in payload
    assert "chat:write" in scopes
    assert "channels:history" in scopes
    assert "im:history" in scopes
    assert "im:write" in scopes
