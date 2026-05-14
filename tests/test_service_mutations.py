import copy
import datetime as dt
from collections.abc import Mapping

import pytest

from projdash.service.commands import BatchCommandEnvelope, CommandEnvelope
from projdash.service.queries import QueryEnvelope
from projdash.service.repository import InMemoryProjectRepository
from projdash.service.service import ProjectService

UTC = dt.UTC


def _at(day: int, hour: int = 9) -> dt.datetime:
    return dt.datetime(2026, 5, day, hour, tzinfo=UTC)


def _iso(day: int, hour: int = 9) -> str:
    return _at(day, hour).isoformat()


def _weekday_windows() -> list[dict[str, object]]:
    return [
        {
            "window_id": f"weekday-{weekday}",
            "weekday": weekday,
            "start_local_time": "09:00",
            "end_local_time": "17:00",
            "capacity_hours": 8,
        }
        for weekday in range(5)
    ]


def _handle(
    service: ProjectService,
    command: Mapping[str, object],
    *,
    command_id: str | None = None,
):
    payload: dict[str, object] = {"command": command}
    if command_id is not None:
        payload["command_id"] = command_id
    return service.handle_command(CommandEnvelope.model_validate(payload))


def _handle_batch(service: ProjectService, commands: list[Mapping[str, object]]):
    return service.handle_batch(
        BatchCommandEnvelope.model_validate(
            {
                "commands": [
                    {"command": command}
                    for command in commands
                ],
            }
        )
    )


def _query(service: ProjectService, query: Mapping[str, object]):
    return service.handle_query(QueryEnvelope.model_validate({"query": query}))


def _create_project(service: ProjectService) -> str:
    return _handle(
        service,
        {
            "action": "create_project",
            "name": "Mutation Contract",
            "start_at": _iso(13),
        },
    ).entity_ids["project_id"]


def _create_process(
    service: ProjectService,
    project_id: str,
    *,
    name: str,
    dependencies: list[str] | None = None,
    due_at: str | None = None,
) -> str:
    command: dict[str, object] = {
        "action": "upsert_process_revision",
        "project_id": project_id,
        "name": name,
        "effective_at": _iso(13),
        "duration_business_days": 1,
        "dependencies": dependencies or [],
    }
    if due_at is not None:
        command["due_at"] = due_at
    return _handle(service, command).entity_ids["process_id"]


def _create_legacy_process(
    service: ProjectService,
    project_id: str,
    *,
    name: str,
    duration_business_days: int,
    required_roles: Mapping[str, float],
    dependencies: list[str] | None = None,
) -> str:
    return _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "name": name,
            "effective_at": _iso(13),
            "duration_business_days": duration_business_days,
            "dependencies": dependencies or [],
            "required_roles": dict(required_roles),
        },
    ).entity_ids["process_id"]


def _seed_linear_graph(service: ProjectService) -> tuple[str, str, str, str]:
    project_id = _create_project(service)
    design_id = _create_process(service, project_id, name="Design")
    build_id = _create_process(
        service,
        project_id,
        name="Build",
        dependencies=[design_id],
    )
    ship_id = _create_process(
        service,
        project_id,
        name="Ship",
        dependencies=[build_id],
    )
    return project_id, design_id, build_id, ship_id


def _process_graph(
    service: ProjectService,
    project_id: str,
    *,
    day: int,
    hour: int = 10,
) -> dict[str, object]:
    return _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(day, hour),
            "now": _iso(day, hour),
        },
    ).data


def _edge_pairs(graph: Mapping[str, object]) -> set[tuple[str, str]]:
    return {
        (edge["predecessor_process_id"], edge["successor_process_id"])
        for edge in graph["edges"]
    }


def _node_ids(graph: Mapping[str, object]) -> set[str]:
    return {node["process_id"] for node in graph["nodes"]}


def _repository_snapshot(repository: InMemoryProjectRepository):
    return copy.deepcopy(repository.__dict__)


def _assert_structured_validation_error(result, *, loc_field: str | None = None):
    assert result.ok is False
    assert result.error.code == "validation_error"
    assert result.error.message
    assert result.error.details == {}
    assert result.error.validation_errors
    assert result.warnings == []
    validation_error = result.error.validation_errors[0]
    assert validation_error.loc[0] == "command"
    assert validation_error.type
    if loc_field is not None:
        assert loc_field in validation_error.loc


class NonTransactionalRepository:
    def __init__(self) -> None:
        self.create_project_calls = 0

    def create_project(
        self,
        name: str,
        start_at: dt.datetime,
        default_currency: str = "USD",
    ):
        self.create_project_calls += 1
        raise AssertionError("create_project should not be called")


def test_first_time_command_without_transactional_staging_fails_without_write():
    repository = NonTransactionalRepository()
    service = ProjectService(repository)
    command = {
        "action": "create_project",
        "name": "No Transaction",
        "start_at": _iso(13),
    }
    command_id = "00000000-0000-4000-8000-000000000709"

    failed = _handle(service, command, command_id=command_id)
    replay = _handle(service, command, command_id=command_id)
    conflict = _handle(
        service,
        {
            **command,
            "name": "Different No Transaction",
        },
        command_id=command_id,
    )

    assert failed.ok is False
    assert failed.error.code == "transaction_required"
    assert failed.error.message
    assert failed.error.details == {}
    assert failed.warnings == []
    assert repository.create_project_calls == 0
    assert replay is failed
    assert conflict.ok is False
    assert conflict.error.code == "idempotency_conflict"
    assert repository.create_project_calls == 0


def test_batch_without_transactional_staging_fails_without_write():
    repository = NonTransactionalRepository()
    service = ProjectService(repository)

    results = _handle_batch(
        service,
        [
            {
                "action": "create_project",
                "name": "No Transaction Batch",
                "start_at": _iso(13),
            },
        ],
    )

    assert len(results) == 1
    result = results[0]
    assert result.ok is False
    assert result.error.code == "transaction_required"
    assert result.error.message
    assert result.error.details == {}
    assert result.warnings == []
    assert repository.create_project_calls == 0


def test_failed_mixed_batch_rolls_back_staged_success_without_reporting_ids():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    create_command = {
        "action": "create_project",
        "name": "Rolled Back Project",
        "start_at": _iso(13),
    }
    create_command_id = "00000000-0000-4000-8000-000000000711"
    failing_command_id = "00000000-0000-4000-8000-000000000712"
    batch_payload = {
        "commands": [
            {
                "command_id": create_command_id,
                "command": create_command,
            },
            {
                "command_id": failing_command_id,
                "command": {
                    "action": "create_role",
                    "project_id": "missing-project",
                    "name": "Engineer",
                },
            },
        ],
    }
    before_failure = _repository_snapshot(repository)

    results = service.handle_batch(BatchCommandEnvelope.model_validate(batch_payload))
    replay_results = service.handle_batch(
        BatchCommandEnvelope.model_validate(batch_payload)
    )

    assert len(results) == 2
    rolled_back, failed = results
    assert rolled_back.ok is False
    assert rolled_back.error.code == "batch_rolled_back"
    assert rolled_back.error.details["failed_command_id"] == failing_command_id
    assert "entity_ids" not in rolled_back.model_dump()
    assert failed.ok is False
    assert failed.error.code == "project_not_found"
    assert _repository_snapshot(repository) == before_failure

    replay_rolled_back, replay_failed = replay_results
    assert replay_rolled_back.ok is False
    assert replay_rolled_back.error.code == "batch_rolled_back"
    assert "entity_ids" not in replay_rolled_back.model_dump()
    assert replay_failed.ok is False
    assert replay_failed.error.code == "project_not_found"
    assert _repository_snapshot(repository) == before_failure

    standalone = _handle(
        service,
        create_command,
        command_id=create_command_id,
    )

    assert standalone.ok is True
    assert standalone.entity_ids["project_id"] in repository.projects


def test_failed_first_time_upsert_process_revision_is_atomic_and_replayable():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    command = {
        "action": "upsert_process_revision",
        "project_id": project_id,
        "name": "Orphan Candidate",
        "effective_at": _iso(13),
        "duration_business_days": 1,
        "dependencies": ["missing-dependency"],
    }
    command_id = "00000000-0000-4000-8000-000000000708"
    before_failure = _repository_snapshot(repository)

    failed = _handle(service, command, command_id=command_id)
    after_failure = _repository_snapshot(repository)
    replay = _handle(service, command, command_id=command_id)
    conflict = _handle(
        service,
        {
            **command,
            "name": "Different Orphan Candidate",
        },
        command_id=command_id,
    )

    assert failed.ok is False
    assert failed.error.code == "process_not_found"
    assert failed.warnings == []
    assert after_failure == before_failure
    assert _repository_snapshot(repository) == before_failure
    assert replay is failed
    assert conflict.ok is False
    assert conflict.error.code == "idempotency_conflict"
    assert conflict.warnings == []
    assert repository.processes == {}
    assert repository.process_ids_by_project.get(project_id, []) == []
    assert all(
        process.symbol != "orphan-candidate"
        for process in repository.processes.values()
    )


def test_project_due_history_distinguishes_explicit_and_derived_totals():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _create_process(service, project_id, name="Build API")

    set_project_due = _handle(
        service,
        {
            "action": "set_project_due_at",
            "project_id": project_id,
            "due_at": _iso(30, 17),
            "edit_at": _iso(13, 10),
        },
        command_id="00000000-0000-4000-8000-000000000101",
    )
    _handle(
        service,
        {
            "action": "set_process_due_at",
            "project_id": project_id,
            "process_id": process_id,
            "due_at": _iso(24, 17),
            "edit_at": _iso(13, 11),
        },
    )
    clear_project_due = _handle(
        service,
        {
            "action": "clear_project_due_at",
            "project_id": project_id,
            "edit_at": _iso(14, 10),
        },
    )

    history = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(14, 12),
            "include_project_total": True,
        },
    ).data

    assert set_project_due.entity_ids["due_history_event_id"]
    assert clear_project_due.entity_ids["due_history_event_id"]
    assert history["current_project_due_at"] is None
    assert history["derived_project_due_at"] == _iso(24, 17)
    assert [
        event["mutation_action"] for event in history["project_total_events"]
    ] == [
        "set_project_due_at",
        "derived_project_due_at_changed",
        "clear_project_due_at",
    ]
    assert history["project_total_events"][0]["before_due_at"] is None
    assert history["project_total_events"][0]["after_due_at"] == _iso(30, 17)
    assert history["project_total_events"][2]["after_due_at"] is None
    assert all(
        dt.datetime.fromisoformat(event["edit_at"]).tzinfo is not None
        for event in history["project_total_events"]
    )


def test_set_project_due_at_exact_command_replay_reuses_result_without_duplicate_events():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    command = {
        "action": "set_project_due_at",
        "project_id": project_id,
        "due_at": _iso(30, 17),
        "edit_at": _iso(13, 10),
    }

    first = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000701",
    )
    history_after_first = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(13, 11),
            "include_project_total": True,
        },
    ).data
    replay = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000701",
    )
    history_after_replay = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(13, 11),
            "include_project_total": True,
        },
    ).data

    assert replay.command_id == first.command_id
    assert replay.entity_ids == first.entity_ids
    assert history_after_replay == history_after_first
    assert [
        event["mutation_action"]
        for event in history_after_replay["project_total_events"]
    ] == ["set_project_due_at"]


def test_clear_project_due_at_exact_command_replay_reuses_result_without_duplicate_events():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    _handle(
        service,
        {
            "action": "set_project_due_at",
            "project_id": project_id,
            "due_at": _iso(30, 17),
            "edit_at": _iso(13, 10),
        },
    )
    command = {
        "action": "clear_project_due_at",
        "project_id": project_id,
        "edit_at": _iso(14, 10),
    }

    first = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000702",
    )
    history_after_first = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(14, 11),
            "include_project_total": True,
        },
    ).data
    replay = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000702",
    )
    history_after_replay = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(14, 11),
            "include_project_total": True,
        },
    ).data

    assert replay.command_id == first.command_id
    assert replay.entity_ids == first.entity_ids
    assert history_after_replay == history_after_first
    assert [
        event["mutation_action"]
        for event in history_after_replay["project_total_events"]
    ] == ["set_project_due_at", "clear_project_due_at"]


def test_set_process_due_at_exact_command_replay_reuses_result_without_duplicate_events():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _create_process(service, project_id, name="Build API")
    command = {
        "action": "set_process_due_at",
        "project_id": project_id,
        "process_id": process_id,
        "due_at": _iso(24, 17),
        "edit_at": _iso(13, 11),
    }

    first = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000703",
    )
    history_after_first = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "include_project_total": True,
        },
    ).data
    replay = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000703",
    )
    history_after_replay = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": _iso(13, 12),
            "include_project_total": True,
        },
    ).data

    assert replay.command_id == first.command_id
    assert replay.entity_ids == first.entity_ids
    assert history_after_replay == history_after_first
    assert [event["process_id"] for event in history_after_replay["process_events"]] == [
        process_id
    ]
    assert [
        event["mutation_action"]
        for event in history_after_replay["project_total_events"]
    ] == ["derived_project_due_at_changed"]


