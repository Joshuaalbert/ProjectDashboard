"""Presentation adapters for the service-backed UI."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from graphviz import Digraph

GANTT_WAITING_COLOR = "#64748b"
GANTT_EARLY_START_COLOR = "#7c3aed"
GANTT_READY_COLOR = "#2563eb"
GANTT_STARTED_COLOR = "#f59e0b"
GANTT_DUE_COLOR = "#eab308"
GANTT_FINISHED_COLOR = "#16a34a"
GANTT_COMPLETEDNESS_COLORS = {
    "waiting": GANTT_WAITING_COLOR,
    "early_start": GANTT_EARLY_START_COLOR,
    "ready": GANTT_READY_COLOR,
    "started": GANTT_STARTED_COLOR,
    "due": GANTT_DUE_COLOR,
    "finished": GANTT_FINISHED_COLOR,
}
GANTT_COMPLETEDNESS_LEGEND = (
    (
        "waiting",
        "Waiting - parents unfinished, unpinned",
        GANTT_WAITING_COLOR,
    ),
    (
        "early_start",
        "Early start - pinned before parents finish",
        GANTT_EARLY_START_COLOR,
    ),
    (
        "ready",
        "Ready - parents finished, unpinned",
        GANTT_READY_COLOR,
    ),
    (
        "started",
        "Started - pinned, forecast has passed",
        GANTT_STARTED_COLOR,
    ),
    (
        "due",
        "Due - pinned before forecast finish",
        GANTT_DUE_COLOR,
    ),
    (
        "finished",
        "Finished - all roles verified done",
        GANTT_FINISHED_COLOR,
    ),
)


def as_dict(value: Any) -> dict[str, Any]:
    """Return a JSON-style dictionary for dicts or Pydantic models."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value)


def as_rows(values: list[Any] | tuple[Any, ...] | None) -> list[dict[str, Any]]:
    """Return JSON-style rows for table rendering."""
    return [as_dict(value) for value in values or []]


