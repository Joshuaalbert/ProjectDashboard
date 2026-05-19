"""Streamlit entrypoint for the service-backed ProjectDashboard UI."""

from __future__ import annotations

import datetime as dt
import importlib
import inspect
import json
import os
import re
import subprocess
import threading
import uuid
from collections.abc import Iterable
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
    blocker_table_rows,
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
    schedule_time_span,
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
def _service(db_path: str, storage: str):
    return create_project_service(db_path, storage=storage)


_SLACK_RUN_JOBS: dict[str, dict[str, Any]] = {}
_SLACK_RUN_JOBS_LOCK = threading.Lock()
_SERVICE_ACCESS_LOCK = threading.RLock()
_MAIN_SECTIONS = [
    "Dashboard",
    "Project",
    "Context",
    "Processes",
    "Graph",
    "Blockers",
    "Resources",
    "Slack",
    "Schedule",
    "Slippage",
    "Costs",
    "History",
    "Topology",
]
_MAIN_SECTION_STATE_KEY = "projdash_previous_main_section"
_SLACK_ACTION_PASSPHRASE_PREFIX = "slack_action_passphrase_"


def _remember_main_section_and_clear_slack_action_passphrases(
    selected_section: str,
) -> None:
    previous_section = st.session_state.get(_MAIN_SECTION_STATE_KEY)
    for key in _slack_action_passphrase_keys_to_clear(
        previous_section,
        selected_section,
        st.session_state.keys(),
    ):
        _clear_session_keys(key)
    st.session_state[_MAIN_SECTION_STATE_KEY] = selected_section


def _slack_action_passphrase_keys_to_clear(
    previous_section: Any,
    selected_section: str,
    session_keys: Iterable[Any],
) -> list[str]:
    if previous_section != "Slack" or selected_section == "Slack":
        return []
    return [
        key
        for key in session_keys
        if isinstance(key, str) and key.startswith(_SLACK_ACTION_PASSPHRASE_PREFIX)
    ]


class _LockedProjectService:
    """Serialize background worker access to the cached in-process service."""

    def __init__(self, service) -> None:
        self._service = service

    def handle_query(self, envelope):
        with _SERVICE_ACCESS_LOCK:
            return self._service.handle_query(envelope)

    def handle_command(self, envelope):
        with _SERVICE_ACCESS_LOCK:
            return self._service.handle_command(envelope)

    def handle_batch(self, envelope):
        with _SERVICE_ACCESS_LOCK:
            return self._service.handle_batch(envelope)


def main() -> None:
    """Render the service-backed Streamlit application."""
    st.set_page_config(page_title="ProjectDashboard", layout="wide")
    st.title("ProjectDashboard")

    db_path = os.environ.get("PROJDASH_DB_PATH", "projdash.sqlite")
    storage = os.environ.get("PROJDASH_STORAGE", "auto")
    service = _service(db_path, storage)
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

    selected_section = st.radio(
        "Section",
        _MAIN_SECTIONS,
        horizontal=True,
        key="main_section",
        label_visibility="collapsed",
    )
    _remember_main_section_and_clear_slack_action_passphrases(selected_section)
    _prepare_context_for_section(service, controls, context, selected_section)
    if selected_section == "Dashboard":
        _render_dashboard(service, controls, context)
    elif selected_section == "Project":
        _render_project_settings(service, controls, context)
    elif selected_section == "Context":
        _render_context_summary(service, controls, context)
    elif selected_section == "Processes":
        _render_processes(service, controls, context)
    elif selected_section == "Graph":
        _render_graph(service, controls, context)
    elif selected_section == "Blockers":
        _render_blockers(service, controls, context)
    elif selected_section == "Resources":
        _render_resources(service, controls, context)
    elif selected_section == "Slack":
        _render_slack(service, controls, context, db_path)
    elif selected_section == "Schedule":
        _render_schedule(service, controls, context)
    elif selected_section == "Slippage":
        _render_slippage(service, controls, context)
    elif selected_section == "Costs":
        _render_costs(service, controls, context)
    elif selected_section == "History":
        _render_history(service, controls, context)
    elif selected_section == "Topology":
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
        help="Durable service database file.",
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


def _allocation_slice_span(
    slices: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    starts = [
        str(slice_data["starts_at"])
        for slice_data in slices
        if slice_data.get("starts_at")
    ]
    ends = [
        str(slice_data["ends_at"])
        for slice_data in slices
        if slice_data.get("ends_at")
    ]
    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


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
                st.session_state[effort_key] = 0
    requirements = []
    for role_id in selected_roles:
        effort_key = f"{key_prefix}_{role_id}_effort"
        effort_kwargs = {}
        if effort_key not in st.session_state:
            effort_kwargs["value"] = int(round(defaults.get(role_id, 0.0)))
        effort = st.number_input(
            f"{role_id} effort hours",
            0,
            10000,
            step=1,
            key=effort_key,
            help="Total whole-number effort hours required from this role.",
            **effort_kwargs,
        )
        if effort > 0:
            requirements.append({"role_id": role_id, "effort_hours": int(effort)})
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
    """Load only cheap project-wide data required before section rendering."""
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
        "agent_context": None,
        "scope": None,
        "terminal_symbols": [],
        "now": controls["now"],
    }
    if base["project"] is None:
        return base
    return base


def _prepare_context_for_section(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
    section: str,
) -> dict[str, Any]:
    """Populate the data required by one active UI section."""
    if section == "Dashboard":
        _ensure_catalog(service, controls, context)
        _ensure_blockers(service, controls, context)
    elif section == "Context":
        _ensure_catalog(service, controls, context)
        _ensure_agent_context(service, controls, context)
    elif section == "Processes":
        _ensure_catalog(service, controls, context)
        _ensure_graph_context(service, controls, context)
        _ensure_blockers(service, controls, context)
    elif section == "Graph":
        _ensure_graph_context(service, controls, context)
    elif section == "Blockers":
        _ensure_graph_context(service, controls, context)
        _ensure_blockers(service, controls, context)
    elif section == "Resources":
        _ensure_catalog(service, controls, context)
        _ensure_resource_schedule(service, controls, context)
        _ensure_utilization(service, controls, context)
    elif section == "Slack":
        _ensure_catalog(service, controls, context)
    elif section == "Schedule":
        _ensure_catalog(service, controls, context)
        _ensure_graph_context(service, controls, context)
        _ensure_resource_schedule(service, controls, context)
        _ensure_blockers(service, controls, context)
    elif section == "Slippage":
        _ensure_catalog(service, controls, context)
        _ensure_graph_context(service, controls, context)
    elif section == "Costs":
        _ensure_costs(service, controls, context)
        _ensure_utilization(service, controls, context)
    elif section == "History":
        _ensure_blockers(service, controls, context)
    elif section == "Topology":
        _ensure_catalog(service, controls, context)
        _ensure_graph_context(service, controls, context)
    return context


