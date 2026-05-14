"""Streamlit entrypoint for the service-backed ProjectDashboard UI."""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import matplotlib.pyplot as plt
import streamlit as st
from pydantic import ValidationError

from projdash.ui.adapters import (
    build_process_graph_dot,
    catalog_from_query_data,
    cost_time_series_rows,
    edge_table_rows,
    process_table_rows,
)
from projdash.ui.service_client import (
    DEFAULT_TIMEZONE,
    batch_payload_envelope,
    combine_datetime,
    command_payload_envelope,
    create_project_service,
    parse_dependency_lines,
    parse_holiday_lines,
    parse_resource_lines,
    parse_role_lines,
    parse_subgraph_process_lines,
    query_payload_envelope,
    result_to_dict,
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
    controls = _render_sidebar(db_path)

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
            "Processes",
            "Graph",
            "Resources",
            "Schedule",
            "Costs",
            "History",
            "Topology",
        ]
    )
    with tabs[0]:
        _render_dashboard(context)
    with tabs[1]:
        _render_processes(service, controls, context)
    with tabs[2]:
        _render_graph(context)
    with tabs[3]:
        _render_resources(service, controls, context)
    with tabs[4]:
        _render_schedule(context)
    with tabs[5]:
        _render_costs(context)
    with tabs[6]:
        _render_history(context)
    with tabs[7]:
        _render_topology(service, controls, context)


def _render_sidebar(db_path: str) -> dict[str, Any]:
    now_utc = dt.datetime.now(dt.UTC)
    st.sidebar.text_input("Service database", db_path, disabled=True)
    project_id = st.sidebar.text_input(
        "Project id",
        st.session_state.get("project_id", ""),
    ).strip()
    if project_id:
        st.session_state["project_id"] = project_id

    timezone_name = st.sidebar.text_input("Timezone", DEFAULT_TIMEZONE).strip()
    try:
        validate_timezone(timezone_name)
    except ValueError as exc:
        st.sidebar.error(str(exc))
        st.stop()
    as_of_date = st.sidebar.date_input("As of date", now_utc.date())
    as_of_time = st.sidebar.time_input("As of time", now_utc.time().replace(microsecond=0))
    horizon_days = st.sidebar.number_input("Horizon days", 1, 365, 45)
    now_at = combine_datetime(as_of_date, as_of_time, timezone_name)
    horizon_starts_at = combine_datetime(as_of_date, dt.time(0, 0), timezone_name)
    horizon_ends_at = horizon_starts_at + dt.timedelta(days=int(horizon_days))
    return {
        "project_id": project_id,
        "timezone": timezone_name,
        "as_of": now_at,
        "now": now_at,
        "horizon_starts_at": horizon_starts_at,
        "horizon_ends_at": horizon_ends_at,
    }


