import datetime as dt

import matplotlib.dates as mdates
import pytest

from projdash.ui.adapters import (
    aggregate_process_properties,
    allowed_dependency_symbols,
    allowed_shared_dependency_symbols,
    allowed_successor_symbols,
    ancestor_scope_symbols,
    blocker_table_rows,
    build_process_graph_dot,
    catalog_from_query_data,
    cost_time_series_rows,
    existing_dependency_symbols,
    gantt_bar_color,
    gantt_completedness_legend_items,
    gantt_rows,
    process_symbol_maps,
    process_table_rows,
    resource_priority_rows,
    resource_utilization_heatmap,
    role_priority_rows,
    role_utilization_heatmap,
    schedule_time_span,
)
from projdash.ui.app import (
    _datetime_axis_locator_and_formatter,
    _role_effort_defaults,
)
from projdash.ui.service_client import (
    batch_payload_envelope,
    calendar_options,
    combine_datetime,
    command_payload_envelope,
    format_display_datetime,
    format_display_datetimes,
    infer_subgraph_roots_and_leaves,
    parse_dependency_lines,
    parse_holiday_lines,
    parse_resource_lines,
    parse_role_lines,
    parse_subgraph_process_lines,
    project_options,
    query_payload_envelope,
    scoped_id,
    to_display_timezone,
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
    children = parse_subgraph_process_lines(
        "A | First child | role_eng:6,role_review:2 | First definition\n"
        "B | Second child | role_eng:4,role_qa:2"
    )
    patterned_children = parse_subgraph_process_lines(
        "C | Patterned child | *_lead:8,role_qa:5",
        ["role_design_lead", "role_qa", "role_writer"],
    )
    regex_children = parse_subgraph_process_lines(
        "D | Regex child | role_[eq][a-z]+:3",
        ["role_eng", "role_qa", "role_writer"],
    )
    roots, leaves = infer_subgraph_roots_and_leaves(
        [
            {"process_symbol": "A"},
            {"process_symbol": "B"},
            {"process_symbol": "C"},
        ],
        [("A", "B")],
    )

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
    assert children[0]["description"] == "First definition"
    assert children[0]["role_requirements"] == [
        {"role_id": "role_eng", "effort_hours": 6.0},
        {"role_id": "role_review", "effort_hours": 2.0},
    ]
    assert children[1]["duration_hours"] == 6.0
    assert children[1]["description"] == ""
    assert children[1]["role_requirements"] == [
        {"role_id": "role_eng", "effort_hours": 4.0},
        {"role_id": "role_qa", "effort_hours": 2.0},
    ]
    assert patterned_children[0]["role_requirements"] == [
        {"role_id": "role_design_lead", "effort_hours": 8.0},
        {"role_id": "role_qa", "effort_hours": 5.0},
    ]
    assert regex_children[0]["role_requirements"] == [
        {"role_id": "role_eng", "effort_hours": 3.0},
        {"role_id": "role_qa", "effort_hours": 3.0},
    ]
    assert roots == ["A", "C"]
    assert leaves == ["B", "C"]


def test_process_table_rows_and_role_defaults_include_pm_description_and_effort():
    graph = {
        "nodes": [
            {
                "process_id": "p1",
                "process_symbol": "A",
                "name": "Design",
                "description": "Definition of design completion",
                "resource_aware": {
                    "inferred_duration_hours": 6.5,
                    "allocation_diagnostic": "Needs more calendar capacity.",
                },
                "role_requirements": [
                    {"role_id": "role_eng", "effort_hours": 2},
                    {"role_id": "role_eng", "effort_hours": 3},
                    {"role_id": "role_qa", "effort_hours": 1},
                ],
            }
        ],
    }

    rows = process_table_rows(graph)

    assert rows[0]["description"] == "Definition of design completion"
    assert rows[0]["inferred_duration_hours"] == 6.5
    assert rows[0]["allocation_diagnostic"] == "Needs more calendar capacity."
    assert "focus_evidence" not in rows[0]
    assert _role_effort_defaults(graph["nodes"][0]) == {
        "role_eng": 5.0,
        "role_qa": 1.0,
    }


def test_blocker_table_rows_join_process_priority_roles_and_resources():
    now = dt.datetime(2026, 5, 13, 12, tzinfo=dt.UTC)
    blockers = {
        "blockers": [
            {
                "blocker_id": "blocker-review",
                "process_id": "p1",
                "process_symbol": "A",
                "summary": "Reviewer unavailable",
                "details": "Waiting for security review.",
                "severity": "blocking",
                "created_at": "2026-05-13T11:00:00+00:00",
                "resolved_at": None,
                "resolution": None,
                "is_blocking_as_of": True,
                "is_resolved_as_of": False,
            },
            {
                "blocker_id": "blocker-archive",
                "process_id": "p2",
                "process_symbol": "B",
                "summary": "Old issue",
                "details": None,
                "severity": "blocking",
                "created_at": "2026-05-12T11:00:00+00:00",
                "resolved_at": "2026-05-13T09:00:00+00:00",
                "resolution": "Closed.",
                "is_blocking_as_of": False,
                "is_resolved_as_of": True,
            },
        ],
    }
    graph = {
        "nodes": [
            {
                "process_id": "p1",
                "process_symbol": "A",
                "name": "Design",
                "status": "planned",
                "computed_status": "started",
                "role_requirements": [
                    {"role_id": "role_eng", "effort_hours": 4},
                    {"role_id": "role_qa", "effort_hours": 2},
                ],
                "dependency_only": {
                    "es_at": "2026-05-13T09:00:00+00:00",
                    "ef_at": "2026-05-13T11:00:00+00:00",
                    "ls_at": "2026-05-13T13:00:00+00:00",
                    "lf_at": "2026-05-13T15:00:00+00:00",
                },
            },
            {
                "process_id": "p2",
                "process_symbol": "B",
                "name": "Done",
                "status": "done",
                "computed_status": "done",
                "role_requirements": [{"role_id": "role_ops", "effort_hours": 1}],
                "dependency_only": {
                    "es_at": "2026-05-12T09:00:00+00:00",
                    "ef_at": "2026-05-12T10:00:00+00:00",
                    "ls_at": "2026-05-12T09:00:00+00:00",
                    "lf_at": "2026-05-12T10:00:00+00:00",
                },
            },
        ],
    }
    schedule = {
        "allocation_slices": [
            {"process_id": "p1", "resource_id": "res_ada"},
            {"process_id": "p1", "resource_id": "res_grace"},
            {"process_id": "p2", "resource_id": "res_ops"},
        ]
    }

    rows = blocker_table_rows(blockers, graph, schedule, now)

    assert rows[0]["blocker_id"] == "blocker-review"
    assert rows[0]["blocker_status"] == "blocking"
    assert rows[0]["priority"] == "P0"
    assert rows[0]["process_status"] == "planned"
    assert rows[0]["computed_status"] == "started"
    assert rows[0]["role_ids"] == "role_eng, role_qa"
    assert rows[0]["resource_ids"] == "res_ada, res_grace"
    assert rows[1]["blocker_id"] == "blocker-archive"
    assert rows[1]["blocker_status"] == "resolved"
    assert rows[1]["priority"] == "-"


def test_display_datetime_helpers_use_selected_timezone_and_visible_format():
    value = "2026-01-01T18:00:00+00:00"
    rows = [
        {
            "process_symbol": "A",
            "started_at": value,
            "resource_start": value,
            "nested": {"finished_at": value},
        }
    ]

    assert format_display_datetime(value, "America/New_York") == (
        "Thu, 01 Jan 2026, 13:00"
    )
    assert to_display_timezone(value, "America/New_York").isoformat() == (
        "2026-01-01T13:00:00-05:00"
    )
    assert format_display_datetime(value, "Europe/Paris") == (
        "Thu, 01 Jan 2026, 19:00"
    )
    assert format_display_datetimes(rows, "America/New_York") == [
        {
            "process_symbol": "A",
            "started_at": "Thu, 01 Jan 2026, 13:00",
            "resource_start": "Thu, 01 Jan 2026, 13:00",
            "nested": {"finished_at": "Thu, 01 Jan 2026, 13:00"},
        }
    ]


def test_chart_datetime_formatter_uses_selected_timezone_and_visible_format():
    _locator, formatter = _datetime_axis_locator_and_formatter("America/New_York")
    timestamps = [
        dt.datetime(2026, 1, 1, 18, tzinfo=dt.UTC),
        dt.datetime(2026, 1, 1, 22, tzinfo=dt.UTC),
    ]
    values = mdates.date2num(timestamps)

    formatter.set_locs(values)
    assert formatter.format_ticks(values) == ["13:00", "17:00"]


@pytest.mark.parametrize(
    "line",
    [
        "B | Second child | ",
        "B | Second child | 8",
        "B | Second child | :4",
        "B | Second child | role_eng:",
        "B | Second child | role_eng:4,role_eng:2",
        "B | Second child | role_eng:0",
    ],
)
def test_subgraph_process_role_effort_rows_reject_malformed_tokens(line: str):
    with pytest.raises(ValueError):
        parse_subgraph_process_lines(line)


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


def test_project_management_payloads_validate_against_service_contract():
    update = command_payload_envelope(
        {
            "action": "update_project",
            "project_id": "project-ui",
            "name": "Renamed",
            "default_currency": "eur",
        }
    )
    delete = command_payload_envelope(
        {
            "action": "delete_project",
            "project_id": "project-ui",
            "confirm_project_id": "project-ui",
        }
    )
    projects = query_payload_envelope({"action": "query_projects"})

    assert update.command.default_currency == "EUR"
    assert delete.command.action == "delete_project"
    assert projects.query.action == "query_projects"


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


def test_ui_selection_helpers_use_defined_options_and_project_scoped_ids():
    projects = project_options(
        [
            {"project_id": "project-beta", "name": "Beta"},
            {"project_id": "project-alpha", "name": "Alpha"},
        ]
    )
    calendars = calendar_options(
        [
            {"calendar_id": "calendar-night", "name": "Night Shift"},
            {"calendar_id": "calendar-day", "name": "Day Shift"},
        ]
    )

    assert [project.project_id for project in projects] == [
        "project-alpha",
        "project-beta",
    ]
    assert projects[0].label == "Alpha (project-alpha)"
    assert [calendar.calendar_id for calendar in calendars] == [
        "calendar-day",
        "calendar-night",
    ]
    assert scoped_id("project-alpha", "role", "Engineer") == (
        "role_project_alpha_engineer"
    )


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


def test_process_symbol_helpers_filter_cycle_safe_dependency_choices():
    graph = {
        "nodes": [
            {"process_id": "p1", "process_symbol": "A"},
            {"process_id": "p2", "process_symbol": "B"},
            {"process_id": "p3", "process_symbol": "C"},
            {"process_id": "p4", "process_symbol": "D"},
        ],
        "edges": [
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "B",
            },
            {
                "predecessor_process_symbol": "B",
                "successor_process_symbol": "C",
            },
        ],
    }

    id_by_symbol, symbol_by_id = process_symbol_maps(graph)

    assert id_by_symbol == {"A": "p1", "B": "p2", "C": "p3", "D": "p4"}
    assert symbol_by_id["p3"] == "C"
    assert existing_dependency_symbols(graph, "B") == ["A"]
    assert allowed_dependency_symbols(graph, "B") == ["A", "D"]
    assert allowed_shared_dependency_symbols(graph, ["B", "C"]) == ["A", "D"]
    assert allowed_successor_symbols(graph, ["B"]) == ["C", "D"]
    assert ancestor_scope_symbols(graph, ["C"]) == ["A", "B", "C"]