def test_process_lifecycle_status_transitions_and_finished_at_semantics():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _, process_id, _ = _seed_linear_graph(service)

    done = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "done",
            "edit_at": _iso(15, 16),
        },
    )
    inferred_done_graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(15, 17),
            "now": _iso(15, 17),
            "include_resource_fields": True,
            "horizon_starts_at": _iso(13, 0),
            "horizon_ends_at": _iso(25, 0),
        },
    ).data
    inferred_done_node = next(
        node
        for node in inferred_done_graph["nodes"]
        if node["process_id"] == process_id
    )

    explicit_done = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "done",
            "edit_at": _iso(16, 12),
            "finished_at": _iso(15, 15),
        },
    )
    explicit_done_graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(16, 13),
            "now": _iso(16, 13),
            "include_resource_fields": True,
            "horizon_starts_at": _iso(13, 0),
            "horizon_ends_at": _iso(25, 0),
        },
    ).data
    explicit_done_node = next(
        node
        for node in explicit_done_graph["nodes"]
        if node["process_id"] == process_id
    )

    reopened = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "in_progress",
            "edit_at": _iso(17, 9),
        },
    )
    reopened_graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(17, 10),
            "now": _iso(17, 10),
            "include_resource_fields": True,
            "horizon_starts_at": _iso(13, 0),
            "horizon_ends_at": _iso(25, 0),
        },
    ).data
    reopened_node = next(
        node for node in reopened_graph["nodes"] if node["process_id"] == process_id
    )
    non_done_finished_at = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "in_progress",
            "edit_at": _iso(17, 11),
            "finished_at": _iso(17, 10),
        },
    )
    cancel_non_done_finished_at = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "canceled",
            "edit_at": _iso(17, 12),
            "finished_at": _iso(17, 10),
        },
    )
    final_done = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "done",
            "edit_at": _iso(17, 14),
            "finished_at": _iso(17, 13),
        },
    )
    canceled_done = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "canceled",
            "edit_at": _iso(18, 9),
        },
    )
    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(18, 10),
            "now": _iso(18, 10),
            "include_resource_fields": True,
            "horizon_starts_at": _iso(13, 0),
            "horizon_ends_at": _iso(25, 0),
        },
    ).data
    node = next(node for node in graph["nodes"] if node["process_id"] == process_id)

    assert done.entity_ids["lifecycle_event_id"]
    assert explicit_done.entity_ids["lifecycle_event_id"]
    assert reopened.entity_ids["lifecycle_event_id"]
    assert final_done.entity_ids["lifecycle_event_id"]
    assert canceled_done.entity_ids["lifecycle_event_id"]
    assert inferred_done_node["status"] == "done"
    assert inferred_done_node["finished_at"] == _iso(15, 16)
    assert explicit_done_node["status"] == "done"
    assert explicit_done_node["finished_at"] == _iso(15, 15)
    assert reopened_node["status"] == "in_progress"
    assert reopened_node["finished_at"] is None
    assert non_done_finished_at.ok is False
    assert non_done_finished_at.error.code == "validation_error"
    assert cancel_non_done_finished_at.ok is False
    assert cancel_non_done_finished_at.error.code == "validation_error"
    assert node["status"] == "canceled"
    assert node["finished_at"] == _iso(17, 13)
    assert "ends_at" not in node
    assert node["resource_aware"]["ends_at"] is not None
    assert node["finished_at"] != node["resource_aware"]["ends_at"]


def test_started_process_anchors_dependency_schedule_windows():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _design_id, build_id, _ship_id = _seed_linear_graph(service)
    started_at = _iso(14, 11)

    started = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": build_id,
            "status": "in_progress",
            "edit_at": _iso(14, 12),
            "started_at": started_at,
        },
    )
    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 13),
            "now": _iso(14, 13),
        },
    ).data
    node = next(node for node in graph["nodes"] if node["process_id"] == build_id)

    assert started.ok is True
    assert node["status"] == "in_progress"
    assert node["started_at"] == started_at
    assert node["dependency_only"]["es_at"] == started_at
    assert node["dependency_only"]["ls_at"] == started_at
    assert node["dependency_only"]["slack_hours"] == 0


def test_commit_project_state_records_slippage_points_by_terminal_scope():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _design_id, build_id, _ship_id = _seed_linear_graph(service)

    first = _handle(
        service,
        {
            "action": "commit_project_state",
            "project_id": project_id,
            "committed_at": _iso(13, 12),
            "note": "Initial plan",
        },
    )
    _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": build_id,
            "status": "in_progress",
            "edit_at": _iso(14, 12),
            "started_at": _iso(14, 11),
        },
    )
    second = _handle(
        service,
        {
            "action": "commit_project_state",
            "project_id": project_id,
            "committed_at": _iso(14, 12),
            "note": "Build started late",
        },
    )
    terminal = _handle(
        service,
        {
            "action": "commit_project_state",
            "project_id": project_id,
            "committed_at": _iso(14, 13),
            "terminal_process_symbols": ["build"],
            "note": "Build-only target",
        },
    )
    project_snapshots = _query(
        service,
        {
            "action": "query_schedule_snapshots",
            "project_id": project_id,
            "as_of": _iso(14, 14),
        },
    ).data["snapshots"]
    build_snapshots = _query(
        service,
        {
            "action": "query_schedule_snapshots",
            "project_id": project_id,
            "as_of": _iso(14, 14),
            "terminal_process_symbols": ["build"],
        },
    ).data["snapshots"]

    assert first.ok is True
    assert second.ok is True
    assert terminal.ok is True
    assert [row["completion_at"] for row in project_snapshots] == [
        _iso(13, 9),
        _iso(14, 11),
    ]
    assert project_snapshots[0]["terminal_process_symbols"] == []
    assert project_snapshots[1]["note"] == "Build started late"
    assert build_snapshots[0]["completion_at"] == _iso(14, 11)
    assert build_snapshots[0]["terminal_process_symbols"] == ["build"]


def test_schedule_snapshot_terminal_symbols_are_set_idempotent():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    alpha_id = _create_process(service, project_id, name="Alpha")
    beta_id = _create_process(service, project_id, name="Beta", dependencies=[alpha_id])
    _create_process(service, project_id, name="Gamma", dependencies=[beta_id])

    first = _handle(
        service,
        {
            "action": "commit_project_state",
            "project_id": project_id,
            "committed_at": _iso(13, 12),
            "terminal_process_symbols": ["gamma", "beta"],
        },
    )
    second = _handle(
        service,
        {
            "action": "commit_project_state",
            "project_id": project_id,
            "committed_at": _iso(13, 12),
            "terminal_process_symbols": ["beta", "gamma"],
        },
    )
    snapshots = _query(
        service,
        {
            "action": "query_schedule_snapshots",
            "project_id": project_id,
            "as_of": _iso(13, 13),
            "terminal_process_symbols": ["gamma", "beta"],
        },
    ).data["snapshots"]

    assert first.ok is True
    assert second.ok is True
    assert first.entity_ids["schedule_snapshot_id"] == (
        second.entity_ids["schedule_snapshot_id"]
    )
    assert len(snapshots) == 1
    assert snapshots[0]["terminal_process_symbols"] == ["beta", "gamma"]


def test_commit_project_state_extends_horizon_to_sparse_resource_capacity():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    role_id = _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-sparse",
            "name": "Sparse Specialist",
        },
    ).entity_ids["role_id"]
    calendar_id = _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-sparse",
            "name": "Sparse Fridays",
            "timezone": "UTC",
            "weekly_windows": [
                {
                    "window_id": "friday-one-hour",
                    "weekday": 4,
                    "start_local_time": "09:00",
                    "end_local_time": "10:00",
                    "capacity_hours": 1,
                },
            ],
        },
    ).entity_ids["calendar_id"]
    _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-sparse",
            "name": "Sparse Resource",
            "role_ids": [role_id],
            "calendar_id": calendar_id,
            "available_from_at": _iso(13),
            "cost_rate": "100.00",
            "cost_unit": "hour",
        },
    )
    _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "process_id": "process-sparse",
            "name": "Sparse Work",
            "effective_at": _iso(13),
            "duration_business_days": 1,
            "role_requirements": [
                {
                    "requirement_id": "req-sparse",
                    "role_id": role_id,
                    "effort_hours": 40,
                },
            ],
        },
    )

    committed = _handle(
        service,
        {
            "action": "commit_project_state",
            "project_id": project_id,
            "committed_at": _iso(13, 12),
        },
    )
    snapshots = _query(
        service,
        {
            "action": "query_schedule_snapshots",
            "project_id": project_id,
            "as_of": _iso(13, 13),
        },
    ).data["snapshots"]

    assert committed.ok is True
    assert snapshots[0]["completion_at"] is not None
    assert snapshots[0]["unallocated_count"] == 0
    assert snapshots[0]["horizon_ends_at"] > "2026-06-30T00:00:00+00:00"


def test_done_terminal_snapshot_uses_actual_finished_at():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _design_id, build_id, _ship_id = _seed_linear_graph(service)
    _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": build_id,
            "status": "done",
            "edit_at": _iso(14, 15),
            "finished_at": _iso(14, 14),
        },
    )

    result = _handle(
        service,
        {
            "action": "commit_project_state",
            "project_id": project_id,
            "committed_at": _iso(14, 16),
            "terminal_process_symbols": ["build"],
        },
    )
    snapshots = _query(
        service,
        {
            "action": "query_schedule_snapshots",
            "project_id": project_id,
            "as_of": _iso(14, 17),
            "terminal_process_symbols": ["build"],
        },
    ).data["snapshots"]

    assert result.ok is True
    assert snapshots[0]["completion_at"] == _iso(14, 14)


def test_cancel_unfinished_process_preserves_null_finished_at_without_inference():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _, process_id, _ = _seed_linear_graph(service)

    _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "in_progress",
            "edit_at": _iso(14, 9),
        },
    )
    canceled = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "canceled",
            "edit_at": _iso(15, 9),
        },
    )
    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(15, 10),
            "now": _iso(15, 10),
        },
    ).data
    node = next(node for node in graph["nodes"] if node["process_id"] == process_id)

    assert canceled.entity_ids["lifecycle_event_id"]
    assert node["status"] == "canceled"
    assert node["finished_at"] is None


def test_cancel_done_process_with_same_finished_at_is_accepted_and_preserved():
    service = ProjectService(InMemoryProjectRepository())
    project_id, _, process_id, _ = _seed_linear_graph(service)

    _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "done",
            "edit_at": _iso(15, 16),
            "finished_at": _iso(15, 15),
        },
    )
    canceled = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "canceled",
            "edit_at": _iso(16, 9),
            "finished_at": _iso(15, 15),
        },
    )
    graph = _process_graph(service, project_id, day=16, hour=10)
    node = next(node for node in graph["nodes"] if node["process_id"] == process_id)

    assert canceled.ok is True
    assert canceled.entity_ids["lifecycle_event_id"]
    assert node["status"] == "canceled"
    assert node["finished_at"] == _iso(15, 15)


def test_cancel_done_process_with_different_finished_at_rejects_without_write():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, _, process_id, _ = _seed_linear_graph(service)

    _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "done",
            "edit_at": _iso(15, 16),
            "finished_at": _iso(15, 15),
        },
    )
    before_rejection = _repository_snapshot(repository)

    rejected = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "canceled",
            "edit_at": _iso(16, 9),
            "finished_at": _iso(15, 14),
        },
    )
    graph = _process_graph(service, project_id, day=16, hour=10)
    node = next(node for node in graph["nodes"] if node["process_id"] == process_id)

    assert rejected.ok is False
    assert rejected.error.code == "validation_error"
    assert rejected.warnings == []
    assert _repository_snapshot(repository) == before_rejection
    assert node["status"] == "done"
    assert node["finished_at"] == _iso(15, 15)


