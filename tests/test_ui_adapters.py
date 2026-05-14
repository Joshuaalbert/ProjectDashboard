import datetime as dt

import pytest

from projdash.ui.adapters import (
    build_process_graph_dot,
    catalog_from_query_data,
    cost_time_series_rows,
)
from projdash.ui.service_client import (
    batch_payload_envelope,
    combine_datetime,
    command_payload_envelope,
    parse_dependency_lines,
    parse_holiday_lines,
    parse_resource_lines,
    parse_role_lines,
    parse_subgraph_process_lines,
    query_payload_envelope,
    validate_timezone,
)


def test_combine_datetime_returns_timezone_aware_datetime():
    combined = combine_datetime(
        dt.date(2026, 5, 13),
        dt.time(9, 30),
        "America/New_York",
    )

    assert combined.isoformat() == "2026-05-13T09:30:00-04:00"


def test_guided_form_parsers_accept_compact_rows():
    roles = parse_role_lines("role_eng: Engineer\nReviewer")
    resources = parse_resource_lines("Alice | role_eng, role_review | 125")
    holidays = parse_holiday_lines("2026-05-25 | Memorial Day", "UTC")
    precise_holidays = parse_holiday_lines(
        (
            "holiday-pto | 2026-05-25T09:00:00+00:00 | "
            "2026-05-25T13:00:00+00:00 | PTO"
        ),
        "UTC",
    )
    identified_holidays = parse_holiday_lines(
        "holiday-closed | 2026-05-26..2026-05-27 | Closed",
        "UTC",
    )
    dependencies = parse_dependency_lines("A -> B\nB, C")
    children = parse_subgraph_process_lines("A | First child | 8")

    assert roles[0].role_id == "role_eng"
    assert roles[1].role_id == "role_reviewer"
    assert resources[0].role_ids == ["role_eng", "role_review"]
    assert holidays[0]["starts_at"].isoformat() == "2026-05-25T00:00:00+00:00"
    assert holidays[0]["ends_at"].isoformat() == "2026-05-26T00:00:00+00:00"
    assert precise_holidays[0]["holiday_id"] == "holiday-pto"
    assert precise_holidays[0]["ends_at"].isoformat() == "2026-05-25T13:00:00+00:00"
    assert identified_holidays[0]["holiday_id"] == "holiday-closed"
    assert identified_holidays[0]["ends_at"].isoformat() == "2026-05-28T00:00:00+00:00"
    assert dependencies == [("A", "B"), ("B", "C")]
    assert children[0]["duration_hours"] == 8.0


def test_command_payload_envelope_validates_service_command():
    envelope = command_payload_envelope(
        {
            "action": "create_project",
            "project_id": "project-ui",
            "name": "UI Project",
            "start_at": "2026-05-13T09:00:00+00:00",
        }
    )

    assert envelope.command.action == "create_project"
    assert envelope.command.project_id == "project-ui"
    assert envelope.command.start_at.tzinfo is not None


def test_batch_payload_envelope_validates_atomic_first_run_payloads():
    envelope = batch_payload_envelope(
        [
            {
                "action": "create_project",
                "project_id": "project-ui",
                "name": "UI Project",
                "start_at": "2026-05-13T09:00:00+00:00",
            },
            {
                "action": "create_role",
                "project_id": "project-ui",
                "role_id": "role-engineer",
                "name": "Engineer",
            },
        ]
    )

    assert [item.command.action for item in envelope.commands] == [
        "create_project",
        "create_role",
    ]


def test_query_payload_envelope_validates_catalog_query():
    envelope = query_payload_envelope(
        {
            "action": "query_project_catalog",
            "project_id": "project-ui",
        }
    )

    assert envelope.query.action == "query_project_catalog"


def test_validate_timezone_reports_invalid_names():
    assert validate_timezone("America/New_York") == "America/New_York"
    with pytest.raises(ValueError):
        validate_timezone("Mars/Base")


def test_catalog_extracts_ids_from_query_data():
    catalog = catalog_from_query_data(
        {
            "roles": [{"role_id": "role_catalog"}],
            "resources": [
                {
                    "resource_id": "resource_catalog",
                    "calendar_id": "calendar_catalog",
                    "role_ids": ["role_catalog"],
                }
            ],
            "calendars": [{"calendar_id": "calendar_catalog"}],
            "nodes": [
                {
                    "process_id": "p1",
                    "process_symbol": "A",
                    "role_requirements": [{"role_id": "role_eng"}],
                }
            ],
            "blockers": [{"blocker_id": "b1", "process_id": "p1"}],
            "buckets": [
                {
                    "resource_id": "r1",
                    "calendar_id": "c1",
                    "role_ids": ["role_eng"],
                }
            ],
        }
    )

    assert catalog["process_ids"] == ["p1"]
    assert catalog["process_symbols"] == ["A"]
    assert catalog["role_ids"] == ["role_catalog", "role_eng"]
    assert catalog["resource_ids"] == ["r1", "resource_catalog"]
    assert catalog["calendar_ids"] == ["c1", "calendar_catalog"]
    assert catalog["blocker_ids"] == ["b1"]


def test_graph_adapter_marks_critical_path_and_collapsed_nodes():
    dot = build_process_graph_dot(
        {
            "critical_path_process_ids": ["p1", "p2"],
            "nodes": [
                {
                    "process_id": "p1",
                    "process_symbol": "A",
                    "name": "Start",
                    "computed_status": "work_now",
                },
                {
                    "process_id": "p2",
                    "process_symbol": "B",
                    "name": "Finish",
                    "computed_status": "ready",
                },
            ],
            "edges": [
                {
                    "predecessor_process_id": "p1",
                    "successor_process_id": "p2",
                }
            ],
        },
        collapsed_process_ids={"p2"},
    )

    assert "penwidth=3" in dot
    assert "[+]B" in dot
    assert "p1 -> p2" in dot


def test_cost_time_series_rows_casts_decimal_strings():
    rows = cost_time_series_rows(
        {
            "time_series": [
                {
                    "starts_at": "2026-05-13T09:00:00+00:00",
                    "ends_at": "2026-05-13T10:00:00+00:00",
                    "allocated_hours": 1,
                    "currency": "USD",
                    "cost_amount": "12.50",
                }
            ]
        }
    )

    assert rows[0]["cost_amount"] == 12.5