def test_process_aggregation_and_priority_rows_use_schedule_windows():
    now = dt.datetime(2026, 5, 13, 12, tzinfo=dt.UTC)
    graph = {
        "nodes": [
            {
                "process_id": "p1",
                "process_symbol": "A",
                "name": "Design",
                "status": "planned",
                "started_at": None,
                "finished_at": None,
                "earliest_start_at": "2026-05-13T09:00:00+00:00",
                "blocker_summary": {"blocker_ids": ["b1"]},
                "role_requirements": [
                    {"role_id": "role_eng", "effort_hours": 2},
                ],
                "dependency_only": {
                    "es_at": "2026-05-13T09:00:00+00:00",
                    "ef_at": "2026-05-13T11:00:00+00:00",
                    "ls_at": "2026-05-13T10:00:00+00:00",
                    "lf_at": "2026-05-13T14:00:00+00:00",
                },
                "resource_aware": {
                    "starts_at": "2026-05-13T09:00:00+00:00",
                    "ends_at": "2026-05-13T11:00:00+00:00",
                    "es_at": "2026-05-13T09:00:00+00:00",
                    "ef_at": "2026-05-13T11:00:00+00:00",
                    "ls_at": "2026-05-13T13:00:00+00:00",
                    "lf_at": "2026-05-13T15:00:00+00:00",
                    "slack_hours": 4,
                    "criticality_label": "non_critical",
                },
            },
            {
                "process_id": "p2",
                "process_symbol": "B",
                "name": "Build",
                "status": "planned",
                "role_requirements": [
                    {"role_id": "role_eng", "effort_hours": 3},
                    {"role_id": "role_qa", "effort_hours": 5},
                ],
                "dependency_only": {
                    "es_at": "2026-05-18T12:00:00+00:00",
                    "ef_at": "2026-05-18T13:00:00+00:00",
                    "ls_at": "2026-05-18T14:00:00+00:00",
                    "lf_at": "2026-05-18T16:00:00+00:00",
                },
            },
        ],
        "edges": [
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "B",
            }
        ],
    }
    schedule = {
        "allocation_slices": [
            {
                "process_id": "A",
                "resource_id": "ignored",
                "role_id": "role_eng",
                "effort_hours": 10,
            },
            {
                "process_id": "p1",
                "resource_id": "res_alice",
                "role_id": "role_eng",
                "effort_hours": 2,
            }
        ]
    }

    aggregate = aggregate_process_properties(graph, ["A", "B"])
    role_rows = role_priority_rows(graph, now)
    resource_rows = resource_priority_rows(graph, schedule, now)

    assert aggregate["predecessors"] == []
    assert aggregate["children"] == []
    assert aggregate["role_efforts"] == {"role_eng": 5.0, "role_qa": 5.0}
    assert aggregate["blocker_ids"] == ["b1"]
    assert role_rows[0]["priority"] == "P0"
    assert role_rows[0]["process_symbol"] == "A"
    assert role_rows[0]["hours_until_planned_start"] == -3.0
    assert role_rows[-1]["priority"] == "P1"
    assert len(resource_rows) == 1
    assert resource_rows[0]["priority"] == "P0"
    assert resource_rows[0]["process_symbol"] == "A"
    assert resource_rows[0]["resource_id"] == "res_alice"
    assert resource_rows[0]["effort_hours"] == 2.0
    assert resource_rows[0]["role_ids"] == "role_eng"


