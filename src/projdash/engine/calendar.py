"""Business-day helpers and pure resource calendar expansion."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.UTC


@dataclass(frozen=True, slots=True)
class CapacityBucket:
    """Expanded capacity for one resource over one UTC half-open interval."""

    resource_id: str
    calendar_id: str
    starts_at: dt.datetime
    ends_at: dt.datetime
    capacity_hours: float
    available_hours: float
    allocated_hours: float
    remaining_hours: float
    role_ids: tuple[str, ...]
    local_date: str
    local_week: str


@dataclass(frozen=True, slots=True)
class _WeeklyWindow:
    window_id: str
    weekday: int
    starts_at: dt.time
    ends_at: dt.time
    capacity_hours: float


@dataclass(frozen=True, slots=True)
class _CalendarException:
    exception_id: str
    starts_at: dt.datetime
    ends_at: dt.datetime
    capacity_hours: float


@dataclass(frozen=True, slots=True)
class _ResourceHoliday:
    holiday_id: str
    starts_at: dt.datetime
    ends_at: dt.datetime


@dataclass(frozen=True, slots=True)
class _CapacityInterval:
    starts_at: dt.datetime
    ends_at: dt.datetime
    capacity_hours: float
    local_date: str
    local_week: str


def require_aware(value: dt.datetime) -> dt.datetime:
    """Validate that a datetime carries timezone information."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def next_business_day(value: dt.datetime) -> dt.datetime:
    """Return `value` if it is a weekday, otherwise the following Monday."""
    value = require_aware(value)
    if value.weekday() < 5:
        return value
    return value + dt.timedelta(days=7 - value.weekday())


def previous_business_day(value: dt.datetime) -> dt.datetime:
    """Return `value` if it is a weekday, otherwise the previous Friday."""
    value = require_aware(value)
    if value.weekday() < 5:
        return value
    return value - dt.timedelta(days=value.weekday() - 4)


def add_business_days(value: dt.datetime, days: int) -> dt.datetime:
    """Add weekday-only business days.

    Args:
        value: Starting timezone-aware datetime.
        days: Non-negative number of business days to add.

    Returns:
        The timezone-aware datetime reached after adding `days` business days.

    Raises:
        ValueError: If `days` is negative or `value` is timezone-naive.
    """
    if days < 0:
        raise ValueError("days must be non-negative")

    output = previous_business_day(value)
    count = 0
    while count < days:
        output += dt.timedelta(days=1)
        if output.weekday() < 5:
            count += 1
    return output


def subtract_business_days(value: dt.datetime, days: int) -> dt.datetime:
    """Subtract weekday-only business days.

    Args:
        value: Starting timezone-aware datetime.
        days: Non-negative number of business days to subtract.

    Returns:
        The timezone-aware datetime reached after subtracting `days`.

    Raises:
        ValueError: If `days` is negative or `value` is timezone-naive.
    """
    if days < 0:
        raise ValueError("days must be non-negative")

    output = next_business_day(value)
    count = 0
    while count < days:
        output -= dt.timedelta(days=1)
        if output.weekday() < 5:
            count += 1
    return output


def count_business_days(start: dt.datetime, end: dt.datetime) -> int:
    """Count business days from the start of `start` to the start of `end`.

    Monday 09:00 to Wednesday 17:00 counts as two business days. The local date
    of `end` itself is not counted.

    Args:
        start: Timezone-aware start datetime.
        end: Timezone-aware end datetime.

    Returns:
        Number of weekday business days between the two local dates.

    Raises:
        ValueError: If either datetime is timezone-naive or `end` is before
            `start`.
    """
    start = require_aware(start)
    end = require_aware(end)
    if end < start:
        raise ValueError("end must be on or after start")

    current = start.date()
    end_date = end.astimezone(start.tzinfo).date()
    count = 0
    while current < end_date:
        if current.weekday() < 5:
            count += 1
        current += dt.timedelta(days=1)
    return count