def _render_first_run(service, controls: dict[str, Any]) -> None:
    st.subheader("First-run setup")
    with st.form("first_run"):
        name = st.text_input("Project name", "New project")
        project_id = st.text_input("Project id", stable_id("project", name))
        currency = st.text_input("Default currency", "USD", max_chars=3)
        start_date = st.date_input("Project start date", controls["as_of"].date())
        start_time = st.time_input("Project start time", dt.time(9, 0))
        set_due = st.checkbox("Set project due date")
        due_date = st.date_input("Due date", controls["as_of"].date() + dt.timedelta(days=30))
        due_time = st.time_input("Due time", dt.time(17, 0))
        role_lines = st.text_area(
            "Roles",
            "role_engineer: Engineer\nrole_reviewer: Reviewer",
        )
        include_calendar = st.checkbox("Create default weekday calendar", True)
        work_start = st.time_input("Work starts", dt.time(9, 0))
        work_end = st.time_input("Work ends", dt.time(17, 0))
        capacity = st.number_input("Daily capacity hours", 0.0, 24.0, 8.0)
        resource_lines = st.text_area(
            "Resources",
            "Alice | role_engineer | 100\nBob | role_reviewer | 90",
        )
        holiday_lines = st.text_area(
            "Resource holidays",
            "",
        )
        submitted = st.form_submit_button("Create project")

    if not submitted:
        return

    try:
        start_at = combine_datetime(start_date, start_time, controls["timezone"])
        roles = parse_role_lines(role_lines)
        holidays = parse_holiday_lines(holiday_lines, controls["timezone"])
        resources = parse_resource_lines(resource_lines) if include_calendar else []
        commands = [
            {
                "action": "create_project",
                "project_id": project_id,
                "name": name,
                "start_at": start_at,
                "default_currency": currency,
            }
        ]
        for role in roles:
            commands.append(
                {
                    "action": "create_role",
                    "project_id": project_id,
                    "role_id": role.role_id,
                    "name": role.name,
                }
            )
        if set_due:
            commands.append(
                {
                    "action": "set_project_due_at",
                    "project_id": project_id,
                    "due_at": combine_datetime(due_date, due_time, controls["timezone"]),
                    "edit_at": controls["as_of"],
                }
            )
        if include_calendar:
            commands.append(
                {
                    "action": "upsert_resource_calendar",
                    "project_id": project_id,
                    "calendar_id": "cal_default",
                    "name": "Default weekday calendar",
                    "timezone": controls["timezone"],
                    "weekly_windows": _weekly_windows(
                        [0, 1, 2, 3, 4],
                        work_start,
                        work_end,
                        capacity,
                    ),
                    "active": True,
                }
            )
            for resource in resources:
                commands.append(
                    {
                        "action": "upsert_resource",
                        "project_id": project_id,
                        "resource_id": stable_id("res", resource.name),
                        "name": resource.name,
                        "role_ids": resource.role_ids,
                        "calendar_id": "cal_default",
                        "available_from_at": start_at,
                        "cost_rate": resource.cost_rate,
                        "cost_unit": "hour",
                        "cost_currency": currency,
                        "holidays": holidays,
                        "active": True,
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
        "blockers": None,
        "history": None,
        "catalog": None,
        "resource_schedule": None,
        "capacity": None,
        "utilization": None,
        "costs": None,
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
    base["graph"] = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": controls["as_of"],
            "now": controls["now"],
            "include_resource_fields": True,
            "horizon_starts_at": controls["horizon_starts_at"],
            "horizon_ends_at": controls["horizon_ends_at"],
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
            "include_project_total": True,
        },
    )
    resource_query = {
        "project_id": project_id,
        "as_of": controls["as_of"],
        "now": controls["now"],
        "horizon_starts_at": controls["horizon_starts_at"],
        "horizon_ends_at": controls["horizon_ends_at"],
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
            "horizon_starts_at": controls["horizon_starts_at"],
            "horizon_ends_at": controls["horizon_ends_at"],
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


def _render_processes(service, controls: dict[str, Any], context: dict[str, Any]) -> None:
    graph = context.get("graph") or {}
    catalog = catalog_from_query_data(
        context.get("catalog"),
        graph,
        context.get("blockers"),
    )
    st.subheader("Process plan")
    st.dataframe(process_table_rows(graph), use_container_width=True, hide_index=True)

    with st.expander("Create or revise process", expanded=True):
        with st.form("process_revision"):
            existing = st.selectbox("Existing process id", [""] + catalog["process_ids"])
            name = st.text_input("Name")
            duration = st.number_input("Duration business days", 0, 3650, 1)
            dependencies = st.multiselect("Dependencies", catalog["process_ids"])
            effective_date = st.date_input("Effective date", controls["as_of"].date())
            effective_time = st.time_input("Effective time", controls["as_of"].time())
            due_enabled = st.checkbox("Set process due date")
            due_date = st.date_input("Process due date", controls["as_of"].date())
            due_time = st.time_input("Process due time", dt.time(17, 0))
            earliest_enabled = st.checkbox("Set earliest start")
            earliest_date = st.date_input("Earliest start date", controls["as_of"].date())
            earliest_time = st.time_input("Earliest start time", dt.time(9, 0))
            role_id = st.selectbox("Required role id", [""] + catalog["role_ids"])
            effort = st.number_input("Effort hours for role", 0.0, 10000.0, 0.0)
            submitted = st.form_submit_button("Save revision")
        if submitted:
            payload = {
                "action": "upsert_process_revision",
                "project_id": controls["project_id"],
                "process_id": existing or None,
                "name": name,
                "effective_at": combine_datetime(
                    effective_date,
                    effective_time,
                    controls["timezone"],
                ),
                "duration_business_days": int(duration),
                "dependencies": dependencies,
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

    _render_process_status_form(service, controls, catalog)
    _render_process_due_form(service, controls, catalog)
    _render_blocker_forms(service, controls, catalog)


def _render_process_status_form(service, controls: dict[str, Any], catalog: dict[str, list[str]]):
    with st.expander("Set process status"):
        with st.form("process_status"):
            process_id = st.selectbox("Process id", [""] + catalog["process_ids"])
            status = st.selectbox(
                "Status",
                ["planned", "in_progress", "paused", "done", "canceled"],
            )
            finished_enabled = st.checkbox("Set finished time")
            finished_date = st.date_input("Finished date", controls["as_of"].date())
            finished_time = st.time_input("Finished time", controls["as_of"].time())
            note = st.text_input("Note")
            submitted = st.form_submit_button("Set status")
        if submitted and process_id:
            _apply_command(
                service,
                {
                    "action": "set_process_status",
                    "project_id": controls["project_id"],
                    "process_id": process_id,
                    "status": status,
                    "edit_at": controls["as_of"],
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


def _render_process_due_form(service, controls: dict[str, Any], catalog: dict[str, list[str]]):
    with st.expander("Set or clear due date"):
        with st.form("process_due"):
            process_id = st.selectbox("Process id", [""] + catalog["process_ids"], key="due_pid")
            clear_due = st.checkbox("Clear due date")
            due_date = st.date_input("Due date", controls["as_of"].date(), key="due_d")
            due_time = st.time_input("Due time", dt.time(17, 0), key="due_t")
            submitted = st.form_submit_button("Save due date")
        if submitted and process_id:
            _apply_command(
                service,
                {
                    "action": "set_process_due_at",
                    "project_id": controls["project_id"],
                    "process_id": process_id,
                    "due_at": None
                    if clear_due
                    else combine_datetime(due_date, due_time, controls["timezone"]),
                    "edit_at": controls["as_of"],
                },
            )


def _render_blocker_forms(service, controls: dict[str, Any], catalog: dict[str, list[str]]):
    with st.expander("Blockers"):
        with st.form("add_blocker"):
            process_id = st.selectbox("Process id", [""] + catalog["process_ids"], key="blk_pid")
            summary = st.text_input("Summary")
            details = st.text_area("Details")
            severity = st.selectbox("Severity", ["blocking", "warning", "info"])
            add = st.form_submit_button("Add blocker")
        if add and process_id:
            _apply_command(
                service,
                {
                    "action": "add_blocker",
                    "project_id": controls["project_id"],
                    "process_id": process_id,
                    "summary": summary,
                    "details": details or None,
                    "severity": severity,
                    "created_at": controls["as_of"],
                },
            )
        with st.form("resolve_blocker"):
            blocker_id = st.selectbox("Blocker id", [""] + catalog["blocker_ids"])
            resolution = st.text_input("Resolution")
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

    st.subheader("Capacity")
    st.dataframe(
        (context.get("capacity") or {}).get("buckets", []),
        use_container_width=True,
        hide_index=True,
    )
    _render_calendar_forms(service, controls, catalog)
    _render_resource_forms(service, controls, context, catalog)


def _render_role_forms(service, controls: dict[str, Any], catalog: dict[str, list[str]]):
    with st.expander("Role commands"):
        with st.form("create_role"):
            name = st.text_input("Role name")
            role_id = st.text_input("Role id")
            create = st.form_submit_button("Create role")
        if create:
            _apply_command(
                service,
                {
                    "action": "create_role",
                    "project_id": controls["project_id"],
                    "role_id": role_id or stable_id("role", name),
                    "name": name,
                },
            )
        with st.form("rename_role"):
            old_role = st.selectbox("Role id", [""] + catalog["role_ids"], key="ren_role")
            new_name = st.text_input("New role name")
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
            role_id = st.selectbox("Role id", [""] + catalog["role_ids"], key="deact_role")
            force = st.checkbox("Force")
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


def _render_calendar_forms(service, controls: dict[str, Any], catalog: dict[str, list[str]]):
    with st.expander("Calendar commands"):
        with st.form("upsert_calendar"):
            calendar_id = st.text_input("Calendar id", "cal_default")
            name = st.text_input("Calendar name", "Default weekday calendar")
            weekdays = st.multiselect(
                "Weekdays",
                [0, 1, 2, 3, 4, 5, 6],
                default=[0, 1, 2, 3, 4],
            )
            start_time = st.time_input("Window start", dt.time(9, 0))
            end_time = st.time_input("Window end", dt.time(17, 0))
            capacity = st.number_input("Capacity hours", 0.0, 24.0, 8.0)
            active = st.checkbox("Active", True)
            upsert = st.form_submit_button("Save calendar")
        if upsert:
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
            )
            starts_date = st.date_input("Starts", controls["as_of"].date())
            starts_time = st.time_input("Starts time", dt.time(0, 0))
            ends_date = st.date_input("Ends", controls["as_of"].date())
            ends_time = st.time_input("Ends time", dt.time(23, 59))
            exc_capacity = st.number_input("Exception capacity hours", 0.0, 24.0, 0.0)
            reason = st.text_input("Reason")
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
        )
        selected_resource = resources_by_id.get(selected_resource_id, {})
        form_key = selected_resource_id or "new"
        with st.form(f"upsert_resource_{form_key}"):
            resource_id = st.text_input(
                "Resource id",
                selected_resource.get("resource_id", ""),
                key=f"resource_id_{form_key}",
            )
            name = st.text_input(
                "Resource name",
                selected_resource.get("name", ""),
                key=f"resource_name_{form_key}",
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
            )
            extra_roles = st.text_input(
                "Additional role ids",
                key=f"resource_extra_roles_{form_key}",
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
            )
            available_from = _parse_iso_datetime(
                selected_resource.get("available_from_at"),
                controls["as_of"],
            )
            available_date = st.date_input(
                "Available from",
                available_from.date(),
                key=f"resource_available_date_{form_key}",
            )
            available_time = st.time_input(
                "Available from time",
                available_from.timetz().replace(tzinfo=None),
                key=f"resource_available_time_{form_key}",
            )
            cost_rate = st.text_input(
                "Cost rate",
                str(selected_resource.get("cost_rate", "0")),
                key=f"resource_cost_rate_{form_key}",
            )
            cost_currency = st.text_input(
                "Cost currency",
                selected_resource.get("cost_currency", "USD"),
                max_chars=3,
                key=f"resource_cost_currency_{form_key}",
            )
            cost_units = ["hour", "day", "week", "fixed"]
            selected_unit = selected_resource.get("cost_unit", "hour")
            cost_unit = st.selectbox(
                "Cost unit",
                cost_units,
                index=cost_units.index(selected_unit) if selected_unit in cost_units else 0,
                key=f"resource_cost_unit_{form_key}",
            )
            holidays = st.text_area(
                "Holidays",
                _holiday_lines(selected_resource.get("holidays", [])),
                key=f"resource_holidays_{form_key}",
            )
            active = st.checkbox(
                "Active resource",
                selected_resource.get("active", True),
                key=f"resource_active_{form_key}",
            )
            save = st.form_submit_button("Save resource")
        if save:
            try:
                all_roles = [*role_ids, *split_csv(extra_roles)]
                parsed_holidays = parse_holiday_lines(holidays, controls["timezone"])
            except ValueError as exc:
                st.error(str(exc))
            else:
                _apply_command(
                    service,
                    {
                        "action": "upsert_resource",
                        "project_id": controls["project_id"],
                        "resource_id": resource_id or stable_id("res", name),
                        "name": name,
                        "role_ids": all_roles,
                        "calendar_id": calendar_id,
                        "available_from_at": combine_datetime(
                            available_date,
                            available_time,
                            controls["timezone"],
                        ),
                        "cost_rate": cost_rate,
                        "cost_unit": cost_unit,
                        "cost_currency": cost_currency or None,
                        "holidays": parsed_holidays,
                        "active": active,
                    },
                )
        with st.form("resource_active"):
            rid = st.selectbox("Resource id", [""] + catalog["resource_ids"])
            active_state = st.checkbox("Active", True, key="res_active_state")
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


def _render_schedule(context: dict[str, Any]) -> None:
    schedule = context.get("resource_schedule") or {}
    st.metric("Converged", str(schedule.get("converged", "-")))
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


def _render_costs(context: dict[str, Any]) -> None:
    costs = context.get("costs") or {}
    utilization = context.get("utilization") or {}
    cols = st.columns(2)
    cols[0].metric("Total cost", costs.get("total_cost", "0"))
    cols[1].metric("Currency", costs.get("currency", "-"))
    rows = cost_time_series_rows(costs)
    if rows:
        fig, ax = plt.subplots()
        ax.plot([row["starts_at"] for row in rows], [row["cost_amount"] for row in rows])
        ax.set_ylabel("Cost")
        ax.tick_params(axis="x", rotation=30)
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
    graph = context.get("graph") or {}
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
            target = st.selectbox("Parent process id", [""] + catalog["process_ids"])
            children = st.text_area("Children", "A | First child | 8\nB | Second child | 8")
            dependencies = st.text_area("Internal dependencies", "A -> B")
            roots = st.text_input("Root symbols", "A")
            leaves = st.text_input("Leaf symbols", "B")
            alias_target = st.text_input("Parent alias target symbol", "A")
            preserve_alias = st.checkbox("Preserve parent symbol as alias", True)
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
                        "process_id": target,
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
            symbols = st.multiselect("Process symbols", catalog["process_symbols"])
            new_symbol = st.text_input("New process symbol")
            new_name = st.text_input("New process name")
            duration = st.number_input("Duration hours", 0.0, 10000.0, 8.0)
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
                        "duration_hours": duration,
                    },
                },
            )


def _query(service, payload: dict[str, Any], *, key: str | None = None) -> Any:
    try:
        result = service.handle_query(query_payload_envelope(payload))
    except (ValidationError, ValueError) as exc:
        st.error(str(exc))
        return None
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


if __name__ == "__main__":
    main()