def test_gantt_rows_use_terminal_ancestor_scope():
    graph = {
        "critical_path_process_ids": ["p1", "p2"],
        "nodes": [
            {
                "process_id": "p1",
                "process_symbol": "A",
                "name": "Start",
                "dependency_only": {
                    "es_at": "2026-05-13T09:00:00+00:00",
                    "ef_at": "2026-05-13T17:00:00+00:00",
                    "ls_at": "2026-05-13T09:00:00+00:00",
                    "lf_at": "2026-05-13T17:00:00+00:00",
                    "slack_hours": 0,
                    "criticality_label": "critical",
                },
            },
            {
                "process_id": "p2",
                "process_symbol": "B",
                "name": "Finish",
                "dependency_only": {
                    "es_at": "2026-05-14T09:00:00+00:00",
                    "ef_at": "2026-05-15T17:00:00+00:00",
                    "ls_at": "2026-05-14T09:00:00+00:00",
                    "lf_at": "2026-05-15T17:00:00+00:00",
                    "slack_hours": 0,
                    "criticality_label": "critical",
                },
            },
            {
                "process_id": "p3",
                "process_symbol": "C",
                "name": "Unrelated",
                "dependency_only": {
                    "es_at": "2026-06-01T09:00:00+00:00",
                    "ef_at": "2026-06-01T17:00:00+00:00",
                    "ls_at": "2026-06-01T09:00:00+00:00",
                    "lf_at": "2026-06-01T17:00:00+00:00",
                    "slack_hours": 0,
                    "criticality_label": "non_critical",
                },
            },
        ],
        "edges": [
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "B",
            }
        ],
    }

    rows = gantt_rows(graph, terminal_symbols=["B"])

    assert [row["symbol"] for row in rows] == ["A", "B"]
    assert all(row["sensitive"] for row in rows)


