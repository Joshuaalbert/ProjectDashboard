import datetime as dt
from importlib import import_module
from typing import NamedTuple

import pytest

from projdash.engine.calendar import (
    add_business_days,
    count_business_days,
    next_business_day,
    subtract_business_days,
)

UTC = dt.UTC


class ExpectedBucket(NamedTuple):
    starts_at: str
    ends_at: str
    capacity_hours: float


def _at(day: int, hour: int = 9) -> dt.datetime:
    return dt.datetime(2024, 1, day, hour, tzinfo=UTC)


def test_business_day_add_and_subtract_round_trip():
    for day in range(1, 8):
        value = _at(day)
        if value.weekday() >= 5:
            continue

        for offset in range(0, 8):
            assert subtract_business_days(add_business_days(value, offset), offset) == value


def test_weekend_advances_to_monday_preserving_time_and_timezone():
    assert next_business_day(_at(6, 14)) == _at(8, 14)
    assert next_business_day(_at(7, 16)) == _at(8, 16)


def test_count_business_days_excludes_endpoints_after_start():
    assert count_business_days(_at(1), _at(3, 17)) == 2
    assert count_business_days(_at(5), _at(8)) == 1


def test_calendar_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="timezone-aware"):
        next_business_day(dt.datetime(2024, 1, 1, 9))


def _expand_resource_calendar(
    *,
    calendar: dict[str, object],
    resource: dict[str, object],
    horizon_starts_at: dt.datetime,
    horizon_ends_at: dt.datetime,
) -> list[object]:
    calendar_module = import_module("projdash.engine.calendar")
    expand = getattr(
        calendar_module,
        "expand_resource_calendar",
        None,
    )
    if expand is None:
        pytest.fail("expected projdash.engine.calendar.expand_resource_calendar")

    return list(
        expand(
            calendar=calendar,
            resource=resource,
            horizon_starts_at=horizon_starts_at,
            horizon_ends_at=horizon_ends_at,
            planning_granularity="hour",
        )
    )


def _base_calendar() -> dict[str, object]:
    return {
        "calendar_id": "cal_validation",
        "project_id": "project",
        "name": "Validation calendar",
        "timezone": "UTC",
        "weekly_windows": [
            {
                "window_id": "monday",
                "weekday": 0,
                "start_local_time": "09:00:00",
                "end_local_time": "17:00:00",
                "capacity_hours": 8,
            }
        ],
        "exceptions": [],
        "active": True,
    }


def _base_resource() -> dict[str, object]:
    return {
        "resource_id": "res_validation",
        "role_ids": ["role_dev"],
        "available_from_at": dt.datetime(2026, 5, 11, 0, tzinfo=UTC),
        "available_until_at": None,
        "active": True,
    }


def _value(item: object, key: str) -> object:
    if isinstance(item, dict):
        return item[key]
    return getattr(item, key)


def _iso(value: object) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return str(value)


def _bucket_spans(buckets: list[object]) -> list[tuple[str, str]]:
    return [
        (_iso(_value(bucket, "starts_at")), _iso(_value(bucket, "ends_at")))
        for bucket in buckets
    ]


def _bucket_expectations(buckets: list[object]) -> list[ExpectedBucket]:
    return [
        ExpectedBucket(
            starts_at=_iso(_value(bucket, "starts_at")),
            ends_at=_iso(_value(bucket, "ends_at")),
            capacity_hours=float(_value(bucket, "capacity_hours")),
        )
        for bucket in buckets
    ]


def _assert_utc_datetimes(buckets: list[object]) -> None:
    for bucket in buckets:
        starts_at = _value(bucket, "starts_at")
        ends_at = _value(bucket, "ends_at")
        assert isinstance(starts_at, dt.datetime)
        assert isinstance(ends_at, dt.datetime)
        assert starts_at.tzinfo is not None
        assert ends_at.tzinfo is not None
        assert starts_at.utcoffset() == dt.timedelta(0)
        assert ends_at.utcoffset() == dt.timedelta(0)


