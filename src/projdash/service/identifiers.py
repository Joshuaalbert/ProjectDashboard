"""Identifier and symbol helpers."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class IdentifierModel(BaseModel):
    """Base model for project-scoped identifier objects."""

    model_config = ConfigDict(extra="forbid")


class ProcessIdentity(IdentifierModel):
    """Process identity by id or symbol."""

    process_id: str | None = Field(default=None, min_length=1)
    process_symbol: str | None = Field(default=None, min_length=1)


class OperationIds(IdentifierModel):
    """Stable operation-level id collections."""

    operation_ids: list[object] = Field(default_factory=list)


def new_id() -> str:
    """Return a new opaque service identifier."""
    return str(uuid.uuid4())


def symbolify(text: str) -> str:
    """Create a compact human-readable symbol from a process name."""
    cleaned = text
    for char in "!@#$%^&*()_-=+":
        cleaned = cleaned.replace(char, " ")

    parts = []
    for token in cleaned.split():
        stripped = token.strip()
        if not stripped:
            continue
        if stripped.isnumeric() or stripped.upper() == stripped:
            parts.append(stripped)
        else:
            parts.append(stripped.upper()[0])
    return "".join(parts) or "P"
