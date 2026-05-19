import datetime as dt
from collections.abc import Mapping
from typing import Any

from projdash.service.commands import CommandEnvelope
from projdash.service.queries import QueryEnvelope
from projdash.service.repository import InMemoryProjectRepository
from projdash.service.service import ProjectService

UTC = dt.UTC


def _at(day: int, hour: int = 9) -> dt.datetime:
    return dt.datetime(2026, 5, day, hour, tzinfo=UTC)


def _iso(day: int, hour: int = 9) -> str:
    return _at(day, hour).isoformat()


def _handle(service: ProjectService, command: Mapping[str, Any]) -> dict[str, str]:
    result = service.handle_command(CommandEnvelope.model_validate({"command": command}))
    assert result.ok is True
    return result.entity_ids


def _query(service: ProjectService, query: Mapping[str, Any]) -> None:
    result = service.handle_query(QueryEnvelope.model_validate({"query": query}))
    assert result.ok is True
    assert result.warnings == []


def _seed_project(service: ProjectService) -> tuple[str, str]:
    project_ids = _handle(
        service,
        {
            "action": "create_project",
            "name": "Cache Project",
            "start_at": _iso(13, 9),
        },
    )
    process_ids = _handle(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": project_ids["project_id"],
            "process_symbol": "build",
            "name": "Build",
            "effective_at": _iso(13, 9),
            "duration_business_days": 1,
        },
    )
    return project_ids["project_id"], process_ids["process_id"]


def _counting_scheduler(calls: list[dict[str, object]]):
    def scheduler(input_data: dict[str, object]) -> dict[str, object]:
        calls.append(input_data)
        now = input_data["now"]
        assert isinstance(now, dt.datetime)
        starts_at = now
        ends_at = starts_at + dt.timedelta(hours=8)
        latest_start_at = starts_at + dt.timedelta(hours=4)
        latest_finish_at = starts_at + dt.timedelta(hours=12)
        process_rows = []
        for process in input_data["processes"]:
            process_id = str(process["process_id"])
            process_rows.append(
                {
                    "process_id": process_id,
                    "ready_at": starts_at,
                    "starts_at": starts_at,
                    "ends_at": ends_at,
                    "resource_es_at": starts_at,
                    "resource_ef_at": ends_at,
                    "resource_ls_at": latest_start_at,
                    "resource_lf_at": latest_finish_at,
                    "inferred_duration_hours": 8,
                    "resource_delay_hours": 0,
                    "resource_slack_hours": 4,
                    "allocation_state": "allocated",
                }
            )
        return {
            "project_id": input_data["project_id"],
            "as_of": input_data["as_of"],
            "now": input_data["now"],
            "horizon_starts_at": input_data["options"]["horizon_starts_at"],
            "horizon_ends_at": input_data["options"]["horizon_ends_at"],
            "planning_granularity": input_data["options"]["planning_granularity"],
            "processes": process_rows,
            "allocation_slices": [],
            "critical_path_process_ids": [
                row["process_id"] for row in process_rows
            ],
            "converged": True,
            "iteration_count": 1,
            "convergence": {
                "converged": True,
                "iteration_count": 1,
                "max_iterations": input_data["options"]["max_iterations"],
                "tolerance_hours": input_data["options"][
                    "convergence_tolerance_hours"
                ],
                "changed_process_ids": [],
                "reason_changes": [],
                "allocation_fingerprint_changed": False,
            },
        }

    return scheduler


def _base_query(project_id: str) -> dict[str, object]:
    return {
        "project_id": project_id,
        "as_of": _iso(13, 12),
        "now": _iso(13, 12),
    }


def test_resource_schedule_cache_reuses_full_projection_across_query_types():
    calls: list[dict[str, object]] = []
    service = ProjectService(
        InMemoryProjectRepository(),
        resource_scheduler=_counting_scheduler(calls),
    )
    project_id, _process_id = _seed_project(service)

    _query(
        service,
        {
            "action": "query_resource_schedule",
            **_base_query(project_id),
            "include_allocation_slices": False,
        },
    )
    _query(service, {"action": "query_utilization", **_base_query(project_id)})
    _query(service, {"action": "query_costs", **_base_query(project_id)})
    _query(
        service,
        {
            "action": "query_process_graph",
            **_base_query(project_id),
            "include_resource_fields": True,
            "include_allocation_slices": False,
        },
    )
    _query(service, {"action": "query_agent_context", **_base_query(project_id)})

    assert len(calls) == 1
    assert calls[0]["options"]["include_allocation_slices"] is True


def test_resource_schedule_cache_clears_after_successful_mutating_command():
    calls: list[dict[str, object]] = []
    service = ProjectService(
        InMemoryProjectRepository(),
        resource_scheduler=_counting_scheduler(calls),
    )
    project_id, process_id = _seed_project(service)

    _query(service, {"action": "query_utilization", **_base_query(project_id)})
    _query(service, {"action": "query_utilization", **_base_query(project_id)})
    assert len(calls) == 1

    _handle(
        service,
        {
            "action": "set_process_status",
            "project_id": project_id,
            "process_id": process_id,
            "status": "in_progress",
            "edit_at": _iso(13, 13),
        },
    )
    _query(service, {"action": "query_utilization", **_base_query(project_id)})

    assert len(calls) == 2


def test_resource_schedule_cache_key_uses_repository_version_when_available():
    class VersionedRepository(InMemoryProjectRepository):
        def __init__(self) -> None:
            super().__init__()
            self.version = 0

        def cache_version(self, project_id: str) -> int:
            self.get_project(project_id)
            return self.version

    calls: list[dict[str, object]] = []
    repository = VersionedRepository()
    service = ProjectService(repository, resource_scheduler=_counting_scheduler(calls))
    project_id, _process_id = _seed_project(service)

    _query(service, {"action": "query_utilization", **_base_query(project_id)})
    _query(service, {"action": "query_utilization", **_base_query(project_id)})
    assert len(calls) == 1

    repository.version += 1
    _query(service, {"action": "query_utilization", **_base_query(project_id)})

    assert len(calls) == 2
