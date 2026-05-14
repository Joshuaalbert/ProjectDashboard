import datetime as dt

from projdash.ui.app import (
    _batch_role_requirements_by_symbol,
    _dependency_set_operations,
    _process_revision_defaults_signature,
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
