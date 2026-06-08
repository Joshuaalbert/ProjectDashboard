import datetime as dt
import json
from types import SimpleNamespace

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from projdash.ui.app import (
    _batch_role_requirements_by_symbol,
    _blocker_sections,
    _capacity_buckets_for_display,
    _commit_project_state_payload,
    _completed_process_rows,
    _context_terminal_symbols,
    _decrypt_slack_token_for_ui,
    _dependency_set_operations,
    _encrypt_slack_token_for_ui,
    _enrich_priority_rows,
    _ensure_resource_schedule,
    _load_context,
    _normalize_slack_users,
    _parse_codex_debug_models,
    _plot_gantt_pin_marker,
    _prepare_context_for_section,
    _priority_expander_sections,
    _priority_markdown,
    _priority_process_markdown,
    _process_child_symbols,
    _process_revision_defaults_signature,
    _process_role_revision_command,
    _project_context_markdown,
    _recover_orphaned_slack_run,
    _render_gantt_chart,
    _render_graph,
    _resource_calendar_rules_markdown,
    _schedule_debug_payload,
    _schedule_snapshot_query_payload,
    _slack_action_passphrase_keys_to_clear,
    _slack_manifest_payload,
    _slack_mapping_commands,
    _slack_mapping_rows,
    _slack_service_run_is_orphaned,
)


def test_dependency_set_operations_preserve_internal_selected_edges():
    graph = {
        "nodes": [
            {"process_symbol": "A"},
            {"process_symbol": "B"},
            {"process_symbol": "C"},
        ],
        "edges": [
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "B",
            },
            {
                "predecessor_process_symbol": "C",
                "successor_process_symbol": "B",
            },
        ],
    }

    operations = _dependency_set_operations(
        graph,
        ["A", "B"],
        ["C"],
        side="predecessors",
    )

    assert operations == [
        {
            "action": "add_dependency",
            "operation_id": "add-C-A",
            "predecessor_process_symbol": "C",
            "successor_process_symbol": "A",
        }
    ]


def test_gantt_pin_marker_plot_uses_one_o_per_start_and_one_x_per_finish():
    fig, ax = plt.subplots()
    try:
        for marker in [
            {"kind": "pin_start", "at": dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)},
            {"kind": "pin_start", "at": dt.datetime(2026, 5, 13, 10, tzinfo=dt.UTC)},
            {"kind": "pin_finish", "at": dt.datetime(2026, 5, 13, 12, tzinfo=dt.UTC)},
            {"kind": "pin_finish", "at": dt.datetime(2026, 5, 13, 13, tzinfo=dt.UTC)},
            {"kind": "pin_finish", "at": dt.datetime(2026, 5, 13, 14, tzinfo=dt.UTC)},
            {"kind": "pin_start", "at": "2026-05-13T15:00:00+00:00"},
        ]:
            _plot_gantt_pin_marker(ax, marker, y=0)

        markers = [line.get_marker() for line in ax.lines]
        assert markers.count("o") == 2
        assert markers.count("x") == 3
    finally:
        plt.close(fig)