def test_blockers_add_resolve_derivation_and_resolved_history_retention():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _create_process(service, project_id, name="Wait for Review")

    blocker_id = _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": process_id,
            "blocker_id": "blocker-review",
            "summary": "Reviewer unavailable",
            "details": "Primary reviewer is out this week.",
            "severity": "blocking",
            "created_at": _iso(14, 9),
        },
    ).entity_ids["blocker_id"]

    unresolved = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(14, 12),
        },
    ).data
    _handle(
        service,
        {
            "action": "resolve_blocker",
            "project_id": project_id,
            "blocker_id": blocker_id,
            "resolved_at": _iso(15, 10),
            "resolution": "Review reassigned.",
        },
    )
    active_after_resolution = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(16, 9),
        },
    ).data
    history = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(16, 9),
            "include_resolved": True,
        },
    ).data

    assert unresolved["blocked_process_ids"] == [process_id]
    assert unresolved["blockers"][0]["is_resolved_as_of"] is False
    assert unresolved["blockers"][0]["is_blocking_as_of"] is True
    assert active_after_resolution["blockers"] == []
    assert active_after_resolution["blocked_process_ids"] == []
    assert history["blockers"][0]["blocker_id"] == blocker_id
    assert history["blockers"][0]["resolved_at"] == _iso(15, 10)
    assert history["blockers"][0]["is_resolved_as_of"] is True
    assert history["blockers"][0]["is_blocking_as_of"] is False


@pytest.mark.parametrize("severity", ["warning", "info"])
def test_non_blocking_blocker_severities_do_not_derive_blocked_state(
    severity: str,
):
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _create_process(service, project_id, name=f"{severity} Review")

    _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": process_id,
            "blocker_id": f"blocker-{severity}",
            "summary": f"{severity.title()} note",
            "severity": severity,
            "created_at": _iso(14, 9),
        },
    )

    blockers = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(14, 12),
        },
    ).data
    graph = _process_graph(service, project_id, day=14, hour=12)
    node = next(node for node in graph["nodes"] if node["process_id"] == process_id)

    assert blockers["blocked_process_ids"] == []
    assert blockers["blockers"][0]["severity"] == severity
    assert blockers["blockers"][0]["is_resolved_as_of"] is False
    assert blockers["blockers"][0]["is_blocking_as_of"] is False
    assert node["computed_status"] != "blocked"


@pytest.mark.parametrize(
    ("status", "expected_computed_status"),
    [("done", "complete"), ("canceled", "canceled")],
)
def test_unresolved_blocking_blockers_do_not_block_done_or_canceled_processes(
    status: str,
    expected_computed_status: str,
):
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _create_process(service, project_id, name=f"{status} Review")

    _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": process_id,
            "blocker_id": f"blocker-{status}",
            "summary": "External approval pending",
            "severity": "blocking",
            "created_at": _iso(14, 9),
        },
    )
    _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": status,
            "edit_at": _iso(14, 10),
        },
    )

    blockers = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(14, 12),
        },
    ).data
    graph = _process_graph(service, project_id, day=14, hour=12)
    node = next(node for node in graph["nodes"] if node["process_id"] == process_id)

    assert blockers["blocked_process_ids"] == []
    assert blockers["blockers"][0]["blocker_id"] == f"blocker-{status}"
    assert blockers["blockers"][0]["is_resolved_as_of"] is False
    assert blockers["blockers"][0]["is_blocking_as_of"] is False
    assert node["status"] == status
    assert node["computed_status"] == expected_computed_status


def test_blocker_as_of_controls_created_and_resolved_effective_state():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _create_process(service, project_id, name="Wait for Legal")

    blocker_id = _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": process_id,
            "blocker_id": "blocker-legal",
            "summary": "Legal review pending",
            "severity": "blocking",
            "created_at": _iso(14, 9),
        },
    ).entity_ids["blocker_id"]
    before_created = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(14, 8),
        },
    ).data
    graph_before_created = _process_graph(service, project_id, day=14, hour=8)
    at_created = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(14, 9),
        },
    ).data
    graph_at_created = _process_graph(service, project_id, day=14, hour=9)

    _handle(
        service,
        {
            "action": "resolve_blocker",
            "project_id": project_id,
            "blocker_id": blocker_id,
            "resolved_at": _iso(15, 10),
            "resolution": "Legal approved.",
        },
    )
    historical_unresolved = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(14, 12),
        },
    ).data
    graph_historical_unresolved = _process_graph(service, project_id, day=14, hour=12)
    after_resolved = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(15, 11),
        },
    ).data
    at_resolved = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(15, 10),
        },
    ).data
    graph_at_resolved = _process_graph(service, project_id, day=15, hour=10)

    node_before_created = next(
        node
        for node in graph_before_created["nodes"]
        if node["process_id"] == process_id
    )
    node_at_created = next(
        node for node in graph_at_created["nodes"] if node["process_id"] == process_id
    )
    node_historical_unresolved = next(
        node
        for node in graph_historical_unresolved["nodes"]
        if node["process_id"] == process_id
    )
    node_at_resolved = next(
        node
        for node in graph_at_resolved["nodes"]
        if node["process_id"] == process_id
    )

    assert before_created["blockers"] == []
    assert before_created["blocked_process_ids"] == []
    assert node_before_created["computed_status"] != "blocked"
    assert at_created["blocked_process_ids"] == [process_id]
    assert at_created["blockers"][0]["blocker_id"] == blocker_id
    assert at_created["blockers"][0]["is_resolved_as_of"] is False
    assert at_created["blockers"][0]["is_blocking_as_of"] is True
    assert node_at_created["computed_status"] == "blocked"
    assert historical_unresolved["blocked_process_ids"] == [process_id]
    assert historical_unresolved["blockers"][0]["blocker_id"] == blocker_id
    assert historical_unresolved["blockers"][0]["is_resolved_as_of"] is False
    assert historical_unresolved["blockers"][0]["is_blocking_as_of"] is True
    assert node_historical_unresolved["computed_status"] == "blocked"
    assert at_resolved["blockers"] == []
    assert at_resolved["blocked_process_ids"] == []
    assert node_at_resolved["computed_status"] != "blocked"
    assert after_resolved["blockers"] == []
    assert after_resolved["blocked_process_ids"] == []


def test_add_blocker_exact_command_replay_reuses_result_without_duplicate_facts():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _create_process(service, project_id, name="Wait for Review")
    command = {
        "action": "add_blocker",
        "project_id": project_id,
        "process_id": process_id,
        "blocker_id": "blocker-review-replay",
        "summary": "Reviewer unavailable",
        "details": "Primary reviewer is out this week.",
        "severity": "blocking",
        "created_at": _iso(14, 9),
    }

    first = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000704",
    )
    blockers_after_first = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(14, 12),
            "include_resolved": True,
        },
    ).data
    replay = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000704",
    )
    blockers_after_replay = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(14, 12),
            "include_resolved": True,
        },
    ).data

    assert replay.command_id == first.command_id
    assert replay.entity_ids == first.entity_ids
    assert blockers_after_replay == blockers_after_first
    assert [blocker["blocker_id"] for blocker in blockers_after_replay["blockers"]] == [
        "blocker-review-replay"
    ]
    assert blockers_after_replay["blocked_process_ids"] == [process_id]


@pytest.mark.parametrize(
    "repeat_command_id",
    [
        pytest.param(None, id="omitted_command_id"),
        pytest.param(
            "00000000-0000-4000-8000-000000000706",
            id="fresh_command_id",
        ),
    ],
)
def test_resolve_blocker_identical_repeat_is_idempotent_without_duplicate_facts(
    repeat_command_id: str | None,
):
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _create_process(service, project_id, name="Wait for Review")
    blocker_id = _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": process_id,
            "blocker_id": "blocker-repeat-resolution",
            "summary": "Reviewer unavailable",
            "severity": "blocking",
            "created_at": _iso(14, 9),
        },
    ).entity_ids["blocker_id"]
    command = {
        "action": "resolve_blocker",
        "project_id": project_id,
        "blocker_id": blocker_id,
        "resolved_at": _iso(15, 10),
        "resolution": "Review reassigned.",
    }

    first = _handle(service, command)
    blockers_after_first = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(16, 9),
            "include_resolved": True,
        },
    ).data
    repeated = _handle(service, command, command_id=repeat_command_id)
    blockers_after_repeat = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(16, 9),
            "include_resolved": True,
        },
    ).data

    assert repeated.entity_ids == first.entity_ids
    assert blockers_after_repeat == blockers_after_first
    assert [
        blocker["blocker_id"] for blocker in blockers_after_repeat["blockers"]
    ] == [blocker_id]
    assert blockers_after_repeat["blockers"][0]["resolved_at"] == _iso(15, 10)
    assert blockers_after_repeat["blockers"][0]["resolution"] == (
        "Review reassigned."
    )
    assert blockers_after_repeat["blocked_process_ids"] == []


@pytest.mark.parametrize(
    "replay_override",
    [
        {"resolved_at": _iso(15, 11)},
        {"resolution": "Review no longer required."},
    ],
)
def test_resolve_blocker_replay_conflict_leaves_resolved_fact_unchanged(
    replay_override: Mapping[str, object],
):
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _create_process(service, project_id, name="Wait for Review")
    blocker_id = _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": process_id,
            "blocker_id": "blocker-resolution-conflict",
            "summary": "Reviewer unavailable",
            "severity": "blocking",
            "created_at": _iso(14, 9),
        },
    ).entity_ids["blocker_id"]
    command = {
        "action": "resolve_blocker",
        "project_id": project_id,
        "blocker_id": blocker_id,
        "resolved_at": _iso(15, 10),
        "resolution": "Review reassigned.",
    }

    _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000705",
    )
    blockers_after_first = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(16, 9),
            "include_resolved": True,
        },
    ).data
    replay_payload = {**command, **replay_override}
    replay_conflict = _handle(
        service,
        replay_payload,
        command_id="00000000-0000-4000-8000-000000000705",
    )
    blockers_after_conflict = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(16, 9),
            "include_resolved": True,
        },
    ).data

    assert replay_conflict.ok is False
    assert replay_conflict.error.code == "idempotency_conflict"
    assert replay_conflict.warnings == []
    assert blockers_after_conflict == blockers_after_first
    assert [
        blocker["blocker_id"] for blocker in blockers_after_conflict["blockers"]
    ] == [blocker_id]
    assert blockers_after_conflict["blockers"][0]["resolved_at"] == _iso(15, 10)
    assert blockers_after_conflict["blockers"][0]["resolution"] == (
        "Review reassigned."
    )


@pytest.mark.parametrize(
    "repeat_override",
    [
        pytest.param({"resolved_at": _iso(15, 11)}, id="different_resolved_at"),
        pytest.param(
            {"resolution": "Review no longer required."},
            id="different_resolution",
        ),
    ],
)
@pytest.mark.parametrize(
    "repeat_command_id",
    [
        pytest.param(None, id="omitted_command_id"),
        pytest.param(
            "00000000-0000-4000-8000-000000000706",
            id="fresh_command_id",
        ),
    ],
)
def test_resolve_blocker_repeated_resolve_conflict_preserves_original_fact(
    repeat_override: Mapping[str, object],
    repeat_command_id: str | None,
):
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _create_process(service, project_id, name="Wait for Review")
    blocker_id = _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": process_id,
            "blocker_id": "blocker-repeated-resolution-conflict",
            "summary": "Reviewer unavailable",
            "severity": "blocking",
            "created_at": _iso(14, 9),
        },
    ).entity_ids["blocker_id"]
    command = {
        "action": "resolve_blocker",
        "project_id": project_id,
        "blocker_id": blocker_id,
        "resolved_at": _iso(15, 10),
        "resolution": "Review reassigned.",
    }

    first = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000707",
    )
    blockers_after_first = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(16, 9),
            "include_resolved": True,
        },
    ).data
    repeat_result = _handle(
        service,
        {**command, **repeat_override},
        command_id=repeat_command_id,
    )
    blockers_after_repeat = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(16, 9),
            "include_resolved": True,
        },
    ).data

    assert first.ok is True
    assert repeat_result.ok is False
    assert repeat_result.warnings == []
    assert blockers_after_repeat == blockers_after_first
    assert [
        blocker["blocker_id"] for blocker in blockers_after_repeat["blockers"]
    ] == [blocker_id]
    assert blockers_after_repeat["blockers"][0]["resolved_at"] == _iso(15, 10)
    assert blockers_after_repeat["blockers"][0]["resolution"] == "Review reassigned."


