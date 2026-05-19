import datetime as dt
import json
from types import SimpleNamespace

from projdash.ui.app import (
    _batch_role_requirements_by_symbol,
    _blocker_sections,
    _capacity_buckets_for_display,
    _commit_project_state_payload,
    _context_terminal_symbols,
    _decrypt_slack_token_for_ui,
    _dependency_set_operations,
    _encrypt_slack_token_for_ui,
    _ensure_resource_schedule,
    _load_context,
    _normalize_slack_users,
    _parse_codex_debug_models,
    _prepare_context_for_section,
    _priority_expander_sections,
    _priority_markdown,
    _process_revision_defaults_signature,
    _project_context_markdown,
    _resource_calendar_rules_markdown,
    _schedule_debug_payload,
    _schedule_snapshot_query_payload,
    _slack_action_passphrase_keys_to_clear,
    _slack_manifest_payload,
    _slack_mapping_commands,
    _slack_mapping_rows,
)


def test_dependency_set_operations_preserve_internal_selected_edges():
    graph = {
        "nodes": [
            {"process_symbol": "A"},
            {"process_symbol": "B"},
            {"process_symbol": "C"},
        ],
        "edges": [
            {
                "predecessor_process_symbol": "A",
                "successor_process_symbol": "B",
            },
            {
                "predecessor_process_symbol": "C",
                "successor_process_symbol": "B",
            },
        ],
    }

    operations = _dependency_set_operations(
        graph,
        ["A", "B"],
        ["C"],
        side="predecessors",
    )

    assert operations == [
        {
            "action": "add_dependency",
            "operation_id": "add-C-A",
            "predecessor_process_symbol": "C",
            "successor_process_symbol": "A",
        }
    ]


def test_batch_role_requirements_distribute_aggregate_effort_without_multiplying():
    graph = {
        "nodes": [
            {
                "process_symbol": "A",
                "role_requirements": [
                    {"role_id": "role_eng", "effort_hours": 2},
                ],
            },
            {
                "process_symbol": "B",
                "role_requirements": [
                    {"role_id": "role_eng", "effort_hours": 3},
                ],
            },
        ]
    }

    by_symbol = _batch_role_requirements_by_symbol(
        graph,
        ["A", "B"],
        [{"role_id": "role_eng", "effort_hours": 10}],
    )

    assert by_symbol == {
        "A": [{"role_id": "role_eng", "effort_hours": 4}],
        "B": [{"role_id": "role_eng", "effort_hours": 6}],
    }

    uneven = _batch_role_requirements_by_symbol(
        graph,
        ["A", "B"],
        [{"role_id": "role_eng", "effort_hours": 11}],
    )

    assert uneven == {
        "A": [{"role_id": "role_eng", "effort_hours": 4}],
        "B": [{"role_id": "role_eng", "effort_hours": 7}],
    }


def test_process_revision_defaults_signature_ignores_volatile_as_of_time():
    aggregate = {
        "process_symbols": ["A", "B"],
        "predecessors": ["P"],
        "children": ["C"],
        "role_efforts": {"role_eng": 5.0},
        "status": "planned",
        "name": "",
        "description": "",
        "earliest_start_at": None,
        "started_at": None,
        "finished_at": None,
        "blocker_ids": [],
    }
    first = _process_revision_defaults_signature(
        aggregate,
        {
            "timezone": "UTC",
            "as_of": dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC),
        },
    )
    second = _process_revision_defaults_signature(
        aggregate,
        {
            "timezone": "UTC",
            "as_of": dt.datetime(2026, 5, 13, 10, tzinfo=dt.UTC),
        },
    )

    assert first == second


def test_capacity_buckets_for_display_uses_utilization_allocations():
    rows = _capacity_buckets_for_display(
        [
            {
                "resource_id": "res-a",
                "starts_at": "2026-05-13T09:00:00+00:00",
                "ends_at": "2026-05-13T10:00:00+00:00",
                "capacity_hours": 1,
                "allocated_hours": 0,
                "remaining_hours": 1,
            }
        ],
        {
            "time_series": [
                {
                    "resource_id": "res-a",
                    "starts_at": "2026-05-13T09:00:00+00:00",
                    "ends_at": "2026-05-13T10:00:00+00:00",
                    "allocated_hours": 0.75,
                }
            ]
        },
    )

    assert rows[0]["allocated_hours"] == 0.75
    assert rows[0]["remaining_hours"] == 0.25


