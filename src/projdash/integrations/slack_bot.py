"""Per-project Slack collection and Codex draft runner."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

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
MAX_CONTINUITY_NOTE_CHARS = 4096


class IntegrationError(RuntimeError):
    """Raised when the Slack integration cannot complete the requested step."""


class PMEvidenceClaim(BaseModel):
    """Claim that a draft satisfies a verifiable PM communication obligation."""

    model_config = ConfigDict(extra="forbid")

    evidence_type: Literal[
        "process_full_update",
        "process_pre_start_3_day",
        "process_pre_start_24_hour",
        "process_overdue_checkin",
        "process_in_progress_checkin",
        "resource_assignment_review",
        "message_receipt_ack",
        "project_update_notice",
    ]
    resource_id: str | None = Field(default=None, min_length=1)
    process_id: str | None = Field(default=None, min_length=1)
    process_symbol: str | None = Field(default=None, min_length=1)
    obligation_id: str | None = Field(default=None, min_length=1)
    content_hash: str | None = Field(default=None, min_length=1)
    evidence_note: str | None = Field(default=None, min_length=1)


class SlackDraftMessage(BaseModel):
    """Validated teammate draft message emitted by Codex."""

    model_config = ConfigDict(extra="forbid")

    teammate_id: str = Field(min_length=1)
    slack_user_id: str = Field(min_length=1)
    message_markdown: str = Field(min_length=1)
    reason: str | None = None
    source_message_keys: list[str] = Field(default_factory=list)
    pm_evidence_claims: list[PMEvidenceClaim] = Field(default_factory=list)

    @field_validator("message_markdown")
    @classmethod
    def _strip_message_markdown(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Draft message_markdown must not be blank.")
        _validate_teammate_message_text(stripped)
        return stripped


class SlackTeamChannelDraftMessage(BaseModel):
    """Validated project-channel draft emitted by Codex."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    channel_name: str | None = Field(default=None, min_length=1)
    message_markdown: str = Field(min_length=1)
    reason: str | None = None
    source_message_keys: list[str] = Field(default_factory=list)
    pm_evidence_claims: list[PMEvidenceClaim] = Field(default_factory=list)

    @field_validator("message_markdown")
    @classmethod
    def _strip_message_markdown(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Draft message_markdown must not be blank.")
        _validate_teammate_message_text(stripped)
        return stripped


class SlackNoMessageDecision(BaseModel):
    """Validated decision that no teammate Slack follow-up is needed."""

    model_config = ConfigDict(extra="forbid")

    teammate_id: str = Field(min_length=1)
    slack_user_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    source_message_keys: list[str] = Field(default_factory=list)


class PMEvidenceLineAnswer(BaseModel):
    """Codex answer to one service-prepared PM evidence question."""

    model_config = ConfigDict(extra="forbid")

    evidence_line_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    answer: Literal["yes", "no"]
    reason: str = Field(min_length=1)
    outcome: str | None = Field(default=None, min_length=1)
    update_intent: str | None = Field(default=None, min_length=1)
    source_keys: list[str] = Field(default_factory=list)

    @field_validator("answer", mode="before")
    @classmethod
    def _normalize_answer(cls, value: Any) -> str:
        answer = str(value).strip().lower()
        if answer not in {"yes", "no"}:
            raise ValueError("answer must be Yes/No.")
        return answer


class PMReviewerNote(BaseModel):
    """Reviewer-pass finding for the PM-flow output."""

    model_config = ConfigDict(extra="forbid")

    reviewer: str | None = Field(default=None, min_length=1)
    status: Literal["approved", "needs_changes", "noted"] = "noted"
    note: str = Field(min_length=1)
    required_changes: list[str] = Field(default_factory=list)


class SlackContinuityNote(BaseModel):
    """Simple continuity handoff required from the Codex PM runner."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 3
    summary: str | None = Field(default=None, min_length=1)
    generated_at: str | None = Field(default=None, min_length=1)
    note: str | dict[str, Any] | None = None
    next_run_focus: list[str] = Field(default_factory=list)


class CodexDraftOutput(BaseModel):
    """Schema required from the Codex runner."""

    model_config = ConfigDict(extra="forbid")

    summary: str | None = None
    evidence_line_answers: list[PMEvidenceLineAnswer] = Field(default_factory=list)
    reviewer_notes: list[PMReviewerNote] = Field(default_factory=list)
    project_updates: list[str] = Field(default_factory=list)
    continuity_note: SlackContinuityNote | str
    draft_messages: list[SlackDraftMessage] = Field(default_factory=list)
    team_channel_draft_messages: list[SlackTeamChannelDraftMessage] = Field(
        default_factory=list,
    )
    no_message_decisions: list[SlackNoMessageDecision] = Field(default_factory=list)


INTERNAL_TEAMMATE_MESSAGE_PATTERNS = (
    re.compile(r"\bprojdash\b", re.IGNORECASE),
    re.compile(r"\bdashboard\b", re.IGNORECASE),
    re.compile(r"\bproject manager tools?\b", re.IGNORECASE),
    re.compile(r"\bprocess graph\b", re.IGNORECASE),
    re.compile(r"\bgraph\b", re.IGNORECASE),
    re.compile(r"\bnode\b", re.IGNORECASE),
    re.compile(r"\bcritical path\b", re.IGNORECASE),
    re.compile(r"\bLS\b"),
    re.compile(r"\bLF\b"),
    re.compile(r"\bES\b"),
    re.compile(r"\bEF\b"),
    re.compile(r"\bschedule snapshot\b", re.IGNORECASE),
    re.compile(r"\bmilestone slippage\b", re.IGNORECASE),
    re.compile(r"\bresource[- ]aware\b", re.IGNORECASE),
    re.compile(r"\bsensitivity\b", re.IGNORECASE),
    re.compile(r"\bschedule buffer\b", re.IGNORECASE),
    re.compile(r"\bslack\b", re.IGNORECASE),
    re.compile(r"\bresource id\b", re.IGNORECASE),
    re.compile(r"\brole id\b", re.IGNORECASE),
)


def _validate_teammate_message_text(text: str) -> None:
    """Reject teammate-facing drafts that rely on ProjDash/UI terminology."""
    for pattern in INTERNAL_TEAMMATE_MESSAGE_PATTERNS:
        if pattern.search(text):
            raise ValueError(
                "Draft text must be self-contained and avoid internal "
                "project-management terminology."
            )


def _iter_slack_block_text(value: Any) -> Iterable[str]:
    """Yield human-visible text fields from a generic Slack Block Kit payload."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"text", "alt_text", "title"} and isinstance(item, str):
                yield item
            else:
                yield from _iter_slack_block_text(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_slack_block_text(item)


def _validate_slack_blocks(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate Block Kit shape and teammate-facing visible text."""
    if len(value) > 50:
        raise ValueError("Slack messages may contain at most 50 blocks.")
    for index, block in enumerate(value):
        block_type = block.get("type")
        if not isinstance(block_type, str) or not block_type.strip():
            raise ValueError(f"Slack block {index} must include a non-empty type.")
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Slack blocks must be JSON-serializable.") from exc
    for text in _iter_slack_block_text(value):
        if text.strip():
            _validate_teammate_message_text(text)
    return value


def _chunk_slack_text(text: str, max_chars: int = 2900) -> list[str]:
    """Split fallback text into Slack section-sized chunks."""
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


def _plain_text_from_markdown(markdown: str) -> str:
    """Create plain text for Slack header fields from a markdown heading."""
    text = re.sub(r"^#{1,6}\s+", "", markdown.strip())
    text = re.sub(r"[*_`~]", "", text)
    return text.strip()


def _blocks_for_draft(message_markdown: str) -> list[dict[str, Any]]:
    """Render agent-authored markdown into Slack Block Kit blocks."""
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n{2,}", message_markdown.strip())
        if paragraph.strip()
    ] or [message_markdown.strip()]
    blocks: list[dict[str, Any]] = []
    for paragraph in paragraphs:
        if paragraph.strip() == "---":
            blocks.append({"type": "divider"})
            if len(blocks) >= 50:
                return _validate_slack_blocks(blocks)
            continue
        lines = paragraph.splitlines()
        heading_match = re.match(r"^(#{1,3})\s+(.+)$", lines[0].strip())
        if heading_match is not None:
            heading_text = _plain_text_from_markdown(lines[0])
            if heading_text and len(heading_text) <= 150:
                blocks.append(
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": heading_text},
                    }
                )
            elif heading_text:
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*{heading_text}*"},
                    }
                )
            if len(blocks) >= 50:
                return _validate_slack_blocks(blocks)
            paragraph = "\n".join(lines[1:]).strip()
            if not paragraph:
                continue
        for chunk in _chunk_slack_text(paragraph):
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": chunk},
                }
            )
            if len(blocks) >= 50:
                return _validate_slack_blocks(blocks)
    return _validate_slack_blocks(blocks)


def _continuity_note_storage_text(note: SlackContinuityNote | str) -> str:
    """Serialize structured continuity in the existing string storage column."""
    if isinstance(note, str):
        return note[:MAX_CONTINUITY_NOTE_CHARS]
    payload = note.model_dump(mode="json", exclude_none=True)
    if payload.get("next_run_focus") == []:
        payload.pop("next_run_focus")
    return json.dumps(payload, indent=2, sort_keys=True)[:MAX_CONTINUITY_NOTE_CHARS]


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
            resource_schedule_backend="mcts",
            include_resource_sensitivity=True,
            resource_schedule_sensitivity_backend="mcts",
            resource_schedule_sensitivity_workers=1,
            resource_schedule_sensitivity_process_pool=False,
        )
        return data or {}

    def query_pm_communication_protocol(
        self,
        project_id: str,
        now: dt.datetime,
    ) -> dict[str, Any]:
        data = self._optional_call(
            "query_pm_communication_protocol",
            query_action="query_pm_communication_protocol",
            project_id=project_id,
            as_of=now,
            now=now,
            resource_schedule_backend="mcts",
            include_satisfied=True,
        )
        return data or {}

    def query_pm_markdown_context(
        self,
        project_id: str,
        now: dt.datetime,
    ) -> dict[str, Any] | str:
        """Return service-prepared PM flow context when this checkout supports it."""
        payload = {
            "project_id": project_id,
            "as_of": now,
            "now": now,
            "resource_schedule_backend": "mcts",
            "include_resource_sensitivity": True,
            "resource_schedule_sensitivity_backend": "mcts",
            "resource_schedule_sensitivity_workers": 1,
            "resource_schedule_sensitivity_process_pool": False,
        }
        for method_name, query_action in (
            ("query_pm_markdown_context", "query_pm_markdown_context"),
            ("query_pm_agent_context", "query_pm_agent_context"),
            ("query_pm_flow_context", "query_pm_flow_context"),
        ):
            data = self._optional_call(
                method_name,
                query_action=query_action,
                **payload,
            )
            if data:
                return data
        return {}

    def query_pm_agent_context(
        self,
        project_id: str,
        now: dt.datetime,
    ) -> dict[str, Any] | str:
        """Backward-compatible alias for older runner call sites."""
        return self.query_pm_markdown_context(project_id, now)

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
        continuity_note: str | None,
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

    def query_slack_outbox(
        self,
        project_id: str,
        statuses: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"project_id": project_id}
        if statuses is not None:
            payload["statuses"] = statuses
        if limit is not None:
            payload["limit"] = limit
        data = self._optional_call(
            "query_slack_outbox",
            query_action="query_slack_outbox",
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

    def record_pm_communication_evidence(
        self,
        *,
        project_id: str,
        evidence_type: str,
        outbox_id: str,
        communicated_at: dt.datetime,
        resource_id: str | None = None,
        slack_user_id: str | None = None,
        slack_channel_id: str | None = None,
        process_id: str | None = None,
        process_symbol: str | None = None,
        obligation_id: str | None = None,
        run_id: str | None = None,
        content_hash: str | None = None,
        evidence_note: str | None = None,
    ) -> None:
        self._required_call(
            "record_pm_communication_evidence",
            command_action="record_pm_communication_evidence",
            project_id=project_id,
            evidence_type=evidence_type,
            outbox_id=outbox_id,
            communicated_at=communicated_at,
            resource_id=resource_id,
            slack_user_id=slack_user_id,
            slack_channel_id=slack_channel_id,
            process_id=process_id,
            process_symbol=process_symbol,
            obligation_id=obligation_id,
            run_id=run_id,
            content_hash=content_hash,
            evidence_note=evidence_note,
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
    config = _config_with_bot_identity(config, client)
    project_data_dir = Path(data_root) / _safe_path_segment(project_id)
    run_dir = (
        project_data_dir
        / "unreconciled"
        / "slack"
        / _safe_path_segment(run_id)
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    manual_notes_source_dir = project_data_dir / "unreconciled" / "manual_notes"
    manual_notes_reconciled_dir = (
        project_data_dir / "reconciled" / "manual_notes" / _safe_path_segment(run_id)
    )
    manual_notes = _snapshot_manual_notes(
        source_dir=manual_notes_source_dir,
        snapshot_dir=run_dir / "manual_notes",
        now=now,
    )

    pm_agent_context = gateway.query_pm_markdown_context(project_id, now)
    agent_context = _pm_agent_context_agent_context(pm_agent_context)
    if not agent_context:
        agent_context = gateway.query_agent_context(project_id, now)
    config = _config_with_project_start(config, agent_context)
    unsent_outbox = gateway.query_slack_outbox(
        project_id,
        statuses=["draft"],
        limit=50,
    )
    conversations = _eligible_conversations(client, config)
    collected = _collect_messages(client, config, conversations, now)
    pm_protocol_context = _pm_protocol_with_message_ack_obligations(
        gateway.query_pm_communication_protocol(project_id, now),
        collected,
        config,
        now,
    )
    _write_evidence_files(
        run_dir=run_dir,
        project_id=project_id,
        run_id=run_id,
        config=config,
        agent_context=agent_context,
        unsent_outbox=unsent_outbox,
        conversations=conversations,
        collected=collected,
        pm_protocol_context=pm_protocol_context,
        pm_agent_context=pm_agent_context,
        manual_notes=manual_notes,
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
    codex_output = None
    previous_stdout = ""
    last_validation_error = ""
    for attempt in range(1, 4):
        attempt_prompt = (
            prompt
            if attempt == 1
            else _codex_correction_prompt(
                prompt,
                attempt=attempt,
                max_attempts=3,
                validation_error=last_validation_error,
                previous_stdout=previous_stdout,
            )
        )
        attempt_started_at = time.time()
        completed = subprocess_runner.run(
            codex_args,
            input=attempt_prompt,
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
        previous_stdout = completed.stdout
        validation_pm_protocol_context = _pm_protocol_with_message_ack_obligations(
            gateway.query_pm_communication_protocol(project_id, now),
            collected,
            config,
            now,
        )
        try:
            parsed_output = _parse_codex_output(completed.stdout)
            parsed_output = _normalize_codex_output_for_pm_protocol(
                parsed_output,
                validation_pm_protocol_context,
            )
            _validate_codex_reconciliation_output(
                parsed_output,
                config,
                pm_protocol_context=validation_pm_protocol_context,
                pm_agent_context=pm_agent_context,
            )
        except IntegrationError as exc:
            last_validation_error = str(exc)
            recovered_output = _recover_codex_output_from_artifacts(
                run_dir=run_dir,
                reconciled_dir=reconciled_dir,
                not_before=attempt_started_at,
                config=config,
                pm_protocol_context=validation_pm_protocol_context,
                pm_agent_context=pm_agent_context,
            )
            if recovered_output is not None:
                codex_output = recovered_output
                pm_protocol_context = validation_pm_protocol_context
                break
            _write_text(
                run_dir / f"codex_invalid_output_attempt_{attempt}.txt",
                completed.stdout,
            )
            if attempt == 3:
                message = last_validation_error
                print(message, file=sys.stderr)
                return CommandResult(exit_code=1, message=message)
            continue
        codex_output = parsed_output
        pm_protocol_context = validation_pm_protocol_context
        break

    if codex_output is None:
        message = "codex exec did not return valid draft JSON after 3 attempts."
        print(message, file=sys.stderr)
        return CommandResult(exit_code=1, message=message)
    _write_json(run_dir / "codex_output.json", codex_output.model_dump(mode="json"))
    gateway.update_slack_continuity_note(
        project_id=project_id,
        continuity_note=_continuity_note_storage_text(codex_output.continuity_note),
        updated_at=now,
    )
    drafts = codex_output.draft_messages
    team_drafts = codex_output.team_channel_draft_messages
    outbox_messages = _outbox_messages_from_drafts(
        drafts,
        team_drafts,
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
        run_selected_outbox_ids = selected_outbox_ids
        if run_selected_outbox_ids is None:
            run_selected_outbox_ids = [
                row.get("outbox_id", row.get("message_id"))
                for row in pending
                if row.get("run_id") == run_id
            ]
        send_result = _send_pending_outbox(
            client=client,
            gateway=gateway,
            project_id=project_id,
            pending=pending,
            now=now,
            dry_run_send=dry_run_send,
            config=config,
            selected_outbox_ids=run_selected_outbox_ids,
        )

    _archive_manual_notes_successful_run(
        manual_notes=manual_notes,
        source_dir=manual_notes_source_dir,
        reconciled_dir=manual_notes_reconciled_dir,
    )
    _archive_successful_run(run_dir, reconciled_dir)
    message = (
        f"Slack run {run_id} collected {len(collected['messages'])} messages and "
        f"persisted {len(drafts) + len(team_drafts)} draft messages."
    )
    print(message)
    return CommandResult(
        exit_code=0,
        message=message,
        data={
            "run_id": run_id,
            "run_dir": str(run_dir),
            "reconciled_dir": str(reconciled_dir),
            "artifact_dir": str(reconciled_dir),
            "artifacts_archived_to_reconciled": True,
            "message_count": len(collected["messages"]),
            "manual_note_count": len(manual_notes),
            "manual_notes_reconciled_dir": str(manual_notes_reconciled_dir),
            "draft_count": len(drafts) + len(team_drafts),
            "no_message_count": len(codex_output.no_message_decisions),
            "continuity_note": (
                codex_output.continuity_note
                if isinstance(codex_output.continuity_note, str)
                else codex_output.continuity_note.model_dump(mode="json")
            ),
            "evidence_line_answers": [
                answer.model_dump(mode="json")
                for answer in codex_output.evidence_line_answers
            ],
            "reviewer_notes": [
                note.model_dump(mode="json")
                for note in codex_output.reviewer_notes
            ],
            "draft_messages": [
                draft.model_dump(mode="json") for draft in codex_output.draft_messages
            ],
            "team_channel_draft_messages": [
                draft.model_dump(mode="json")
                for draft in codex_output.team_channel_draft_messages
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


def _config_with_bot_identity(config: SlackProjectConfig, client: Any) -> SlackProjectConfig:
    try:
        response = client.auth_test()
    except Exception:
        return config
    raw = dict(config.raw or {})
    if response.get("user_id"):
        raw["bot_user_id"] = str(response["user_id"])
    if response.get("bot_id"):
        raw["bot_id"] = str(response["bot_id"])
    return replace(config, raw=raw)


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
            if _is_bot_authored_message(message, config):
                max_ts = _max_slack_ts(max_ts, str(message.get("ts") or ""))
                continue
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
        if _is_bot_authored_message(reply, config):
            max_ts = _max_slack_ts(max_ts, str(reply.get("ts") or ""))
            continue
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


def _is_bot_authored_message(message: dict[str, Any], config: SlackProjectConfig) -> bool:
    # The PM protocol requires acknowledging teammate input, not our own prior
    # outbox messages. Filtering bot-authored rows here avoids self-ack loops
    # while still advancing cursors past messages we should ignore.
    if message.get("bot_id"):
        return True
    if message.get("subtype") == "bot_message":
        return True
    raw = config.raw or {}
    bot_user_id = raw.get("bot_user_id")
    if bot_user_id and message.get("user") == bot_user_id:
        return True
    return False


def _snapshot_manual_notes(
    *,
    source_dir: Path,
    snapshot_dir: Path,
    now: dt.datetime,
) -> list[dict[str, Any]]:
    """Copy unreconciled manual notes into this run's evidence folder."""
    if not source_dir.exists():
        return []
    notes: list[dict[str, Any]] = []
    for source_path in sorted(path for path in source_dir.rglob("*") if path.is_file()):
        relative_path = source_path.relative_to(source_dir)
        snapshot_path = snapshot_dir / relative_path
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, snapshot_path)
        stat = source_path.stat()
        note = {
            "note_key": f"manual_notes:{relative_path.as_posix()}",
            "relative_path": relative_path.as_posix(),
            "source_path": str(source_path),
            "snapshot_path": str(snapshot_path),
            "size_bytes": stat.st_size,
            "modified_at": dt.datetime.fromtimestamp(
                stat.st_mtime,
                tz=dt.UTC,
            ).isoformat(),
            "collected_at": now.isoformat(),
        }
        try:
            note["content"] = source_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            note["content"] = None
            note["content_error"] = "not valid UTF-8; inspect copied attachment file"
        notes.append(note)
    return notes


def _manual_note_manifest_rows(
    manual_notes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for note in manual_notes:
        rows.append(
            {
                "note_key": note.get("note_key"),
                "relative_path": note.get("relative_path"),
                "size_bytes": note.get("size_bytes"),
                "modified_at": note.get("modified_at"),
                "collected_at": note.get("collected_at"),
                "snapshot_path": note.get("snapshot_path"),
                "content_error": note.get("content_error"),
            }
        )
    return rows


def _write_manual_notes_markdown(
    path: Path,
    manual_notes: list[dict[str, Any]],
) -> None:
    lines = [
        "# Manual notes",
        "",
        (
            "Manual notes are operator-provided PM evidence copied from "
            "`unreconciled/manual_notes` for this run."
        ),
        "",
    ]
    if not manual_notes:
        lines.append("- None")
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    for note in manual_notes:
        lines.extend(
            [
                f"## {note.get('note_key')}",
                f"- File: manual_notes/{note.get('relative_path')}",
                f"- Modified at: {note.get('modified_at')}",
                "",
            ]
        )
        content = note.get("content")
        if isinstance(content, str):
            lines.extend([content.rstrip(), ""])
        else:
            lines.extend(
                [
                    (
                        "Content was copied as an attachment but was not valid "
                        "UTF-8. Inspect the file above directly."
                    ),
                    "",
                ]
            )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_evidence_files(
    *,
    run_dir: Path,
    project_id: str,
    run_id: str,
    config: SlackProjectConfig,
    agent_context: dict[str, Any],
    unsent_outbox: list[dict[str, Any]],
    conversations: list[dict[str, Any]],
    collected: dict[str, Any],
    pm_protocol_context: dict[str, Any],
    pm_agent_context: dict[str, Any] | str,
    manual_notes: list[dict[str, Any]],
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
    _write_manual_notes_markdown(run_dir / "manual_notes.md", manual_notes)

    manifest_payload = {
        "project_id": project_id,
        "run_id": run_id,
        "collected_at": now.isoformat(),
        "message_count": len(messages),
        "manual_note_count": len(manual_notes),
        "manual_notes": _manual_note_manifest_rows(manual_notes),
        "conversations": conversations,
        "cursors": collected["cursors"],
        "configured_channels": [channel.as_dict() for channel in config.channels],
        "fetch_thread_replies": config.fetch_thread_replies,
    }
    _write_json(run_dir / "collection_manifest.json", manifest_payload)
    _write_json(run_dir / "agent_context.json", agent_context)
    _write_json(run_dir / "pm_communication_protocol.json", pm_protocol_context)
    if pm_agent_context:
        if isinstance(pm_agent_context, dict):
            _write_json(run_dir / "pm_agent_context.json", pm_agent_context)
        markdown = _pm_agent_context_markdown(pm_agent_context)
        if markdown:
            (run_dir / "pm_agent_context.md").write_text(markdown, encoding="utf-8")
    _write_json(
        run_dir / "unsent_outbox.json",
        _summarize_unsent_outbox(unsent_outbox),
    )
    _write_json(
        run_dir / "continuity_note.json",
        {
            "project_id": project_id,
            "current_time": now.isoformat(),
            "previous_continuity_note": config.continuity_note,
            "previous_continuity_note_json": _json_or_none(config.continuity_note),
            "previous_continuity_updated_at": (
                config.continuity_updated_at.isoformat()
                if config.continuity_updated_at
                else None
            ),
        },
    )
    _write_json(
        run_dir / "resource_slack_map.json",
        [entry.as_dict() for entry in config.resource_slack_map],
    )


def _pm_agent_context_markdown(pm_agent_context: dict[str, Any] | str) -> str | None:
    """Extract markdown from service PM context payloads without fixing one API."""
    if isinstance(pm_agent_context, str):
        markdown = pm_agent_context.strip()
        return markdown or None
    for key in (
        "markdown",
        "context_markdown",
        "pm_agent_context_markdown",
        "pm_flow_context_markdown",
        "body",
        "content",
    ):
        value = pm_agent_context.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _pm_agent_context_agent_context(
    pm_agent_context: dict[str, Any] | str,
) -> dict[str, Any]:
    """Reuse the generated PM context's embedded service context when present."""
    if not isinstance(pm_agent_context, dict):
        return {}
    agent_context = pm_agent_context.get("agent_context")
    if isinstance(agent_context, dict):
        return agent_context
    return {}


def _pm_agent_context_evidence_line_ids(
    pm_agent_context: dict[str, Any] | str,
) -> dict[str, str]:
    if not isinstance(pm_agent_context, dict):
        return {}
    line_items = pm_agent_context.get("evidence_line_items")
    if not isinstance(line_items, list):
        return {}
    evidence_by_id: dict[str, str] = {}
    for item in line_items:
        if not isinstance(item, dict):
            continue
        evidence_line_id = str(item.get("evidence_line_id") or "").strip()
        if not evidence_line_id:
            continue
        evidence_by_id[evidence_line_id] = str(item.get("question") or "")
    return evidence_by_id


def _pm_protocol_with_message_ack_obligations(
    pm_protocol_context: dict[str, Any],
    collected: dict[str, Any],
    config: SlackProjectConfig,
    now: dt.datetime,
) -> dict[str, Any]:
    """Add per-message acknowledgment obligations to service-derived protocol."""
    protocol = dict(pm_protocol_context or {})
    if not protocol:
        return protocol
    obligations = list(protocol.get("obligations") or [])
    map_by_slack_user = {entry.slack_user_id: entry for entry in config.resource_slack_map}
    existing_ids = {
        str(obligation.get("obligation_id"))
        for obligation in obligations
        if isinstance(obligation, dict) and obligation.get("obligation_id")
    }
    existing_ids.update(
        str(evidence.get("obligation_id"))
        for evidence in protocol.get("evidence", []) or []
        if isinstance(evidence, dict)
        and evidence.get("evidence_type") == "message_receipt_ack"
        and evidence.get("obligation_id")
    )
    for message in collected.get("messages", []) or []:
        if not isinstance(message, dict):
            continue
        message_key = str(message.get("message_key") or "")
        user_id = str(message.get("user_id") or "")
        if not message_key or not user_id or user_id == "unknown":
            continue
        obligation_id = f"message_receipt_ack:{message_key}"
        if obligation_id in existing_ids:
            continue
        mapped = map_by_slack_user.get(user_id)
        target_type = message.get("conversation_type") or "channel"
        obligations.append(
            {
                "obligation_id": obligation_id,
                "evidence_type": "message_receipt_ack",
                "resource_id": (
                    mapped.resource_id if target_type == "im" and mapped else None
                ),
                "process_id": None,
                "process_symbol": None,
                "status": "due_now",
                "due": True,
                "due_reason": (
                    "Every teammate or team-channel message to the PM bot must "
                    "receive at least an acknowledgment."
                ),
                "evaluated_at": now.isoformat(),
                "required_evidence_types": ["message_receipt_ack"],
                "target_type": "dm" if target_type == "im" and mapped else "channel",
                "slack_user_id": (
                    mapped.slack_user_id if target_type == "im" and mapped else None
                ),
                "slack_channel_id": (
                    message.get("conversation_id")
                    if target_type != "im" or mapped is None
                    else None
                ),
                "source_message_key": message_key,
                "last_evidence_at": None,
                "last_evidence_outbox_id": None,
            }
        )
        existing_ids.add(obligation_id)
    protocol["obligations"] = obligations
    return protocol


def _summarize_unsent_outbox(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep pending outbox evidence compact for the agent."""
    output = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        body = " ".join(str(row.get("body") or row.get("text") or "").split())
        output.append(
            {
                "outbox_id": row.get("outbox_id"),
                "target_type": row.get("target_type")
                or (
                    "channel"
                    if not row.get("slack_user_id") and row.get("slack_channel_id")
                    else "dm"
                ),
                "resource_id": row.get("resource_id"),
                "slack_user_id": row.get("slack_user_id"),
                "slack_channel_id": row.get("slack_channel_id"),
                "status": row.get("status"),
                "run_id": row.get("run_id"),
                "created_at": row.get("created_at"),
                "pm_evidence_claims": row.get("pm_evidence_claims") or [],
                "block_count": len(row.get("blocks") or []),
                "body_snippet": body[:197] + "..." if len(body) > 200 else body,
            }
        )
    return output


def _pm_process_signal(
    node: dict[str, Any],
    now: dt.datetime,
    mapped_resource_ids: set[str],
    open_blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    dependency = node.get("dependency_only") or node.get("schedule") or {}
    resource = node.get("resource_aware") or node.get("schedule") or {}
    role_requirements = node.get("role_requirements") or []
    role_ids = [
        str(requirement.get("role_id"))
        for requirement in role_requirements
        if isinstance(requirement, dict) and requirement.get("role_id")
    ]
    process_role_pins = [
        {
            **pin,
            "role_id": pin.get("role_id") or requirement.get("role_id"),
            "process_symbol": node.get("process_symbol") or node.get("symbol"),
        }
        for requirement in role_requirements
        if isinstance(requirement, dict)
        for pin in requirement.get("pins", []) or []
        if isinstance(pin, dict)
    ]
    pinned_started_at = node.get("started_at") or min(
        (
            str(pin["pinned_at"])
            for pin in process_role_pins
            if pin.get("pinned_at")
        ),
        default=None,
    )
    pinned_resource_ids = sorted(
        {
            str(resource_id)
            for requirement in role_requirements
            if isinstance(requirement, dict)
            for resource_id in requirement.get("active_pinned_resource_ids", []) or []
        }
        | {
            str(resource_id)
            for requirement in role_requirements
            if isinstance(requirement, dict)
            for resource_id in requirement.get("recent_pinned_resource_ids", []) or []
        }
        | {
            str(pin.get("resource_id"))
            for pin in process_role_pins
            if pin.get("resource_id")
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
    planned_start_at = _maybe_datetime(
        resource.get("starts_at")
        or resource.get("planned_start_at")
        or dependency.get("starts_at")
        or dependency.get("es_at")
    )
    planned_finish_at = _maybe_datetime(
        resource.get("ends_at")
        or resource.get("planned_finish_at")
        or dependency.get("ends_at")
        or dependency.get("ef_at")
    )
    schedule_window_starts_at = _maybe_datetime(
        resource.get("schedule_window_starts_at")
        or dependency.get("schedule_window_starts_at")
    )
    schedule_window_ends_at = _maybe_datetime(
        resource.get("schedule_window_ends_at")
        or dependency.get("schedule_window_ends_at")
    )
    schedule_buffer_hours = resource.get("schedule_buffer_hours")
    if schedule_buffer_hours is None:
        schedule_buffer_hours = dependency.get("schedule_buffer_hours")
    if schedule_buffer_hours is None:
        schedule_buffer_hours = dependency.get("slack_hours")
    schedule_buffer_days = None
    if schedule_buffer_hours is not None:
        try:
            schedule_buffer_days = round(float(schedule_buffer_hours) / 24, 2)
        except (TypeError, ValueError):
            schedule_buffer_days = None
    max_sensitivity = resource.get("max_makespan_sensitivity_hours")
    if max_sensitivity is None:
        max_sensitivity = dependency.get("max_makespan_sensitivity_hours")
    if max_sensitivity is None and (
        dependency.get("criticality_label") == "critical"
        or resource.get("criticality_label") == "critical"
    ):
        max_sensitivity = 1.0
    max_sensitivity_value = _float_or_none(max_sensitivity)
    sensitivity_label = (
        resource.get("sensitivity_label")
        or dependency.get("sensitivity_label")
        or (
            "makespan_sensitive"
            if max_sensitivity_value is not None and max_sensitivity_value > 0
            else "unknown"
        )
    )
    status = str(node.get("status") or "")
    done = status in {"done", "canceled"} or bool(node.get("finished_at"))
    not_started = not pinned_started_at and status not in {"done", "canceled"}
    required_resource_count = sum(
        int(requirement.get("required_resource_count") or 1)
        for requirement in role_requirements
        if isinstance(requirement, dict)
    )
    role_effort_hours = sum(
        float(requirement.get("effort_hours") or 0)
        for requirement in role_requirements
        if isinstance(requirement, dict)
    )
    description = str(node.get("description") or "").strip()
    duration_business_days = None
    duration_source = resource.get("schedule_elapsed_hours")
    if duration_source is None:
        duration_source = resource.get("inferred_duration_hours")
    if duration_source is not None:
        try:
            duration_business_days = round(
                float(duration_source) / 24,
                2,
            )
        except (TypeError, ValueError):
            duration_business_days = None
    return {
        "process_id": node.get("process_id"),
        "process_symbol": node.get("process_symbol") or node.get("symbol"),
        "name": node.get("name"),
        "status": status,
        "computed_status": node.get("computed_status"),
        "planned_start_at": (
            planned_start_at.isoformat() if planned_start_at is not None else None
        ),
        "planned_finish_at": (
            planned_finish_at.isoformat() if planned_finish_at is not None else None
        ),
        "schedule_window_starts_at": (
            schedule_window_starts_at.isoformat()
            if schedule_window_starts_at is not None
            else None
        ),
        "schedule_window_ends_at": (
            schedule_window_ends_at.isoformat()
            if schedule_window_ends_at is not None
            else None
        ),
        "schedule_buffer_days": schedule_buffer_days,
        "sensitivity_label": sensitivity_label,
        "max_makespan_sensitivity_hours": max_sensitivity_value,
        "role_sensitivity": resource.get("role_sensitivity") or [],
        "predecessors": node.get("predecessors") or [],
        "successors": node.get("successors") or [],
        "days_until_planned_start": _days_until(planned_start_at, now),
        "days_until_planned_finish": _days_until(planned_finish_at, now),
        "not_started": not_started,
        "done": done,
        "started_at": pinned_started_at,
        "finished_at": node.get("finished_at"),
        "definition_of_done": description or None,
        "has_done_definition": bool(description),
        "role_ids": role_ids,
        "role_effort_hours": role_effort_hours,
        "pinned_resource_ids": pinned_resource_ids,
        "process_role_pins": process_role_pins,
        "active_pin_resource_ids": sorted(
            {
                str(pin.get("resource_id"))
                for pin in process_role_pins
                if pin.get("resource_id") and pin.get("status") == "pinned_started"
            }
        ),
        "eligible_resource_ids": eligible_resource_ids,
        "eligible_resource_count": len(eligible_resource_ids),
        "required_resource_count": required_resource_count,
        "duration_business_days": duration_business_days or 0,
        "assumption_note": node.get("assumption_note"),
        "open_blockers": [
            {
                "blocker_id": blocker.get("blocker_id"),
                "summary": blocker.get("summary"),
                "details": blocker.get("details"),
                "severity": blocker.get("severity"),
                "created_at": blocker.get("created_at"),
                "resolution_owner_resource_id": blocker.get(
                    "resolution_owner_resource_id"
                ),
                "immediate_blocked_processes": blocker.get(
                    "immediate_blocked_processes",
                    [],
                ),
                "needed_by_role_ids": blocker.get("needed_by_role_ids") or [],
                "needed_by_resource_ids": blocker.get("needed_by_resource_ids") or [],
            }
            for blocker in open_blockers
        ],
        "resource_aware": resource,
        "coordination_risk": len(role_ids) > 1 or required_resource_count > 1,
        "estimate_confidence_risk": not node.get("assumption_note")
        or not description,
        "role_assigned_but_unpinned_multi_eligible": (
            len(eligible_resource_ids) > 1 and not pinned_resource_ids
        ),
        "mapped_resource_needed_but_unpinned": (
            bool(mapped_resource_ids)
            and bool(role_ids)
            and not set(pinned_resource_ids).intersection(mapped_resource_ids)
        ),
    }


def _process_attribute_evidence(signal: dict[str, Any]) -> dict[str, Any]:
    """Return attribute-level evidence checks for one process signal."""

    owner_state = _named_resource_ownership_state(signal)
    schedule_review_needed = (
        signal.get("planned_start_at") is None
        or signal.get("planned_finish_at") is None
        or (
            signal.get("not_started")
            and signal.get("days_until_planned_start") is not None
            and signal["days_until_planned_start"] <= 1
        )
        or (
            not signal.get("done")
            and signal.get("days_until_planned_finish") is not None
            and signal["days_until_planned_finish"] <= 1
        )
    )
    questions: list[str] = []
    if owner_state == "needs_owner_confirmation":
        questions.append(
            "Ask for owner and estimate confirmation before messaging a named "
            "teammate as accountable."
        )
    if schedule_review_needed:
        questions.append(
            "Check whether planned start, planned finish, blockers, and "
            "planning effort estimate still match teammate evidence."
        )
    if signal.get("estimate_confidence_risk"):
        questions.append(
            "Verify effort estimate and done definition before treating the "
            "current schedule as reliable."
        )

    return {
        "process_id": signal.get("process_id"),
        "process_symbol": signal.get("process_symbol"),
        "name": signal.get("name"),
        "attributes": {
            "status": {
                "tracked_fields": [
                    "status",
                    "computed_status",
                    "started_at",
                    "finished_at",
                ],
                "current_value": {
                    "status": signal.get("status"),
                    "computed_status": signal.get("computed_status"),
                    "started_at": signal.get("started_at"),
                    "finished_at": signal.get("finished_at"),
                },
                "verification_state": (
                    "focus_evidence_present"
                    if signal.get("started_at") or signal.get("finished_at")
                    else "needs_started_or_done_confirmation"
                    if signal.get("status") in {"in_progress", "paused"}
                    else "service_fact"
                ),
                "evidence_fields": [
                    "process_role_pins",
                    "finished_at",
                ],
            },
            "planned_schedule": {
                "tracked_fields": [
                    "planned_start_at",
                    "planned_finish_at",
                    "schedule_window_starts_at",
                    "schedule_window_ends_at",
                    "schedule_buffer_days",
                ],
                "current_value": {
                    "planned_start_at": signal.get("planned_start_at"),
                    "planned_finish_at": signal.get("planned_finish_at"),
                    "schedule_window_starts_at": signal.get(
                        "schedule_window_starts_at",
                    ),
                    "schedule_window_ends_at": signal.get(
                        "schedule_window_ends_at",
                    ),
                    "schedule_buffer_days": signal.get("schedule_buffer_days"),
                },
                "verification_state": (
                    "computed_needs_review"
                    if schedule_review_needed
                    else "computed_projection"
                ),
                "evidence_fields": [
                    "predecessors",
                    "successors",
                    "role_requirements",
                    "open_blockers",
                    "process_role_pins",
                ],
            },
            "named_resource_ownership": {
                "tracked_fields": [
                    "pinned_resource_ids",
                    "active_pin_resource_ids",
                    "eligible_resource_ids",
                    "role_ids",
                ],
                "evidence_state": owner_state,
                "confirmed_resource_ids": signal.get("pinned_resource_ids") or [],
                "active_pin_resource_ids": signal.get(
                    "active_pin_resource_ids",
                )
                or [],
                "eligible_resource_ids": signal.get("eligible_resource_ids") or [],
                "role_ids": signal.get("role_ids") or [],
                "assignment_message_guardrail": (
                    "Do not describe role-eligible or scheduled resources as "
                    "owners until pin or direct evidence confirms ownership."
                ),
            },
            "role_requirements": {
                "tracked_fields": [
                    "role_ids",
                    "role_effort_hours",
                    "required_resource_count",
                ],
                "current_value": {
                    "role_ids": signal.get("role_ids") or [],
                    "role_effort_hours": signal.get("role_effort_hours"),
                    "required_resource_count": signal.get(
                        "required_resource_count",
                    ),
                },
                "verification_state": (
                    "needs_estimate_confirmation"
                    if signal.get("estimate_confidence_risk")
                    else "service_fact"
                ),
                "evidence_fields": [
                    "assumption_note",
                    "process_role_pins",
                ],
            },
            "done_definition": {
                "tracked_fields": ["definition_of_done"],
                "current_value": signal.get("definition_of_done"),
                "verification_state": (
                    "present" if signal.get("has_done_definition") else "missing"
                ),
            },
            "dependencies": {
                "tracked_fields": ["predecessors", "successors"],
                "current_value": {
                    "predecessors": signal.get("predecessors") or [],
                    "successors": signal.get("successors") or [],
                },
                "verification_state": "service_fact",
            },
            "blockers": {
                "tracked_fields": ["open_blockers"],
                "current_value": signal.get("open_blockers") or [],
                "verification_state": (
                    "needs_resolution_update"
                    if signal.get("open_blockers")
                    else "none_open"
                ),
            },
        },
        "verification_questions": questions,
    }


def _named_resource_ownership_state(signal: dict[str, Any]) -> str:
    if signal.get("active_pin_resource_ids"):
        return "confirmed_active_pin"
    if signal.get("pinned_resource_ids"):
        return "confirmed_by_pin"
    if signal.get("role_ids") or signal.get("eligible_resource_ids"):
        return "needs_owner_confirmation"
    return "no_named_resource_required"


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


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _message_keys_matching(
    collected: dict[str, Any],
    needles: tuple[str, ...],
    *,
    conversation_type: str | None = None,
) -> list[str]:
    output = []
    lowered_needles = tuple(needle.casefold() for needle in needles)
    for message in collected.get("messages", []):
        if conversation_type and message.get("conversation_type") != conversation_type:
            continue
        text = str(message.get("text") or "").casefold()
        if any(needle in text for needle in lowered_needles):
            output.append(str(message.get("message_key")))
    return output


def _message_evidence_by_key(collected: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Summarize collected Slack messages for evidence-recency lookups."""
    evidence: dict[str, dict[str, Any]] = {}
    for message in collected.get("messages", []):
        if not isinstance(message, dict):
            continue
        key = str(message.get("message_key") or "")
        if not key:
            continue
        text = " ".join(str(message.get("text") or "").split())
        snippet = text[:117] + "..." if len(text) > 120 else text
        conversation = message.get("conversation_name") or message.get(
            "conversation_id"
        )
        evidence[key] = {
            "last_evidence_at": message.get("message_at"),
            "last_evidence_note": (
                f"Slack {message.get('conversation_type') or 'message'} {key}"
                f" from {message.get('user_id') or 'unknown'}"
                f" in {conversation or 'unknown conversation'}: {snippet}"
            ),
            "conversation_type": message.get("conversation_type"),
            "teammate_id": message.get("teammate_id"),
            "resource_id": message.get("resource_id"),
        }
    return evidence


def _mapped_teammate_priority_context(
    config: SlackProjectConfig,
    resource_priorities: list[Any],
    *,
    assignment_lists_by_resource: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    assignment_lists_by_resource = assignment_lists_by_resource or {}
    priorities_by_resource = {
        str(row.get("resource_id")): row
        for row in resource_priorities
        if isinstance(row, dict) and row.get("resource_id")
    }
    return [
        {
            **entry.as_dict(),
            "resource_priority": priorities_by_resource.get(entry.resource_id),
            "assignment_list": assignment_lists_by_resource.get(entry.resource_id),
        }
        for entry in config.resource_slack_map
    ]


def _assignment_lists_from_pm_protocol(
    pm_protocol_context: dict[str, Any],
    config: SlackProjectConfig,
) -> list[dict[str, Any]]:
    """Return service-rendered assignment-list artifacts from PM protocol query."""
    rows_by_resource: dict[str, dict[str, Any]] = {}
    for row in pm_protocol_context.get("resource_processes") or []:
        if not isinstance(row, dict) or not row.get("resource_id"):
            continue
        resource_id = str(row["resource_id"])
        artifact = row.get("message_artifact") if isinstance(
            row.get("message_artifact"),
            dict,
        ) else {}
        normalized = {
            **row,
            "content_hash": row.get("content_hash")
            or row.get("assignment_content_hash"),
            "message_markdown": row.get("message_markdown")
            or artifact.get("message_markdown"),
            "message_blocks": row.get("message_blocks")
            or artifact.get("message_blocks")
            or [],
            "message_artifact": artifact or row.get("message_artifact"),
            "rendered_by": artifact.get("rendered_by")
            or "query_pm_communication_protocol",
        }
        rows_by_resource[resource_id] = normalized
    return [
        rows_by_resource[entry.resource_id]
        for entry in config.resource_slack_map
        if entry.resource_id in rows_by_resource
    ]


def _assigned_processes_by_resource(
    agent_context: dict[str, Any],
    resource_priorities: list[Any],
) -> list[dict[str, Any]]:
    """Return current per-resource assignment lists ready for teammate messages."""
    graph = agent_context.get("graph") or {}
    nodes = [
        node
        for node in graph.get("nodes", []) or []
        if isinstance(node, dict)
    ]
    nodes_by_symbol = {
        str(node.get("symbol") or node.get("process_symbol")): node
        for node in nodes
        if node.get("symbol") or node.get("process_symbol")
    }
    nodes_by_id = {
        str(node.get("process_id")): node
        for node in nodes
        if node.get("process_id")
    }
    blockers_by_process: dict[str, list[dict[str, Any]]] = {}
    for blocker in agent_context.get("blockers") or []:
        if not isinstance(blocker, dict) or blocker.get("is_resolved_as_of"):
            continue
        for key in (blocker.get("process_id"), blocker.get("process_symbol")):
            if key:
                blockers_by_process.setdefault(str(key), []).append(
                    _assignment_blocker_summary(blocker),
                )

    output = []
    for priority in resource_priorities:
        if not isinstance(priority, dict):
            continue
        resource_id = str(priority.get("resource_id") or "")
        if not resource_id:
            continue
        resource_name = str(priority.get("resource_name") or resource_id)
        assigned_processes = []
        for process in priority.get("processes") or []:
            if not isinstance(process, dict):
                continue
            symbol = str(process.get("process_symbol") or process.get("symbol") or "")
            process_id = str(process.get("process_id") or "")
            node = nodes_by_symbol.get(symbol) or nodes_by_id.get(process_id) or {}
            if not process_id and node.get("process_id"):
                process_id = str(node["process_id"])
            schedule = (
                node.get("schedule")
                or node.get("resource_aware")
                or node.get("dependency_only")
                or {}
            )
            planned_start_at = _first_assignment_value(
                process.get("planned_start_at"),
                schedule.get("starts_at"),
                schedule.get("planned_start_at"),
            )
            planned_finish_at = _first_assignment_value(
                process.get("planned_finish_at"),
                schedule.get("ends_at"),
                schedule.get("planned_finish_at"),
            )
            schedule_window_starts_at = _first_assignment_value(
                process.get("schedule_window_starts_at"),
                schedule.get("schedule_window_starts_at"),
            )
            schedule_window_ends_at = _first_assignment_value(
                process.get("schedule_window_ends_at"),
                schedule.get("schedule_window_ends_at"),
            )
            role_ids = _assignment_role_ids(process.get("role_ids"))
            assignment_certainty = _resource_assignment_certainty(node, resource_id)
            ownership_evidence_state = _resource_ownership_evidence_state(
                node,
                resource_id,
            )
            row = {
                "priority": process.get("priority"),
                "process_id": process_id or None,
                "process_symbol": symbol or None,
                "process_name": process.get("process_name")
                or node.get("name")
                or symbol
                or None,
                "status": node.get("computed_status")
                or process.get("computed_status")
                or node.get("status")
                or process.get("status"),
                "planned_start_at": planned_start_at,
                "planned_finish_at": planned_finish_at,
                "schedule_window_starts_at": schedule_window_starts_at,
                "schedule_window_ends_at": schedule_window_ends_at,
                "started_at": node.get("started_at"),
                "finished_at": node.get("finished_at"),
                "start_buffer_days": _assignment_duration_days(
                    schedule_window_starts_at,
                    planned_start_at,
                ),
                "duration_days": _assignment_duration_days(
                    planned_start_at,
                    planned_finish_at,
                ),
                "finish_buffer_days": _assignment_duration_days(
                    planned_finish_at,
                    schedule_window_ends_at,
                ),
                "hours_until_planned_start": process.get(
                    "hours_until_planned_start",
                ),
                "hours_until_planned_finish": process.get(
                    "hours_until_planned_finish",
                ),
                "role_ids": role_ids,
                "effort_hours": process.get("effort_hours"),
                "assignment_certainty": assignment_certainty,
                "ownership_evidence_state": ownership_evidence_state,
                "message_caveat": _assignment_message_caveat(
                    assignment_certainty,
                    ownership_evidence_state,
                ),
                "active_pin": bool(process.get("active_pin"))
                or _resource_has_active_pin(node, resource_id),
                "pin_started_at": process.get("pin_started_at")
                or _resource_pin_started_at(node, resource_id),
                "pin_history": _resource_pin_history(node, resource_id),
                "planned_assignment": {
                    "resource_id": resource_id,
                    "resource_name": resource_name,
                    "role_ids": role_ids,
                },
                "done_definition": node.get("description") or None,
                "max_makespan_sensitivity_hours": process.get(
                    "max_makespan_sensitivity_hours",
                )
                or schedule.get("max_makespan_sensitivity_hours"),
                "sensitivity_label": process.get("sensitivity_label")
                or schedule.get("sensitivity_label"),
                "blockers": blockers_by_process.get(process_id)
                or blockers_by_process.get(symbol)
                or [],
            }
            assigned_processes.append(row)
        assigned_processes = sorted(
            assigned_processes,
            key=lambda item: (
                _priority_rank(item.get("priority")),
                str(item.get("planned_start_at") or ""),
                str(item.get("process_symbol") or ""),
            ),
        )
        payload = {
            "resource_id": resource_id,
            "assigned_processes": assigned_processes,
        }
        content_hash = "sha256:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8"),
        ).hexdigest()
        output.append(
            {
                "resource_id": resource_id,
                "resource_name": resource_name,
                "assignment_count": len(assigned_processes),
                "content_hash": content_hash,
                "assigned_processes": assigned_processes,
                "message_markdown": _assignment_list_markdown(
                    resource_name,
                    resource_id,
                    assigned_processes,
                ),
                "message_blocks": _assignment_list_blocks(
                    resource_name,
                    resource_id,
                    assigned_processes,
                ),
            }
        )
    return sorted(output, key=lambda item: str(item.get("resource_id") or ""))


def _empty_assignment_list(resource_id: str, resource_name: str | None) -> dict[str, Any]:
    name = resource_name or resource_id
    payload = {"resource_id": resource_id, "assigned_processes": []}
    content_hash = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8"),
    ).hexdigest()
    return {
        "resource_id": resource_id,
        "resource_name": name,
        "assignment_count": 0,
        "content_hash": content_hash,
        "assigned_processes": [],
        "message_markdown": _assignment_list_markdown(name, resource_id, []),
        "message_blocks": _assignment_list_blocks(name, resource_id, []),
    }


def _assignment_blocker_summary(blocker: dict[str, Any]) -> dict[str, Any]:
    return {
        "blocker_id": blocker.get("blocker_id"),
        "summary": blocker.get("summary"),
        "details": blocker.get("details"),
        "severity": blocker.get("severity"),
        "resolution_owner_resource_id": blocker.get("resolution_owner_resource_id"),
    }


def _assignment_role_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return sorted(str(part) for part in value if part)
    return [str(value)]


def _first_assignment_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _resource_has_active_pin(node: dict[str, Any], resource_id: str) -> bool:
    return any(
        pin.get("resource_id") == resource_id
        and pin.get("status") == "pinned_started"
        for pin in _resource_pin_history(node, resource_id)
    )


def _resource_pin_started_at(node: dict[str, Any], resource_id: str) -> Any:
    active_starts = [
        pin.get("pinned_at")
        for pin in _resource_pin_history(node, resource_id)
        if pin.get("status") == "pinned_started"
    ]
    return min(active_starts) if active_starts else None


def _resource_pin_history(node: dict[str, Any], resource_id: str) -> list[dict[str, Any]]:
    pins = []
    for requirement in node.get("role_requirements") or []:
        if not isinstance(requirement, dict):
            continue
        role_id = requirement.get("role_id")
        for pin in requirement.get("pins", []) or []:
            if not isinstance(pin, dict) or pin.get("resource_id") != resource_id:
                continue
            pins.append(
                {
                    **pin,
                    "role_id": pin.get("role_id") or role_id,
                }
            )
    return sorted(pins, key=lambda item: str(item.get("pinned_at") or ""))


def _resource_assignment_certainty(node: dict[str, Any], resource_id: str) -> str:
    pin_history = _resource_pin_history(node, resource_id)
    if any(pin.get("status") == "pinned_started" for pin in pin_history):
        return "confirmed_active_pin"
    if pin_history:
        return "confirmed_by_pin"
    return "scheduled_role_allocation_unconfirmed"


def _resource_ownership_evidence_state(
    node: dict[str, Any],
    resource_id: str,
) -> str:
    if _resource_assignment_certainty(node, resource_id) != (
        "scheduled_role_allocation_unconfirmed"
    ):
        return "confirmed_by_pin"
    return "needs_owner_confirmation"


def _assignment_message_caveat(
    assignment_certainty: str,
    ownership_evidence_state: str,
) -> str | None:
    if (
        assignment_certainty == "scheduled_role_allocation_unconfirmed"
        or ownership_evidence_state == "needs_owner_confirmation"
    ):
        return (
            "This is planned role work and needs owner confirmation before it "
            "is described as accepted ownership."
        )
    return None


def _assignment_duration_days(start_value: Any, end_value: Any) -> float | None:
    start_at = _maybe_datetime(start_value)
    end_at = _maybe_datetime(end_value)
    if start_at is None or end_at is None:
        return None
    return round((end_at - start_at).total_seconds() / 86400, 2)


def _priority_rank(priority: Any) -> int:
    match str(priority or "").upper():
        case "P0":
            return 0
        case "P1":
            return 1
        case "P2":
            return 2
        case "P3":
            return 3
        case _:
            return 99


def _assignment_list_markdown(
    resource_name: str,
    resource_id: str,
    assigned_processes: list[dict[str, Any]],
) -> str:
    if not assigned_processes:
        return (
            f"Current process work list for {resource_name} (`{resource_id}`): "
            "no current or upcoming process work."
        )
    lines = [f"Current process work list for {resource_name} (`{resource_id}`):"]
    for title, group in _assignment_process_groups(assigned_processes):
        if not group:
            continue
        lines.append("")
        lines.append(f"{title}:")
        for index, process in enumerate(group, start=1):
            rendered = _assignment_process_markdown(
                process,
                resource_name=resource_name,
                index=index,
            )
            lines.extend(f"  {line}" for line in rendered.splitlines())
    return "\n".join(lines)


def _assignment_list_blocks(
    resource_name: str,
    resource_id: str,
    assigned_processes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    header_text = f"Tasks for {resource_name}"[:150]
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
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
    if not assigned_processes:
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
    for title, group in _assignment_process_groups(assigned_processes):
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
            text = _assignment_process_markdown(
                process,
                resource_name=resource_name,
                index=index,
            )
            for chunk in _chunk_slack_text(text, 2800):
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": chunk},
                    }
                )
                if len(blocks) >= 50:
                    return blocks
    return blocks


def _assignment_process_groups(
    assigned_processes: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped = {
        "Pinned": [],
        "Needs attention": [],
        "Upcoming": [],
        "Later": [],
    }
    for process in assigned_processes:
        if process.get("active_pin"):
            grouped["Pinned"].append(process)
        elif _assignment_needs_attention(process):
            grouped["Needs attention"].append(process)
        elif _assignment_is_upcoming(process):
            grouped["Upcoming"].append(process)
        else:
            grouped["Later"].append(process)
    return [(title, grouped[title]) for title in grouped]


def _assignment_needs_attention(process: dict[str, Any]) -> bool:
    if process.get("blockers"):
        return True
    status = str(process.get("computed_status") or process.get("status") or "")
    if status in {"early_start", "due", "started", "paused"}:
        return True
    finish_hours = process.get("hours_until_planned_finish")
    try:
        return finish_hours is not None and float(finish_hours) <= 0
    except (TypeError, ValueError):
        return False


def _assignment_is_upcoming(process: dict[str, Any]) -> bool:
    start_hours = process.get("hours_until_planned_start")
    try:
        return start_hours is not None and 0 <= float(start_hours) <= 72
    except (TypeError, ValueError):
        return False


def _assignment_process_markdown(
    process: dict[str, Any],
    *,
    resource_name: str,
    index: int,
) -> str:
    symbol = process.get("process_symbol") or "-"
    name = process.get("process_name") or "-"
    priority = process.get("priority") or "-"
    status = process.get("status") or "unknown"
    lines = [f"{index}. *{priority}* `{symbol}` - {name} ({status})"]
    lines.append(
        "   Start: "
        f"{process.get('planned_start_at') or '-'} | Finish: "
        f"{process.get('planned_finish_at') or '-'}"
    )
    lines.append(
        "   "
        f"{_format_assignment_days(process.get('start_buffer_days'))} pre-buffer | "
        f"{_format_assignment_days(process.get('duration_days'))} duration | "
        f"{_format_assignment_days(process.get('finish_buffer_days'))} post-buffer"
    )
    if process.get("started_at"):
        started_delta = _format_assignment_delta(
            process.get("started_at"),
            process.get("planned_start_at"),
        )
        lines.append(
            "   Started: "
            f"{started_delta}"
        )
    if process.get("finished_at"):
        finished_delta = _format_assignment_delta(
            process.get("finished_at"),
            process.get("planned_finish_at"),
        )
        lines.append(
            "   Finished: "
            f"{finished_delta}"
        )
    role_text = ", ".join(f"`{role_id}`" for role_id in process.get("role_ids", []))
    if role_text:
        if process.get("ownership_evidence_state") == "needs_owner_confirmation":
            lines.append(
                "   Planned resource (needs owner confirmation): "
                f"{resource_name} -> {role_text}"
            )
        else:
            lines.append(f"   Planned resource: {resource_name} -> {role_text}")
    if process.get("message_caveat"):
        lines.append(f"   Note: {process['message_caveat']}")
    if process.get("active_pin"):
        lines.append(
            "   Pinned since "
            f"{process.get('pin_started_at') or '-'}"
        )
    blockers = process.get("blockers") or []
    if blockers:
        blocker_text = "; ".join(
            str(blocker.get("summary") or blocker.get("blocker_id") or "blocker")
            for blocker in blockers
        )
        lines.append(f"   Blockers: {blocker_text}")
    if process.get("done_definition"):
        lines.append(f"   Done: {process['done_definition']}")
    return "\n".join(lines)


def _format_assignment_days(value: Any) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return f"{text} days"


def _format_assignment_delta(actual_value: Any, planned_value: Any) -> str:
    actual_at = _maybe_datetime(actual_value)
    planned_at = _maybe_datetime(planned_value)
    if actual_at is None or planned_at is None:
        return "-"
    delta_days = (actual_at - planned_at).total_seconds() / 86400
    if abs(delta_days) < 0.000001:
        return "0 days early"
    direction = "late" if delta_days > 0 else "early"
    text = f"{abs(delta_days):.2f}".rstrip("0").rstrip(".")
    return f"{text} days {direction}"


def _json_or_none(value: str | None) -> Any | None:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


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


def _archive_manual_notes_successful_run(
    *,
    manual_notes: list[dict[str, Any]],
    source_dir: Path,
    reconciled_dir: Path,
) -> None:
    """Move manual-note source files after the PM run reconciles them."""
    if not manual_notes:
        return
    reconciled_dir.mkdir(parents=True, exist_ok=True)
    for note in manual_notes:
        relative = Path(str(note.get("relative_path") or ""))
        if not relative.parts:
            continue
        source_path = Path(str(note.get("source_path") or ""))
        if not source_path.exists():
            continue
        destination = reconciled_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        shutil.move(str(source_path), str(destination))
    _remove_empty_manual_note_dirs(source_dir)


def _remove_empty_manual_note_dirs(source_dir: Path) -> None:
    if not source_dir.exists():
        return
    directories = sorted(
        (path for path in source_dir.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        source_dir.rmdir()
    except OSError:
        pass


def _archive_successful_run(run_dir: Path, reconciled_dir: Path) -> None:
    """Move successful Slack run artifacts out of unreconciled storage."""
    reconciled_dir.mkdir(parents=True, exist_ok=True)
    if not run_dir.exists():
        return
    for path in list(run_dir.iterdir()):
        destination = reconciled_dir / path.name
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        shutil.move(str(path), str(destination))
    try:
        run_dir.rmdir()
    except OSError:
        pass


def _codex_prompt(
    run_dir: Path,
    reconciled_dir: Path,
    *,
    db_path: str | Path,
    project_id: str,
) -> str:
    reconciled_dir.mkdir(parents=True, exist_ok=True)
    resolved_db_path = Path(db_path).expanduser().resolve()
    pm_context_path = run_dir / "pm_agent_context.md"
    return (
        f"$projdash-pm-flow from {run_dir} putting in {reconciled_dir} "
        "after processing. Use $projdash-project-manager only for the "
        "ProjectDashboard service command/query mechanics.\n\n"
        f"Project id: {project_id}\n"
        f"ProjDash database path: {resolved_db_path}\n"
        f"PM markdown context path: {pm_context_path}\n\n"
        "Read these inputs first: pm_agent_context.md, messages.md, "
        "raw_messages.jsonl, manual_notes.md, manual_notes/, "
        "continuity_note.json, unsent_outbox.json, "
        "pm_communication_protocol.json, and resource_slack_map.json. "
        "Use only newly collected Slack messages, unreconciled manual notes, "
        "continuity, and unsent outbox rows as evidence; do not mine past sent, "
        "failed, or skipped outbound messages. "
        "Treat the input folder as read-only. Write reconciled artifacts only "
        "to the output folder.\n\n"
        "Procedure:\n"
        "1. Read: use pm_agent_context.md as the source of project structure, "
        "field definitions, priorities, and evidence line items. Prioritize "
        "line items marked with `*`.\n"
        "2. Reconcile: inspect Slack and manual notes for clear changes to "
        "processes, dependencies, blockers, role requirements, pins, forecasts, "
        "verified finishes, resources, calendars, or plan data. Apply clear "
        "updates through validated ProjectDashboard service command/query "
        "envelopes against this database. Use exact `role_<resource_id>` roles "
        "when evidence clearly names one person; use shared roles only when "
        "the evidence is genuinely shareable or indeterminate.\n"
        "3. Answer evidence: answer every service-prepared evidence line item "
        "Yes/No with a source-backed reason, outcome, update intent, and "
        "source_keys. Use Yes when new evidence addresses correctness, "
        "including explicit no-change evidence. Use No when new evidence does "
        "not address that line item. Accepted source keys include "
        "`channel:timestamp`, `manual_notes:path`, `pm_agent_context.md:<line "
        "or section>`, and reconciled artifact paths.\n"
        "4. Update service: update `last_evidence` only for Yes answers, and "
        "apply only evidence-backed project state updates. Do not put "
        "unapplied proposed commands in the JSON response.\n"
        "5. Diff: if service state changed, regenerate PM context to "
        f"{reconciled_dir / 'pm_agent_context.after_project_updates.md'} "
        "and write a unified diff against the original context to "
        f"{reconciled_dir / 'pm_agent_context.after_project_updates.diff'}. "
        "Read the diff before drafting.\n"
        "6. Optional corrective cycle: if the diff exposes a mistake or one "
        "more clear update, apply that corrective service update, regenerate "
        "context to "
        f"{reconciled_dir / 'pm_agent_context.after_adjustment_pass.md'} "
        "and write a second unified diff to "
        f"{reconciled_dir / 'pm_agent_context.after_adjustment_pass.diff'}. "
        "If no corrective cycle is needed, say so in reviewer_notes.\n"
        "7. Draft: query the refreshed PM communication protocol. Draft "
        "teammate and team-channel messages only when evidence or due protocol "
        "obligations require a useful follow-up. Include no_message_decisions "
        "for mapped teammates without a draft.\n"
        "8. Review: run a reviewer pass over reconciliation choices, evidence "
        "answers, service updates, diffs or no-diff reason, drafts, no-message "
        "decisions, and continuity. Record it in reviewer_notes.\n"
        "9. Return JSON: update a concise continuity_note under 4096 "
        "characters and return only the JSON shape below.\n\n"
        "PM communication protocol rules:\n"
        "- pm_communication_protocol.json is the programmatic source of "
        "required communication obligations and prior evidence. Any obligation "
        "with due=true must be satisfied by at least one draft message.\n"
        "- For every due obligation, add pm_evidence_claims to the relevant "
        "draft message with the required evidence_type values and exact "
        "obligation_id. A no_message_decision cannot satisfy a due obligation.\n"
        "- Due process and resource planning obligations may include a "
        "message_artifact with service-rendered message_markdown. Use that "
        "markdown artifact instead of inventing task-list formatting.\n"
        "- Every new Slack message collected for the PM bot requires at least "
        "a message_receipt_ack claim in a DM or channel response. If the "
        "project was updated from a teammate's information, include a "
        "project_update_notice claim in the message that explains what changed.\n\n"
        "Slack formatting rules:\n"
        "- Return only `message_markdown` for each draft message. Do not "
        "return `text`, `body`, `blocks`, or any Block Kit JSON in Codex "
        "output.\n"
        "- Use readable Markdown with headings, short paragraphs, and "
        "newline-separated lists when a message has multiple tasks, decisions, "
        "blockers, status lines, or questions.\n"
        "- The runner converts `message_markdown` into Slack Block Kit and "
        "derives the plain fallback/audit body programmatically. Simple "
        "one-paragraph messages may remain one paragraph of markdown.\n\n"
        "Teammate-facing message rules:\n"
        "- Assume teammates do not have access to ProjDash, the dashboard, "
        "graphs, schedule calculations, or project-manager tooling.\n"
        "- Do not ask teammates to inspect the UI or refer to graph/node, "
        "ES/EF, LS/LF, slack, schedule buffer, sensitivity, critical path, "
        "schedule snapshot, resource-aware schedule, process ids, role ids, "
        "blocker ids, or similar internal terms.\n"
        "- Translate internal findings into plain context: task name, current "
        "understanding, why it matters, what date or time window is at risk, "
        "and the specific reply or action requested.\n"
        "- Do not merely check in. Provide the relevant project state, then ask "
        "for the smallest useful confirmation, correction, estimate, blocker "
        "update, or next action.\n\n"
        "Return only JSON with this shape:\n"
        "{\n"
        '  "summary": "brief run summary",\n'
        '  "evidence_line_answers": [\n'
        "    {\n"
        '      "evidence_line_id": "service evidence line id",\n'
        '      "question": "service evidence question",\n'
        '      "answer": "yes or no",\n'
        '      "reason": "source-backed reason",\n'
        '      "outcome": "what this means for PM state or null",\n'
        '      "update_intent": "applied update, skipped update, or null",\n'
        '      "source_keys": ["message key, context line, or artifact id"]\n'
        "    }\n"
        "  ],\n"
        '  "reviewer_notes": [\n'
        "    {\n"
        '      "reviewer": "reviewer label or null",\n'
        '      "status": "approved, needs_changes, or noted",\n'
        '      "note": "review finding",\n'
        '      "required_changes": []\n'
        "    }\n"
        "  ],\n"
        '  "project_updates": ["validated project update"],\n'
        '  "continuity_note": {\n'
        '    "schema_version": 3,\n'
        '    "summary": "short continuity summary",\n'
        '    "generated_at": "current ISO datetime",\n'
        '    "note": "simple next-run continuity note",\n'
        '    "next_run_focus": ["what the next run should inspect first"]\n'
        "  },\n"
        '  "draft_messages": [\n'
        "    {\n"
        '      "teammate_id": "service teammate id",\n'
        '      "slack_user_id": "Slack user id",\n'
        '      "message_markdown": "markdown message to send",\n'
        '      "reason": "brief source-backed reason",\n'
        '      "source_message_keys": ["conversation:ts"],\n'
        '      "pm_evidence_claims": []\n'
        "    }\n"
        "  ],\n"
        '  "team_channel_draft_messages": [\n'
        "    {\n"
        '      "channel_id": "configured Slack project channel id",\n'
        '      "channel_name": "channel name or null",\n'
        '      "message_markdown": "markdown message to send to the team channel",\n'
        '      "reason": "brief source-backed reason",\n'
        '      "source_message_keys": ["conversation:ts"],\n'
        '      "pm_evidence_claims": []\n'
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
        "Do not send Slack messages. If there are no useful teammate drafts, "
        "return an empty draft_messages array. If the team channel does not "
        "need a message, return an empty team_channel_draft_messages array."
    )


def _codex_correction_prompt(
    base_prompt: str,
    *,
    attempt: int,
    max_attempts: int,
    validation_error: str,
    previous_stdout: str,
) -> str:
    previous = previous_stdout.strip()
    if len(previous) > 12000:
        previous = previous[:12000] + "\n... [truncated previous output]"
    return (
        f"{base_prompt}\n\n"
        "Your previous response did not satisfy the strict JSON schema and was "
        "not accepted. Correct the response and try again.\n\n"
        f"Correction attempt: {attempt} of {max_attempts}.\n\n"
        "Validation error:\n"
        f"{validation_error}\n\n"
        "Previous response:\n"
        "```json\n"
        f"{previous}\n"
        "```\n\n"
        "Return only the corrected JSON object. Do not include markdown fences, "
        "comments, explanations, extra keys, or schema shorthand. In particular, "
        "ownership fields must be `owner_type` and `owner_id`; do not emit an "
        "`owner` field."
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


def _recover_codex_output_from_artifacts(
    *,
    run_dir: Path,
    reconciled_dir: Path,
    not_before: float,
    config: SlackProjectConfig,
    pm_protocol_context: dict[str, Any],
    pm_agent_context: dict[str, Any] | str | None,
) -> CodexDraftOutput | None:
    """Recover from valid JSON artifacts when Codex stdout is not final JSON."""
    for path in _codex_output_artifact_candidates(
        run_dir,
        reconciled_dir,
        not_before=not_before,
    ):
        try:
            parsed_output = _parse_codex_output(path.read_text(encoding="utf-8"))
            parsed_output = _normalize_codex_output_for_pm_protocol(
                parsed_output,
                pm_protocol_context,
            )
            _validate_codex_reconciliation_output(
                parsed_output,
                config,
                pm_protocol_context=pm_protocol_context,
                pm_agent_context=pm_agent_context,
            )
        except (OSError, UnicodeDecodeError, IntegrationError):
            continue
        return parsed_output
    return None


def _codex_output_artifact_candidates(
    run_dir: Path,
    reconciled_dir: Path,
    *,
    not_before: float,
) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for directory in (run_dir, reconciled_dir):
        if not directory.exists():
            continue
        for pattern in ("codex_output*.json", "pm_flow_result*.json"):
            for path in directory.glob(pattern):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                try:
                    if path.stat().st_mtime + 1.0 < not_before:
                        continue
                except OSError:
                    continue
                candidates.append(path)
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates


def _normalize_codex_output_for_pm_protocol(
    codex_output: CodexDraftOutput,
    pm_protocol_context: dict[str, Any],
) -> CodexDraftOutput:
    """Fill service-derived PM claim fields and attach required artifacts."""
    obligations = [
        obligation
        for obligation in pm_protocol_context.get("obligations", []) or []
        if isinstance(obligation, dict) and obligation.get("obligation_id")
    ]
    if not obligations:
        return codex_output
    obligations_by_id = {
        str(obligation["obligation_id"]): obligation for obligation in obligations
    }
    return codex_output.model_copy(
        update={
            "draft_messages": [
                _normalize_draft_pm_claims(draft, obligations_by_id)
                for draft in codex_output.draft_messages
            ],
            "team_channel_draft_messages": [
                _normalize_draft_pm_claims(draft, obligations_by_id)
                for draft in codex_output.team_channel_draft_messages
            ],
        },
    )


def _normalize_draft_pm_claims(
    draft: SlackDraftMessage | SlackTeamChannelDraftMessage,
    obligations_by_id: dict[str, dict[str, Any]],
) -> SlackDraftMessage | SlackTeamChannelDraftMessage:
    claims = [
        _normalize_pm_evidence_claim(claim, obligations_by_id)
        for claim in draft.pm_evidence_claims
    ]
    message_markdown = _message_markdown_with_required_pm_artifacts(
        draft.message_markdown,
        claims,
        obligations_by_id,
    )
    return draft.model_copy(
        update={
            "message_markdown": message_markdown,
            "pm_evidence_claims": claims,
        },
    )


def _normalize_pm_evidence_claim(
    claim: PMEvidenceClaim,
    obligations_by_id: dict[str, dict[str, Any]],
) -> PMEvidenceClaim:
    if not claim.obligation_id:
        return claim
    obligation = obligations_by_id.get(claim.obligation_id)
    if obligation is None:
        return claim
    updates = {}
    for field_name in ("resource_id", "process_id", "process_symbol", "content_hash"):
        expected = obligation.get(field_name)
        if expected and getattr(claim, field_name) is None:
            updates[field_name] = expected
    if not updates:
        return claim
    return claim.model_copy(update=updates)


def _message_markdown_with_required_pm_artifacts(
    message_markdown: str,
    claims: list[PMEvidenceClaim],
    obligations_by_id: dict[str, dict[str, Any]],
) -> str:
    additions = []
    for claim in claims:
        if claim.evidence_type not in {
            "process_full_update",
            "process_pre_start_3_day",
            "process_pre_start_24_hour",
            "process_overdue_checkin",
            "process_in_progress_checkin",
            "resource_assignment_review",
        }:
            continue
        if not claim.obligation_id:
            continue
        obligation = obligations_by_id.get(claim.obligation_id)
        if obligation is None or _pm_claim_message_artifact_satisfied(
            message_markdown,
            obligation,
        ):
            continue
        artifact = obligation.get("message_artifact")
        if not isinstance(artifact, dict):
            continue
        artifact_markdown = str(
            artifact.get("message_markdown")
            or artifact.get("required_visible_text")
            or "",
        ).strip()
        if (
            artifact_markdown
            and artifact_markdown not in message_markdown
            and artifact_markdown not in additions
        ):
            additions.append(artifact_markdown)
    if not additions:
        return message_markdown
    return f"{message_markdown.rstrip()}\n\n" + "\n\n".join(additions)


def _validate_codex_reconciliation_output(
    codex_output: CodexDraftOutput,
    config: SlackProjectConfig,
    *,
    pm_protocol_context: dict[str, Any] | None = None,
    pm_agent_context: dict[str, Any] | str | None = None,
) -> None:
    mapping_by_slack_user = {
        entry.slack_user_id: entry for entry in config.resource_slack_map
    }

    covered_slack_users: set[str] = set()
    for draft in codex_output.draft_messages:
        if not mapping_by_slack_user:
            raise IntegrationError(
                "Codex output included teammate drafts, but no mapped Slack "
                "users are configured."
            )
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
    allowed_channels = {channel.channel_id for channel in config.channels}
    for draft in codex_output.team_channel_draft_messages:
        if draft.channel_id not in allowed_channels:
            raise IntegrationError(
                "Codex team-channel draft targeted an unconfigured Slack "
                f"channel: {draft.channel_id!r}."
            )

    missing = sorted(set(mapping_by_slack_user) - covered_slack_users)
    if mapping_by_slack_user and missing:
        raise IntegrationError(
            "Codex output omitted draft/no-message decisions for mapped Slack "
            f"users: {', '.join(missing)}."
        )
    _validate_continuity_note(codex_output.continuity_note)
    if pm_agent_context is not None:
        _validate_evidence_line_answer_coverage(codex_output, pm_agent_context)
    _validate_pm_evidence_claims(codex_output, pm_protocol_context or {})


def _validate_evidence_line_answer_coverage(
    codex_output: CodexDraftOutput,
    pm_agent_context: dict[str, Any] | str,
) -> None:
    required = _pm_agent_context_evidence_line_ids(pm_agent_context)
    if not required:
        return
    answered_ids = [answer.evidence_line_id for answer in codex_output.evidence_line_answers]
    answered = set(answered_ids)
    duplicate_ids = sorted(
        evidence_line_id
        for evidence_line_id in answered
        if answered_ids.count(evidence_line_id) > 1
    )
    missing = sorted(set(required) - answered)
    extra = sorted(answered - set(required))
    errors = []
    if missing:
        errors.append(f"missing evidence line answers: {', '.join(missing)}")
    if extra:
        errors.append(f"unknown evidence line answers: {', '.join(extra)}")
    if duplicate_ids:
        errors.append(f"duplicate evidence line answers: {', '.join(duplicate_ids)}")
    if errors:
        raise IntegrationError(
            "Codex output must answer every service-prepared evidence line item: "
            f"{'; '.join(errors)}."
        )


def _validate_pm_evidence_claims(
    codex_output: CodexDraftOutput,
    pm_protocol_context: dict[str, Any],
) -> None:
    protocol_obligations = [
        obligation
        for obligation in pm_protocol_context.get("obligations", []) or []
        if isinstance(obligation, dict) and obligation.get("obligation_id")
    ]
    obligations_by_id = {
        str(obligation["obligation_id"]): obligation for obligation in protocol_obligations
    }
    due_obligations = [
        obligation
        for obligation in protocol_obligations
        if isinstance(obligation, dict) and obligation.get("due")
    ]
    claims_by_obligation: dict[
        str, list[tuple[PMEvidenceClaim, str, str, str]]
    ] = {}
    all_claims: list[tuple[PMEvidenceClaim, str, str, str]] = []
    for draft in codex_output.draft_messages:
        for claim in draft.pm_evidence_claims:
            item = (claim, "dm", draft.slack_user_id, draft.message_markdown)
            all_claims.append(item)
            if claim.obligation_id:
                claims_by_obligation.setdefault(claim.obligation_id, []).append(item)
    for draft in codex_output.team_channel_draft_messages:
        for claim in draft.pm_evidence_claims:
            item = (claim, "channel", draft.channel_id, draft.message_markdown)
            all_claims.append(item)
            if claim.obligation_id:
                claims_by_obligation.setdefault(claim.obligation_id, []).append(item)

    if not due_obligations and not codex_output.project_updates and not all_claims:
        return

    claim_errors = []
    for claim, target_type, target_id, draft_markdown in all_claims:
        if claim.evidence_type == "project_update_notice":
            continue
        if not claim.obligation_id:
            claim_errors.append(
                f"{claim.evidence_type} claims require an obligation_id."
            )
            continue
        obligation = obligations_by_id.get(claim.obligation_id)
        if obligation is None:
            claim_errors.append(
                f"{claim.evidence_type} claim references unknown obligation "
                f"{claim.obligation_id!r}."
            )
            continue
        try:
            _validate_pm_claim_target(claim, target_type, target_id, obligation)
        except IntegrationError as exc:
            claim_errors.append(str(exc))
            continue
        try:
            _validate_pm_claim_message_artifact(
                claim,
                draft_markdown,
                obligation,
            )
        except IntegrationError as exc:
            claim_errors.append(str(exc))
            continue
        required_types = set(obligation.get("required_evidence_types") or [])
        if claim.evidence_type not in required_types:
            claim_errors.append(
                f"{claim.evidence_type} is not required by {claim.obligation_id!r}."
            )
    if claim_errors:
        raise IntegrationError(
            "Codex output included invalid PM evidence claims: "
            f"{'; '.join(claim_errors)}."
        )

    missing_obligations = []
    for obligation in due_obligations:
        obligation_id = str(obligation.get("obligation_id") or "")
        if not obligation_id:
            continue
        claims = claims_by_obligation.get(obligation_id, [])
        required_types = set(obligation.get("required_evidence_types") or [])
        validated_types = set()
        invalid_claim_errors = []
        for claim, target_type, target_id, draft_markdown in claims:
            try:
                _validate_pm_claim_target(claim, target_type, target_id, obligation)
                _validate_pm_claim_message_artifact(
                    claim,
                    draft_markdown,
                    obligation,
                )
            except IntegrationError as exc:
                invalid_claim_errors.append(str(exc))
                continue
            validated_types.add(claim.evidence_type)
        if not required_types.issubset(validated_types):
            missing_obligations.append(
                f"{obligation_id} requires {sorted(required_types - validated_types)}"
            )
            if invalid_claim_errors:
                missing_obligations.extend(invalid_claim_errors)
    if missing_obligations:
        raise IntegrationError(
            "Codex output omitted required PM evidence claims for due "
            f"communication obligations: {'; '.join(missing_obligations)}."
        )

    if codex_output.project_updates and not any(
        claim.evidence_type == "project_update_notice"
        for claim, _target_type, _target, _markdown in all_claims
    ):
        raise IntegrationError(
            "Codex output listed project_updates but did not include a "
            "project_update_notice PM evidence claim on any draft message."
        )


def _validate_pm_claim_target(
    claim: PMEvidenceClaim,
    target_type: str,
    target_id: str,
    obligation: dict[str, Any],
) -> None:
    # Claims are proof metadata, so the draft must bind them to the exact
    # service-computed obligation: target, resource, process, and content hash.
    # Future evidence types should add their invariant checks here.
    expected_target_type = obligation.get("target_type")
    if expected_target_type and target_type != expected_target_type:
        raise IntegrationError(
            f"PM evidence claim {claim.obligation_id!r} targeted {target_type}, "
            f"but obligation requires {expected_target_type}."
        )
    expected_user = obligation.get("slack_user_id")
    if expected_user and target_id != expected_user:
        raise IntegrationError(
            f"PM evidence claim {claim.obligation_id!r} targeted Slack user "
            f"{target_id}, but obligation requires {expected_user}."
        )
    expected_channel = obligation.get("slack_channel_id")
    if expected_channel and target_id != expected_channel:
        raise IntegrationError(
            f"PM evidence claim {claim.obligation_id!r} targeted channel "
            f"{target_id}, but obligation requires {expected_channel}."
        )
    expected_resource = obligation.get("resource_id")
    if expected_resource and claim.resource_id != expected_resource:
        raise IntegrationError(
            f"PM evidence claim {claim.obligation_id!r} must use resource "
            f"{expected_resource}."
        )
    expected_process_id = obligation.get("process_id")
    if expected_process_id and claim.process_id != expected_process_id:
        raise IntegrationError(
            f"PM evidence claim {claim.obligation_id!r} must use process id "
            f"{expected_process_id}."
        )
    expected_process_symbol = obligation.get("process_symbol")
    if expected_process_symbol and claim.process_symbol != expected_process_symbol:
        raise IntegrationError(
            f"PM evidence claim {claim.obligation_id!r} must use process symbol "
            f"{expected_process_symbol}."
        )
    expected_content_hash = obligation.get("content_hash")
    if expected_content_hash and claim.content_hash != expected_content_hash:
        raise IntegrationError(
            f"PM evidence claim {claim.obligation_id!r} must use content hash "
            f"{expected_content_hash}."
        )


def _validate_pm_claim_message_artifact(
    claim: PMEvidenceClaim,
    draft_markdown: str,
    obligation: dict[str, Any],
) -> None:
    if claim.evidence_type not in {
        "process_full_update",
        "process_pre_start_3_day",
        "process_pre_start_24_hour",
        "process_overdue_checkin",
        "process_in_progress_checkin",
        "resource_assignment_review",
    }:
        return
    if _pm_claim_message_artifact_satisfied(draft_markdown, obligation):
        return
    raise IntegrationError(
        f"PM evidence claim {claim.obligation_id!r} omitted the "
        "service-generated message artifact. Include the query-provided "
        "message_markdown for the process or assignment list."
    )


def _pm_claim_message_artifact_satisfied(
    draft_markdown: str,
    obligation: dict[str, Any],
) -> bool:
    artifact = obligation.get("message_artifact")
    if not isinstance(artifact, dict):
        return True
    draft_blocks = _blocks_for_draft(draft_markdown)
    visible_text = "\n".join(
        part
        for part in (
            draft_markdown,
            "\n".join(_iter_slack_block_text(draft_blocks)),
        )
        if part
    )
    required_candidates = [
        str(candidate).strip()
        for candidate in (
            artifact.get("required_visible_text"),
            artifact.get("message_markdown"),
        )
        if str(candidate or "").strip()
    ]
    for required_text in required_candidates:
        required_lines = _required_artifact_lines(required_text)
        if required_lines and all(line in visible_text for line in required_lines):
            return True
    if not required_candidates:
        return True
    return False


def _required_artifact_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped in {"---", "*"}:
            continue
        lines.append(stripped)
    return lines


def _validate_continuity_note(continuity_note: SlackContinuityNote | str) -> None:
    if isinstance(continuity_note, str):
        if not continuity_note.strip():
            raise IntegrationError("Continuity note must not be blank.")
        return
    if not continuity_note.summary and not continuity_note.note:
        raise IntegrationError("Continuity note must include summary or note.")


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
    team_drafts: list[SlackTeamChannelDraftMessage],
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
        body = draft.message_markdown
        blocks = _blocks_for_draft(body)
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
                "body": body,
                "blocks": blocks,
                "content_hash": _message_content_hash(body, blocks),
                "run_id": run_id,
                "created_at": created_at.isoformat(),
                "pm_evidence_claims": [
                    claim.model_dump(mode="json")
                    for claim in draft.pm_evidence_claims
                ],
            }
        )
    allowed_channels = {channel.channel_id for channel in config.channels}
    for draft in team_drafts:
        body = draft.message_markdown
        blocks = _blocks_for_draft(body)
        if draft.channel_id not in allowed_channels:
            raise IntegrationError(
                f"Codex channel draft targeted unconfigured channel {draft.channel_id!r}."
            )
        messages.append(
            {
                "target_type": "channel",
                "slack_channel_id": draft.channel_id,
                "body": body,
                "blocks": blocks,
                "content_hash": _message_content_hash(body, blocks),
                "run_id": run_id,
                "created_at": created_at.isoformat(),
                "pm_evidence_claims": [
                    claim.model_dump(mode="json")
                    for claim in draft.pm_evidence_claims
                ],
            }
        )
    return messages


def _message_content_hash(text: str, blocks: list[dict[str, Any]]) -> str:
    payload = {"body": text, "blocks": blocks or []}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _content_hash(text: str) -> str:
    return _message_content_hash(text, [])


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
    allowed_channel_ids = {channel.channel_id for channel in config.channels}
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
        target_type = row.get("target_type") or (
            "channel" if channel and not slack_user_id else "dm"
        )
        text = row.get("text") or row.get("body")
        blocks = row.get("blocks") or []
        if target_type == "channel":
            if channel not in allowed_channel_ids:
                gateway.mark_slack_outbox_failed(
                    project_id=project_id,
                    outbox_id=outbox_id,
                    failed_at=now,
                    error="Pending Slack outbox row targets an unconfigured channel.",
                )
                result["failed"] += 1
                continue
        elif slack_user_id not in allowed_slack_user_ids:
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
            message_payload = {"channel": channel, "text": text}
            if blocks:
                message_payload["blocks"] = blocks
            response = client.chat_postMessage(**message_payload)
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
        for claim in row.get("pm_evidence_claims") or []:
            if not isinstance(claim, dict):
                continue
            gateway.record_pm_communication_evidence(
                project_id=project_id,
                evidence_type=str(claim.get("evidence_type") or ""),
                outbox_id=outbox_id,
                communicated_at=now,
                resource_id=claim.get("resource_id"),
                slack_user_id=slack_user_id,
                slack_channel_id=response.get("channel", channel),
                process_id=claim.get("process_id"),
                process_symbol=claim.get("process_symbol"),
                obligation_id=claim.get("obligation_id"),
                run_id=row.get("run_id"),
                content_hash=claim.get("content_hash") or row.get("content_hash"),
                evidence_note=claim.get("evidence_note"),
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


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