def test_gantt_chart_renders_parent_rows_above_children(monkeypatch):
    import projdash.ui.app as app

    captured: dict[str, object] = {}

    def capture_pyplot(fig):
        captured["fig"] = fig

    monkeypatch.setattr(app.st, "pyplot", capture_pyplot)
    graph = {
        "nodes": [
            {
                "process_id": "p-child",
                "process_symbol": "B",
                "name": "Child",
                "computed_status": "ready",
                "started_at": "2026-05-13T12:30:00+00:00",
                "role_requirements": [
                    {
                        "requirement_id": "req-child",
                        "role_id": "role-child",
                        "pins": [
                            {
                                "pin_id": "pin-child",
                                "resource_id": "res-child",
                                "pinned_at": "2026-05-13T12:15:00+00:00",
                                "forecast_finish_at": "2026-05-13T16:00:00+00:00",
                                "status": "pinned_started",
                            }
                        ],
                    }
                ],
                "resource_aware": {
                    "starts_at": "2026-05-13T13:00:00+00:00",
                    "ends_at": "2026-05-13T17:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-13T13:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-13T17:00:00+00:00",
                },
            },
            {
                "process_id": "p-parent",
                "process_symbol": "A",
                "name": "Parent",
                "computed_status": "finished",
                "finished_at": "2026-05-13T11:00:00+00:00",
                "role_requirements": [
                    {
                        "requirement_id": "req-parent",
                        "role_id": "role-parent",
                        "pins": [
                            {
                                "pin_id": "pin-parent",
                                "resource_id": "res-parent",
                                "pinned_at": "2026-05-13T09:00:00+00:00",
                                "forecast_finish_at": "2026-05-13T11:30:00+00:00",
                                "verified_finished_at": "2026-05-13T11:30:00+00:00",
                                "status": "pinned_finished",
                            }
                        ],
                    }
                ],
                "resource_aware": {
                    "starts_at": "2026-05-13T09:00:00+00:00",
                    "ends_at": "2026-05-13T12:00:00+00:00",
                    "schedule_window_starts_at": "2026-05-13T09:00:00+00:00",
                    "schedule_window_ends_at": "2026-05-13T12:00:00+00:00",
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

    _render_gantt_chart(
        graph,
        controls_now=None,
        terminal_symbols=[],
        timezone_name="UTC",
    )

    fig = captured["fig"]
    try:
        ax = fig.axes[0]
        assert [tick.get_text() for tick in ax.get_yticklabels()] == ["A", "B"]
        assert bool(ax.yaxis_inverted()) is True
        connectors = [line for line in ax.lines if line.get_color() == "black"]
        assert len(connectors) == 1
        connector = connectors[0]
        start_x = mdates.date2num(dt.datetime(2026, 5, 13, 11, 30, tzinfo=dt.UTC))
        end_x = mdates.date2num(dt.datetime(2026, 5, 13, 12, 15, tzinfo=dt.UTC))
        mid_x = start_x + ((end_x - start_x) / 2)
        assert list(connector.get_xdata()) == [start_x, mid_x, mid_x, end_x]
        assert list(connector.get_ydata()) == [0, 0, 1, 1]
        assert connector.get_linewidth() == 1
        assert connector.get_linestyle() == "-"
        pin_lines = [
            (line.get_marker(), list(line.get_xdata()), list(line.get_ydata()))
            for line in ax.lines
            if line.get_marker() in {"o", "x"}
        ]
        assert (
            "o",
            [mdates.date2num(dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC))],
            [0],
        ) in pin_lines
        assert (
            "o",
            [mdates.date2num(dt.datetime(2026, 5, 13, 12, 15, tzinfo=dt.UTC))],
            [1],
        ) in pin_lines
        assert (
            "x",
            [mdates.date2num(dt.datetime(2026, 5, 13, 11, 30, tzinfo=dt.UTC))],
            [0],
        ) in pin_lines
        endpoint_collections = [
            collection
            for collection in ax.collections
            if list(collection.get_sizes()) == [5]
        ]
        assert len(endpoint_collections) == 1
        assert endpoint_collections[0].get_offsets().tolist() == [
            [start_x, 0.0],
            [end_x, 1.0],
        ]
    finally:
        plt.close(fig)


def test_batch_role_requirements_distribute_aggregate_effort_without_multiplying():
    graph = {
        "nodes": [
            {
                "process_symbol": "A",
                "role_requirements": [
                    {"role_id": "role_eng", "effort_hours": 2},
                ],
            },
            {
                "process_symbol": "B",
                "role_requirements": [
                    {"role_id": "role_eng", "effort_hours": 3},
                ],
            },
        ]
    }

    by_symbol = _batch_role_requirements_by_symbol(
        graph,
        ["A", "B"],
        [{"role_id": "role_eng", "effort_hours": 10}],
    )

    assert by_symbol == {
        "A": [{"role_id": "role_eng", "effort_hours": 4}],
        "B": [{"role_id": "role_eng", "effort_hours": 6}],
    }

    uneven = _batch_role_requirements_by_symbol(
        graph,
        ["A", "B"],
        [{"role_id": "role_eng", "effort_hours": 11}],
    )

    assert uneven == {
        "A": [{"role_id": "role_eng", "effort_hours": 4}],
        "B": [{"role_id": "role_eng", "effort_hours": 7}],
    }


def test_priority_process_markdown_matches_pm_context_shape_without_sensitivity():
    row = {
        "process_id": "proc-a",
        "process_symbol": "A",
        "process_name": "Task A",
        "process_type": "standard",
        "computed_status": "ready",
        "description": "Acceptance checklist is complete.",
        "predecessors": ["P"],
        "successors": ["C"],
        "planned_start_at": "2026-05-13T09:00:00+00:00",
        "planned_finish_at": "2026-05-13T17:00:00+00:00",
        "schedule_window_starts_at": "2026-05-13T08:00:00+00:00",
        "schedule_window_ends_at": "2026-05-14T09:00:00+00:00",
        "planned_assignments": [
            {
                "resource_id": "res-ada",
                "resource_label": "Ada (`res-ada`)",
                "role_id": "role_eng",
                "role_label": "Engineer (`role_eng`)",
            }
        ],
        "role_requirements": [
            {
                "requirement_id": "req-a",
                "role_id": "role_eng",
                "effort_hours": 8,
            }
        ],
        "max_makespan_sensitivity_hours": 99,
    }

    markdown = _priority_process_markdown(
        row,
        "UTC",
        role_labels={"role_eng": "Engineer (`role_eng`)"},
    )

    assert "- Type: normal" in markdown
    assert "- Mode: planned" in markdown
    assert "- Status: `ready`" in markdown
    assert "- Role requirement: Engineer (`role_eng`) | `req-a`" in markdown
    assert "- Effort hours: 8 hours" in markdown
    assert "- Definition: Acceptance checklist is complete." in markdown
    assert "- Parents: {P}" in markdown
    assert "- Children: {C}" in markdown
    assert "- Assigned to: Ada (`res-ada`) for Engineer (`role_eng`)" in markdown
    assert "- Planned start: 2026-05-13 09:00 UTC" in markdown
    assert "- Planned finish: 2026-05-13 17:00 UTC" in markdown
    assert "pre-buffer" in markdown
    assert "Sensitivity" not in markdown


def test_enrich_priority_rows_derives_topology_from_graph_edges():
    graph = {
        "nodes": [
            {
                "process_id": "proc-parent",
                "process_symbol": "P",
                "name": "Parent",
            },
            {
                "process_id": "proc-a",
                "process_symbol": "A",
                "name": "Task A",
                "process_type": "standard",
                "computed_status": "early_start",
                "description": "Acceptance checklist is complete.",
                "role_requirements": [
                    {
                        "requirement_id": "req-a",
                        "role_id": "role_eng",
                        "effort_hours": 8,
                    }
                ],
            },
            {
                "process_id": "proc-child",
                "process_symbol": "C",
                "name": "Child",
            },
        ],
        "edges": [
            {
                "predecessor_process_symbol": "P",
                "successor_process_symbol": "A",
            },
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "C",
            },
        ],
    }
    rows = [
        {
            "priority": "P0",
            "process_id": "proc-a",
            "process_symbol": "A",
            "process_name": "Task A",
            "planned_start_at": "2026-05-13T09:00:00+00:00",
            "planned_finish_at": "2026-05-13T17:00:00+00:00",
        }
    ]

    enriched = _enrich_priority_rows(rows, graph, {"blockers": []})

    assert enriched[0]["predecessors"] == ["P"]
    assert enriched[0]["successors"] == ["C"]
    markdown = _priority_process_markdown(
        enriched[0],
        "UTC",
        role_labels={"role_eng": "Engineer (`role_eng`)"},
    )
    assert "- Parents: {P}" in markdown
    assert "- Children: {C}" in markdown


def test_priority_process_markdown_shows_pin_mode_fields():
    row = {
        "process_symbol": "A",
        "process_name": "Task A",
        "process_type": "blocker",
        "computed_status": "due",
        "role_requirements": [
            {
                "requirement_id": "req-a",
                "role_id": "role_eng",
                "effort_hours": 1,
                "pins": [
                    {
                        "resource_id": "res-ada",
                        "pinned_at": "2026-05-13T09:00:00+00:00",
                        "forecast_finish_at": "2026-05-13T12:00:00+00:00",
                        "status": "pinned_started",
                    }
                ],
            }
        ],
    }

    markdown = _priority_process_markdown(
        row,
        "UTC",
        role_labels={"role_eng": "Engineer (`role_eng`)"},
        resource_labels={"res-ada": "Ada (`res-ada`)"},
    )

    assert "- Type: blocker" in markdown
    assert "- Mode: pinned" in markdown
    assert "- Pinned to: Ada (`res-ada`)" in markdown
    assert "- Pinned started: 2026-05-13 09:00 UTC" in markdown
    assert "- Forecasted finish: 2026-05-13 12:00 UTC" in markdown


def test_process_role_revision_command_preserves_process_metadata_and_dependencies():
    graph = {
        "nodes": [
            {
                "process_id": "proc-p",
                "process_symbol": "P",
            },
            {
                "process_id": "proc-a",
                "process_symbol": "A",
            },
        ],
        "edges": [
            {
                "predecessor_process_symbol": "P",
                "successor_process_symbol": "A",
            }
        ],
    }
    row = {
        "process_id": "proc-a",
        "process_symbol": "A",
        "process_name": "Task A",
        "description": "Done definition.",
        "process_type": "standard",
        "duration_business_days": 2,
        "earliest_start_at": "2026-05-13T09:00:00+00:00",
        "start_at_earliest": True,
        "delay_after_dependencies_business_days": 1,
        "assumption_note": "Keep this note.",
        "role_requirements": [
            {
                "requirement_id": "req-a",
                "role_id": "role_old",
                "effort_hours": 3,
                "allocation_policy": "split_allowed",
                "required_resource_count": 1,
            }
        ],
    }

    command = _process_role_revision_command(
        project_id="project-alpha",
        graph=graph,
        row=row,
        role_id="role_new",
        effort_hours=5,
        effective_at=dt.datetime(2026, 5, 14, 9, tzinfo=dt.UTC),
    )

    assert command == {
        "action": "upsert_process_revision",
        "project_id": "project-alpha",
        "process_symbol": "A",
        "process_type": "standard",
        "name": "Task A",
        "description": "Done definition.",
        "effective_at": dt.datetime(2026, 5, 14, 9, tzinfo=dt.UTC),
        "duration_business_days": 2,
        "dependencies": ["proc-p"],
        "earliest_start_at": "2026-05-13T09:00:00+00:00",
        "start_at_earliest": True,
        "delay_after_dependencies_business_days": 1,
        "role_requirements": [
            {
                "requirement_id": "req-a",
                "role_id": "role_new",
                "effort_hours": 5,
                "allocation_policy": "split_allowed",
                "required_resource_count": 1,
            }
        ],
        "assumption_note": "Keep this note.",
    }


def test_process_child_symbols_returns_sorted_unique_children():
    graph = {
        "edges": [
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "B",
            },
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "B",
            },
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "C",
            },
        ]
    }

    assert _process_child_symbols(graph, "A") == ["B", "C"]