def test_priority_markdown_filters_and_formats_priority_fields():
    markdown = _priority_markdown(
        [
            {
                "priority": "P2",
                "priority_rank": 2,
                "process_symbol": "A",
                "process_name": "Design",
                "hours_until_ls": 1,
                "hours_until_lf": 3,
                "effort_hours": 2,
                "role_id": "role_eng",
            },
            {
                "priority": "P3",
                "priority_rank": 3,
                "process_symbol": "B",
                "process_name": "Build",
                "hours_until_ls": 12,
                "hours_until_lf": 16,
                "effort_hours": 4,
                "role_id": "role_qa",
            },
        ],
        "role_id",
        ["role_qa"],
        id_label="Role",
    )

    assert "### Role `role_qa`" in markdown
    assert "#### P3 | B | Build" in markdown
    assert "Start window: latest start in 0.5 days" in markdown
    assert "Effort: 4 hours" in markdown
    assert "role_eng" not in markdown
    assert "`A`" not in markdown


def test_priority_expander_sections_group_selected_entities():
    sections = _priority_expander_sections(
        [
            {
                "priority": "P1",
                "process_symbol": "A",
                "process_name": "Design",
                "hours_until_ls": -1,
                "effort_hours": 2,
                "resource_id": "res_ada",
            },
            {
                "priority": "P2",
                "process_symbol": "B",
                "process_name": "Build",
                "hours_until_ls": 4,
                "effort_hours": 6,
                "resource_id": "res_grace",
            },
        ],
        "resource_id",
        ["res_ada"],
        id_label="Resource",
    )

    assert len(sections) == 1
    assert sections[0]["label"] == "Resource `res_ada` (1 process)"
    assert len(sections[0]["rows"]) == 1
    assert sections[0]["rows"][0]["priority"] == "P1"
    assert sections[0]["rows"][0]["process_symbol"] == "A"
    assert sections[0]["rows"][0]["hours_until_ls"] == -1


def test_blocker_sections_split_unresolved_and_resolved_rows():
    sections = _blocker_sections(
        [
            {"blocker_id": "blocker-open", "is_resolved_as_of": False},
            {"blocker_id": "blocker-closed", "is_resolved_as_of": True},
        ]
    )

    assert [row["blocker_id"] for row in sections["unresolved"]] == ["blocker-open"]
    assert [row["blocker_id"] for row in sections["resolved"]] == ["blocker-closed"]


def test_resource_calendar_rules_markdown_lists_default_and_overrides():
    markdown = _resource_calendar_rules_markdown(
        {
            "resources": [
                {
                    "resource_id": "res_josh",
                    "name": "Josh",
                    "calendar_id": "cal_default",
                    "available_from_at": "2026-05-14T09:00:00+00:00",
                    "available_until_at": None,
                    "calendar_overrides": [
                        {
                            "rule_id": "august",
                            "calendar_id": "cal_august",
                            "starts_at": "2026-08-01T00:00:00+00:00",
                            "ends_at": "2026-09-01T00:00:00+00:00",
                            "reason": "August availability.",
                        }
                    ],
                }
            ],
            "calendars": [
                {
                    "calendar_id": "cal_default",
                    "name": "Default",
                    "timezone": "Europe/Amsterdam",
                },
                {
                    "calendar_id": "cal_august",
                    "name": "August",
                    "timezone": "Europe/Amsterdam",
                },
            ],
        },
        "UTC",
    )

    assert "### Josh (`res_josh`)" in markdown
    assert "- Default: **Default** (`cal_default`)" in markdown
    assert "- Override `august`: **August** (`cal_august`)" in markdown
    assert "August availability." in markdown


def test_slack_manifest_payload_uses_slack_schema_fields_and_scopes():
    payload = _slack_manifest_payload("project-a", "Project A Bot")

    assert payload["display_information"]["name"] == "Project A Bot"
    assert payload["features"]["app_home"]["messages_tab_enabled"] is True
    assert (
        payload["features"]["app_home"]["messages_tab_read_only_enabled"]
        is False
    )
    assert payload["features"]["bot_user"]["display_name"] == "Project A Bot"
    assert "_metadata" not in payload
    assert "chat:write" in payload["oauth_config"]["scopes"]["bot"]
    assert "users:read" in payload["oauth_config"]["scopes"]["bot"]


