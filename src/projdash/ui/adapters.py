"""Presentation adapters for the service-backed UI."""

from __future__ import annotations

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
                "status": node.get("status"),
                "computed": node.get("computed_status"),
                "duration_hours": node.get("duration_hours"),
                "due_at": node.get("due_at"),
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