def test_completed_process_rows_excludes_resource_priority_processes():
    graph = {
        "nodes": [
            {
                "process_id": "proc-done",
                "process_symbol": "done-process",
                "name": "Done process",
                "computed_status": "finished",
                "process_type": "standard",
                "description": "Done definition.",
                "role_requirements": [
                    {
                        "requirement_id": "req-done",
                        "role_id": "role_eng",
                        "effort_hours": 1,
                    }
                ],
                "resource_aware": {
                    "starts_at": "2026-05-13T09:00:00+00:00",
                    "ends_at": "2026-05-13T10:00:00+00:00",
                },
            },
            {
                "process_id": "proc-visible",
                "process_symbol": "visible-process",
                "computed_status": "finished",
            },
            {
                "process_id": "proc-ready",
                "process_symbol": "ready-process",
                "computed_status": "ready",
            },
        ]
    }

    rows = _completed_process_rows(graph, {"visible-process"})

    assert [row["process_symbol"] for row in rows] == ["done-process"]
    assert rows[0]["completed_group"] == "finished"
    assert rows[0]["priority"] == "Done"


def test_process_revision_defaults_signature_ignores_volatile_as_of_time():
    aggregate = {
        "process_symbols": ["A", "B"],
        "predecessors": ["P"],
        "children": ["C"],
        "role_efforts": {"role_eng": 5.0},
        "status": "planned",
        "name": "",
        "description": "",
        "earliest_start_at": None,
        "started_at": None,
        "finished_at": None,
        "blocker_ids": [],
    }
    first = _process_revision_defaults_signature(
        aggregate,
        {
            "timezone": "UTC",
            "as_of": dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC),
        },
    )
    second = _process_revision_defaults_signature(
        aggregate,
        {
            "timezone": "UTC",
            "as_of": dt.datetime(2026, 5, 13, 10, tzinfo=dt.UTC),
        },
    )

    assert first == second