def test_parse_codex_debug_models_handles_json_and_text_output():
    json_output = json.dumps(
        {
            "models": [
                {"slug": "gpt-5.5", "display_name": "GPT-5.5"},
                {"id": "gpt-5-codex", "label": "GPT-5 Codex"},
                {"name": "o4-mini"},
                {"id": "gpt-5-codex"},
            ]
        }
    )
    text_output = """
    MODEL                 DESCRIPTION
    gpt-5-codex           default coding model
    | o4-mini | compact |
    """

    assert _parse_codex_debug_models(json_output) == [
        "gpt-5.5",
        "gpt-5-codex",
        "o4-mini",
    ]
    assert _parse_codex_debug_models(text_output) == ["gpt-5-codex", "o4-mini"]


def test_slack_action_passphrase_keys_clear_only_when_leaving_slack_section():
    keys = [
        "slack_action_passphrase_project-a",
        "slack_store_passphrase_project-a",
        "slack_action_passphrase_project-b",
        "other",
    ]

    assert _slack_action_passphrase_keys_to_clear("Slack", "Resources", keys) == [
        "slack_action_passphrase_project-a",
        "slack_action_passphrase_project-b",
    ]
    assert _slack_action_passphrase_keys_to_clear("Slack", "Slack", keys) == []
    assert _slack_action_passphrase_keys_to_clear("Dashboard", "Resources", keys) == []


def test_slack_token_crypto_helpers_round_trip_with_service_helper():
    encrypted, encrypt_error = _encrypt_slack_token_for_ui(
        "xoxb-test-token",
        "correct horse battery staple",
    )

    assert encrypt_error is None
    assert encrypted is not None
    assert encrypted["ciphertext"] != "xoxb-test-token"

    token, decrypt_error = _decrypt_slack_token_for_ui(
        None,
        "project-a",
        {"encrypted_token": encrypted},
        "correct horse battery staple",
    )

    assert decrypt_error is None
    assert token == "xoxb-test-token"


def test_slack_token_decrypt_does_not_fall_back_to_env_var(monkeypatch):
    monkeypatch.setenv("PROJDASH_SLACK_TEST_TOKEN", "xoxb-env-token")

    token, decrypt_error = _decrypt_slack_token_for_ui(
        None,
        "project-a",
        {"config": {"bot_token_secret_ref": "PROJDASH_SLACK_TEST_TOKEN"}},
        "irrelevant-passphrase",
    )

    assert token is None
    assert decrypt_error == (
        "No decryptable Slack token is available. Store an encrypted token first."
    )


def test_slack_mapping_rows_and_commands_clear_and_set_mappings():
    rows = _slack_mapping_rows(
        slack_users=[
            {"slack_user_id": "U1", "slack_name": "Ada"},
            {"slack_user_id": "U2", "slack_name": "Grace"},
        ],
        resources=[
            {"resource_id": "res_ada", "name": "Ada"},
            {"resource_id": "res_grace", "name": "Grace"},
        ],
        resource_mappings=[
            {
                "resource_id": "res_ada",
                "slack_user_id": "U1",
                "display_name": "Ada",
                "active": True,
            }
        ],
    )

    assert rows == [
        {
            "mapped": True,
            "slack_name": "Ada",
            "slack_user_id": "U1",
            "resource_id": "res_ada",
        },
        {
            "mapped": False,
            "slack_name": "Grace",
            "slack_user_id": "U2",
            "resource_id": "",
        },
    ]

    edited = [
        {**rows[0], "mapped": False, "resource_id": ""},
        {**rows[1], "mapped": True, "resource_id": "res_grace"},
    ]
    commands, error = _slack_mapping_commands(
        project_id="project-a",
        rows=edited,
        current_mappings=[
            {
                "resource_id": "res_ada",
                "slack_user_id": "U1",
                "display_name": "Ada",
                "active": True,
            }
        ],
        updated_at=dt.datetime(2026, 5, 19, 12, tzinfo=dt.UTC),
    )

    assert error is None
    assert commands == [
        {
            "action": "set_resource_slack_user",
            "project_id": "project-a",
            "resource_id": "res_ada",
            "slack_user_id": None,
            "display_name": None,
            "active": False,
            "updated_at": dt.datetime(2026, 5, 19, 12, tzinfo=dt.UTC),
        },
        {
            "action": "set_resource_slack_user",
            "project_id": "project-a",
            "resource_id": "res_grace",
            "slack_user_id": "U2",
            "display_name": "Grace",
            "active": True,
            "updated_at": dt.datetime(2026, 5, 19, 12, tzinfo=dt.UTC),
        },
    ]