def test_gantt_rows_use_sensitivity_over_legacy_criticality_labels():
    graph = {
        "nodes": [
            {
                "process_id": "p1",
                "process_symbol": "A",
                "resource_aware": {
                    "starts_at": "2026-05-13T09:00:00+00:00",
                    "ends_at": "2026-05-13T10:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-13T09:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-13T10:00:00+00:00",
                    "schedule_buffer_hours": 0,
                    "max_makespan_sensitivity_hours": 0,
                    "criticality_label": "critical",
                },
            },
            {
                "process_id": "p2",
                "process_symbol": "B",
                "resource_aware": {
                    "starts_at": "2026-05-13T10:00:00+00:00",
                    "ends_at": "2026-05-13T11:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-13T10:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-13T11:00:00+00:00",
                    "schedule_buffer_hours": 0,
                    "max_makespan_sensitivity_hours": 1,
                    "criticality_label": "non_critical",
                },
            },
        ],
    }

    rows = gantt_rows(graph)

    assert [row["symbol"] for row in rows] == ["A", "B"]
    assert [row["sensitive"] for row in rows] == [False, True]


def test_gantt_bar_color_reflects_completedness_states():
    now = dt.datetime(2026, 5, 13, 12, tzinfo=dt.UTC)
    graph = {
        "nodes": [
            {
                "process_id": "p-waiting",
                "process_symbol": "WAITING",
                "name": "Waiting",
                "status": "planned",
                "computed_status": "waiting",
                "resource_aware": {
                    "starts_at": "2026-05-14T09:00:00+00:00",
                    "ends_at": "2026-05-14T17:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-14T09:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-14T17:00:00+00:00",
                },
            },
            {
                "process_id": "p-early-start",
                "process_symbol": "EARLY",
                "name": "Early start",
                "status": "planned",
                "computed_status": "early_start",
                "started_at": "2026-05-13T10:00:00+00:00",
                "resource_aware": {
                    "starts_at": "2026-05-14T09:00:00+00:00",
                    "ends_at": "2026-05-14T17:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-14T09:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-14T17:00:00+00:00",
                },
            },
            {
                "process_id": "p-ready",
                "process_symbol": "READY",
                "name": "Ready",
                "status": "planned",
                "computed_status": "ready",
                "resource_aware": {
                    "starts_at": "2026-05-13T13:00:00+00:00",
                    "ends_at": "2026-05-13T17:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-13T13:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-13T17:00:00+00:00",
                },
            },
            {
                "process_id": "p-started",
                "process_symbol": "STARTED",
                "name": "Started",
                "status": "planned",
                "computed_status": "started",
                "started_at": "2026-05-13T10:00:00+00:00",
                "resource_aware": {
                    "starts_at": "2026-05-13T09:00:00+00:00",
                    "ends_at": "2026-05-13T11:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-13T09:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-13T11:00:00+00:00",
                },
            },
            {
                "process_id": "p-due",
                "process_symbol": "DUE",
                "name": "Due",
                "status": "planned",
                "computed_status": "due",
                "started_at": "2026-05-13T10:00:00+00:00",
                "resource_aware": {
                    "starts_at": "2026-05-13T09:00:00+00:00",
                    "ends_at": "2026-05-13T17:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-13T09:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-13T17:00:00+00:00",
                },
            },
            {
                "process_id": "p-done",
                "process_symbol": "FINISHED",
                "name": "Finished",
                "status": "done",
                "computed_status": "finished",
                "finished_at": "2026-05-13T10:00:00+00:00",
                "resource_aware": {
                    "starts_at": "2026-05-13T09:00:00+00:00",
                    "ends_at": "2026-05-13T10:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-13T09:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-13T10:00:00+00:00",
                },
            },
            {
                "process_id": "p-legacy-done",
                "process_symbol": "DONE_READY",
                "name": "Legacy done without verified pins",
                "status": "done",
                "computed_status": "ready",
                "finished_at": "2026-05-13T10:00:00+00:00",
                "resource_aware": {
                    "starts_at": "2026-05-13T13:00:00+00:00",
                    "ends_at": "2026-05-13T17:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-13T13:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-13T17:00:00+00:00",
                },
            },
        ],
    }

    rows = {row["symbol"]: row for row in gantt_rows(graph)}

    assert gantt_bar_color(rows["WAITING"], now) == "#64748b"
    assert gantt_bar_color(rows["EARLY"], now) == "#7c3aed"
    assert gantt_bar_color(rows["READY"], now) == "#2563eb"
    assert gantt_bar_color(rows["STARTED"], now) == "#f59e0b"
    assert gantt_bar_color(rows["DUE"], now) == "#eab308"
    assert gantt_bar_color(rows["FINISHED"], now) == "#16a34a"
    assert gantt_bar_color(rows["DONE_READY"], now) == "#2563eb"