def test_capacity_buckets_for_display_uses_utilization_allocations():
    rows = _capacity_buckets_for_display(
        [
            {
                "resource_id": "res-a",
                "starts_at": "2026-05-13T09:00:00+00:00",
                "ends_at": "2026-05-13T10:00:00+00:00",
                "capacity_hours": 1,
                "allocated_hours": 0,
                "remaining_hours": 1,
            }
        ],
        {
            "time_series": [
                {
                    "resource_id": "res-a",
                    "starts_at": "2026-05-13T09:00:00+00:00",
                    "ends_at": "2026-05-13T10:00:00+00:00",
                    "allocated_hours": 0.75,
                }
            ]
        },
    )

    assert rows[0]["allocated_hours"] == 0.75
    assert rows[0]["remaining_hours"] == 0.25


def test_priority_markdown_filters_and_formats_priority_fields():
    markdown = _priority_markdown(
        [
            {
                "priority": "P2",
                "priority_rank": 2,
                "process_symbol": "A",
                "process_name": "Design",
                "hours_until_planned_start": 1,
                "hours_until_planned_finish": 3,
                "effort_hours": 2,
                "role_id": "role_eng",
            },
            {
                "priority": "P3",
                "priority_rank": 3,
                "process_symbol": "B",
                "process_name": "Build",
                "planned_start_at": dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC),
                "planned_finish_at": dt.datetime(2026, 5, 14, 9, tzinfo=dt.UTC),
                "schedule_window_starts_at": dt.datetime(
                    2026,
                    5,
                    13,
                    3,
                    tzinfo=dt.UTC,
                ),
                "schedule_window_ends_at": dt.datetime(2026, 5, 15, 9, tzinfo=dt.UTC),
                "hours_until_planned_start": 12,
                "hours_until_planned_finish": 16,
                "effort_hours": 4,
                "role_id": "role_qa",
                "planned_assignments": [
                    {
                        "resource_id": "res_grace",
                        "role_id": "role_qa",
                        "resource_label": "Grace (`res_grace`)",
                        "role_label": "QA (`role_qa`)",
                    }
                ],
            },
        ],
        "role_id",
        ["role_qa"],
        id_label="Role",
    )

    assert "### Role `role_qa`" in markdown
    assert "#### P3 | B | Build" in markdown
    assert "- Type: normal" in markdown
    assert "- Mode: planned" in markdown
    assert "- Planned start: 2026-05-13 09:00 UTC" in markdown
    assert "- Planned finish: 2026-05-14 09:00 UTC" in markdown
    assert "0.25 days pre-buffer | 1 day duration | 1 day post-buffer" in markdown
    assert "- Assigned to: Grace (`res_grace`) for QA (`role_qa`)" in markdown
    assert "- Effort hours: 4 hours" in markdown
    assert "- Pinned started:" not in markdown
    assert "role_eng" not in markdown
    assert "`A`" not in markdown


def test_priority_markdown_formats_on_time_started_and_finished_as_early():
    planned_start_at = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    planned_finish_at = dt.datetime(2026, 5, 13, 17, tzinfo=dt.UTC)

    markdown = _priority_markdown(
        [
            {
                "priority": "P1",
                "process_symbol": "A",
                "process_name": "Design",
                "planned_start_at": planned_start_at,
                "planned_finish_at": planned_finish_at,
                "schedule_window_starts_at": planned_start_at,
                "schedule_window_ends_at": planned_finish_at,
                "effort_hours": 2,
                "role_id": "role_eng",
                "pin_started_at": planned_start_at,
                "pin_status": "pinned_finished",
                "pin_verified_done_at": planned_finish_at,
            },
        ],
        "role_id",
        [],
        id_label="Role",
    )

    assert "- Mode: pinned" in markdown
    assert "- Pinned started: 2026-05-13 09:00 UTC" in markdown
    assert "- Verified finish: 2026-05-13 17:00 UTC" in markdown


def test_priority_expander_sections_group_selected_entities():
    sections = _priority_expander_sections(
        [
            {
                "priority": "P1",
                "process_symbol": "A",
                "process_name": "Design",
                "hours_until_planned_start": -1,
                "effort_hours": 2,
                "resource_id": "res_ada",
            },
            {
                "priority": "P2",
                "process_symbol": "B",
                "process_name": "Build",
                "hours_until_planned_start": 4,
                "effort_hours": 6,
                "resource_id": "res_grace",
            },
        ],
        "resource_id",
        ["res_ada"],
        id_label="Resource",
    )

    assert len(sections) == 1
    assert sections[0]["label"] == "Resource `res_ada` (1 process)"
    assert len(sections[0]["rows"]) == 1
    assert sections[0]["rows"][0]["priority"] == "P1"
    assert sections[0]["rows"][0]["process_symbol"] == "A"
    assert sections[0]["rows"][0]["hours_until_planned_start"] == -1


def test_blocker_sections_split_unresolved_and_resolved_rows():
    sections = _blocker_sections(
        [
            {"blocker_id": "blocker-open", "is_resolved_as_of": False},
            {"blocker_id": "blocker-closed", "is_resolved_as_of": True},
        ]
    )

    assert [row["blocker_id"] for row in sections["unresolved"]] == ["blocker-open"]
    assert [row["blocker_id"] for row in sections["resolved"]] == ["blocker-closed"]


def test_resource_calendar_rules_markdown_lists_default_and_overrides():
    markdown = _resource_calendar_rules_markdown(
        {
            "resources": [
                {
                    "resource_id": "res_josh",
                    "name": "Josh",
                    "calendar_id": "cal_default",
                    "available_from_at": "2026-05-14T09:00:00+00:00",
                    "available_until_at": None,
                    "calendar_overrides": [
                        {
                            "rule_id": "august",
                            "calendar_id": "cal_august",
                            "starts_at": "2026-08-01T00:00:00+00:00",
                            "ends_at": "2026-09-01T00:00:00+00:00",
                            "reason": "August availability.",
                        }
                    ],
                }
            ],
            "calendars": [
                {
                    "calendar_id": "cal_default",
                    "name": "Default",
                    "timezone": "Europe/Amsterdam",
                },
                {
                    "calendar_id": "cal_august",
                    "name": "August",
                    "timezone": "Europe/Amsterdam",
                },
            ],
        },
        "UTC",
    )

    assert "### Josh (`res_josh`)" in markdown
    assert "- Default: **Default** (`cal_default`)" in markdown
    assert "- Override `august`: **August** (`cal_august`)" in markdown
    assert "August availability." in markdown


