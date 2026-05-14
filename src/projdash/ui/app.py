"""Streamlit entrypoint for the service-backed ProjectDashboard UI."""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import streamlit as st
from pydantic import ValidationError

from projdash.ui.adapters import (
    aggregate_process_properties,
    allowed_dependency_symbols,
    allowed_shared_dependency_symbols,
    allowed_successor_symbols,
    auto_horizon_from_graph,
    build_process_graph_dot,
    catalog_from_query_data,
    cost_time_series_rows,
    edge_table_rows,
    existing_dependency_symbols,
    gantt_rows,
    process_symbol_maps,
    process_table_rows,
    resource_priority_rows,
    resource_utilization_heatmap,
    role_priority_rows,
    role_utilization_heatmap,
)
from projdash.ui.service_client import (
    DEFAULT_TIMEZONE,
    batch_payload_envelope,
    calendar_options,
    combine_datetime,
    command_payload_envelope,
    create_project_service,
    format_display_datetime,
    format_display_datetimes,
    infer_subgraph_roots_and_leaves,
    parse_dependency_lines,
    parse_subgraph_process_lines,
    project_options,
    query_payload_envelope,
    result_to_dict,
    scoped_id,
    stable_id,
    to_display_timezone,
    validate_timezone,
)


@st.cache_resource(show_spinner=False)
def _service(db_path: str):
    return create_project_service(db_path)


def main() -> None:
    """Render the service-backed Streamlit application."""
    st.set_page_config(page_title="ProjectDashboard", layout="wide")
    st.title("ProjectDashboard")

    db_path = os.environ.get("PROJDASH_DB_PATH", "projdash.lbug")
    service = _service(db_path)
    projects_data = _query(
        service,
        {"action": "query_projects"},
        render=False,
    ) or {"projects": []}
    controls = _render_sidebar(db_path, projects_data.get("projects", []))

    if not controls["project_id"]:
        _render_first_run(service, controls)
        return

    context = _load_context(service, controls)
    if context["project"] is None:
        st.warning("No project data was returned for the selected project id.")
        _render_first_run(service, controls)
        return

    tabs = st.tabs(
        [
            "Dashboard",
            "Project",
            "Processes",
            "Graph",
            "Resources",
            "Schedule",
            "Slippage",
            "Costs",
            "History",
            "Topology",
        ]
    )
    with tabs[0]:
        _render_dashboard(controls, context)
    with tabs[1]:
        _render_project_settings(service, controls, context)
    with tabs[2]:
        _render_processes(service, controls, context)
    with tabs[3]:
        _render_graph(context)
    with tabs[4]:
        _render_resources(service, controls, context)
    with tabs[5]:
        _render_schedule(controls, context)
    with tabs[6]:
        _render_slippage(service, controls, context)
    with tabs[7]:
        _render_costs(controls, context)
    with tabs[8]:
        _render_history(controls, context)
    with tabs[9]:
        _render_topology(service, controls, context)


def _render_sidebar(db_path: str, projects: list[dict[str, Any]]) -> dict[str, Any]:
    now_utc = dt.datetime.now(dt.UTC)
    override_as_of = st.session_state.pop("as_of_override", None)
    if isinstance(override_as_of, str):
        override_as_of = _parse_iso_datetime(override_as_of, now_utc)
    has_as_of_override = isinstance(override_as_of, dt.datetime)
    default_as_of = override_as_of if has_as_of_override else now_utc
    st.sidebar.text_input(
        "Service database",
        db_path,
        disabled=True,
        help="Durable LadybugDB file used by the service.",
    )
    options = project_options(projects)
    option_ids = [option.project_id for option in options]
    current_project_id = st.session_state.get("project_id", "")
    selected_index = (
        option_ids.index(current_project_id) + 1
        if current_project_id in option_ids
        else 0
    )
    selected_project_id = st.sidebar.selectbox(
        "Project",
        [""] + option_ids,
        index=selected_index,
        format_func=lambda value: _project_label(value, options),
        help="Select an existing project from the service database.",
    )
    project_id = selected_project_id.strip()
    if project_id:
        st.session_state["project_id"] = project_id
    else:
        st.session_state.pop("project_id", None)

    timezone_name = st.sidebar.text_input(
        "Timezone",
        DEFAULT_TIMEZONE,
        help="IANA timezone used for form date/time inputs, such as UTC or Europe/Paris.",
    ).strip()
    try:
        validate_timezone(timezone_name)
    except ValueError as exc:
        st.sidebar.error(str(exc))
        st.stop()
    default_as_of = to_display_timezone(default_as_of, timezone_name)
    if has_as_of_override or "sidebar_as_of_date" not in st.session_state:
        st.session_state["sidebar_as_of_date"] = default_as_of.date()
    if has_as_of_override or "sidebar_as_of_time" not in st.session_state:
        st.session_state["sidebar_as_of_time"] = default_as_of.time().replace(
            microsecond=0,
        )
    as_of_date = st.sidebar.date_input(
        "As of date",
        key="sidebar_as_of_date",
        help="Planning snapshot date for schedule and history queries.",
    )
    as_of_time = st.sidebar.time_input(
        "As of time",
        key="sidebar_as_of_time",
        help="Planning snapshot time for schedule and history queries.",
    )
    now_at = combine_datetime(as_of_date, as_of_time, timezone_name)
    return {
        "project_id": project_id,
        "timezone": timezone_name,
        "as_of": now_at,
        "now": now_at,
    }


def _project_label(project_id: str, options: list[Any]) -> str:
    if not project_id:
        return "Create or select a project"
    labels = {option.project_id: option.label for option in options}
    return labels.get(project_id, project_id)


def _calendar_label(calendar_id: str, options: list[Any]) -> str:
    if not calendar_id:
        return "Create a new calendar"
    labels = {option.calendar_id: option.label for option in options}
    return labels.get(calendar_id, calendar_id)


def _snapshot_label(
    snapshot_id: str,
    snapshots: list[dict[str, Any]],
    timezone_name: str,
) -> str:
    if not snapshot_id:
        return "Select a committed timestamp"
    rows = {snapshot["snapshot_id"]: snapshot for snapshot in snapshots}
    snapshot = rows.get(snapshot_id)
    if snapshot is None:
        return snapshot_id
    committed = format_display_datetime(snapshot.get("committed_at"), timezone_name)
    completion = (
        format_display_datetime(snapshot.get("completion_at"), timezone_name)
        if snapshot.get("completion_at")
        else "unresolved"
    )
    return f"{committed} -> {completion}"


def _datetime_axis_locator_and_formatter(timezone_name: str):
    """Return Matplotlib date locator/formatter for the selected UI timezone."""
    timezone = ZoneInfo(validate_timezone(timezone_name))
    locator = mdates.AutoDateLocator(tz=timezone, minticks=3, maxticks=6)
    return locator, mdates.ConciseDateFormatter(locator, tz=timezone)


def _format_datetime_axis(axis: Any, timezone_name: str) -> None:
    locator, formatter = _datetime_axis_locator_and_formatter(timezone_name)
    axis.set_major_locator(locator)
    axis.set_major_formatter(formatter)
    axis.set_tick_params(labelrotation=30, labelsize=8)


def _role_effort_defaults(node: dict[str, Any]) -> dict[str, float]:
    defaults: dict[str, float] = {}
    for requirement in node.get("role_requirements") or []:
        role_id = requirement.get("role_id")
        if not role_id:
            continue
        defaults[role_id] = defaults.get(role_id, 0.0) + float(
            requirement.get("effort_hours") or 0.0
        )
    return defaults