def test_gantt_rows_are_topological_and_include_process_role_pin_markers():
    graph = {
        "nodes": [
            {
                "process_id": "p-child",
                "process_symbol": "B",
                "name": "Child",
                "status": "planned",
                "computed_status": "started",
                "resource_aware": {
                    "starts_at": "2026-05-14T09:00:00+00:00",
                    "ends_at": "2026-05-14T17:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-14T09:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-14T17:00:00+00:00",
                },
                "role_requirements": [
                    {
                        "requirement_id": "req-dev",
                        "role_id": "role-dev",
                        "pins": [
                            {
                                "pin_id": "pin-dev",
                                "resource_id": "res-ada",
                                "pinned_at": "2026-05-13T10:00:00+00:00",
                                "forecast_finish_at": "2026-05-13T16:00:00+00:00",
                                "status": "pinned_started",
                            },
                            {
                                "pin_id": "pin-qa",
                                "resource_id": "res-grace",
                                "pinned_at": "2026-05-13T11:00:00+00:00",
                                "forecast_finish_at": "2026-05-13T15:00:00+00:00",
                                "verified_finished_at": "2026-05-13T14:00:00+00:00",
                                "status": "pinned_finished",
                            },
                        ],
                    }
                ],
            },
            {
                "process_id": "p-parent",
                "process_symbol": "A",
                "name": "Parent",
                "status": "planned",
                "computed_status": "ready",
                "resource_aware": {
                    "starts_at": "2026-05-13T09:00:00+00:00",
                    "ends_at": "2026-05-13T17:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-13T09:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-13T17:00:00+00:00",
                },
            },
        ],
        "edges": [
            {
                "predecessor_process_id": "p-parent",
                "successor_process_id": "p-child",
            }
        ],
    }

    rows = gantt_rows(graph)

    assert [row["symbol"] for row in rows] == ["A", "B"]
    markers = rows[1]["pin_markers"]
    assert [marker["kind"] for marker in markers] == [
        "pin_start",
        "pin_start",
        "pin_finish",
    ]
    assert [marker["at"].isoformat() for marker in markers] == [
        "2026-05-13T10:00:00+00:00",
        "2026-05-13T11:00:00+00:00",
        "2026-05-13T14:00:00+00:00",
    ]