def test_slack_manifest_payload_uses_slack_schema_fields_and_scopes():
    payload = _slack_manifest_payload("project-a", "Project A Bot")

    assert payload["display_information"]["name"] == "Project A Bot"
    assert payload["features"]["app_home"]["messages_tab_enabled"] is True
    assert (
        payload["features"]["app_home"]["messages_tab_read_only_enabled"]
        is False
    )
    assert payload["features"]["bot_user"]["display_name"] == "Project A Bot"
    assert "_metadata" not in payload
    assert "chat:write" in payload["oauth_config"]["scopes"]["bot"]
    assert "users:read" in payload["oauth_config"]["scopes"]["bot"]


def test_parse_codex_debug_models_handles_json_and_text_output():
    json_output = json.dumps(
        {
            "models": [
                {"slug": "gpt-5.5", "display_name": "GPT-5.5"},
                {"id": "gpt-5-codex", "label": "GPT-5 Codex"},
                {"name": "o4-mini"},
                {"id": "gpt-5-codex"},
            ]
        }
    )
    text_output = """
    MODEL                 DESCRIPTION
    gpt-5-codex           default coding model
    | o4-mini | compact |
    """

    assert _parse_codex_debug_models(json_output) == [
        "gpt-5.5",
        "gpt-5-codex",
        "o4-mini",
    ]
    assert _parse_codex_debug_models(text_output) == ["gpt-5-codex", "o4-mini"]


def test_slack_action_passphrase_keys_clear_only_when_leaving_slack_section():
    keys = [
        "slack_action_passphrase_project-a",
        "slack_store_passphrase_project-a",
        "slack_action_passphrase_project-b",
        "other",
    ]

    assert _slack_action_passphrase_keys_to_clear("Slack", "Resources", keys) == [
        "slack_action_passphrase_project-a",
        "slack_action_passphrase_project-b",
    ]
    assert _slack_action_passphrase_keys_to_clear("Slack", "Slack", keys) == []
    assert _slack_action_passphrase_keys_to_clear("Project", "Resources", keys) == []


class _RecordingCommandService:
    def __init__(self) -> None:
        self.commands: list[dict] = []

    def handle_command(self, envelope):
        payload = envelope.command.model_dump(mode="json")
        self.commands.append(payload)
        return SimpleNamespace(ok=True, warnings=[], entity_ids={})


def test_slack_orphaned_run_can_be_marked_failed_to_unlock_ui():
    started_at = dt.datetime(2026, 5, 19, 12, tzinfo=dt.UTC).isoformat()
    service_job = {
        "run_id": "run-stuck",
        "project_id": "project-a",
        "status": "running",
        "started_at": started_at,
    }

    assert _slack_service_run_is_orphaned(service_job, None) is True
    assert (
        _slack_service_run_is_orphaned(
            service_job,
            {"run_id": "run-stuck", "status": "running"},
        )
        is False
    )

    service = _RecordingCommandService()
    assert _recover_orphaned_slack_run(service, "project-a", service_job) is True

    assert service.commands == [
        {
            "action": "finish_slack_run",
            "project_id": "project-a",
            "run_id": "run-stuck",
            "status": "failed",
            "finished_at": service.commands[0]["finished_at"],
            "collected_message_count": 0,
            "draft_outbox_ids": [],
            "result_json": {
                "message": (
                    "Marked failed by the UI because the active Slack run had "
                    "no worker in this app process."
                ),
                "recovered_orphaned_run": True,
            },
            "error_text": "Interrupted Slack run had no active UI worker.",
        }
    ]


def test_slack_token_crypto_helpers_round_trip_with_service_helper():
    encrypted, encrypt_error = _encrypt_slack_token_for_ui(
        "xoxb-test-token",
        "correct horse battery staple",
    )

    assert encrypt_error is None
    assert encrypted is not None
    assert encrypted["ciphertext"] != "xoxb-test-token"

    token, decrypt_error = _decrypt_slack_token_for_ui(
        None,
        "project-a",
        {"encrypted_token": encrypted},
        "correct horse battery staple",
    )

    assert decrypt_error is None
    assert token == "xoxb-test-token"


def test_slack_token_decrypt_does_not_fall_back_to_env_var(monkeypatch):
    monkeypatch.setenv("PROJDASH_SLACK_TEST_TOKEN", "xoxb-env-token")

    token, decrypt_error = _decrypt_slack_token_for_ui(
        None,
        "project-a",
        {"config": {"bot_token_secret_ref": "PROJDASH_SLACK_TEST_TOKEN"}},
        "irrelevant-passphrase",
    )

    assert token is None
    assert decrypt_error == (
        "No decryptable Slack token is available. Store an encrypted token first."
    )