def _role_requirement_inputs(
    role_ids: list[str],
    *,
    key_prefix: str,
    defaults: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    defaults = defaults or {}
    role_key = f"{key_prefix}_roles"
    selected_default = [role_id for role_id in defaults if role_id in role_ids]
    multiselect_kwargs = {}
    if role_key not in st.session_state:
        multiselect_kwargs["default"] = selected_default
    selected_roles = st.multiselect(
        "Required roles",
        role_ids,
        key=role_key,
        help="Defined roles required by this process.",
        **multiselect_kwargs,
    )
    for role_id in role_ids:
        if role_id not in selected_roles:
            effort_key = f"{key_prefix}_{role_id}_effort"
            if effort_key in st.session_state:
                st.session_state[effort_key] = 0.0
    requirements = []
    for role_id in selected_roles:
        effort_key = f"{key_prefix}_{role_id}_effort"
        effort_kwargs = {}
        if effort_key not in st.session_state:
            effort_kwargs["value"] = float(defaults.get(role_id, 0.0))
        effort = st.number_input(
            f"{role_id} effort hours",
            0.0,
            10000.0,
            key=effort_key,
            help="Total effort required from this role.",
            **effort_kwargs,
        )
        if effort > 0:
            requirements.append({"role_id": role_id, "effort_hours": effort})
    return requirements


def _project_currency(context: dict[str, Any]) -> str:
    project = (context.get("project") or {}).get("project", {})
    return str(project.get("default_currency") or "USD")


def _render_first_run(service, controls: dict[str, Any]) -> None:
    st.subheader("Create project")
    with st.form("first_run"):
        name = st.text_input(
            "Project name",
            "New project",
            help="Human-readable project name.",
        )
        project_id = st.text_input(
            "Project id",
            stable_id("project", name),
            help="Stable id agents and UI commands use to reference this project.",
        )
        currency = st.text_input(
            "Default currency",
            "USD",
            max_chars=3,
            help="ISO 4217 default cost currency for resources in this project.",
        )
        start_date = st.date_input(
            "Project start date",
            controls["as_of"].date(),
            help="Project start date in the selected sidebar timezone.",
        )
        start_time = st.time_input(
            "Project start time",
            dt.time(9, 0),
            help="Project start time in the selected sidebar timezone.",
        )
        submitted = st.form_submit_button("Create project")

    if not submitted:
        return

    try:
        start_at = combine_datetime(start_date, start_time, controls["timezone"])
        batch_results = _apply_batch(
            service,
            [
                {
                    "action": "create_project",
                    "project_id": project_id,
                    "name": name,
                    "start_at": start_at,
                    "default_currency": currency,
                }
            ],
            rerun=False,
        )
        if batch_results is None or not all(result.ok for result in batch_results):
            return
        st.session_state["project_id"] = project_id
        st.rerun()
    except (ValueError, ValidationError) as exc:
        st.error(str(exc))


def _load_context(service, controls: dict[str, Any]) -> dict[str, Any]:
    project_id = controls["project_id"]
    base = {
        "project": _query(
            service,
            {"action": "get_project", "project_id": project_id},
            key="project",
        ),
        "graph": None,
        "full_graph": None,
        "blockers": None,
        "schedule_snapshots": None,
        "catalog": None,
        "resource_schedule": None,
        "capacity": None,
        "utilization": None,
        "costs": None,
        "scope": None,
        "terminal_symbols": [],
        "now": controls["now"],
        "horizon_starts_at": None,
        "horizon_ends_at": None,
    }
    if base["project"] is None:
        return base
    base["catalog"] = _query(
        service,
        {
            "action": "query_project_catalog",
            "project_id": project_id,
        },
    )
    dependency_graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": controls["as_of"],
            "now": controls["now"],
        },
    )
    base["full_graph"] = dependency_graph
    terminal_symbols = _valid_process_symbols(
        st.session_state.get("terminal_process_symbols", []),
        dependency_graph or {},
    )
    scope = _terminal_scope(terminal_symbols)
    horizon_starts_at, horizon_ends_at = auto_horizon_from_graph(
        base["project"],
        dependency_graph or {},
        controls["as_of"],
        terminal_symbols=terminal_symbols,
    )
    controls["horizon_starts_at"] = horizon_starts_at
    controls["horizon_ends_at"] = horizon_ends_at
    base["scope"] = scope
    base["terminal_symbols"] = terminal_symbols
    base["horizon_starts_at"] = horizon_starts_at
    base["horizon_ends_at"] = horizon_ends_at
    scoped_query = {"scope": scope} if scope else {}
    base["graph"] = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": controls["as_of"],
            "now": controls["now"],
            **scoped_query,
            "include_resource_fields": True,
            "horizon_starts_at": horizon_starts_at,
            "horizon_ends_at": horizon_ends_at,
            "include_allocation_slices": True,
        },
    )
    base["blockers"] = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": project_id,
            "as_of": controls["as_of"],
            "include_resolved": True,
        },
    )
    base["schedule_snapshots"] = _query(
        service,
        {
            "action": "query_schedule_snapshots",
            "project_id": project_id,
            "as_of": controls["as_of"],
            "terminal_process_symbols": terminal_symbols,
        },
    )
    resource_query = {
        "project_id": project_id,
        "as_of": controls["as_of"],
        "now": controls["now"],
        **scoped_query,
        "horizon_starts_at": horizon_starts_at,
        "horizon_ends_at": horizon_ends_at,
    }
    base["resource_schedule"] = _query(
        service,
        {
            "action": "query_resource_schedule",
            **resource_query,
            "include_allocation_slices": True,
        },
    )
    base["capacity"] = _query(
        service,
        {
            "action": "query_resource_capacity",
            "project_id": project_id,
            "as_of": controls["as_of"],
            "horizon_starts_at": horizon_starts_at,
            "horizon_ends_at": horizon_ends_at,
        },
    )
    base["utilization"] = _query(
        service,
        {"action": "query_utilization", **resource_query},
    )
    base["costs"] = _query(
        service,
        {"action": "query_costs", **resource_query},
    )
    return base


def _render_dashboard(controls: dict[str, Any], context: dict[str, Any]) -> None:
    project = context["project"]["project"]
    graph = context.get("graph") or {}
    blockers = context.get("blockers") or {}
    costs = context.get("costs") or {}
    nodes = graph.get("nodes", [])
    unresolved = [
        blocker
        for blocker in blockers.get("blockers", [])
        if not blocker.get("is_resolved_as_of")
    ]
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Project", project["name"])
    col2.metric("Processes", len(nodes))
    col3.metric("Open blockers", len(unresolved))
    col4.metric("Total cost", costs.get("total_cost", "0"))
    st.dataframe(
        format_display_datetimes(process_table_rows(graph), controls["timezone"]),
        use_container_width=True,
        hide_index=True,
    )
    st.metric("Schedule basis", graph.get("schedule_basis", "-"))


