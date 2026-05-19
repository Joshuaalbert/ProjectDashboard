import datetime as dt
import json

import pytest

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

    def query_slack_project_config(self, project_id):
        assert project_id == "project-a"
        return self.config

    def query_agent_context(self, project_id, as_of=None, now=None):
        assert project_id == "project-a"
        return {"summary": "Current project context", "as_of": as_of.isoformat()}

    def record_slack_collection_cursor(self, **kwargs):
        self.cursors.append(kwargs)
        return {"ok": True}

    def create_slack_outbox_messages(self, **kwargs):
        self.created_messages.append(kwargs)
        self.pending.extend(
            {
                "outbox_id": f"outbox-{index}",
                "slack_user_id": message["slack_user_id"],
                "body": message["body"],
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
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
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
        return FakeCompletedProcess(self.stdout, returncode=self.returncode)


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
        db_path=tmp_path / "project.lbug",
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
        db_path=tmp_path / "project.lbug",
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
        db_path=tmp_path / "project.lbug",
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
        db_path=tmp_path / "project.lbug",
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
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "continuity_note": "Next run should confirm Ada saw the blocker update.",
                "draft_messages": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                        "text": "I saw the credential blocker and will track it.",
                        "reason": "Follow up on Slack blocker.",
                        "source_message_keys": ["D1:1779184800.000200"],
                    }
                ]
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.lbug",
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
    assert result.exit_code == 0
    assert (run_dir / "raw_messages.jsonl").exists()
    assert "Need draft copy by Friday" in (run_dir / "messages.md").read_text()
    manifest = json.loads((run_dir / "collection_manifest.json").read_text())
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
    assert "$projdash-project-manager from" in runner.calls[0]["input"]
    assert "sub-agent to review reconciliation choices" in runner.calls[0]["input"]
    assert service.created_messages[0]["messages"][0]["body"].startswith("I saw")
    assert service.created_messages[0]["messages"][0]["content_hash"].startswith(
        "sha256:"
    )
    assert slack_client.opened_dms == [{"users": "U1"}]
    assert slack_client.posts == [
        {
            "channel": "D-U1",
            "text": "I saw the credential blocker and will track it.",
        }
    ]
    assert service.sent[0]["outbox_id"] == "outbox-1"
    assert service.failed == []
    assert {call["conversation_id"] for call in service.cursors} == {
        "C1",
        "C1:thread:1779181200.000100",
        "D1",
    }
    assert [call["channel"] for call in slack_client.history_calls] == ["C1", "D1"]
    assert slack_client.reply_calls[0]["channel"] == "C1"


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
                "project_updates": ["Credential blocker remains active."],
                "continuity_note": "Next run should check whether Ada replied today.",
                "draft_messages": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                        "text": "Please send the credential update.",
                        "reason": "Credential blocker is relevant.",
                        "source_message_keys": ["D1:1779184800.000200"],
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
        db_path=tmp_path / "project.lbug",
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
    assert result.exit_code == 0
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
    assert json.loads((run_dir / "codex_output.json").read_text())[
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
    slack_client = EmptySlackClient()
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "continuity_note": "No new evidence; check Ada again this afternoon.",
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
        db_path=tmp_path / "project.lbug",
        project_id="project-a",
        data_root=tmp_path / "data",
        service=service,
        slack_client_factory=lambda token: slack_client,
        subprocess_runner=runner,
        now=_at(10),
        run_id="run-empty",
    )

    run_dir = tmp_path / "data" / "project-a" / "unreconciled" / "slack" / "run-empty"
    assert result.exit_code == 0
    assert result.noop is False
    assert runner.calls
    prompt = runner.calls[0]["input"]
    assert "continuity_note.json" in prompt
    assert "Project id: project-a" in prompt
    assert f"ProjDash database path: {(tmp_path / 'project.lbug').resolve()}" in prompt
    assert "Apply validated project-management updates directly" in prompt
    for phrase in [
        "should already have started work",
        "past LS or LF",
        "critical-path work",
        "estimate updates before and during",
        "multi-role or multi-resource",
        "high-slack work",
        "specific staked resource",
        "clear staked/current assignment",
        "DM responsiveness",
        "new blockers and resolved blockers",
        "uncaptured work",
        "recurring weekly capacity changes",
        "topology is too coarse or too granular",
        "Commit a schedule snapshot",
        "milestone slippage",
        "project channel",
        "definitions of done",
        "role assignments and estimate buy-in",
    ]:
        assert phrase in prompt
    assert json.loads((run_dir / "collection_manifest.json").read_text())[
        "message_count"
    ] == 0
    continuity = json.loads((run_dir / "continuity_note.json").read_text())
    assert continuity["previous_continuity_note"] == "Check whether Ada replied by noon."
    assert service.continuity_notes[0]["continuity_note"] == (
        "No new evidence; check Ada again this afternoon."
    )
    assert service.created_messages == []
    assert service.sent == []
    assert {call["conversation_id"] for call in service.cursors} == {"C1", "D1"}
    assert {call["conversation_id"] for call in service.cursors} == {"C1", "D1"}