def test_normalize_slack_users_accepts_integration_user_objects():
    class SlackUserLike:
        def __init__(
            self,
            slack_user_id: str,
            display_name: str,
            *,
            is_app_user: bool = False,
        ) -> None:
            self.slack_user_id = slack_user_id
            self.display_name = display_name
            self.real_name = None
            self.name = None
            self.email = None
            self.timezone = "UTC"
            self.deleted = False
            self.is_bot = False
            self.is_app_user = is_app_user

    rows = _normalize_slack_users(
        [
            SlackUserLike("U1", "Ada"),
            SlackUserLike("UAPP", "ProjDash", is_app_user=True),
        ]
    )

    assert rows == [
        {
            "slack_user_id": "U1",
            "slack_name": "Ada",
            "email": None,
            "team_id": None,
        }
    ]


def test_project_context_markdown_summarizes_schedule_and_risks():
    markdown = _project_context_markdown(
        {
            "timezone": "UTC",
            "as_of": dt.datetime(2026, 5, 15, 9, tzinfo=dt.UTC),
        },
        {
            "agent_context": {
                "project": {
                    "name": "Accelerating Astrophysics",
                    "project_id": "accelerating_astro",
                },
                "summary": {
                    "projected_completion_at": "2026-11-04T10:00:00+00:00",
                    "total_role_effort_hours": 286,
                    "critical_path": ["run-workshop", "write-paper"],
                    "process_count": 17,
                    "edge_count": 27,
                    "status_counts": {"planned": 12, "done": 5},
                    "blocked_process_count": 1,
                    "converged": True,
                },
                "slippage": {"completion_change_hours": 12},
                "schedule": {
                    "processes": [
                        {
                            "symbol": "write-paper",
                            "name": "Write paper",
                            "status": "planned",
                            "computed_status": "late_risk",
                            "ls_at": "2026-05-16T09:00:00+00:00",
                            "ends_at": "2026-05-20T17:00:00+00:00",
                            "slack_hours": 0,
                            "critical": True,
                            "allocation_state": "allocated",
                        }
                    ]
                },
                "prioritized_work": {
                    "by_role": [
                        {
                            "role_id": "role_write",
                            "role_name": "Writing",
                            "processes": [
                                {
                                    "priority": "P1",
                                    "process_symbol": "write-paper",
                                    "process_name": "Write paper",
                                    "hours_until_ls": -2,
                                    "effort_hours": 12,
                                    "computed_status": "late_risk",
                                }
                            ],
                        }
                    ],
                    "by_resource": [
                        {
                            "resource_id": "res_ada",
                            "resource_name": "Ada",
                            "processes": [
                                {
                                    "priority": "P2",
                                    "process_symbol": "write-paper",
                                    "process_name": "Write paper",
                                    "hours_until_ls": 4,
                                    "effort_hours": 6,
                                    "role_ids": ["role_write"],
                                    "computed_status": "work_now",
                                }
                            ],
                        }
                    ],
                },
                "blockers": [
                    {
                        "severity": "warning",
                        "process_symbol": "poster-inputs",
                        "summary": "Poster inputs needed",
                    }
                ],
            },
            "catalog": {
                "resources": [
                    {
                        "resource_id": "res_ada",
                        "name": "Ada",
                        "calendar_id": "cal_default",
                        "available_from_at": "2026-05-13T09:00:00+00:00",
                        "available_until_at": None,
                        "calendar_overrides": [],
                    }
                ],
                "calendars": [
                    {
                        "calendar_id": "cal_default",
                        "name": "Default",
                        "timezone": "UTC",
                    }
                ],
            },
        },
    )

    assert "# Accelerating Astrophysics" in markdown
    assert "- Projected completion: 2026-11-04 10:00 UTC" in markdown
    assert "- Completion change: 12 hours" in markdown
    assert "- Status counts: done: 5, planned: 12" in markdown
    assert "- `run-workshop`" in markdown
    assert "## Role Priorities" in markdown
    assert "- **Writing** (`role_write`)" in markdown
    assert (
        "**P1** `write-paper` - Write paper; start window: overdue by 0.08 days; "
        "effort: 12 hours; status: late_risk"
    ) in markdown
    assert "## Resource Priorities" in markdown
    assert "- **Ada** (`res_ada`)" in markdown
    assert "roles: `role_write`" in markdown
    assert "## Schedule Watchlist" in markdown
    assert (
        "- **critical** `write-paper` - Write paper; status: late_risk; "
        "LS: 2026-05-16 09:00 UTC; ends: 2026-05-20 17:00 UTC; "
        "slack: 0 hours; allocation: allocated"
    ) in markdown
    assert "- [warning] `poster-inputs`: Poster inputs needed" in markdown
    assert "## Resource Calendar Rules" in markdown
    assert "### Ada (`res_ada`)" in markdown


