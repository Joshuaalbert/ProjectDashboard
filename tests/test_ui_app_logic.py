import datetime as dt
import json

from projdash.ui.app import (
    _batch_role_requirements_by_symbol,
    _blocker_sections,
    _capacity_buckets_for_display,
    _dependency_set_operations,
    _priority_expander_sections,
    _priority_markdown,
    _process_revision_defaults_signature,
    _project_context_markdown,
    _resource_calendar_rules_markdown,
    _schedule_debug_payload,
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
                "hours_until_ls": 1,
                "hours_until_lf": 3,
                "effort_hours": 2,
                "role_id": "role_eng",
            },
            {
                "priority": "P3",
                "priority_rank": 3,
                "process_symbol": "B",
                "process_name": "Build",
                "hours_until_ls": 12,
                "hours_until_lf": 16,
                "effort_hours": 4,
                "role_id": "role_qa",
            },
        ],
        "role_id",
        ["role_qa"],
        id_label="Role",
    )

    assert "### Role `role_qa`" in markdown
    assert "#### P3 | B | Build" in markdown
    assert "Start window: latest start in 0.5 days" in markdown
    assert "Effort: 4 hours" in markdown
    assert "role_eng" not in markdown
    assert "`A`" not in markdown


def test_priority_expander_sections_group_selected_entities():
    sections = _priority_expander_sections(
        [
            {
                "priority": "P1",
                "process_symbol": "A",
                "process_name": "Design",
                "hours_until_ls": -1,
                "effort_hours": 2,
                "resource_id": "res_ada",
            },
            {
                "priority": "P2",
                "process_symbol": "B",
                "process_name": "Build",
                "hours_until_ls": 4,
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
    assert sections[0]["rows"][0]["hours_until_ls"] == -1


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
                    "critical_path": ["run-workshop", "write-paper"],
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
                            "computed_status": "late_risk",
                            "ls_at": "2026-05-16T09:00:00+00:00",
                            "ends_at": "2026-05-20T17:00:00+00:00",
                            "slack_hours": 0,
                            "critical": True,
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
                                    "hours_until_ls": -2,
                                    "effort_hours": 12,
                                    "computed_status": "late_risk",
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
                                    "hours_until_ls": 4,
                                    "effort_hours": 6,
                                    "role_ids": ["role_write"],
                                    "computed_status": "work_now",
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
    assert "- `run-workshop`" in markdown
    assert "## Role Priorities" in markdown
    assert "- **Writing** (`role_write`)" in markdown
    assert (
        "**P1** `write-paper` - Write paper; start window: overdue by 0.08 days; "
        "effort: 12 hours; status: late_risk"
    ) in markdown
    assert "## Resource Priorities" in markdown
    assert "- **Ada** (`res_ada`)" in markdown
    assert "roles: `role_write`" in markdown
    assert "## Schedule Watchlist" in markdown
    assert (
        "- **critical** `write-paper` - Write paper; status: late_risk; "
        "LS: 2026-05-16 09:00 UTC; ends: 2026-05-20 17:00 UTC; "
        "slack: 0 hours; allocation: allocated"
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