def expand_resource_calendar(
    *,
    calendar: Mapping[str, object],
    resource: Mapping[str, object],
    horizon_starts_at: dt.datetime,
    horizon_ends_at: dt.datetime,
    planning_granularity: str = "hour",
) -> tuple[CapacityBucket, ...]:
    """Expand a resource calendar into deterministic UTC capacity buckets.

    Args:
        calendar: Calendar read model containing timezone, weekly windows, and
            exceptions.
        resource: Resource read model containing availability bounds and roles.
        horizon_starts_at: Inclusive timezone-aware query horizon start.
        horizon_ends_at: Exclusive timezone-aware query horizon end.
        planning_granularity: Bucket size. Only `"hour"` is supported in v1.

    Returns:
        Sorted UTC capacity buckets clipped to the resource and query horizon.

    Raises:
        ValueError: If timezone, datetime, overlap, horizon, or calendar window
            values are invalid.
    """
    if planning_granularity != "hour":
        raise ValueError("planning_granularity must be 'hour'")

    horizon_starts_at = require_aware(horizon_starts_at).astimezone(UTC)
    horizon_ends_at = require_aware(horizon_ends_at).astimezone(UTC)
    if horizon_ends_at <= horizon_starts_at:
        raise ValueError("horizon_ends_at must be after horizon_starts_at")

    if not bool(calendar.get("active", True)) or not bool(resource.get("active", True)):
        return ()

    timezone_name = str(calendar.get("timezone", ""))
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid IANA timezone: {timezone_name}") from exc

    available_from_at = _coerce_aware_datetime(
        resource["available_from_at"],
        "available_from_at",
    ).astimezone(UTC)
    available_until_value = resource.get("available_until_at")
    available_until_at = (
        None
        if available_until_value is None
        else _coerce_aware_datetime(
            available_until_value,
            "available_until_at",
        ).astimezone(UTC)
    )
    if available_until_at is not None and available_until_at <= available_from_at:
        raise ValueError("available_until_at must be after available_from_at")

    clipped_starts_at = max(horizon_starts_at, available_from_at)
    clipped_ends_at = horizon_ends_at
    if available_until_at is not None:
        clipped_ends_at = min(clipped_ends_at, available_until_at)
    if clipped_ends_at <= clipped_starts_at:
        return ()

    weekly_windows = _parse_weekly_windows(calendar.get("weekly_windows", ()))
    exceptions = _parse_exceptions(calendar.get("exceptions", ()))
    holidays = _parse_holidays(resource.get("holidays", ()))
    _validate_weekly_windows_do_not_overlap(weekly_windows)
    _validate_exceptions_do_not_conflict(exceptions)

    intervals: list[_CapacityInterval] = []
    for local_date in _iter_local_dates(clipped_starts_at, clipped_ends_at, timezone):
        for window in weekly_windows:
            if local_date.weekday() != window.weekday:
                continue
            interval = _expand_weekly_window(
                window=window,
                local_date=local_date,
                timezone=timezone,
                clipped_starts_at=clipped_starts_at,
                clipped_ends_at=clipped_ends_at,
                exceptions=exceptions,
            )
            intervals.extend(interval)
    intervals = list(_apply_resource_holidays(tuple(intervals), holidays))

    buckets: list[CapacityBucket] = []
    resource_id = str(resource["resource_id"])
    calendar_id = str(calendar["calendar_id"])
    role_ids = tuple(str(role_id) for role_id in resource.get("role_ids", ()))
    for interval in intervals:
        buckets.extend(
            _split_interval_into_buckets(
                interval=interval,
                resource_id=resource_id,
                calendar_id=calendar_id,
                role_ids=role_ids,
            )
        )

    return tuple(
        sorted(
            buckets,
            key=lambda bucket: (
                bucket.starts_at,
                bucket.resource_id,
                bucket.calendar_id,
                bucket.ends_at,
            ),
        )
    )