def _ensure_catalog(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if context.get("catalog") is not None:
        return context["catalog"]
    context["catalog"] = _query(
        service,
        {
            "action": "query_project_catalog",
            "project_id": controls["project_id"],
        },
    )
    return context["catalog"] or {}


def _ensure_graph_context(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if context.get("full_graph") is not None and context.get("graph") is not None:
        return context

    project_id = controls["project_id"]
    dependency_graph = _query(
        service,
        {
            "action": "query_process_graph",
            "project_id": project_id,
            "as_of": controls["as_of"],
            "now": controls["now"],
            "include_resource_fields": True,
            "include_allocation_slices": True,
        },
    )
    context["full_graph"] = dependency_graph
    terminal_symbols = _valid_process_symbols(
        st.session_state.get("terminal_process_symbols", []),
        dependency_graph or {},
    )
    scope = _terminal_scope(terminal_symbols)
    context["scope"] = scope
    context["terminal_symbols"] = terminal_symbols
    scoped_query = {"scope": scope} if scope else {}
    if scoped_query:
        context["graph"] = _query(
            service,
            {
                "action": "query_process_graph",
                "project_id": project_id,
                "as_of": controls["as_of"],
                "now": controls["now"],
                **scoped_query,
                "include_resource_fields": True,
                "include_allocation_slices": True,
            },
        )
    else:
        context["graph"] = dependency_graph
    return context


def _ensure_blockers(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if context.get("blockers") is not None:
        return context["blockers"]
    context["blockers"] = _query(
        service,
        {
            "action": "query_blockers",
            "project_id": controls["project_id"],
            "as_of": controls["as_of"],
            "include_resolved": True,
        },
    )
    return context["blockers"] or {}


def _resource_scope_query(
    controls: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    terminal_symbols = _context_terminal_symbols(context)
    scope = _terminal_scope(terminal_symbols)
    context["scope"] = scope
    context["terminal_symbols"] = terminal_symbols
    scoped_query = {"scope": scope} if scope else {}
    return {
        "project_id": controls["project_id"],
        "as_of": controls["as_of"],
        "now": controls["now"],
        **scoped_query,
    }


def _context_terminal_symbols(context: dict[str, Any]) -> list[str]:
    if context.get("terminal_symbols"):
        return list(context["terminal_symbols"])
    if context.get("full_graph"):
        return _valid_process_symbols(
            st.session_state.get("terminal_process_symbols", []),
            context.get("full_graph") or {},
        )
    return []


def _selected_slippage_milestone(
    context: dict[str, Any],
) -> dict[str, Any] | None:
    milestone_id = st.session_state.get("slippage_milestone_id")
    if not milestone_id:
        return None
    for milestone in (context.get("catalog") or {}).get("milestones", []):
        if milestone.get("milestone_id") == milestone_id:
            return milestone
    return None


def _schedule_snapshot_query_payload(
    controls: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    milestone = _selected_slippage_milestone(context)
    if milestone is not None:
        context["terminal_symbols"] = list(milestone.get("process_symbols") or [])
        return {
            "action": "query_schedule_snapshots",
            "project_id": controls["project_id"],
            "as_of": controls["as_of"],
            "milestone_id": milestone["milestone_id"],
        }
    terminal_symbols = _context_terminal_symbols(context)
    context["terminal_symbols"] = terminal_symbols
    return {
        "action": "query_schedule_snapshots",
        "project_id": controls["project_id"],
        "as_of": controls["as_of"],
        "terminal_process_symbols": terminal_symbols,
    }


def _commit_project_state_payload(
    controls: dict[str, Any],
    *,
    terminal_symbols: list[str],
    milestone: dict[str, Any] | None,
    committed_at: dt.datetime,
    note: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "commit_project_state",
        "project_id": controls["project_id"],
        "committed_at": committed_at,
        "note": note,
    }
    if milestone is not None:
        payload["milestone_id"] = milestone["milestone_id"]
    else:
        payload["terminal_process_symbols"] = terminal_symbols
    return payload


def _ensure_schedule_snapshots(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if context.get("schedule_snapshots") is not None:
        return context["schedule_snapshots"]
    query_payload = _schedule_snapshot_query_payload(controls, context)
    context["schedule_snapshots"] = _query(
        service,
        query_payload,
    )
    return context["schedule_snapshots"] or {}


def _ensure_resource_schedule(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if context.get("resource_schedule") is not None:
        return context["resource_schedule"]
    resource_query = _resource_scope_query(controls, context)
    context["resource_schedule"] = _query(
        service,
        {
            "action": "query_resource_schedule",
            **resource_query,
            "include_allocation_slices": True,
        },
    )
    return context["resource_schedule"] or {}


def _ensure_utilization(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if context.get("utilization") is not None:
        return context["utilization"]
    resource_query = _resource_scope_query(controls, context)
    context["utilization"] = _query(
        service,
        {"action": "query_utilization", **resource_query},
    )
    return context["utilization"] or {}


def _ensure_costs(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if context.get("costs") is not None:
        return context["costs"]
    resource_query = _resource_scope_query(controls, context)
    context["costs"] = _query(
        service,
        {"action": "query_costs", **resource_query},
    )
    return context["costs"] or {}


def _ensure_agent_context(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if context.get("agent_context") is not None:
        return context["agent_context"]
    terminal_symbols = _context_terminal_symbols(context)
    resource_query = {
        "project_id": controls["project_id"],
        "as_of": controls["as_of"],
        "now": controls["now"],
    }
    context["terminal_symbols"] = terminal_symbols
    context["agent_context"] = _query(
        service,
        {
            "action": "query_agent_context",
            **resource_query,
            "terminal_process_symbols": terminal_symbols,
            "snapshot_limit": 5,
        },
    )
    return context["agent_context"] or {}


def _render_dashboard(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> None:
    project = context["project"]["project"]
    catalog = _ensure_catalog(service, controls, context)
    blockers = _ensure_blockers(service, controls, context)
    unresolved = [
        blocker
        for blocker in blockers.get("blockers", [])
        if not blocker.get("is_resolved_as_of")
    ]
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Project", project["name"])
    col2.metric("Roles", len(catalog.get("roles", [])))
    col3.metric("Resources", len(catalog.get("resources", [])))
    col4.metric("Open blockers", len(unresolved))
    if unresolved:
        st.subheader("Open blockers")
        st.dataframe(
            format_display_datetimes(unresolved, controls["timezone"]),
            use_container_width=True,
            hide_index=True,
        )


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


def _render_context_summary(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> None:
    _ensure_catalog(service, controls, context)
    _ensure_agent_context(service, controls, context)
    markdown = _project_context_markdown(controls, context)
    st.markdown(markdown)
    st.text_area(
        "Markdown",
        markdown,
        height=420,
        help="Agent-readable project context summary.",
    )


def _project_context_markdown(
    controls: dict[str, Any],
    context: dict[str, Any],
) -> str:
    agent_context = context.get("agent_context") or {}
    project = agent_context.get("project") or {}
    summary = agent_context.get("summary") or {}
    slippage = agent_context.get("slippage") or {}
    schedule = agent_context.get("schedule") or {}
    prioritized_work = agent_context.get("prioritized_work") or {}
    blockers = agent_context.get("blockers") or []
    project_name = project.get("name") or controls.get("project_id") or "Project"
    timezone_name = controls.get("timezone", DEFAULT_TIMEZONE)
    as_of = agent_context.get("as_of") or controls.get("as_of")
    now = agent_context.get("now") or controls.get("now") or as_of
    terminal_symbols = (
        agent_context.get("canonical_terminal_process_symbols")
        or agent_context.get("terminal_process_symbols")
        or context.get("terminal_symbols")
        or []
    )
    scope_json = _markdown_json(
        agent_context.get("scope")
        or context.get("scope")
        or {"type": "project"}
    )
    completion_at = (
        summary.get("projected_completion_at")
        or schedule.get("completion_at")
        or None
    )
    completion = (
        _markdown_datetime(completion_at, timezone_name)
        if completion_at
        else "unresolved"
    )
    lines = [
        f"# {project_name}",
        "",
        "## Snapshot",
        f"- Project id: `{project.get('project_id', controls.get('project_id', ''))}`",
        f"- As of: {_markdown_datetime(as_of, timezone_name)}",
        f"- Now: {_markdown_datetime(now, timezone_name)}",
        f"- Scope: `{scope_json}`",
        (
            "- Completion targets: "
            f"{_markdown_code_list(terminal_symbols) or 'all terminal processes'}"
        ),
        f"- Projected completion: {completion}",
        (
            "- Completion change: "
            f"{_markdown_duration_hours(slippage.get('completion_change_hours', 0))}"
        ),
        (
            "- Total role effort: "
            f"{_markdown_duration_hours(summary.get('total_role_effort_hours', 0))}"
        ),
        (
            "- Processes: "
            f"{summary.get('process_count', 0)} "
            f"({summary.get('blocked_process_count', 0)} blocked)"
        ),
        f"- Dependencies: {summary.get('edge_count', 0)}",
        f"- Status counts: {_markdown_status_counts(summary)}",
        f"- Resource schedule converged: {summary.get('converged', '-')}",
        "",
        "## Critical Path",
    ]
    critical_path = summary.get("critical_path") or schedule.get("critical_path") or []
    if critical_path:
        lines.extend(f"- `{symbol}`" for symbol in critical_path)
    else:
        lines.append("- None")
    lines.extend(["", "## Role Priorities"])
    lines.extend(
        _agent_priority_group_lines(
            prioritized_work.get("by_role") or [],
            id_field="role_id",
            name_field="role_name",
        )
    )
    lines.extend(["", "## Resource Priorities"])
    lines.extend(
        _agent_priority_group_lines(
            prioritized_work.get("by_resource") or [],
            id_field="resource_id",
            name_field="resource_name",
        )
    )
    lines.extend(["", "## Schedule Watchlist"])
    lines.extend(_schedule_watchlist_lines(schedule, timezone_name))
    lines.extend(["", "## Open Blockers"])
    unresolved = [
        blocker for blocker in blockers if not blocker.get("is_resolved_as_of")
    ]
    if unresolved:
        for blocker in unresolved:
            process_symbol = blocker.get("process_symbol") or blocker.get("process_id")
            lines.append(
                "- "
                f"[{blocker.get('severity', 'blocking')}] "
                f"`{process_symbol}`: {blocker.get('summary', '')}"
            )
    else:
        lines.append("- None")
    lines.extend(["", "## Resource Calendar Rules"])
    lines.append(
        _resource_calendar_rules_markdown(
            context.get("catalog") or {},
            timezone_name,
        )
    )
    available_queries = agent_context.get("available_queries") or []
    if available_queries:
        lines.extend(["", "## Follow-up Queries"])
        lines.extend(f"- `{query_name}`" for query_name in available_queries)
    return "\n".join(lines)


def _markdown_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=_json_default)


def _markdown_code_list(values: list[Any] | tuple[Any, ...]) -> str:
    return ", ".join(f"`{value}`" for value in values if value)


def _markdown_status_counts(summary: dict[str, Any]) -> str:
    counts = summary.get("status_counts") or {}
    if not counts:
        return "unknown"
    return ", ".join(f"{status}: {count}" for status, count in sorted(counts.items()))


def _markdown_duration_hours(value: Any) -> str:
    if value is None:
        return "unknown"
    return _format_priority_hours(value, "hour")


def _agent_priority_group_lines(
    groups: list[dict[str, Any]],
    *,
    id_field: str,
    name_field: str,
    limit: int = 6,
) -> list[str]:
    if not groups:
        return ["- None"]

    lines = []
    for group in groups:
        group_id = group.get(id_field) or "unassigned"
        name = group.get(name_field) or group_id
        processes = group.get("processes") or []
        lines.append(f"- **{name}** (`{group_id}`)")
        if not processes:
            lines.append("  - No active priorities")
            continue
        for process in processes[:limit]:
            lines.append(f"  - {_agent_priority_process_markdown(process)}")
        remaining = len(processes) - limit
        if remaining > 0:
            lines.append(f"  - {remaining} more")
    return lines


def _agent_priority_process_markdown(row: dict[str, Any]) -> str:
    priority = row.get("priority") or "-"
    symbol = row.get("process_symbol") or "-"
    name = row.get("process_name") or ""
    label = f"**{priority}** `{symbol}`{f' - {name}' if name else ''}"
    details = [
        f"start window: {_format_time_until_ls(row.get('hours_until_ls'))}",
        f"effort: {_format_priority_hours(row.get('effort_hours'), 'hour')}",
    ]
    status = row.get("computed_status") or row.get("status")
    if status:
        details.append(f"status: {status}")
    role_ids = row.get("role_ids") or []
    if role_ids:
        details.append(f"roles: {_markdown_code_list(role_ids)}")
    blocking_count = int(row.get("blocking_count") or 0)
    if blocking_count:
        details.append(f"blockers: {blocking_count}")
    return f"{label}; {'; '.join(details)}"


def _schedule_watchlist_lines(
    schedule: dict[str, Any],
    timezone_name: str,
    *,
    limit: int = 12,
) -> list[str]:
    rows = [
        row
        for row in schedule.get("processes", [])
        if row.get("status") not in {"done", "canceled"}
    ]
    if not rows:
        return ["- None"]
    rows = sorted(
        rows,
        key=lambda row: (
            not bool(row.get("critical")),
            _parse_iso_datetime(row.get("ls_at"), dt.datetime.max.replace(tzinfo=dt.UTC)),
            str(row.get("symbol") or ""),
        ),
    )
    lines = []
    for row in rows[:limit]:
        label = "critical" if row.get("critical") else "watch"
        symbol = row.get("symbol") or "-"
        name = row.get("name") or ""
        status = row.get("computed_status") or row.get("status") or "-"
        ls_at = _markdown_datetime(row.get("ls_at"), timezone_name)
        ends_at = _markdown_datetime(row.get("ends_at"), timezone_name)
        slack = _markdown_duration_hours(row.get("slack_hours"))
        allocation = row.get("allocation_state") or "-"
        lines.append(
            "- "
            f"**{label}** `{symbol}`"
            f"{f' - {name}' if name else ''}; "
            f"status: {status}; "
            f"LS: {ls_at}; "
            f"ends: {ends_at}; "
            f"slack: {slack}; "
            f"allocation: {allocation}"
        )
    remaining = len(rows) - limit
    if remaining > 0:
        lines.append(f"- {remaining} more")
    return lines


def _markdown_datetime(value: Any, timezone_name: str) -> str:
    if value is None or value == "":
        return "-"
    return to_display_timezone(value, timezone_name).strftime("%Y-%m-%d %H:%M %Z")


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
    _ensure_catalog(service, controls, context)
    _ensure_graph_context(service, controls, context)
    _ensure_blockers(service, controls, context)
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
    _render_milestone_menu(
        service,
        controls,
        catalog["process_symbols"],
        context.get("catalog", {}).get("milestones", []),
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
                    "start_at_earliest": bool(node.get("start_at_earliest", False)),
                    "delay_after_dependencies_business_days": int(
                        node.get("delay_after_dependencies_business_days") or 0
                    ),
                    "role_requirements": (
                        role_requirements_by_symbol.get(symbol, [])
                        if update_roles
                        else current_role_requirements
                    ),
                    "staked_resource_ids": node.get("staked_resource_ids") or [],
                    "assumption_note": node.get("assumption_note"),
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


def _render_milestone_menu(
    service,
    controls: dict[str, Any],
    process_symbols: list[str],
    milestones: list[dict[str, Any]],
) -> None:
    with st.expander("Milestones"):
        milestone_options = {
            f"{milestone.get('name') or milestone['milestone_id']} "
            f"({milestone['milestone_id']})": milestone
            for milestone in milestones
            if milestone.get("milestone_id")
        }
        selected_label = st.selectbox(
            "Milestone",
            ["Create new", *milestone_options],
            key="process_milestone_selected",
            help="Named process subsets used for milestone slippage tracking.",
        )
        selected = (
            milestone_options[selected_label]
            if selected_label in milestone_options
            else None
        )
        selected_id = selected.get("milestone_id") if selected else "new"
        name = st.text_input(
            "Milestone name",
            value=selected.get("name", "") if selected else "",
            key=f"process_milestone_name_{selected_id}",
            help="Human-readable milestone name.",
        )
        description = st.text_area(
            "Milestone description",
            value=selected.get("description", "") if selected else "",
            key=f"process_milestone_description_{selected_id}",
            help="Scope or delivery meaning for this milestone.",
        )
        default_processes = [
            symbol
            for symbol in (selected.get("process_symbols", []) if selected else [])
            if symbol in process_symbols
        ]
        milestone_processes = st.multiselect(
            "Milestone processes",
            process_symbols,
            default=default_processes,
            key=f"process_milestone_processes_{selected_id}",
            help="Terminal or checkpoint processes whose slippage defines the milestone.",
        )
        active = st.checkbox(
            "Active",
            value=bool(selected.get("active", True)) if selected else True,
            key=f"process_milestone_active_{selected_id}",
            help="Inactive milestones are hidden from agent context.",
        )
        save = st.button(
            "Save milestone",
            key=f"process_milestone_save_{selected_id}",
        )
        deactivate = (
            st.button(
                "Deactivate milestone",
                key=f"process_milestone_deactivate_{selected_id}",
            )
            if selected and selected.get("active", True)
            else False
        )

    if save and name and milestone_processes:
        payload = {
            "action": "upsert_milestone",
            "project_id": controls["project_id"],
            "name": name,
            "description": description,
            "process_symbols": milestone_processes,
            "active": active,
            "edit_at": controls["as_of"],
        }
        if selected:
            payload["milestone_id"] = selected["milestone_id"]
        _apply_command(service, payload)
    if deactivate and selected:
        _apply_command(
            service,
            {
                "action": "set_milestone_active",
                "project_id": controls["project_id"],
                "milestone_id": selected["milestone_id"],
                "active": False,
                "edit_at": controls["as_of"],
            },
        )


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
                shares = _integer_effort_shares(
                    selected_symbols,
                    int(round(target_total)),
                    {
                        item: current_by_symbol.get(item, {}).get(role_id, 0.0)
                        for item in selected_symbols
                    },
                )
            else:
                shares = _integer_effort_shares(
                    selected_symbols,
                    int(round(target_total)),
                    {},
                )
            effort_hours = shares[symbol]
            if effort_hours > 0:
                output[symbol].append(
                    {"role_id": role_id, "effort_hours": effort_hours}
                )
    return output


def _integer_effort_shares(
    symbols: list[str],
    total_effort: int,
    weights: dict[str, float],
) -> dict[str, int]:
    if total_effort <= 0 or not symbols:
        return {symbol: 0 for symbol in symbols}
    weight_total = sum(max(0.0, weights.get(symbol, 0.0)) for symbol in symbols)
    if weight_total <= 0:
        weight_total = float(len(symbols))
        weights = {symbol: 1.0 for symbol in symbols}

    raw_shares = [
        (
            symbol,
            total_effort * max(0.0, weights.get(symbol, 0.0)) / weight_total,
        )
        for symbol in symbols
    ]
    output = {symbol: int(share) for symbol, share in raw_shares}
    remainder = total_effort - sum(output.values())
    fractional_order = sorted(
        raw_shares,
        key=lambda item: (item[1] - int(item[1]), item[0]),
        reverse=True,
    )
    for symbol, _share in fractional_order[:remainder]:
        output[symbol] += 1
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


def _render_graph(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
) -> None:
    _ensure_graph_context(service, controls, context)
    graph = context.get("graph") or {}
    collapsed = set(st.session_state.get("collapsed_process_ids", []))
    if graph.get("nodes"):
        st.graphviz_chart(
            build_process_graph_dot(graph, collapsed_process_ids=collapsed),
            use_container_width=True,
        )
    st.dataframe(edge_table_rows(graph), use_container_width=True, hide_index=True)


def _render_blockers(service, controls: dict[str, Any], context: dict[str, Any]) -> None:
    blockers = _ensure_blockers(service, controls, context)
    _ensure_graph_context(service, controls, context)
    graph = context.get("full_graph") or context.get("graph") or {}
    schedule = (
        graph
        if graph.get("allocation_slices")
        else context.get("resource_schedule") or {}
    )
    rows = blocker_table_rows(
        blockers,
        graph,
        schedule,
        context.get("now") or controls["now"],
    )
    open_rows = [row for row in rows if not row.get("is_resolved_as_of")]
    blocking_rows = [row for row in rows if row.get("is_blocking_as_of")]
    col1, col2, col3 = st.columns(3)
    col1.metric("Blockers", len(rows))
    col2.metric("Open", len(open_rows))
    col3.metric("Blocking now", len(blocking_rows))

    if not rows:
        st.write("No blockers recorded.")
        return
    sections = _blocker_sections(rows)
    _render_blocker_section(
        service,
        controls,
        "Unresolved blockers",
        sections["unresolved"],
        empty_text="No unresolved blockers.",
    )
    _render_blocker_section(
        service,
        controls,
        "Resolved blockers",
        sections["resolved"],
        empty_text="No resolved blockers.",
    )


def _blocker_sections(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "unresolved": [
            row for row in rows if not row.get("is_resolved_as_of")
        ],
        "resolved": [row for row in rows if row.get("is_resolved_as_of")],
    }


def _render_blocker_section(
    service,
    controls: dict[str, Any],
    title: str,
    rows: list[dict[str, Any]],
    *,
    empty_text: str,
) -> None:
    st.subheader(title)
    if not rows:
        st.write(empty_text)
        return
    for row in rows:
        with st.expander(_blocker_expander_header(row), expanded=False):
            _render_blocker_expander(service, controls, row)


def _blocker_expander_header(row: dict[str, Any]) -> str:
    status = row.get("blocker_status") or "-"
    priority = row.get("priority") or "-"
    symbol = row.get("process_symbol") or row.get("blocker_id") or "-"
    summary = row.get("summary") or "Untitled blocker"
    return " | ".join(str(part) for part in (status, priority, symbol, summary) if part)


def _render_blocker_expander(
    service,
    controls: dict[str, Any],
    row: dict[str, Any],
) -> None:
    st.markdown(_blocker_detail_markdown(row, controls["timezone"]))
    blocker_id = row.get("blocker_id")
    if not blocker_id:
        st.warning("This blocker has no blocker id and cannot be updated.")
        return
    is_resolved = bool(row.get("is_resolved_as_of"))
    key = f"blocker_resolved_{blocker_id}"
    baseline_key = f"{key}_baseline"
    if st.session_state.get(baseline_key) != is_resolved:
        st.session_state[key] = is_resolved
        st.session_state[baseline_key] = is_resolved
    resolved_clicked = st.checkbox(
        "Resolved",
        value=is_resolved,
        key=key,
        help="Check to resolve this blocker now, or uncheck to move it back to unresolved.",
    )
    if resolved_clicked and not is_resolved:
        edit_at = _current_ui_datetime(controls["timezone"])
        _apply_command(
            service,
            {
                "action": "resolve_blocker",
                "project_id": controls["project_id"],
                "blocker_id": blocker_id,
                "resolved_at": edit_at,
                "resolution": "Resolved from the blockers tab.",
            },
        )
    if not resolved_clicked and is_resolved:
        edit_at = _current_ui_datetime(controls["timezone"])
        _apply_command(
            service,
            {
                "action": "reopen_blocker",
                "project_id": controls["project_id"],
                "blocker_id": blocker_id,
                "edit_at": edit_at,
                "note": "Reopened from the blockers tab.",
            },
        )


def _blocker_detail_markdown(row: dict[str, Any], timezone_name: str) -> str:
    process_symbol = row.get("process_symbol") or "-"
    process_name = row.get("process_name") or ""
    process_label = (
        f"`{process_symbol}` - {process_name}" if process_name else f"`{process_symbol}`"
    )
    details = row.get("details") or "No details recorded."
    lines = [
        f"- Process: {process_label}",
        f"- Process status: {_blocker_process_status_text(row)}",
        f"- Priority: {row.get('priority') or '-'}",
        f"- Severity: {row.get('severity') or '-'}",
        f"- Roles: {row.get('role_ids') or '-'}",
        f"- Resources: {row.get('resource_ids') or '-'}",
        f"- Created: {_markdown_datetime(row.get('created_at'), timezone_name)}",
        f"- Details: {details}",
    ]
    if row.get("resolved_at"):
        lines.append(f"- Resolved: {_markdown_datetime(row.get('resolved_at'), timezone_name)}")
    if row.get("resolution"):
        lines.append(f"- Resolution: {row['resolution']}")
    lines.append(f"- Blocker id: `{row.get('blocker_id') or '-'}`")
    return "\n".join(lines)


def _blocker_process_status_text(row: dict[str, Any]) -> str:
    status = row.get("process_status")
    computed_status = row.get("computed_status")
    if status and computed_status and status != computed_status:
        return f"`{status}` (currently `{computed_status}`)"
    if computed_status:
        return f"`{computed_status}`"
    if status:
        return f"`{status}`"
    return "-"


def _render_resources(service, controls: dict[str, Any], context: dict[str, Any]) -> None:
    _ensure_catalog(service, controls, context)
    _ensure_resource_schedule(service, controls, context)
    _ensure_utilization(service, controls, context)
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

    st.subheader("Utilization")
    _render_utilization_heatmaps(
        context.get("utilization") or {},
        context.get("resource_schedule") or {},
        timezone_name=controls["timezone"],
    )

    st.subheader("Resource calendar rules")
    st.markdown(
        _resource_calendar_rules_markdown(
            context.get("catalog") or {},
            controls["timezone"],
        )
    )
    _render_calendar_forms(service, controls, context, catalog)
    _render_resource_forms(service, controls, context, catalog)


def _resource_calendar_rules_markdown(
    catalog_data: dict[str, Any],
    timezone_name: str,
) -> str:
    calendars = {
        calendar["calendar_id"]: calendar
        for calendar in catalog_data.get("calendars", [])
    }
    resources = sorted(
        catalog_data.get("resources", []),
        key=lambda resource: (
            str(resource.get("name", "")).casefold(),
            str(resource.get("resource_id", "")),
        ),
    )
    if not resources:
        return "_No resources configured._"
    sections = []
    for resource in resources:
        resource_id = resource.get("resource_id", "")
        name = resource.get("name") or resource_id
        default_calendar = calendars.get(resource.get("calendar_id"), {})
        available_from = format_display_datetime(
            resource.get("available_from_at"),
            timezone_name,
        )
        available_until = (
            format_display_datetime(resource.get("available_until_at"), timezone_name)
            if resource.get("available_until_at")
            else "unbounded"
        )
        lines = [
            f"### {name} (`{resource_id}`)",
            (
                "- Availability: "
                f"{available_from} to {available_until}"
            ),
            (
                "- Default: "
                f"**{default_calendar.get('name', resource.get('calendar_id'))}** "
                f"(`{resource.get('calendar_id')}`)"
                f" in {default_calendar.get('timezone', timezone_name)}"
            ),
        ]
        overrides = sorted(
            resource.get("calendar_overrides") or [],
            key=lambda rule: str(rule.get("starts_at", "")),
        )
        if overrides:
            for override in overrides:
                calendar = calendars.get(override.get("calendar_id"), {})
                starts_at = format_display_datetime(
                    override.get("starts_at"),
                    timezone_name,
                )
                ends_at = (
                    format_display_datetime(override.get("ends_at"), timezone_name)
                    if override.get("ends_at")
                    else "unbounded"
                )
                reason = override.get("reason")
                suffix = f" - {reason}" if reason else ""
                lines.append(
                    "- "
                    f"Override `{override.get('rule_id')}`: "
                    f"**{calendar.get('name', override.get('calendar_id'))}** "
                    f"(`{override.get('calendar_id')}`), "
                    f"{starts_at} to {ends_at}{suffix}"
                )
        else:
            lines.append("- Overrides: none")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _capacity_buckets_for_display(
    buckets: list[dict[str, Any]],
    utilization: dict[str, Any],
) -> list[dict[str, Any]]:
    utilization_by_bucket = {
        (
            row.get("resource_id"),
            row.get("starts_at"),
            row.get("ends_at"),
        ): row
        for row in utilization.get("time_series", [])
    }
    rows = []
    for bucket in buckets:
        row = dict(bucket)
        utilization_row = utilization_by_bucket.get(
            (
                row.get("resource_id"),
                row.get("starts_at"),
                row.get("ends_at"),
            ),
        )
        if utilization_row is not None:
            allocated_hours = float(utilization_row.get("allocated_hours") or 0)
            capacity_hours = float(row.get("capacity_hours") or 0)
            row["allocated_hours"] = allocated_hours
            row["remaining_hours"] = capacity_hours - allocated_hours
        rows.append(row)
    return rows


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
                    "calendar_overrides": selected_resource.get(
                        "calendar_overrides",
                        [],
                    ),
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
        "calendar_overrides": resource.get("calendar_overrides", []),
        "active": resource.get("active", True),
    }


def _render_slack(
    service,
    controls: dict[str, Any],
    context: dict[str, Any],
    db_path: str,
) -> None:
    project_id = controls["project_id"]
    _ensure_catalog(service, controls, context)
    slack_data = _optional_service_query(
        service,
        {"action": "query_slack_project_config", "project_id": project_id},
    ) or {
        "project_id": project_id,
        "config": {},
        "resource_mappings": [],
        "collection_cursors": [],
    }
    catalog_data = context.get("catalog") or {}
    resources = sorted(
        catalog_data.get("resources", []),
        key=lambda resource: (
            str(resource.get("name", "")).casefold(),
            str(resource.get("resource_id", "")),
        ),
    )

    st.subheader("Slack")
    st.caption(
        "Project-scoped Slack setup, collection, draft review, and delivery."
    )
    _render_slack_notice(project_id)
    _render_slack_manifest_section(project_id, context)
    _render_slack_settings_section(service, controls, slack_data)
    _render_slack_token_section(service, controls, slack_data)

    action_passphrase_key = f"slack_action_passphrase_{project_id}"
    _consume_session_clear(action_passphrase_key)
    action_passphrase = st.text_input(
        "Passphrase for Slack actions",
        type="password",
        key=action_passphrase_key,
        help=(
            "Used only to decrypt the stored Slack bot token for discovery, "
            "verification, run-once, and sending."
        ),
    )

    _render_slack_mapping_section(
        service,
        controls,
        resources,
        slack_data,
        action_passphrase,
    )
    _render_slack_run_section(
        service,
        controls,
        db_path,
        slack_data,
        action_passphrase,
    )
    _render_slack_draft_section(
        service,
        controls,
        slack_data,
        resources,
        action_passphrase,
    )
    _render_slack_history_section(service, controls, resources)


def _set_slack_notice(project_id: str, level: str, message: str) -> None:
    st.session_state[f"slack_notice_{project_id}"] = {
        "level": level,
        "message": message,
    }


def _render_slack_notice(project_id: str) -> None:
    notice = st.session_state.pop(f"slack_notice_{project_id}", None)
    if not isinstance(notice, dict):
        return
    message = str(notice.get("message") or "")
    if not message:
        return
    level = str(notice.get("level") or "info")
    if level == "success":
        st.success(message)
    elif level == "error":
        st.error(message)
    elif level == "warning":
        st.warning(message)
    else:
        st.info(message)


def _render_slack_manifest_section(project_id: str, context: dict[str, Any]) -> None:
    project = ((context.get("project") or {}).get("project") or {})
    default_name = f"{project.get('name') or project_id} Bot"
    with st.expander("App manifest", expanded=True):
        name = st.text_input(
            "Slack app name",
            default_name,
            key=f"slack_manifest_name_{project_id}",
            help="Name used in the generated Slack app manifest.",
        )
        manifest_payload = _slack_manifest_payload(project_id, name)
        st.json(manifest_payload)


def _render_slack_settings_section(
    service,
    controls: dict[str, Any],
    slack_data: dict[str, Any],
) -> None:
    project_id = controls["project_id"]
    config = slack_data.get("config") or {}
    with st.expander("Enable and settings", expanded=True):
        with st.form(f"slack_settings_{project_id}"):
            enabled = st.checkbox(
                "Enable Slack for this project",
                bool(config.get("enabled", False)),
            )
            workspace_id = st.text_input(
                "Workspace id",
                config.get("workspace_id") or "",
                help="Optional Slack workspace/team id.",
            )
            workspace_name = st.text_input(
                "Workspace name",
                config.get("workspace_name") or "",
                help="Optional Slack workspace display name.",
            )
            default_channel_id = st.text_input(
                "Default channel id",
                config.get("default_channel_id") or "",
                help="Optional channel id to restrict collection to one invited channel.",
            )
            save = st.form_submit_button("Save Slack settings")
        if save:
            _apply_command(
                service,
                {
                    "action": "upsert_slack_project_config",
                    "project_id": project_id,
                    "enabled": enabled,
                    "workspace_id": workspace_id or None,
                    "workspace_name": workspace_name or None,
                    "bot_token_secret_ref": config.get("bot_token_secret_ref"),
                    "signing_secret_ref": config.get("signing_secret_ref"),
                    "default_channel_id": default_channel_id or None,
                    "updated_at": controls["as_of"],
                },
            )


def _render_slack_token_section(
    service,
    controls: dict[str, Any],
    slack_data: dict[str, Any],
) -> None:
    project_id = controls["project_id"]
    token_present = _slack_has_encrypted_token(slack_data)
    with st.expander("Encrypted bot token", expanded=not token_present):
        st.write("Encrypted token stored." if token_present else "No encrypted token stored.")
        token_key = f"slack_raw_token_{project_id}"
        passphrase_key = f"slack_store_passphrase_{project_id}"
        _consume_session_clear(token_key)
        _consume_session_clear(passphrase_key)
        with st.form(f"slack_token_store_{project_id}"):
            raw_token = st.text_input(
                "Raw bot token",
                type="password",
                key=token_key,
                help="Slack xoxb token. It is encrypted before service storage.",
            )
            passphrase = st.text_input(
                "Token passphrase",
                type="password",
                key=passphrase_key,
                help="Passphrase used to encrypt the token. It is not stored.",
            )
            store = st.form_submit_button("Encrypt and store token")
        if not store:
            return
        if not raw_token or not passphrase:
            _set_slack_notice(project_id, "error", "Both raw token and passphrase are required.")
            _clear_session_keys(token_key, passphrase_key)
            st.rerun()
            return
        encrypted_token, error = _encrypt_slack_token_for_ui(raw_token, passphrase)
        if error:
            _set_slack_notice(project_id, "error", error)
            _clear_session_keys(token_key, passphrase_key)
            st.rerun()
            return
        result = _optional_service_command(
            service,
            {
                "action": "store_slack_bot_token",
                "project_id": project_id,
                **encrypted_token,
                "updated_at": controls["as_of"],
            },
        )
        if not _service_result_ok(result):
            result = _optional_service_command(
                service,
                {
                    "action": "store_encrypted_slack_bot_token",
                    "project_id": project_id,
                    "encrypted_token": encrypted_token,
                    "updated_at": controls["as_of"],
                },
            )
        if not _service_result_ok(result):
            _clear_session_keys(token_key, passphrase_key)
            _set_slack_notice(
                project_id,
                "error",
                "The encrypted token storage API is not available yet. "
                "Expected `store_slack_bot_token`.",
            )
            st.rerun()
            return
        _clear_session_keys(token_key, passphrase_key)
        _set_slack_notice(project_id, "success", "Encrypted Slack token stored.")
        st.rerun()


def _render_slack_mapping_section(
    service,
    controls: dict[str, Any],
    resources: list[dict[str, Any]],
    slack_data: dict[str, Any],
    action_passphrase: str,
) -> None:
    project_id = controls["project_id"]
    users_key = f"slack_users_{project_id}"
    with st.expander("Users and resource mapping", expanded=True):
        cols = st.columns(2)
        if cols[0].button(
            "Discover Slack users",
            key=f"slack_discover_users_{project_id}",
        ):
            token, error = _decrypt_slack_token_for_ui(
                service,
                project_id,
                slack_data,
                action_passphrase,
            )
            if error:
                _set_slack_notice(project_id, "error", error)
            else:
                users, user_error = _list_slack_users_for_ui(
                    service,
                    project_id,
                    token or "",
                )
                if user_error:
                    _set_slack_notice(project_id, "error", user_error)
                else:
                    st.session_state[users_key] = users
                    _set_slack_notice(
                        project_id,
                        "success",
                        f"Loaded {len(users)} Slack users.",
                    )
            st.rerun()
        if cols[1].button("Verify settings", key=f"slack_verify_{project_id}"):
            token, error = _decrypt_slack_token_for_ui(
                service,
                project_id,
                slack_data,
                action_passphrase,
            )
            if error:
                _set_slack_notice(project_id, "error", error)
            else:
                ok, message = _verify_slack_settings_for_ui(
                    service,
                    project_id,
                    token or "",
                )
                _set_slack_notice(project_id, "success" if ok else "error", message)
            st.rerun()

        users = st.session_state.get(users_key, [])
        if not users:
            st.info("Discover Slack users before editing mappings.")
            return
        if not resources:
            st.warning("Create ProjDash resources before mapping Slack users.")
            return

        rows = _slack_mapping_rows(
            slack_users=users,
            resources=resources,
            resource_mappings=slack_data.get("resource_mappings") or [],
        )
        edited_rows = st.data_editor(
            rows,
            key=f"slack_mapping_editor_{project_id}",
            use_container_width=True,
            hide_index=True,
            column_config={
                "mapped": st.column_config.CheckboxColumn("Mapped"),
                "slack_name": st.column_config.TextColumn("Slack user", disabled=True),
                "slack_user_id": st.column_config.TextColumn("Slack id", disabled=True),
                "resource_id": st.column_config.SelectboxColumn(
                    "Resource",
                    options=[""] + [str(resource["resource_id"]) for resource in resources],
                ),
            },
            disabled=["slack_name", "slack_user_id"],
        )
        if st.button("Save mappings", key=f"slack_save_mappings_{project_id}"):
            commands, error = _slack_mapping_commands(
                project_id=project_id,
                rows=edited_rows,
                current_mappings=slack_data.get("resource_mappings") or [],
                updated_at=controls["as_of"],
            )
            if error:
                st.error(error)
            elif not commands:
                st.info("No mapping changes to save.")
            else:
                _apply_batch(service, commands)


def _render_slack_run_section(
    service,
    controls: dict[str, Any],
    db_path: str,
    slack_data: dict[str, Any],
    action_passphrase: str,
) -> None:
    project_id = controls["project_id"]
    with st.expander("Run once", expanded=True):
        model_options = _codex_debug_model_options()
        if model_options:
            selected_model = st.selectbox(
                "Codex model",
                model_options,
                key=f"slack_codex_model_{project_id}",
            )
        else:
            selected_model = st.text_input(
                "Codex model",
                "",
                key=f"slack_codex_model_fallback_{project_id}",
                help=(
                    "`codex debug models` did not return a model list. Leave blank "
                    "to let the runner use its default."
                ),
            )
        job = _slack_active_service_run(service, project_id) or _slack_run_job(project_id)
        _render_slack_job_status(job, controls["timezone"])
        active = _slack_job_is_active(job)
        cols = st.columns(2)
        if cols[0].button(
            "Run once",
            key=f"slack_run_once_{project_id}",
            disabled=active,
        ):
            token, error = _decrypt_slack_token_for_ui(
                service,
                project_id,
                slack_data,
                action_passphrase,
            )
            if error:
                _set_slack_notice(project_id, "error", error)
            else:
                started = _start_slack_run_job(
                    service=service,
                    db_path=db_path,
                    project_id=project_id,
                    token=token or "",
                    model=selected_model or None,
                )
                if started:
                    _set_slack_notice(project_id, "success", "Started Slack run.")
                else:
                    _set_slack_notice(
                        project_id,
                        "error",
                        "A Slack run is already active or could not be started.",
                    )
            st.rerun()
        if cols[1].button("Refresh status", key=f"slack_refresh_run_{project_id}"):
            st.rerun()


def _render_slack_draft_section(
    service,
    controls: dict[str, Any],
    slack_data: dict[str, Any],
    resources: list[dict[str, Any]],
    action_passphrase: str,
) -> None:
    project_id = controls["project_id"]
    with st.expander("Draft messages", expanded=True):
        drafts = _slack_outbox_rows(service, project_id, ["draft"], limit=200)
        if not drafts:
            st.info("No draft Slack messages.")
            return
        rows = _slack_draft_rows(drafts, resources)
        edited_rows = st.data_editor(
            rows,
            key=f"slack_draft_editor_{project_id}",
            use_container_width=True,
            hide_index=True,
            column_config={
                "send": st.column_config.CheckboxColumn("Send"),
                "outbox_id": st.column_config.TextColumn("Outbox id", disabled=True),
                "resource": st.column_config.TextColumn("Resource", disabled=True),
                "slack_user_id": st.column_config.TextColumn("Slack id", disabled=True),
                "status": st.column_config.TextColumn("Status", disabled=True),
                "body": st.column_config.TextColumn("Message"),
            },
            disabled=["outbox_id", "resource", "slack_user_id", "status"],
        )
        st.json(_slack_draft_json(edited_rows))
        cols = st.columns(3)
        if cols[0].button("Save draft edits", key=f"slack_save_drafts_{project_id}"):
            ok = _save_slack_draft_edits(
                service,
                project_id,
                original_rows=drafts,
                edited_rows=edited_rows,
                edited_at=controls["as_of"],
            )
            if ok:
                st.success("Saved draft edits.")
                st.rerun()
        if cols[1].button("Send selected", key=f"slack_send_selected_{project_id}"):
            selected_rows = [row for row in edited_rows if row.get("send")]
            if not selected_rows:
                _set_slack_notice(project_id, "error", "Select at least one draft to send.")
                st.rerun()
                return
            token, error = _decrypt_slack_token_for_ui(
                service,
                project_id,
                slack_data,
                action_passphrase,
            )
            if error:
                _set_slack_notice(project_id, "error", error)
                st.rerun()
                return
            if not _save_slack_draft_edits(
                service,
                project_id,
                original_rows=drafts,
                edited_rows=selected_rows,
                edited_at=controls["as_of"],
                require_changed_success=True,
            ):
                st.rerun()
                return
            ok, message = _send_slack_rows_for_ui(
                service,
                project_id,
                token or "",
                selected_rows,
                controls["as_of"],
            )
            _set_slack_notice(project_id, "success" if ok else "error", message)
            st.rerun()
        if cols[2].button("Skip selected", key=f"slack_skip_selected_{project_id}"):
            selected_rows = [row for row in edited_rows if row.get("send")]
            if not selected_rows:
                st.error("Select at least one draft to skip.")
                return
            ok = _mark_slack_rows_skipped(service, project_id, selected_rows, controls["as_of"])
            if ok:
                st.success("Marked selected drafts skipped.")
                st.rerun()


def _render_slack_history_section(
    service,
    controls: dict[str, Any],
    resources: list[dict[str, Any]],
) -> None:
    with st.expander("History"):
        rows = _slack_outbox_rows(
            service,
            controls["project_id"],
            ["sent", "failed", "skipped"],
            limit=200,
        )
        if not rows:
            st.info("No sent, failed, or skipped Slack messages.")
            return
        st.dataframe(
            format_display_datetimes(_slack_draft_rows(rows, resources), controls["timezone"]),
            use_container_width=True,
            hide_index=True,
        )


def _render_schedule(service, controls: dict[str, Any], context: dict[str, Any]) -> None:
    _ensure_catalog(service, controls, context)
    _ensure_graph_context(service, controls, context)
    _ensure_resource_schedule(service, controls, context)
    _ensure_blockers(service, controls, context)
    graph = context.get("graph") or {}
    full_graph = context.get("full_graph") or graph
    schedule = context.get("resource_schedule") or {}
    resources = (context.get("catalog") or {}).get("resources", [])
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

    st.metric("Converged", str(schedule.get("converged", "-")))
    _render_gantt_chart(
        graph,
        controls_now=context.get("now"),
        terminal_symbols=terminal_symbols,
        timezone_name=controls["timezone"],
    )
    st.subheader("Resource priorities")
    resource_rows = resource_priority_rows(
        graph,
        schedule,
        context.get("now") or controls["now"],
        terminal_symbols=terminal_symbols,
    )
    resource_options = sorted(
        {row["resource_id"] for row in resource_rows if row.get("resource_id")},
    )
    resource_filter_key = "resource_priority_filter"
    st.session_state[resource_filter_key] = [
        resource_id
        for resource_id in st.session_state.get(resource_filter_key, [])
        if resource_id in resource_options
    ]
    selected_resource_ids = st.multiselect(
        "Resource priority filter",
        resource_options,
        key=resource_filter_key,
        help="Leave empty to show all resource priorities.",
    )
    enriched_resource_rows = _enrich_priority_rows(
        resource_rows,
        graph,
        context.get("blockers") or {},
    )
    _render_priority_expanders(
        service,
        controls,
        enriched_resource_rows,
        "resource_id",
        selected_resource_ids,
        id_label="Resource",
        resources=resources,
    )
    st.subheader("Role priorities")
    role_rows = role_priority_rows(
        graph,
        context.get("now") or controls["now"],
        terminal_symbols=terminal_symbols,
    )
    role_options = sorted({row["role_id"] for row in role_rows if row.get("role_id")})
    role_filter_key = "role_priority_filter"
    st.session_state[role_filter_key] = [
        role_id
        for role_id in st.session_state.get(role_filter_key, [])
        if role_id in role_options
    ]
    selected_role_ids = st.multiselect(
        "Role priority filter",
        role_options,
        key=role_filter_key,
        help="Leave empty to show all role priorities.",
    )
    enriched_role_rows = _enrich_priority_rows(
        role_rows,
        graph,
        context.get("blockers") or {},
    )
    _render_priority_expanders(
        service,
        controls,
        enriched_role_rows,
        "role_id",
        selected_role_ids,
        id_label="Role",
        resources=resources,
    )


def _render_priority_expanders(
    service,
    controls: dict[str, Any],
    rows: list[dict[str, Any]],
    id_field: str,
    selected_ids: list[str] | tuple[str, ...],
    *,
    id_label: str,
    resources: list[dict[str, Any]] | None = None,
) -> None:
    sections = _priority_expander_sections(
        rows,
        id_field,
        selected_ids,
        id_label=id_label,
    )
    if not sections:
        st.markdown("_No matching priorities._")
        return

    for section in sections:
        with st.expander(section["label"], expanded=False):
            for row in section["rows"]:
                with st.expander(_priority_process_header(row), expanded=False):
                    _render_priority_process_entry(
                        service,
                        controls,
                        row,
                        resources or [],
                    )


def _priority_markdown(
    rows: list[dict[str, Any]],
    id_field: str,
    selected_ids: list[str] | tuple[str, ...],
    *,
    id_label: str,
) -> str:
    sections = _priority_expander_sections(
        rows,
        id_field,
        selected_ids,
        id_label=id_label,
    )
    if not sections:
        return "_No matching priorities._"

    chunks = []
    for section in sections:
        chunks.append(f"### {section['label']}")
        for row in section["rows"]:
            chunks.append(f"#### {_priority_process_header(row)}")
            chunks.append(_priority_process_markdown(row, DEFAULT_TIMEZONE))
    return "\n\n".join(chunks)


def _priority_expander_sections(
    rows: list[dict[str, Any]],
    id_field: str,
    selected_ids: list[str] | tuple[str, ...],
    *,
    id_label: str,
) -> list[dict[str, Any]]:
    groups = _priority_groups(rows, id_field, selected_ids)
    sections = []
    for group_id, group_rows in groups.items():
        process_count = len(group_rows)
        suffix = "process" if process_count == 1 else "processes"
        sections.append(
            {
                "label": f"{id_label} `{group_id}` ({process_count} {suffix})",
                "rows": group_rows,
            }
        )
    return sections


def _priority_groups(
    rows: list[dict[str, Any]],
    id_field: str,
    selected_ids: list[str] | tuple[str, ...],
) -> dict[str, list[dict[str, Any]]]:
    visible_rows = _priority_rows(rows, id_field, selected_ids)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in visible_rows:
        group_id = str(row.get(id_field) or "unassigned")
        groups.setdefault(group_id, []).append(row)
    return groups


def _priority_rows(
    rows: list[dict[str, Any]],
    id_field: str,
    selected_ids: list[str] | tuple[str, ...],
) -> list[dict[str, Any]]:
    selected = {item for item in selected_ids if item}
    visible_rows = []
    for row in rows:
        if selected and row.get(id_field) not in selected:
            continue
        visible_rows.append(
            {
                "priority": row.get("priority"),
                "process_symbol": row.get("process_symbol"),
                "process_name": row.get("process_name"),
                "hours_until_ls": row.get("hours_until_ls"),
                "effort_hours": row.get("effort_hours"),
                "status": row.get("status"),
                "computed_status": row.get("computed_status"),
                "started_at": row.get("started_at"),
                "finished_at": row.get("finished_at"),
                "description": row.get("description"),
                "blockers": row.get("blockers") or [],
                "blocking_count": row.get("blocking_count") or 0,
                "role_ids": row.get("role_ids"),
                "dependencies": row.get("dependencies") or [],
                "duration_business_days": row.get("duration_business_days"),
                "earliest_start_at": row.get("earliest_start_at"),
                "start_at_earliest": row.get("start_at_earliest"),
                "delay_after_dependencies_business_days": row.get(
                    "delay_after_dependencies_business_days",
                ),
                "role_requirements": row.get("role_requirements") or [],
                "required_roles": row.get("required_roles") or {},
                "staked_resource_ids": row.get("staked_resource_ids") or [],
                "assumption_note": row.get("assumption_note"),
                id_field: row.get(id_field),
            }
        )
    return visible_rows


def _enrich_priority_rows(
    rows: list[dict[str, Any]],
    graph: dict[str, Any],
    blockers: dict[str, Any],
) -> list[dict[str, Any]]:
    nodes_by_symbol = {
        node.get("process_symbol"): node
        for node in graph.get("nodes", [])
        if node.get("process_symbol")
    }
    blockers_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for blocker in blockers.get("blockers", []):
        if blocker.get("is_resolved_as_of"):
            continue
        process_symbol = blocker.get("process_symbol") or blocker.get("process_id")
        if process_symbol:
            blockers_by_symbol.setdefault(process_symbol, []).append(blocker)

    enriched = []
    for row in rows:
        process_symbol = row.get("process_symbol")
        node = nodes_by_symbol.get(process_symbol, {})
        process_blockers = blockers_by_symbol.get(process_symbol, [])
        blocker_summary = node.get("blocker_summary") or {}
        blocking_count = (
            sum(1 for blocker in process_blockers if blocker.get("is_blocking_as_of"))
            or int(blocker_summary.get("blocking_count") or 0)
        )
        enriched.append(
            {
                **row,
                "status": node.get("status"),
                "computed_status": node.get("computed_status"),
                "started_at": node.get("started_at"),
                "finished_at": node.get("finished_at"),
                "description": node.get("description"),
                "duration_business_days": node.get("duration_business_days"),
                "dependencies": node.get("dependencies") or [],
                "earliest_start_at": node.get("earliest_start_at"),
                "start_at_earliest": node.get("start_at_earliest"),
                "delay_after_dependencies_business_days": node.get(
                    "delay_after_dependencies_business_days",
                ),
                "role_requirements": node.get("role_requirements") or [],
                "required_roles": node.get("required_roles") or {},
                "staked_resource_ids": node.get("staked_resource_ids") or [],
                "assumption_note": node.get("assumption_note"),
                "blockers": process_blockers,
                "blocking_count": blocking_count,
            }
        )
    return enriched


def _priority_process_header(row: dict[str, Any]) -> str:
    priority = row.get("priority") or "-"
    symbol = row.get("process_symbol") or "-"
    name = row.get("process_name") or ""
    return " | ".join(part for part in (priority, symbol, name) if part)


def _render_priority_process_entry(
    service,
    controls: dict[str, Any],
    row: dict[str, Any],
    resources: list[dict[str, Any]],
) -> None:
    st.markdown(_priority_process_markdown(row, controls["timezone"]))
    _render_block_status(row)
    _render_done_criteria(row)
    _render_staked_resources_control(service, controls, row, resources)
    _render_priority_status_controls(service, controls, row)


def _priority_process_markdown(row: dict[str, Any], timezone_name: str) -> str:
    details = [
        f"- Start window: {_format_start_window(row.get('hours_until_ls'))}",
        f"- Effort: {_format_priority_hours(row.get('effort_hours'), 'hour')}",
    ]
    status = row.get("computed_status") or row.get("status")
    if status:
        details.append(f"- Status: `{status}`")
    role_ids = row.get("role_ids")
    if role_ids:
        role_text = (
            role_ids
            if isinstance(role_ids, str)
            else ", ".join(f"`{role_id}`" for role_id in role_ids)
        )
        details.append(f"- Roles: {role_text}")
    if row.get("staked_resource_ids"):
        details.append(
            "- Staked resources: "
            + ", ".join(f"`{resource_id}`" for resource_id in row["staked_resource_ids"])
        )
    if row.get("started_at"):
        started_at = _markdown_datetime(row.get("started_at"), timezone_name)
        details.append(f"- Started: {started_at}")
    if row.get("finished_at"):
        finished_at = _markdown_datetime(row.get("finished_at"), timezone_name)
        details.append(f"- Done: {finished_at}")
    return "\n".join(details)


def _render_block_status(row: dict[str, Any]) -> None:
    blockers = row.get("blockers") or []
    blocking_count = int(row.get("blocking_count") or 0)
    blocked = blocking_count > 0
    color = "#dc2626" if blocked else "#16a34a"
    label = "Blocked" if blocked else "Unblocked"
    st.markdown(
        (
            f"<span style='display:inline-block;width:0.72rem;height:0.72rem;"
            f"border-radius:999px;background:{color};vertical-align:middle;"
            f"margin-right:0.35rem;'></span><strong>{label}</strong>"
        ),
        unsafe_allow_html=True,
    )
    if blockers:
        st.markdown("**Blockers**")
        for blocker in blockers:
            severity = blocker.get("severity", "blocking")
            summary = blocker.get("summary", "")
            details = blocker.get("details")
            line = f"- `{severity}` {summary}"
            if details:
                line += f" - {details}"
            st.markdown(line)


def _render_done_criteria(row: dict[str, Any]) -> None:
    st.markdown("**Done criteria**")
    description = row.get("description")
    st.write(description or "No done criteria recorded.")


def _render_staked_resources_control(
    service,
    controls: dict[str, Any],
    row: dict[str, Any],
    resources: list[dict[str, Any]],
) -> None:
    process_symbol = row.get("process_symbol")
    if not process_symbol or not resources:
        return
    resource_options = [
        str(resource["resource_id"])
        for resource in resources
        if resource.get("resource_id") and resource.get("active", True)
    ]
    current = [
        resource_id
        for resource_id in row.get("staked_resource_ids", [])
        if resource_id in resource_options
    ]
    row_scope = f"{row.get('role_id')}_{row.get('resource_id')}"
    key = f"schedule_staked_resources_{process_symbol}_{row.get('priority')}_{row_scope}"
    selected = st.multiselect(
        "Staked resources",
        resource_options,
        default=current,
        key=key,
        help=(
            "Pin this process to specific resources before resource scheduling "
            "fills role capacity."
        ),
    )
    if st.button(
        "Save staked resources",
        key=f"schedule_save_staked_{process_symbol}_{row.get('priority')}_{row_scope}",
    ):
        _apply_command(
            service,
            {
                "action": "upsert_process_revision",
                "project_id": controls["project_id"],
                "process_symbol": process_symbol,
                "name": row.get("process_name") or process_symbol,
                "description": row.get("description") or "",
                "effective_at": controls["as_of"],
                "duration_business_days": int(row.get("duration_business_days") or 0),
                "dependencies": list(row.get("dependencies") or []),
                "earliest_start_at": row.get("earliest_start_at"),
                "start_at_earliest": bool(row.get("start_at_earliest")),
                "delay_after_dependencies_business_days": int(
                    row.get("delay_after_dependencies_business_days") or 0,
                ),
                "required_roles": row.get("required_roles") or {},
                "role_requirements": row.get("role_requirements") or [],
                "staked_resource_ids": selected,
                "assumption_note": row.get("assumption_note"),
            },
        )


def _render_priority_status_controls(
    service,
    controls: dict[str, Any],
    row: dict[str, Any],
) -> None:
    process_symbol = row.get("process_symbol")
    if not process_symbol:
        return
    status = row.get("status")
    already_started = bool(row.get("started_at")) or status in {
        "in_progress",
        "paused",
        "done",
    }
    already_done = status == "done" or bool(row.get("finished_at"))
    col1, col2 = st.columns(2)
    started_clicked = col1.checkbox(
        "Started",
        value=already_started,
        disabled=already_done,
        key=(
            f"schedule_started_{process_symbol}_{row.get('priority')}_"
            f"{row.get('role_id')}_{row.get('resource_id')}"
        ),
        help=(
            "Mark this process started using the current datetime, or clear the "
            "start if it was marked accidentally."
        ),
    )
    done_clicked = col2.checkbox(
        "Done",
        value=already_done,
        key=(
            f"schedule_done_{process_symbol}_{row.get('priority')}_"
            f"{row.get('role_id')}_{row.get('resource_id')}"
        ),
        help=(
            "Mark this process done using the current datetime, or reopen it if "
            "it was marked accidentally."
        ),
    )
    if started_clicked and not already_started:
        edit_at = _current_ui_datetime(controls["timezone"])
        _apply_command(
            service,
            {
                "action": "set_process_status",
                "project_id": controls["project_id"],
                "process_symbol": process_symbol,
                "status": "in_progress",
                "edit_at": edit_at,
                "started_at": edit_at,
                "note": "Marked started from the schedule priority view.",
            },
        )
    if not started_clicked and already_started and not already_done:
        edit_at = _current_ui_datetime(controls["timezone"])
        _apply_command(
            service,
            {
                "action": "set_process_status",
                "project_id": controls["project_id"],
                "process_symbol": process_symbol,
                "status": "planned",
                "edit_at": edit_at,
                "note": "Cleared started state from the schedule priority view.",
            },
        )
    if done_clicked and not already_done:
        edit_at = _current_ui_datetime(controls["timezone"])
        _apply_command(
            service,
            {
                "action": "set_process_status",
                "project_id": controls["project_id"],
                "process_symbol": process_symbol,
                "status": "done",
                "edit_at": edit_at,
                "started_at": row.get("started_at") or edit_at,
                "finished_at": edit_at,
                "note": "Marked done from the schedule priority view.",
            },
        )
    if not done_clicked and already_done:
        edit_at = _current_ui_datetime(controls["timezone"])
        _apply_command(
            service,
            {
                "action": "set_process_status",
                "project_id": controls["project_id"],
                "process_symbol": process_symbol,
                "status": "in_progress",
                "edit_at": edit_at,
                "started_at": row.get("started_at") or edit_at,
                "note": "Reopened from the schedule priority view.",
            },
        )


def _current_ui_datetime(timezone_name: str) -> dt.datetime:
    timezone = ZoneInfo(validate_timezone(timezone_name))
    return dt.datetime.now(timezone).replace(microsecond=0)


def _format_start_window(value: Any) -> str:
    if value is None:
        return "-"
    hours = float(value)
    if abs(hours) < 0.0001:
        return "latest start is now"
    days = _format_decimal_days(abs(hours))
    if hours < 0:
        return f"overdue by {days}"
    return f"latest start in {days}"


def _format_time_until_ls(value: Any) -> str:
    return _format_start_window(value)


def _format_decimal_days(hours: float) -> str:
    days = hours / 24
    rounded = round(days, 2)
    display_value = int(rounded) if rounded.is_integer() else rounded
    suffix = "day" if abs(days - 1) < 0.0001 else "days"
    return f"{display_value} {suffix}"


def _format_priority_hours(value: Any, unit: str) -> str:
    if value is None:
        return "-"
    hours = float(value)
    display_value = int(hours) if hours.is_integer() else round(hours, 1)
    suffix = unit if abs(hours - 1) < 0.0001 else f"{unit}s"
    return f"{display_value} {suffix}"


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
        "resource_schedule_query": {
            "action": "query_resource_schedule",
            "project_id": controls["project_id"],
            "as_of": controls["as_of"],
            "now": context.get("now") or controls["now"],
            **scoped_query,
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
        st.info(f"No {title.lower()} data for the computed schedule span.")
        return
    step = times[1] - times[0] if len(times) > 1 else dt.timedelta(hours=1)
    time_edges = [*times, times[-1] + step]
    y_edges = list(range(len(labels) + 1))
    fig_height = max(2.5, len(labels) * 0.35)
    fig, ax = plt.subplots(figsize=(12, fig_height))
    ax.pcolormesh(
        mdates.date2num(time_edges),
        y_edges,
        matrix,
        cmap=_utilization_colormap(),
        vmin=0,
        vmax=1,
        shading="flat",
    )
    ax.set_yticks([index + 0.5 for index in range(len(labels))])
    ax.set_yticklabels(labels)
    _format_datetime_axis(ax.xaxis, timezone_name)
    ax.set_xlabel("Time")
    fig.tight_layout()
    st.pyplot(fig)


def _render_utilization_heatmaps(
    utilization: dict[str, Any],
    schedule: dict[str, Any],
    *,
    timezone_name: str,
) -> None:
    resource_panel = (
        "Resources",
        *resource_utilization_heatmap(utilization, schedule),
    )
    role_panel = (
        "Roles",
        *role_utilization_heatmap(utilization, schedule),
    )
    panels = [
        panel
        for panel in (resource_panel, role_panel)
        if panel[1] and panel[2] and panel[3]
    ]
    if not panels:
        st.info("No utilization data for the computed schedule span.")
        return

    height_ratios = [max(1.2, len(panel[1]) * 0.35) for panel in panels]
    fig_height = max(3.0, sum(height_ratios) + 1.0)
    fig, axes = plt.subplots(
        len(panels),
        1,
        figsize=(12, fig_height),
        sharex=True,
        gridspec_kw={"height_ratios": height_ratios},
    )
    axes_list = list(axes.flat) if hasattr(axes, "flat") else [axes]
    for ax, (title, labels, times, matrix) in zip(axes_list, panels, strict=False):
        step = times[1] - times[0] if len(times) > 1 else dt.timedelta(hours=1)
        time_edges = [*times, times[-1] + step]
        y_edges = list(range(len(labels) + 1))
        ax.pcolormesh(
            mdates.date2num(time_edges),
            y_edges,
            matrix,
            cmap=_utilization_colormap(),
            vmin=0,
            vmax=1,
            shading="flat",
        )
        ax.set_title(title, loc="left", fontsize=10)
        ax.set_yticks([index + 0.5 for index in range(len(labels))])
        ax.set_yticklabels(labels)
        ax.grid(axis="x", color="#e5e7eb", linewidth=0.8)

    span = schedule_time_span(schedule)
    if span is not None:
        axes_list[-1].set_xlim(mdates.date2num(span[0]), mdates.date2num(span[1]))
    _format_datetime_axis(axes_list[-1].xaxis, timezone_name)
    axes_list[-1].set_xlabel("Time")
    fig.autofmt_xdate(rotation=30, ha="right")
    st.pyplot(fig)


def _utilization_colormap():
    cmap = plt.get_cmap("jet").copy()
    cmap.set_bad("#f8fafc")
    return cmap


def _render_slippage(service, controls: dict[str, Any], context: dict[str, Any]) -> None:
    _ensure_catalog(service, controls, context)
    milestones = [
        milestone
        for milestone in (context.get("catalog") or {}).get("milestones", [])
        if milestone.get("active", True)
    ]
    milestone_by_id = {
        milestone["milestone_id"]: milestone
        for milestone in milestones
        if milestone.get("milestone_id")
    }
    st.selectbox(
        "Snapshot scope",
        ["", *milestone_by_id],
        key="slippage_milestone_id",
        format_func=lambda value: (
            "Current terminal selection"
            if not value
            else f"{milestone_by_id[value].get('name') or value} ({value})"
        ),
        help="Choose a milestone to view and commit milestone-specific slippage.",
    )
    selected_milestone = _selected_slippage_milestone(context)
    _ensure_schedule_snapshots(service, controls, context)
    terminal_symbols = context.get("terminal_symbols") or []
    snapshots = (context.get("schedule_snapshots") or {}).get("snapshots", [])
    st.subheader("Committed schedule snapshots")
    if selected_milestone is not None:
        st.caption(
            "Milestone scope: "
            f"{selected_milestone.get('name') or selected_milestone['milestone_id']} "
            f"({', '.join(terminal_symbols) or 'no processes'})"
        )
    elif terminal_symbols:
        st.caption(f"Terminal scope: {', '.join(terminal_symbols)}")
    else:
        st.caption("Project-wide terminal scope")
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
            _commit_project_state_payload(
                controls,
                terminal_symbols=terminal_symbols,
                milestone=selected_milestone,
                committed_at=committed_at,
                note=note or None,
            ),
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


def _render_costs(service, controls: dict[str, Any], context: dict[str, Any]) -> None:
    costs = _ensure_costs(service, controls, context)
    utilization = _ensure_utilization(service, controls, context)
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


def _render_history(service, controls: dict[str, Any], context: dict[str, Any]) -> None:
    blockers = _ensure_blockers(service, controls, context)
    st.subheader("Blocker history")
    st.dataframe(
        format_display_datetimes(blockers.get("blockers", []), controls["timezone"]),
        use_container_width=True,
        hide_index=True,
    )


def _render_topology(service, controls: dict[str, Any], context: dict[str, Any]) -> None:
    _ensure_catalog(service, controls, context)
    _ensure_graph_context(service, controls, context)
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


def _slack_manifest_payload(project_id: str, name: str) -> dict[str, Any]:
    scopes = [
        "channels:history",
        "channels:read",
        "chat:write",
        "groups:history",
        "groups:read",
        "im:history",
        "im:read",
        "im:write",
        "users:read",
    ]
    module = _slack_bot_module()
    if module is not None and hasattr(module, "REQUIRED_BOT_SCOPES"):
        scopes = list(module.REQUIRED_BOT_SCOPES)
    return {
        "display_information": {"name": name},
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {
                "display_name": name,
                "always_online": False,
            }
        },
        "oauth_config": {"scopes": {"bot": scopes}},
        "settings": {
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }


def _slack_has_encrypted_token(slack_data: dict[str, Any]) -> bool:
    config = slack_data.get("config") or {}
    return bool(
        slack_data.get("encrypted_token")
        or slack_data.get("encrypted_bot_token")
        or config.get("encrypted_token")
        or config.get("encrypted_bot_token")
        or config.get("bot_token_ciphertext")
        or config.get("token_ciphertext")
        or config.get("has_encrypted_bot_token")
        or slack_data.get("has_encrypted_bot_token")
    )


def _slack_encrypted_token_payload(slack_data: dict[str, Any]) -> dict[str, Any] | None:
    containers = [
        slack_data,
        slack_data.get("config") or {},
        slack_data.get("token") or {},
    ]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in (
            "encrypted_token",
            "encrypted_bot_token",
            "bot_token_encrypted",
            "bot_token_ciphertext",
            "token_ciphertext",
            "ciphertext",
        ):
            value = container.get(key)
            if isinstance(value, dict):
                return value
            if isinstance(value, str) and value:
                payload = dict(container)
                payload["ciphertext"] = value
                return payload
    return None


def _encrypt_slack_token_for_ui(
    raw_token: str,
    passphrase: str,
) -> tuple[dict[str, Any] | None, str | None]:
    helper = _find_slack_helper(
        "encrypt_slack_bot_token",
        "encrypt_slack_token",
        "encrypt_bot_token",
        "encrypt_token",
    )
    if helper is None:
        return (
            None,
            "Encrypted token helper is not available yet. Expected "
            "`projdash.service.slack_crypto.encrypt_slack_bot_token` or an "
            "equivalent Slack integration helper.",
        )
    try:
        result = _call_token_crypto_helper(
            helper,
            token=raw_token,
            passphrase=passphrase,
        )
    except Exception as exc:
        return None, f"Token encryption failed: {exc}"
    return _jsonable_mapping(result), None


def _decrypt_slack_token_for_ui(
    service,
    project_id: str,
    slack_data: dict[str, Any],
    passphrase: str,
) -> tuple[str | None, str | None]:
    if not passphrase:
        return None, "Passphrase is required."

    blob = _slack_encrypted_token_payload(slack_data)
    if blob is None and _slack_has_encrypted_token(slack_data):
        token_data = _optional_service_query(
            service,
            {"action": "query_slack_bot_token", "project_id": project_id},
        ) or {}
        blob = token_data.get("encrypted_token")
    if blob is not None:
        helper = _find_slack_helper(
            "decrypt_slack_bot_token",
            "decrypt_slack_token",
            "decrypt_bot_token",
            "decrypt_token",
        )
        if helper is None:
            return (
                None,
                "Encrypted token decrypt helper is not available yet. Expected "
                "`projdash.service.slack_crypto.decrypt_slack_bot_token` or an "
                "equivalent Slack integration helper.",
            )
        try:
            result = _call_token_crypto_helper(
                helper,
                encrypted_token=blob,
                passphrase=passphrase,
            )
        except Exception as exc:
            return None, f"Token decrypt failed: {exc}"
        token = _extract_token_string(result)
        if token:
            return token, None
        return None, "Token decrypt helper did not return a bot token."

    return (
        None,
        "No decryptable Slack token is available. Store an encrypted token first.",
    )


def _find_slack_helper(*names: str):
    for module_name in (
        "projdash.service.slack_crypto",
        "projdash.integrations.slack_crypto",
        "projdash.integrations.slack_bot",
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for name in names:
            helper = getattr(module, name, None)
            if callable(helper):
                return helper
    return None


def _call_token_crypto_helper(
    helper,
    *,
    token: str | None = None,
    encrypted_token: dict[str, Any] | None = None,
    passphrase: str,
) -> Any:
    try:
        parameters = inspect.signature(helper).parameters
    except (TypeError, ValueError):
        parameters = {}

    if not parameters:
        if encrypted_token is not None:
            return helper(encrypted_token, passphrase)
        return helper(token, passphrase)

    kwargs: dict[str, Any] = {"passphrase": passphrase}
    if token is not None:
        for key in ("raw_token", "token", "bot_token"):
            if key in parameters:
                kwargs[key] = token
                break
    if encrypted_token is not None:
        for key in ("encrypted_token", "token_blob", "blob", "payload"):
            if key in parameters:
                kwargs[key] = encrypted_token
                break
    return helper(**kwargs)


def _jsonable_mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {"ciphertext": value}
    return {"value": value}


def _extract_token_string(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        for key in ("token", "bot_token", "raw_token", "slack_bot_token"):
            token = value.get(key)
            if isinstance(token, str) and token:
                return token
    return None


def _list_slack_users_for_ui(
    service,
    project_id: str,
    token: str,
) -> tuple[list[dict[str, Any]], str | None]:
    if not token:
        return [], "Slack token is required."
    helper = _find_slack_helper("list_slack_users", "fetch_slack_users")
    if helper is not None:
        try:
            result = _call_with_supported_kwargs(
                helper,
                {
                    "db_path": "",
                    "project_id": project_id,
                    "service": service,
                    "token_override": token,
                    "token": token,
                },
            )
        except Exception as exc:
            return [], f"Slack user discovery failed: {exc}"
        return _normalize_slack_users(result), None

    try:
        client = _make_slack_client_for_ui(token)
        members: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {"limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            response = client.users_list(**kwargs)
            members.extend(response.get("members", []))
            cursor = (
                response.get("response_metadata", {}).get("next_cursor")
                if isinstance(response, dict)
                else None
            )
            if not cursor:
                break
    except Exception as exc:
        return [], f"Slack user discovery failed: {exc}"
    return _normalize_slack_users(members), None


def _normalize_slack_users(raw: Any) -> list[dict[str, Any]]:
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="json")
    if isinstance(raw, dict):
        raw = raw.get("members", raw.get("users", raw.get("rows", [])))
    rows = []
    for member in raw or []:
        if hasattr(member, "model_dump"):
            member = member.model_dump(mode="json")
        elif hasattr(member, "as_dict"):
            member = member.as_dict()
        elif not isinstance(member, dict):
            member = {
                "slack_user_id": getattr(member, "slack_user_id", None),
                "id": getattr(member, "id", None),
                "user_id": getattr(member, "user_id", None),
                "display_name": getattr(member, "display_name", None),
                "real_name": getattr(member, "real_name", None),
                "name": getattr(member, "name", None),
                "email": getattr(member, "email", None),
                "team_id": getattr(member, "team_id", None),
                "team": getattr(member, "team", None),
                "deleted": getattr(member, "deleted", False),
                "is_bot": getattr(member, "is_bot", False),
                "is_app_user": getattr(member, "is_app_user", False),
            }
        if not isinstance(member, dict):
            continue
        slack_user_id = member.get("id") or member.get("slack_user_id") or member.get("user_id")
        if (
            not slack_user_id
            or member.get("deleted")
            or member.get("is_bot")
            or member.get("is_app_user")
        ):
            continue
        profile = member.get("profile") or {}
        slack_name = (
            member.get("display_name")
            or member.get("real_name")
            or profile.get("display_name")
            or profile.get("real_name")
            or member.get("name")
            or slack_user_id
        )
        rows.append(
            {
                "slack_user_id": str(slack_user_id),
                "slack_name": str(slack_name),
                "email": profile.get("email") or member.get("email"),
                "team_id": member.get("team_id") or member.get("team"),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("slack_name", "")).casefold(),
            str(row.get("slack_user_id", "")),
        ),
    )


def _slack_mapping_rows(
    *,
    slack_users: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    resource_mappings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resource_ids = {str(resource.get("resource_id")) for resource in resources}
    mapping_by_user = {
        str(mapping.get("slack_user_id")): mapping
        for mapping in resource_mappings
        if mapping.get("active", True) and mapping.get("slack_user_id")
    }
    rows = []
    for user in slack_users:
        slack_user_id = str(user.get("slack_user_id") or "")
        mapping = mapping_by_user.get(slack_user_id) or {}
        resource_id = str(mapping.get("resource_id") or "")
        rows.append(
            {
                "mapped": bool(resource_id and resource_id in resource_ids),
                "slack_name": user.get("slack_name") or slack_user_id,
                "slack_user_id": slack_user_id,
                "resource_id": resource_id if resource_id in resource_ids else "",
            }
        )
    return rows


def _slack_mapping_commands(
    *,
    project_id: str,
    rows: list[dict[str, Any]],
    current_mappings: list[dict[str, Any]],
    updated_at: dt.datetime,
) -> tuple[list[dict[str, Any]], str | None]:
    desired_by_resource: dict[str, dict[str, Any]] = {}
    seen_slack_users: set[str] = set()
    for row in rows:
        if not row.get("mapped"):
            continue
        slack_user_id = str(row.get("slack_user_id") or "")
        resource_id = str(row.get("resource_id") or "")
        if not slack_user_id or not resource_id:
            return [], "Mapped rows must have both a Slack user and a resource."
        if slack_user_id in seen_slack_users:
            return [], f"Slack user `{slack_user_id}` is mapped more than once."
        if resource_id in desired_by_resource:
            return [], f"Resource `{resource_id}` is mapped more than once."
        seen_slack_users.add(slack_user_id)
        desired_by_resource[resource_id] = row

    current_by_resource = {
        str(mapping.get("resource_id")): mapping
        for mapping in current_mappings
        if mapping.get("active", True) and mapping.get("resource_id")
    }
    commands: list[dict[str, Any]] = []
    for resource_id, mapping in sorted(current_by_resource.items()):
        desired = desired_by_resource.get(resource_id)
        if desired and desired.get("slack_user_id") == mapping.get("slack_user_id"):
            continue
        commands.append(
            {
                "action": "set_resource_slack_user",
                "project_id": project_id,
                "resource_id": resource_id,
                "slack_user_id": None,
                "display_name": None,
                "active": False,
                "updated_at": updated_at,
            }
        )
    for resource_id, row in sorted(desired_by_resource.items()):
        current = current_by_resource.get(resource_id) or {}
        if (
            current.get("slack_user_id") == row.get("slack_user_id")
            and current.get("display_name") == row.get("slack_name")
        ):
            continue
        commands.append(
            {
                "action": "set_resource_slack_user",
                "project_id": project_id,
                "resource_id": resource_id,
                "slack_user_id": row.get("slack_user_id"),
                "display_name": row.get("slack_name"),
                "active": True,
                "updated_at": updated_at,
            }
        )
    return commands, None


@st.cache_data(show_spinner=False, ttl=300)
def _codex_debug_model_options() -> list[str]:
    try:
        completed = subprocess.run(
            ["codex", "debug", "models"],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return _parse_codex_debug_models(completed.stdout)


def _parse_codex_debug_models(output: str) -> list[str]:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        models: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key in ("slug", "id", "name", "model"):
                    item = value.get(key)
                    if isinstance(item, str) and _looks_like_model_id(item):
                        models.append(item)
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(parsed)
        return _dedupe(models)

    models = []
    for line in output.splitlines():
        cleaned = line.strip().strip("|").strip()
        if not cleaned or set(cleaned) <= {"-", " ", "|"}:
            continue
        for token in re.split(r"[\s|,]+", cleaned):
            token = token.strip("`'\"*")
            if _looks_like_model_id(token):
                models.append(token)
                break
    return _dedupe(models)


def _looks_like_model_id(value: str) -> bool:
    return bool(
        re.match(
            r"^(?:gpt|o\d|codex|openai/|anthropic/|claude|gemini)[A-Za-z0-9_.:/-]*$",
            value,
            re.IGNORECASE,
        )
    )


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _slack_run_job_key(project_id: str) -> str:
    return f"slack-run:{project_id}"


def _slack_run_job(project_id: str) -> dict[str, Any] | None:
    with _SLACK_RUN_JOBS_LOCK:
        job = _SLACK_RUN_JOBS.get(_slack_run_job_key(project_id))
        return dict(job) if job else None


def _slack_active_service_run(service, project_id: str) -> dict[str, Any] | None:
    data = _optional_service_query(
        service,
        {
            "action": "query_slack_runs",
            "project_id": project_id,
            "statuses": ["queued", "running"],
            "limit": 1,
        },
    )
    rows = data.get("runs", []) if isinstance(data, dict) else []
    return dict(rows[0]) if rows else None


def _slack_job_is_active(job: dict[str, Any] | None) -> bool:
    return bool(job and job.get("status") in {"queued", "running"})


def _render_slack_job_status(
    job: dict[str, Any] | None,
    timezone_name: str,
) -> None:
    if not job:
        st.info("No Slack run has been started in this app process.")
        return
    status = str(job.get("status", "unknown"))
    started_at = format_display_datetime(job.get("started_at"), timezone_name)
    finished_at = (
        format_display_datetime(job.get("finished_at"), timezone_name)
        if job.get("finished_at")
        else "-"
    )
    st.write(
        {
            "status": status,
            "run_id": job.get("run_id"),
            "started_at": started_at,
            "finished_at": finished_at,
            "message": job.get("message"),
        }
    )


def _start_slack_run_job(
    *,
    service,
    db_path: str,
    project_id: str,
    token: str,
    model: str | None,
) -> bool:
    key = _slack_run_job_key(project_id)
    now = dt.datetime.now(dt.UTC)
    run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    with _SLACK_RUN_JOBS_LOCK:
        if _slack_job_is_active(_SLACK_RUN_JOBS.get(key)):
            return False
        service_result = _optional_service_command(
            service,
            {
                "action": "start_slack_run",
                "project_id": project_id,
                "run_id": run_id,
                "trigger": "ui",
                "codex_model": model,
                "started_at": now,
            },
        )
        if service_result is not None and not _service_result_ok(service_result):
            return False
        _SLACK_RUN_JOBS[key] = {
            "status": "queued",
            "run_id": run_id,
            "project_id": project_id,
            "model": model,
            "started_at": now.isoformat(),
            "message": "Queued.",
        }
    thread = threading.Thread(
        target=_slack_run_worker,
        kwargs={
            "key": key,
            "service": service,
            "db_path": db_path,
            "project_id": project_id,
            "token": token,
            "model": model,
            "run_id": run_id,
        },
        daemon=True,
    )
    thread.start()
    return True


def _slack_run_worker(
    *,
    key: str,
    service,
    db_path: str,
    project_id: str,
    token: str,
    model: str | None,
    run_id: str,
) -> None:
    _set_slack_run_job(key, status="running", message="Collecting Slack data.")
    try:
        result = _run_slack_once_for_ui(
            service=service,
            db_path=db_path,
            project_id=project_id,
            token=token,
            model=model,
            run_id=run_id,
        )
        result_data = getattr(result, "data", None) or {}
        status = "succeeded" if getattr(result, "exit_code", 1) == 0 else "failed"
        if status == "succeeded" and result_data.get("skipped_codex"):
            status = "no_new_data"
        message = getattr(result, "message", None) or status
        _set_slack_run_job(
            key,
            status=status,
            message=message,
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
        )
        _finish_slack_run(
            service,
            project_id,
            run_id,
            status,
            model,
            message,
            result_data,
        )
    except Exception as exc:
        message = str(exc)
        _set_slack_run_job(
            key,
            status="failed",
            message=message,
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
        )
        _finish_slack_run(service, project_id, run_id, "failed", model, message)
    finally:
        token = ""


def _set_slack_run_job(key: str, **updates: Any) -> None:
    with _SLACK_RUN_JOBS_LOCK:
        job = dict(_SLACK_RUN_JOBS.get(key, {}))
        job.update(updates)
        _SLACK_RUN_JOBS[key] = job


def _finish_slack_run(
    service,
    project_id: str,
    run_id: str,
    status: str,
    model: str | None,
    message: str | None = None,
    result_data: dict[str, Any] | None = None,
) -> None:
    result_data = result_data or {}
    rows = _slack_outbox_rows(
        service,
        project_id,
        ["draft", "sent", "failed", "skipped"],
        limit=500,
    )
    draft_ids = [
        str(row["outbox_id"])
        for row in rows
        if row.get("outbox_id") and row.get("run_id") == run_id
    ]
    _optional_service_command(
        service,
        {
            "action": "finish_slack_run",
            "project_id": project_id,
            "run_id": run_id,
            "status": status,
            "finished_at": dt.datetime.now(dt.UTC),
            "collected_message_count": int(result_data.get("message_count") or 0),
            "draft_outbox_ids": draft_ids,
            "result_json": {
                "codex_model": model,
                "message": message,
                "runner": result_data,
            },
            "error_text": message if status == "failed" else None,
        },
    )


def _run_slack_once_for_ui(
    *,
    service,
    db_path: str,
    project_id: str,
    token: str,
    model: str | None,
    run_id: str,
) -> Any:
    module = _slack_bot_module()
    if module is None or not hasattr(module, "run_once"):
        raise RuntimeError("Slack run_once integration helper is not available.")
    run_once = module.run_once
    service = _LockedProjectService(service)
    kwargs: dict[str, Any] = {
        "db_path": db_path,
        "project_id": project_id,
        "service": service,
        "dry_run_send": True,
        "prepare_only": True,
        "now": dt.datetime.now(dt.UTC),
        "run_id": run_id,
    }
    signature = inspect.signature(run_once)
    if "codex_model" in signature.parameters and model:
        kwargs["codex_model"] = model
    elif "model" in signature.parameters and model:
        kwargs["model"] = model
    if "token_override" in signature.parameters:
        kwargs["token_override"] = token
        return _call_with_supported_kwargs(run_once, kwargs)

    raise RuntimeError("Slack run_once helper must support token_override.")


def _slack_outbox_rows(
    service,
    project_id: str,
    statuses: list[str],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    data = _optional_service_query(
        service,
        {
            "action": "query_pending_slack_outbox",
            "project_id": project_id,
            "statuses": statuses,
            "limit": limit,
        },
    )
    if isinstance(data, dict):
        return list(data.get("outbox", data.get("messages", data.get("rows", []))))
    if isinstance(data, list):
        return list(data)
    return []


def _slack_draft_rows(
    rows: list[dict[str, Any]],
    resources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resource_labels = {
        str(resource.get("resource_id")): (
            f"{resource.get('name') or resource.get('resource_id')} "
            f"({resource.get('resource_id')})"
        )
        for resource in resources
    }
    return [
        {
            "send": False,
            "outbox_id": row.get("outbox_id"),
            "status": row.get("status"),
            "resource": resource_labels.get(
                str(row.get("resource_id")),
                row.get("resource_id") or "",
            ),
            "resource_id": row.get("resource_id"),
            "slack_user_id": row.get("slack_user_id"),
            "body": row.get("body") or row.get("text") or "",
            "run_id": row.get("run_id"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "sent_at": row.get("sent_at"),
            "failed_at": row.get("failed_at"),
            "error_text": row.get("error_text"),
            "slack_channel_id": row.get("slack_channel_id"),
            "slack_message_ts": row.get("slack_message_ts"),
        }
        for row in rows
    ]


def _slack_draft_json(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "outbox_id": row.get("outbox_id"),
            "resource_id": row.get("resource_id"),
            "slack_user_id": row.get("slack_user_id"),
            "body": row.get("body"),
            "send": bool(row.get("send")),
        }
        for row in rows
    ]


def _save_slack_draft_edits(
    service,
    project_id: str,
    *,
    original_rows: list[dict[str, Any]],
    edited_rows: list[dict[str, Any]],
    edited_at: dt.datetime,
    require_changed_success: bool = False,
) -> bool:
    original_by_id = {
        str(row.get("outbox_id")): row.get("body") or row.get("text") or ""
        for row in original_rows
        if row.get("outbox_id")
    }
    changed = [
        row
        for row in edited_rows
        if row.get("outbox_id")
        and str(row.get("body") or "") != str(original_by_id.get(str(row["outbox_id"]), ""))
    ]
    if not changed:
        return True
    for row in changed:
        result = _optional_service_command(
            service,
            {
                "action": "update_slack_outbox_body",
                "project_id": project_id,
                "outbox_id": row.get("outbox_id"),
                "body": row.get("body") or "",
                "updated_at": edited_at,
            },
        )
        if not _service_result_ok(result):
            _set_slack_notice(
                project_id,
                "error",
                "The draft edit API is not available yet. Expected "
                "`update_slack_outbox_body`.",
            )
            return False
    return True


def _send_slack_rows_for_ui(
    service,
    project_id: str,
    token: str,
    rows: list[dict[str, Any]],
    sent_at: dt.datetime,
) -> tuple[bool, str]:
    if not token:
        return False, "Slack token is required."
    module = _slack_bot_module()
    if module is None:
        return False, "Slack integration module is not available."

    helper = (
        getattr(module, "send_outbox_messages", None)
        or getattr(module, "send_slack_outbox_messages", None)
    )
    if callable(helper):
        try:
            result = _call_with_supported_kwargs(
                helper,
                {
                    "db_path": "",
                    "service": service,
                    "project_id": project_id,
                    "token_override": token,
                    "outbox_ids": [
                        row.get("outbox_id") for row in rows if row.get("outbox_id")
                    ],
                    "rows": rows,
                    "now": sent_at,
                    "sent_at": sent_at,
                },
            )
        except Exception as exc:
            return False, f"Slack send failed: {exc}"
        message = getattr(result, "message", None) or "Selected Slack messages sent."
        return getattr(result, "exit_code", 0) == 0, message

    try:
        gateway = module.ServiceGateway(service)
        config = module._normalize_config(  # noqa: SLF001 - integration fallback.
            gateway.query_slack_project_config(project_id),
            project_id,
        )
        if config is None:
            return False, "Slack project config is not available."
        client = _make_slack_client_for_ui(token)
        pending = [
            {
                "outbox_id": row.get("outbox_id"),
                "slack_user_id": row.get("slack_user_id"),
                "slack_channel_id": row.get("slack_channel_id"),
                "body": row.get("body"),
            }
            for row in rows
        ]
        module._send_pending_outbox(  # noqa: SLF001 - integration fallback.
            client=client,
            gateway=gateway,
            project_id=project_id,
            pending=pending,
            now=sent_at,
            dry_run_send=False,
            config=config,
        )
    except Exception as exc:
        return False, f"Slack send failed: {exc}"
    return True, "Selected Slack messages sent."


def _mark_slack_rows_skipped(
    service,
    project_id: str,
    rows: list[dict[str, Any]],
    skipped_at: dt.datetime,
) -> bool:
    for row in rows:
        result = _optional_service_command(
            service,
            {
                "action": "mark_slack_outbox_skipped",
                "project_id": project_id,
                "outbox_id": row.get("outbox_id"),
                "skipped_at": skipped_at,
            },
        )
        if not _service_result_ok(result):
            st.error(
                "The skip API is not available yet. Expected "
                "`mark_slack_outbox_skipped`."
            )
            return False
    return True


def _verify_slack_settings_for_ui(
    service,
    project_id: str,
    token: str,
) -> tuple[bool, str]:
    if not token:
        return False, "Slack token is required."
    module = _slack_bot_module()
    if module is None:
        return False, "Slack integration module is not available."
    verify = getattr(module, "verify", None)
    if callable(verify) and "token_override" in inspect.signature(verify).parameters:
        try:
            result = _call_with_supported_kwargs(
                verify,
                {
                    "db_path": "",
                    "project_id": project_id,
                    "service": service,
                    "token_override": token,
                },
            )
        except Exception as exc:
            return False, f"Slack verification failed: {exc}"
        message = getattr(result, "message", None) or "Slack integration verified."
        return getattr(result, "exit_code", 1) == 0, message

    try:
        client = _make_slack_client_for_ui(token)
        client.auth_test()
        client.conversations_list(
            types="public_channel,private_channel,im",
            exclude_archived=True,
            limit=1,
        )
        slack_data = _optional_service_query(
            service,
            {"action": "query_slack_project_config", "project_id": project_id},
        ) or {}
        for mapping in slack_data.get("resource_mappings", []):
            if mapping.get("active", True) and mapping.get("slack_user_id"):
                client.users_info(user=mapping["slack_user_id"])
    except Exception as exc:
        return False, f"Slack verification failed: {exc}"
    return True, "Slack integration verified."


def _make_slack_client_for_ui(token: str) -> Any:
    module = _slack_bot_module()
    if module is not None:
        for name in ("make_slack_client", "_make_slack_client"):
            helper = getattr(module, name, None)
            if callable(helper):
                return helper(token)
    from slack_sdk import WebClient

    return WebClient(token=token)


def _slack_bot_module():
    try:
        return importlib.import_module("projdash.integrations.slack_bot")
    except ImportError:
        return None


def _call_with_supported_kwargs(function, kwargs: dict[str, Any]) -> Any:
    parameters = inspect.signature(function).parameters
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return function(**kwargs)
    filtered = {key: value for key, value in kwargs.items() if key in parameters}
    return function(**filtered)


def _optional_service_query(service, payload: dict[str, Any]) -> Any:
    try:
        with _SERVICE_ACCESS_LOCK:
            result = service.handle_query(query_payload_envelope(payload))
    except (ValidationError, ValueError):
        return None
    if not getattr(result, "ok", False):
        return None
    return getattr(result, "data", None)


def _optional_service_command(service, payload: dict[str, Any]) -> Any:
    try:
        with _SERVICE_ACCESS_LOCK:
            return service.handle_command(command_payload_envelope(payload))
    except (ValidationError, ValueError):
        return None


def _service_result_ok(result: Any) -> bool:
    return bool(getattr(result, "ok", False))


def _service_result_data(result: Any) -> Any:
    if result is None:
        return None
    if hasattr(result, "data"):
        return result.data
    if isinstance(result, dict):
        return result.get("data", result)
    return None


def _clear_session_keys(*keys: str) -> None:
    for key in keys:
        st.session_state[f"{key}__clear_next"] = True
        try:
            st.session_state[key] = ""
        except Exception:
            st.session_state[f"{key}__clear_next"] = True


def _consume_session_clear(key: str) -> None:
    if st.session_state.pop(f"{key}__clear_next", False):
        st.session_state[key] = ""


def _query(
    service,
    payload: dict[str, Any],
    *,
    key: str | None = None,
    render: bool = True,
) -> Any:
    try:
        with _SERVICE_ACCESS_LOCK:
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
        with _SERVICE_ACCESS_LOCK:
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
        with _SERVICE_ACCESS_LOCK:
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
