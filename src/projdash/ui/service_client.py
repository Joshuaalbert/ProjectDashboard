"""Small helpers for the service-backed Streamlit UI."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from projdash.service.commands import BatchCommandEnvelope, CommandEnvelope
from projdash.service.ladybug_repository import LadybugProjectRepository
from projdash.service.queries import QueryEnvelope
from projdash.service.service import ProjectService

DEFAULT_TIMEZONE = "UTC"
DISPLAY_DATETIME_FORMAT = "%a, %d %b %Y, %H:%M"
DISPLAY_DATETIME_KEYS = {
    "as_of",
    "available_from_at",
    "available_until_at",
    "committed_at",
    "completion_at",
    "created_at",
    "current_due_at",
    "dep_finish",
    "dep_start",
    "derived_due_at",
    "due_at",
    "edit_at",
    "ef_at",
    "ends_at",
    "finished_at",
    "horizon_ends_at",
    "horizon_starts_at",
    "lf_at",
    "ls_at",
    "opened_at",
    "ready_at",
    "resolved_at",
    "resource_finish",
    "resource_start",
    "started_at",
    "starts_at",
}


@dataclass(frozen=True)
class RoleSeed:
    """Role row parsed from a compact guided form."""

    role_id: str
    name: str


@dataclass(frozen=True)
class ResourceSeed:
    """Resource row parsed from a compact guided form."""

    name: str
    role_ids: list[str]
    cost_rate: str


@dataclass(frozen=True)
class CalendarSeed:
    """Calendar option used by project management forms."""

    calendar_id: str
    label: str


@dataclass(frozen=True)
class ProjectOption:
    """Project option used by the sidebar selector."""

    project_id: str
    label: str


def create_project_service(db_path: str) -> ProjectService:
    """Create the in-process service backed by the LadybugDB repository."""
    repository = LadybugProjectRepository(db_path)
    repository.initialize_schema()
    return ProjectService(repository)


def combine_datetime(
    date_value: dt.date,
    time_value: dt.time,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dt.datetime:
    """Combine form date/time fields into a timezone-aware datetime."""
    return dt.datetime.combine(date_value, time_value).replace(
        tzinfo=ZoneInfo(timezone_name),
    )


def validate_timezone(timezone_name: str) -> str:
    """Validate an IANA timezone name for sidebar controls."""
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Timezone must be a valid IANA timezone name.") from exc
    return timezone_name


def format_display_datetime(
    value: Any,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> str:
    """Format a service datetime for visible UI text in the selected timezone."""
    if value is None or value == "":
        return "-"
    return to_display_timezone(value, timezone_name).strftime(DISPLAY_DATETIME_FORMAT)


def to_display_timezone(
    value: Any,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dt.datetime:
    """Convert a service datetime into the selected UI timezone."""
    timezone = ZoneInfo(validate_timezone(timezone_name))
    parsed = _parse_datetime_value(value)
    if parsed.tzinfo is None:
        raise ValueError("Visible datetimes must be timezone-aware.")
    return parsed.astimezone(timezone)


def format_display_datetimes(
    value: Any,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> Any:
    """Recursively format timestamp fields for visible UI tables."""
    if isinstance(value, list):
        return [format_display_datetimes(item, timezone_name) for item in value]
    if isinstance(value, tuple):
        return tuple(format_display_datetimes(item, timezone_name) for item in value)
    if isinstance(value, dict):
        formatted = {}
        for key, item in value.items():
            if _is_display_datetime_key(str(key)) and item is not None and item != "":
                try:
                    formatted[key] = format_display_datetime(item, timezone_name)
                except (TypeError, ValueError):
                    formatted[key] = item
            else:
                formatted[key] = format_display_datetimes(item, timezone_name)
        return formatted
    return value


def _is_display_datetime_key(key: str) -> bool:
    return key in DISPLAY_DATETIME_KEYS or key.endswith("_at")


def _parse_datetime_value(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def split_lines(value: str) -> list[str]:
    """Return non-empty stripped lines."""
    return [line.strip() for line in value.splitlines() if line.strip()]


def split_csv(value: str) -> list[str]:
    """Return non-empty comma-separated values."""
    return [part.strip() for part in value.split(",") if part.strip()]


def stable_id(prefix: str, label: str) -> str:
    """Create a stable service id from a user-facing label."""
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    if not slug:
        slug = "item"
    if slug.startswith(f"{prefix}_"):
        return slug
    return f"{prefix}_{slug}"


def scoped_id(project_id: str, prefix: str, label: str) -> str:
    """Create a globally stable id namespaced by project."""
    return stable_id(prefix, f"{project_id}_{label}")


def project_options(projects: list[dict[str, Any]]) -> list[ProjectOption]:
    """Build display options for project selection."""
    return [
        ProjectOption(
            project_id=project["project_id"],
            label=f"{project['name']} ({project['project_id']})",
        )
        for project in sorted(
            projects,
            key=lambda item: (str(item["name"]).casefold(), item["project_id"]),
        )
    ]


def calendar_options(calendars: list[dict[str, Any]]) -> list[CalendarSeed]:
    """Build display options for calendar selection."""
    return [
        CalendarSeed(
            calendar_id=calendar["calendar_id"],
            label=f"{calendar['name']} ({calendar['calendar_id']})",
        )
        for calendar in sorted(
            calendars,
            key=lambda item: (str(item["name"]).casefold(), item["calendar_id"]),
        )
    ]


def parse_role_lines(value: str) -> list[RoleSeed]:
    """Parse role lines as `role_id: Name`, `role_id=Name`, or plain names."""
    roles: list[RoleSeed] = []
    seen: set[str] = set()
    for line in split_lines(value):
        role_id: str
        name: str
        if ":" in line:
            role_id, name = [part.strip() for part in line.split(":", 1)]
        elif "=" in line:
            role_id, name = [part.strip() for part in line.split("=", 1)]
        else:
            name = line
            role_id = stable_id("role", line)
        if not role_id or not name:
            raise ValueError("Role lines must include both an id and name.")
        if role_id in seen:
            raise ValueError(f"Duplicate role id: {role_id}")
        seen.add(role_id)
        roles.append(RoleSeed(role_id=role_id, name=name))
    return roles


def parse_resource_lines(value: str) -> list[ResourceSeed]:
    """Parse resources as `Name | role_a, role_b | hourly_rate`."""
    resources: list[ResourceSeed] = []
    for line in split_lines(value):
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in {2, 3}:
            raise ValueError(
                "Resource lines must use `Name | role_a, role_b | hourly_rate`.",
            )
        name = parts[0]
        role_ids = split_csv(parts[1])
        cost_rate = parts[2] if len(parts) == 3 and parts[2] else "0"
        if not name or not role_ids:
            raise ValueError("Resource lines must include a name and role ids.")
        resources.append(ResourceSeed(name=name, role_ids=role_ids, cost_rate=cost_rate))
    return resources


def parse_holiday_lines(
    value: str,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> list[dict[str, Any]]:
    """Parse resource holidays from compact date rows or exact interval rows.

    Accepted forms are:
    - `YYYY-MM-DD..YYYY-MM-DD | reason`
    - `holiday_id | YYYY-MM-DD..YYYY-MM-DD | reason`
    - `holiday_id | starts_at | ends_at | reason`
    """
    holidays = []
    timezone = ZoneInfo(timezone_name)
    for line in split_lines(value):
        parts = [part.strip() for part in line.split("|")]
        if len(parts) >= 3 and not _holiday_text_starts_with_date(parts[0]):
            holiday_id = parts[0] or None
            if _can_parse_holiday_endpoint(parts[1], timezone) and (
                _can_parse_holiday_endpoint(parts[2], timezone)
            ):
                holiday = {
                    "starts_at": _parse_holiday_endpoint(parts[1], timezone),
                    "ends_at": _parse_holiday_endpoint(parts[2], timezone),
                    "reason": " | ".join(parts[3:]).strip() or None,
                }
                if holiday_id is not None:
                    holiday["holiday_id"] = holiday_id
                holidays.append(holiday)
                continue
            holidays.append(
                _holiday_from_date_part(
                    parts[1],
                    " | ".join(parts[2:]),
                    timezone,
                    holiday_id,
                )
            )
            continue
        date_part = parts[0]
        reason = " | ".join(parts[1:]) if len(parts) > 1 else ""
        holidays.append(_holiday_from_date_part(date_part, reason, timezone))
    return holidays


def _holiday_text_starts_with_date(value: str) -> bool:
    return len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-"


def _parse_holiday_endpoint(value: str, timezone: ZoneInfo) -> dt.datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        parsed = dt.datetime.combine(dt.date.fromisoformat(text), dt.time.min)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed


def _can_parse_holiday_endpoint(value: str, timezone: ZoneInfo) -> bool:
    try:
        _parse_holiday_endpoint(value, timezone)
    except ValueError:
        return False
    return True


def _holiday_from_date_part(
    date_part: str,
    reason: str,
    timezone: ZoneInfo,
    holiday_id: str | None = None,
) -> dict[str, Any]:
    start_text, separator, end_text = date_part.strip().partition("..")
    start_date = dt.date.fromisoformat(start_text.strip())
    if separator:
        end_date = dt.date.fromisoformat(end_text.strip())
    else:
        end_date = start_date
    starts_at = dt.datetime.combine(start_date, dt.time.min).replace(
        tzinfo=timezone,
    )
    ends_at = dt.datetime.combine(
        end_date + dt.timedelta(days=1),
        dt.time.min,
    ).replace(tzinfo=timezone)
    holiday = {
        "starts_at": starts_at,
        "ends_at": ends_at,
        "reason": reason.strip() or None,
    }
    if holiday_id is not None:
        holiday["holiday_id"] = holiday_id
    return holiday


def parse_dependency_lines(value: str) -> list[tuple[str, str]]:
    """Parse topology dependencies as `A -> B` or `A, B` pairs."""
    dependencies: list[tuple[str, str]] = []
    for line in split_lines(value):
        if "->" in line:
            predecessor, successor = [part.strip() for part in line.split("->", 1)]
        elif "," in line:
            predecessor, successor = [part.strip() for part in line.split(",", 1)]
        else:
            raise ValueError("Dependency lines must use `A -> B` or `A, B`.")
        if not predecessor or not successor:
            raise ValueError("Dependency endpoints must be non-empty.")
        dependencies.append((predecessor, successor))
    return dependencies


def parse_subgraph_process_lines(value: str) -> list[dict[str, Any]]:
    """Parse child rows as `SYMBOL | Name | role_id:hours,...`.

    The service still accepts a diagnostic ``duration_hours`` field for
    topology rewrites. The UI derives it from total role effort so operators do
    not maintain two duration-like inputs for the same child process.
    """
    processes = []
    seen: set[str] = set()
    for line in split_lines(value):
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 3:
            raise ValueError(
                "Subgraph process lines must use `SYMBOL | Name | role_id:hours,...`."
            )
        symbol, name, effort_text = parts
        if not symbol or not name:
            raise ValueError("Subgraph process symbols and names must be non-empty.")
        if symbol in seen:
            raise ValueError(f"Duplicate subgraph process symbol: {symbol}")
        seen.add(symbol)
        process = {
            "process_symbol": symbol,
            "name": name,
        }
        role_requirements = _parse_role_effort_tokens(effort_text)
        process["role_requirements"] = role_requirements
        process["duration_hours"] = sum(
            requirement["effort_hours"] for requirement in role_requirements
        )
        processes.append(process)
    return processes


def infer_subgraph_roots_and_leaves(
    processes: list[dict[str, Any]],
    dependencies: list[tuple[str, str]],
) -> tuple[list[str], list[str]]:
    """Infer topological roots and leaves from child dependency rows.

    Args:
        processes: Parsed child process dictionaries with ``process_symbol``.
        dependencies: Parsed ``(predecessor_symbol, successor_symbol)`` pairs.

    Returns:
        Ordered root symbols and ordered leaf symbols, preserving child row
        order.

    Raises:
        ValueError: If a dependency endpoint is unknown or the child graph
            contains a directed cycle.
    """
    child_symbols = [
        str(process.get("process_symbol"))
        for process in processes
        if process.get("process_symbol")
    ]
    child_symbol_set = set(child_symbols)
    incoming = {symbol: set() for symbol in child_symbols}
    outgoing = {symbol: set() for symbol in child_symbols}
    for predecessor, successor in dependencies:
        if predecessor not in child_symbol_set or successor not in child_symbol_set:
            raise ValueError(
                "Child dependency endpoints must name supplied child processes."
            )
        incoming[successor].add(predecessor)
        outgoing[predecessor].add(successor)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(symbol: str) -> None:
        if symbol in visiting:
            raise ValueError("Child dependency graph must be acyclic.")
        if symbol in visited:
            return
        visiting.add(symbol)
        for successor in outgoing[symbol]:
            visit(successor)
        visiting.remove(symbol)
        visited.add(symbol)

    for symbol in child_symbols:
        visit(symbol)
    roots = [symbol for symbol in child_symbols if not incoming[symbol]]
    leaves = [symbol for symbol in child_symbols if not outgoing[symbol]]
    return roots, leaves


def _parse_role_effort_tokens(value: str) -> list[dict[str, Any]]:
    requirements = []
    seen: set[str] = set()
    for token in split_csv(value):
        role_id, separator, hours_text = token.partition(":")
        role_id = role_id.strip()
        if not separator or not role_id or not hours_text.strip():
            raise ValueError("Role effort tokens must use `role_id:hours`.")
        if role_id in seen:
            raise ValueError(f"Duplicate role id in child row: {role_id}")
        seen.add(role_id)
        effort_hours = float(hours_text)
        if effort_hours <= 0:
            raise ValueError("Role effort hours must be greater than 0.")
        requirements.append({"role_id": role_id, "effort_hours": effort_hours})
    if not requirements:
        raise ValueError("At least one role effort token is required.")
    return requirements


def command_envelope(command: Any) -> CommandEnvelope:
    """Wrap a command model for service execution."""
    return CommandEnvelope(command=command)


def command_payload_envelope(command: dict[str, Any]) -> CommandEnvelope:
    """Validate and wrap a command payload dictionary."""
    return CommandEnvelope.model_validate({"command": command})


def batch_envelope(commands: list[Any]) -> BatchCommandEnvelope:
    """Wrap command models in a transactional batch envelope."""
    return BatchCommandEnvelope(
        commands=[command_envelope(command) for command in commands],
    )


def batch_payload_envelope(commands: list[dict[str, Any]]) -> BatchCommandEnvelope:
    """Validate and wrap command payload dictionaries in a batch."""
    return BatchCommandEnvelope.model_validate(
        {"commands": [{"command": command} for command in commands]},
    )


def query_envelope(query: Any) -> QueryEnvelope:
    """Wrap a query model for service execution."""
    return QueryEnvelope(query=query)


def query_payload_envelope(query: dict[str, Any]) -> QueryEnvelope:
    """Validate and wrap a query payload dictionary."""
    return QueryEnvelope.model_validate({"query": query})


def result_to_dict(result: Any) -> dict[str, Any]:
    """Convert Pydantic result models to JSON-compatible dictionaries."""
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    return dict(result)