def test_gantt_rows_keep_newly_ready_children_close_to_parent():
    def node(symbol: str) -> dict[str, object]:
        return {
            "process_id": f"p-{symbol.lower()}",
            "process_symbol": symbol,
            "computed_status": "ready",
            "resource_aware": {
                "starts_at": "2026-05-13T09:00:00+00:00",
                "ends_at": "2026-05-13T10:00:00+00:00",
                "schedule_window_starts_at": "2026-05-13T09:00:00+00:00",
                "schedule_window_ends_at": "2026-05-13T10:00:00+00:00",
            },
        }

    graph = {
        "nodes": [
            node("A"),
            node("X"),
            node("B"),
            node("C"),
        ],
        "edges": [
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "B",
            },
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "C",
            },
        ],
    }

    rows = gantt_rows(graph)

    assert [row["symbol"] for row in rows] == ["A", "B", "C", "X"]


def test_gantt_rows_compact_shared_child_dependencies_without_breaking_topology():
    def node(symbol: str) -> dict[str, object]:
        return {
            "process_id": f"p-{symbol.lower()}",
            "process_symbol": symbol,
            "computed_status": "ready",
            "resource_aware": {
                "starts_at": "2026-05-13T09:00:00+00:00",
                "ends_at": "2026-05-13T10:00:00+00:00",
                "schedule_window_starts_at": "2026-05-13T09:00:00+00:00",
                "schedule_window_ends_at": "2026-05-13T10:00:00+00:00",
            },
        }

    graph = {
        "nodes": [
            node("A"),
            node("X"),
            node("C"),
            node("D"),
        ],
        "edges": [
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "D",
            },
            {
                "predecessor_process_symbol": "X",
                "successor_process_symbol": "C",
            },
            {
                "predecessor_process_symbol": "C",
                "successor_process_symbol": "D",
            },
        ],
    }

    rows = gantt_rows(graph)

    assert [row["symbol"] for row in rows] == ["X", "C", "A", "D"]


