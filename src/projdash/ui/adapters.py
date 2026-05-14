"""Presentation adapters for the service-backed UI."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from graphviz import Digraph


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
                "started_at": node.get("started_at"),
                "dep_start": dependency.get("es_at"),
                "dep_finish": dependency.get("ef_at"),
                "slack_hours": dependency.get("slack_hours"),
                "resource_start": resource.get("starts_at"),
                "resource_finish": resource.get("ends_at"),
                "allocation": resource.get("allocation_state"),
                "blocking": blockers.get("blocking_count", 0),
                "process_id": node.get("process_id"),
            }
        )
    return rows


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
            role_ids.update(resource.get("role_ids") or [])
        for calendar in data.get("calendars", []):
            if calendar.get("calendar_id"):
                calendar_ids.add(calendar["calendar_id"])
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
        for row in data.get("unallocated_requirements", []):
            if row.get("role_id"):
                role_ids.add(row["role_id"])
            if row.get("process_id"):
                process_ids.add(row["process_id"])
            resource_ids.update(row.get("eligible_resource_ids") or [])

    return {
        "process_ids": sorted(process_ids),
        "process_symbols": sorted(process_symbols),
        "role_ids": sorted(role_ids),
        "resource_ids": sorted(resource_ids),
        "calendar_ids": sorted(calendar_ids),
        "blocker_ids": sorted(blocker_ids),
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


def auto_horizon_from_graph(
    project_data: dict[str, Any],
    graph_data: dict[str, Any],
    as_of: dt.datetime,
    *,
    terminal_symbols: list[str] | tuple[str, ...] | None = None,
) -> tuple[dt.datetime, dt.datetime]:
    """Infer a query horizon from project dates and selected graph endpoints."""
    scoped_symbols = set(ancestor_scope_symbols(graph_data, terminal_symbols or ()))
    project = project_data.get("project", project_data)
    candidates = [_parse_datetime(project.get("start_at")), as_of]
    finish_candidates = [as_of + dt.timedelta(hours=1)]

    for node in graph_data.get("nodes", []):
        if scoped_symbols and node.get("process_symbol") not in scoped_symbols:
            continue
        dependency = node.get("dependency_only") or {}
        resource = node.get("resource_aware") or {}
        for key in ("ls_at",):
            candidates.append(_parse_datetime(resource.get(key)))
        for key in ("lf_at",):
            finish_candidates.append(_parse_datetime(resource.get(key)))
        for key in ("es_at", "ls_at"):
            candidates.append(_parse_datetime(dependency.get(key)))
        for key in ("ready_at", "starts_at"):
            candidates.append(_parse_datetime(resource.get(key)))
        for key in ("ef_at", "lf_at"):
            finish_candidates.append(_parse_datetime(dependency.get(key)))
        for key in ("ends_at",):
            finish_candidates.append(_parse_datetime(resource.get(key)))
        for key in ("finished_at", "earliest_start_at"):
            value = _parse_datetime(node.get(key))
            candidates.append(value)
            finish_candidates.append(value)

    start = min(value for value in candidates if value is not None)
    finish = max(value for value in finish_candidates if value is not None)
    horizon_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon_end = (finish + dt.timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if horizon_end <= horizon_start:
        horizon_end = horizon_start + dt.timedelta(days=1)
    return horizon_start, horizon_end


def gantt_rows(
    graph_data: dict[str, Any],
    *,
    terminal_symbols: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Normalize graph nodes for ES/LS/EF/LF Gantt plotting."""
    scoped_symbols = set(ancestor_scope_symbols(graph_data, terminal_symbols or ()))
    critical_ids = set(graph_data.get("critical_path_process_ids") or [])
    rows = []
    for node in graph_data.get("nodes", []):
        symbol = node.get("process_symbol")
        if scoped_symbols and symbol not in scoped_symbols:
            continue
        dependency = node.get("dependency_only") or {}
        resource = node.get("resource_aware") or {}
        es_at = _parse_datetime(resource.get("es_at") or resource.get("starts_at"))
        ef_at = _parse_datetime(resource.get("ef_at") or resource.get("ends_at"))
        ls_at = _parse_datetime(resource.get("ls_at"))
        lf_at = _parse_datetime(resource.get("lf_at"))
        if es_at is None:
            es_at = _parse_datetime(dependency.get("es_at"))
        if ef_at is None:
            ef_at = _parse_datetime(dependency.get("ef_at"))
        if ls_at is None:
            ls_at = _parse_datetime(dependency.get("ls_at"))
        if lf_at is None:
            lf_at = _parse_datetime(dependency.get("lf_at"))
        rows.append(
            {
                "process_id": node.get("process_id"),
                "symbol": symbol,
                "name": node.get("name"),
                "es_at": es_at,
                "ef_at": ef_at,
                "ls_at": ls_at,
                "lf_at": lf_at,
                "resource_starts_at": _parse_datetime(resource.get("starts_at")),
                "resource_ends_at": _parse_datetime(resource.get("ends_at")),
                "slack_hours": (
                    resource.get("slack_hours")
                    if resource.get("slack_hours") is not None
                    else dependency.get("slack_hours")
                ),
                "critical": node.get("process_id") in critical_ids
                or resource.get("criticality_label") == "critical"
                or dependency.get("criticality_label") == "critical",
                "allocation_state": resource.get("allocation_state"),
            }
        )
    return rows