def test_resource_calendar_rejects_overlapping_weekly_windows_for_same_day():
    calendar = {
        **_base_calendar(),
        "weekly_windows": [
            {
                "window_id": "monday_morning",
                "weekday": 0,
                "start_local_time": "09:00:00",
                "end_local_time": "12:00:00",
                "capacity_hours": 3,
            },
            {
                "window_id": "monday_overlap",
                "weekday": 0,
                "start_local_time": "11:00:00",
                "end_local_time": "17:00:00",
                "capacity_hours": 6,
            },
        ],
    }

    with pytest.raises(ValueError, match="overlap"):
        _expand_resource_calendar(
            calendar=calendar,
            resource=_base_resource(),
            horizon_starts_at=dt.datetime(2026, 5, 11, 0, tzinfo=UTC),
            horizon_ends_at=dt.datetime(2026, 5, 12, 0, tzinfo=UTC),
        )


def test_resource_calendar_rejects_conflicting_overlapping_exceptions():
    calendar = {
        **_base_calendar(),
        "exceptions": [
            {
                "exception_id": "pto_morning",
                "starts_at": dt.datetime(2026, 5, 11, 13, tzinfo=UTC),
                "ends_at": dt.datetime(2026, 5, 11, 16, tzinfo=UTC),
                "capacity_hours": 0,
                "reason": "PTO",
            },
            {
                "exception_id": "partial_capacity",
                "starts_at": dt.datetime(2026, 5, 11, 15, tzinfo=UTC),
                "ends_at": dt.datetime(2026, 5, 11, 18, tzinfo=UTC),
                "capacity_hours": 2,
                "reason": "partial coverage",
            },
        ],
    }

    with pytest.raises(ValueError, match="conflict|overlap"):
        _expand_resource_calendar(
            calendar=calendar,
            resource=_base_resource(),
            horizon_starts_at=dt.datetime(2026, 5, 11, 0, tzinfo=UTC),
            horizon_ends_at=dt.datetime(2026, 5, 12, 0, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("horizon_starts_at", "horizon_ends_at"),
    [
        (
            dt.datetime(2026, 5, 11, 9, tzinfo=UTC),
            dt.datetime(2026, 5, 11, 9, tzinfo=UTC),
        ),
        (
            dt.datetime(2026, 5, 11, 10, tzinfo=UTC),
            dt.datetime(2026, 5, 11, 9, tzinfo=UTC),
        ),
    ],
)
def test_resource_calendar_rejects_invalid_horizons(
    horizon_starts_at: dt.datetime,
    horizon_ends_at: dt.datetime,
):
    with pytest.raises(ValueError, match="horizon"):
        _expand_resource_calendar(
            calendar=_base_calendar(),
            resource=_base_resource(),
            horizon_starts_at=horizon_starts_at,
            horizon_ends_at=horizon_ends_at,
        )


def test_resource_calendar_rejects_invalid_iana_timezone_name():
    calendar = {
        **_base_calendar(),
        "timezone": "Not/A_Real_Zone",
    }

    with pytest.raises(ValueError, match="timezone|IANA"):
        _expand_resource_calendar(
            calendar=calendar,
            resource=_base_resource(),
            horizon_starts_at=dt.datetime(2026, 5, 11, 0, tzinfo=UTC),
            horizon_ends_at=dt.datetime(2026, 5, 12, 0, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("horizon_starts_at", "horizon_ends_at"),
    [
        (
            dt.datetime(2026, 5, 11, 0),
            dt.datetime(2026, 5, 12, 0, tzinfo=UTC),
        ),
        (
            dt.datetime(2026, 5, 11, 0, tzinfo=UTC),
            dt.datetime(2026, 5, 12, 0),
        ),
    ],
)
def test_resource_calendar_rejects_naive_horizon_datetimes(
    horizon_starts_at: dt.datetime,
    horizon_ends_at: dt.datetime,
):
    with pytest.raises(ValueError, match="timezone-aware"):
        _expand_resource_calendar(
            calendar=_base_calendar(),
            resource=_base_resource(),
            horizon_starts_at=horizon_starts_at,
            horizon_ends_at=horizon_ends_at,
        )


@pytest.mark.parametrize(
    "exception",
    [
        {
            "exception_id": "naive_start",
            "starts_at": dt.datetime(2026, 5, 11, 13),
            "ends_at": dt.datetime(2026, 5, 11, 16, tzinfo=UTC),
            "capacity_hours": 0,
        },
        {
            "exception_id": "naive_end",
            "starts_at": dt.datetime(2026, 5, 11, 13, tzinfo=UTC),
            "ends_at": dt.datetime(2026, 5, 11, 16),
            "capacity_hours": 0,
        },
    ],
)
def test_resource_calendar_rejects_naive_exception_datetimes(
    exception: dict[str, object],
):
    calendar = {
        **_base_calendar(),
        "exceptions": [exception],
    }

    with pytest.raises(ValueError, match="timezone-aware"):
        _expand_resource_calendar(
            calendar=calendar,
            resource=_base_resource(),
            horizon_starts_at=dt.datetime(2026, 5, 11, 0, tzinfo=UTC),
            horizon_ends_at=dt.datetime(2026, 5, 12, 0, tzinfo=UTC),
        )


def test_resource_calendar_expands_local_time_to_utc_half_open_buckets():
    calendar = {
        "calendar_id": "cal_ny",
        "project_id": "project",
        "name": "New York weekdays",
        "timezone": "America/New_York",
        "weekly_windows": [
            {
                "window_id": "monday",
                "weekday": 0,
                "start_local_time": "09:00:00",
                "end_local_time": "17:00:00",
                "capacity_hours": 8,
            }
        ],
        "exceptions": [],
        "active": True,
    }
    resource = {
        "resource_id": "res_dev",
        "role_ids": ["role_dev"],
        "available_from_at": dt.datetime(2026, 5, 11, 13, 30, tzinfo=UTC),
        "available_until_at": dt.datetime(2026, 5, 11, 15, 30, tzinfo=UTC),
        "active": True,
    }

    buckets = _expand_resource_calendar(
        calendar=calendar,
        resource=resource,
        horizon_starts_at=dt.datetime(2026, 5, 11, 13, tzinfo=UTC),
        horizon_ends_at=dt.datetime(2026, 5, 11, 16, tzinfo=UTC),
    )

    assert _bucket_spans(buckets) == [
        ("2026-05-11T13:30:00+00:00", "2026-05-11T14:00:00+00:00"),
        ("2026-05-11T14:00:00+00:00", "2026-05-11T15:00:00+00:00"),
        ("2026-05-11T15:00:00+00:00", "2026-05-11T15:30:00+00:00"),
    ]
    _assert_utc_datetimes(buckets)
    assert [float(_value(bucket, "capacity_hours")) for bucket in buckets] == [
        0.5,
        1.0,
        0.5,
    ]
    assert [float(_value(bucket, "available_hours")) for bucket in buckets] == [
        0.5,
        1.0,
        0.5,
    ]
    assert {_value(bucket, "local_date") for bucket in buckets} == {"2026-05-11"}
    assert all(
        _value(bucket, "remaining_hours") == _value(bucket, "capacity_hours")
        for bucket in buckets
    )
    assert all(float(_value(bucket, "allocated_hours")) == 0 for bucket in buckets)


def test_resource_calendar_handles_dst_spring_forward_and_fall_back():
    base_calendar = {
        "calendar_id": "cal_dst",
        "project_id": "project",
        "name": "DST weekends",
        "timezone": "America/New_York",
        "weekly_windows": [],
        "exceptions": [],
        "active": True,
    }
    resource = {
        "resource_id": "res_ops",
        "role_ids": ["role_ops"],
        "available_from_at": dt.datetime(2026, 1, 1, tzinfo=UTC),
        "available_until_at": None,
        "active": True,
    }

    spring_buckets = _expand_resource_calendar(
        calendar={
            **base_calendar,
            "weekly_windows": [
                {
                    "window_id": "spring",
                    "weekday": 6,
                    "start_local_time": "01:00:00",
                    "end_local_time": "03:00:00",
                    "capacity_hours": 2,
                }
            ],
        },
        resource=resource,
        horizon_starts_at=dt.datetime(2026, 3, 8, 5, tzinfo=UTC),
        horizon_ends_at=dt.datetime(2026, 3, 8, 8, tzinfo=UTC),
    )

    assert _bucket_spans(spring_buckets) == [
        ("2026-03-08T06:00:00+00:00", "2026-03-08T07:00:00+00:00")
    ]
    _assert_utc_datetimes(spring_buckets)
    assert sum(float(_value(bucket, "available_hours")) for bucket in spring_buckets) == 1.0
    assert sum(float(_value(bucket, "capacity_hours")) for bucket in spring_buckets) == 1.0

    fall_buckets = _expand_resource_calendar(
        calendar={
            **base_calendar,
            "weekly_windows": [
                {
                    "window_id": "fall",
                    "weekday": 6,
                    "start_local_time": "01:00:00",
                    "end_local_time": "02:00:00",
                    "capacity_hours": 2,
                }
            ],
        },
        resource=resource,
        horizon_starts_at=dt.datetime(2026, 11, 1, 4, tzinfo=UTC),
        horizon_ends_at=dt.datetime(2026, 11, 1, 8, tzinfo=UTC),
    )

    assert _bucket_spans(fall_buckets) == [
        ("2026-11-01T05:00:00+00:00", "2026-11-01T06:00:00+00:00"),
        ("2026-11-01T06:00:00+00:00", "2026-11-01T07:00:00+00:00"),
    ]
    _assert_utc_datetimes(fall_buckets)
    assert sum(float(_value(bucket, "available_hours")) for bucket in fall_buckets) == 2.0
    assert sum(float(_value(bucket, "capacity_hours")) for bucket in fall_buckets) == 2.0


def test_resource_calendar_omits_spring_forward_window_inside_nonexistent_gap():
    calendar = {
        "calendar_id": "cal_dst_gap",
        "project_id": "project",
        "name": "DST gap",
        "timezone": "America/New_York",
        "weekly_windows": [
            {
                "window_id": "spring-gap",
                "weekday": 6,
                "start_local_time": "02:10:00",
                "end_local_time": "02:50:00",
                "capacity_hours": 1,
            }
        ],
        "exceptions": [],
        "active": True,
    }
    resource = {
        "resource_id": "res_ops",
        "role_ids": ["role_ops"],
        "available_from_at": dt.datetime(2026, 1, 1, tzinfo=UTC),
        "available_until_at": None,
        "active": True,
    }

    buckets = _expand_resource_calendar(
        calendar=calendar,
        resource=resource,
        horizon_starts_at=dt.datetime(2026, 3, 8, 6, tzinfo=UTC),
        horizon_ends_at=dt.datetime(2026, 3, 8, 8, tzinfo=UTC),
    )

    assert buckets == []


def test_resource_calendar_exceptions_replace_only_recurring_capacity():
    calendar = {
        "calendar_id": "cal_exceptions",
        "project_id": "project",
        "name": "Exception calendar",
        "timezone": "America/New_York",
        "weekly_windows": [
            {
                "window_id": "monday",
                "weekday": 0,
                "start_local_time": "09:00:00",
                "end_local_time": "17:00:00",
                "capacity_hours": 8,
            }
        ],
        "exceptions": [
            {
                "exception_id": "lunch_close",
                "starts_at": dt.datetime(2026, 5, 11, 16, tzinfo=UTC),
                "ends_at": dt.datetime(2026, 5, 11, 18, tzinfo=UTC),
                "capacity_hours": 0,
                "reason": "closed",
            },
            {
                "exception_id": "outside_window",
                "starts_at": dt.datetime(2026, 5, 11, 11, tzinfo=UTC),
                "ends_at": dt.datetime(2026, 5, 11, 12, tzinfo=UTC),
                "capacity_hours": 4,
                "reason": "must not create capacity",
            },
        ],
        "active": True,
    }
    resource = {
        "resource_id": "res_dev",
        "role_ids": ["role_dev"],
        "available_from_at": dt.datetime(2026, 5, 11, 13, tzinfo=UTC),
        "available_until_at": dt.datetime(2026, 5, 11, 21, tzinfo=UTC),
        "active": True,
    }

    buckets = _expand_resource_calendar(
        calendar=calendar,
        resource=resource,
        horizon_starts_at=dt.datetime(2026, 5, 11, 10, tzinfo=UTC),
        horizon_ends_at=dt.datetime(2026, 5, 11, 22, tzinfo=UTC),
    )

    assert sum(float(_value(bucket, "capacity_hours")) for bucket in buckets) == 6.0
    assert all(
        _value(bucket, "ends_at") <= dt.datetime(2026, 5, 11, 16, tzinfo=UTC)
        or _value(bucket, "starts_at") >= dt.datetime(2026, 5, 11, 18, tzinfo=UTC)
        for bucket in buckets
    )
    assert not any(
        dt.datetime(2026, 5, 11, 11, tzinfo=UTC) <= _value(bucket, "starts_at")
        < dt.datetime(2026, 5, 11, 12, tzinfo=UTC)
        for bucket in buckets
    )


def test_resource_calendar_clips_horizon_and_resource_boundaries_as_half_open():
    calendar = {
        "calendar_id": "cal_utc",
        "project_id": "project",
        "name": "UTC weekdays",
        "timezone": "UTC",
        "weekly_windows": [
            {
                "window_id": "monday",
                "weekday": 0,
                "start_local_time": "09:00:00",
                "end_local_time": "12:00:00",
                "capacity_hours": 3,
            }
        ],
        "exceptions": [],
        "active": True,
    }
    resource = {
        "resource_id": "res_dev",
        "role_ids": ["role_dev"],
        "available_from_at": dt.datetime(2026, 5, 11, 10, tzinfo=UTC),
        "available_until_at": dt.datetime(2026, 5, 11, 11, tzinfo=UTC),
        "active": True,
    }

    buckets = _expand_resource_calendar(
        calendar=calendar,
        resource=resource,
        horizon_starts_at=dt.datetime(2026, 5, 11, 11, tzinfo=UTC),
        horizon_ends_at=dt.datetime(2026, 5, 11, 12, tzinfo=UTC),
    )

    assert buckets == []

    clipped_buckets = _expand_resource_calendar(
        calendar=calendar,
        resource=resource,
        horizon_starts_at=dt.datetime(2026, 5, 11, 9, 30, tzinfo=UTC),
        horizon_ends_at=dt.datetime(2026, 5, 11, 11, tzinfo=UTC),
    )

    assert _bucket_expectations(clipped_buckets) == [
        ExpectedBucket(
            starts_at="2026-05-11T10:00:00+00:00",
            ends_at="2026-05-11T11:00:00+00:00",
            capacity_hours=1.0,
        )
    ]
    _assert_utc_datetimes(clipped_buckets)


def test_resource_calendar_uses_proportional_capacity_for_partial_window_overlap():
    calendar = {
        "calendar_id": "cal_part_time",
        "project_id": "project",
        "name": "Part-time UTC",
        "timezone": "UTC",
        "weekly_windows": [
            {
                "window_id": "monday",
                "weekday": 0,
                "start_local_time": "09:00:00",
                "end_local_time": "17:00:00",
                "capacity_hours": 4,
            }
        ],
        "exceptions": [],
        "active": True,
    }
    resource = {
        "resource_id": "res_part_time",
        "role_ids": ["role_dev"],
        "available_from_at": dt.datetime(2026, 5, 11, 9, 30, tzinfo=UTC),
        "available_until_at": dt.datetime(2026, 5, 11, 11, 30, tzinfo=UTC),
        "active": True,
    }

    buckets = _expand_resource_calendar(
        calendar=calendar,
        resource=resource,
        horizon_starts_at=dt.datetime(2026, 5, 11, 9, tzinfo=UTC),
        horizon_ends_at=dt.datetime(2026, 5, 11, 12, tzinfo=UTC),
    )

    assert _bucket_expectations(buckets) == [
        ExpectedBucket(
            starts_at="2026-05-11T09:30:00+00:00",
            ends_at="2026-05-11T10:00:00+00:00",
            capacity_hours=0.25,
        ),
        ExpectedBucket(
            starts_at="2026-05-11T10:00:00+00:00",
            ends_at="2026-05-11T11:00:00+00:00",
            capacity_hours=0.5,
        ),
        ExpectedBucket(
            starts_at="2026-05-11T11:00:00+00:00",
            ends_at="2026-05-11T11:30:00+00:00",
            capacity_hours=0.25,
        ),
    ]
    assert [float(_value(bucket, "available_hours")) for bucket in buckets] == [
        0.5,
        1.0,
        0.5,
    ]


def test_resource_calendar_pto_exception_replaces_capacity_only_inside_work_window():
    calendar = {
        "calendar_id": "cal_pto",
        "project_id": "project",
        "name": "PTO calendar",
        "timezone": "America/Los_Angeles",
        "weekly_windows": [
            {
                "window_id": "monday",
                "weekday": 0,
                "start_local_time": "09:00:00",
                "end_local_time": "17:00:00",
                "capacity_hours": 8,
            }
        ],
        "exceptions": [
            {
                "exception_id": "pto_afternoon",
                "starts_at": dt.datetime(2026, 5, 11, 19, tzinfo=UTC),
                "ends_at": dt.datetime(2026, 5, 12, 0, tzinfo=UTC),
                "capacity_hours": 0,
                "reason": "PTO",
            },
            {
                "exception_id": "after_hours_override",
                "starts_at": dt.datetime(2026, 5, 12, 1, tzinfo=UTC),
                "ends_at": dt.datetime(2026, 5, 12, 2, tzinfo=UTC),
                "capacity_hours": 1,
                "reason": "after-hours exception must not open work",
            },
        ],
        "active": True,
    }
    resource = {
        "resource_id": "res_designer",
        "role_ids": ["role_design"],
        "available_from_at": dt.datetime(2026, 5, 11, 15, tzinfo=UTC),
        "available_until_at": dt.datetime(2026, 5, 12, 3, tzinfo=UTC),
        "active": True,
    }

    buckets = _expand_resource_calendar(
        calendar=calendar,
        resource=resource,
        horizon_starts_at=dt.datetime(2026, 5, 11, 15, tzinfo=UTC),
        horizon_ends_at=dt.datetime(2026, 5, 12, 3, tzinfo=UTC),
    )

    assert _bucket_expectations(buckets) == [
        ExpectedBucket(
            starts_at="2026-05-11T16:00:00+00:00",
            ends_at="2026-05-11T17:00:00+00:00",
            capacity_hours=1.0,
        ),
        ExpectedBucket(
            starts_at="2026-05-11T17:00:00+00:00",
            ends_at="2026-05-11T18:00:00+00:00",
            capacity_hours=1.0,
        ),
        ExpectedBucket(
            starts_at="2026-05-11T18:00:00+00:00",
            ends_at="2026-05-11T19:00:00+00:00",
            capacity_hours=1.0,
        ),
    ]
    assert all(
        _value(bucket, "starts_at") < dt.datetime(2026, 5, 11, 19, tzinfo=UTC)
        for bucket in buckets
    )


def test_resource_calendar_output_is_sorted_by_time_resource_and_calendar_ids():
    calendar = {
        "calendar_id": "cal_sort",
        "project_id": "project",
        "name": "Sorting calendar",
        "timezone": "UTC",
        "weekly_windows": [
            {
                "window_id": "late",
                "weekday": 0,
                "start_local_time": "13:00:00",
                "end_local_time": "14:00:00",
                "capacity_hours": 1,
            },
            {
                "window_id": "early",
                "weekday": 0,
                "start_local_time": "09:00:00",
                "end_local_time": "10:00:00",
                "capacity_hours": 1,
            },
        ],
        "exceptions": [],
        "active": True,
    }
    resource = {
        "resource_id": "res_zed",
        "role_ids": ["role_dev"],
        "available_from_at": dt.datetime(2026, 5, 11, 0, tzinfo=UTC),
        "available_until_at": None,
        "active": True,
    }

    buckets = _expand_resource_calendar(
        calendar=calendar,
        resource=resource,
        horizon_starts_at=dt.datetime(2026, 5, 11, 0, tzinfo=UTC),
        horizon_ends_at=dt.datetime(2026, 5, 12, 0, tzinfo=UTC),
    )

    assert _bucket_spans(buckets) == [
        ("2026-05-11T09:00:00+00:00", "2026-05-11T10:00:00+00:00"),
        ("2026-05-11T13:00:00+00:00", "2026-05-11T14:00:00+00:00"),
    ]
    assert [_value(bucket, "resource_id") for bucket in buckets] == [
        "res_zed",
        "res_zed",
    ]


def test_multiple_timezone_resources_expand_non_identical_schedules_in_same_horizon():
    london_calendar = {
        "calendar_id": "cal_london",
        "project_id": "project",
        "name": "London Tuesday",
        "timezone": "Europe/London",
        "weekly_windows": [
            {
                "window_id": "tuesday",
                "weekday": 1,
                "start_local_time": "09:00:00",
                "end_local_time": "11:00:00",
                "capacity_hours": 2,
            }
        ],
        "exceptions": [],
        "active": True,
    }
    tokyo_calendar = {
        "calendar_id": "cal_tokyo",
        "project_id": "project",
        "name": "Tokyo Tuesday",
        "timezone": "Asia/Tokyo",
        "weekly_windows": [
            {
                "window_id": "tuesday",
                "weekday": 1,
                "start_local_time": "13:00:00",
                "end_local_time": "15:00:00",
                "capacity_hours": 1,
            }
        ],
        "exceptions": [],
        "active": True,
    }
    london_resource = {
        "resource_id": "res_london",
        "role_ids": ["role_design"],
        "available_from_at": dt.datetime(2026, 5, 12, 0, tzinfo=UTC),
        "available_until_at": None,
        "active": True,
    }
    tokyo_resource = {
        "resource_id": "res_tokyo",
        "role_ids": ["role_qa"],
        "available_from_at": dt.datetime(2026, 5, 12, 0, tzinfo=UTC),
        "available_until_at": None,
        "active": True,
    }

    london_buckets = _expand_resource_calendar(
        calendar=london_calendar,
        resource=london_resource,
        horizon_starts_at=dt.datetime(2026, 5, 12, 0, tzinfo=UTC),
        horizon_ends_at=dt.datetime(2026, 5, 12, 12, tzinfo=UTC),
    )
    tokyo_buckets = _expand_resource_calendar(
        calendar=tokyo_calendar,
        resource=tokyo_resource,
        horizon_starts_at=dt.datetime(2026, 5, 12, 0, tzinfo=UTC),
        horizon_ends_at=dt.datetime(2026, 5, 12, 12, tzinfo=UTC),
    )
    buckets = sorted(
        [*london_buckets, *tokyo_buckets],
        key=lambda bucket: (
            _value(bucket, "starts_at"),
            _value(bucket, "resource_id"),
            _value(bucket, "calendar_id"),
        ),
    )

    assert [
        (
            _value(bucket, "resource_id"),
            _value(bucket, "calendar_id"),
            _iso(_value(bucket, "starts_at")),
            _iso(_value(bucket, "ends_at")),
            float(_value(bucket, "capacity_hours")),
            _value(bucket, "local_date"),
        )
        for bucket in buckets
    ] == [
        (
            "res_tokyo",
            "cal_tokyo",
            "2026-05-12T04:00:00+00:00",
            "2026-05-12T05:00:00+00:00",
            0.5,
            "2026-05-12",
        ),
        (
            "res_tokyo",
            "cal_tokyo",
            "2026-05-12T05:00:00+00:00",
            "2026-05-12T06:00:00+00:00",
            0.5,
            "2026-05-12",
        ),
        (
            "res_london",
            "cal_london",
            "2026-05-12T08:00:00+00:00",
            "2026-05-12T09:00:00+00:00",
            1.0,
            "2026-05-12",
        ),
        (
            "res_london",
            "cal_london",
            "2026-05-12T09:00:00+00:00",
            "2026-05-12T10:00:00+00:00",
            1.0,
            "2026-05-12",
        ),
    ]
    _assert_utc_datetimes(buckets)