def test_gantt_completedness_legend_describes_process_state_colors():
    legend = gantt_completedness_legend_items()

    assert [item["state"] for item in legend] == [
        "waiting",
        "early_start",
        "ready",
        "started",
        "due",
        "finished",
    ]
    assert legend[0]["label"] == "Waiting - parents unfinished, unpinned"
    assert legend[-1]["label"] == "Finished - all roles verified done"


def test_utilization_heatmap_adapters_normalize_resource_and_role_series():
    utilization = {
        "time_series": [
            {
                "starts_at": "2026-05-13T09:00:00+00:00",
                "ends_at": "2026-05-13T10:00:00+00:00",
                "resource_id": "res_a",
                "role_ids": ["role_dev"],
                "capacity_hours": 1,
                "allocated_hours": 0.5,
                "utilization_ratio": 0.5,
            },
            {
                "starts_at": "2026-05-13T10:00:00+00:00",
                "ends_at": "2026-05-13T11:00:00+00:00",
                "resource_id": "res_a",
                "role_ids": ["role_dev"],
                "capacity_hours": 1,
                "allocated_hours": 1,
                "utilization_ratio": 1,
            },
            {
                "starts_at": "2026-05-13T11:00:00+00:00",
                "ends_at": "2026-05-13T12:00:00+00:00",
                "resource_id": "res_a",
                "role_ids": ["role_dev"],
                "capacity_hours": 1,
                "allocated_hours": 0,
                "utilization_ratio": 0,
            },
            {
                "starts_at": "2026-05-13T10:00:00+00:00",
                "ends_at": "2026-05-13T11:00:00+00:00",
                "resource_id": "res_b",
                "role_ids": ["role_ops"],
                "capacity_hours": 1,
                "allocated_hours": 0.25,
                "utilization_ratio": 0.25,
            },
        ]
    }
    schedule = {
        "allocation_slices": [
            {
                "role_id": "role_dev",
                "starts_at": "2026-05-13T09:30:00+00:00",
                "ends_at": "2026-05-13T10:30:00+00:00",
            }
        ],
        "processes": [
            {
                "starts_at": "2026-05-13T09:30:00+00:00",
                "ends_at": "2026-05-13T10:30:00+00:00",
            }
        ],
    }

    resource_labels, resource_times, resource_matrix = resource_utilization_heatmap(
        utilization,
        schedule,
    )
    role_labels, role_times, role_matrix = role_utilization_heatmap(utilization, schedule)

    assert resource_labels == ["res_a", "res_b"]
    assert [time.hour for time in resource_times] == [9, 10]
    assert resource_matrix[0] == [0.5, 1.0]
    assert resource_matrix[1][0] == 0.0
    assert resource_matrix[1][1] == 0.25
    assert role_labels == ["role_dev", "role_ops"]
    assert [time.hour for time in role_times] == [9, 10]
    assert role_matrix[0] == [0.5, 0.5]
    assert role_matrix[1][0] == 0.0
    assert role_matrix[1][1] == 0.0
    now = dt.datetime(2026, 5, 13, 10, tzinfo=dt.UTC)
    _, future_resource_times, future_resource_matrix = resource_utilization_heatmap(
        utilization,
        schedule,
        now=now,
    )
    _, future_role_times, future_role_matrix = role_utilization_heatmap(
        utilization,
        schedule,
        now=now,
    )

    assert [time.hour for time in future_resource_times] == [10]
    assert future_resource_matrix == [[1.0], [0.25]]
    assert [time.hour for time in future_role_times] == [10]
    assert future_role_matrix == [[0.5], [0.0]]
    span = schedule_time_span(schedule)
    assert span is not None
    assert [span[0].hour, span[0].minute] == [9, 30]
    assert [span[1].hour, span[1].minute] == [10, 30]


