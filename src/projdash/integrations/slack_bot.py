"""Per-project Slack collection and Codex draft runner."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

REQUIRED_BOT_SCOPES = [
    "channels:history",
    "channels:read",
    "chat:write",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "im:write",
    "users:read",
]


class IntegrationError(RuntimeError):
    """Raised when the Slack integration cannot complete the requested step."""


class SlackDraftMessage(BaseModel):
    """Validated teammate draft message emitted by Codex."""

    model_config = ConfigDict(extra="forbid")

    teammate_id: str = Field(min_length=1)
    slack_user_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    reason: str | None = None
    source_message_keys: list[str] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Draft text must not be blank.")
        return stripped


class SlackNoMessageDecision(BaseModel):
    """Validated decision that no teammate Slack follow-up is needed."""

    model_config = ConfigDict(extra="forbid")

    teammate_id: str = Field(min_length=1)
    slack_user_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    source_message_keys: list[str] = Field(default_factory=list)


class CodexDraftOutput(BaseModel):
    """Schema required from the Codex runner."""

    model_config = ConfigDict(extra="forbid")

    summary: str | None = None
    project_updates: list[str] = Field(default_factory=list)
    continuity_note: str = Field(min_length=1)
    draft_messages: list[SlackDraftMessage] = Field(default_factory=list)
    no_message_decisions: list[SlackNoMessageDecision] = Field(default_factory=list)


@dataclass(frozen=True)
class SlackMapEntry:
    """Resource-to-Slack mapping normalized from service config."""

    resource_id: str
    teammate_id: str
    slack_user_id: str
    name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "teammate_id": self.teammate_id,
            "slack_user_id": self.slack_user_id,
            "name": self.name,
        }


@dataclass(frozen=True)
class SlackChannelConfig:
    """Configured Slack channel that may be collected."""

    channel_id: str
    name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"channel_id": self.channel_id, "name": self.name}


@dataclass(frozen=True)
class SlackProjectConfig:
    """Slack integration settings owned by the service."""

    project_id: str
    enabled: bool
    token_env_var: str | None
    start_at: dt.datetime
    channels: tuple[SlackChannelConfig, ...]
    resource_slack_map: tuple[SlackMapEntry, ...]
    conversation_cursors: dict[str, str]
    continuity_note: str | None = None
    continuity_updated_at: dt.datetime | None = None
    fetch_thread_replies: bool = True
    raw: dict[str, Any] | None = None

    @property
    def slack_user_ids(self) -> set[str]:
        return {entry.slack_user_id for entry in self.resource_slack_map}


@dataclass(frozen=True)
class SlackUser:
    """Slack user row normalized for UI resource mapping."""

    slack_user_id: str
    name: str | None = None
    real_name: str | None = None
    display_name: str | None = None
    email: str | None = None
    timezone: str | None = None
    deleted: bool = False
    is_bot: bool = False
    is_app_user: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "slack_user_id": self.slack_user_id,
            "name": self.name,
            "real_name": self.real_name,
            "display_name": self.display_name,
            "email": self.email,
            "timezone": self.timezone,
            "deleted": self.deleted,
            "is_bot": self.is_bot,
            "is_app_user": self.is_app_user,
        }


@dataclass(frozen=True)
class CommandResult:
    """Small CLI result object for tests and main()."""

    exit_code: int
    noop: bool = False
    message: str | None = None
    data: dict[str, Any] | None = None


class ServiceGateway:
    """Adapter around current and incoming service APIs."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def query_slack_project_config(self, project_id: str) -> dict[str, Any] | None:
        return self._optional_call(
            "query_slack_project_config",
            query_action="query_slack_project_config",
            project_id=project_id,
        )

    def query_agent_context(self, project_id: str, now: dt.datetime) -> dict[str, Any]:
        data = self._optional_call(
            "query_agent_context",
            query_action="query_agent_context",
            project_id=project_id,
            as_of=now,
            now=now,
        )
        return data or {}

    def record_slack_collection_cursor(
        self,
        *,
        project_id: str,
        conversation_id: str,
        latest_collected_ts: str,
        run_id: str,
        updated_at: dt.datetime,
        conversation_type: str,
        conversation_name: str | None = None,
        last_run_status: str = "success",
    ) -> None:
        self._required_call(
            "record_slack_collection_cursor",
            command_action="record_slack_collection_cursor",
            project_id=project_id,
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            conversation_name=conversation_name,
            latest_collected_ts=latest_collected_ts,
            last_run_id=run_id,
            last_run_status=last_run_status,
            updated_at=updated_at,
        )

    def update_slack_continuity_note(
        self,
        *,
        project_id: str,
        continuity_note: str,
        updated_at: dt.datetime,
    ) -> None:
        self._required_call(
            "update_slack_continuity_note",
            command_action="update_slack_continuity_note",
            project_id=project_id,
            continuity_note=continuity_note,
            updated_at=updated_at,
        )

    def create_slack_outbox_messages(
        self,
        *,
        project_id: str,
        run_id: str,
        messages: list[dict[str, Any]],
        created_at: dt.datetime,
    ) -> Any:
        return self._required_call(
            "create_slack_outbox_messages",
            command_action="create_slack_outbox_messages",
            project_id=project_id,
            messages=messages,
        )

    def query_pending_slack_outbox(
        self,
        project_id: str,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"project_id": project_id}
        if statuses is not None:
            payload["statuses"] = statuses
        data = self._required_call(
            "query_pending_slack_outbox",
            query_action="query_pending_slack_outbox",
            **payload,
        )
        if isinstance(data, list):
            return [_coerce_mapping(row) for row in data]
        if isinstance(data, dict):
            rows = data.get("messages", data.get("outbox", data.get("rows", [])))
            return [_coerce_mapping(row) for row in rows]
        return []

    def mark_slack_outbox_sent(
        self,
        *,
        project_id: str,
        outbox_id: str,
        sent_at: dt.datetime,
        slack_channel_id: str | None,
        slack_message_ts: str | None,
    ) -> None:
        self._required_call(
            "mark_slack_outbox_sent",
            command_action="mark_slack_outbox_sent",
            project_id=project_id,
            outbox_id=outbox_id,
            sent_at=sent_at,
            slack_channel_id=slack_channel_id,
            slack_message_ts=slack_message_ts,
        )

    def mark_slack_outbox_failed(
        self,
        *,
        project_id: str,
        outbox_id: str,
        failed_at: dt.datetime,
        error: str,
    ) -> None:
        self._required_call(
            "mark_slack_outbox_failed",
            command_action="mark_slack_outbox_failed",
            project_id=project_id,
            outbox_id=outbox_id,
            failed_at=failed_at,
            error_text=error,
        )

    def _optional_call(
        self,
        method_name: str,
        *,
        query_action: str | None = None,
        command_action: str | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            return self._required_call(
                method_name,
                query_action=query_action,
                command_action=command_action,
                **kwargs,
            )
        except IntegrationError as exc:
            if "not available" in str(exc):
                return None
            raise

    def _required_call(
        self,
        method_name: str,
        *,
        query_action: str | None = None,
        command_action: str | None = None,
        **kwargs: Any,
    ) -> Any:
        method = getattr(self._service, method_name, None)
        if callable(method):
            return _unwrap_service_result(_call_with_supported_kwargs(method, kwargs))

        if query_action is not None:
            result = self._call_query_envelope(query_action, kwargs)
            if result is not _UNAVAILABLE:
                return result
        if command_action is not None:
            result = self._call_command_envelope(command_action, kwargs)
            if result is not _UNAVAILABLE:
                return result

        raise IntegrationError(
            f"Service API {method_name!r} is not available in this checkout."
        )

    def _call_query_envelope(self, action: str, payload: dict[str, Any]) -> Any:
        handle_query = getattr(self._service, "handle_query", None)
        if not callable(handle_query):
            return _UNAVAILABLE
        try:
            from projdash.service.queries import QueryEnvelope

            envelope = QueryEnvelope.model_validate(
                {"query": {"action": action, **_jsonable_payload(payload)}}
            )
        except Exception:
            return _UNAVAILABLE
        return _unwrap_service_result(handle_query(envelope))

    def _call_command_envelope(self, action: str, payload: dict[str, Any]) -> Any:
        handle_command = getattr(self._service, "handle_command", None)
        if not callable(handle_command):
            return _UNAVAILABLE
        try:
            from projdash.service.commands import CommandEnvelope

            envelope = CommandEnvelope.model_validate(
                {"command": {"action": action, **_jsonable_payload(payload)}}
            )
        except Exception:
            return _UNAVAILABLE
        return _unwrap_service_result(handle_command(envelope))


_UNAVAILABLE = object()


def manifest(project_id: str, name: str = "ProjDash Slack Bot") -> CommandResult:
    """Print a Slack app manifest for a project-scoped bot."""
    payload = {
        "display_information": {"name": name},
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {
                "display_name": name,
                "always_online": False,
            }
        },
        "oauth_config": {"scopes": {"bot": REQUIRED_BOT_SCOPES}},
        "settings": {
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return CommandResult(exit_code=0)


def verify(
    *,
    db_path: str | Path,
    project_id: str,
    token_override: str | None = None,
    service: Any | None = None,
    slack_client_factory: Any | None = None,
) -> CommandResult:
    """Verify that the project has an enabled Slack config and usable token."""
    gateway = ServiceGateway(service or _load_service(db_path))
    config = _normalize_config(gateway.query_slack_project_config(project_id), project_id)
    if config is None or not config.enabled:
        message = f"No enabled Slack config found for project {project_id}."
        print(message, file=sys.stderr)
        return CommandResult(exit_code=1, noop=True, message=message)

    token = _resolve_token(config, token_override=token_override)
    if token is None:
        message = _missing_token_message(config, project_id)
        print(message, file=sys.stderr)
        return CommandResult(exit_code=1, message=message)

    client = (slack_client_factory or _make_slack_client)(token)
    try:
        client.auth_test()
        client.conversations_list(
            types="public_channel,private_channel,im",
            exclude_archived=True,
            limit=1,
        )
        for slack_user_id in sorted(config.slack_user_ids):
            client.users_info(user=slack_user_id)
    except Exception as exc:
        message = f"Slack auth or scopes are insufficient: {_slack_error_message(exc)}"
        print(message, file=sys.stderr)
        return CommandResult(exit_code=1, message=message)

    message = f"Slack integration verified for project {project_id}."
    print(message)
    return CommandResult(exit_code=0, message=message)


def list_slack_users(
    *,
    db_path: str | Path,
    project_id: str,
    token_override: str | None = None,
    include_deleted: bool = False,
    include_bots: bool = False,
    service: Any | None = None,
    slack_client_factory: Any | None = None,
) -> list[SlackUser]:
    """List Slack workspace users for UI mapping controls."""
    gateway = ServiceGateway(service or _load_service(db_path))
    config = _normalize_config(gateway.query_slack_project_config(project_id), project_id)
    if config is None or not config.enabled:
        raise IntegrationError(f"No enabled Slack config found for project {project_id}.")

    token = _resolve_token(config, token_override=token_override)
    if token is None:
        raise IntegrationError(_missing_token_message(config, project_id))

    client = (slack_client_factory or _make_slack_client)(token)
    users = []
    for raw_user in _paged_users_list(client):
        user = _normalize_slack_user(raw_user)
        if user.deleted and not include_deleted:
            continue
        if (user.is_bot or user.is_app_user) and not include_bots:
            continue
        users.append(user)
    return sorted(
        users,
        key=lambda user: (
            (user.display_name or user.real_name or user.name or "").casefold(),
            user.slack_user_id,
        ),
    )


def run_once(
    *,
    db_path: str | Path,
    project_id: str,
    data_root: str | Path = "data",
    codex_bin: str = "codex",
    codex_model: str | None = None,
    token_override: str | None = None,
    prepare_only: bool = False,
    dry_run_send: bool = False,
    selected_outbox_ids: Iterable[str] | None = None,
    service: Any | None = None,
    slack_client_factory: Any | None = None,
    subprocess_runner: Any = subprocess,
    now: dt.datetime | None = None,
    run_id: str | None = None,
) -> CommandResult:
    """Collect Slack evidence, ask Codex for drafts, persist/send pending outbox."""
    if now is None:
        now = dt.datetime.now(tz=dt.UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if run_id is None:
        run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"

    gateway = ServiceGateway(service or _load_service(db_path))
    config = _normalize_config(gateway.query_slack_project_config(project_id), project_id)
    if config is None or not config.enabled:
        message = f"No enabled Slack config found for project {project_id}; no-op."
        print(message)
        return CommandResult(exit_code=0, noop=True, message=message)

    token = _resolve_token(config, token_override=token_override)
    if token is None:
        message = _missing_token_message(config, project_id)
        print(message, file=sys.stderr)
        return CommandResult(exit_code=1, message=message)

    client = (slack_client_factory or _make_slack_client)(token)
    run_dir = (
        Path(data_root)
        / _safe_path_segment(project_id)
        / "unreconciled"
        / "slack"
        / _safe_path_segment(run_id)
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    agent_context = gateway.query_agent_context(project_id, now)
    config = _config_with_project_start(config, agent_context)
    conversations = _eligible_conversations(client, config)
    collected = _collect_messages(client, config, conversations, now)
    _write_evidence_files(
        run_dir=run_dir,
        project_id=project_id,
        run_id=run_id,
        config=config,
        agent_context=agent_context,
        conversations=conversations,
        collected=collected,
        now=now,
    )

    conversations_by_id = {conversation["id"]: conversation for conversation in conversations}

    reconciled_dir = (
        Path(data_root)
        / _safe_path_segment(project_id)
        / "reconciled"
        / "slack"
        / _safe_path_segment(run_id)
    )
    prompt = _codex_prompt(
        run_dir,
        reconciled_dir,
        db_path=db_path,
        project_id=project_id,
    )
    codex_args = [
        codex_bin,
        "exec",
    ]
    if codex_model and codex_model.strip():
        codex_args.extend(["--model", codex_model.strip()])
    codex_args.extend(
        [
            "-C",
            str(Path.cwd()),
            "-s",
            "danger-full-access",
        ]
    )
    completed = subprocess_runner.run(
        codex_args,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
        env=_codex_subprocess_env(config),
    )
    if completed.returncode != 0:
        message = (
            f"codex exec failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
        print(message, file=sys.stderr)
        return CommandResult(exit_code=1, message=message)

    codex_output = _parse_codex_output(completed.stdout)
    _write_json(run_dir / "codex_output.json", codex_output.model_dump(mode="json"))
    _validate_codex_reconciliation_output(codex_output, config)
    gateway.update_slack_continuity_note(
        project_id=project_id,
        continuity_note=codex_output.continuity_note,
        updated_at=now,
    )
    drafts = codex_output.draft_messages
    outbox_messages = _outbox_messages_from_drafts(
        drafts,
        config=config,
        run_id=run_id,
        created_at=now,
    )
    if outbox_messages:
        gateway.create_slack_outbox_messages(
            project_id=project_id,
            run_id=run_id,
            messages=outbox_messages,
            created_at=now,
        )

    _record_collected_cursors(
        gateway=gateway,
        project_id=project_id,
        collected=collected,
        conversations_by_id=conversations_by_id,
        now=now,
        run_id=run_id,
    )

    send_result = None
    if not prepare_only:
        pending = gateway.query_pending_slack_outbox(project_id)
        send_result = _send_pending_outbox(
            client=client,
            gateway=gateway,
            project_id=project_id,
            pending=pending,
            now=now,
            dry_run_send=dry_run_send,
            config=config,
            selected_outbox_ids=selected_outbox_ids,
        )

    message = (
        f"Slack run {run_id} collected {len(collected['messages'])} messages and "
        f"persisted {len(drafts)} draft messages."
    )
    print(message)
    return CommandResult(
        exit_code=0,
        message=message,
        data={
            "run_id": run_id,
            "run_dir": str(run_dir),
            "reconciled_dir": str(reconciled_dir),
            "message_count": len(collected["messages"]),
            "draft_count": len(drafts),
            "no_message_count": len(codex_output.no_message_decisions),
            "continuity_note": codex_output.continuity_note,
            "draft_messages": [
                draft.model_dump(mode="json") for draft in codex_output.draft_messages
            ],
            "no_message_decisions": [
                decision.model_dump(mode="json")
                for decision in codex_output.no_message_decisions
            ],
            "prepare_only": prepare_only,
            "send_result": send_result,
        },
    )


def send_outbox_messages(
    *,
    db_path: str | Path,
    project_id: str,
    token_override: str | None = None,
    outbox_ids: Iterable[str] | None = None,
    dry_run_send: bool = False,
    service: Any | None = None,
    slack_client_factory: Any | None = None,
    now: dt.datetime | None = None,
) -> CommandResult:
    """Send selected pending Slack outbox messages for a project."""
    if now is None:
        now = dt.datetime.now(tz=dt.UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    gateway = ServiceGateway(service or _load_service(db_path))
    config = _normalize_config(gateway.query_slack_project_config(project_id), project_id)
    if config is None or not config.enabled:
        message = f"No enabled Slack config found for project {project_id}."
        print(message, file=sys.stderr)
        return CommandResult(exit_code=1, noop=True, message=message)

    token = _resolve_token(config, token_override=token_override)
    if token is None:
        message = _missing_token_message(config, project_id)
        print(message, file=sys.stderr)
        return CommandResult(exit_code=1, message=message)

    client = (slack_client_factory or _make_slack_client)(token)
    pending = gateway.query_pending_slack_outbox(project_id)
    send_result = _send_pending_outbox(
        client=client,
        gateway=gateway,
        project_id=project_id,
        pending=pending,
        now=now,
        dry_run_send=dry_run_send,
        config=config,
        selected_outbox_ids=outbox_ids,
    )
    message = (
        f"Slack outbox send completed: {send_result['sent']} sent, "
        f"{send_result['failed']} failed, {send_result['skipped']} skipped."
    )
    print(message)
    return CommandResult(exit_code=0, message=message, data=send_result)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(prog="python -m projdash.integrations.slack_bot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("--project-id", required=True)
    manifest_parser.add_argument("--name", default="ProjDash Slack Bot")

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--db", required=True)
    verify_parser.add_argument("--project-id", required=True)

    run_parser = subparsers.add_parser("run-once")
    run_parser.add_argument("--db", required=True)
    run_parser.add_argument("--project-id", required=True)
    run_parser.add_argument("--data-root", default="data")
    run_parser.add_argument("--codex-bin", default="codex")
    run_parser.add_argument("--codex-model")
    run_parser.add_argument("--prepare-only", action="store_true")
    run_parser.add_argument("--dry-run-send", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "manifest":
            return manifest(project_id=args.project_id, name=args.name).exit_code
        if args.command == "verify":
            return verify(db_path=args.db, project_id=args.project_id).exit_code
        if args.command == "run-once":
            return run_once(
                db_path=args.db,
                project_id=args.project_id,
                data_root=args.data_root,
                codex_bin=args.codex_bin,
                codex_model=args.codex_model,
                prepare_only=args.prepare_only,
                dry_run_send=args.dry_run_send,
            ).exit_code
    except IntegrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    raise AssertionError(f"Unhandled command {args.command!r}")


def _load_service(db_path: str | Path) -> Any:
    from projdash.service.service import ProjectService
    from projdash.service.storage import bootstrap_project_repository

    return ProjectService(bootstrap_project_repository(db_path))


def _make_slack_client(token: str) -> Any:
    try:
        from slack_sdk import WebClient
    except ImportError as exc:  # pragma: no cover - exercised only without dependency.
        raise IntegrationError(
            "slack_sdk is required for Slack integration. Install project dependencies."
        ) from exc
    return WebClient(token=token)


def _normalize_config(raw: Any, project_id: str) -> SlackProjectConfig | None:
    raw = _unwrap_service_result(raw)
    if raw is None:
        return None
    if isinstance(raw, SlackProjectConfig):
        return raw
    data = _coerce_mapping(raw)
    outer = dict(data)
    nested = data.get("config", data.get("slack_config"))
    if nested is not None:
        data = _coerce_mapping(nested)
        for key in ("project_start_at", "start_at"):
            if key not in data and key in outer:
                data[key] = outer[key]
    if not data:
        return None

    enabled = bool(data.get("enabled", True))
    token_env_var = data.get("token_env_var", data.get("bot_token_secret_ref"))
    start_value = data.get("start_at", data.get("project_start_at"))
    if start_value is None:
        start_at = dt.datetime.now(tz=dt.UTC)
    else:
        start_at = _parse_aware_datetime(start_value, field_name="start_at")

    channel_source = data.get("channels", data.get("channel_ids", []))
    channels = tuple(_normalize_channels(channel_source))
    if not channels and data.get("default_channel_id"):
        channels = (SlackChannelConfig(channel_id=str(data["default_channel_id"])),)
    resource_slack_map = tuple(
        _normalize_resource_slack_map(
            data.get(
                "resource_slack_map",
                outer.get(
                    "resource_mappings",
                    data.get("teammate_context", data.get("teammates", [])),
                ),
            )
        )
    )
    cursor_source = data.get("conversation_cursors", outer.get("collection_cursors", {}))
    conversation_cursors = _normalize_conversation_cursors(cursor_source)
    continuity_note = data.get("continuity_note", outer.get("continuity_note"))
    continuity_updated_value = data.get(
        "continuity_updated_at",
        outer.get("continuity_updated_at"),
    )
    continuity_updated_at = (
        _parse_aware_datetime(
            continuity_updated_value,
            field_name="continuity_updated_at",
        )
        if continuity_updated_value is not None
        else None
    )
    fetch_thread_replies = bool(data.get("fetch_thread_replies", True))
    return SlackProjectConfig(
        project_id=str(data.get("project_id", project_id)),
        enabled=enabled,
        token_env_var=str(token_env_var) if token_env_var is not None else None,
        start_at=start_at,
        channels=channels,
        resource_slack_map=resource_slack_map,
        conversation_cursors=conversation_cursors,
        continuity_note=str(continuity_note) if continuity_note else None,
        continuity_updated_at=continuity_updated_at,
        fetch_thread_replies=fetch_thread_replies,
        raw=data,
    )


def _normalize_conversation_cursors(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items() if value is not None}
    cursors = {}
    for item in raw or []:
        data = _coerce_mapping(item)
        conversation_id = data.get("conversation_id")
        latest_collected_ts = data.get("latest_collected_ts")
        if conversation_id and latest_collected_ts:
            cursors[str(conversation_id)] = str(latest_collected_ts)
    return cursors


def _config_with_project_start(
    config: SlackProjectConfig,
    agent_context: dict[str, Any],
) -> SlackProjectConfig:
    raw = config.raw or {}
    if raw.get("start_at") or raw.get("project_start_at"):
        return config
    project = agent_context.get("project") or {}
    project_start_at = project.get("start_at")
    if not project_start_at:
        return config
    return replace(
        config,
        start_at=_parse_aware_datetime(project_start_at, field_name="project.start_at"),
    )


def _normalize_channels(raw: Any) -> list[SlackChannelConfig]:
    if isinstance(raw, dict):
        raw = raw.values()
    channels = []
    for item in raw or []:
        if isinstance(item, str):
            channels.append(SlackChannelConfig(channel_id=item))
            continue
        data = _coerce_mapping(item)
        if not data or data.get("enabled", True) is False:
            continue
        channel_id = data.get("channel_id", data.get("id"))
        if channel_id:
            channels.append(
                SlackChannelConfig(
                    channel_id=str(channel_id),
                    name=data.get("name") or data.get("channel_name"),
                )
            )
    return channels


def _normalize_resource_slack_map(raw: Any) -> list[SlackMapEntry]:
    entries = []
    if isinstance(raw, dict):
        raw = [
            {"resource_id": resource_id, "slack_user_id": slack_user_id}
            for resource_id, slack_user_id in raw.items()
        ]
    for item in raw or []:
        data = _coerce_mapping(item)
        if not data or data.get("enabled", data.get("active", True)) is False:
            continue
        slack_user_id = data.get("slack_user_id", data.get("user_id"))
        resource_id = data.get("resource_id", data.get("teammate_id", slack_user_id))
        teammate_id = data.get("teammate_id", resource_id)
        if resource_id and teammate_id and slack_user_id:
            entries.append(
                SlackMapEntry(
                    resource_id=str(resource_id),
                    teammate_id=str(teammate_id),
                    slack_user_id=str(slack_user_id),
                    name=data.get("name") or data.get("display_name"),
                )
            )
    return entries


def _token_from_env(config: SlackProjectConfig) -> str | None:
    if not config.token_env_var:
        return None
    token = os.environ.get(config.token_env_var)
    if token is None or not token.strip():
        return None
    return token


def _resolve_token(
    config: SlackProjectConfig,
    *,
    token_override: str | None = None,
) -> str | None:
    if token_override is not None and token_override.strip():
        return token_override.strip()
    return _token_from_env(config)


def _missing_token_message(config: SlackProjectConfig, project_id: str) -> str:
    if config.token_env_var:
        return (
            "Slack token was not provided and env var "
            f"{config.token_env_var!r} is not set for project {project_id}."
        )
    return (
        "Slack token was not provided and no token env var is configured for "
        f"project {project_id}."
    )


def _paged_users_list(client: Any) -> list[dict[str, Any]]:
    users = []
    cursor = None
    while True:
        kwargs = {"limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        response = client.users_list(**kwargs)
        users.extend(response.get("members", []))
        cursor = _next_cursor(response)
        if not cursor:
            return users


def _normalize_slack_user(raw: dict[str, Any]) -> SlackUser:
    profile = raw.get("profile") or {}
    user_id = raw.get("id")
    if not user_id:
        raise IntegrationError(f"Slack user is missing an id: {raw}")
    return SlackUser(
        slack_user_id=str(user_id),
        name=raw.get("name"),
        real_name=raw.get("real_name"),
        display_name=profile.get("display_name") or profile.get("display_name_normalized"),
        email=profile.get("email"),
        timezone=raw.get("tz"),
        deleted=bool(raw.get("deleted", False)),
        is_bot=bool(raw.get("is_bot", False)),
        is_app_user=bool(raw.get("is_app_user", False)),
    )


def _eligible_conversations(client: Any, config: SlackProjectConfig) -> list[dict[str, Any]]:
    configured_channels = {channel.channel_id for channel in config.channels}
    mapped_users = config.slack_user_ids
    conversations = []
    for channel in _paged_conversations_list(client):
        channel_id = channel.get("id")
        if not channel_id:
            continue
        if channel.get("is_im"):
            if channel.get("user") in mapped_users:
                conversations.append(dict(channel))
            continue
        if configured_channels and channel_id not in configured_channels:
            continue
        is_private = bool(channel.get("is_private"))
        is_member = bool(channel.get("is_member", is_private))
        if is_member:
            conversations.append(dict(channel))
    return conversations


def _paged_conversations_list(client: Any) -> list[dict[str, Any]]:
    channels = []
    cursor = None
    while True:
        kwargs = {
            "types": "public_channel,private_channel,im",
            "exclude_archived": True,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor
        response = client.conversations_list(**kwargs)
        channels.extend(response.get("channels", []))
        cursor = _next_cursor(response)
        if not cursor:
            return channels


def _collect_messages(
    client: Any,
    config: SlackProjectConfig,
    conversations: list[dict[str, Any]],
    now: dt.datetime,
) -> dict[str, Any]:
    messages = []
    cursors = {}
    seen_keys = set()
    fallback_cursor = _slack_ts(now)
    for conversation in conversations:
        conversation_id = conversation["id"]
        oldest = config.conversation_cursors.get(conversation_id, _slack_ts(config.start_at))
        conversation_messages = _paged_conversation_history(client, conversation_id, oldest)
        max_ts = oldest
        fetched_thread_ts: set[str] = set()
        for message in conversation_messages:
            normalized = _normalize_message(conversation, message, config)
            key = normalized["message_key"]
            if key not in seen_keys:
                seen_keys.add(key)
                messages.append(normalized)
                max_ts = _max_slack_ts(max_ts, normalized["ts"])
            if config.fetch_thread_replies and _has_thread_replies(message):
                thread_ts = str(message["thread_ts"])
                fetched_thread_ts.add(thread_ts)
                thread_cursor_key = _thread_cursor_key(conversation_id, thread_ts)
                thread_oldest = config.conversation_cursors.get(thread_cursor_key, oldest)
                cursors[thread_cursor_key] = _collect_thread_replies(
                    client=client,
                    conversation=conversation,
                    thread_ts=thread_ts,
                    oldest=thread_oldest,
                    fallback_cursor=fallback_cursor,
                    config=config,
                    seen_keys=seen_keys,
                    messages=messages,
                )
        if config.fetch_thread_replies:
            for thread_ts, thread_oldest in _thread_cursors_for_conversation(
                config,
                conversation_id,
            ).items():
                if thread_ts in fetched_thread_ts:
                    continue
                cursors[_thread_cursor_key(conversation_id, thread_ts)] = (
                    _collect_thread_replies(
                        client=client,
                        conversation=conversation,
                        thread_ts=thread_ts,
                        oldest=thread_oldest,
                        fallback_cursor=fallback_cursor,
                        config=config,
                        seen_keys=seen_keys,
                        messages=messages,
                    )
                )
        cursors[conversation_id] = max_ts if max_ts != oldest else fallback_cursor
    messages.sort(key=lambda item: (item["ts"], item["conversation_id"]))
    return {"messages": messages, "cursors": cursors}


def _collect_thread_replies(
    *,
    client: Any,
    conversation: dict[str, Any],
    thread_ts: str,
    oldest: str,
    fallback_cursor: str,
    config: SlackProjectConfig,
    seen_keys: set[str],
    messages: list[dict[str, Any]],
) -> str:
    conversation_id = conversation["id"]
    max_ts = oldest
    for reply in _paged_thread_replies(
        client,
        conversation_id,
        thread_ts,
        oldest,
    ):
        normalized_reply = _normalize_message(conversation, reply, config)
        reply_key = normalized_reply["message_key"]
        if reply_key not in seen_keys:
            seen_keys.add(reply_key)
            messages.append(normalized_reply)
            max_ts = _max_slack_ts(max_ts, normalized_reply["ts"])
    return max_ts if max_ts != oldest else fallback_cursor


def _thread_cursors_for_conversation(
    config: SlackProjectConfig,
    conversation_id: str,
) -> dict[str, str]:
    prefix = f"{conversation_id}:thread:"
    return {
        key.removeprefix(prefix): value
        for key, value in config.conversation_cursors.items()
        if key.startswith(prefix)
    }


def _thread_cursor_key(conversation_id: str, thread_ts: str) -> str:
    return f"{conversation_id}:thread:{thread_ts}"


def _paged_conversation_history(
    client: Any,
    conversation_id: str,
    oldest: str,
) -> list[dict[str, Any]]:
    messages = []
    cursor = None
    while True:
        kwargs = {
            "channel": conversation_id,
            "oldest": oldest,
            "inclusive": False,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor
        response = client.conversations_history(**kwargs)
        messages.extend(response.get("messages", []))
        cursor = _next_cursor(response)
        if not cursor:
            return messages


def _paged_thread_replies(
    client: Any,
    conversation_id: str,
    thread_ts: str,
    oldest: str,
) -> list[dict[str, Any]]:
    messages = []
    cursor = None
    while True:
        kwargs = {
            "channel": conversation_id,
            "ts": thread_ts,
            "oldest": oldest,
            "inclusive": False,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor
        response = client.conversations_replies(**kwargs)
        messages.extend(response.get("messages", []))
        cursor = _next_cursor(response)
        if not cursor:
            return messages


def _normalize_message(
    conversation: dict[str, Any],
    message: dict[str, Any],
    config: SlackProjectConfig,
) -> dict[str, Any]:
    conversation_id = conversation["id"]
    user_id = message.get("user") or message.get("bot_id") or "unknown"
    map_by_user = {entry.slack_user_id: entry for entry in config.resource_slack_map}
    mapped = map_by_user.get(user_id)
    ts = str(message.get("ts", ""))
    thread_ts = message.get("thread_ts")
    return {
        "message_key": f"{conversation_id}:{ts}",
        "conversation_id": conversation_id,
        "conversation_name": conversation.get("name"),
        "conversation_type": "im" if conversation.get("is_im") else "channel",
        "user_id": user_id,
        "resource_id": mapped.resource_id if mapped else None,
        "teammate_id": mapped.teammate_id if mapped else None,
        "text": message.get("text", ""),
        "ts": ts,
        "message_at": _datetime_from_slack_ts(ts).isoformat() if ts else None,
        "thread_ts": thread_ts,
        "is_thread_reply": bool(thread_ts and thread_ts != ts),
        "raw": message,
    }


def _write_evidence_files(
    *,
    run_dir: Path,
    project_id: str,
    run_id: str,
    config: SlackProjectConfig,
    agent_context: dict[str, Any],
    conversations: list[dict[str, Any]],
    collected: dict[str, Any],
    now: dt.datetime,
) -> None:
    messages = collected["messages"]
    with (run_dir / "raw_messages.jsonl").open("w", encoding="utf-8") as handle:
        for message in messages:
            handle.write(json.dumps(message, sort_keys=True) + "\n")

    lines = [f"# Slack collection {run_id}", ""]
    for message in messages:
        speaker = message.get("teammate_id") or message.get("user_id")
        at = message.get("message_at") or message.get("ts")
        lines.extend(
            [
                f"## {message['message_key']}",
                f"- Conversation: {message['conversation_name'] or message['conversation_id']}",
                f"- Speaker: {speaker}",
                f"- At: {at}",
                "",
                message.get("text", ""),
                "",
            ]
        )
    (run_dir / "messages.md").write_text("\n".join(lines), encoding="utf-8")

    manifest_payload = {
        "project_id": project_id,
        "run_id": run_id,
        "collected_at": now.isoformat(),
        "message_count": len(messages),
        "conversations": conversations,
        "cursors": collected["cursors"],
        "configured_channels": [channel.as_dict() for channel in config.channels],
        "fetch_thread_replies": config.fetch_thread_replies,
    }
    _write_json(run_dir / "collection_manifest.json", manifest_payload)
    _write_json(run_dir / "agent_context.json", agent_context)
    _write_json(
        run_dir / "continuity_note.json",
        {
            "project_id": project_id,
            "current_time": now.isoformat(),
            "previous_continuity_note": config.continuity_note,
            "previous_continuity_updated_at": (
                config.continuity_updated_at.isoformat()
                if config.continuity_updated_at
                else None
            ),
        },
    )
    _write_json(
        run_dir / "pm_signal_context.json",
        _pm_signal_context(
            project_id=project_id,
            now=now,
            config=config,
            agent_context=agent_context,
            collected=collected,
        ),
    )
    _write_json(
        run_dir / "resource_slack_map.json",
        [entry.as_dict() for entry in config.resource_slack_map],
    )
    _write_json(
        run_dir / "teammate_context.json",
        {
            "teammates": [
                {
                    "teammate_id": entry.teammate_id,
                    "resource_id": entry.resource_id,
                    "slack_user_id": entry.slack_user_id,
                    "name": entry.name,
                }
                for entry in config.resource_slack_map
            ]
        },
    )


def _pm_signal_context(
    *,
    project_id: str,
    now: dt.datetime,
    config: SlackProjectConfig,
    agent_context: dict[str, Any],
    collected: dict[str, Any],
) -> dict[str, Any]:
    graph = agent_context.get("graph") or {}
    nodes = graph.get("nodes") or []
    blockers = agent_context.get("blockers") or []
    milestones = agent_context.get("milestones") or []
    mapped_resource_ids = {entry.resource_id for entry in config.resource_slack_map}
    mapped_slack_users = {entry.slack_user_id for entry in config.resource_slack_map}
    node_signals = [
        _pm_process_signal(node, now, mapped_resource_ids)
        for node in nodes
        if isinstance(node, dict)
    ]
    critical = [
        item for item in node_signals if item["criticality"] == "critical"
    ]
    overdue_start = [
        item
        for item in node_signals
        if item["not_started"] and item["days_until_latest_start"] is not None
        and item["days_until_latest_start"] <= 0
    ]
    overdue_finish = [
        item
        for item in node_signals
        if not item["done"] and item["days_until_latest_finish"] is not None
        and item["days_until_latest_finish"] <= 0
    ]
    upcoming_critical = [
        item
        for item in critical
        if item["not_started"] and item["days_until_earliest_start"] is not None
        and -0.25 <= item["days_until_earliest_start"] <= 3
    ]
    high_slack = [
        item
        for item in node_signals
        if item["slack_days"] is not None and item["slack_days"] >= 5
    ]
    in_progress = [item for item in node_signals if item["status"] == "in_progress"]
    unmapped_messages = [
        message["message_key"]
        for message in collected.get("messages", [])
        if message.get("user_id") and message.get("user_id") not in mapped_slack_users
    ]
    return {
        "project_id": project_id,
        "current_time": now.isoformat(),
        "previous_continuity_note": config.continuity_note,
        "message_count": len(collected.get("messages", [])),
        "mapped_teammates": [entry.as_dict() for entry in config.resource_slack_map],
        "pm_watchlist": {
            "critical_path": critical,
            "critical_starting_within_3_days": upcoming_critical,
            "past_latest_start_not_started": overdue_start,
            "past_latest_finish_not_done": overdue_finish,
            "in_progress": in_progress,
            "high_slack_processes": high_slack,
            "unstaked_mapped_resource_work": [
                item for item in node_signals if item["mapped_resource_needed_but_unstaked"]
            ],
            "multi_role_or_multi_resource_risk": [
                item for item in node_signals if item["coordination_risk"]
            ],
            "unmapped_slack_message_keys": unmapped_messages,
            "milestones_with_slippage_history": [
                milestone
                for milestone in milestones
                if isinstance(milestone, dict)
                and (milestone.get("slippage") or {}).get("snapshot_count")
            ],
        },
        "milestones": milestones,
        "open_blockers": [
            blocker
            for blocker in blockers
            if isinstance(blocker, dict) and not blocker.get("is_resolved_as_of")
        ],
        "agent_instruction_hints": [
            "Keep messages concise; prefer focused follow-ups over broad summaries.",
            "Ask each mapped teammate whether current in-progress work, blockers, "
            "and done definitions are accurately captured when the evidence is stale.",
            "Escalate critical-path uncertainty earlier than non-critical work.",
            "Use channel-visible messages when a teammate repeatedly does not respond "
            "or when coordination across resources is required.",
            "Update the continuity note with expected replies, due times, and what "
            "the next run should check.",
        ],
    }


def _pm_process_signal(
    node: dict[str, Any],
    now: dt.datetime,
    mapped_resource_ids: set[str],
) -> dict[str, Any]:
    dependency = node.get("dependency_only") or {}
    resource = node.get("resource_aware") or {}
    role_requirements = node.get("role_requirements") or []
    role_ids = [
        str(requirement.get("role_id"))
        for requirement in role_requirements
        if isinstance(requirement, dict) and requirement.get("role_id")
    ]
    staked_resource_ids = sorted(
        {
            str(resource_id)
            for resource_id in node.get("staked_resource_ids", []) or []
        }
        | {
            str(resource_id)
            for requirement in role_requirements
            if isinstance(requirement, dict)
            for resource_id in requirement.get("staked_resource_ids", []) or []
        }
    )
    eligible_resource_ids = sorted(
        {
            str(resource_id)
            for requirement in role_requirements
            if isinstance(requirement, dict)
            for resource_id in requirement.get("eligible_resource_ids", []) or []
        }
    )
    ls_at = _maybe_datetime(dependency.get("ls_at"))
    lf_at = _maybe_datetime(dependency.get("lf_at"))
    es_at = _maybe_datetime(dependency.get("es_at"))
    slack_hours = dependency.get("slack_hours")
    slack_days = None
    if slack_hours is not None:
        try:
            slack_days = round(float(slack_hours) / 24, 2)
        except (TypeError, ValueError):
            slack_days = None
    status = str(node.get("status") or "")
    done = status in {"done", "canceled"} or bool(node.get("finished_at"))
    not_started = not node.get("started_at") and status not in {
        "in_progress",
        "paused",
        "done",
        "canceled",
    }
    required_resource_count = sum(
        int(requirement.get("required_resource_count") or 1)
        for requirement in role_requirements
        if isinstance(requirement, dict)
    )
    return {
        "process_id": node.get("process_id"),
        "process_symbol": node.get("process_symbol"),
        "name": node.get("name"),
        "status": status,
        "computed_status": node.get("computed_status"),
        "criticality": dependency.get("criticality_label"),
        "slack_days": slack_days,
        "days_until_earliest_start": _days_until(es_at, now),
        "days_until_latest_start": _days_until(ls_at, now),
        "days_until_latest_finish": _days_until(lf_at, now),
        "not_started": not_started,
        "done": done,
        "started_at": node.get("started_at"),
        "finished_at": node.get("finished_at"),
        "definition_of_done": node.get("description"),
        "role_ids": role_ids,
        "staked_resource_ids": staked_resource_ids,
        "eligible_resource_ids": eligible_resource_ids,
        "required_resource_count": required_resource_count,
        "resource_aware": resource,
        "coordination_risk": len(role_ids) > 1 or required_resource_count > 1,
        "mapped_resource_needed_but_unstaked": (
            bool(mapped_resource_ids)
            and bool(role_ids)
            and not set(staked_resource_ids).intersection(mapped_resource_ids)
        ),
    }


def _maybe_datetime(value: Any) -> dt.datetime | None:
    if value is None or isinstance(value, dt.datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_until(value: dt.datetime | None, now: dt.datetime) -> float | None:
    if value is None:
        return None
    return round((value - now).total_seconds() / 86400, 2)


def _cursor_record_metadata(
    cursor_id: str,
    conversations_by_id: dict[str, dict[str, Any]],
) -> dict[str, str | None]:
    thread_marker = ":thread:"
    if thread_marker in cursor_id:
        conversation_id, thread_ts = cursor_id.split(thread_marker, 1)
        conversation = conversations_by_id.get(conversation_id, {})
        name = conversation.get("name") or conversation_id
        return {
            "conversation_type": "thread",
            "conversation_name": f"{name} thread {thread_ts}",
        }
    conversation = conversations_by_id.get(cursor_id, {})
    return {
        "conversation_type": "im" if conversation.get("is_im") else "channel",
        "conversation_name": conversation.get("name"),
    }


def _record_collected_cursors(
    *,
    gateway: ServiceGateway,
    project_id: str,
    collected: dict[str, Any],
    conversations_by_id: dict[str, dict[str, Any]],
    now: dt.datetime,
    run_id: str,
) -> None:
    for conversation_id, cursor in collected["cursors"].items():
        cursor_metadata = _cursor_record_metadata(conversation_id, conversations_by_id)
        gateway.record_slack_collection_cursor(
            project_id=project_id,
            conversation_id=conversation_id,
            latest_collected_ts=cursor,
            run_id=run_id,
            updated_at=now,
            conversation_type=cursor_metadata["conversation_type"],
            conversation_name=cursor_metadata["conversation_name"],
        )


def _codex_prompt(
    run_dir: Path,
    reconciled_dir: Path,
    *,
    db_path: str | Path,
    project_id: str,
) -> str:
    reconciled_dir.mkdir(parents=True, exist_ok=True)
    resolved_db_path = Path(db_path).expanduser().resolve()
    return (
        f"$projdash-project-manager from {run_dir} putting in {reconciled_dir} "
        "after processing. Use a sub-agent to review reconciliation choices "
        "before finalizing commands, draft messages, or no-message decisions.\n\n"
        f"Project id: {project_id}\n"
        f"ProjDash database path: {resolved_db_path}\n"
        "Use ProjectDashboard service command/query envelopes against this "
        "database. Apply validated project-management updates directly to the "
        "service state during this run; do not substitute proposed commands in "
        "the JSON response for actual service updates. Summarize the applied "
        "or deliberately skipped service updates in project_updates.\n\n"
        "Read the Slack evidence files, continuity_note.json, and "
        "pm_signal_context.json in the input folder. Reconcile only the facts "
        "that are clear enough for ProjectDashboard service commands. Draft "
        "teammate Slack follow-up messages when the collected evidence, current "
        "time, prior continuity expectations, schedule risk, blockers, or project "
        "updates affect that teammate as a resource or through their roles. Treat "
        "the input folder as read-only: do not move, rename, delete, or rewrite "
        "any input evidence files or parent directories. Write any reconciled "
        "artifacts to the output folder only.\n\n"
        "Project-management checklist for this run:\n"
        "1. Check who should already have started work and whether ProjDash marks "
        "those processes started.\n"
        "2. Check processes past LS or LF and missing started/done state.\n"
        "3. Give advance notice for critical-path work and check in on start day.\n"
        "4. Seek estimate updates before and during complex critical-path work.\n"
        "5. Flag multi-role or multi-resource critical-path work for coordination.\n"
        "6. Use high-slack work to look for missing dependencies or false slack.\n"
        "7. Check whether role-assigned work really needs a specific staked resource.\n"
        "8. Check whether every teammate has a clear staked/current assignment.\n"
        "9. Prefer channel-visible follow-up when DM responsiveness is historically poor.\n"
        "10. Ask about new blockers and resolved blockers explicitly.\n"
        "11. Verify teammates are not doing uncaptured work outside the process graph.\n"
        "12. Capture holidays, exceptions, or recurring weekly capacity changes.\n"
        "13. Consider whether process topology is too coarse or too granular.\n"
        "14. Commit a schedule snapshot after meaningful PM message preparation.\n"
        "15. Track milestone slippage, not only whole-project slippage.\n"
        "16. Use DMs for focused accountability and the project channel for team coordination.\n"
        "17. Check definitions of done and whether the team agrees with them.\n"
        "18. Check role assignments and estimate buy-in before work starts.\n\n"
        "Keep drafted Slack messages concise and focused. Prefer small frequent "
        "messages over large status dumps. Always prepare a continuity_note for "
        "the next run: summarize what was decided, what replies or state changes "
        "are expected, who is expected to respond, where to follow up (DM or "
        "channel), and the expected time scale.\n\n"
        "Return only JSON with this exact shape:\n"
        "{\n"
        '  "summary": "brief run summary",\n'
        '  "project_updates": ["validated project update"],\n'
        '  "continuity_note": "handoff note for the next run with expectations and time scales",\n'
        '  "draft_messages": [\n'
        "    {\n"
        '      "teammate_id": "service teammate id",\n'
        '      "slack_user_id": "Slack user id",\n'
        '      "text": "message to send",\n'
        '      "reason": "brief source-backed reason",\n'
        '      "source_message_keys": ["conversation:ts"]\n'
        "    }\n"
        "  ],\n"
        '  "no_message_decisions": [\n'
        "    {\n"
        '      "teammate_id": "service teammate id",\n'
        '      "slack_user_id": "Slack user id",\n'
        '      "reason": "why no Slack message is needed",\n'
        '      "source_message_keys": ["conversation:ts"]\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Do not send Slack messages. Include a no_message_decisions entry for "
        "each mapped teammate that does not need an update. If there are no "
        "useful drafts, return an empty draft_messages array."
    )


def _parse_codex_output(stdout: str) -> CodexDraftOutput:
    text = stdout.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
        parsed = CodexDraftOutput.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise IntegrationError(f"codex exec did not return valid draft JSON: {exc}") from exc
    return parsed


def _parse_codex_drafts(stdout: str) -> list[SlackDraftMessage]:
    parsed = _parse_codex_output(stdout)
    return parsed.draft_messages


def _validate_codex_reconciliation_output(
    codex_output: CodexDraftOutput,
    config: SlackProjectConfig,
) -> None:
    mapping_by_slack_user = {
        entry.slack_user_id: entry for entry in config.resource_slack_map
    }
    if not mapping_by_slack_user:
        return

    covered_slack_users: set[str] = set()
    for draft in codex_output.draft_messages:
        _validate_codex_teammate_reference(
            mapping_by_slack_user,
            draft.slack_user_id,
            draft.teammate_id,
            "draft",
        )
        covered_slack_users.add(draft.slack_user_id)
    for decision in codex_output.no_message_decisions:
        _validate_codex_teammate_reference(
            mapping_by_slack_user,
            decision.slack_user_id,
            decision.teammate_id,
            "no-message decision",
        )
        covered_slack_users.add(decision.slack_user_id)

    missing = sorted(set(mapping_by_slack_user) - covered_slack_users)
    if missing:
        raise IntegrationError(
            "Codex output omitted draft/no-message decisions for mapped Slack "
            f"users: {', '.join(missing)}."
        )


def _validate_codex_teammate_reference(
    mapping_by_slack_user: dict[str, SlackMapEntry],
    slack_user_id: str,
    teammate_id: str,
    output_kind: str,
) -> None:
    mapping = mapping_by_slack_user.get(slack_user_id)
    if mapping is None:
        raise IntegrationError(
            f"Codex {output_kind} targeted unmapped Slack user {slack_user_id!r}."
        )
    if teammate_id not in {mapping.teammate_id, mapping.resource_id}:
        raise IntegrationError(
            f"Codex {output_kind} teammate id does not match the configured "
            f"resource mapping for Slack user {slack_user_id!r}."
        )


def _outbox_messages_from_drafts(
    drafts: list[SlackDraftMessage],
    *,
    config: SlackProjectConfig,
    run_id: str,
    created_at: dt.datetime,
) -> list[dict[str, Any]]:
    mapping_by_slack_user = {
        entry.slack_user_id: entry for entry in config.resource_slack_map
    }
    messages = []
    for draft in drafts:
        mapping = mapping_by_slack_user.get(draft.slack_user_id)
        if mapping is None:
            raise IntegrationError(
                f"Codex draft targeted unmapped Slack user {draft.slack_user_id!r}."
            )
        if draft.teammate_id not in {mapping.teammate_id, mapping.resource_id}:
            raise IntegrationError(
                "Codex draft teammate id does not match the configured "
                f"resource mapping for Slack user {draft.slack_user_id!r}."
            )
        messages.append(
            {
                "resource_id": mapping.resource_id,
                "slack_user_id": draft.slack_user_id,
                "body": draft.text,
                "content_hash": _content_hash(draft.text),
                "run_id": run_id,
                "created_at": created_at.isoformat(),
            }
        )
    return messages


def _content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _codex_subprocess_env(config: SlackProjectConfig) -> dict[str, str]:
    env = dict(os.environ)
    if config.token_env_var:
        env.pop(config.token_env_var, None)
    return env


def _send_pending_outbox(
    *,
    client: Any,
    gateway: ServiceGateway,
    project_id: str,
    pending: list[dict[str, Any]],
    now: dt.datetime,
    dry_run_send: bool,
    config: SlackProjectConfig,
    selected_outbox_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    selected = (
        {str(outbox_id) for outbox_id in selected_outbox_ids}
        if selected_outbox_ids is not None
        else None
    )
    seen_selected: set[str] = set()
    result = {
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "dry_run": 0,
        "selected_not_found": [],
    }
    allowed_slack_user_ids = config.slack_user_ids
    for row in pending:
        outbox_id = str(row.get("outbox_id", row.get("message_id", "")))
        if not outbox_id:
            raise IntegrationError(f"Pending Slack outbox row is missing an id: {row}")
        if selected is not None and outbox_id not in selected:
            result["skipped"] += 1
            continue
        if selected is not None:
            seen_selected.add(outbox_id)
        channel = row.get("channel_id") or row.get("slack_channel_id")
        slack_user_id = row.get("slack_user_id")
        text = row.get("text") or row.get("body")
        if slack_user_id not in allowed_slack_user_ids:
            gateway.mark_slack_outbox_failed(
                project_id=project_id,
                outbox_id=outbox_id,
                failed_at=now,
                error="Pending Slack outbox row targets an unmapped Slack user.",
            )
            result["failed"] += 1
            continue
        if not text or (not channel and not slack_user_id):
            gateway.mark_slack_outbox_failed(
                project_id=project_id,
                outbox_id=outbox_id,
                failed_at=now,
                error="Pending Slack outbox row is missing channel or text.",
            )
            result["failed"] += 1
            continue
        if dry_run_send:
            result["dry_run"] += 1
            continue
        try:
            if not channel:
                channel = _open_dm_channel(client, str(slack_user_id))
            response = client.chat_postMessage(channel=channel, text=text)
        except Exception as exc:
            gateway.mark_slack_outbox_failed(
                project_id=project_id,
                outbox_id=outbox_id,
                failed_at=now,
                error=_slack_error_message(exc),
            )
            result["failed"] += 1
            continue
        gateway.mark_slack_outbox_sent(
            project_id=project_id,
            outbox_id=outbox_id,
            sent_at=now,
            slack_channel_id=response.get("channel", channel),
            slack_message_ts=response.get("ts"),
        )
        result["sent"] += 1
    if selected is not None:
        result["selected_not_found"] = sorted(selected - seen_selected)
    return result


def _open_dm_channel(client: Any, slack_user_id: str) -> str:
    response = client.conversations_open(users=slack_user_id)
    channel = response.get("channel") or {}
    channel_id = channel.get("id")
    if not channel_id:
        raise IntegrationError(f"Slack did not return a DM channel for {slack_user_id}.")
    return str(channel_id)


def _call_with_supported_kwargs(method: Any, kwargs: dict[str, Any]) -> Any:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(**kwargs)
    parameters = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return method(**kwargs)
    accepted = {
        name: value
        for name, value in kwargs.items()
        if name in parameters
        or (
            name == "project_id"
            and len(parameters) == 1
            and next(iter(parameters.values())).kind
            in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        )
    }
    if len(parameters) == 1 and "project_id" in kwargs:
        parameter_name = next(iter(parameters))
        accepted = {parameter_name: kwargs["project_id"]}
    return method(**accepted)


def _unwrap_service_result(value: Any) -> Any:
    if value is None:
        return None
    ok = getattr(value, "ok", None)
    if ok is False:
        error = getattr(value, "error", None)
        raise IntegrationError(f"Service call failed: {error}")
    data = getattr(value, "data", None)
    if data is not None:
        return data
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value


def _jsonable_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in payload.items():
        if isinstance(value, dt.datetime):
            result[key] = value.isoformat()
        elif isinstance(value, list):
            result[key] = [
                item.isoformat() if isinstance(item, dt.datetime) else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _parse_aware_datetime(value: Any, *, field_name: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _safe_path_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "project"


def _slack_ts(value: dt.datetime) -> str:
    return f"{value.timestamp():.6f}"


def _datetime_from_slack_ts(value: str) -> dt.datetime:
    return dt.datetime.fromtimestamp(float(value), tz=dt.UTC)


def _max_slack_ts(left: str, right: str) -> str:
    return right if float(right) > float(left) else left


def _has_thread_replies(message: dict[str, Any]) -> bool:
    thread_ts = message.get("thread_ts")
    return bool(thread_ts and int(message.get("reply_count", 0) or 0) > 0)


def _next_cursor(response: dict[str, Any]) -> str | None:
    metadata = response.get("response_metadata") or {}
    cursor = metadata.get("next_cursor")
    return cursor or None


def _slack_error_message(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            error = response.get("error")
            needed = response.get("needed")
            if needed:
                return f"{error}; needed scopes: {needed}"
            if error:
                return str(error)
        except AttributeError:
            pass
    return str(exc)


if __name__ == "__main__":
    raise SystemExit(main())
