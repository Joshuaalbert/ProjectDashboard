import datetime as dt
import json

from projdash.ui.app import (
    _batch_role_requirements_by_symbol,
    _dependency_set_operations,
    _process_revision_defaults_signature,
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
        "A": [{"role_id": "role_eng", "effort_hours": 4.0}],
        "B": [{"role_id": "role_eng", "effort_hours": 6.0}],
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