def test_rename_process_alias_uniqueness_resolution_and_retired_alias_visibility():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _create_process(service, project_id, name="Design")
    _create_process(service, project_id, name="Build")

    rename = _handle(
        service,
        {
            "action": "rename_process",
            "project_id": project_id,
            "process_id": process_id,
            "new_symbol": "architecture",
            "edit_at": _iso(14, 9),
            "keep_old_symbol_as_alias": True,
        },
    )
    _handle(
        service,
        {
            "action": "add_process_aliases",
            "project_id": project_id,
            "process_symbol": "architecture",
            "aliases": ["arch", "solution-design"],
            "edit_at": _iso(14, 10),
        },
    )
    collision = _handle(
        service,
        {
            "action": "add_process_aliases",
            "project_id": project_id,
            "process_symbol": "architecture",
            "aliases": ["build"],
            "edit_at": _iso(14, 11),
        },
    )
    _handle(
        service,
        {
            "action": "replace_process_with_subgraph",
            "project_id": project_id,
            "process_symbol": "architecture",
            "edit_at": _iso(15, 9),
            "processes": [
                {
                    "process_symbol": "schema",
                    "name": "Schema",
                    "duration_hours": 4,
                }
            ],
            "dependencies": [],
            "root_symbols": ["schema"],
            "leaf_symbols": ["schema"],
        },
    )
    active_resolution = _handle(
        service,
        {
            "action": "set_process_due_at",
            "project_id": project_id,
            "process_symbol": "design",
            "due_at": _iso(20, 17),
            "edit_at": _iso(15, 10),
        },
    )
    retired_alias = _handle(
        service,
        {
            "action": "set_process_due_at",
            "project_id": project_id,
            "process_symbol": "arch",
            "due_at": _iso(20, 17),
            "edit_at": _iso(15, 11),
        },
    )

    assert rename.entity_ids["process_id"] == process_id
    assert collision.ok is False
    assert collision.error.code == "validation_error"
    assert active_resolution.entity_ids["process_id"] != process_id
    assert retired_alias.ok is False
    assert retired_alias.error.code in {"not_found", "ambiguous_process_symbol"}


def test_auto_generated_process_symbols_skip_active_aliases():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    design_id = _create_process(service, project_id, name="Design")
    build_id = _create_process(service, project_id, name="Build", dependencies=[design_id])
    _create_process(service, project_id, name="Owner")
    _handle(
        service,
        {
            "action": "add_process_aliases",
            "project_id": project_id,
            "process_symbol": "owner",
            "aliases": ["implementation"],
            "edit_at": _iso(14, 9),
        },
    )
    _handle(
        service,
        {
            "action": "rename_process",
            "project_id": project_id,
            "process_id": design_id,
            "new_symbol": "architecture",
            "edit_at": _iso(14, 10),
            "keep_old_symbol_as_alias": True,
        },
    )

    created = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_id,
            "name": "Design",
            "effective_at": _iso(14, 11),
            "duration_business_days": 1,
        },
    )
    collapsed = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(15, 9),
            "process_symbols": ["architecture", "build"],
            "new_process": {"name": "Implementation"},
        },
    )
    graph = _process_graph(service, project_id, day=15, hour=10)
    symbols_by_id = {
        node["process_id"]: node["process_symbol"]
        for node in graph["nodes"]
    }

    assert created.entity_ids["process_id"] in symbols_by_id
    assert symbols_by_id[created.entity_ids["process_id"]] == "design1"
    assert symbols_by_id[collapsed.entity_ids["process_id"]] == "implementation1"
    assert build_id in collapsed.entity_ids["retired_process_ids"]


def test_batch_role_requirement_coalescing_noops_replay_and_operation_results():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    process_id = _create_process(service, project_id, name="Build API")
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )

    duplicate_conflict = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 8),
            "operations": [
                {
                    "operation_id": "op-add-conflict",
                    "action": "add_role_requirement",
                    "process_id": process_id,
                    "requirement": {
                        "requirement_id": "req-conflict",
                        "role_id": "role-engineer",
                        "effort_hours": 4,
                    },
                },
                {
                    "operation_id": "op-add-conflict-again",
                    "action": "add_role_requirement",
                    "process_id": process_id,
                    "requirement": {
                        "requirement_id": "req-conflict",
                        "role_id": "role-engineer",
                        "effort_hours": 5,
                    },
                },
            ],
        },
    )
    after_duplicate_conflict = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 8),
            "now": _iso(14, 8),
        },
    ).data
    command = {
        "action": "batch_update_process_graph",
        "project_id": project_id,
        "edit_at": _iso(14, 9),
        "operations": [
            {
                "operation_id": "op-remove-absent",
                "action": "remove_role_requirement",
                "process_id": process_id,
                "requirement_id": "req-absent",
            },
            {
                "operation_id": "op-add-eng",
                "action": "add_role_requirement",
                "process_id": process_id,
                "requirement": {
                    "requirement_id": "req-eng",
                    "role_id": "role-engineer",
                    "effort_hours": 8,
                },
            },
            {
                "operation_id": "op-duplicate-eng",
                "action": "add_role_requirement",
                "process_id": process_id,
                "requirement": {
                    "requirement_id": "req-eng",
                    "role_id": "role-engineer",
                    "effort_hours": 8,
                },
            },
            {
                "operation_id": "op-add-temp",
                "action": "add_role_requirement",
                "process_id": process_id,
                "requirement": {
                    "requirement_id": "req-temp",
                    "role_id": "role-engineer",
                    "effort_hours": 2,
                },
            },
            {
                "operation_id": "op-remove-temp",
                "action": "remove_role_requirement",
                "process_id": process_id,
                "requirement_id": "req-temp",
            },
        ],
    }

    first = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000201",
    )
    after_first = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 10),
            "now": _iso(14, 10),
        },
    ).data
    replay = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000201",
    )
    operation_results = first.entity_ids["operation_ids"]
    final_revision_id = first.entity_ids["revision_ids"][0]
    replay_graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 11),
            "now": _iso(14, 11),
        },
    ).data
    cancel_remove_readd = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 12),
            "operations": [
                {
                    "operation_id": "op-remove-eng",
                    "action": "remove_role_requirement",
                    "process_id": process_id,
                    "requirement_id": "req-eng",
                },
                {
                    "operation_id": "op-readd-eng",
                    "action": "add_role_requirement",
                    "process_id": process_id,
                    "requirement": {
                        "requirement_id": "req-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 8,
                    },
                },
            ],
        },
        command_id="00000000-0000-4000-8000-000000000202",
    )
    changed_readd = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 13),
            "operations": [
                {
                    "operation_id": "op-remove-eng-change",
                    "action": "remove_role_requirement",
                    "process_id": process_id,
                    "requirement_id": "req-eng",
                },
                {
                    "operation_id": "op-readd-eng-changed",
                    "action": "add_role_requirement",
                    "process_id": process_id,
                    "requirement": {
                        "requirement_id": "req-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 10,
                    },
                },
            ],
        },
    )
    final_graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 14),
            "now": _iso(14, 14),
        },
    ).data
    failed_edit_graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 13),
            "now": _iso(14, 13),
        },
    ).data
    after_first_node = next(
        node for node in after_first["nodes"] if node["process_id"] == process_id
    )
    replay_node = next(
        node for node in replay_graph["nodes"] if node["process_id"] == process_id
    )
    failed_edit_node = next(
        node
        for node in failed_edit_graph["nodes"]
        if node["process_id"] == process_id
    )
    final_node = next(
        node for node in final_graph["nodes"] if node["process_id"] == process_id
    )
    duplicate_conflict_node = next(
        node
        for node in after_duplicate_conflict["nodes"]
        if node["process_id"] == process_id
    )

    assert duplicate_conflict.ok is False
    assert duplicate_conflict.error.code == "validation_error"
    assert duplicate_conflict.error.validation_errors[0].loc[:3] == [
        "command",
        "operations",
        1,
    ]
    assert duplicate_conflict_node["role_requirements"] == []
    assert replay.entity_ids == first.entity_ids
    assert first.entity_ids["process_ids"] == [process_id]
    assert first.entity_ids["revision_ids"] == [final_revision_id]
    assert first.entity_ids["requirement_ids"] == ["req-eng"]
    assert after_first_node["role_requirements"] == [
        {
            "requirement_id": "req-eng",
            "role_id": "role-engineer",
            "effort_hours": 8,
            "required_resource_count": 1,
            "allocation_policy": "split_allowed",
            "min_allocation_hours_per_day": None,
            "max_allocation_hours_per_day": None,
        }
    ]
    assert replay_node["role_requirements"] == after_first_node["role_requirements"]
    assert [entry["operation_id"] for entry in operation_results] == [
        "op-remove-absent",
        "op-add-eng",
        "op-duplicate-eng",
        "op-add-temp",
        "op-remove-temp",
    ]
    assert [entry["status"] for entry in operation_results] == [
        "no_op",
        "applied",
        "no_op",
        "validated_only",
        "validated_only",
    ]
    assert [
        entry["operation_index"] for entry in operation_results
    ] == list(range(len(operation_results)))
    assert [entry["revision_id"] for entry in operation_results] == [
        final_revision_id,
        final_revision_id,
        final_revision_id,
        final_revision_id,
        final_revision_id,
    ]
    assert [entry["requirement_ids"] for entry in operation_results] == [
        ["req-absent"],
        ["req-eng"],
        ["req-eng"],
        ["req-temp"],
        ["req-temp"],
    ]
    assert operation_results[0]["no_op_reason"] == "requirement_already_absent"
    assert operation_results[1]["created_ids"]["requirement_ids"] == ["req-eng"]
    assert operation_results[2]["no_op_reason"] == "requirement_already_present"
    assert operation_results[2]["matched_ids"]["requirement_ids"] == ["req-eng"]
    assert operation_results[3]["candidate_only_ids"]["requirement_ids"] == [
        "req-temp"
    ]
    assert operation_results[4]["candidate_only_ids"]["requirement_ids"] == [
        "req-temp"
    ]
    assert operation_results[3]["validation_reason"] == "candidate_add_then_remove"
    assert operation_results[4]["validation_reason"] == "candidate_add_then_remove"
    assert cancel_remove_readd.ok is True
    assert cancel_remove_readd.entity_ids["revision_ids"] == []
    assert cancel_remove_readd.entity_ids["requirement_ids"] == ["req-eng"]
    assert [
        entry["status"] for entry in cancel_remove_readd.entity_ids["operation_ids"]
    ] == ["validated_only", "validated_only"]
    assert [
        entry["revision_id"]
        for entry in cancel_remove_readd.entity_ids["operation_ids"]
    ] == [final_revision_id, final_revision_id]
    assert [
        entry["validation_reason"]
        for entry in cancel_remove_readd.entity_ids["operation_ids"]
    ] == ["candidate_remove_then_readd", "candidate_remove_then_readd"]
    assert changed_readd.ok is False
    assert changed_readd.error.code == "validation_error"
    assert failed_edit_node["role_requirements"] == after_first_node[
        "role_requirements"
    ]
    assert final_node["role_requirements"] == after_first_node["role_requirements"]


def test_batch_generated_operation_ids_are_stable_on_exact_command_replay():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    design_id = _create_process(service, project_id, name="Design")
    build_id = _create_process(service, project_id, name="Build")
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )
    command = {
        "action": "batch_update_process_graph",
        "project_id": project_id,
        "edit_at": _iso(14, 9),
        "operations": [
            {
                "action": "add_dependency",
                "predecessor_process_id": design_id,
                "successor_process_id": build_id,
            },
            {
                "action": "add_role_requirement",
                "process_id": build_id,
                "requirement": {
                    "requirement_id": "req-build-eng-generated-op",
                    "role_id": "role-engineer",
                    "effort_hours": 8,
                },
            },
        ],
    }

    first = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000203",
    )
    graph_after_first = _process_graph(service, project_id, day=14, hour=10)
    snapshot_after_first = _repository_snapshot(repository)
    replay = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000203",
    )
    graph_after_replay = _process_graph(service, project_id, day=14, hour=10)
    operation_results = first.entity_ids["operation_ids"]
    replay_operation_results = replay.entity_ids["operation_ids"]
    build_node = next(
        node for node in graph_after_replay["nodes"] if node["process_id"] == build_id
    )

    assert first.ok is True
    assert replay.ok is True
    assert replay.entity_ids == first.entity_ids
    assert [
        entry["operation_id"] for entry in operation_results
    ] == [entry["operation_id"] for entry in replay_operation_results]
    assert all(entry["operation_id"] for entry in operation_results)
    assert {entry["operation_id"] for entry in operation_results} != {None}
    assert [entry["operation_index"] for entry in operation_results] == [0, 1]
    assert [entry["action"] for entry in operation_results] == [
        "add_dependency",
        "add_role_requirement",
    ]
    assert graph_after_replay == graph_after_first
    assert _repository_snapshot(repository) == snapshot_after_first
    assert _edge_pairs(graph_after_replay) == {(design_id, build_id)}
    assert build_node["role_requirements"] == [
        {
            "requirement_id": "req-build-eng-generated-op",
            "role_id": "role-engineer",
            "effort_hours": 8,
            "required_resource_count": 1,
            "allocation_policy": "split_allowed",
            "min_allocation_hours_per_day": None,
            "max_allocation_hours_per_day": None,
        }
    ]