def test_slack_mapping_rows_and_commands_clear_and_set_mappings():
    rows = _slack_mapping_rows(
        slack_users=[
            {"slack_user_id": "U1", "slack_name": "Ada"},
            {"slack_user_id": "U2", "slack_name": "Grace"},
        ],
        resources=[
            {"resource_id": "res_ada", "name": "Ada"},
            {"resource_id": "res_grace", "name": "Grace"},
        ],
        resource_mappings=[
            {
                "resource_id": "res_ada",
                "slack_user_id": "U1",
                "display_name": "Ada",
                "active": True,
            }
        ],
    )

    assert rows == [
        {
            "mapped": True,
            "slack_name": "Ada",
            "slack_user_id": "U1",
            "resource_id": "res_ada",
        },
        {
            "mapped": False,
            "slack_name": "Grace",
            "slack_user_id": "U2",
            "resource_id": "",
        },
    ]

    edited = [
        {**rows[0], "mapped": False, "resource_id": ""},
        {**rows[1], "mapped": True, "resource_id": "res_grace"},
    ]
    commands, error = _slack_mapping_commands(
        project_id="project-a",
        rows=edited,
        current_mappings=[
            {
                "resource_id": "res_ada",
                "slack_user_id": "U1",
                "display_name": "Ada",
                "active": True,
            }
        ],
        updated_at=dt.datetime(2026, 5, 19, 12, tzinfo=dt.UTC),
    )

    assert error is None
    assert commands == [
        {
            "action": "set_resource_slack_user",
            "project_id": "project-a",
            "resource_id": "res_ada",
            "slack_user_id": None,
            "display_name": None,
            "active": False,
            "updated_at": dt.datetime(2026, 5, 19, 12, tzinfo=dt.UTC),
        },
        {
            "action": "set_resource_slack_user",
            "project_id": "project-a",
            "resource_id": "res_grace",
            "slack_user_id": "U2",
            "display_name": "Grace",
            "active": True,
            "updated_at": dt.datetime(2026, 5, 19, 12, tzinfo=dt.UTC),
        },
    ]


def test_normalize_slack_users_accepts_integration_user_objects():
    class SlackUserLike:
        def __init__(
            self,
            slack_user_id: str,
            display_name: str,
            *,
            is_app_user: bool = False,
        ) -> None:
            self.slack_user_id = slack_user_id
            self.display_name = display_name
            self.real_name = None
            self.name = None
            self.email = None
            self.timezone = "UTC"
            self.deleted = False
            self.is_bot = False
            self.is_app_user = is_app_user

    rows = _normalize_slack_users(
        [
            SlackUserLike("U1", "Ada"),
            SlackUserLike("UAPP", "ProjDash", is_app_user=True),
        ]
    )

    assert rows == [
        {
            "slack_user_id": "U1",
            "slack_name": "Ada",
            "email": None,
            "team_id": None,
        }
    ]


def test_project_context_markdown_summarizes_schedule_and_risks():
    markdown = _project_context_markdown(
        {
            "timezone": "UTC",
            "as_of": dt.datetime(2026, 5, 15, 9, tzinfo=dt.UTC),
        },
        {
            "agent_context": {
                "project": {
                    "name": "Accelerating Astrophysics",
                    "project_id": "accelerating_astro",
                },
                "summary": {
                    "projected_completion_at": "2026-11-04T10:00:00+00:00",
                    "total_role_effort_hours": 286,
                    "top_makespan_sensitivity": [
                        {
                            "symbol": "write-paper",
                            "max_makespan_sensitivity_hours": 2,
                        }
                    ],
                    "process_count": 17,
                    "edge_count": 27,
                    "status_counts": {"planned": 12, "done": 5},
                    "blocked_process_count": 1,
                    "converged": True,
                },
                "slippage": {"completion_change_hours": 12},
                "schedule": {
                    "processes": [
                        {
                            "symbol": "write-paper",
                            "name": "Write paper",
                            "status": "planned",
                            "computed_status": "ready",
                            "planned_start_at": "2026-05-16T09:00:00+00:00",
                            "planned_finish_at": "2026-05-20T17:00:00+00:00",
                            "schedule_buffer_hours": 0,
                            "max_makespan_sensitivity_hours": 2,
                            "sensitivity_label": "makespan_sensitive",
                            "allocation_state": "allocated",
                        }
                    ]
                },
                "prioritized_work": {
                    "by_role": [
                        {
                            "role_id": "role_write",
                            "role_name": "Writing",
                            "processes": [
                                {
                                    "priority": "P1",
                                    "process_symbol": "write-paper",
                                    "process_name": "Write paper",
                                    "hours_until_planned_start": -2,
                                    "effort_hours": 12,
                                    "computed_status": "ready",
                                }
                            ],
                        }
                    ],
                    "by_resource": [
                        {
                            "resource_id": "res_ada",
                            "resource_name": "Ada",
                            "processes": [
                                {
                                    "priority": "P2",
                                    "process_symbol": "write-paper",
                                    "process_name": "Write paper",
                                    "hours_until_planned_start": 4,
                                    "effort_hours": 6,
                                    "role_ids": ["role_write"],
                                    "computed_status": "started",
                                }
                            ],
                        }
                    ],
                },
                "blockers": [
                    {
                        "severity": "warning",
                        "process_symbol": "poster-inputs",
                        "summary": "Poster inputs needed",
                    }
                ],
            },
            "catalog": {
                "resources": [
                    {
                        "resource_id": "res_ada",
                        "name": "Ada",
                        "calendar_id": "cal_default",
                        "available_from_at": "2026-05-13T09:00:00+00:00",
                        "available_until_at": None,
                        "calendar_overrides": [],
                    }
                ],
                "calendars": [
                    {
                        "calendar_id": "cal_default",
                        "name": "Default",
                        "timezone": "UTC",
                    }
                ],
            },
        },
    )

    assert "# Accelerating Astrophysics" in markdown
    assert "- Projected completion: 2026-11-04 10:00 UTC" in markdown
    assert "- Completion change: 12 hours" in markdown
    assert "- Status counts: done: 5, planned: 12" in markdown
    assert "- `write-paper`: 2 hours" in markdown
    assert "## Role Priorities" in markdown
    assert "- **Writing** (`role_write`)" in markdown
    assert (
        "**P1** `write-paper` - Write paper; planned start: overdue by 0.08 days; "
        "effort: 12 hours; status: ready"
    ) in markdown
    assert "## Resource Priorities" in markdown
    assert "- **Ada** (`res_ada`)" in markdown
    assert "roles: `role_write`" in markdown
    assert "## Schedule Watchlist" in markdown
    assert (
        "- **sensitive** `write-paper` - Write paper; status: ready; "
        "planned start: 2026-05-16 09:00 UTC; "
        "planned finish: 2026-05-20 17:00 UTC; "
        "buffer: 0 hours; sensitivity: 2 hours"
    ) in markdown
    assert "- [warning] `poster-inputs`: Poster inputs needed" in markdown
    assert "## Resource Calendar Rules" in markdown
    assert "### Ada (`res_ada`)" in markdown


