"""Streamlit entrypoint for the service-backed ProjectDashboard UI."""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import streamlit as st
from pydantic import ValidationError

from projdash.ui.adapters import (
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
    resource_utilization_heatmap,
    role_utilization_heatmap,
)
from projdash.ui.service_client import (
    DEFAULT_TIMEZONE,
    batch_payload_envelope,
    calendar_options,
    combine_datetime,
    command_payload_envelope,
    create_project_service,
    parse_dependency_lines,
    parse_subgraph_process_lines,
    project_options,
    query_payload_envelope,
    result_to_dict,
    scoped_id,
    split_csv,
    stable_id,
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
        _render_dashboard(context)
    with tabs[1]:
        _render_project_settings(service, controls, context)
    with tabs[2]:
        _render_processes(service, controls, context)
    with tabs[3]:
        _render_graph(context)
    with tabs[4]:
        _render_resources(service, controls, context)
    with tabs[5]:
        _render_schedule(context)
    with tabs[6]:
        _render_slippage(service, controls, context)
    with tabs[7]:
        _render_costs(context)
    with tabs[8]:
        _render_history(context)
    with tabs[9]:
        _render_topology(service, controls, context)


def _render_sidebar(db_path: str, projects: list[dict[str, Any]]) -> dict[str, Any]:
    now_utc = dt.datetime.now(dt.UTC)
    override_as_of = st.session_state.pop("as_of_override", None)
    if isinstance(override_as_of, str):
        override_as_of = _parse_iso_datetime(override_as_of, now_utc)
    default_as_of = override_as_of if isinstance(override_as_of, dt.datetime) else now_utc
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
    as_of_date = st.sidebar.date_input(
        "As of date",
        default_as_of.date(),
        help="Planning snapshot date for schedule and history queries.",
    )
    as_of_time = st.sidebar.time_input(
        "As of time",
        default_as_of.time().replace(microsecond=0),
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


def _snapshot_label(snapshot_id: str, snapshots: list[dict[str, Any]]) -> str:
    if not snapshot_id:
        return "Select a committed timestamp"
    rows = {snapshot["snapshot_id"]: snapshot for snapshot in snapshots}
    snapshot = rows.get(snapshot_id)
    if snapshot is None:
        return snapshot_id
    completion = snapshot.get("completion_at") or "unresolved"
    return f"{snapshot.get('committed_at')} -> {completion}"


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
        set_due = st.checkbox(
            "Set project due date",
            help="Optionally set an explicit whole-project due datetime.",
        )
        due_date = st.date_input(
            "Due date",
            controls["as_of"].date() + dt.timedelta(days=30),
            help="Explicit project due date.",
        )
        due_time = st.time_input(
            "Due time",
            dt.time(17, 0),
            help="Explicit project due time in the selected sidebar timezone.",
        )
        submitted = st.form_submit_button("Create project")

    if not submitted:
        return

    try:
        start_at = combine_datetime(start_date, start_time, controls["timezone"])
        commands = [
            {
                "action": "create_project",
                "project_id": project_id,
                "name": name,
                "start_at": start_at,
                "default_currency": currency,
            }
        ]
        if set_due:
            commands.append(
                {
                    "action": "set_project_due_at",
                    "project_id": project_id,
                    "due_at": combine_datetime(due_date, due_time, controls["timezone"]),
                    "edit_at": controls["as_of"],
                }
            )
        batch_results = _apply_batch(service, commands, rerun=False)
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
        "history": None,
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
    base["history"] = _query(
        service,
        {
            "action": "query_due_date_history",
            "project_id": project_id,
            "as_of": controls["as_of"],
            **scoped_query,
            "include_project_total": True,
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


def _render_dashboard(context: dict[str, Any]) -> None:
    project = context["project"]["project"]
    graph = context.get("graph") or {}
    blockers = context.get("blockers") or {}
    history = context.get("history") or {}
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
    st.dataframe(process_table_rows(graph), use_container_width=True, hide_index=True)
    due_cols = st.columns(3)
    due_cols[0].metric("Explicit due", history.get("current_project_due_at") or "-")
    due_cols[1].metric("Derived due", history.get("derived_project_due_at") or "-")
    due_cols[2].metric("Schedule basis", graph.get("schedule_basis", "-"))


def _render_project_settings(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> None:
    project = (context.get("project") or {}).get("project", {})
    st.subheader("Project settings")
    start_at = _parse_iso_datetime(project.get("start_at"), controls["as_of"])
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

    st.subheader("Project due date")
    history = context.get("history") or {}
    current_due = _parse_iso_datetime(
        history.get("current_project_due_at"),
        controls["as_of"] + dt.timedelta(days=30),
    )
    with st.form("project_due"):
        clear_due = st.checkbox(
            "Clear project due date",
            help="Remove the explicit due datetime for the whole project.",
        )
        due_date = st.date_input(
            "Due date",
            current_due.date(),
            help="Explicit whole-project due date.",
        )
        due_time = st.time_input(
            "Due time",
            current_due.timetz().replace(tzinfo=None),
            help="Explicit whole-project due time in the selected sidebar timezone.",
        )
        save_due = st.form_submit_button("Save project due date")
    if save_due:
        _apply_command(
            service,
            {
                "action": "clear_project_due_at" if clear_due else "set_project_due_at",
                "project_id": controls["project_id"],
                "edit_at": controls["as_of"],
                **(
                    {}
                    if clear_due
                    else {
                        "due_at": combine_datetime(
                            due_date,
                            due_time,
                            controls["timezone"],
                        )
                    }
                ),
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
) -> tuple[list[dict[str, Any]], list[str]]:
    rows = process_table_rows(graph)
    selected_symbols: list[str] = []
    try:
        event = st.dataframe(
            rows,
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
        st.dataframe(rows, use_container_width=True, hide_index=True)
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
    _rows, selected_symbols = _render_process_table(graph, key="process_table")

    with st.expander("Create or revise process", expanded=True):
        with st.form("process_revision"):
            default_symbol = selected_symbols[0] if len(selected_symbols) == 1 else ""
            symbol_options = [""] + catalog["process_symbols"]
            selected_index = (
                symbol_options.index(default_symbol)
                if default_symbol in symbol_options
                else 0
            )
            existing = st.selectbox(
                "Existing process",
                symbol_options,
                index=selected_index,
                help=(
                    "Choose a defined process to revise, or leave blank to create a "
                    "new process."
                ),
            )
            selected_node = node_by_symbol.get(existing, {})
            process_symbol = st.text_input(
                "New process symbol",
                help=(
                    "Canonical symbol for a new process. Existing processes use "
                    "the selector above."
                ),
                disabled=bool(existing),
            )
            name = st.text_input(
                "Name",
                selected_node.get("name", ""),
                help="Human-readable process name.",
            )
            dependency_options = allowed_dependency_symbols(graph, existing)
            dependency_defaults = [
                symbol
                for symbol in existing_dependency_symbols(graph, existing)
                if symbol in dependency_options
            ]
            dependencies = st.multiselect(
                "Dependencies",
                dependency_options,
                default=dependency_defaults,
                help="Defined predecessor processes that must finish first.",
            )
            effective_date = st.date_input(
                "Effective date",
                controls["as_of"].date(),
                help="Date this revision becomes active.",
            )
            effective_time = st.time_input(
                "Effective time",
                controls["as_of"].time(),
                help="Time this revision becomes active.",
            )
            due_enabled = st.checkbox(
                "Set process due date",
                help="Enable an explicit process due datetime.",
            )
            due_date = st.date_input(
                "Process due date",
                controls["as_of"].date(),
                help="Explicit due date for this process.",
            )
            due_time = st.time_input(
                "Process due time",
                dt.time(17, 0),
                help="Explicit due time for this process.",
            )
            earliest_enabled = st.checkbox(
                "Set earliest start",
                help="Enable a not-before datetime for this process.",
            )
            earliest_date = st.date_input(
                "Earliest start date",
                controls["as_of"].date(),
                help="Earliest allowed start date.",
            )
            earliest_time = st.time_input(
                "Earliest start time",
                dt.time(9, 0),
                help="Earliest allowed start time.",
            )
            role_id = st.selectbox(
                "Required role id",
                [""] + catalog["role_ids"],
                help="Defined role required by this process revision.",
            )
            effort = st.number_input(
                "Effort hours for role",
                0.0,
                10000.0,
                0.0,
                help="Total effort required from the selected role.",
            )
            submitted = st.form_submit_button("Save revision")
        if submitted:
            active_symbol = existing or process_symbol.strip()
            if not active_symbol:
                st.error("Process symbol is required for new processes.")
                return
            duration_days = int((selected_node.get("duration_hours") or 0) / 8)
            payload = {
                "action": "upsert_process_revision",
                "project_id": controls["project_id"],
                "process_symbol": active_symbol,
                "name": name,
                "effective_at": combine_datetime(
                    effective_date,
                    effective_time,
                    controls["timezone"],
                ),
                "duration_business_days": duration_days,
                "dependencies": [
                    id_by_symbol[symbol]
                    for symbol in dependencies
                    if symbol in id_by_symbol
                ],
                "due_at": combine_datetime(due_date, due_time, controls["timezone"])
                if due_enabled
                else None,
                "earliest_start_at": combine_datetime(
                    earliest_date,
                    earliest_time,
                    controls["timezone"],
                )
                if earliest_enabled
                else None,
                "role_requirements": [
                    {
                        "role_id": role_id,
                        "effort_hours": effort,
                    }
                ]
                if role_id and effort > 0
                else [],
            }
            _apply_command(service, payload)

    _render_batch_process_menu(service, controls, graph, catalog, selected_symbols)
    _render_process_status_form(service, controls, catalog, selected_symbols)
    _render_process_due_form(service, controls, catalog, selected_symbols)
    _render_blocker_forms(service, controls, catalog, selected_symbols)


def _render_batch_process_menu(
    service,
    controls: dict[str, Any],
    graph: dict[str, Any],
    catalog: dict[str, list[str]],
    selected_symbols: list[str],
) -> None:
    id_by_symbol, _symbol_by_id = process_symbol_maps(graph)
    with st.expander("Batch update processes"):
        default_symbols = [
            symbol for symbol in selected_symbols if symbol in catalog["process_symbols"]
        ]
        target_symbols = st.multiselect(
            "Selected process symbols",
            catalog["process_symbols"],
            default=default_symbols,
            help="Processes affected by the batch operation.",
        )
        predecessor_options = allowed_shared_dependency_symbols(graph, target_symbols)
        successor_options = allowed_successor_symbols(graph, target_symbols)

        with st.form("batch_add_predecessors"):
            predecessors = st.multiselect(
                "Predecessors to add",
                predecessor_options,
                help="Only symbols that can precede every selected process are shown.",
            )
            add_predecessors = st.form_submit_button("Add predecessors")
        if add_predecessors and target_symbols and predecessors:
            _apply_command(
                service,
                {
                    "action": "batch_update_process_graph",
                    "project_id": controls["project_id"],
                    "edit_at": controls["as_of"],
                    "operations": [
                        {
                            "action": "add_dependency",
                            "operation_id": f"add-{predecessor}-{target}",
                            "predecessor_process_symbol": predecessor,
                            "successor_process_symbol": target,
                        }
                        for target in target_symbols
                        for predecessor in predecessors
                    ],
                },
            )

        with st.form("batch_add_existing_children"):
            children = st.multiselect(
                "Existing children to add",
                successor_options,
                help="Only symbols that can follow every selected process are shown.",
            )
            add_children = st.form_submit_button("Add existing children")
        if add_children and target_symbols and children:
            _apply_command(
                service,
                {
                    "action": "batch_update_process_graph",
                    "project_id": controls["project_id"],
                    "edit_at": controls["as_of"],
                    "operations": [
                        {
                            "action": "add_dependency",
                            "operation_id": f"add-{parent}-{child}",
                            "predecessor_process_symbol": parent,
                            "successor_process_symbol": child,
                        }
                        for parent in target_symbols
                        for child in children
                    ],
                },
            )

        with st.form("batch_add_new_child"):
            child_symbol = st.text_input(
                "New child symbol",
                help="Canonical symbol for the new child process.",
            )
            child_name = st.text_input(
                "New child name",
                help="Human-readable name for the new child process.",
            )
            role_id = st.selectbox(
                "Required role",
                [""] + catalog["role_ids"],
                key="batch_child_role",
                help="Defined role required by the new child process.",
            )
            effort = st.number_input(
                "Effort hours",
                0.0,
                10000.0,
                0.0,
                key="batch_child_effort",
                help="Total role effort used by resource-aware scheduling.",
            )
            add_new_child = st.form_submit_button("Create child after selected")
        if add_new_child and target_symbols and child_symbol:
            _apply_command(
                service,
                {
                    "action": "upsert_process_revision",
                    "project_id": controls["project_id"],
                    "process_symbol": child_symbol,
                    "name": child_name,
                    "effective_at": controls["as_of"],
                    "duration_business_days": 0,
                    "dependencies": [
                        id_by_symbol[symbol]
                        for symbol in target_symbols
                        if symbol in id_by_symbol
                    ],
                    "role_requirements": [
                        {
                            "role_id": role_id,
                            "effort_hours": effort,
                        }
                    ]
                    if role_id and effort > 0
                    else [],
                },
            )

        with st.form("batch_collapse_selected"):
            new_symbol = st.text_input(
                "Collapsed process symbol",
                help="Canonical symbol for the replacement process.",
            )
            new_name = st.text_input(
                "Collapsed process name",
                help="Human-readable name for the replacement process.",
            )
            collapse = st.form_submit_button("Collapse selected")
        if collapse and target_symbols:
            _apply_command(
                service,
                {
                    "action": "collapse_subgraph",
                    "project_id": controls["project_id"],
                    "edit_at": controls["as_of"],
                    "process_symbols": target_symbols,
                    "new_process": {
                        "process_symbol": new_symbol,
                        "name": new_name,
                    },
                },
            )


def _render_process_status_form(
    service,
    controls: dict[str, Any],
    catalog: dict[str, list[str]],
    selected_symbols: list[str],
):
    with st.expander("Set process status"):
        with st.form("process_status"):
            default_symbol = selected_symbols[0] if len(selected_symbols) == 1 else ""
            symbol_options = [""] + catalog["process_symbols"]
            process_symbol = st.selectbox(
                "Process",
                symbol_options,
                index=(
                    symbol_options.index(default_symbol)
                    if default_symbol in symbol_options
                    else 0
                ),
                help="Defined process to update.",
            )
            status = st.selectbox(
                "Status",
                ["planned", "in_progress", "paused", "done", "canceled"],
                help="Lifecycle status for the selected process.",
            )
            started_enabled = st.checkbox(
                "Set started time",
                help="Record the actual start datetime that pins ES and LS.",
            )
            started_date = st.date_input(
                "Started date",
                controls["as_of"].date(),
                help="Actual start date.",
            )
            started_time = st.time_input(
                "Started time",
                controls["as_of"].time(),
                help="Actual start time.",
            )
            finished_enabled = st.checkbox(
                "Set finished time",
                help="Record a completion datetime when applicable.",
            )
            finished_date = st.date_input(
                "Finished date",
                controls["as_of"].date(),
                help="Completion date.",
            )
            finished_time = st.time_input(
                "Finished time",
                controls["as_of"].time(),
                help="Completion time.",
            )
            note = st.text_input("Note", help="Optional status note.")
            submitted = st.form_submit_button("Set status")
        if submitted and process_symbol:
            _apply_command(
                service,
                {
                    "action": "set_process_status",
                    "project_id": controls["project_id"],
                    "process_symbol": process_symbol,
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
                    "note": note or None,
                },
            )


def _render_process_due_form(
    service,
    controls: dict[str, Any],
    catalog: dict[str, list[str]],
    selected_symbols: list[str],
):
    with st.expander("Set or clear due date"):
        with st.form("process_due"):
            default_symbol = selected_symbols[0] if len(selected_symbols) == 1 else ""
            symbol_options = [""] + catalog["process_symbols"]
            process_symbol = st.selectbox(
                "Process",
                symbol_options,
                index=(
                    symbol_options.index(default_symbol)
                    if default_symbol in symbol_options
                    else 0
                ),
                key="due_pid",
                help="Defined process whose due datetime will change.",
            )
            clear_due = st.checkbox(
                "Clear due date",
                help="Remove the explicit due datetime.",
            )
            due_date = st.date_input(
                "Due date",
                controls["as_of"].date(),
                key="due_d",
                help="Explicit process due date.",
            )
            due_time = st.time_input(
                "Due time",
                dt.time(17, 0),
                key="due_t",
                help="Explicit process due time.",
            )
            submitted = st.form_submit_button("Save due date")
        if submitted and process_symbol:
            _apply_command(
                service,
                {
                    "action": "set_process_due_at",
                    "project_id": controls["project_id"],
                    "process_symbol": process_symbol,
                    "due_at": None
                    if clear_due
                    else combine_datetime(due_date, due_time, controls["timezone"]),
                    "edit_at": controls["as_of"],
                },
            )


def _render_blocker_forms(
    service,
    controls: dict[str, Any],
    catalog: dict[str, list[str]],
    selected_symbols: list[str],
):
    with st.expander("Blockers"):
        with st.form("add_blocker"):
            default_symbol = selected_symbols[0] if len(selected_symbols) == 1 else ""
            symbol_options = [""] + catalog["process_symbols"]
            process_symbol = st.selectbox(
                "Process",
                symbol_options,
                index=(
                    symbol_options.index(default_symbol)
                    if default_symbol in symbol_options
                    else 0
                ),
                key="blk_pid",
                help="Defined process that is blocked.",
            )
            summary = st.text_input("Summary", help="Short blocker summary.")
            details = st.text_area("Details", help="Optional blocker detail.")
            severity = st.selectbox(
                "Severity",
                ["blocking", "warning", "info"],
                help="Whether this blocker prevents work or is informational.",
            )
            add = st.form_submit_button("Add blocker")
        if add and process_symbol:
            _apply_command(
                service,
                {
                    "action": "add_blocker",
                    "project_id": controls["project_id"],
                    "process_symbol": process_symbol,
                    "summary": summary,
                    "details": details or None,
                    "severity": severity,
                    "created_at": controls["as_of"],
                },
            )
        with st.form("resolve_blocker"):
            blocker_id = st.selectbox(
                "Blocker id",
                [""] + catalog["blocker_ids"],
                help="Defined blocker to resolve.",
            )
            resolution = st.text_input("Resolution", help="Optional resolution note.")
            resolve = st.form_submit_button("Resolve blocker")
        if resolve and blocker_id:
            _apply_command(
                service,
                {
                    "action": "resolve_blocker",
                    "project_id": controls["project_id"],
                    "blocker_id": blocker_id,
                    "resolved_at": controls["as_of"],
                    "resolution": resolution or None,
                },
            )


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
    )
    st.subheader("Role utilization")
    _render_heatmap(
        "Role utilization",
        *role_utilization_heatmap(
            context.get("capacity") or {},
            context.get("resource_schedule") or {},
        ),
    )

    st.subheader("Capacity")
    st.dataframe(
        (context.get("capacity") or {}).get("buckets", []),
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
                    "timezone": controls["timezone"],
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
            starts_date = st.date_input(
                "Starts",
                controls["as_of"].date(),
                help="Exception start date.",
            )
            starts_time = st.time_input(
                "Starts time",
                dt.time(0, 0),
                help="Exception start time.",
            )
            ends_date = st.date_input(
                "Ends",
                controls["as_of"].date(),
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
                        controls["timezone"],
                    ),
                    "ends_at": combine_datetime(ends_date, ends_time, controls["timezone"]),
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
            available_from = _parse_iso_datetime(
                selected_resource.get("available_from_at"),
                controls["as_of"],
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
            cost_currency = st.text_input(
                "Cost currency",
                selected_resource.get("cost_currency", "USD"),
                max_chars=3,
                key=f"resource_cost_currency_{form_key}",
                help="ISO 4217 currency for this resource cost rate.",
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
                        controls["timezone"],
                    ),
                    "cost_rate": cost_rate,
                    "cost_unit": cost_unit,
                    "cost_currency": cost_currency or None,
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
        _render_resource_holiday_forms(service, controls, resources_by_id, catalog)


def _render_resource_holiday_forms(
    service,
    controls: dict[str, Any],
    resources_by_id: dict[str, dict[str, Any]],
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

    holidays = resource.get("holidays", [])
    with st.form("add_resource_holiday"):
        starts_date = st.date_input(
            "Holiday starts",
            controls["as_of"].date(),
            help="Holiday start date.",
        )
        starts_time = st.time_input(
            "Holiday starts time",
            dt.time(0, 0),
            help="Holiday start time.",
        )
        ends_date = st.date_input(
            "Holiday ends",
            controls["as_of"].date(),
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
        starts_at = combine_datetime(starts_date, starts_time, controls["timezone"])
        ends_at = combine_datetime(ends_date, ends_time, controls["timezone"])
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


def _render_schedule(context: dict[str, Any]) -> None:
    graph = context.get("graph") or {}
    full_graph = context.get("full_graph") or graph
    schedule = context.get("resource_schedule") or {}
    symbol_options = [
        node.get("process_symbol")
        for node in full_graph.get("nodes", [])
        if node.get("process_symbol")
    ]
    current_terminals = [
        symbol
        for symbol in st.session_state.get("terminal_process_symbols", [])
        if symbol in symbol_options
    ]
    terminal_symbols = st.multiselect(
        "Completion targets",
        symbol_options,
        default=current_terminals,
        help=(
            "Leave empty to plan to all terminal nodes. Select symbols to plan "
            "their ancestor subgraph."
        ),
    )
    if terminal_symbols != current_terminals:
        st.session_state["terminal_process_symbols"] = terminal_symbols
        st.rerun()

    horizon_start = context.get("horizon_starts_at")
    horizon_end = context.get("horizon_ends_at")
    if horizon_start and horizon_end:
        st.caption(
            f"Query horizon: {horizon_start.isoformat()} to {horizon_end.isoformat()}"
        )
    st.metric("Converged", str(schedule.get("converged", "-")))
    _render_gantt_chart(
        graph,
        controls_now=context.get("now"),
        terminal_symbols=terminal_symbols,
    )
    st.dataframe(schedule.get("processes", []), use_container_width=True, hide_index=True)
    st.subheader("Unallocated requirements")
    st.dataframe(
        schedule.get("unallocated_requirements", []),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Allocation slices")
    st.dataframe(
        schedule.get("allocation_slices", []),
        use_container_width=True,
        hide_index=True,
    )


def _render_gantt_chart(
    graph: dict[str, Any],
    *,
    controls_now: dt.datetime | None,
    terminal_symbols: list[str],
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
        y = len(rows) - index - 1
        _barh_datetime(
            ax,
            row["es_at"],
            row["ef_at"],
            y - 0.24,
            0.32,
            facecolor=color,
            edgecolor=color,
            alpha=0.85,
        )
        _barh_datetime(
            ax,
            row["ls_at"],
            row["lf_at"],
            y + 0.12,
            0.20,
            facecolor="none",
            edgecolor=color,
            alpha=1.0,
            linestyle="--",
        )
    if controls_now is not None:
        ax.axvline(mdates.date2num(controls_now), color="#111827", linewidth=1.2)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([row["symbol"] for row in reversed(rows)])
    ax.set_xlabel("Time")
    locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.grid(axis="x", color="#e5e7eb", linewidth=0.8)
    fig.tight_layout()
    st.pyplot(fig)


def _render_heatmap(
    title: str,
    labels: list[str],
    times: list[dt.datetime],
    matrix: list[list[float]],
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
    locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
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
        _apply_command(
            service,
            {
                "action": "commit_project_state",
                "project_id": controls["project_id"],
                "committed_at": controls["as_of"],
                "terminal_process_symbols": terminal_symbols,
                "note": note or None,
            },
        )

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
        locator = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        ax.yaxis.set_major_locator(locator)
        ax.yaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        fig.tight_layout()
        st.pyplot(fig)
    st.dataframe(snapshots, use_container_width=True, hide_index=True)

    if not snapshots:
        return
    snapshot_options = [snapshot["snapshot_id"] for snapshot in snapshots]
    selected_snapshot_id = st.selectbox(
        "Historical commit",
        [""] + snapshot_options,
        format_func=lambda value: _snapshot_label(value, snapshots),
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


def _render_costs(context: dict[str, Any]) -> None:
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
        locator = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        fig.tight_layout()
        st.pyplot(fig)
    st.dataframe(costs.get("by_resource", []), use_container_width=True, hide_index=True)
    st.dataframe(costs.get("by_process", []), use_container_width=True, hide_index=True)
    st.subheader("Utilization by resource")
    st.dataframe(
        utilization.get("by_resource", []),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Utilization by role")
    st.dataframe(utilization.get("by_role", []), use_container_width=True, hide_index=True)


def _render_history(context: dict[str, Any]) -> None:
    history = context.get("history") or {}
    blockers = context.get("blockers") or {}
    st.subheader("Process due-date events")
    st.dataframe(
        history.get("process_events", []),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Project due-date events")
    st.dataframe(
        history.get("project_total_events", []),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Blocker history")
    st.dataframe(blockers.get("blockers", []), use_container_width=True, hide_index=True)


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

    with st.expander("Replace process with subgraph"):
        with st.form("replace_subgraph"):
            target = st.selectbox(
                "Parent process",
                [""] + catalog["process_symbols"],
                help="Defined process to replace with a detailed subgraph.",
            )
            children = st.text_area(
                "Children",
                "A | First child | 8\nB | Second child | 8",
                help="Rows of new process symbol, name, and duration hours.",
            )
            dependencies = st.text_area(
                "Internal dependencies",
                "A -> B",
                help="Dependencies between child process symbols.",
            )
            roots = st.text_input(
                "Root symbols",
                "A",
                help="Comma-separated child symbols that receive original parents.",
            )
            leaves = st.text_input(
                "Leaf symbols",
                "B",
                help="Comma-separated child symbols that receive original children.",
            )
            alias_target = st.text_input(
                "Parent alias target symbol",
                "A",
                help="Child symbol that should keep the parent process symbol as an alias.",
            )
            preserve_alias = st.checkbox(
                "Preserve parent symbol as alias",
                True,
                help="Keep the replaced process symbol as an alias for a child process.",
            )
            replace = st.form_submit_button("Replace")
        if replace and target:
            try:
                parsed_children = parse_subgraph_process_lines(children)
                parsed_dependencies = [
                    {
                        "predecessor_symbol": predecessor,
                        "successor_symbol": successor,
                    }
                    for predecessor, successor in parse_dependency_lines(dependencies)
                ]
            except ValueError as exc:
                st.error(str(exc))
            else:
                _apply_command(
                    service,
                    {
                        "action": "replace_process_with_subgraph",
                        "project_id": controls["project_id"],
                        "process_symbol": target,
                        "edit_at": controls["as_of"],
                        "processes": parsed_children,
                        "dependencies": parsed_dependencies,
                        "root_symbols": split_csv(roots),
                        "leaf_symbols": split_csv(leaves),
                        "preserve_parent_symbol_as_alias": preserve_alias,
                        "parent_alias_target_symbol": alias_target or None,
                    },
                )

    with st.expander("Collapse subgraph into process"):
        with st.form("collapse_subgraph"):
            symbols = st.multiselect(
                "Process symbols",
                catalog["process_symbols"],
                help="Defined process symbols to summarize into one process.",
            )
            new_symbol = st.text_input(
                "New process symbol",
                help="Canonical symbol for the replacement process.",
            )
            new_name = st.text_input(
                "New process name",
                help="Human-readable name for the replacement process.",
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
                        "process_symbol": new_symbol,
                        "name": new_name,
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
