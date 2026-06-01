import datetime as dt

from projdash.service.commands import CommandEnvelope, CreateProject, UpsertProcessRevision
from projdash.service.queries import QueryCriticalPath, QueryEnvelope, QueryProcessGraph
from projdash.service.repository import InMemoryProjectRepository
from projdash.service.service import ProjectService

UTC = dt.UTC


def _at(day: int, hour: int = 9) -> dt.datetime:
    return dt.datetime(2026, 5, day, hour, tzinfo=UTC)


def _project_with_processes() -> tuple[ProjectService, str, str, str, str]:
    service = ProjectService(InMemoryProjectRepository())
    project_id = service.handle_command(
        CommandEnvelope(
            command=CreateProject(
                name="Schedule Project",
                start_at=_at(13),
            )
        )
    ).entity_ids["project_id"]
    design_id = service.handle_command(
        CommandEnvelope(
            command=UpsertProcessRevision(
                project_id=project_id,
                name="Design",
                effective_at=_at(13),
                duration_business_days=2,
            )
        )
    ).entity_ids["process_id"]
    implementation_id = service.handle_command(
        CommandEnvelope(
            command=UpsertProcessRevision(
                project_id=project_id,
                name="Implementation",
                effective_at=_at(13),
                duration_business_days=3,
                dependencies=[design_id],
            )
        )
    ).entity_ids["process_id"]
    review_id = service.handle_command(
        CommandEnvelope(
            command=UpsertProcessRevision(
                project_id=project_id,
                name="Review",
                effective_at=_at(13),
                duration_business_days=1,
                dependencies=[implementation_id],
            )
        )
    ).entity_ids["process_id"]
    return service, project_id, design_id, implementation_id, review_id


def test_schedule_projection_computes_critical_path_datetimes():
    service, project_id, design_id, implementation_id, review_id = _project_with_processes()

    result = service.handle_query(
        QueryEnvelope(
            query=QueryProcessGraph(
                project_id=project_id,
                as_of=_at(13),
                now=_at(13),
            )
        )
    )
    nodes = {node["process_id"]: node for node in result.data["nodes"]}

    assert result.data["schedule_basis"] == "dependency_only"
    assert nodes[design_id]["dependency_only"]["es_at"] == "2026-05-13T09:00:00+00:00"
    assert nodes[design_id]["dependency_only"]["ef_at"] == "2026-05-15T09:00:00+00:00"
    assert (
        nodes[implementation_id]["dependency_only"]["es_at"]
        == "2026-05-15T09:00:00+00:00"
    )
    assert nodes[review_id]["dependency_only"]["ef_at"] == "2026-05-21T09:00:00+00:00"
    assert nodes[review_id]["dependency_only"]["slack_hours"] == 0


def test_schedule_roots_at_project_start_even_on_non_business_day():
    service = ProjectService(InMemoryProjectRepository())
    saturday_start = dt.datetime(2026, 5, 16, 9, tzinfo=UTC)
    project_id = service.handle_command(
        CommandEnvelope(
            command=CreateProject(
                name="Weekend Root",
                start_at=saturday_start,
            )
        )
    ).entity_ids["project_id"]
    process_id = service.handle_command(
        CommandEnvelope(
            command=UpsertProcessRevision(
                project_id=project_id,
                name="Zero Duration",
                effective_at=saturday_start,
                duration_business_days=0,
            )
        )
    ).entity_ids["process_id"]

    result = service.handle_query(
        QueryEnvelope(
            query=QueryProcessGraph(
                project_id=project_id,
                as_of=saturday_start,
                now=saturday_start,
            )
        )
    )
    node = result.data["nodes"][0]

    assert node["process_id"] == process_id
    assert node["dependency_only"]["es_at"] == saturday_start.isoformat()
    assert node["dependency_only"]["ef_at"] == saturday_start.isoformat()


def test_critical_path_query_returns_ordered_process_ids():
    service, project_id, design_id, implementation_id, review_id = _project_with_processes()

    result = service.handle_query(
        QueryEnvelope(
            query=QueryCriticalPath(
                project_id=project_id,
                as_of=_at(13),
                now=_at(13),
            )
        )
    )

    assert result.data["critical_path"] == [design_id, implementation_id, review_id]


def test_unstarted_blocked_by_dependencies_does_not_mark_process_done():
    service, project_id, _, implementation_id, _ = _project_with_processes()

    result = service.handle_query(
        QueryEnvelope(
            query=QueryProcessGraph(
                project_id=project_id,
                as_of=_at(13),
                now=_at(20),
            )
        )
    )
    nodes = {node["process_id"]: node for node in result.data["nodes"]}

    assert nodes[implementation_id]["status"] == "waiting"
    assert nodes[implementation_id]["computed_status"] == "waiting"
    assert nodes[implementation_id]["finished_at"] is None


def test_dependency_cycles_are_rejected():
    service, project_id, design_id, implementation_id, _ = _project_with_processes()

    result = service.handle_command(
        CommandEnvelope(
            command=UpsertProcessRevision(
                project_id=project_id,
                process_id=design_id,
                name="Design",
                effective_at=_at(14),
                duration_business_days=2,
                dependencies=[implementation_id],
            )
        )
    )

    graph_result = service.handle_query(
        QueryEnvelope(
            query=QueryProcessGraph(
                project_id=project_id,
                as_of=_at(14),
                now=_at(14),
            )
        )
    )
    edges = {
        (edge["predecessor_process_id"], edge["successor_process_id"])
        for edge in graph_result.data["edges"]
    }

    assert result.ok is False
    assert result.error.code == "dependency_cycle"
    assert (
        result.error.message
        == "Adding this process revision would create a dependency cycle."
    )
    assert result.error.details["field_path"] == "dependencies"
    assert result.error.details["entity_id"] == design_id
    assert result.error.details["as_of"] == _at(14).isoformat()
    assert {
        (edge["predecessor_process_id"], edge["successor_process_id"])
        for edge in result.error.details["cycle"]
    } == {
        (implementation_id, design_id),
        (design_id, implementation_id),
    }
    assert (implementation_id, design_id) not in edges
    assert (design_id, implementation_id) in edges