def test_schedule_debug_payload_contains_query_and_schedule_context():
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    payload = _schedule_debug_payload(
        {
            "project_id": "project-alpha",
            "timezone": "UTC",
            "as_of": as_of,
            "now": as_of,
        },
        {
            "scope": {"type": "project"},
            "now": as_of,
            "project": {"project": {"project_id": "project-alpha"}},
            "catalog": {"roles": [], "resources": [], "calendars": []},
            "graph": {"nodes": []},
            "full_graph": {"nodes": []},
            "blockers": {"blockers": []},
            "resource_schedule": {"processes": []},
            "capacity": {"buckets": []},
            "utilization": {"by_resource": [], "by_role": []},
            "costs": {"total_cost": "0"},
        },
        ["A"],
    )

    assert payload["debug_schema"] == 1
    assert payload["terminal_process_symbols"] == ["A"]
    assert payload["resource_schedule_query"]["action"] == "query_resource_schedule"
    assert "horizon_starts_at" not in payload["resource_schedule_query"]
    assert "horizon_ends_at" not in payload["resource_schedule_query"]
    json.dumps(payload, default=str)


class _RecordingQueryService:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.payloads: list[dict] = []

    def handle_query(self, envelope):
        payload = envelope.query.model_dump(mode="json")
        action = payload["action"]
        self.actions.append(action)
        self.payloads.append(payload)
        return SimpleNamespace(ok=True, warnings=[], data=self._data(action, payload))

    def _data(self, action: str, payload: dict) -> dict:
        if action == "get_project":
            return {
                "project": {
                    "project_id": payload["project_id"],
                    "name": "Alpha",
                    "start_at": "2026-05-13T09:00:00+00:00",
                    "default_currency": "USD",
                }
            }
        if action == "query_project_catalog":
            return {
                "project_id": payload["project_id"],
                "roles": [],
                "resources": [],
                "calendars": [],
                "milestones": [
                    {
                        "milestone_id": "milestone-alpha",
                        "name": "Alpha",
                        "process_symbols": ["A"],
                        "active": True,
                    }
                ],
            }
        if action == "query_process_graph":
            return {
                "project_id": payload["project_id"],
                "nodes": [
                    {
                        "process_id": "proc-a",
                        "process_symbol": "A",
                        "role_requirements": [],
                    }
                ],
                "edges": [],
                "allocation_slices": [],
            }
        if action == "query_resource_schedule":
            return {
                "project_id": payload["project_id"],
                "processes": [],
                "allocation_slices": [],
            }
        if action == "query_blockers":
            return {
                "project_id": payload["project_id"],
                "blockers": [],
                "blocked_process_ids": [],
            }
        if action == "query_agent_context":
            return {
                "project_id": payload["project_id"],
                "project": {"project_id": payload["project_id"], "name": "Alpha"},
                "summary": {},
                "schedule": {},
                "slippage": {},
                "prioritized_work": {},
                "blockers": [],
            }
        if action == "query_utilization":
            return {
                "project_id": payload["project_id"],
                "by_resource": [],
                "by_role": [],
                "time_series": [],
            }
        if action == "query_costs":
            return {
                "project_id": payload["project_id"],
                "total_cost": "0",
                "currency": "USD",
                "by_resource": [],
                "by_process": [],
                "by_role": [],
                "time_series": [],
            }
        if action == "query_schedule_snapshots":
            return {
                "project_id": payload["project_id"],
                "snapshots": [],
            }
        raise AssertionError(f"Unexpected query action: {action}")


def test_load_context_omits_expensive_tab_queries():
    service = _RecordingQueryService()
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)

    context = _load_context(
        service,
        {
            "project_id": "project-alpha",
            "as_of": as_of,
            "now": as_of,
        },
    )

    assert service.actions == ["get_project"]
    assert context["project"]["project"]["project_id"] == "project-alpha"
    assert context["catalog"] is None
    assert context["graph"] is None
    assert context["resource_schedule"] is None
    assert context["utilization"] is None
    assert context["costs"] is None
    assert context["agent_context"] is None


def test_lazy_resource_schedule_query_runs_once_per_context():
    service = _RecordingQueryService()
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    controls = {
        "project_id": "project-alpha",
        "as_of": as_of,
        "now": as_of,
    }
    context = _load_context(service, controls)
    service.actions.clear()

    first = _ensure_resource_schedule(service, controls, context)
    second = _ensure_resource_schedule(service, controls, context)

    assert first == second
    assert service.actions == ["query_resource_schedule"]
    assert service.payloads[-1]["resource_schedule_backend"] == "mcts"


def test_context_terminal_symbols_ignore_unvalidated_session_state(monkeypatch):
    import projdash.ui.app as app

    monkeypatch.setitem(app.st.session_state, "terminal_process_symbols", ["OLD"])

    assert _context_terminal_symbols({}) == []


def test_slippage_snapshot_query_can_target_selected_milestone(monkeypatch):
    import projdash.ui.app as app

    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    controls = {
        "project_id": "project-alpha",
        "as_of": as_of,
    }
    context = {
        "catalog": {
            "milestones": [
                {
                    "milestone_id": "milestone-alpha",
                    "name": "Alpha",
                    "process_symbols": ["A"],
                    "active": True,
                }
            ]
        },
        "terminal_symbols": ["Z"],
    }
    monkeypatch.setitem(
        app.st.session_state,
        "slippage_milestone_id",
        "milestone-alpha",
    )

    query = _schedule_snapshot_query_payload(controls, context)
    commit_payload = _commit_project_state_payload(
        controls,
        terminal_symbols=context["terminal_symbols"],
        milestone=context["catalog"]["milestones"][0],
        committed_at=as_of,
        note="Milestone baseline",
    )

    assert query == {
        "action": "query_schedule_snapshots",
        "project_id": "project-alpha",
        "as_of": as_of,
        "milestone_id": "milestone-alpha",
    }
    assert context["terminal_symbols"] == ["A"]
    assert commit_payload["milestone_id"] == "milestone-alpha"
    assert "terminal_process_symbols" not in commit_payload