def test_run_once_rejects_codex_draft_for_unmapped_slack_user(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    slack_client = FakeSlackClient()
    service = FakeService(config=_config())
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "continuity_note": "Invalid run should not persist because user is unmapped.",
                "draft_messages": [
                    {
                        "teammate_id": "outside",
                        "slack_user_id": "U999",
                        "text": "This should not be sent.",
                    }
                ]
            }
        )
    )

    with pytest.raises(slack_bot.IntegrationError, match="unmapped Slack user"):
        slack_bot.run_once(
            db_path=tmp_path / "project.lbug",
            project_id="project-a",
            data_root=tmp_path / "data",
            service=service,
            slack_client_factory=lambda token: slack_client,
            subprocess_runner=runner,
            now=_at(10),
            run_id="run-1",
        )

    assert service.cursors == []
    assert service.created_messages == []
    assert slack_client.posts == []


def test_run_once_does_not_advance_cursors_when_codex_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    service = FakeService(config=_config())
    runner = FakeSubprocessRunner("", returncode=1)

    result = slack_bot.run_once(
        db_path=tmp_path / "project.lbug",
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
                "continuity_note": "This should not advance because teammate coverage is missing.",
                "draft_messages": [],
                "no_message_decisions": [],
            }
        )
    )

    with pytest.raises(
        slack_bot.IntegrationError,
        match="omitted draft/no-message decisions",
    ):
        slack_bot.run_once(
            db_path=tmp_path / "project.lbug",
            project_id="project-a",
            data_root=tmp_path / "data",
            service=service,
            slack_client_factory=lambda token: FakeSlackClient(),
            subprocess_runner=runner,
            now=_at(10),
            run_id="run-1",
        )

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
                        "continuity_note": "Next run should keep checking normal schedule risk.",
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
        db_path=tmp_path / "project.lbug",
        project_id="project-a",
        data_root=data_root,
        service=FakeService(config=_config()),
        slack_client_factory=lambda token: FakeSlackClient(),
        subprocess_runner=runner,
        now=_at(10),
        run_id="run-1",
    )

    assert result.exit_code == 0
    assert (run_dir / "codex_output.json").exists()
    assert "Treat the input folder as read-only" in runner.calls[0]["input"]


def test_run_once_dry_run_persists_but_does_not_post_or_mark_sent(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    slack_client = FakeSlackClient()
    service = FakeService(config=_config())
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "continuity_note": "Dry-run continuity note.",
                "draft_messages": [
                    {
                        "teammate_id": "tm-1",
                        "slack_user_id": "U1",
                        "text": "Dry run message.",
                        "reason": "Preview.",
                        "source_message_keys": [],
                    }
                ]
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.lbug",
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
            },
            {
                "outbox_id": "outbox-skip",
                "slack_user_id": "U1",
                "body": "Unselected update.",
            },
        ],
    )

    result = slack_bot.send_outbox_messages(
        db_path=tmp_path / "project.lbug",
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
    assert slack_client.posts == [{"channel": "D-U1", "text": "Selected update."}]
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
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "continuity_note": "No follow-up needed; continue normal monitoring.",
                "draft_messages": [],
                "no_message_decisions": [
                    {
                        "teammate_id": "res-1",
                        "slack_user_id": "U1",
                        "reason": "No teammate follow-up needed for this test run.",
                        "source_message_keys": [],
                    }
                ],
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.lbug",
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
    runner = FakeSubprocessRunner(
        json.dumps(
            {
                "continuity_note": "Next run should check whether Ada sent an update.",
                "draft_messages": [
                    {
                        "teammate_id": "res-1",
                        "slack_user_id": "U1",
                        "text": "Please update the credential blocker.",
                    }
                ]
            }
        )
    )

    result = slack_bot.run_once(
        db_path=tmp_path / "project.lbug",
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
    assert outbox[0]["resource_id"] == "res-1"
    assert outbox[0]["body"] == "Please update the credential blocker."
    assert outbox[0]["slack_channel_id"] == "D-U1"
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