def test_batch_resource_operations_apply_and_report_operation_results():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-reviewer",
            "name": "Reviewer",
        },
    )
    _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-nyc",
            "name": "New York Weekdays",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    )
    _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-remote",
            "name": "Remote Weekdays",
            "timezone": "UTC",
            "weekly_windows": _weekday_windows(),
        },
    )

    result = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 9),
            "operations": [
                {
                    "operation_id": "op-upsert-ada",
                    "action": "upsert_resource",
                    "resource": {
                        "resource_id": "resource-ada",
                        "name": "Ada",
                        "role_ids": ["role-engineer"],
                        "calendar_id": "calendar-nyc",
                        "available_from_at": _iso(13, 13),
                        "cost_rate": "125.00",
                        "cost_unit": "hour",
                    },
                },
                {
                    "operation_id": "op-set-ada-roles",
                    "action": "set_resource_roles",
                    "resource_id": "resource-ada",
                    "role_ids": ["role-engineer", "role-reviewer"],
                },
                {
                    "operation_id": "op-set-ada-calendar",
                    "action": "set_resource_calendar",
                    "resource_id": "resource-ada",
                    "calendar_id": "calendar-remote",
                },
            ],
        },
    )
    resource = repository.resources["resource-ada"].model_dump(mode="json")
    operation_results = result.entity_ids["operation_ids"]

    assert result.ok is True
    assert result.entity_ids["resource_ids"] == ["resource-ada"]
    assert resource["role_ids"] == ["role-engineer", "role-reviewer"]
    assert resource["calendar_id"] == "calendar-remote"
    assert [entry["operation_id"] for entry in operation_results] == [
        "op-upsert-ada",
        "op-set-ada-roles",
        "op-set-ada-calendar",
    ]
    assert [entry["operation_index"] for entry in operation_results] == [0, 1, 2]
    assert [entry["action"] for entry in operation_results] == [
        "upsert_resource",
        "set_resource_roles",
        "set_resource_calendar",
    ]
    assert [entry["status"] for entry in operation_results] == [
        "applied",
        "applied",
        "applied",
    ]
    assert operation_results[0]["created_ids"]["resource_ids"] == ["resource-ada"]
    assert operation_results[1]["matched_ids"]["resource_ids"] == ["resource-ada"]
    assert operation_results[2]["matched_ids"]["calendar_ids"] == ["calendar-remote"]


def test_batch_resource_noop_operations_report_idempotent_results():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )
    _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-nyc",
            "name": "New York Weekdays",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    )
    _handle(
        service,
        {
            "action": "upsert_resource",
            "project_id": project_id,
            "resource_id": "resource-ada",
            "name": "Ada",
            "role_ids": ["role-engineer"],
            "calendar_id": "calendar-nyc",
            "available_from_at": _iso(13, 13),
            "cost_rate": "125.00",
            "cost_unit": "hour",
        },
    )

    result = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 9),
            "operations": [
                {
                    "operation_id": "op-upsert-ada-again",
                    "action": "upsert_resource",
                    "resource": {
                        "resource_id": "resource-ada",
                        "name": "Ada",
                        "role_ids": ["role-engineer"],
                        "calendar_id": "calendar-nyc",
                        "available_from_at": _iso(13, 13),
                        "cost_rate": "125.00",
                        "cost_unit": "hour",
                    },
                },
                {
                    "operation_id": "op-set-same-roles",
                    "action": "set_resource_roles",
                    "resource_id": "resource-ada",
                    "role_ids": ["role-engineer"],
                },
                {
                    "operation_id": "op-set-same-calendar",
                    "action": "set_resource_calendar",
                    "resource_id": "resource-ada",
                    "calendar_id": "calendar-nyc",
                },
            ],
        },
    )
    operation_results = result.entity_ids["operation_ids"]

    assert result.ok is True
    assert result.entity_ids["resource_ids"] == ["resource-ada"]
    assert [entry["status"] for entry in operation_results] == [
        "no_op",
        "no_op",
        "no_op",
    ]
    assert all(entry["no_op_reason"] for entry in operation_results)
    assert all(entry["validation_reason"] is None for entry in operation_results)


def test_batch_resource_operation_failure_rolls_back_atomically():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id = _create_project(service)
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )
    _handle(
        service,
        {
            "action": "upsert_resource_calendar",
            "project_id": project_id,
            "calendar_id": "calendar-nyc",
            "name": "New York Weekdays",
            "timezone": "America/New_York",
            "weekly_windows": _weekday_windows(),
        },
    )
    before_failure = _repository_snapshot(repository)

    result = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 9),
            "operations": [
                {
                    "operation_id": "op-upsert-ada",
                    "action": "upsert_resource",
                    "resource": {
                        "resource_id": "resource-ada",
                        "name": "Ada",
                        "role_ids": ["role-engineer"],
                        "calendar_id": "calendar-nyc",
                        "available_from_at": _iso(13, 13),
                        "cost_rate": "125.00",
                        "cost_unit": "hour",
                    },
                },
                {
                    "operation_id": "op-set-missing-calendar",
                    "action": "set_resource_calendar",
                    "resource_id": "resource-ada",
                    "calendar_id": "calendar-missing",
                },
            ],
        },
    )

    assert result.ok is False
    assert result.error.code == "validation_error"
    assert result.warnings == []
    assert _repository_snapshot(repository) == before_failure


def test_batch_reference_validation_precedes_cycle_validation_and_is_atomic():
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )

    reference_error = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 9),
            "operations": [
                {
                    "action": "add_role_requirement",
                    "process_id": design_id,
                    "requirement": {
                        "requirement_id": "req-design",
                        "role_id": "role-engineer",
                        "effort_hours": 4,
                    },
                },
                {
                    "action": "upsert_resource",
                    "resource": {
                        "resource_id": "resource-ada",
                        "name": "Ada",
                        "role_ids": ["role-engineer"],
                        "calendar_id": "calendar-missing",
                        "available_from_at": _iso(13, 9),
                        "cost_rate": "125.00",
                        "cost_unit": "hour",
                    },
                },
                {
                    "action": "add_dependency",
                    "predecessor_process_id": ship_id,
                    "successor_process_id": design_id,
                },
            ],
        },
    )
    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 10),
            "now": _iso(14, 10),
        },
    ).data
    design_node = next(
        node for node in graph["nodes"] if node["process_id"] == design_id
    )

    assert reference_error.ok is False
    assert reference_error.error.code == "validation_error"
    assert reference_error.error.validation_errors[0].loc[:4] == [
        "command",
        "operations",
        1,
        "resource",
    ]
    assert reference_error.error.validation_errors[0].loc[-1] == "calendar_id"
    assert design_node["role_requirements"] == []
    assert {edge["predecessor_process_id"] for edge in graph["edges"]} == {
        design_id,
        build_id,
    }


def test_batch_dependency_cycle_error_shape_and_atomicity():
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)

    cycle = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 9),
            "operations": [
                {
                    "action": "add_dependency",
                    "predecessor_process_id": ship_id,
                    "successor_process_id": design_id,
                }
            ],
        },
    )
    graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 10),
            "now": _iso(14, 10),
        },
    ).data

    assert cycle.ok is False
    assert cycle.error.code == "dependency_cycle"
    assert cycle.error.details["operation_index"] == 0
    assert cycle.error.details["edge"] == {
        "predecessor_symbol": "ship",
        "successor_symbol": "design",
    }
    assert cycle.error.details["cycle_process_ids"][0] == design_id
    assert cycle.error.details["cycle_process_ids"][-1] == design_id
    assert cycle.error.details["cycle_process_symbols"] == [
        "design",
        "build",
        "ship",
        "design",
    ]
    assert {edge["predecessor_process_id"] for edge in graph["edges"]} == {
        design_id,
        build_id,
    }


def test_replace_process_with_subgraph_default_alias_and_edge_reconnects():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)

    lifecycle = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": build_id,
            "status": "done",
            "edit_at": _iso(15, 8),
            "finished_at": _iso(15, 7),
        },
    )
    result = _handle(
        service,
        {
            "action": "replace_process_with_subgraph",
            "project_id": project_id,
            "process_id": build_id,
            "edit_at": _iso(15, 9),
            "processes": [
                {
                    "process_symbol": "backend",
                    "name": "Backend",
                    "description": "Implement backend API surface",
                    "duration_hours": 8,
                }
            ],
            "dependencies": [],
            "root_symbols": ["backend"],
            "leaf_symbols": ["backend"],
        },
        command_id="00000000-0000-4000-8000-000000000301",
    )
    active = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(15, 10),
            "now": _iso(15, 10),
        },
    ).data
    historical = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(15, 8),
            "now": _iso(15, 8),
        },
    ).data
    alias_update = _handle(
        service,
        {
            "action": "set_process_due_at",
            "project_id": project_id,
            "process_symbol": "build",
            "due_at": _iso(22, 17),
            "edit_at": _iso(15, 11),
        },
    )

    child_id = result.entity_ids["process_ids"][0]
    historical_parent = next(
        node for node in historical["nodes"] if node["process_id"] == build_id
    )
    retired_parent_projection = repository.processes[build_id].model_dump(mode="json")
    assert lifecycle.entity_ids["lifecycle_event_id"]
    assert result.entity_ids["retired_process_ids"] == [build_id]
    assert result.entity_ids["alias_process_id"] == child_id
    assert result.entity_ids["retirement_event_ids"]
    assert result.entity_ids["retired_edge_ids"]
    assert {node["process_id"] for node in active["nodes"]} == {
        design_id,
        child_id,
        ship_id,
    }
    assert {node["process_id"] for node in historical["nodes"]} == {
        design_id,
        build_id,
        ship_id,
    }
    assert next(node for node in active["nodes"] if node["process_id"] == child_id)[
        "description"
    ] == "Implement backend API surface"
    assert historical_parent["status"] == "done"
    assert historical_parent["finished_at"] == _iso(15, 7)
    assert retired_parent_projection["is_active"] is False
    assert retired_parent_projection["retired_at"] == _iso(15, 9)
    assert retired_parent_projection["status"] == "done"
    assert retired_parent_projection["finished_at"] == _iso(15, 7)
    assert {
        (edge["predecessor_process_id"], edge["successor_process_id"])
        for edge in active["edges"]
    } == {(design_id, child_id), (child_id, ship_id)}
    assert alias_update.entity_ids["process_id"] == child_id


def test_replace_process_with_subgraph_explicit_target_and_disabled_alias():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    parent_id = _create_process(service, project_id, name="Build")

    explicit = _handle(
        service,
        {
            "action": "replace_process_with_subgraph",
            "project_id": project_id,
            "process_id": parent_id,
            "edit_at": _iso(15, 9),
            "processes": [
                {"process_symbol": "api", "name": "API", "duration_hours": 4},
                {"process_symbol": "ui", "name": "UI", "duration_hours": 4},
            ],
            "dependencies": [
                {
                    "predecessor_symbol": "api",
                    "successor_symbol": "ui",
                }
            ],
            "root_symbols": ["api"],
            "leaf_symbols": ["ui"],
            "parent_alias_target_symbol": "api",
        },
    )
    alias_target = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_symbol": "build",
            "status": "in_progress",
            "edit_at": _iso(15, 10),
        },
    )
    disabled_parent_id = _create_process(service, project_id, name="Deploy")
    disabled = _handle(
        service,
        {
            "action": "replace_process_with_subgraph",
            "project_id": project_id,
            "process_id": disabled_parent_id,
            "edit_at": _iso(16, 9),
            "processes": [
                {
                    "process_symbol": "release",
                    "name": "Release",
                    "duration_hours": 4,
                }
            ],
            "dependencies": [],
            "root_symbols": ["release"],
            "leaf_symbols": ["release"],
            "preserve_parent_symbol_as_alias": False,
        },
    )
    retired_symbol = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_symbol": "deploy",
            "status": "in_progress",
            "edit_at": _iso(16, 10),
        },
    )

    assert explicit.entity_ids["alias_process_id"] == explicit.entity_ids[
        "process_ids"
    ][0]
    assert alias_target.entity_ids["process_id"] == explicit.entity_ids[
        "alias_process_id"
    ]
    assert "alias_process_id" not in disabled.entity_ids
    assert retired_symbol.ok is False
    assert retired_symbol.error.code == "not_found"