def _coerce_aware_datetime(value: object, field_name: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return require_aware(value)
    if isinstance(value, str):
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return require_aware(parsed)
    raise ValueError(f"{field_name} must be a timezone-aware datetime")


def _parse_weekly_windows(value: object) -> tuple[_WeeklyWindow, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("weekly_windows must be a sequence")

    windows: list[_WeeklyWindow] = []
    seen_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("weekly window must be an object")

        window_id = str(item["window_id"])
        if window_id in seen_ids:
            raise ValueError(f"duplicate weekly window id {window_id!r}")
        seen_ids.add(window_id)

        weekday = int(item["weekday"])
        if weekday < 0 or weekday > 6:
            raise ValueError("weekly window weekday must be between 0 and 6")

        starts_at = _parse_local_time(item["start_local_time"], "start_local_time")
        ends_at = _parse_local_time(item["end_local_time"], "end_local_time")
        if ends_at <= starts_at:
            raise ValueError("weekly window end_local_time must be after start_local_time")

        windows.append(
            _WeeklyWindow(
                window_id=window_id,
                weekday=weekday,
                starts_at=starts_at,
                ends_at=ends_at,
                capacity_hours=_coerce_capacity_hours(
                    item["capacity_hours"],
                    "weekly window capacity_hours",
                ),
            )
        )
    return tuple(windows)


def _parse_exceptions(value: object) -> tuple[_CalendarException, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("exceptions must be a sequence")

    exceptions: list[_CalendarException] = []
    seen_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("calendar exception must be an object")

        exception_id = str(item["exception_id"])
        if exception_id in seen_ids:
            raise ValueError(f"duplicate exception id {exception_id!r}")
        seen_ids.add(exception_id)

        starts_at = _coerce_aware_datetime(item["starts_at"], "exception starts_at")
        ends_at = _coerce_aware_datetime(item["ends_at"], "exception ends_at")
        if ends_at <= starts_at:
            raise ValueError("exception ends_at must be after starts_at")

        exceptions.append(
            _CalendarException(
                exception_id=exception_id,
                starts_at=starts_at.astimezone(UTC),
                ends_at=ends_at.astimezone(UTC),
                capacity_hours=_coerce_capacity_hours(
                    item["capacity_hours"],
                    "exception capacity_hours",
                ),
            )
        )
    return tuple(exceptions)


def _parse_holidays(value: object) -> tuple[_ResourceHoliday, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ValueError("holidays must be a sequence")

    holidays: list[_ResourceHoliday] = []
    seen_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("resource holiday must be an object")

        holiday_id_value = item.get("holiday_id")
        if not holiday_id_value:
            raise ValueError("holiday_id is required")
        holiday_id = str(holiday_id_value)
        if holiday_id in seen_ids:
            raise ValueError(f"duplicate holiday id {holiday_id!r}")
        seen_ids.add(holiday_id)

        starts_at = _coerce_aware_datetime(item["starts_at"], "holiday starts_at")
        ends_at = _coerce_aware_datetime(item["ends_at"], "holiday ends_at")
        if ends_at <= starts_at:
            raise ValueError("holiday ends_at must be after starts_at")

        holidays.append(
            _ResourceHoliday(
                holiday_id=holiday_id,
                starts_at=starts_at.astimezone(UTC),
                ends_at=ends_at.astimezone(UTC),
            )
        )
    return tuple(holidays)


def _parse_local_time(value: object, field_name: str) -> dt.time:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO local time string")
    parsed = dt.time.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise ValueError(f"{field_name} must not include a timezone")
    return parsed


def _coerce_capacity_hours(value: object, field_name: str) -> float:
    capacity = float(value)
    if not math.isfinite(capacity) or capacity < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return capacity


def _validate_weekly_windows_do_not_overlap(windows: tuple[_WeeklyWindow, ...]) -> None:
    by_weekday: dict[int, list[_WeeklyWindow]] = {}
    for window in windows:
        by_weekday.setdefault(window.weekday, []).append(window)

    for weekday_windows in by_weekday.values():
        ordered = sorted(weekday_windows, key=lambda window: window.starts_at)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.starts_at < previous.ends_at:
                raise ValueError(
                    "weekly windows overlap: "
                    f"{previous.window_id!r} and {current.window_id!r}"
                )


def _validate_exceptions_do_not_conflict(
    exceptions: tuple[_CalendarException, ...],
) -> None:
    ordered = sorted(exceptions, key=lambda exception: exception.starts_at)
    for index, current in enumerate(ordered):
        for other in ordered[index + 1 :]:
            if other.starts_at >= current.ends_at:
                break
            if current.capacity_hours != other.capacity_hours:
                raise ValueError(
                    "calendar exceptions overlap with conflicting capacity: "
                    f"{current.exception_id!r} and {other.exception_id!r}"
                )


def _iter_local_dates(
    starts_at: dt.datetime,
    ends_at: dt.datetime,
    timezone: ZoneInfo,
) -> tuple[dt.date, ...]:
    first_date = starts_at.astimezone(timezone).date() - dt.timedelta(days=1)
    last_date = ends_at.astimezone(timezone).date() + dt.timedelta(days=1)
    days = (last_date - first_date).days
    return tuple(first_date + dt.timedelta(days=offset) for offset in range(days + 1))


def _expand_weekly_window(
    *,
    window: _WeeklyWindow,
    local_date: dt.date,
    timezone: ZoneInfo,
    clipped_starts_at: dt.datetime,
    clipped_ends_at: dt.datetime,
    exceptions: tuple[_CalendarException, ...],
) -> tuple[_CapacityInterval, ...]:
    local_starts_at = dt.datetime.combine(local_date, window.starts_at)
    local_ends_at = dt.datetime.combine(local_date, window.ends_at)
    starts_at = _local_time_to_utc(local_starts_at, timezone, prefer_late=False)
    ends_at = _local_time_to_utc(local_ends_at, timezone, prefer_late=True)
    if ends_at <= starts_at:
        return ()

    interval_starts_at = max(starts_at, clipped_starts_at)
    interval_ends_at = min(ends_at, clipped_ends_at)
    if interval_ends_at <= interval_starts_at:
        return ()

    elapsed_hours = _hours_between(starts_at, ends_at)
    if elapsed_hours <= 0:
        return ()

    local_hours = _hours_between_naive(local_starts_at, local_ends_at)
    capacity_hours = _effective_window_capacity(
        configured_capacity=window.capacity_hours,
        elapsed_hours=elapsed_hours,
        local_hours=local_hours,
    )
    if capacity_hours <= 0:
        return ()

    iso_year, iso_week, _ = local_date.isocalendar()
    local_week = f"{iso_year}-W{iso_week:02d}"
    return _apply_exceptions(
        starts_at=starts_at,
        ends_at=ends_at,
        capacity_hours=capacity_hours,
        clipped_starts_at=interval_starts_at,
        clipped_ends_at=interval_ends_at,
        exceptions=exceptions,
        local_date=local_date.isoformat(),
        local_week=local_week,
    )


def _effective_window_capacity(
    *,
    configured_capacity: float,
    elapsed_hours: float,
    local_hours: float,
) -> float:
    if local_hours <= 0:
        return 0
    if elapsed_hours < local_hours:
        if configured_capacity <= local_hours:
            return configured_capacity * elapsed_hours / local_hours
        return configured_capacity * elapsed_hours / local_hours
    return configured_capacity


def _apply_exceptions(
    *,
    starts_at: dt.datetime,
    ends_at: dt.datetime,
    capacity_hours: float,
    clipped_starts_at: dt.datetime,
    clipped_ends_at: dt.datetime,
    exceptions: tuple[_CalendarException, ...],
    local_date: str,
    local_week: str,
) -> tuple[_CapacityInterval, ...]:
    boundaries = {clipped_starts_at, clipped_ends_at}
    overlapping_exceptions = []
    for exception in exceptions:
        overlap_starts_at = max(clipped_starts_at, exception.starts_at)
        overlap_ends_at = min(clipped_ends_at, exception.ends_at)
        if overlap_ends_at <= overlap_starts_at:
            continue
        boundaries.add(overlap_starts_at)
        boundaries.add(overlap_ends_at)
        overlapping_exceptions.append(exception)

    ordered_boundaries = sorted(boundaries)
    intervals: list[_CapacityInterval] = []
    base_elapsed_hours = _hours_between(starts_at, ends_at)
    for segment_starts_at, segment_ends_at in zip(
        ordered_boundaries,
        ordered_boundaries[1:],
        strict=False,
    ):
        if segment_ends_at <= segment_starts_at:
            continue

        active_exceptions = [
            exception
            for exception in overlapping_exceptions
            if exception.starts_at < segment_ends_at
            and segment_starts_at < exception.ends_at
        ]
        if active_exceptions:
            exception = active_exceptions[0]
            exception_elapsed_hours = _hours_between(exception.starts_at, exception.ends_at)
            segment_capacity = (
                0
                if exception_elapsed_hours <= 0
                else exception.capacity_hours
                * _hours_between(segment_starts_at, segment_ends_at)
                / exception_elapsed_hours
            )
        else:
            segment_capacity = (
                capacity_hours
                * _hours_between(segment_starts_at, segment_ends_at)
                / base_elapsed_hours
            )

        if segment_capacity <= 0:
            continue
        intervals.append(
            _CapacityInterval(
                starts_at=segment_starts_at,
                ends_at=segment_ends_at,
                capacity_hours=segment_capacity,
                local_date=local_date,
                local_week=local_week,
            )
        )
    return tuple(intervals)


def _apply_resource_holidays(
    intervals: tuple[_CapacityInterval, ...],
    holidays: tuple[_ResourceHoliday, ...],
) -> tuple[_CapacityInterval, ...]:
    if not holidays:
        return intervals

    output: list[_CapacityInterval] = []
    for interval in intervals:
        boundaries = {interval.starts_at, interval.ends_at}
        overlapping_holidays = []
        for holiday in holidays:
            overlap_starts_at = max(interval.starts_at, holiday.starts_at)
            overlap_ends_at = min(interval.ends_at, holiday.ends_at)
            if overlap_ends_at <= overlap_starts_at:
                continue
            boundaries.add(overlap_starts_at)
            boundaries.add(overlap_ends_at)
            overlapping_holidays.append(holiday)

        ordered_boundaries = sorted(boundaries)
        base_elapsed_hours = _hours_between(interval.starts_at, interval.ends_at)
        for segment_starts_at, segment_ends_at in zip(
            ordered_boundaries,
            ordered_boundaries[1:],
            strict=False,
        ):
            if segment_ends_at <= segment_starts_at:
                continue
            if any(
                holiday.starts_at < segment_ends_at
                and segment_starts_at < holiday.ends_at
                for holiday in overlapping_holidays
            ):
                continue
            segment_capacity = (
                interval.capacity_hours
                * _hours_between(segment_starts_at, segment_ends_at)
                / base_elapsed_hours
            )
            if segment_capacity <= 0:
                continue
            output.append(
                _CapacityInterval(
                    starts_at=segment_starts_at,
                    ends_at=segment_ends_at,
                    capacity_hours=segment_capacity,
                    local_date=interval.local_date,
                    local_week=interval.local_week,
                )
            )
    return tuple(output)


def _split_interval_into_buckets(
    *,
    interval: _CapacityInterval,
    resource_id: str,
    calendar_id: str,
    role_ids: tuple[str, ...],
) -> tuple[CapacityBucket, ...]:
    interval_elapsed_hours = _hours_between(interval.starts_at, interval.ends_at)
    if interval_elapsed_hours <= 0:
        return ()

    buckets: list[CapacityBucket] = []
    starts_at = interval.starts_at
    while starts_at < interval.ends_at:
        ends_at = min(_next_hour_boundary(starts_at), interval.ends_at)
        available_hours = _hours_between(starts_at, ends_at)
        capacity_hours = (
            interval.capacity_hours * available_hours / interval_elapsed_hours
        )
        buckets.append(
            CapacityBucket(
                resource_id=resource_id,
                calendar_id=calendar_id,
                starts_at=starts_at,
                ends_at=ends_at,
                capacity_hours=capacity_hours,
                available_hours=available_hours,
                allocated_hours=0,
                remaining_hours=capacity_hours,
                role_ids=role_ids,
                local_date=interval.local_date,
                local_week=interval.local_week,
            )
        )
        starts_at = ends_at
    return tuple(buckets)


def _next_hour_boundary(value: dt.datetime) -> dt.datetime:
    if value.minute == 0 and value.second == 0 and value.microsecond == 0:
        return value + dt.timedelta(hours=1)
    return (value.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1))


def _local_time_to_utc(
    value: dt.datetime,
    timezone: ZoneInfo,
    *,
    prefer_late: bool,
) -> dt.datetime:
    candidates = _valid_utc_candidates(value, timezone)
    if candidates:
        return (max(candidates) if prefer_late else min(candidates)).astimezone(UTC)
    return _first_valid_utc_after(value, timezone)


def _valid_utc_candidates(
    value: dt.datetime,
    timezone: ZoneInfo,
) -> tuple[dt.datetime, ...]:
    candidates: set[dt.datetime] = set()
    for fold in (0, 1):
        local = value.replace(tzinfo=timezone, fold=fold)
        utc_value = local.astimezone(UTC)
        round_tripped = utc_value.astimezone(timezone)
        if round_tripped.replace(tzinfo=None) == value:
            candidates.add(utc_value)
    return tuple(sorted(candidates))


def _first_valid_utc_after(value: dt.datetime, timezone: ZoneInfo) -> dt.datetime:
    current = value
    for _ in range(24 * 60 * 60 + 1):
        candidates = _valid_utc_candidates(current, timezone)
        if candidates:
            return min(candidates).astimezone(UTC)
        current += dt.timedelta(seconds=1)
    raise ValueError(f"could not resolve nonexistent local time {value!s}")


def _hours_between(starts_at: dt.datetime, ends_at: dt.datetime) -> float:
    return (ends_at - starts_at).total_seconds() / 3600


def _hours_between_naive(starts_at: dt.datetime, ends_at: dt.datetime) -> float:
    return (ends_at - starts_at).total_seconds() / 3600