def test_schedule_debug_payload_contains_query_and_schedule_context():
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    payload = _schedule_debug_payload(
        {
            "project_id": "project-alpha",
            "timezone": "UTC",
            "as_of": as_of,
            "now": as_of,
        },
        {
            "scope": {"type": "project"},
            "now": as_of,
            "project": {"project": {"project_id": "project-alpha"}},
            "catalog": {"roles": [], "resources": [], "calendars": []},
            "graph": {"nodes": []},
            "full_graph": {"nodes": []},
            "blockers": {"blockers": []},
            "resource_schedule": {"processes": []},
            "capacity": {"buckets": []},
            "utilization": {"by_resource": [], "by_role": []},
            "costs": {"total_cost": "0"},
        },
        ["A"],
    )

    assert payload["debug_schema"] == 1
    assert payload["terminal_process_symbols"] == ["A"]
    assert payload["resource_schedule_query"]["action"] == "query_resource_schedule"
    assert "horizon_starts_at" not in payload["resource_schedule_query"]
    assert "horizon_ends_at" not in payload["resource_schedule_query"]
    json.dumps(payload, default=str)


class _RecordingQueryService:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def handle_query(self, envelope):
        payload = envelope.query.model_dump(mode="json")
        action = payload["action"]
        self.actions.append(action)
        return SimpleNamespace(ok=True, warnings=[], data=self._data(action, payload))

    def _data(self, action: str, payload: dict) -> dict:
        if action == "get_project":
            return {
                "project": {
                    "project_id": payload["project_id"],
                    "name": "Alpha",
                    "start_at": "2026-05-13T09:00:00+00:00",
                    "default_currency": "USD",
                }
            }
        if action == "query_project_catalog":
            return {
                "project_id": payload["project_id"],
                "roles": [],
                "resources": [],
                "calendars": [],
                "milestones": [
                    {
                        "milestone_id": "milestone-alpha",
                        "name": "Alpha",
                        "process_symbols": ["A"],
                        "active": True,
                    }
                ],
            }
        if action == "query_process_graph":
            return {
                "project_id": payload["project_id"],
                "nodes": [
                    {
                        "process_id": "proc-a",
                        "process_symbol": "A",
                        "role_requirements": [],
                    }
                ],
                "edges": [],
                "allocation_slices": [],
            }
        if action == "query_resource_schedule":
            return {
                "project_id": payload["project_id"],
                "processes": [],
                "allocation_slices": [],
            }
        if action == "query_blockers":
            return {
                "project_id": payload["project_id"],
                "blockers": [],
                "blocked_process_ids": [],
            }
        if action == "query_agent_context":
            return {
                "project_id": payload["project_id"],
                "project": {"project_id": payload["project_id"], "name": "Alpha"},
                "summary": {},
                "schedule": {},
                "slippage": {},
                "prioritized_work": {},
                "blockers": [],
            }
        if action == "query_utilization":
            return {
                "project_id": payload["project_id"],
                "by_resource": [],
                "by_role": [],
                "time_series": [],
            }
        if action == "query_costs":
            return {
                "project_id": payload["project_id"],
                "total_cost": "0",
                "currency": "USD",
                "by_resource": [],
                "by_process": [],
                "by_role": [],
                "time_series": [],
            }
        if action == "query_schedule_snapshots":
            return {
                "project_id": payload["project_id"],
                "snapshots": [],
            }
        raise AssertionError(f"Unexpected query action: {action}")