def test_graph_adapter_marks_sensitive_and_collapsed_nodes():
    dot = build_process_graph_dot(
        {
            "nodes": [
                {
                    "process_id": "p1",
                    "process_symbol": "A",
                    "name": "Start",
                    "computed_status": "started",
                    "resource_aware": {
                        "starts_at": "2026-05-13T09:00:00+00:00",
                        "ends_at": "2026-05-13T11:00:00+00:00",
                        "schedule_buffer_hours": 0,
                        "max_makespan_sensitivity_hours": 1,
                    },
                },
                {
                    "process_id": "p2",
                    "process_symbol": "B",
                    "name": "Finish",
                    "computed_status": "ready",
                    "resource_aware": {
                        "starts_at": "2026-05-13T11:00:00+00:00",
                        "ends_at": "2026-05-13T13:00:00+00:00",
                        "schedule_buffer_hours": 0,
                        "max_makespan_sensitivity_hours": 1,
                    },
                },
                {
                    "process_id": "p3",
                    "process_symbol": "C",
                    "name": "Optional",
                    "computed_status": "planned",
                    "resource_aware": {
                        "starts_at": "2026-05-13T09:00:00+00:00",
                        "ends_at": "2026-05-13T17:00:00+00:00",
                        "schedule_buffer_hours": 3,
                        "max_makespan_sensitivity_hours": 0,
                    },
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
    assert "duration: 2h; sensitivity: 1h" in dot
    assert "duration: 8h; buffer: 3h" in dot
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