def _render_project_settings(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> None:
    project = (context.get("project") or {}).get("project", {})
    st.subheader("Project settings")
    start_at = to_display_timezone(
        _parse_iso_datetime(project.get("start_at"), controls["as_of"]),
        controls["timezone"],
    )
    with st.form("project_settings"):
        name = st.text_input(
            "Project name",
            project.get("name", ""),
            help="Human-readable project name.",
        )
        currency = st.text_input(
            "Default currency",
            project.get("default_currency", "USD"),
            max_chars=3,
            help="ISO 4217 default currency used when resources omit a currency.",
        )
        start_date = st.date_input(
            "Project start date",
            start_at.date(),
            help="Project start date in the selected sidebar timezone.",
        )
        start_time = st.time_input(
            "Project start time",
            start_at.timetz().replace(tzinfo=None),
            help="Project start time in the selected sidebar timezone.",
        )
        save = st.form_submit_button("Save project settings")
    if save:
        _apply_command(
            service,
            {
                "action": "update_project",
                "project_id": controls["project_id"],
                "name": name,
                "default_currency": currency,
                "start_at": combine_datetime(start_date, start_time, controls["timezone"]),
            },
        )

    st.subheader("Delete project")
    with st.form("delete_project"):
        confirm = st.text_input(
            "Confirm project id",
            help="Type the exact project id to permanently delete this project.",
        )
        delete = st.form_submit_button("Delete project")
    if delete:
        result = _apply_command(
            service,
            {
                "action": "delete_project",
                "project_id": controls["project_id"],
                "confirm_project_id": confirm,
            },
            rerun=False,
        )
        if result is not None and result.ok:
            st.session_state.pop("project_id", None)
            st.rerun()


def _render_process_table(
    graph: dict[str, Any],
    *,
    key: str,
    timezone_name: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    rows = process_table_rows(graph)
    display_rows = format_display_datetimes(rows, timezone_name)
    selected_symbols: list[str] = []
    try:
        event = st.dataframe(
            display_rows,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="multi-row",
            key=key,
        )
        selection = getattr(event, "selection", None)
        if isinstance(selection, dict):
            selected_rows = selection.get("rows", [])
        else:
            selected_rows = getattr(selection, "rows", []) if selection else []
        selected_symbols = [
            rows[index]["symbol"]
            for index in selected_rows
            if 0 <= index < len(rows) and rows[index].get("symbol")
        ]
    except TypeError:
        st.dataframe(display_rows, use_container_width=True, hide_index=True)
    if selected_symbols:
        st.session_state["selected_process_symbols"] = selected_symbols
        return rows, selected_symbols
    stored = _valid_process_symbols(
        st.session_state.get("selected_process_symbols", []),
        graph,
    )
    return rows, stored


def _render_processes(service, controls: dict[str, Any], context: dict[str, Any]) -> None:
    graph = context.get("full_graph") or context.get("graph") or {}
    catalog = catalog_from_query_data(
        context.get("catalog"),
        graph,
        context.get("blockers"),
    )
    id_by_symbol, _symbol_by_id = process_symbol_maps(graph)
    node_by_symbol = {
        node.get("process_symbol"): node
        for node in graph.get("nodes", [])
        if node.get("process_symbol")
    }
    st.subheader("Process plan")
    _rows, selected_symbols = _render_process_table(
        graph,
        key="process_table",
        timezone_name=controls["timezone"],
    )
    _render_create_process_menu(service, controls, graph, catalog, id_by_symbol)
    _render_modify_process_menu(
        service,
        controls,
        graph,
        catalog,
        id_by_symbol,
        node_by_symbol,
        selected_symbols,
    )


def _render_create_process_menu(
    service,
    controls: dict[str, Any],
    graph: dict[str, Any],
    catalog: dict[str, list[str]],
    id_by_symbol: dict[str, str],
) -> None:
    with st.expander("Create process", expanded=True):
        name = st.text_input(
            "Name",
            key="process_create_name",
            help="Human-readable process name. The process symbol is generated.",
        )
        description = st.text_area(
            "Description",
            key="process_create_description",
            help="Definition of done, scope, and PM notes for this process.",
        )
        dependencies = st.multiselect(
            "Predecessors",
            allowed_dependency_symbols(graph, None),
            key="process_create_dependencies",
            help="Defined processes that must finish before this new process starts.",
        )
        earliest_enabled = st.checkbox(
            "Set earliest start",
            key="process_create_earliest_enabled",
            help="Enable a not-before datetime constraint for this process.",
        )
        earliest_date = st.date_input(
            "Earliest start date",
            controls["as_of"].date(),
            key="process_create_earliest_date",
            help="Earliest allowed start date.",
        )
        earliest_time = st.time_input(
            "Earliest start time",
            dt.time(9, 0),
            key="process_create_earliest_time",
            help="Earliest allowed start time.",
        )
        role_requirements = _role_requirement_inputs(
            catalog["role_ids"],
            key_prefix="process_create",
        )
        create = st.button("Create process")
    if not create or not name:
        return
    result = _apply_command(
        service,
        {
            "action": "upsert_process_revision",
            "project_id": controls["project_id"],
            "name": name,
            "description": description,
            "effective_at": controls["as_of"],
            "duration_business_days": 0,
            "dependencies": [
                id_by_symbol[symbol]
                for symbol in dependencies
                if symbol in id_by_symbol
            ],
            "earliest_start_at": combine_datetime(
                earliest_date,
                earliest_time,
                controls["timezone"],
            )
            if earliest_enabled
            else None,
            "role_requirements": role_requirements,
        },
        rerun=False,
    )
    if result is not None and result.ok:
        _clear_widget_prefix("process_create")
        st.rerun()


def _render_modify_process_menu(
    service,
    controls: dict[str, Any],
    graph: dict[str, Any],
    catalog: dict[str, list[str]],
    id_by_symbol: dict[str, str],
    node_by_symbol: dict[str, dict[str, Any]],
    selected_symbols: list[str],
) -> None:
    with st.expander("Modify selected processes", expanded=True):
        _sync_selected_process_widget(selected_symbols, catalog["process_symbols"])
        target_symbols = st.multiselect(
            "Processes",
            catalog["process_symbols"],
            key="process_modify_targets",
            help="Processes affected by the revision commands.",
        )
        target_symbols = _valid_process_symbols(target_symbols, graph)
        aggregate = aggregate_process_properties(graph, target_symbols)
        _sync_process_revision_defaults(aggregate, controls, catalog["role_ids"])
        if not target_symbols:
            st.info("Select one or more process rows above, or choose symbols here.")
            return

        predecessor_options = allowed_shared_dependency_symbols(graph, target_symbols)
        predecessor_defaults = [
            symbol
            for symbol in st.session_state.get("process_modify_predecessors", [])
            if symbol in predecessor_options
        ]
        st.session_state["process_modify_predecessors"] = predecessor_defaults
        predecessors = st.multiselect(
            "Predecessors",
            predecessor_options,
            key="process_modify_predecessors",
            help="External predecessors for every selected process.",
        )
        update_predecessors = st.checkbox(
            "Update predecessors",
            key="process_modify_update_predecessors",
            help="Replace each selected process's external predecessor set.",
        )

        child_options = allowed_successor_symbols(graph, target_symbols)
        child_defaults = [
            symbol
            for symbol in st.session_state.get("process_modify_children", [])
            if symbol in child_options
        ]
        st.session_state["process_modify_children"] = child_defaults
        children = st.multiselect(
            "Children",
            child_options,
            key="process_modify_children",
            help="External children for every selected process.",
        )
        update_children = st.checkbox(
            "Update children",
            key="process_modify_update_children",
            help="Replace each selected process's external child set.",
        )

        update_roles = st.checkbox(
            "Update role effort",
            key="process_modify_update_roles",
            help="Replace selected processes' role effort with the values below.",
        )
        role_requirements = _role_requirement_inputs(
            catalog["role_ids"],
            key_prefix="process_modify",
            defaults=aggregate["role_efforts"],
        )

        update_timing = st.checkbox(
            "Update earliest start",
            key="process_modify_update_timing",
            help="Replace the not-before constraint for selected processes.",
        )
        earliest_enabled = st.checkbox(
            "Set earliest start",
            key="process_modify_earliest_enabled",
            help="Enable a not-before datetime constraint.",
        )
        earliest_date = st.date_input(
            "Earliest start date",
            key="process_modify_earliest_date",
            help="Earliest allowed start date.",
        )
        earliest_time = st.time_input(
            "Earliest start time",
            key="process_modify_earliest_time",
            help="Earliest allowed start time.",
        )

        update_metadata = False
        name = ""
        description = ""
        if len(target_symbols) == 1:
            node = node_by_symbol.get(target_symbols[0], {})
            update_metadata = st.checkbox(
                "Update name and description",
                key="process_modify_update_metadata",
                help="Save a process revision with updated human-readable metadata.",
            )
            name = st.text_input(
                "Name",
                key="process_modify_name",
                help="Human-readable process name.",
            )
            description = st.text_area(
                "Description",
                key="process_modify_description",
                help="Definition of done, scope, and PM notes for this process.",
            )

        update_status = st.checkbox(
            "Update status",
            key="process_modify_update_status",
            help="Set lifecycle state for every selected process.",
        )
        status_options = ["planned", "in_progress", "paused", "done", "canceled"]
        status = st.selectbox(
            "Status",
            status_options,
            key="process_modify_status",
            help="Lifecycle status for selected processes.",
        )
        started_enabled = st.checkbox(
            "Set started time",
            key="process_modify_started_enabled",
            help="Record an actual start datetime that pins ES and LS.",
        )
        started_date = st.date_input(
            "Started date",
            key="process_modify_started_date",
            help="Actual start date.",
        )
        started_time = st.time_input(
            "Started time",
            key="process_modify_started_time",
            help="Actual start time.",
        )
        finished_enabled = st.checkbox(
            "Set finished time",
            key="process_modify_finished_enabled",
            help="Record an actual completion datetime when status is done.",
        )
        finished_date = st.date_input(
            "Finished date",
            key="process_modify_finished_date",
            help="Completion date.",
        )
        finished_time = st.time_input(
            "Finished time",
            key="process_modify_finished_time",
            help="Completion time.",
        )

        st.caption("Open blockers: " + (", ".join(aggregate["blocker_ids"]) or "none"))
        add_blocker = st.checkbox(
            "Add blocker",
            key="process_modify_add_blocker",
            help="Create the same blocker on every selected process.",
        )
        blocker_summary = st.text_input(
            "Blocker summary",
            key="process_modify_blocker_summary",
            help="Short blocker summary.",
        )
        blocker_details = st.text_area(
            "Blocker details",
            key="process_modify_blocker_details",
            help="Optional blocker detail.",
        )
        blocker_severity = st.selectbox(
            "Blocker severity",
            ["blocking", "warning", "info"],
            key="process_modify_blocker_severity",
            help="Whether this blocker prevents work or is informational.",
        )
        blockers_to_resolve = st.multiselect(
            "Resolve blockers",
            aggregate["blocker_ids"],
            key="process_modify_resolve_blockers",
            help="Existing blockers to mark resolved.",
        )
        resolution = st.text_input(
            "Resolution",
            key="process_modify_resolution",
            help="Optional resolution note.",
        )
        apply_changes = st.button("Apply modifications")

    if not apply_changes:
        return

    commands = []
    if update_predecessors or update_children:
        operations = []
        if update_predecessors:
            operations.extend(
                _dependency_set_operations(
                    graph,
                    target_symbols,
                    predecessors,
                    side="predecessors",
                )
            )
        if update_children:
            operations.extend(
                _dependency_set_operations(
                    graph,
                    target_symbols,
                    children,
                    side="children",
                )
            )
        if operations:
            commands.append(
                {
                    "action": "batch_update_process_graph",
                    "project_id": controls["project_id"],
                    "edit_at": controls["as_of"],
                    "operations": operations,
                }
            )

    role_requirements_by_symbol = _batch_role_requirements_by_symbol(
        graph,
        target_symbols,
        role_requirements,
    )
    if update_roles or update_timing or update_metadata:
        for symbol in target_symbols:
            node = node_by_symbol.get(symbol, {})
            current_predecessors = existing_dependency_symbols(graph, symbol)
            current_role_requirements = node.get("role_requirements") or []
            earliest_start_at = node.get("earliest_start_at")
            if update_timing:
                earliest_start_at = (
                    combine_datetime(
                        earliest_date,
                        earliest_time,
                        controls["timezone"],
                    )
                    if earliest_enabled
                    else None
                )
            commands.append(
                {
                    "action": "upsert_process_revision",
                    "project_id": controls["project_id"],
                    "process_symbol": symbol,
                    "name": name if update_metadata and len(target_symbols) == 1 else (
                        node.get("name") or symbol
                    ),
                    "description": (
                        description
                        if update_metadata and len(target_symbols) == 1
                        else node.get("description", "")
                    ),
                    "effective_at": controls["as_of"],
                    "duration_business_days": int(
                        (float(node.get("duration_hours") or 0.0) + 7.9999) // 8
                    ),
                    "dependencies": [
                        id_by_symbol[pred]
                        for pred in current_predecessors
                        if pred in id_by_symbol
                    ],
                    "earliest_start_at": earliest_start_at,
                    "role_requirements": (
                        role_requirements_by_symbol.get(symbol, [])
                        if update_roles
                        else current_role_requirements
                    ),
                }
            )

    if update_status:
        for symbol in target_symbols:
            commands.append(
                {
                    "action": "set_process_status",
                    "project_id": controls["project_id"],
                    "process_symbol": symbol,
                    "status": status,
                    "edit_at": controls["as_of"],
                    "started_at": combine_datetime(
                        started_date,
                        started_time,
                        controls["timezone"],
                    )
                    if started_enabled
                    else None,
                    "finished_at": combine_datetime(
                        finished_date,
                        finished_time,
                        controls["timezone"],
                    )
                    if finished_enabled
                    else None,
                }
            )
    if add_blocker and blocker_summary:
        for symbol in target_symbols:
            commands.append(
                {
                    "action": "add_blocker",
                    "project_id": controls["project_id"],
                    "process_symbol": symbol,
                    "summary": blocker_summary,
                    "details": blocker_details or None,
                    "severity": blocker_severity,
                    "created_at": controls["as_of"],
                }
            )
    for blocker_id in blockers_to_resolve:
        commands.append(
            {
                "action": "resolve_blocker",
                "project_id": controls["project_id"],
                "blocker_id": blocker_id,
                "resolved_at": controls["as_of"],
                "resolution": resolution or None,
            }
        )

    if commands:
        _apply_batch(service, commands)


def _sync_selected_process_widget(
    selected_symbols: list[str],
    process_symbols: list[str],
) -> None:
    valid_selected = [symbol for symbol in selected_symbols if symbol in process_symbols]
    signature = "\0".join(valid_selected)
    if signature and st.session_state.get("process_table_selection_sig") != signature:
        st.session_state["process_modify_targets"] = valid_selected
        st.session_state["process_table_selection_sig"] = signature


def _sync_process_revision_defaults(
    aggregate: dict[str, Any],
    controls: dict[str, Any],
    role_ids: list[str],
) -> None:
    signature = _process_revision_defaults_signature(aggregate, controls)
    if st.session_state.get("process_modify_defaults_sig") == signature:
        return
    st.session_state["process_modify_predecessors"] = aggregate["predecessors"]
    st.session_state["process_modify_children"] = aggregate["children"]
    st.session_state["process_modify_roles"] = [
        role_id for role_id in aggregate["role_efforts"] if role_id in role_ids
    ]
    for role_id in role_ids:
        st.session_state[f"process_modify_{role_id}_effort"] = float(
            aggregate["role_efforts"].get(role_id, 0.0)
        )
    st.session_state["process_modify_status"] = aggregate.get("status") or "planned"
    st.session_state["process_modify_name"] = aggregate.get("name", "")
    st.session_state["process_modify_description"] = aggregate.get("description", "")
    earliest_at = _common_datetime_or_default(
        aggregate.get("earliest_start_at"),
        controls["as_of"],
        controls["timezone"],
    )
    started_at = _common_datetime_or_default(
        aggregate.get("started_at"),
        controls["as_of"],
        controls["timezone"],
    )
    finished_at = _common_datetime_or_default(
        aggregate.get("finished_at"),
        controls["as_of"],
        controls["timezone"],
    )
    st.session_state["process_modify_earliest_enabled"] = (
        aggregate.get("earliest_start_at") is not None
    )
    st.session_state["process_modify_earliest_date"] = earliest_at.date()
    st.session_state["process_modify_earliest_time"] = earliest_at.time()
    st.session_state["process_modify_started_enabled"] = (
        aggregate.get("started_at") is not None
    )
    st.session_state["process_modify_started_date"] = started_at.date()
    st.session_state["process_modify_started_time"] = started_at.time()
    st.session_state["process_modify_finished_enabled"] = (
        aggregate.get("finished_at") is not None
    )
    st.session_state["process_modify_finished_date"] = finished_at.date()
    st.session_state["process_modify_finished_time"] = finished_at.time()
    st.session_state["process_modify_defaults_sig"] = signature


def _process_revision_defaults_signature(
    aggregate: dict[str, Any],
    controls: dict[str, Any],
) -> str:
    """Return a stable form-state signature for selected process properties."""
    role_efforts = tuple(
        sorted(
            (role_id, float(hours))
            for role_id, hours in aggregate.get("role_efforts", {}).items()
        )
    )
    parts = (
        tuple(aggregate.get("process_symbols", [])),
        tuple(aggregate.get("predecessors", [])),
        tuple(aggregate.get("children", [])),
        role_efforts,
        aggregate.get("status") or "",
        aggregate.get("name") or "",
        aggregate.get("description") or "",
        str(aggregate.get("earliest_start_at") or ""),
        str(aggregate.get("started_at") or ""),
        str(aggregate.get("finished_at") or ""),
        tuple(aggregate.get("blocker_ids", [])),
        controls["timezone"],
    )
    return repr(parts)


def _dependency_set_operations(
    graph: dict[str, Any],
    selected_symbols: list[str],
    desired_symbols: list[str],
    *,
    side: str,
) -> list[dict[str, Any]]:
    desired = set(desired_symbols)
    selected_set = set(selected_symbols)
    operations = []
    for selected in selected_symbols:
        if side == "predecessors":
            current = {
                predecessor
                for predecessor in existing_dependency_symbols(graph, selected)
                if predecessor not in selected_set
            }
            for predecessor in sorted(desired - current):
                operations.append(
                    {
                        "action": "add_dependency",
                        "operation_id": f"add-{predecessor}-{selected}",
                        "predecessor_process_symbol": predecessor,
                        "successor_process_symbol": selected,
                    }
                )
            for predecessor in sorted(current - desired):
                operations.append(
                    {
                        "action": "remove_dependency",
                        "operation_id": f"remove-{predecessor}-{selected}",
                        "predecessor_process_symbol": predecessor,
                        "successor_process_symbol": selected,
                    }
                )
            continue
        current = {
            edge.get("successor_process_symbol")
            for edge in graph.get("edges", [])
            if edge.get("predecessor_process_symbol") == selected
            and edge.get("successor_process_symbol") not in selected_set
        }
        for child in sorted(desired - current):
            operations.append(
                {
                    "action": "add_dependency",
                    "operation_id": f"add-{selected}-{child}",
                    "predecessor_process_symbol": selected,
                    "successor_process_symbol": child,
                }
            )
        for child in sorted(current - desired):
            operations.append(
                {
                    "action": "remove_dependency",
                    "operation_id": f"remove-{selected}-{child}",
                    "predecessor_process_symbol": selected,
                    "successor_process_symbol": child,
                }
            )
    return operations


def _batch_role_requirements_by_symbol(
    graph: dict[str, Any],
    selected_symbols: list[str],
    aggregate_requirements: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Distribute aggregate role effort totals across selected processes."""
    if len(selected_symbols) <= 1:
        return {symbol: list(aggregate_requirements) for symbol in selected_symbols}

    current_by_symbol: dict[str, dict[str, float]] = {}
    for node in graph.get("nodes", []):
        symbol = node.get("process_symbol")
        if symbol not in selected_symbols:
            continue
        current_by_symbol[symbol] = _role_effort_defaults(node)
    target_totals = {
        requirement["role_id"]: float(requirement.get("effort_hours") or 0.0)
        for requirement in aggregate_requirements
        if requirement.get("role_id")
    }
    current_totals: dict[str, float] = {}
    for role_efforts in current_by_symbol.values():
        for role_id, effort_hours in role_efforts.items():
            current_totals[role_id] = current_totals.get(role_id, 0.0) + effort_hours

    output: dict[str, list[dict[str, Any]]] = {}
    for symbol in selected_symbols:
        output[symbol] = []
        for role_id, target_total in sorted(target_totals.items()):
            current_total = current_totals.get(role_id, 0.0)
            if current_total > 0:
                effort_hours = target_total * (
                    current_by_symbol.get(symbol, {}).get(role_id, 0.0)
                    / current_total
                )
            else:
                effort_hours = target_total / len(selected_symbols)
            if effort_hours > 0:
                output[symbol].append(
                    {"role_id": role_id, "effort_hours": effort_hours}
                )
    return output


def _common_datetime_or_default(
    value: Any,
    default: dt.datetime,
    timezone_name: str,
) -> dt.datetime:
    if value is None:
        return to_display_timezone(default, timezone_name)
    return to_display_timezone(value, timezone_name)


def _clear_widget_prefix(prefix: str) -> None:
    for key in list(st.session_state):
        if str(key).startswith(prefix):
            del st.session_state[key]


def _render_graph(context: dict[str, Any]) -> None:
    graph = context.get("graph") or {}
    collapsed = set(st.session_state.get("collapsed_process_ids", []))
    if graph.get("nodes"):
        st.graphviz_chart(
            build_process_graph_dot(graph, collapsed_process_ids=collapsed),
            use_container_width=True,
        )
    st.dataframe(edge_table_rows(graph), use_container_width=True, hide_index=True)


def _render_resources(service, controls: dict[str, Any], context: dict[str, Any]) -> None:
    catalog = catalog_from_query_data(
        context.get("catalog"),
        context.get("graph"),
        context.get("capacity"),
        context.get("utilization"),
        context.get("costs"),
    )
    st.subheader("Roles")
    st.write(", ".join(catalog["role_ids"]) or "No roles configured.")
    _render_role_forms(service, controls, catalog)

    st.subheader("Resource utilization")
    _render_heatmap(
        "Resource utilization",
        *resource_utilization_heatmap(context.get("utilization") or {}),
        timezone_name=controls["timezone"],
    )
    st.subheader("Role utilization")
    _render_heatmap(
        "Role utilization",
        *role_utilization_heatmap(
            context.get("capacity") or {},
            context.get("resource_schedule") or {},
        ),
        timezone_name=controls["timezone"],
    )

    st.subheader("Capacity")
    st.dataframe(
        format_display_datetimes(
            (context.get("capacity") or {}).get("buckets", []),
            controls["timezone"],
        ),
        use_container_width=True,
        hide_index=True,
    )
    _render_calendar_forms(service, controls, context, catalog)
    _render_resource_forms(service, controls, context, catalog)


def _render_role_forms(service, controls: dict[str, Any], catalog: dict[str, list[str]]):
    with st.expander("Role commands"):
        with st.form("create_role"):
            name = st.text_input(
                "Role name",
                help="Human-readable role name. The role id is generated from this name.",
            )
            create = st.form_submit_button("Create role")
        if create:
            _apply_command(
                service,
                {
                    "action": "create_role",
                    "project_id": controls["project_id"],
                    "role_id": scoped_id(controls["project_id"], "role", name),
                    "name": name,
                },
            )
        with st.form("rename_role"):
            old_role = st.selectbox(
                "Role id",
                [""] + catalog["role_ids"],
                key="ren_role",
                help="Defined role to rename.",
            )
            new_name = st.text_input("New role name", help="New human-readable role name.")
            rename = st.form_submit_button("Rename role")
        if rename and old_role:
            _apply_command(
                service,
                {
                    "action": "rename_role",
                    "project_id": controls["project_id"],
                    "role_id": old_role,
                    "name": new_name,
                },
            )
        with st.form("deactivate_role"):
            role_id = st.selectbox(
                "Role id",
                [""] + catalog["role_ids"],
                key="deact_role",
                help="Defined role to deactivate.",
            )
            force = st.checkbox(
                "Force",
                help="Allow deactivation even when references would otherwise block it.",
            )
            deactivate = st.form_submit_button("Deactivate role")
        if deactivate and role_id:
            _apply_command(
                service,
                {
                    "action": "deactivate_role",
                    "project_id": controls["project_id"],
                    "role_id": role_id,
                    "force": force,
                },
            )


def _render_calendar_forms(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
    catalog: dict[str, list[str]],
):
    catalog_data = context.get("catalog") or {}
    calendars_by_id = {
        calendar["calendar_id"]: calendar
        for calendar in catalog_data.get("calendars", [])
    }
    with st.expander("Calendar commands"):
        calendar_choices = calendar_options(catalog_data.get("calendars", []))
        calendar_ids = [option.calendar_id for option in calendar_choices]
        selected_calendar_id = st.selectbox(
            "Existing calendar",
            [""] + calendar_ids,
            format_func=lambda value: _calendar_label(value, calendar_choices),
            help="Choose a defined calendar to update, or leave blank to create one.",
        )
        selected_calendar = calendars_by_id.get(selected_calendar_id, {})
        with st.form("upsert_calendar"):
            name = st.text_input(
                "Calendar name",
                selected_calendar.get("name", "Weekday calendar"),
                help=(
                    "Human-readable calendar name. New calendar ids are generated "
                    "from this name and the project id."
                ),
            )
            calendar_timezone = st.text_input(
                "Calendar timezone",
                selected_calendar.get("timezone", controls["timezone"]),
                help=(
                    "IANA timezone for this calendar's local working windows, "
                    "such as UTC or Europe/Paris."
                ),
            ).strip()
            weekdays = st.multiselect(
                "Weekdays",
                [0, 1, 2, 3, 4, 5, 6],
                default=[0, 1, 2, 3, 4],
                help="Local weekdays where this recurring working window applies.",
            )
            start_time = st.time_input(
                "Window start",
                dt.time(9, 0),
                help="Local start time for the recurring working window.",
            )
            end_time = st.time_input(
                "Window end",
                dt.time(17, 0),
                help="Local end time for the recurring working window.",
            )
            capacity = st.number_input(
                "Capacity hours",
                0.0,
                24.0,
                8.0,
                help="Available working capacity during each selected window.",
            )
            active = st.checkbox(
                "Active",
                selected_calendar.get("active", True),
                help="Inactive calendars cannot provide active resource capacity.",
            )
            upsert = st.form_submit_button("Save calendar")
        if upsert:
            calendar_id = selected_calendar_id or scoped_id(
                controls["project_id"],
                "cal",
                name,
            )
            _apply_command(
                service,
                {
                    "action": "upsert_resource_calendar",
                    "project_id": controls["project_id"],
                    "calendar_id": calendar_id,
                    "name": name,
                    "timezone": calendar_timezone,
                    "weekly_windows": _weekly_windows(
                        weekdays,
                        start_time,
                        end_time,
                        capacity,
                    ),
                    "active": active,
                },
            )
        with st.form("calendar_exception"):
            calendar = st.selectbox(
                "Calendar id",
                [""] + catalog["calendar_ids"],
                key="exc_cal",
                help="Defined calendar receiving this one-off capacity exception.",
            )
            exception_timezone = calendars_by_id.get(calendar, {}).get(
                "timezone",
                controls["timezone"],
            )
            exception_default = to_display_timezone(
                controls["as_of"],
                exception_timezone,
            )
            starts_date = st.date_input(
                "Starts",
                exception_default.date(),
                help="Exception start date.",
            )
            starts_time = st.time_input(
                "Starts time",
                dt.time(0, 0),
                help="Exception start time.",
            )
            ends_date = st.date_input(
                "Ends",
                exception_default.date(),
                help="Exception end date.",
            )
            ends_time = st.time_input(
                "Ends time",
                dt.time(23, 59),
                help="Exception end time.",
            )
            exc_capacity = st.number_input(
                "Exception capacity hours",
                0.0,
                24.0,
                0.0,
                help="Replacement capacity for overlapping working windows.",
            )
            reason = st.text_input("Reason", help="Optional exception note.")
            add = st.form_submit_button("Add exception")
        if add and calendar:
            _apply_command(
                service,
                {
                    "action": "add_calendar_exception",
                    "project_id": controls["project_id"],
                    "calendar_id": calendar,
                    "starts_at": combine_datetime(
                        starts_date,
                        starts_time,
                        exception_timezone,
                    ),
                    "ends_at": combine_datetime(
                        ends_date,
                        ends_time,
                        exception_timezone,
                    ),
                    "capacity_hours": exc_capacity,
                    "reason": reason or None,
                },
            )


def _render_resource_forms(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
    catalog: dict[str, list[str]],
):
    catalog_data = context.get("catalog") or {}
    resources_by_id = {
        resource["resource_id"]: resource
        for resource in catalog_data.get("resources", [])
    }
    calendars_by_id = {
        calendar["calendar_id"]: calendar
        for calendar in catalog_data.get("calendars", [])
    }
    project_currency = _project_currency(context)
    with st.expander("Resource commands", expanded=True):
        selected_resource_id = st.selectbox(
            "Existing resource",
            [""] + catalog["resource_ids"],
            key="resource_editor_selected_id",
            help="Choose a defined resource to update, or leave blank to create one.",
        )
        selected_resource = resources_by_id.get(selected_resource_id, {})
        form_key = selected_resource_id or "new"
        with st.form(f"upsert_resource_{form_key}"):
            name = st.text_input(
                "Resource name",
                selected_resource.get("name", ""),
                key=f"resource_name_{form_key}",
                help=(
                    "Human-readable resource name. New resource ids are generated "
                    "from this name and the project id."
                ),
            )
            role_ids = st.multiselect(
                "Role ids",
                catalog["role_ids"],
                default=[
                    role_id
                    for role_id in selected_resource.get("role_ids", [])
                    if role_id in catalog["role_ids"]
                ],
                key=f"resource_roles_{form_key}",
                help="Defined roles this resource can fill.",
            )
            calendar_options = [""] + catalog["calendar_ids"]
            selected_calendar = selected_resource.get("calendar_id", "")
            calendar_index = (
                calendar_options.index(selected_calendar)
                if selected_calendar in calendar_options
                else 0
            )
            calendar_id = st.selectbox(
                "Calendar id",
                calendar_options,
                index=calendar_index,
                key=f"resource_calendar_{form_key}",
                help="Defined calendar controlling this resource's working hours.",
            )
            resource_timezone = calendars_by_id.get(calendar_id, {}).get(
                "timezone",
                controls["timezone"],
            )
            available_from = _parse_iso_datetime(
                selected_resource.get("available_from_at"),
                controls["as_of"],
            )
            available_from = to_display_timezone(
                available_from,
                resource_timezone,
            )
            available_date = st.date_input(
                "Available from",
                available_from.date(),
                key=f"resource_available_date_{form_key}",
                help="First date the resource is available.",
            )
            available_time = st.time_input(
                "Available from time",
                available_from.timetz().replace(tzinfo=None),
                key=f"resource_available_time_{form_key}",
                help="First time the resource is available.",
            )
            cost_rate = st.text_input(
                "Cost rate",
                str(selected_resource.get("cost_rate", "0")),
                key=f"resource_cost_rate_{form_key}",
                help="Cost amount for this resource in the selected cost unit.",
            )
            st.text_input(
                "Cost currency",
                project_currency,
                max_chars=3,
                key=f"resource_cost_currency_{form_key}",
                disabled=True,
                help="Resources use the project's default currency.",
            )
            cost_units = ["hour", "day", "week", "fixed"]
            selected_unit = selected_resource.get("cost_unit", "hour")
            cost_unit = st.selectbox(
                "Cost unit",
                cost_units,
                index=cost_units.index(selected_unit) if selected_unit in cost_units else 0,
                key=f"resource_cost_unit_{form_key}",
                help="Unit for interpreting the resource cost rate.",
            )
            active = st.checkbox(
                "Active resource",
                selected_resource.get("active", True),
                key=f"resource_active_{form_key}",
                help="Inactive resources do not contribute schedulable capacity.",
            )
            save = st.form_submit_button("Save resource")
        if save:
            resource_id = selected_resource_id or scoped_id(
                controls["project_id"],
                "res",
                name,
            )
            _apply_command(
                service,
                {
                    "action": "upsert_resource",
                    "project_id": controls["project_id"],
                    "resource_id": resource_id,
                    "name": name,
                    "role_ids": role_ids,
                    "calendar_id": calendar_id,
                    "available_from_at": combine_datetime(
                        available_date,
                        available_time,
                        resource_timezone,
                    ),
                    "cost_rate": cost_rate,
                    "cost_unit": cost_unit,
                    "cost_currency": project_currency,
                    "holidays": selected_resource.get("holidays", []),
                    "active": active,
                },
            )
        with st.form("resource_active"):
            rid = st.selectbox(
                "Resource id",
                [""] + catalog["resource_ids"],
                help="Defined resource to activate or deactivate.",
            )
            active_state = st.checkbox(
                "Active",
                True,
                key="res_active_state",
                help="Whether the resource contributes capacity.",
            )
            set_active = st.form_submit_button("Set active")
        if set_active and rid:
            _apply_command(
                service,
                {
                    "action": "set_resource_active",
                    "project_id": controls["project_id"],
                    "resource_id": rid,
                    "active": active_state,
                },
            )
        _render_resource_holiday_forms(
            service,
            controls,
            resources_by_id,
            calendars_by_id,
            catalog,
        )


def _render_resource_holiday_forms(
    service,
    controls: dict[str, Any],
    resources_by_id: dict[str, dict[str, Any]],
    calendars_by_id: dict[str, dict[str, Any]],
    catalog: dict[str, list[str]],
) -> None:
    if not catalog["resource_ids"]:
        return
    resource_id = st.selectbox(
        "Holiday resource",
        [""] + catalog["resource_ids"],
        key="holiday_resource_id",
        help="Defined resource whose holiday list will be edited.",
    )
    resource = resources_by_id.get(resource_id)
    if not resource:
        return

    holiday_timezone = calendars_by_id.get(resource.get("calendar_id"), {}).get(
        "timezone",
        controls["timezone"],
    )
    holiday_default = to_display_timezone(controls["as_of"], holiday_timezone)
    holidays = resource.get("holidays", [])
    with st.form("add_resource_holiday"):
        starts_date = st.date_input(
            "Holiday starts",
            holiday_default.date(),
            help="Holiday start date.",
        )
        starts_time = st.time_input(
            "Holiday starts time",
            dt.time(0, 0),
            help="Holiday start time.",
        )
        ends_date = st.date_input(
            "Holiday ends",
            holiday_default.date(),
            help="Holiday end date.",
        )
        ends_time = st.time_input(
            "Holiday ends time",
            dt.time(23, 59),
            help="Holiday end time.",
        )
        reason = st.text_input("Holiday reason", help="Optional holiday note.")
        add = st.form_submit_button("Add resource holiday")
    if add:
        starts_at = combine_datetime(starts_date, starts_time, holiday_timezone)
        ends_at = combine_datetime(ends_date, ends_time, holiday_timezone)
        holiday_id = scoped_id(
            resource_id,
            "holiday",
            f"{starts_at.isoformat()}_{reason or 'holiday'}",
        )
        next_holidays = [
            *holidays,
            {
                "holiday_id": holiday_id,
                "starts_at": starts_at,
                "ends_at": ends_at,
                "reason": reason or None,
            },
        ]
        _apply_command(
            service,
            _resource_payload_with_holidays(
                controls["project_id"],
                resource,
                next_holidays,
            ),
        )

    holiday_ids = [holiday["holiday_id"] for holiday in holidays]
    with st.form("remove_resource_holiday"):
        holiday_id = st.selectbox(
            "Holiday id",
            [""] + holiday_ids,
            help="Defined holiday interval to remove from this resource.",
        )
        remove = st.form_submit_button("Remove resource holiday")
    if remove and holiday_id:
        next_holidays = [
            holiday for holiday in holidays if holiday["holiday_id"] != holiday_id
        ]
        _apply_command(
            service,
            _resource_payload_with_holidays(
                controls["project_id"],
                resource,
                next_holidays,
            ),
        )


def _resource_payload_with_holidays(
    project_id: str,
    resource: dict[str, Any],
    holidays: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "action": "upsert_resource",
        "project_id": project_id,
        "resource_id": resource["resource_id"],
        "name": resource["name"],
        "role_ids": resource["role_ids"],
        "calendar_id": resource["calendar_id"],
        "available_from_at": resource["available_from_at"],
        "available_until_at": resource.get("available_until_at"),
        "cost_rate": resource["cost_rate"],
        "cost_unit": resource["cost_unit"],
        "cost_currency": resource.get("cost_currency"),
        "holidays": holidays,
        "active": resource.get("active", True),
    }


def _render_schedule(controls: dict[str, Any], context: dict[str, Any]) -> None:
    graph = context.get("graph") or {}
    full_graph = context.get("full_graph") or graph
    schedule = context.get("resource_schedule") or {}
    symbol_options = [
        node.get("process_symbol")
        for node in full_graph.get("nodes", [])
        if node.get("process_symbol")
    ]
    terminal_key = "terminal_process_symbols"
    current_terminals = [
        symbol
        for symbol in st.session_state.get(terminal_key, [])
        if symbol in symbol_options
    ]
    if st.session_state.get(terminal_key) != current_terminals:
        st.session_state[terminal_key] = current_terminals
    terminal_symbols = st.multiselect(
        "Completion targets",
        symbol_options,
        key=terminal_key,
        help=(
            "Leave empty to plan to all terminal nodes. Select symbols to plan "
            "their ancestor subgraph."
        ),
    )
    st.download_button(
        "Export schedule debug JSON",
        data=json.dumps(
            _schedule_debug_payload(controls, context, terminal_symbols),
            indent=2,
            sort_keys=True,
            default=_json_default,
        ),
        file_name=_schedule_debug_filename(
            controls["project_id"],
            controls["as_of"],
        ),
        mime="application/json",
        help=(
            "Download the current schedule inputs and computed outputs so the "
            "schedule can be debugged outside Streamlit."
        ),
    )

    horizon_start = context.get("horizon_starts_at")
    horizon_end = context.get("horizon_ends_at")
    if horizon_start and horizon_end:
        st.caption(
            "Query horizon: "
            f"{format_display_datetime(horizon_start, controls['timezone'])} to "
            f"{format_display_datetime(horizon_end, controls['timezone'])}"
        )
    st.metric("Converged", str(schedule.get("converged", "-")))
    _render_gantt_chart(
        graph,
        controls_now=context.get("now"),
        terminal_symbols=terminal_symbols,
        timezone_name=controls["timezone"],
    )
    st.subheader("Role priorities")
    st.dataframe(
        format_display_datetimes(
            role_priority_rows(
                graph,
                context.get("now") or controls["now"],
                terminal_symbols=terminal_symbols,
            ),
            controls["timezone"],
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Resource priorities")
    st.dataframe(
        format_display_datetimes(
            resource_priority_rows(
                graph,
                schedule,
                context.get("now") or controls["now"],
                terminal_symbols=terminal_symbols,
            ),
            controls["timezone"],
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.dataframe(
        format_display_datetimes(schedule.get("processes", []), controls["timezone"]),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Unallocated requirements")
    st.dataframe(
        format_display_datetimes(
            schedule.get("unallocated_requirements", []),
            controls["timezone"],
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Allocation slices")
    st.dataframe(
        format_display_datetimes(schedule.get("allocation_slices", []), controls["timezone"]),
        use_container_width=True,
        hide_index=True,
    )


def _schedule_debug_payload(
    controls: dict[str, Any],
    context: dict[str, Any],
    terminal_symbols: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Build a JSON-safe debug payload for the current schedule view."""
    scoped_query = {"scope": context["scope"]} if context.get("scope") else {}
    return {
        "debug_schema": 1,
        "project_id": controls["project_id"],
        "timezone": controls["timezone"],
        "as_of": controls["as_of"],
        "now": context.get("now") or controls["now"],
        "terminal_process_symbols": list(terminal_symbols),
        "horizon_starts_at": context.get("horizon_starts_at"),
        "horizon_ends_at": context.get("horizon_ends_at"),
        "resource_schedule_query": {
            "action": "query_resource_schedule",
            "project_id": controls["project_id"],
            "as_of": controls["as_of"],
            "now": context.get("now") or controls["now"],
            **scoped_query,
            "horizon_starts_at": context.get("horizon_starts_at"),
            "horizon_ends_at": context.get("horizon_ends_at"),
            "planning_granularity": "hour",
            "include_allocation_slices": True,
        },
        "project": context.get("project"),
        "catalog": context.get("catalog"),
        "graph": context.get("graph"),
        "full_graph": context.get("full_graph"),
        "blockers": context.get("blockers"),
        "resource_schedule": context.get("resource_schedule"),
        "capacity": context.get("capacity"),
        "utilization": context.get("utilization"),
        "costs": context.get("costs"),
    }


def _schedule_debug_filename(project_id: str, as_of: dt.datetime) -> str:
    timestamp = as_of.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_project_id = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in project_id
    )
    return f"projdash_schedule_debug_{safe_project_id}_{timestamp}.json"


def _json_default(value: object) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def _render_gantt_chart(
    graph: dict[str, Any],
    *,
    controls_now: dt.datetime | None,
    terminal_symbols: list[str],
    timezone_name: str,
) -> None:
    rows = gantt_rows(graph, terminal_symbols=terminal_symbols)
    rows = [
        row
        for row in rows
        if row.get("es_at") is not None and row.get("lf_at") is not None
    ]
    if not rows:
        return
    fig_height = max(3, len(rows) * 0.42)
    fig, ax = plt.subplots(figsize=(12, fig_height))
    for index, row in enumerate(rows):
        color = "#dc2626" if row["critical"] else "#2563eb"
        finish_color = "#dc2626" if row["critical"] else "#d97706"
        y = len(rows) - index - 1
        _barh_datetime(
            ax,
            row["es_at"],
            row["lf_at"],
            y - 0.30,
            0.60,
            facecolor=color,
            edgecolor=color,
            linewidth=1.0,
            alpha=0.16,
        )
        _barh_datetime(
            ax,
            row["es_at"],
            row["ls_at"],
            y - 0.30,
            0.60,
            facecolor=color,
            edgecolor="none",
            alpha=0.42,
        )
        _barh_datetime(
            ax,
            row["ef_at"],
            row["lf_at"],
            y - 0.30,
            0.60,
            facecolor=finish_color,
            edgecolor="none",
            alpha=0.32,
        )
        ax.plot(
            [mdates.date2num(row["es_at"]), mdates.date2num(row["lf_at"])],
            [y, y],
            color=color,
            linewidth=1.0,
            alpha=0.9,
        )
    if controls_now is not None:
        ax.axvline(mdates.date2num(controls_now), color="#111827", linewidth=1.2)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([row["symbol"] for row in reversed(rows)])
    ax.set_xlabel("Time")
    _format_datetime_axis(ax.xaxis, timezone_name)
    ax.grid(axis="x", color="#e5e7eb", linewidth=0.8)
    fig.tight_layout()
    fig.autofmt_xdate(rotation=30, ha="right")
    st.pyplot(fig)


def _render_heatmap(
    title: str,
    labels: list[str],
    times: list[dt.datetime],
    matrix: list[list[float]],
    *,
    timezone_name: str,
) -> None:
    if not labels or not times or not matrix:
        st.info(f"No {title.lower()} data for the inferred horizon.")
        return
    step = times[1] - times[0] if len(times) > 1 else dt.timedelta(hours=1)
    time_edges = [*times, times[-1] + step]
    y_edges = list(range(len(labels) + 1))
    fig_height = max(2.5, len(labels) * 0.35)
    fig, ax = plt.subplots(figsize=(12, fig_height))
    image = ax.pcolormesh(
        mdates.date2num(time_edges),
        y_edges,
        matrix,
        cmap="jet",
        vmin=0,
        vmax=1,
        shading="flat",
    )
    ax.set_yticks([index + 0.5 for index in range(len(labels))])
    ax.set_yticklabels(labels)
    _format_datetime_axis(ax.xaxis, timezone_name)
    ax.set_xlabel("Time")
    fig.colorbar(image, ax=ax, label="Utilization")
    fig.tight_layout()
    st.pyplot(fig)


def _render_slippage(service, controls: dict[str, Any], context: dict[str, Any]) -> None:
    terminal_symbols = context.get("terminal_symbols") or []
    snapshots = (context.get("schedule_snapshots") or {}).get("snapshots", [])
    st.subheader("Committed schedule snapshots")
    with st.form("commit_project_state"):
        note = st.text_input(
            "Commit note",
            help="Optional note stored with this committed schedule snapshot.",
        )
        commit = st.form_submit_button("Commit current state")
    if commit:
        committed_at = dt.datetime.now(dt.UTC)
        result = _apply_command(
            service,
            {
                "action": "commit_project_state",
                "project_id": controls["project_id"],
                "committed_at": committed_at,
                "terminal_process_symbols": terminal_symbols,
                "note": note or None,
            },
            rerun=False,
        )
        if result is not None and result.ok:
            st.session_state["as_of_override"] = committed_at.isoformat()
            st.rerun()

    plotted_rows = [
        {
            **snapshot,
            "committed_at_dt": _parse_iso_datetime(
                snapshot.get("committed_at"),
                controls["as_of"],
            ),
            "completion_at_dt": _parse_iso_datetime(
                snapshot.get("completion_at"),
                controls["as_of"],
            )
            if snapshot.get("completion_at")
            else None,
        }
        for snapshot in snapshots
    ]
    chart_rows = [row for row in plotted_rows if row["completion_at_dt"] is not None]
    if chart_rows:
        fig, ax = plt.subplots(figsize=(12, 3.5))
        ax.plot(
            [row["committed_at_dt"] for row in chart_rows],
            [row["completion_at_dt"] for row in chart_rows],
            marker="o",
        )
        ax.set_xlabel("Commit time")
        ax.set_ylabel("Calculated completion")
        _format_datetime_axis(ax.xaxis, controls["timezone"])
        _format_datetime_axis(ax.yaxis, controls["timezone"])
        fig.tight_layout()
        st.pyplot(fig)
    st.dataframe(
        format_display_datetimes(snapshots, controls["timezone"]),
        use_container_width=True,
        hide_index=True,
    )

    if not snapshots:
        return
    snapshot_options = [snapshot["snapshot_id"] for snapshot in snapshots]
    selected_snapshot_id = st.selectbox(
        "Historical commit",
        [""] + snapshot_options,
        format_func=lambda value: _snapshot_label(
            value,
            snapshots,
            controls["timezone"],
        ),
        help="Choose a committed schedule timestamp to load into the as-of controls.",
    )
    if st.button("Load commit timestamp") and selected_snapshot_id:
        selected = next(
            snapshot
            for snapshot in snapshots
            if snapshot["snapshot_id"] == selected_snapshot_id
        )
        st.session_state["as_of_override"] = selected["committed_at"]
        st.rerun()


def _render_costs(controls: dict[str, Any], context: dict[str, Any]) -> None:
    costs = context.get("costs") or {}
    utilization = context.get("utilization") or {}
    cols = st.columns(2)
    cols[0].metric("Total cost", costs.get("total_cost", "0"))
    cols[1].metric("Currency", costs.get("currency", "-"))
    rows = cost_time_series_rows(costs)
    if rows:
        fig, ax = plt.subplots()
        ax.plot(
            [
                _parse_iso_datetime(row["starts_at"], dt.datetime.now(dt.UTC))
                for row in rows
            ],
            [row["cost_amount"] for row in rows],
        )
        ax.set_ylabel("Cost")
        _format_datetime_axis(ax.xaxis, controls["timezone"])
        fig.tight_layout()
        st.pyplot(fig)
    st.dataframe(
        format_display_datetimes(costs.get("by_resource", []), controls["timezone"]),
        use_container_width=True,
        hide_index=True,
    )
    st.dataframe(
        format_display_datetimes(costs.get("by_process", []), controls["timezone"]),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Utilization by resource")
    st.dataframe(
        format_display_datetimes(utilization.get("by_resource", []), controls["timezone"]),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Utilization by role")
    st.dataframe(
        format_display_datetimes(utilization.get("by_role", []), controls["timezone"]),
        use_container_width=True,
        hide_index=True,
    )


def _render_history(controls: dict[str, Any], context: dict[str, Any]) -> None:
    blockers = context.get("blockers") or {}
    st.subheader("Blocker history")
    st.dataframe(
        format_display_datetimes(blockers.get("blockers", []), controls["timezone"]),
        use_container_width=True,
        hide_index=True,
    )


def _render_topology(service, controls: dict[str, Any], context: dict[str, Any]) -> None:
    graph = context.get("full_graph") or context.get("graph") or {}
    catalog = catalog_from_query_data(context.get("catalog"), graph)
    node_by_symbol = {
        node.get("process_symbol"): node.get("process_id")
        for node in graph.get("nodes", [])
        if node.get("process_symbol") and node.get("process_id")
    }
    selected = st.multiselect("Collapse in graph view", catalog["process_symbols"])
    st.session_state["collapsed_process_ids"] = [
        node_by_symbol[symbol] for symbol in selected if symbol in node_by_symbol
    ]
    st.graphviz_chart(
        build_process_graph_dot(
            graph,
            collapsed_process_ids=set(st.session_state["collapsed_process_ids"]),
        ),
        use_container_width=True,
    )

    with st.expander("Replace subgraph with subgraph"):
        with st.form("replace_subgraph"):
            targets = st.multiselect(
                "Processes to replace",
                catalog["process_symbols"],
                help=(
                    "Defined process symbols to replace. External predecessors "
                    "will connect to new roots; new leaves will connect to "
                    "external children."
                ),
            )
            children = st.text_area(
                "Children",
                (
                    "A | First child | role_eng:8 | Define first child\n"
                    "B | Second child | role_eng:8 | Define second child"
                ),
                help=(
                    "Rows of new process symbol, name, role effort hours, and "
                    "optional description, for example "
                    "`A | Name | role_eng:8,*_qa:2 | Definition`."
                ),
            )
            dependencies = st.text_area(
                "Internal dependencies",
                "A -> B",
                help="Dependencies between child process symbols.",
            )
            preserve_alias = st.checkbox(
                "Preserve replaced symbols as aliases",
                False,
                help="Point each replaced process symbol at the first new root.",
            )
            replace = st.form_submit_button("Replace")
        if replace and targets:
            try:
                parsed_children = parse_subgraph_process_lines(
                    children,
                    catalog["role_ids"],
                )
                dependency_pairs = parse_dependency_lines(dependencies)
                root_symbols, _leaf_symbols = infer_subgraph_roots_and_leaves(
                    parsed_children,
                    dependency_pairs,
                )
                parsed_dependencies = [
                    {
                        "predecessor_symbol": predecessor,
                        "successor_symbol": successor,
                    }
                    for predecessor, successor in dependency_pairs
                ]
            except ValueError as exc:
                st.error(str(exc))
            else:
                inferred_alias_target = root_symbols[0] if root_symbols else None
                _apply_command(
                    service,
                    {
                        "action": "replace_process_with_subgraph",
                        "project_id": controls["project_id"],
                        "process_symbols": targets,
                        "edit_at": controls["as_of"],
                        "processes": parsed_children,
                        "dependencies": parsed_dependencies,
                        "preserve_parent_symbol_as_alias": preserve_alias,
                        "parent_alias_target_symbol": (
                            inferred_alias_target
                            if preserve_alias
                            else None
                        ),
                    },
                )

    with st.expander("Collapse subgraph into process"):
        with st.form("collapse_subgraph"):
            symbols = st.multiselect(
                "Process symbols",
                catalog["process_symbols"],
                help="Defined process symbols to summarize into one process.",
            )
            new_name = st.text_input(
                "New process name",
                help="Human-readable name for the replacement process.",
            )
            new_description = st.text_area(
                "New process description",
                help="Definition of done, scope, and PM notes for the replacement.",
            )
            collapse = st.form_submit_button("Collapse")
        if collapse and symbols:
            _apply_command(
                service,
                {
                    "action": "collapse_subgraph",
                    "project_id": controls["project_id"],
                    "edit_at": controls["as_of"],
                    "process_symbols": symbols,
                    "new_process": {
                        "name": new_name,
                        "description": new_description,
                    },
                },
            )


def _query(
    service,
    payload: dict[str, Any],
    *,
    key: str | None = None,
    render: bool = True,
) -> Any:
    try:
        result = service.handle_query(query_payload_envelope(payload))
    except (ValidationError, ValueError) as exc:
        st.error(str(exc))
        return None
    if render:
        _render_result(result)
    if not result.ok:
        return None
    if key is not None:
        return {key: result.data[key]} if key in result.data else result.data
    return result.data


def _apply_command(service, payload: dict[str, Any], *, rerun: bool = True):
    try:
        result = service.handle_command(command_payload_envelope(payload))
    except (ValidationError, ValueError) as exc:
        st.error(str(exc))
        return None
    _render_result(result)
    if result.ok and rerun:
        st.rerun()
    return result


def _apply_batch(service, payloads: list[dict[str, Any]], *, rerun: bool = True):
    try:
        results = service.handle_batch(batch_payload_envelope(payloads))
    except (ValidationError, ValueError) as exc:
        st.error(str(exc))
        return None
    for result in results:
        _render_result(result)
    if all(result.ok for result in results) and rerun:
        st.rerun()
    return results


def _render_result(result: Any) -> None:
    for warning in getattr(result, "warnings", []) or []:
        st.warning(f"{warning.code}: {warning.message}")
    if result.ok:
        return
    error = result.error
    st.error(f"{error.code}: {error.message}")
    details = result_to_dict(result).get("error", {}).get("details") or {}
    if details:
        with st.expander("Error details"):
            st.json(details)


def _parse_iso_datetime(value: Any, default: dt.datetime) -> dt.datetime:
    if value is None:
        return default
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _holiday_lines(holidays: list[dict[str, Any]]) -> str:
    lines = []
    for holiday in holidays:
        starts_at = _parse_iso_datetime(holiday.get("starts_at"), dt.datetime.now(dt.UTC))
        ends_at = _parse_iso_datetime(holiday.get("ends_at"), starts_at)
        fields = [
            holiday.get("holiday_id") or "",
            starts_at.isoformat(),
            ends_at.isoformat(),
        ]
        reason = holiday.get("reason")
        if reason:
            fields.append(str(reason))
        lines.append(" | ".join(fields))
    return "\n".join(lines)


def _weekly_windows(
    weekdays: list[int],
    start_time: dt.time,
    end_time: dt.time,
    capacity_hours: float,
) -> list[dict[str, Any]]:
    return [
        {
            "weekday": weekday,
            "start_local_time": start_time.strftime("%H:%M:%S"),
            "end_local_time": end_time.strftime("%H:%M:%S"),
            "capacity_hours": capacity_hours,
        }
        for weekday in weekdays
    ]


def _valid_process_symbols(
    symbols: list[str] | tuple[str, ...],
    graph: dict[str, Any],
) -> list[str]:
    valid_symbols = {
        node.get("process_symbol")
        for node in graph.get("nodes", [])
        if node.get("process_symbol")
    }
    return [symbol for symbol in symbols if symbol in valid_symbols]


def _terminal_scope(symbols: list[str]) -> dict[str, Any] | None:
    if not symbols:
        return None
    return {
        "type": "topo_filter",
        "root_process_symbols": symbols,
        "direction": "ancestors",
    }


def _barh_datetime(
    ax,
    starts_at: dt.datetime | None,
    ends_at: dt.datetime | None,
    y: float,
    height: float,
    **kwargs,
) -> None:
    if starts_at is None or ends_at is None:
        return
    start_num = mdates.date2num(starts_at)
    width = max(mdates.date2num(ends_at) - start_num, 1 / (24 * 60))
    ax.broken_barh([(start_num, width)], (y, height), **kwargs)


if __name__ == "__main__":
    main()