def test_slippage_snapshot_query_can_include_completed_background_commit(monkeypatch):
    import projdash.ui.app as app

    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    committed_at = dt.datetime(2026, 5, 13, 12, tzinfo=dt.UTC)
    controls = {
        "project_id": "project-alpha",
        "as_of": as_of,
    }
    context = {
        "catalog": {"milestones": []},
        "schedule_snapshot_query_as_of": committed_at,
    }
    monkeypatch.setitem(app.st.session_state, "slippage_milestone_id", "")

    query = _schedule_snapshot_query_payload(controls, context)

    assert query == {
        "action": "query_schedule_snapshots",
        "project_id": "project-alpha",
        "as_of": committed_at,
        "terminal_process_symbols": [],
    }


def test_schedule_section_loads_schedule_data_without_cost_or_agent_queries():
    service = _RecordingQueryService()
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    controls = {
        "project_id": "project-alpha",
        "as_of": as_of,
        "now": as_of,
    }
    context = _load_context(service, controls)
    service.actions.clear()

    _prepare_context_for_section(service, controls, context, "Schedule")

    assert service.actions == [
        "query_project_catalog",
        "query_process_graph",
        "query_resource_schedule",
        "query_blockers",
    ]
    assert "query_costs" not in service.actions
    assert "query_utilization" not in service.actions
    assert "query_agent_context" not in service.actions


def test_schedule_section_does_not_request_gantt_sensitivity():
    service = _RecordingQueryService()
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    controls = {
        "project_id": "project-alpha",
        "as_of": as_of,
        "now": as_of,
    }
    context = _load_context(service, controls)

    _prepare_context_for_section(service, controls, context, "Schedule")

    graph_query = next(
        payload
        for payload in service.payloads
        if payload["action"] == "query_process_graph"
    )
    mcts_schedule_query = next(
        payload
        for payload in service.payloads
        if payload["action"] == "query_resource_schedule"
        and not payload.get("include_resource_sensitivity")
    )
    assert graph_query["resource_schedule_backend"] == "mcts"
    assert mcts_schedule_query["resource_schedule_backend"] == "mcts"
    assert not [
        payload
        for payload in service.payloads
        if payload["action"] == "query_resource_schedule"
        and payload.get("include_resource_sensitivity")
    ]


def test_resources_section_uses_mcts_for_schedule_and_utilization():
    service = _RecordingQueryService()
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    controls = {
        "project_id": "project-alpha",
        "as_of": as_of,
        "now": as_of,
    }
    context = _load_context(service, controls)

    _prepare_context_for_section(service, controls, context, "Resources")

    schedule_query = next(
        payload
        for payload in service.payloads
        if payload["action"] == "query_resource_schedule"
    )
    utilization_query = next(
        payload
        for payload in service.payloads
        if payload["action"] == "query_utilization"
    )
    assert schedule_query["resource_schedule_backend"] == "mcts"
    assert utilization_query["resource_schedule_backend"] == "mcts"


def test_removed_context_sections_do_not_prepare_queries():
    for section in ("Context", "Dashboard", "History", "Topology"):
        service = _RecordingQueryService()
        as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
        controls = {
            "project_id": "project-alpha",
            "as_of": as_of,
            "now": as_of,
        }
        context = _load_context(service, controls)
        service.actions.clear()

        _prepare_context_for_section(service, controls, context, section)

        assert service.actions == []
        assert context["graph"] is None
        assert context["resource_schedule"] is None
        assert context["agent_context"] is None
        assert context["costs"] is None
        assert context["utilization"] is None


def test_obsolete_sections_removed_from_main_navigation():
    import projdash.ui.app as app

    assert "Blockers" not in app._MAIN_SECTIONS
    assert "Dashboard" not in app._MAIN_SECTIONS
    assert "History" not in app._MAIN_SECTIONS
    assert "Topology" not in app._MAIN_SECTIONS


def test_graph_section_renders_without_edge_table(monkeypatch):
    import projdash.ui.app as app

    graph = {
        "nodes": [
            {
                "process_id": "proc-a",
                "process_symbol": "A",
                "role_requirements": [],
            }
        ],
        "edges": [
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "B",
            }
        ],
    }
    context = {"graph": graph, "full_graph": graph}
    controls = {
        "project_id": "project-alpha",
        "as_of": dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC),
        "now": dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC),
    }
    rendered = {"graph": False}

    def graphviz_chart(*args, **kwargs):
        rendered["graph"] = True

    def dataframe(*args, **kwargs):
        raise AssertionError("Graph section should not render the edge table")

    monkeypatch.setattr(app.st, "graphviz_chart", graphviz_chart)
    monkeypatch.setattr(app.st, "dataframe", dataframe)

    _render_graph(_RecordingQueryService(), controls, context)

    assert rendered["graph"] is True


def test_costs_section_loads_costs_without_graph_schedule_or_agent_queries():
    service = _RecordingQueryService()
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    controls = {
        "project_id": "project-alpha",
        "as_of": as_of,
        "now": as_of,
    }
    context = _load_context(service, controls)
    service.actions.clear()

    _prepare_context_for_section(service, controls, context, "Costs")

    assert service.actions == ["query_costs", "query_utilization"]
    assert context["graph"] is None
    assert context["resource_schedule"] is None
    assert context["agent_context"] is None