def test_replace_process_with_subgraph_infers_roots_and_leaves_from_topology():
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)
    for role_id, name in (
        ("role-engineer", "Engineer"),
        ("role-docs", "Documentation"),
    ):
        _handle(
            service,
            {
                "action": "create_role",
                "project_id": project_id,
                "role_id": role_id,
                "name": name,
            },
        )

    result = _handle(
        service,
        {
            "action": "replace_process_with_subgraph",
            "project_id": project_id,
            "process_id": build_id,
            "edit_at": _iso(15, 9),
            "processes": [
                {
                    "process_symbol": "api",
                    "name": "API",
                    "description": "Expose user-facing API endpoints",
                    "role_requirements": [
                        {"role_id": "role-engineer", "effort_hours": 4}
                    ],
                },
                {
                    "process_symbol": "worker",
                    "name": "Worker",
                    "description": "Run background processing",
                    "role_requirements": [
                        {"role_id": "role-engineer", "effort_hours": 6}
                    ],
                },
                {
                    "process_symbol": "docs",
                    "name": "Docs",
                    "description": "Publish operator documentation",
                    "role_requirements": [
                        {"role_id": "role-docs", "effort_hours": 2}
                    ],
                },
            ],
            "dependencies": [
                {"predecessor_symbol": "api", "successor_symbol": "worker"},
                {"predecessor_symbol": "api", "successor_symbol": "docs"},
            ],
            "parent_alias_target_symbol": "api",
        },
    )
    graph = _process_graph(service, project_id, day=15, hour=10)
    ids_by_symbol = {
        node["process_symbol"]: node["process_id"]
        for node in graph["nodes"]
    }
    nodes_by_symbol = {
        node["process_symbol"]: node
        for node in graph["nodes"]
    }

    assert result.ok is True
    assert _edge_pairs(graph) == {
        (design_id, ids_by_symbol["api"]),
        (ids_by_symbol["api"], ids_by_symbol["worker"]),
        (ids_by_symbol["api"], ids_by_symbol["docs"]),
        (ids_by_symbol["worker"], ship_id),
        (ids_by_symbol["docs"], ship_id),
    }
    assert {
        node["process_symbol"]: {
            requirement["role_id"]: requirement["effort_hours"]
            for requirement in node["role_requirements"]
        }
        for node in (
            nodes_by_symbol["api"],
            nodes_by_symbol["worker"],
            nodes_by_symbol["docs"],
        )
    } == {
        "api": {"role-engineer": 4},
        "worker": {"role-engineer": 6},
        "docs": {"role-docs": 2},
    }
    assert {
        symbol: nodes_by_symbol[symbol]["description"]
        for symbol in ("api", "worker", "docs")
    } == {
        "api": "Expose user-facing API endpoints",
        "worker": "Run background processing",
        "docs": "Publish operator documentation",
    }


def test_replace_process_with_subgraph_alias_collision_rejects_without_writes():
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)
    baseline = _process_graph(service, project_id, day=15, hour=10)

    collision = _handle(
        service,
        {
            "action": "replace_process_with_subgraph",
            "project_id": project_id,
            "process_id": build_id,
            "edit_at": _iso(15, 9),
            "processes": [
                {
                    "process_symbol": "api",
                    "name": "API",
                    "duration_hours": 4,
                    "aliases": ["build"],
                },
                {
                    "process_symbol": "worker",
                    "name": "Worker",
                    "duration_hours": 4,
                },
            ],
            "dependencies": [
                {
                    "predecessor_symbol": "api",
                    "successor_symbol": "worker",
                }
            ],
            "root_symbols": ["api"],
            "leaf_symbols": ["worker"],
            "parent_alias_target_symbol": "worker",
        },
        command_id="00000000-0000-4000-8000-000000000303",
    )
    after_collision = _process_graph(service, project_id, day=15, hour=10)

    assert collision.ok is False
    assert collision.error.code == "validation_error"
    assert collision.warnings == []
    assert collision.error.validation_errors
    validation_error = collision.error.validation_errors[0]
    assert validation_error.loc[0] == "command"
    assert validation_error.type
    assert validation_error.ctx["symbol"] == "build"
    assert after_collision == baseline
    assert _node_ids(after_collision) == {design_id, build_id, ship_id}
    assert {
        node["process_symbol"] for node in after_collision["nodes"]
    } == {"design", "build", "ship"}
    assert _edge_pairs(after_collision) == {(design_id, build_id), (build_id, ship_id)}
    assert all(
        node["process_symbol"] not in {"api", "worker"}
        for node in after_collision["nodes"]
    )


@pytest.mark.parametrize(
    ("command_updates", "loc_field"),
    [
        pytest.param(
            {
                "root_symbols": ["api", "missing-root"],
                "leaf_symbols": ["worker", "missing-leaf"],
            },
            "root_symbols",
            id="roots_leaves_outside_child_set",
        ),
        pytest.param(
            {
                "root_symbols": ["worker"],
                "leaf_symbols": ["api"],
            },
            "root_symbols",
            id="explicit_roots_leaves_must_match_topology",
        ),
        pytest.param(
            {
                "dependencies": [
                    {
                        "predecessor_symbol": "api",
                        "successor_symbol": "missing-child",
                    }
                ],
            },
            "dependencies",
            id="child_dependency_endpoint_outside_child_set",
        ),
        pytest.param(
            {
                "processes": [
                    {"process_symbol": "api", "name": "API", "duration_hours": 4},
                    {"process_symbol": "api", "name": "API Again", "duration_hours": 4},
                ],
                "root_symbols": ["api"],
                "leaf_symbols": ["api"],
            },
            "processes",
            id="duplicate_child_symbols",
        ),
        pytest.param(
            {"root_symbols": ["api", "api"]},
            "root_symbols",
            id="duplicate_root_symbols",
        ),
        pytest.param(
            {"leaf_symbols": ["worker", "worker"]},
            "leaf_symbols",
            id="duplicate_leaf_symbols",
        ),
        pytest.param(
            {"root_symbols": ["worker"]},
            "root_symbols",
            id="root_symbols_must_match_topological_roots",
        ),
        pytest.param(
            {"leaf_symbols": ["api"]},
            "leaf_symbols",
            id="leaf_symbols_must_match_topological_leaves",
        ),
        pytest.param(
            {"parent_alias_target_symbol": "missing-child"},
            "parent_alias_target_symbol",
            id="invalid_parent_alias_target_symbol",
        ),
        pytest.param(
            {
                "dependencies": [
                    {"predecessor_symbol": "api", "successor_symbol": "worker"},
                    {"predecessor_symbol": "worker", "successor_symbol": "api"},
                ],
            },
            "dependencies",
            id="replace_created_internal_cycle",
        ),
    ],
)
def test_replace_process_with_subgraph_validation_errors_do_not_write(
    command_updates: dict[str, object],
    loc_field: str,
):
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)
    baseline = _process_graph(service, project_id, day=16, hour=10)
    command = {
        "action": "replace_process_with_subgraph",
        "project_id": project_id,
        "process_id": build_id,
        "edit_at": _iso(16, 9),
        "processes": [
            {"process_symbol": "api", "name": "API", "duration_hours": 4},
            {"process_symbol": "worker", "name": "Worker", "duration_hours": 4},
        ],
        "dependencies": [{"predecessor_symbol": "api", "successor_symbol": "worker"}],
        "root_symbols": ["api"],
        "leaf_symbols": ["worker"],
        "parent_alias_target_symbol": "api",
    }
    command.update(command_updates)

    result = _handle(service, command)
    after_rejection = _process_graph(service, project_id, day=16, hour=10)

    _assert_structured_validation_error(result, loc_field=loc_field)
    assert after_rejection == baseline
    assert _node_ids(after_rejection) == {design_id, build_id, ship_id}
    assert _edge_pairs(after_rejection) == {(design_id, build_id), (build_id, ship_id)}
    assert {
        node["process_symbol"] for node in after_rejection["nodes"]
    } == {"design", "build", "ship"}


def test_collapse_subgraph_disconnected_selection_rejects_without_writes():
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)
    deploy_id = _create_process(service, project_id, name="Deploy")
    baseline = _process_graph(service, project_id, day=16, hour=10)

    result = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 9),
            "process_symbols": ["design", "deploy"],
            "new_process": {
                "process_symbol": "delivery",
                "name": "Delivery",
            },
        },
    )
    after_rejection = _process_graph(service, project_id, day=16, hour=10)

    _assert_structured_validation_error(result, loc_field="process_symbols")
    assert after_rejection == baseline
    assert _node_ids(after_rejection) == {design_id, build_id, ship_id, deploy_id}
    assert _edge_pairs(after_rejection) == {(design_id, build_id), (build_id, ship_id)}
    assert {
        node["process_symbol"] for node in after_rejection["nodes"]
    } == {"design", "build", "ship", "deploy"}


def test_collapse_subgraph_soft_retires_unions_edges_and_merges_requirements():
    repository = InMemoryProjectRepository()
    service = ProjectService(repository)
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )
    _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 9),
            "operations": [
                {
                    "action": "add_role_requirement",
                    "process_id": design_id,
                    "requirement": {
                        "requirement_id": "req-design-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 3,
                        "required_resource_count": 2,
                    },
                },
                {
                    "action": "add_role_requirement",
                    "process_id": build_id,
                    "requirement": {
                        "requirement_id": "req-build-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 5,
                        "required_resource_count": 2,
                    },
                },
            ],
        },
    )
    design_done = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": design_id,
            "status": "done",
            "edit_at": _iso(16, 7),
            "finished_at": _iso(16, 6),
        },
    )
    build_started = _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": build_id,
            "status": "in_progress",
            "edit_at": _iso(16, 8),
        },
    )

    collapse = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 9),
            "process_symbols": ["design", "build"],
            "new_process": {
                "name": "Implementation",
                "description": "Combined implementation scope",
            },
        },
    )
    active = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(16, 10),
            "now": _iso(16, 10),
        },
    ).data
    historical = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(16, 8),
            "now": _iso(16, 8),
        },
    ).data

    replacement_id = collapse.entity_ids["process_id"]
    replacement_node = next(
        node for node in active["nodes"] if node["process_id"] == replacement_id
    )
    historical_nodes = {node["process_id"]: node for node in historical["nodes"]}
    retired_design_projection = repository.processes[design_id].model_dump(mode="json")
    retired_build_projection = repository.processes[build_id].model_dump(mode="json")
    assert design_done.entity_ids["lifecycle_event_id"]
    assert build_started.entity_ids["lifecycle_event_id"]
    assert collapse.entity_ids["retired_process_ids"] == [design_id, build_id]
    assert collapse.entity_ids["retirement_event_ids"]
    assert collapse.entity_ids["retired_edge_ids"]
    assert {node["process_id"] for node in active["nodes"]} == {
        replacement_id,
        ship_id,
    }
    assert active["edges"] == [
        {
            "edge_id": collapse.entity_ids["edge_ids"][0],
            "project_id": project_id,
            "predecessor_process_id": replacement_id,
            "successor_process_id": ship_id,
            "predecessor_process_symbol": "implementation",
            "successor_process_symbol": "ship",
            "dependency_type": "finish_to_start",
        }
    ]
    assert replacement_node["process_symbol"] == "implementation"
    assert replacement_node["description"] == "Combined implementation scope"
    assert replacement_node["role_requirements"] == [
        {
            "requirement_id": collapse.entity_ids["requirement_ids"][0],
            "role_id": "role-engineer",
            "effort_hours": 8,
            "required_resource_count": 2,
            "allocation_policy": "split_allowed",
            "min_allocation_hours_per_day": None,
            "max_allocation_hours_per_day": None,
        }
    ]
    assert historical_nodes[design_id]["status"] == "done"
    assert historical_nodes[design_id]["finished_at"] == _iso(16, 6)
    assert historical_nodes[build_id]["status"] == "in_progress"
    assert historical_nodes[build_id]["finished_at"] is None
    assert retired_design_projection["is_active"] is False
    assert retired_design_projection["retired_at"] == _iso(16, 9)
    assert retired_design_projection["status"] == "done"
    assert retired_design_projection["finished_at"] == _iso(16, 6)
    assert retired_build_projection["is_active"] is False
    assert retired_build_projection["retired_at"] == _iso(16, 9)
    assert retired_build_projection["status"] == "in_progress"
    assert retired_build_projection["finished_at"] is None


def test_collapse_subgraph_required_resource_count_conflict_requires_replacement():
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, _ = _seed_linear_graph(service)
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )
    _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 9),
            "operations": [
                {
                    "action": "add_role_requirement",
                    "process_id": design_id,
                    "requirement": {
                        "requirement_id": "req-design-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 3,
                        "required_resource_count": 1,
                    },
                },
                {
                    "action": "add_role_requirement",
                    "process_id": build_id,
                    "requirement": {
                        "requirement_id": "req-build-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 5,
                        "required_resource_count": 2,
                    },
                },
            ],
        },
    )

    conflict = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 9),
            "process_symbols": ["design", "build"],
            "new_process": {
                "process_symbol": "implementation",
                "name": "Implementation",
            },
        },
    )
    explicit = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 10),
            "process_symbols": ["design", "build"],
            "new_process": {
                "process_symbol": "implementation",
                "name": "Implementation",
                "role_requirements": [
                    {
                        "requirement_id": "req-implementation-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 8,
                        "required_resource_count": 2,
                    }
                ],
            },
        },
    )

    assert conflict.ok is False
    assert conflict.error.code == "validation_error"
    validation_error = conflict.error.validation_errors[0]
    assert validation_error.type == "collapse_role_requirement_conflict"
    assert validation_error.ctx["field"] == "required_resource_count"
    assert validation_error.ctx["role_id"] == "role-engineer"
    assert validation_error.ctx["values"] == [1, 2]
    assert explicit.ok is True
    assert explicit.entity_ids["process_id"]
    assert explicit.entity_ids["requirement_ids"] == ["req-implementation-eng"]


