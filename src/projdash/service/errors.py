"""Service-level exceptions and structured error models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_serializer


class ValidationIssue(BaseModel):
    """One structured validation issue from an envelope root."""

    model_config = ConfigDict(extra="forbid")

    loc: list[str | int]
    msg: str
    type: str
    input: Any | None = None
    ctx: dict[str, Any] = Field(default_factory=dict)


class Error(BaseModel):
    """Structured command/query error payload."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    validation_errors: list[ValidationIssue] | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if self.code != "validation_error" or self.validation_errors is None:
            data.pop("validation_errors", None)
        return data


class ServiceValidationError(ValueError):
    """Raised when a validated command violates domain invariants."""

    def __init__(
        self,
        code: str,
        message: str,
        field_path: str | None = None,
        entity_id: str | None = None,
        details: dict[str, Any] | None = None,
        validation_errors: list[ValidationIssue] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field_path = field_path
        self.entity_id = entity_id
        self.details = details or {}
        self.validation_errors = validation_errors

    def to_error(self) -> Error:
        """Return the shared structured error model."""
        details = dict(self.details)
        if self.field_path is not None:
            details.setdefault("field_path", self.field_path)
        if self.entity_id is not None:
            details.setdefault("entity_id", self.entity_id)
        return Error(
            code=self.code,
            message=self.message,
            details=details,
            validation_errors=(
                self.validation_errors if self.code == "validation_error" else None
            ),
        )

    def to_json_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable error payload."""
        return self.to_error().model_dump(mode="json", exclude_none=True)