def process_table_rows(graph_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten process graph nodes into an operational table."""
    rows = []
    for node in graph_data.get("nodes", []):
        dependency = node.get("dependency_only") or {}
        resource = node.get("resource_aware") or {}
        blockers = node.get("blocker_summary") or {}
        rows.append(
            {
                "symbol": node.get("process_symbol"),
                "name": node.get("name"),
                "description": node.get("description"),
                "status": node.get("status"),
                "computed": node.get("computed_status"),
                "duration_hours": node.get("duration_hours"),
                "inferred_duration_hours": node.get("inferred_duration_hours")
                or resource.get("inferred_duration_hours"),
                "started_at": node.get("started_at"),
                "dep_start": dependency.get("es_at"),
                "dep_finish": dependency.get("ef_at"),
                "slack_hours": dependency.get("slack_hours"),
                "resource_start": resource.get("starts_at"),
                "resource_finish": resource.get("ends_at"),
                "allocation": resource.get("allocation_state"),
                "allocation_diagnostic": resource.get("allocation_diagnostic"),
                "blocking": blockers.get("blocking_count", 0),
                "process_id": node.get("process_id"),
            }
        )
    return rows


def blocker_table_rows(
    blockers_data: dict[str, Any],
    graph_data: dict[str, Any],
    schedule_data: dict[str, Any],
    now: dt.datetime,
    *,
    terminal_symbols: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Return blocker rows enriched with process, role, priority, and resource data."""
    node_by_id = {
        node.get("process_id"): node
        for node in graph_data.get("nodes", [])
        if node.get("process_id")
    }
    node_by_symbol = {
        node.get("process_symbol"): node
        for node in graph_data.get("nodes", [])
        if node.get("process_symbol")
    }
    priority_by_process = {
        node.get("process_id"): priority
        for node, priority in _priority_nodes(graph_data, now, terminal_symbols)
    }
    resources_by_process = _resources_by_process(schedule_data)

    rows = []
    for blocker in blockers_data.get("blockers", []):
        process_id = blocker.get("process_id")
        process_symbol = blocker.get("process_symbol")
        node = node_by_id.get(process_id) or node_by_symbol.get(process_symbol) or {}
        node_process_id = node.get("process_id") or process_id
        priority = priority_by_process.get(node_process_id, {})
        role_ids = sorted(
            {
                requirement.get("role_id")
                for requirement in node.get("role_requirements") or []
                if requirement.get("role_id")
            }
        )
        rows.append(
            {
                "blocker_id": blocker.get("blocker_id"),
                "blocker_status": _blocker_status(blocker),
                "severity": blocker.get("severity"),
                "summary": blocker.get("summary"),
                "details": blocker.get("details"),
                "resolution_owner_resource_id": blocker.get(
                    "resolution_owner_resource_id"
                ),
                "process_symbol": node.get("process_symbol") or process_symbol,
                "process_name": node.get("name"),
                "process_status": node.get("status"),
                "computed_status": node.get("computed_status"),
                "priority": priority.get("priority") or "-",
                "role_ids": ", ".join(role_ids),
                "resource_ids": ", ".join(
                    resources_by_process.get(node_process_id)
                    or resources_by_process.get(process_symbol)
                    or []
                ),
                "needed_by_role_ids": ", ".join(
                    blocker.get("needed_by_role_ids") or []
                ),
                "needed_by_resource_ids": ", ".join(
                    blocker.get("needed_by_resource_ids") or []
                ),
                "immediate_blocked_processes": blocker.get(
                    "immediate_blocked_processes",
                    [],
                ),
                "created_at": blocker.get("created_at"),
                "resolved_at": blocker.get("resolved_at"),
                "resolution": blocker.get("resolution"),
                "is_blocking_as_of": blocker.get("is_blocking_as_of"),
                "is_resolved_as_of": blocker.get("is_resolved_as_of"),
            }
        )
    return sorted(rows, key=_blocker_row_sort_key)


def edge_table_rows(graph_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten graph edges into a dependency table."""
    rows = []
    for edge in graph_data.get("edges", []):
        rows.append(
            {
                "from": edge.get("predecessor_process_symbol"),
                "to": edge.get("successor_process_symbol"),
                "type": edge.get("dependency_type"),
                "edge_id": edge.get("edge_id"),
            }
        )
    return rows


def catalog_from_query_data(*datasets: dict[str, Any] | None) -> dict[str, list[str]]:
    """Extract known ids from query projections without repository introspection."""
    process_ids: set[str] = set()
    process_symbols: set[str] = set()
    role_ids: set[str] = set()
    resource_ids: set[str] = set()
    calendar_ids: set[str] = set()
    blocker_ids: set[str] = set()
    milestone_ids: set[str] = set()

    for data in datasets:
        if not data:
            continue
        for role in data.get("roles", []):
            if role.get("role_id"):
                role_ids.add(role["role_id"])
        for resource in data.get("resources", []):
            if resource.get("resource_id"):
                resource_ids.add(resource["resource_id"])
            if resource.get("calendar_id"):
                calendar_ids.add(resource["calendar_id"])
            for override in resource.get("calendar_overrides") or []:
                if override.get("calendar_id"):
                    calendar_ids.add(override["calendar_id"])
            role_ids.update(resource.get("role_ids") or [])
        for calendar in data.get("calendars", []):
            if calendar.get("calendar_id"):
                calendar_ids.add(calendar["calendar_id"])
        for milestone in data.get("milestones", []):
            if milestone.get("milestone_id"):
                milestone_ids.add(milestone["milestone_id"])
            process_symbols.update(milestone.get("process_symbols") or [])
        for node in data.get("nodes", []):
            if node.get("process_id"):
                process_ids.add(node["process_id"])
            if node.get("process_symbol"):
                process_symbols.add(node["process_symbol"])
            for role_id in (node.get("required_roles") or {}).keys():
                role_ids.add(role_id)
            for requirement in node.get("role_requirements") or []:
                if requirement.get("role_id"):
                    role_ids.add(requirement["role_id"])
        for row in data.get("processes", []):
            if row.get("process_id"):
                process_ids.add(row["process_id"])
        for blocker in data.get("blockers", []):
            if blocker.get("blocker_id"):
                blocker_ids.add(blocker["blocker_id"])
            if blocker.get("process_id"):
                process_ids.add(blocker["process_id"])
            if blocker.get("process_symbol"):
                process_symbols.add(blocker["process_symbol"])
        for row in data.get("by_resource", []):
            if row.get("resource_id"):
                resource_ids.add(row["resource_id"])
        for row in data.get("by_role", []):
            if row.get("role_id"):
                role_ids.add(row["role_id"])
        for bucket in data.get("buckets", []):
            if bucket.get("resource_id"):
                resource_ids.add(bucket["resource_id"])
            if bucket.get("calendar_id"):
                calendar_ids.add(bucket["calendar_id"])
            role_ids.update(bucket.get("role_ids") or [])
        for slice_data in data.get("allocation_slices", []):
            if slice_data.get("resource_id"):
                resource_ids.add(slice_data["resource_id"])
            if slice_data.get("role_id"):
                role_ids.add(slice_data["role_id"])
            if slice_data.get("process_id"):
                process_ids.add(slice_data["process_id"])
    return {
        "process_ids": sorted(process_ids),
        "process_symbols": sorted(process_symbols),
        "role_ids": sorted(role_ids),
        "resource_ids": sorted(resource_ids),
        "calendar_ids": sorted(calendar_ids),
        "blocker_ids": sorted(blocker_ids),
        "milestone_ids": sorted(milestone_ids),
    }


def process_symbol_maps(
    graph_data: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    """Return process symbol/id lookup maps from graph query data."""
    id_by_symbol = {}
    symbol_by_id = {}
    for node in graph_data.get("nodes", []):
        process_id = node.get("process_id")
        symbol = node.get("process_symbol")
        if process_id and symbol:
            id_by_symbol[symbol] = process_id
            symbol_by_id[process_id] = symbol
    return id_by_symbol, symbol_by_id


def existing_dependency_symbols(
    graph_data: dict[str, Any],
    process_symbol: str,
) -> list[str]:
    """Return predecessor symbols already linked to the selected process."""
    symbols = []
    for edge in graph_data.get("edges", []):
        if edge.get("successor_process_symbol") == process_symbol:
            predecessor = edge.get("predecessor_process_symbol")
            if predecessor:
                symbols.append(predecessor)
    return sorted(dict.fromkeys(symbols))


def allowed_dependency_symbols(
    graph_data: dict[str, Any],
    process_symbol: str | None,
) -> list[str]:
    """Return symbols that can be predecessors without introducing a cycle."""
    symbols = _process_symbols_in_graph_order(graph_data)
    if not process_symbol:
        return symbols
    successors = _successors_by_symbol(graph_data)
    return [
        symbol
        for symbol in symbols
        if symbol != process_symbol
        and not _has_symbol_path(successors, process_symbol, symbol)
    ]


def allowed_shared_dependency_symbols(
    graph_data: dict[str, Any],
    process_symbols: list[str],
) -> list[str]:
    """Return predecessor symbols that are safe for every selected process."""
    selected = set(process_symbols)
    if not selected:
        return []
    symbols = _process_symbols_in_graph_order(graph_data)
    successors = _successors_by_symbol(graph_data)
    allowed = []
    for candidate in symbols:
        if candidate in selected:
            continue
        if all(
            not _has_symbol_path(successors, process_symbol, candidate)
            for process_symbol in selected
        ):
            allowed.append(candidate)
    return allowed


def allowed_successor_symbols(
    graph_data: dict[str, Any],
    predecessor_symbols: list[str],
) -> list[str]:
    """Return symbols that can safely become children of all selected symbols."""
    selected = set(predecessor_symbols)
    if not selected:
        return []
    symbols = _process_symbols_in_graph_order(graph_data)
    successors = _successors_by_symbol(graph_data)
    allowed = []
    for candidate in symbols:
        if candidate in selected:
            continue
        if all(
            not _has_symbol_path(successors, candidate, predecessor)
            for predecessor in selected
        ):
            allowed.append(candidate)
    return allowed


def ancestor_scope_symbols(
    graph_data: dict[str, Any],
    root_symbols: list[str] | tuple[str, ...],
) -> list[str]:
    """Return roots and all predecessor symbols, ordered like the graph nodes."""
    roots = {symbol for symbol in root_symbols if symbol}
    if not roots:
        return _process_symbols_in_graph_order(graph_data)
    predecessors: dict[str, set[str]] = {}
    for edge in graph_data.get("edges", []):
        predecessor = edge.get("predecessor_process_symbol")
        successor = edge.get("successor_process_symbol")
        if predecessor and successor:
            predecessors.setdefault(successor, set()).add(predecessor)

    selected = set(roots)
    stack = list(roots)
    while stack:
        current = stack.pop()
        for predecessor in predecessors.get(current, set()):
            if predecessor in selected:
                continue
            selected.add(predecessor)
            stack.append(predecessor)
    return [
        symbol
        for symbol in _process_symbols_in_graph_order(graph_data)
        if symbol in selected
    ]


def gantt_rows(
    graph_data: dict[str, Any],
    *,
    terminal_symbols: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Normalize graph nodes for schedule-window Gantt plotting."""
    scoped_symbols = set(ancestor_scope_symbols(graph_data, terminal_symbols or ()))
    rows = []
    for node in graph_data.get("nodes", []):
        symbol = node.get("process_symbol")
        if scoped_symbols and symbol not in scoped_symbols:
            continue
        dependency = node.get("dependency_only") or {}
        resource = node.get("resource_aware") or {}
        planned_start_at = _parse_datetime(
            resource.get("starts_at") or dependency.get("es_at")
        )
        planned_finish_at = _parse_datetime(
            resource.get("ends_at") or dependency.get("ef_at")
        )
        window_starts_at = _parse_datetime(
            resource.get("schedule_window_starts_at") or dependency.get("es_at")
        )
        window_ends_at = _parse_datetime(
            resource.get("schedule_window_ends_at") or dependency.get("lf_at")
        )
        buffer_hours = resource.get("schedule_buffer_hours")
        if buffer_hours is None:
            buffer_hours = dependency.get("slack_hours")
        sensitive = _is_sensitive_process(
            node,
            resource=resource,
            dependency=dependency,
        )
        pin_markers = _gantt_pin_markers(node.get("role_requirements") or [])
        rows.append(
            {
                "process_id": node.get("process_id"),
                "symbol": symbol,
                "name": node.get("name"),
                "status": node.get("status"),
                "computed_status": node.get("computed_status"),
                "started_at": _parse_datetime(node.get("started_at")),
                "finished_at": _parse_datetime(node.get("finished_at")),
                "window_starts_at": window_starts_at,
                "window_ends_at": window_ends_at,
                "planned_start_at": planned_start_at,
                "planned_finish_at": planned_finish_at,
                "es_at": window_starts_at,
                "ef_at": planned_finish_at,
                "ls_at": planned_start_at,
                "lf_at": window_ends_at,
                "resource_starts_at": planned_start_at,
                "resource_ends_at": planned_finish_at,
                "schedule_buffer_hours": buffer_hours,
                "slack_hours": buffer_hours,
                "max_makespan_sensitivity_hours": resource.get(
                    "max_makespan_sensitivity_hours"
                ),
                "sensitivity_label": resource.get("sensitivity_label"),
                "sensitive": sensitive,
                "critical": sensitive,
                "allocation_state": resource.get("allocation_state"),
                "pin_markers": pin_markers,
            }
        )
    return _topologically_order_gantt_rows(rows, graph_data)


def gantt_completedness_legend_items() -> list[dict[str, str]]:
    """Return the process-state legend items for Gantt charts."""
    return [
        {"state": state, "label": label, "color": color}
        for state, label, color in GANTT_COMPLETEDNESS_LEGEND
    ]


def _topologically_order_gantt_rows(
    rows: list[dict[str, Any]],
    graph_data: dict[str, Any],
) -> list[dict[str, Any]]:
    if len(rows) < 2:
        return rows
    row_by_symbol = {str(row.get("symbol")): row for row in rows if row.get("symbol")}
    selected_symbols = set(row_by_symbol)
    original_index = {str(row.get("symbol")): index for index, row in enumerate(rows)}
    id_to_symbol = {
        str(node.get("process_id")): str(node.get("process_symbol"))
        for node in graph_data.get("nodes", []) or []
        if node.get("process_id") and node.get("process_symbol")
    }
    successors: dict[str, set[str]] = {symbol: set() for symbol in selected_symbols}
    indegree: dict[str, int] = {symbol: 0 for symbol in selected_symbols}
    dependency_edges = _gantt_dependency_edges(graph_data, selected_symbols, id_to_symbol)
    for predecessor, successor in dependency_edges:
        if successor in successors[predecessor]:
            continue
        successors[predecessor].add(successor)
        indegree[successor] += 1

    ready = sorted(
        [symbol for symbol in selected_symbols if indegree[symbol] == 0],
        key=original_index.get,
    )
    ordered_symbols = []
    while ready:
        symbol = ready.pop(0)
        ordered_symbols.append(symbol)
        newly_ready = []
        for successor in sorted(successors[symbol], key=original_index.get):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                newly_ready.append(successor)
        if newly_ready:
            ready = newly_ready + ready
    if len(ordered_symbols) != len(selected_symbols):
        ordered = set(ordered_symbols)
        ordered_symbols.extend(
            symbol for symbol in row_by_symbol if symbol not in ordered
        )
    else:
        ordered_symbols = _compact_gantt_topological_order(
            ordered_symbols,
            dependency_edges,
            successors,
        )
    return [row_by_symbol[symbol] for symbol in ordered_symbols]


def _gantt_dependency_edges(
    graph_data: dict[str, Any],
    selected_symbols: set[str],
    id_to_symbol: dict[str, str],
) -> list[tuple[str, str]]:
    dependency_edges = []
    seen = set()
    for edge in graph_data.get("edges", []) or []:
        predecessor = edge.get("predecessor_process_symbol")
        successor = edge.get("successor_process_symbol")
        if predecessor is None:
            predecessor = id_to_symbol.get(str(edge.get("predecessor_process_id")))
        if successor is None:
            successor = id_to_symbol.get(str(edge.get("successor_process_id")))
        predecessor = str(predecessor) if predecessor else ""
        successor = str(successor) if successor else ""
        if predecessor not in selected_symbols or successor not in selected_symbols:
            continue
        if (predecessor, successor) in seen:
            continue
        dependency_edges.append((predecessor, successor))
        seen.add((predecessor, successor))
    return dependency_edges


def _compact_gantt_topological_order(
    ordered_symbols: list[str],
    dependency_edges: list[tuple[str, str]],
    successors: dict[str, set[str]],
) -> list[str]:
    if len(ordered_symbols) < 3 or not dependency_edges:
        return ordered_symbols
    descendants = _gantt_descendants(successors)
    compacted = list(ordered_symbols)
    max_passes = len(compacted) * 2
    for _ in range(max_passes):
        improved = False
        for index in range(len(compacted) - 1):
            left = compacted[index]
            right = compacted[index + 1]
            if right in descendants.get(left, set()):
                continue
            current_score = _gantt_order_score(compacted, dependency_edges)
            candidate = list(compacted)
            candidate[index], candidate[index + 1] = right, left
            candidate_score = _gantt_order_score(candidate, dependency_edges)
            if candidate_score < current_score:
                compacted = candidate
                improved = True
        if not improved:
            break
    return compacted


def _gantt_descendants(successors: dict[str, set[str]]) -> dict[str, set[str]]:
    descendants: dict[str, set[str]] = {symbol: set() for symbol in successors}

    def visit(root: str, symbol: str) -> None:
        for successor in successors.get(symbol, set()):
            if successor in descendants[root]:
                continue
            descendants[root].add(successor)
            visit(root, successor)

    for symbol in successors:
        visit(symbol, symbol)
    return descendants


def _gantt_order_score(
    ordered_symbols: list[str],
    dependency_edges: list[tuple[str, str]],
) -> int:
    position = {symbol: index for index, symbol in enumerate(ordered_symbols)}
    distance_score = sum(
        (position[successor] - position[predecessor]) ** 2
        for predecessor, successor in dependency_edges
    )
    crossing_score = _gantt_dependency_crossing_count(position, dependency_edges)
    return distance_score + (20 * crossing_score)


def _gantt_dependency_crossing_count(
    position: dict[str, int],
    dependency_edges: list[tuple[str, str]],
) -> int:
    crossings = 0
    for index, (first_parent, first_child) in enumerate(dependency_edges):
        for second_parent, second_child in dependency_edges[index + 1 :]:
            if len({first_parent, first_child, second_parent, second_child}) < 4:
                continue
            parent_order = position[first_parent] - position[second_parent]
            child_order = position[first_child] - position[second_child]
            if parent_order * child_order < 0:
                crossings += 1
    return crossings


def _gantt_pin_markers(
    role_requirements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    markers = []
    for requirement in role_requirements:
        if not isinstance(requirement, dict):
            continue
        for pin in requirement.get("pins") or []:
            if not isinstance(pin, dict):
                continue
            started_at = _parse_datetime(
                pin.get("pinned_started_at")
                or pin.get("pinned_at")
                or pin.get("starts_at")
            )
            if started_at is not None:
                markers.append(
                    {
                        "kind": "pin_start",
                        "at": started_at,
                        "requirement_id": requirement.get("requirement_id")
                        or pin.get("requirement_id"),
                        "resource_id": pin.get("resource_id"),
                    }
                )
            finished_at = _parse_datetime(
                pin.get("verified_finished_at")
                or pin.get("verified_done_at")
                or pin.get("ends_at")
            )
            if finished_at is not None:
                markers.append(
                    {
                        "kind": "pin_finish",
                        "at": finished_at,
                        "requirement_id": requirement.get("requirement_id")
                        or pin.get("requirement_id"),
                        "resource_id": pin.get("resource_id"),
                    }
                )
    return sorted(markers, key=lambda item: (item["at"], item["kind"]))


def gantt_bar_color(row: dict[str, Any], now: dt.datetime | None) -> str:
    """Return the completedness-state color for a Gantt row."""
    del now
    computed_status = str(row.get("computed_status") or "").strip().lower()
    started_at = _parse_datetime(row.get("started_at"))

    if computed_status == "finished":
        return GANTT_FINISHED_COLOR

    if computed_status in GANTT_COMPLETEDNESS_COLORS:
        return GANTT_COMPLETEDNESS_COLORS[computed_status]

    if started_at is not None:
        return GANTT_STARTED_COLOR

    return GANTT_WAITING_COLOR


def resource_utilization_heatmap(
    utilization_data: dict[str, Any],
    schedule_data: dict[str, Any] | None = None,
    *,
    now: dt.datetime | None = None,
) -> tuple[list[str], list[dt.datetime], list[list[float]]]:
    """Return resource utilization matrix as labels, bucket starts, values."""
    span = schedule_time_span(schedule_data or {})
    labels = sorted(
        {
            row["resource_id"]
            for row in utilization_data.get("time_series", [])
            if row.get("resource_id") and _bucket_overlaps_window(row, span, now)
        }
    )
    times = sorted(
        {
            _parse_datetime(row.get("starts_at"))
            for row in utilization_data.get("time_series", [])
            if row.get("starts_at") and _bucket_overlaps_window(row, span, now)
        }
    )
    matrix = [[0.0 for _time in times] for _label in labels]
    label_index = {label: index for index, label in enumerate(labels)}
    time_index = {time: index for index, time in enumerate(times)}
    for row in utilization_data.get("time_series", []):
        label = row.get("resource_id")
        starts_at = _parse_datetime(row.get("starts_at"))
        if not _bucket_overlaps_window(row, span, now):
            continue
        if label not in label_index or starts_at not in time_index:
            continue
        current_value = matrix[label_index[label]][time_index[starts_at]]
        utilization_ratio = float(row.get("utilization_ratio") or 0)
        matrix[label_index[label]][time_index[starts_at]] = max(
            current_value,
            utilization_ratio,
        )
    return labels, times, matrix


def role_utilization_heatmap(
    utilization_data: dict[str, Any],
    schedule_data: dict[str, Any],
    *,
    now: dt.datetime | None = None,
) -> tuple[list[str], list[dt.datetime], list[list[float]]]:
    """Return role utilization matrix from utilization buckets and allocations."""
    span = schedule_time_span(schedule_data)
    labels = sorted(
        {
            role_id
            for bucket in utilization_data.get("time_series", [])
            if _bucket_overlaps_window(bucket, span, now)
            for role_id in bucket.get("role_ids", [])
        }
        | {
            slice_data["role_id"]
            for slice_data in schedule_data.get("allocation_slices", [])
            if slice_data.get("role_id")
            and _allocation_slice_overlaps_window(slice_data, now)
        }
    )
    times = sorted(
        {
            _parse_datetime(bucket.get("starts_at"))
            for bucket in utilization_data.get("time_series", [])
            if bucket.get("starts_at") and _bucket_overlaps_window(bucket, span, now)
        }
    )
    capacity = {
        (role_id, time): 0.0
        for role_id in labels
        for time in times
    }
    allocated = {
        (role_id, time): 0.0
        for role_id in labels
        for time in times
    }
    for bucket in utilization_data.get("time_series", []):
        starts_at = _parse_datetime(bucket.get("starts_at"))
        if not _bucket_overlaps_window(bucket, span, now):
            continue
        if starts_at not in times:
            continue
        for role_id in bucket.get("role_ids", []):
            capacity[(role_id, starts_at)] += float(bucket.get("capacity_hours") or 0)
    for slice_data in schedule_data.get("allocation_slices", []):
        role_id = slice_data.get("role_id")
        if role_id not in labels:
            continue
        if not _allocation_slice_overlaps_window(slice_data, now):
            continue
        slice_start = _parse_datetime(slice_data.get("starts_at"))
        slice_end = _parse_datetime(slice_data.get("ends_at"))
        if slice_start is None or slice_end is None:
            continue
        for time in times:
            next_time = time + dt.timedelta(hours=1)
            overlap = _overlap_hours(time, next_time, slice_start, slice_end)
            if overlap > 0:
                allocated[(role_id, time)] += overlap
    matrix = []
    for role_id in labels:
        row = []
        for time in times:
            capacity_hours = capacity[(role_id, time)]
            value = (
                allocated[(role_id, time)] / capacity_hours
                if capacity_hours
                else 0.0
            )
            row.append(value)
        matrix.append(row)
    return labels, times, matrix


def _is_sensitive_process(
    node: dict[str, Any],
    *,
    resource: dict[str, Any],
    dependency: dict[str, Any],
) -> bool:
    sensitivity = _float_or_none(resource.get("max_makespan_sensitivity_hours"))
    if sensitivity is None:
        sensitivity = _float_or_none(dependency.get("max_makespan_sensitivity_hours"))
    if sensitivity is not None:
        return sensitivity > 0
    if resource.get("sensitivity_label") == "makespan_sensitive":
        return True
    if dependency.get("sensitivity_label") == "makespan_sensitive":
        return True
    legacy_ids = node.get("critical_path_process_ids")
    if isinstance(legacy_ids, (list, tuple, set)) and node.get("process_id") in legacy_ids:
        return True
    return (
        resource.get("criticality_label") == "critical"
        or dependency.get("criticality_label") == "critical"
    )


def _resources_by_process(schedule_data: dict[str, Any]) -> dict[str, list[str]]:
    resources: dict[str, set[str]] = {}
    for slice_data in schedule_data.get("allocation_slices", []):
        process_id = slice_data.get("process_id")
        resource_id = slice_data.get("resource_id")
        if process_id and resource_id:
            resources.setdefault(process_id, set()).add(resource_id)
    return {
        process_id: sorted(resource_ids)
        for process_id, resource_ids in resources.items()
    }


def _blocker_row_sort_key(row: dict[str, Any]) -> tuple[int, int, str, str, str]:
    if row.get("is_blocking_as_of"):
        state_rank = 0
    elif not row.get("is_resolved_as_of"):
        state_rank = 1
    else:
        state_rank = 2
    priority = str(row.get("priority") or "P9")
    return (
        state_rank,
        _priority_rank(priority),
        str(row.get("process_symbol") or ""),
        str(row.get("created_at") or ""),
        str(row.get("blocker_id") or ""),
    )


def _blocker_status(blocker: dict[str, Any]) -> str:
    if blocker.get("is_resolved_as_of"):
        return "resolved"
    if blocker.get("is_blocking_as_of"):
        return "blocking"
    return "open"


def _priority_rank(priority: str) -> int:
    if priority.startswith("P") and priority[1:].isdigit():
        return int(priority[1:])
    return 9


def schedule_time_span(
    schedule_data: dict[str, Any],
) -> tuple[dt.datetime, dt.datetime] | None:
    """Return the actual scheduled work span from allocation slices or rows."""
    starts: list[dt.datetime] = []
    ends: list[dt.datetime] = []
    for slice_data in schedule_data.get("allocation_slices", []):
        starts_at = _parse_datetime(slice_data.get("starts_at"))
        ends_at = _parse_datetime(slice_data.get("ends_at"))
        if starts_at is not None and ends_at is not None and ends_at > starts_at:
            starts.append(starts_at)
            ends.append(ends_at)
    for row in schedule_data.get("processes", []):
        starts_at = _parse_datetime(row.get("starts_at"))
        ends_at = _parse_datetime(row.get("ends_at"))
        if starts_at is not None and ends_at is not None and ends_at > starts_at:
            starts.append(starts_at)
            ends.append(ends_at)
    if not starts or not ends:
        return None
    return min(starts), max(ends)


def _bucket_overlaps_span(
    bucket: dict[str, Any],
    span: tuple[dt.datetime, dt.datetime] | None,
) -> bool:
    if span is None:
        return True
    starts_at = _parse_datetime(bucket.get("starts_at"))
    ends_at = _parse_datetime(bucket.get("ends_at"))
    if starts_at is None or ends_at is None:
        return False
    span_start, span_end = span
    return starts_at < span_end and ends_at > span_start


def _bucket_overlaps_window(
    bucket: dict[str, Any],
    span: tuple[dt.datetime, dt.datetime] | None,
    now: dt.datetime | None,
) -> bool:
    if not _bucket_overlaps_span(bucket, span):
        return False
    if now is None:
        return True
    ends_at = _parse_datetime(bucket.get("ends_at"))
    return ends_at is not None and ends_at > now


def _allocation_slice_overlaps_window(
    slice_data: dict[str, Any],
    now: dt.datetime | None,
) -> bool:
    if now is None:
        return True
    ends_at = _parse_datetime(slice_data.get("ends_at"))
    return ends_at is not None and ends_at > now


def aggregate_process_properties(
    graph_data: dict[str, Any],
    process_symbols: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Aggregate editable properties for a selected process set."""
    selected = {symbol for symbol in process_symbols if symbol}
    nodes = [
        node
        for node in graph_data.get("nodes", [])
        if node.get("process_symbol") in selected
    ]
    role_efforts: dict[str, float] = {}
    blocker_ids: set[str] = set()
    statuses = set()
    started_values = set()
    finished_values = set()
    earliest_values = set()
    for node in nodes:
        statuses.add(node.get("status"))
        started_values.add(node.get("started_at"))
        finished_values.add(node.get("finished_at"))
        earliest_values.add(node.get("earliest_start_at"))
        blocker_summary = node.get("blocker_summary") or {}
        blocker_ids.update(blocker_summary.get("blocker_ids") or [])
        for requirement in node.get("role_requirements") or []:
            role_id = requirement.get("role_id")
            if not role_id:
                continue
            role_efforts[role_id] = role_efforts.get(role_id, 0.0) + float(
                requirement.get("effort_hours") or 0.0
            )

    predecessors = set()
    children = set()
    for edge in graph_data.get("edges", []):
        predecessor = edge.get("predecessor_process_symbol")
        successor = edge.get("successor_process_symbol")
        if successor in selected and predecessor and predecessor not in selected:
            predecessors.add(predecessor)
        if predecessor in selected and successor and successor not in selected:
            children.add(successor)

    return {
        "process_symbols": [symbol for symbol in process_symbols if symbol in selected],
        "predecessors": sorted(predecessors),
        "children": sorted(children),
        "role_efforts": dict(sorted(role_efforts.items())),
        "status": statuses.pop() if len(statuses) == 1 else "",
        "name": nodes[0].get("name") if len(nodes) == 1 else "",
        "description": nodes[0].get("description") if len(nodes) == 1 else "",
        "earliest_start_at": earliest_values.pop() if len(earliest_values) == 1 else None,
        "started_at": started_values.pop() if len(started_values) == 1 else None,
        "finished_at": finished_values.pop() if len(finished_values) == 1 else None,
        "blocker_ids": sorted(blocker_ids),
    }


def role_priority_rows(
    graph_data: dict[str, Any],
    now: dt.datetime,
    *,
    terminal_symbols: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Return role-scoped process priorities from planned schedule windows."""
    rows = []
    for node, priority in _priority_nodes(graph_data, now, terminal_symbols):
        for requirement in node.get("role_requirements") or []:
            role_id = requirement.get("role_id")
            if not role_id:
                continue
            rows.append(
                {
                    **priority,
                    "role_id": role_id,
                    "effort_hours": float(requirement.get("effort_hours") or 0.0),
                }
            )
    return _sort_priority_rows(rows)


def resource_priority_rows(
    graph_data: dict[str, Any],
    schedule_data: dict[str, Any],
    now: dt.datetime,
    *,
    terminal_symbols: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Return resource-scoped priorities when allocation slices assign work."""
    priority_by_process = {
        node.get("process_id"): priority
        for node, priority in _priority_nodes(graph_data, now, terminal_symbols)
    }
    node_by_process = {
        node.get("process_id"): node
        for node in graph_data.get("nodes", []) or []
        if node.get("process_id")
    }
    assignments: dict[tuple[str, str], dict[str, Any]] = {}
    for slice_data in schedule_data.get("allocation_slices", []):
        process_id = slice_data.get("process_id")
        resource_id = slice_data.get("resource_id")
        if process_id not in priority_by_process or not resource_id:
            continue
        key = (resource_id, process_id)
        assignment = assignments.setdefault(
            key,
            {
                **priority_by_process[process_id],
                "resource_id": resource_id,
                "effort_hours": 0.0,
                "role_ids": set(),
            },
        )
        assignment["effort_hours"] += float(slice_data.get("effort_hours") or 0.0)
        role_id = slice_data.get("role_id")
        if role_id:
            assignment["role_ids"].add(role_id)

    for process_id, priority in priority_by_process.items():
        node = node_by_process.get(process_id) or {}
        for requirement in node.get("role_requirements") or []:
            role_id = requirement.get("role_id")
            if not role_id:
                continue
            active_pins = [
                pin
                for pin in requirement.get("pins", []) or []
                if isinstance(pin, dict)
                and pin.get("status") == "pinned_started"
            ]
            for pin in active_pins:
                resource_id = pin.get("resource_id")
                if not resource_id:
                    continue
                key = (str(resource_id), str(process_id))
                assignment = assignments.setdefault(
                    key,
                    {
                        **priority,
                        "resource_id": str(resource_id),
                        "effort_hours": float(
                            requirement.get("effort_hours") or 0.0,
                        ),
                        "role_ids": set(),
                    },
                )
                assignment["role_ids"].add(str(role_id))
                assignment["active_pin"] = True
                assignment["pin_id"] = pin.get("pin_id")
                assignment["pin_status"] = pin.get("status")
                assignment["pin_started_at"] = pin.get("pinned_at") or pin.get("starts_at")
                assignment["pin_forecast_finish_at"] = pin.get("forecast_finish_at")
                assignment["pin_verified_done_at"] = pin.get("verified_done_at")
                assignment["pin_overdue"] = bool(pin.get("overdue"))

    rows = []
    for assignment in assignments.values():
        rows.append(
            {
                **assignment,
                "role_ids": ", ".join(sorted(assignment["role_ids"])),
            }
        )
    return _sort_priority_rows(rows)


def build_process_graph_dot(
    graph_data: dict[str, Any],
    *,
    collapsed_process_ids: set[str] | None = None,
) -> str:
    """Build a Graphviz DOT process graph with sensitivity styling."""
    collapsed_process_ids = collapsed_process_ids or set()
    nodes = {node.get("process_id"): node for node in graph_data.get("nodes", [])}
    sensitive_process_ids = {
        process_id
        for process_id, node in nodes.items()
        if process_id
        and _is_sensitive_process(
            node,
            resource=node.get("resource_aware") or {},
            dependency=node.get("dependency_only") or {},
        )
    }
    dot = Digraph("process_graph")
    dot.attr("graph", rankdir="LR", bgcolor="transparent", pad="0.2")
    dot.attr("node", shape="box", style="rounded,filled", fontname="Helvetica")
    dot.attr("edge", fontname="Helvetica", color="#6b7280")

    for process_id, node in nodes.items():
        if not process_id:
            continue
        is_sensitive = process_id in sensitive_process_ids
        status = str(node.get("computed_status") or node.get("status") or "planned")
        fill = _node_fill(status)
        border = "#dc2626" if is_sensitive else "#64748b"
        penwidth = "3" if is_sensitive else "1.4"
        if process_id in collapsed_process_ids:
            fill = "#f3f4f6"
            penwidth = "2"
        label = _node_label(
            node,
            collapsed=process_id in collapsed_process_ids,
            sensitive=is_sensitive,
        )
        dot.node(
            process_id,
            label=label,
            fillcolor=fill,
            color=border,
            penwidth=penwidth,
        )

    for edge in graph_data.get("edges", []):
        predecessor = edge.get("predecessor_process_id")
        successor = edge.get("successor_process_id")
        if predecessor not in nodes or successor not in nodes:
            continue
        is_sensitive_edge = (
            predecessor in sensitive_process_ids and successor in sensitive_process_ids
        )
        dot.edge(
            predecessor,
            successor,
            color="#dc2626" if is_sensitive_edge else "#94a3b8",
            penwidth="2.4" if is_sensitive_edge else "1.2",
        )

    return dot.source


def cost_time_series_rows(cost_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize cost bucket rows for charts and tables."""
    rows = []
    for bucket in cost_data.get("time_series", []):
        rows.append(
            {
                "starts_at": bucket.get("starts_at"),
                "ends_at": bucket.get("ends_at"),
                "resource_id": bucket.get("resource_id"),
                "process_id": bucket.get("process_id"),
                "role_id": bucket.get("role_id"),
                "allocated_hours": bucket.get("allocated_hours"),
                "currency": bucket.get("currency"),
                "cost_amount": float(Decimal(str(bucket.get("cost_amount", "0")))),
            }
        )
    return rows


def _node_fill(status: str) -> str:
    if status in {"finished", "complete", "done"}:
        return "#dcfce7"
    if status == "early_start":
        return "#ede9fe"
    if status in {"started", "due"}:
        return "#fef3c7"
    if status == "ready":
        return "#dbeafe"
    if status == "waiting":
        return "#f1f5f9"
    if status in {"canceled", "cancelled"}:
        return "#e5e7eb"
    return "#f8fafc"


def _node_label(node: dict[str, Any], *, collapsed: bool, sensitive: bool) -> str:
    symbol = node.get("process_symbol") or node.get("process_id")
    name = node.get("name") or ""
    status = node.get("computed_status") or node.get("status") or ""
    timing = _node_timing_label(node, sensitive=sensitive)
    prefix = "[+]" if collapsed else ""
    label = f"{prefix}{symbol}\\n{name}\\n{status}\\n{timing}"
    return label[:180]


def _node_timing_label(node: dict[str, Any], *, sensitive: bool) -> str:
    resource = node.get("resource_aware") or {}
    dependency = node.get("dependency_only") or {}
    duration_hours = _schedule_window_hours(
        resource,
        dependency,
        start_fields=("starts_at", "es_at"),
        end_fields=("ends_at", "ef_at"),
    )
    buffer_hours = _first_number(
        resource,
        dependency,
        ("schedule_buffer_hours", "slack_hours"),
    )
    sensitivity_hours = _first_number(
        resource,
        dependency,
        ("max_makespan_sensitivity_hours",),
    )
    if sensitive:
        return (
            f"duration: {_format_hours(duration_hours)}; "
            f"sensitivity: {_format_hours(sensitivity_hours)}"
        )
    return (
        f"duration: {_format_hours(duration_hours)}; "
        f"buffer: {_format_hours(buffer_hours)}"
    )


def _schedule_window_hours(
    resource: dict[str, Any],
    dependency: dict[str, Any],
    *,
    start_fields: tuple[str, ...],
    end_fields: tuple[str, ...],
) -> float | None:
    starts_at = _first_datetime(resource, dependency, start_fields)
    ends_at = _first_datetime(resource, dependency, end_fields)
    if starts_at is None or ends_at is None or ends_at <= starts_at:
        return None
    return (ends_at - starts_at).total_seconds() / 3600


def _first_datetime(
    resource: dict[str, Any],
    dependency: dict[str, Any],
    fields: tuple[str, ...],
) -> dt.datetime | None:
    for field in fields:
        for source in (resource, dependency):
            value = _parse_datetime(source.get(field))
            if value is not None:
                return value
    return None


def _first_number(
    resource: dict[str, Any],
    dependency: dict[str, Any],
    fields: tuple[str, ...],
) -> float | None:
    for field in fields:
        for source in (resource, dependency):
            value = _float_or_none(source.get(field))
            if value is not None:
                return value
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_hours(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{float(value):g}h"


def _process_symbols_in_graph_order(graph_data: dict[str, Any]) -> list[str]:
    symbols = [
        node.get("process_symbol")
        for node in graph_data.get("nodes", [])
        if node.get("process_symbol")
    ]
    return list(dict.fromkeys(symbols))


def _successors_by_symbol(graph_data: dict[str, Any]) -> dict[str, set[str]]:
    successors = {symbol: set() for symbol in _process_symbols_in_graph_order(graph_data)}
    for edge in graph_data.get("edges", []):
        predecessor = edge.get("predecessor_process_symbol")
        successor = edge.get("successor_process_symbol")
        if predecessor and successor:
            successors.setdefault(predecessor, set()).add(successor)
            successors.setdefault(successor, set())
    return successors


def _has_symbol_path(
    successors: dict[str, set[str]],
    start_symbol: str,
    end_symbol: str,
) -> bool:
    stack = list(successors.get(start_symbol, set()))
    seen = set()
    while stack:
        current = stack.pop()
        if current == end_symbol:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(successors.get(current, set()))
    return False


def _parse_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    return dt.datetime.fromisoformat(text)


def _overlap_hours(
    starts_at: dt.datetime,
    ends_at: dt.datetime,
    other_starts_at: dt.datetime,
    other_ends_at: dt.datetime,
) -> float:
    latest_start = max(starts_at, other_starts_at)
    earliest_end = min(ends_at, other_ends_at)
    if earliest_end <= latest_start:
        return 0.0
    return (earliest_end - latest_start).total_seconds() / 3600


def _priority_nodes(
    graph_data: dict[str, Any],
    now: dt.datetime,
    terminal_symbols: list[str] | tuple[str, ...] | None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    scoped_symbols = set(ancestor_scope_symbols(graph_data, terminal_symbols or ()))
    rows = []
    for node in graph_data.get("nodes", []):
        symbol = node.get("process_symbol")
        if scoped_symbols and symbol not in scoped_symbols:
            continue
        status = str(node.get("computed_status") or node.get("status") or "")
        if status in {"done", "finished", "canceled"}:
            continue
        dependency = node.get("dependency_only") or {}
        resource = node.get("resource_aware") or {}
        planned_start_at = _parse_datetime(
            resource.get("starts_at") or dependency.get("es_at")
        )
        planned_finish_at = _parse_datetime(
            resource.get("ends_at") or dependency.get("ef_at")
        )
        schedule_window_starts_at = _parse_datetime(
            resource.get("schedule_window_starts_at")
            or dependency.get("schedule_window_starts_at")
            or resource.get("es_at")
            or dependency.get("es_at")
        )
        schedule_window_ends_at = _parse_datetime(
            resource.get("schedule_window_ends_at")
            or dependency.get("schedule_window_ends_at")
            or resource.get("lf_at")
            or dependency.get("lf_at")
        )
        if planned_start_at is None or planned_finish_at is None:
            continue
        time_until_start = planned_start_at - now
        if status in {"started", "due", "early_start"}:
            priority = "P0"
            priority_rank = 0
        elif time_until_start < dt.timedelta(days=3):
            priority = "P0"
            priority_rank = 0
        elif time_until_start < dt.timedelta(days=7):
            priority = "P1"
            priority_rank = 1
        elif time_until_start < dt.timedelta(days=14):
            priority = "P2"
            priority_rank = 2
        else:
            priority = "P3"
            priority_rank = 3
        rows.append(
            (
                node,
                {
                    "priority": priority,
                    "priority_rank": priority_rank,
                    "process_id": node.get("process_id"),
                    "process_symbol": symbol,
                    "process_name": node.get("name"),
                    "planned_start_at": planned_start_at,
                    "planned_finish_at": planned_finish_at,
                    "schedule_window_starts_at": schedule_window_starts_at,
                    "schedule_window_ends_at": schedule_window_ends_at,
                    "hours_until_planned_start": (
                        planned_start_at - now
                    ).total_seconds() / 3600,
                    "hours_until_planned_finish": (
                        planned_finish_at - now
                    ).total_seconds() / 3600,
                    "schedule_buffer_hours": resource.get("schedule_buffer_hours"),
                    "max_makespan_sensitivity_hours": resource.get(
                        "max_makespan_sensitivity_hours"
                    ),
                    "sensitivity_label": resource.get("sensitivity_label"),
                },
            )
        )
    return rows


def _sort_priority_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            row["priority_rank"],
            row["hours_until_planned_start"],
            str(row.get("role_id") or row.get("resource_id") or ""),
            str(row.get("process_symbol") or ""),
        ),
    )