def test_collapse_subgraph_preserves_total_effort_hours_by_role_when_omitted():
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, _ship_id = _seed_linear_graph(service)
    for role_id, name in (
        ("role-engineer", "Engineer"),
        ("role-qa", "QA"),
    ):
        _handle(
            service,
            {
                "action": "create_role",
                "project_id": project_id,
                "role_id": role_id,
                "name": name,
            },
        )
    _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 9),
            "operations": [
                {
                    "action": "add_role_requirement",
                    "process_id": design_id,
                    "requirement": {
                        "requirement_id": "req-design-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 3,
                    },
                },
                {
                    "action": "add_role_requirement",
                    "process_id": design_id,
                    "requirement": {
                        "requirement_id": "req-design-qa",
                        "role_id": "role-qa",
                        "effort_hours": 2,
                    },
                },
                {
                    "action": "add_role_requirement",
                    "process_id": build_id,
                    "requirement": {
                        "requirement_id": "req-build-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 5,
                    },
                },
            ],
        },
    )

    collapse = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 9),
            "process_symbols": ["design", "build"],
            "new_process": {
                "process_symbol": "implementation",
                "name": "Implementation",
            },
        },
    )
    graph = _process_graph(service, project_id, day=16, hour=10)
    replacement = next(
        node
        for node in graph["nodes"]
        if node["process_id"] == collapse.entity_ids["process_id"]
    )

    assert {
        requirement["role_id"]: requirement["effort_hours"]
        for requirement in replacement["role_requirements"]
    } == {
        "role-engineer": 8,
        "role-qa": 2,
    }


def test_collapse_subgraph_explicit_effort_requirements_do_not_merge_legacy_roles():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    design_id = _create_legacy_process(
        service,
        project_id,
        name="Design",
        duration_business_days=1,
        required_roles={"engineer": 0.25},
    )
    build_id = _create_legacy_process(
        service,
        project_id,
        name="Build",
        duration_business_days=1,
        required_roles={"engineer": 0.75},
        dependencies=[design_id],
    )
    _create_process(
        service,
        project_id,
        name="Ship",
        dependencies=[build_id],
    )
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-delivery",
            "name": "Delivery",
        },
    )

    collapse = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 9),
            "process_symbols": ["design", "build"],
            "new_process": {
                "process_symbol": "implementation",
                "name": "Implementation",
                "role_requirements": [
                    {"role_id": "role-delivery", "effort_hours": 10}
                ],
            },
        },
    )
    graph = _process_graph(service, project_id, day=16, hour=10)
    replacement = next(
        node
        for node in graph["nodes"]
        if node["process_id"] == collapse.entity_ids["process_id"]
    )

    assert collapse.ok is True
    assert replacement.get("required_roles", {}) == {}
    assert {
        requirement["role_id"]: requirement["effort_hours"]
        for requirement in replacement["role_requirements"]
    } == {"role-delivery": 10}


def test_collapse_subgraph_mixed_role_modes_require_explicit_replacement():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    design_id = _create_legacy_process(
        service,
        project_id,
        name="Design",
        duration_business_days=1,
        required_roles={"engineer": 0.25},
    )
    build_id = _create_process(
        service,
        project_id,
        name="Build",
        dependencies=[design_id],
    )
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )
    _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 9),
            "operations": [
                {
                    "action": "add_role_requirement",
                    "process_id": build_id,
                    "requirement": {
                        "role_id": "role-engineer",
                        "effort_hours": 5,
                    },
                }
            ],
        },
    )
    baseline = _process_graph(service, project_id, day=16, hour=10)

    collapse = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 9),
            "process_symbols": ["design", "build"],
            "new_process": {
                "process_symbol": "implementation",
                "name": "Implementation",
            },
        },
    )
    after_rejection = _process_graph(service, project_id, day=16, hour=10)

    assert collapse.ok is False
    assert collapse.error.code == "validation_error"
    validation_error = collapse.error.validation_errors[0]
    assert validation_error.type == "collapse_mixed_role_requirement_modes"
    assert validation_error.ctx["role_requirement_process_ids"] == [build_id]
    assert validation_error.ctx["legacy_required_role_process_ids"] == [design_id]
    assert after_rejection == baseline


def test_collapse_subgraph_legacy_required_roles_conserves_attention_by_role():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    design_id = _create_legacy_process(
        service,
        project_id,
        name="Design",
        duration_business_days=1,
        required_roles={"engineer": 0.25, "qa": 0.5},
    )
    build_id = _create_legacy_process(
        service,
        project_id,
        name="Build",
        duration_business_days=3,
        dependencies=[design_id],
        required_roles={"engineer": 0.75, "qa": 0.25},
    )
    ship_id = _create_process(
        service,
        project_id,
        name="Ship",
        dependencies=[build_id],
    )

    collapse = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 9),
            "process_symbols": ["design", "build"],
            "new_process": {
                "process_symbol": "implementation",
                "name": "Implementation",
            },
        },
    )
    active = _process_graph(service, project_id, day=16, hour=10)

    replacement_id = collapse.entity_ids["process_id"]
    replacement_node = next(
        node for node in active["nodes"] if node["process_id"] == replacement_id
    )
    assert collapse.ok is True
    assert collapse.entity_ids["retired_process_ids"] == [design_id, build_id]
    assert _node_ids(active) == {replacement_id, ship_id}
    assert _edge_pairs(active) == {(replacement_id, ship_id)}
    assert replacement_node["required_roles"] == {
        "engineer": pytest.approx(((0.25 * 1) + (0.75 * 3)) / 4),
        "qa": pytest.approx(((0.5 * 1) + (0.25 * 3)) / 4),
    }


def test_collapse_subgraph_zero_duration_legacy_attention_rejects_without_writes():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    design_id = _create_legacy_process(
        service,
        project_id,
        name="Design",
        duration_business_days=0,
        required_roles={"engineer": 0.5},
    )
    build_id = _create_legacy_process(
        service,
        project_id,
        name="Build",
        duration_business_days=0,
        dependencies=[design_id],
        required_roles={"engineer": 0.25},
    )
    ship_id = _create_process(
        service,
        project_id,
        name="Ship",
        dependencies=[build_id],
    )
    baseline = _process_graph(service, project_id, day=16, hour=10)

    collapse = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 9),
            "process_symbols": ["design", "build"],
            "new_process": {
                "process_symbol": "implementation",
                "name": "Implementation",
            },
        },
    )
    after_rejection = _process_graph(service, project_id, day=16, hour=10)

    assert collapse.ok is False
    assert collapse.error.code == "validation_error"
    assert collapse.warnings == []
    assert collapse.error.validation_errors
    validation_error = collapse.error.validation_errors[0]
    assert validation_error.loc[0] == "command"
    assert validation_error.type
    assert validation_error.ctx["subgraph_cp_duration"] == 0
    assert validation_error.ctx["roles"] == ["engineer"]
    assert after_rejection == baseline
    assert _node_ids(after_rejection) == {design_id, build_id, ship_id}
    assert _edge_pairs(after_rejection) == {(design_id, build_id), (build_id, ship_id)}
    assert {
        node["process_symbol"] for node in after_rejection["nodes"]
    } == {"design", "build", "ship"}


def test_active_and_historical_graph_visibility_after_replace_and_collapse():
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)
    replace = _handle(
        service,
        {
            "action": "replace_process_with_subgraph",
            "project_id": project_id,
            "process_id": build_id,
            "edit_at": _iso(15, 9),
            "processes": [
                {"process_symbol": "api", "name": "API", "duration_hours": 4},
                {
                    "process_symbol": "worker",
                    "name": "Worker",
                    "duration_hours": 4,
                },
            ],
            "dependencies": [
                {"predecessor_symbol": "api", "successor_symbol": "worker"}
            ],
            "root_symbols": ["api"],
            "leaf_symbols": ["worker"],
            "parent_alias_target_symbol": "api",
        },
    )
    collapse = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 9),
            "process_symbols": ["api", "worker"],
            "new_process": {
                "process_symbol": "implementation",
                "name": "Implementation",
            },
        },
    )

    before_replace = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(14, 12),
            "now": _iso(14, 12),
        },
    ).data
    after_replace = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(15, 12),
            "now": _iso(15, 12),
        },
    ).data
    after_collapse = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(16, 12),
            "now": _iso(16, 12),
        },
    ).data

    api_id, worker_id = replace.entity_ids["process_ids"]
    implementation_id = collapse.entity_ids["process_id"]
    assert {node["process_id"] for node in before_replace["nodes"]} == {
        design_id,
        build_id,
        ship_id,
    }
    assert {node["process_id"] for node in after_replace["nodes"]} == {
        design_id,
        api_id,
        worker_id,
        ship_id,
    }
    assert {node["process_id"] for node in after_collapse["nodes"]} == {
        design_id,
        implementation_id,
        ship_id,
    }
    assert all(
        node["process_id"] not in {build_id, api_id, worker_id}
        for node in after_collapse["nodes"]
    )
    assert {
        (edge["predecessor_process_id"], edge["successor_process_id"])
        for edge in after_collapse["edges"]
    } == {(design_id, implementation_id), (implementation_id, ship_id)}


def test_retired_process_blockers_remain_auditable_resolvable_not_active_blocked():
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)
    blocker_id = _handle(
        service,
        {
            "action": "add_blocker",
            "project_id": project_id,
            "process_id": build_id,
            "blocker_id": "blocker-retired-build",
            "summary": "Legacy build review pending",
            "severity": "blocking",
            "created_at": _iso(14, 9),
        },
    ).entity_ids["blocker_id"]
    replace = _handle(
        service,
        {
            "action": "replace_process_with_subgraph",
            "project_id": project_id,
            "process_id": build_id,
            "edit_at": _iso(15, 9),
            "processes": [
                {"process_symbol": "api", "name": "API", "duration_hours": 4},
                {"process_symbol": "worker", "name": "Worker", "duration_hours": 4},
            ],
            "dependencies": [
                {"predecessor_symbol": "api", "successor_symbol": "worker"}
            ],
            "root_symbols": ["api"],
            "leaf_symbols": ["worker"],
            "parent_alias_target_symbol": "api",
        },
    )
    active_graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": _iso(15, 12),
            "now": _iso(15, 12),
        },
    ).data
    active_blockers = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(15, 12),
        },
    ).data
    audit_before_resolution = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(15, 12),
            "process_ids": [build_id],
            "include_resolved": True,
        },
    ).data
    resolved = _handle(
        service,
        {
            "action": "resolve_blocker",
            "project_id": project_id,
            "blocker_id": blocker_id,
            "resolved_at": _iso(15, 13),
            "resolution": "Retired process blocker closed for audit.",
        },
    )
    audit_after_resolution = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": _iso(15, 14),
            "process_ids": [build_id],
            "include_resolved": True,
        },
    ).data

    api_id, worker_id = replace.entity_ids["process_ids"]
    assert replace.entity_ids["retired_process_ids"] == [build_id]
    assert {node["process_id"] for node in active_graph["nodes"]} == {
        design_id,
        api_id,
        worker_id,
        ship_id,
    }
    assert all(node["process_id"] != build_id for node in active_graph["nodes"])
    assert all(
        node["computed_status"] != "blocked"
        for node in active_graph["nodes"]
        if node["process_id"] in {api_id, worker_id}
    )
    assert build_id not in active_blockers["blocked_process_ids"]
    assert audit_before_resolution["blocked_process_ids"] == []
    assert audit_before_resolution["blockers"][0]["blocker_id"] == blocker_id
    assert audit_before_resolution["blockers"][0]["process_id"] == build_id
    assert audit_before_resolution["blockers"][0]["is_resolved_as_of"] is False
    assert audit_before_resolution["blockers"][0]["is_blocking_as_of"] is False
    assert resolved.entity_ids["blocker_id"] == blocker_id
    assert audit_after_resolution["blocked_process_ids"] == []
    assert audit_after_resolution["blockers"][0]["resolved_at"] == _iso(15, 13)
    assert audit_after_resolution["blockers"][0]["resolution"] == (
        "Retired process blocker closed for audit."
    )