def test_load_context_omits_expensive_tab_queries():
    service = _RecordingQueryService()
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)

    context = _load_context(
        service,
        {
            "project_id": "project-alpha",
            "as_of": as_of,
            "now": as_of,
        },
    )

    assert service.actions == ["get_project"]
    assert context["project"]["project"]["project_id"] == "project-alpha"
    assert context["catalog"] is None
    assert context["graph"] is None
    assert context["resource_schedule"] is None
    assert context["utilization"] is None
    assert context["costs"] is None
    assert context["agent_context"] is None


def test_lazy_resource_schedule_query_runs_once_per_context():
    service = _RecordingQueryService()
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    controls = {
        "project_id": "project-alpha",
        "as_of": as_of,
        "now": as_of,
    }
    context = _load_context(service, controls)
    service.actions.clear()

    first = _ensure_resource_schedule(service, controls, context)
    second = _ensure_resource_schedule(service, controls, context)

    assert first == second
    assert service.actions == ["query_resource_schedule"]


def test_context_terminal_symbols_ignore_unvalidated_session_state(monkeypatch):
    import projdash.ui.app as app

    monkeypatch.setitem(app.st.session_state, "terminal_process_symbols", ["OLD"])

    assert _context_terminal_symbols({}) == []


def test_slippage_snapshot_query_can_target_selected_milestone(monkeypatch):
    import projdash.ui.app as app

    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    controls = {
        "project_id": "project-alpha",
        "as_of": as_of,
    }
    context = {
        "catalog": {
            "milestones": [
                {
                    "milestone_id": "milestone-alpha",
                    "name": "Alpha",
                    "process_symbols": ["A"],
                    "active": True,
                }
            ]
        },
        "terminal_symbols": ["Z"],
    }
    monkeypatch.setitem(
        app.st.session_state,
        "slippage_milestone_id",
        "milestone-alpha",
    )

    query = _schedule_snapshot_query_payload(controls, context)
    commit_payload = _commit_project_state_payload(
        controls,
        terminal_symbols=context["terminal_symbols"],
        milestone=context["catalog"]["milestones"][0],
        committed_at=as_of,
        note="Milestone baseline",
    )

    assert query == {
        "action": "query_schedule_snapshots",
        "project_id": "project-alpha",
        "as_of": as_of,
        "milestone_id": "milestone-alpha",
    }
    assert context["terminal_symbols"] == ["A"]
    assert commit_payload["milestone_id"] == "milestone-alpha"
    assert "terminal_process_symbols" not in commit_payload


def test_schedule_section_loads_schedule_data_without_cost_or_agent_queries():
    service = _RecordingQueryService()
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    controls = {
        "project_id": "project-alpha",
        "as_of": as_of,
        "now": as_of,
    }
    context = _load_context(service, controls)
    service.actions.clear()

    _prepare_context_for_section(service, controls, context, "Schedule")

    assert service.actions == [
        "query_project_catalog",
        "query_process_graph",
        "query_resource_schedule",
        "query_blockers",
    ]
    assert "query_costs" not in service.actions
    assert "query_utilization" not in service.actions
    assert "query_agent_context" not in service.actions


def test_context_section_loads_agent_context_without_extra_graph_or_cost_queries():
    service = _RecordingQueryService()
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    controls = {
        "project_id": "project-alpha",
        "as_of": as_of,
        "now": as_of,
    }
    context = _load_context(service, controls)
    service.actions.clear()

    _prepare_context_for_section(service, controls, context, "Context")

    assert service.actions == ["query_project_catalog", "query_agent_context"]
    assert context["graph"] is None
    assert context["resource_schedule"] is None
    assert context["costs"] is None
    assert context["utilization"] is None


def test_costs_section_loads_costs_without_graph_schedule_or_agent_queries():
    service = _RecordingQueryService()
    as_of = dt.datetime(2026, 5, 13, 9, tzinfo=dt.UTC)
    controls = {
        "project_id": "project-alpha",
        "as_of": as_of,
        "now": as_of,
    }
    context = _load_context(service, controls)
    service.actions.clear()

    _prepare_context_for_section(service, controls, context, "Costs")

    assert service.actions == ["query_costs", "query_utilization"]
    assert context["graph"] is None
    assert context["resource_schedule"] is None
    assert context["agent_context"] is None