def resource_utilization_heatmap(
    utilization_data: dict[str, Any],
) -> tuple[list[str], list[dt.datetime], list[list[float]]]:
    """Return resource utilization matrix as labels, bucket starts, values."""
    labels = sorted(
        {
            row["resource_id"]
            for row in utilization_data.get("time_series", [])
            if row.get("resource_id")
        }
    )
    times = sorted(
        {
            _parse_datetime(row.get("starts_at"))
            for row in utilization_data.get("time_series", [])
            if row.get("starts_at")
        }
    )
    matrix = [[0.0 for _time in times] for _label in labels]
    label_index = {label: index for index, label in enumerate(labels)}
    time_index = {time: index for index, time in enumerate(times)}
    for row in utilization_data.get("time_series", []):
        label = row.get("resource_id")
        starts_at = _parse_datetime(row.get("starts_at"))
        if label not in label_index or starts_at not in time_index:
            continue
        matrix[label_index[label]][time_index[starts_at]] = max(
            matrix[label_index[label]][time_index[starts_at]],
            float(row.get("utilization_ratio") or 0),
        )
    return labels, times, matrix


def role_utilization_heatmap(
    capacity_data: dict[str, Any],
    schedule_data: dict[str, Any],
) -> tuple[list[str], list[dt.datetime], list[list[float]]]:
    """Return role utilization matrix from capacity buckets and allocations."""
    labels = sorted(
        {
            role_id
            for bucket in capacity_data.get("buckets", [])
            for role_id in bucket.get("role_ids", [])
        }
        | {
            slice_data["role_id"]
            for slice_data in schedule_data.get("allocation_slices", [])
            if slice_data.get("role_id")
        }
    )
    times = sorted(
        {
            _parse_datetime(bucket.get("starts_at"))
            for bucket in capacity_data.get("buckets", [])
            if bucket.get("starts_at")
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
    for bucket in capacity_data.get("buckets", []):
        starts_at = _parse_datetime(bucket.get("starts_at"))
        if starts_at not in times:
            continue
        for role_id in bucket.get("role_ids", []):
            capacity[(role_id, starts_at)] += float(bucket.get("capacity_hours") or 0)
    for slice_data in schedule_data.get("allocation_slices", []):
        role_id = slice_data.get("role_id")
        if role_id not in labels:
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
            value = allocated[(role_id, time)] / capacity_hours if capacity_hours else 0
            row.append(value)
        matrix.append(row)
    return labels, times, matrix


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
    """Return role-scoped process priorities from ES/LS/LF schedule windows."""
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
                "allocated_hours": 0.0,
                "role_ids": set(),
            },
        )
        assignment["allocated_hours"] += float(slice_data.get("effort_hours") or 0.0)
        role_id = slice_data.get("role_id")
        if role_id:
            assignment["role_ids"].add(role_id)

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
    """Build a Graphviz DOT process graph with critical-path styling."""
    collapsed_process_ids = collapsed_process_ids or set()
    critical_ids = set(graph_data.get("critical_path_process_ids") or [])
    nodes = {node.get("process_id"): node for node in graph_data.get("nodes", [])}
    dot = Digraph("process_graph")
    dot.attr("graph", rankdir="LR", bgcolor="transparent", pad="0.2")
    dot.attr("node", shape="box", style="rounded,filled", fontname="Helvetica")
    dot.attr("edge", fontname="Helvetica", color="#6b7280")

    for process_id, node in nodes.items():
        if not process_id:
            continue
        status = str(node.get("computed_status") or node.get("status") or "planned")
        fill = _node_fill(status)
        border = "#dc2626" if process_id in critical_ids else "#64748b"
        penwidth = "3" if process_id in critical_ids else "1.4"
        if process_id in collapsed_process_ids:
            fill = "#f3f4f6"
            penwidth = "2"
        label = _node_label(node, collapsed=process_id in collapsed_process_ids)
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
        is_critical_edge = predecessor in critical_ids and successor in critical_ids
        dot.edge(
            predecessor,
            successor,
            color="#dc2626" if is_critical_edge else "#94a3b8",
            penwidth="2.4" if is_critical_edge else "1.2",
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
    if status in {"complete", "done"}:
        return "#dcfce7"
    if status in {"blocked", "blocked_zero_capacity"}:
        return "#ffedd5"
    if status in {"late_risk", "late"}:
        return "#fee2e2"
    if status == "work_now":
        return "#dbeafe"
    if status in {"canceled", "cancelled"}:
        return "#e5e7eb"
    return "#f8fafc"


def _node_label(node: dict[str, Any], *, collapsed: bool) -> str:
    symbol = node.get("process_symbol") or node.get("process_id")
    name = node.get("name") or ""
    status = node.get("computed_status") or node.get("status") or ""
    prefix = "[+]" if collapsed else ""
    label = f"{prefix}{symbol}\\n{name}\\n{status}"
    return label[:180]


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
        if node.get("status") in {"done", "canceled"}:
            continue
        dependency = node.get("dependency_only") or {}
        resource = node.get("resource_aware") or {}
        es_at = _parse_datetime(resource.get("es_at") or resource.get("starts_at"))
        ls_at = _parse_datetime(resource.get("ls_at"))
        lf_at = _parse_datetime(resource.get("lf_at"))
        if es_at is None:
            es_at = _parse_datetime(dependency.get("es_at"))
        if ls_at is None:
            ls_at = _parse_datetime(dependency.get("ls_at"))
        if lf_at is None:
            lf_at = _parse_datetime(dependency.get("lf_at"))
        if es_at is None or ls_at is None or lf_at is None:
            continue
        if now >= lf_at:
            priority = "P0"
            priority_rank = 0
        elif now >= ls_at:
            priority = "P1"
            priority_rank = 1
        elif now >= es_at:
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
                    "process_symbol": symbol,
                    "process_name": node.get("name"),
                    "es_at": es_at,
                    "ls_at": ls_at,
                    "lf_at": lf_at,
                    "hours_until_lf": (lf_at - now).total_seconds() / 3600,
                },
            )
        )
    return rows


def _sort_priority_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            row["priority_rank"],
            row["hours_until_lf"],
            str(row.get("role_id") or row.get("resource_id") or ""),
            str(row.get("process_symbol") or ""),
        ),
    )