def test_batch_dependency_noops_and_edge_id_errors_are_atomic():
    service = ProjectService(InMemoryProjectRepository())
    project_id = _create_project(service)
    design_id = _create_process(service, project_id, name="Design")
    build_id = _create_process(service, project_id, name="Build")
    _create_process(service, project_id, name="Ship")

    add = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 9),
            "operations": [
                {
                    "operation_id": "op-add-design-build",
                    "action": "add_dependency",
                    "edge_id": "edge-design-build",
                    "predecessor_process_symbol": "design",
                    "successor_process_symbol": "build",
                }
            ],
        },
    )
    baseline = _process_graph(service, project_id, day=14, hour=10)

    already_present = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 11),
            "operations": [
                {
                    "operation_id": "op-add-present",
                    "action": "add_dependency",
                    "predecessor_process_symbol": "design",
                    "successor_process_symbol": "build",
                }
            ],
        },
    )
    absent_remove = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 12),
            "operations": [
                {
                    "operation_id": "op-remove-absent",
                    "action": "remove_dependency",
                    "predecessor_process_symbol": "ship",
                    "successor_process_symbol": "design",
                }
            ],
        },
    )
    unknown_edge_remove = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 13),
            "operations": [
                {
                    "operation_id": "op-remove-missing-id",
                    "action": "remove_dependency",
                    "edge_id": "edge-missing",
                }
            ],
        },
    )
    reused_edge_id = _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 14),
            "operations": [
                {
                    "operation_id": "op-reuse-edge-id",
                    "action": "add_dependency",
                    "edge_id": "edge-design-build",
                    "predecessor_process_symbol": "design",
                    "successor_process_symbol": "ship",
                }
            ],
        },
    )
    final_graph = _process_graph(service, project_id, day=14, hour=15)

    assert add.ok is True
    assert add.entity_ids["edge_ids"] == ["edge-design-build"]
    assert _edge_pairs(baseline) == {(design_id, build_id)}
    assert already_present.ok is True
    assert already_present.entity_ids["edge_ids"] == ["edge-design-build"]
    present_operation = already_present.entity_ids["operation_ids"][0]
    assert present_operation["operation_index"] == 0
    assert present_operation["operation_id"] == "op-add-present"
    assert present_operation["action"] == "add_dependency"
    assert present_operation["status"] == "no_op"
    assert present_operation["edge_ids"] == ["edge-design-build"]
    assert present_operation["matched_ids"]["edge_ids"] == ["edge-design-build"]
    assert present_operation["no_op_reason"] == "dependency_already_present"
    assert present_operation["validation_reason"] is None
    after_present = _process_graph(service, project_id, day=14, hour=11)
    assert _node_ids(after_present) == _node_ids(baseline)
    assert _edge_pairs(after_present) == _edge_pairs(baseline)
    assert absent_remove.ok is True
    assert absent_remove.entity_ids["edge_ids"] == []
    assert absent_remove.entity_ids["operation_ids"][0]["status"] == "no_op"
    assert (
        absent_remove.entity_ids["operation_ids"][0]["no_op_reason"]
        == "dependency_already_absent"
    )
    assert absent_remove.entity_ids["operation_ids"][0]["edge_ids"] == []
    after_absent_remove = _process_graph(service, project_id, day=14, hour=12)
    assert _node_ids(after_absent_remove) == _node_ids(baseline)
    assert _edge_pairs(after_absent_remove) == _edge_pairs(baseline)
    assert unknown_edge_remove.ok is False
    assert unknown_edge_remove.error.code == "not_found"
    assert reused_edge_id.ok is False
    assert reused_edge_id.error.code == "validation_error"
    assert _node_ids(final_graph) == _node_ids(baseline)
    assert _edge_pairs(final_graph) == {(design_id, build_id)}
    assert _edge_pairs(final_graph) == _edge_pairs(baseline)


@pytest.mark.parametrize(
    (
        "conflict_field",
        "design_requirement",
        "build_requirement",
        "explicit_requirement",
        "expected_values",
    ),
    [
        (
            "min_allocation_hours_per_day",
            {"min_allocation_hours_per_day": 2},
            {"min_allocation_hours_per_day": 4},
            {"min_allocation_hours_per_day": 3},
            [2, 4],
        ),
        (
            "max_allocation_hours_per_day",
            {"max_allocation_hours_per_day": 4},
            {"max_allocation_hours_per_day": 6},
            {"max_allocation_hours_per_day": 5},
            [4, 6],
        ),
        (
            "allocation_policy",
            {"allocation_policy": "split_allowed"},
            {"allocation_policy": "contiguous"},
            {"allocation_policy": "contiguous"},
            ["split_allowed", "contiguous"],
        ),
    ],
)
def test_collapse_requirement_control_conflicts_require_explicit_replacement(
    conflict_field: str,
    design_requirement: dict[str, object],
    build_requirement: dict[str, object],
    explicit_requirement: dict[str, object],
    expected_values: list[object],
):
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )
    design_req = {
        "requirement_id": "req-design-eng",
        "role_id": "role-engineer",
        "effort_hours": 3,
        "required_resource_count": 1,
        **design_requirement,
    }
    build_req = {
        "requirement_id": "req-build-eng",
        "role_id": "role-engineer",
        "effort_hours": 5,
        "required_resource_count": 1,
        **build_requirement,
    }
    _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 9),
            "operations": [
                {
                    "action": "add_role_requirement",
                    "process_id": design_id,
                    "requirement": design_req,
                },
                {
                    "action": "add_role_requirement",
                    "process_id": build_id,
                    "requirement": build_req,
                },
            ],
        },
    )
    baseline = _process_graph(service, project_id, day=14, hour=10)

    conflict = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 9),
            "process_symbols": ["design", "build"],
            "new_process": {
                "process_symbol": "implementation",
                "name": "Implementation",
            },
        },
    )
    after_conflict = _process_graph(service, project_id, day=16, hour=10)
    explicit = _handle(
        service,
        {
            "action": "collapse_subgraph",
            "project_id": project_id,
            "edit_at": _iso(16, 11),
            "process_symbols": ["design", "build"],
            "new_process": {
                "process_symbol": "implementation",
                "name": "Implementation",
                "role_requirements": [
                    {
                        "requirement_id": "req-implementation-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 8,
                        "required_resource_count": 1,
                        **explicit_requirement,
                    }
                ],
            },
        },
    )
    after_explicit = _process_graph(service, project_id, day=16, hour=12)

    assert conflict.ok is False
    assert conflict.error.code == "validation_error"
    validation_error = conflict.error.validation_errors[0]
    assert validation_error.type == "collapse_role_requirement_conflict"
    assert validation_error.ctx["field"] == conflict_field
    assert validation_error.ctx["values"] == expected_values
    assert validation_error.ctx["process_ids"] == [design_id, build_id]
    assert validation_error.ctx["requirement_ids"] == [
        "req-design-eng",
        "req-build-eng",
    ]
    assert _node_ids(after_conflict) == _node_ids(baseline)
    assert _edge_pairs(after_conflict) == _edge_pairs(baseline)
    for process_id in (design_id, build_id):
        baseline_node = next(
            node for node in baseline["nodes"] if node["process_id"] == process_id
        )
        conflict_node = next(
            node
            for node in after_conflict["nodes"]
            if node["process_id"] == process_id
        )
        assert conflict_node["role_requirements"] == baseline_node[
            "role_requirements"
        ]
    assert explicit.ok is True
    assert explicit.entity_ids["process_id"]
    assert explicit.entity_ids["retired_process_ids"] == [design_id, build_id]
    assert explicit.entity_ids["requirement_ids"] == ["req-implementation-eng"]
    assert {node["process_id"] for node in after_explicit["nodes"]} == {
        explicit.entity_ids["process_id"],
        ship_id,
    }


def test_replace_process_with_subgraph_exact_command_replay_reuses_result_ids():
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )
    command = {
        "action": "replace_process_with_subgraph",
        "project_id": project_id,
        "process_id": build_id,
        "edit_at": _iso(15, 9),
        "processes": [
            {
                "process_symbol": "api",
                "name": "API",
                "duration_hours": 4,
                "role_requirements": [
                    {
                        "requirement_id": "req-api-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 6,
                    }
                ],
            },
            {
                "process_symbol": "worker",
                "name": "Worker",
                "duration_hours": 4,
            },
        ],
        "dependencies": [
            {
                "edge_id": "edge-api-worker",
                "predecessor_symbol": "api",
                "successor_symbol": "worker",
            }
        ],
        "root_symbols": ["api"],
        "leaf_symbols": ["worker"],
        "parent_alias_target_symbol": "api",
    }

    first = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000401",
    )
    graph_after_first = _process_graph(service, project_id, day=15, hour=10)
    replay = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000401",
    )
    graph_after_replay = _process_graph(service, project_id, day=15, hour=10)

    assert replay.command_id == first.command_id
    assert replay.entity_ids == first.entity_ids
    assert replay.entity_ids["process_ids"] == first.entity_ids["process_ids"]
    assert replay.entity_ids["retired_process_ids"] == [build_id]
    assert replay.entity_ids["retirement_event_ids"] == first.entity_ids[
        "retirement_event_ids"
    ]
    assert replay.entity_ids["edge_ids"] == first.entity_ids["edge_ids"]
    assert replay.entity_ids["retired_edge_ids"] == first.entity_ids[
        "retired_edge_ids"
    ]
    assert replay.entity_ids["alias_process_id"] == first.entity_ids[
        "alias_process_id"
    ]
    assert graph_after_replay == graph_after_first
    assert {node["process_id"] for node in graph_after_replay["nodes"]} == {
        design_id,
        *first.entity_ids["process_ids"],
        ship_id,
    }
    api_node = next(
        node
        for node in graph_after_replay["nodes"]
        if node["process_id"] == first.entity_ids["alias_process_id"]
    )
    assert api_node["role_requirements"] == [
        {
            "requirement_id": "req-api-eng",
            "role_id": "role-engineer",
            "effort_hours": 6,
            "required_resource_count": 1,
            "allocation_policy": "split_allowed",
            "min_allocation_hours_per_day": None,
            "max_allocation_hours_per_day": None,
        }
    ]


def test_collapse_subgraph_exact_command_replay_reuses_result_ids():
    service = ProjectService(InMemoryProjectRepository())
    project_id, design_id, build_id, ship_id = _seed_linear_graph(service)
    _handle(
        service,
        {
            "action": "create_role",
            "project_id": project_id,
            "role_id": "role-engineer",
            "name": "Engineer",
        },
    )
    _handle(
        service,
        {
            "action": "batch_update_process_graph",
            "project_id": project_id,
            "edit_at": _iso(14, 9),
            "operations": [
                {
                    "action": "add_role_requirement",
                    "process_id": design_id,
                    "requirement": {
                        "requirement_id": "req-design-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 3,
                    },
                },
                {
                    "action": "add_role_requirement",
                    "process_id": build_id,
                    "requirement": {
                        "requirement_id": "req-build-eng",
                        "role_id": "role-engineer",
                        "effort_hours": 5,
                    },
                },
            ],
        },
    )
    command = {
        "action": "collapse_subgraph",
        "project_id": project_id,
        "edit_at": _iso(16, 9),
        "process_symbols": ["design", "build"],
        "new_process": {
            "process_symbol": "implementation",
            "name": "Implementation",
        },
    }

    first = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000402",
    )
    graph_after_first = _process_graph(service, project_id, day=16, hour=10)
    replay = _handle(
        service,
        command,
        command_id="00000000-0000-4000-8000-000000000402",
    )
    graph_after_replay = _process_graph(service, project_id, day=16, hour=10)

    assert replay.command_id == first.command_id
    assert replay.entity_ids == first.entity_ids
    assert replay.entity_ids["process_id"] == first.entity_ids["process_id"]
    assert replay.entity_ids["retired_process_ids"] == [design_id, build_id]
    assert replay.entity_ids["retirement_event_ids"] == first.entity_ids[
        "retirement_event_ids"
    ]
    assert replay.entity_ids["edge_ids"] == first.entity_ids["edge_ids"]
    assert replay.entity_ids["retired_edge_ids"] == first.entity_ids[
        "retired_edge_ids"
    ]
    assert replay.entity_ids["requirement_ids"] == first.entity_ids[
        "requirement_ids"
    ]
    assert graph_after_replay == graph_after_first
    assert {node["process_id"] for node in graph_after_replay["nodes"]} == {
        first.entity_ids["process_id"],
        ship_id,
    }
    replacement_node = next(
        node
        for node in graph_after_replay["nodes"]
        if node["process_id"] == first.entity_ids["process_id"]
    )
    assert replacement_node["role_requirements"] == [
        {
            "requirement_id": first.entity_ids["requirement_ids"][0],
            "role_id": "role-engineer",
            "effort_hours": 8,
            "required_resource_count": 1,
            "allocation_policy": "split_allowed",
            "min_allocation_hours_per_day": None,
            "max_allocation_hours_per_day": None,
        }
    ]
