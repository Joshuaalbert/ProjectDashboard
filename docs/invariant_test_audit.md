# Invariant Test Audit

Audit date: 2026-05-31

This audit maps every invariant in `docs/invariants.md` to at least one proving
`test_*` function. The complete top-level test-function inventory generated from
the Python AST is in `docs/test_function_inventory.md`.

Verification after the audit/fix cycle:

- `conda run -n projdash_py pytest` -> 586 passed
- `conda run -n projdash_py ruff check .` -> passed

## Fixes Added During Audit

- `tests/test_service_api.py::test_service_api_datetime_fields_are_aware_and_use_moment_names`
- `tests/test_service_mutations.py::test_successful_batch_commits_with_one_repository_replacement`
- `tests/test_service_mutations.py::test_failed_batch_commits_with_zero_repository_replacements`
- `tests/test_service_mutations.py::test_add_blocker_requires_process_reference`
- `tests/test_service_mutations.py::test_reusing_blocker_id_for_two_processes_is_rejected_without_write`
- `tests/test_service_mutations.py::test_process_without_role_requirements_gets_default_josh_process_role`
- `tests/test_service_mutations.py::test_orphan_blocker_resolver_process_is_rejected_at_commit`
- `tests/test_service_mutations.py::test_retiring_process_deletes_childless_blocker_and_resolver_process`
- `tests/test_service_mutations.py::test_completedness_allowed_states_are_exact`
- `tests/test_service_mutations.py::test_completedness_ready_and_pin_time_derivations`
- `tests/test_resource_api.py::test_blocker_update_does_not_refresh_other_process_evidence_attributes`
- `tests/test_resource_api.py::test_pm_evidence_freshness_targets_follow_process_priority`
- `tests/test_resource_api.py::test_unpinned_same_role_resource_can_plan_from_now_while_pin_reserves_resource`
- `tests/test_resource_api.py::test_child_planned_start_uses_max_of_parent_verified_finish_and_now`
- `tests/test_resource_schedule.py::test_process_role_dependencies_inherit_process_edges_without_same_process_edges`
- `tests/test_slack_service.py::test_slack_outbox_message_targets_are_exact_dm_or_channel_forms`
- `tests/test_sqlite_repository.py::test_sqlite_rows_exclude_computed_fields_and_snapshots_are_explicit`
- `tests/test_sqlite_repository.py::test_sqlite_repository_repairs_processes_missing_role_requirements`
- `tests/test_service_mutations.py::test_process_with_multiple_role_requirements_is_rejected_without_write`
- `tests/test_sqlite_repository.py::test_sqlite_repository_repairs_multi_role_process_to_best_exact_resource_role`
- `tests/test_sqlite_repository.py::test_sqlite_repository_deletes_legacy_childless_blocker_processes`
- `tests/test_sqlite_repository.py::test_local_state_databases_and_secrets_are_gitignored`
- `tests/test_ui_app_logic.py::test_gantt_pin_marker_plot_uses_one_o_per_start_and_one_x_per_finish`
- `tests/test_ui_app_logic.py::test_blockers_section_removed_from_main_navigation`

The audit also removed one untestable invariant about removing a blocker
reference independently of the blocker/process, because no public command
supports that operation. PM evidence prioritization wording was narrowed to the
observable service contract: stale evidence line items are marked for priority.

## Coverage Matrix

### API and Time

- Service authority for validation, persistence, graph rewrites, schedule
  projection, resource allocation, utilization, cost, slippage, and Slack state:
  `tests/test_service_api.py::test_service_creates_project_and_process_revision`;
  `tests/test_resource_queries.py::test_resource_schedule_capacity_and_utilization_contracts`;
  `tests/test_resource_queries.py::test_cost_queries_cover_filters_grouping_and_totals`;
  `tests/test_service_mutations.py::test_commit_project_state_records_slippage_points_by_terminal_scope`;
  `tests/test_slack_service.py::test_slack_state_round_trips_through_sqlite`.
- Typed models and validated JSON envelopes:
  `tests/test_service_api.py::test_command_envelope_json_round_trip`;
  `tests/test_service_api.py::test_resource_command_envelopes_json_round_trip`;
  `tests/test_service_api.py::test_new_query_payloads_round_trip`;
  `tests/test_slack_integration.py::test_run_once_works_against_service_command_and_query_envelopes`.
- Timezone-aware API/persistence moments:
  `tests/test_service_api.py::test_naive_datetime_payload_reports_validation_error`;
  `tests/test_service_api.py::test_resource_command_moments_must_be_timezone_aware`;
  `tests/test_service_api.py::test_new_command_moments_reject_naive_datetimes`;
  `tests/test_service_api.py::test_new_query_moments_reject_naive_datetimes`;
  `tests/test_sqlite_repository.py::test_sqlite_repository_round_trips_process_role_pins`.
- Moment fields use `*_at`, `as_of`, or `now` names:
  `tests/test_service_api.py::test_service_api_datetime_fields_are_aware_and_use_moment_names`.
- Commands mutate facts and queries compute projections:
  `tests/test_resource_api.py::test_started_projection_is_derived_only_from_process_role_pins`;
  `tests/test_resource_api.py::test_process_role_pin_drives_start_progress_and_done_time`.
- Batch commands stage in memory and commit through repository replacement:
  `tests/test_service_mutations.py::test_successful_batch_commits_with_one_repository_replacement`;
  `tests/test_service_mutations.py::test_failed_batch_commits_with_zero_repository_replacements`;
  `tests/test_service_mutations.py::test_batch_reference_validation_precedes_cycle_validation_and_is_atomic`.

### Blockers

- Blocker process reference is required:
  `tests/test_service_mutations.py::test_add_blocker_requires_process_reference`.
- One blocker id cannot reference multiple processes:
  `tests/test_service_mutations.py::test_reusing_blocker_id_for_two_processes_is_rejected_without_write`.
- Blockers are resolver processes named `resolve-...` with `process_type="blocker"`:
  `tests/test_resource_queries.py::test_blockers_add_resolver_dependency_without_special_resource_exclusion`.
- Unresolved and resolved blockers keep mandatory resolver dependencies:
  `tests/test_service_api.py::test_unresolved_blocker_derives_resolver_parent_until_resolved`;
  `tests/test_service_mutations.py::test_active_blocker_resolver_dependency_cannot_be_removed`;
  `tests/test_service_mutations.py::test_resolved_blocker_resolver_dependency_remains_tied_to_blocker_reference`.
- Adding a blocker to a started process makes it `early_start`:
  `tests/test_service_mutations.py::test_started_process_becomes_early_start_when_blocker_parent_is_added`.
- Blocker metadata is not a planning primitive, and blockers affect planning
  only through resolver processes/dependencies:
  `tests/test_resource_schedule.py::test_mcts_backend_treats_blocker_metadata_as_non_planning_data`;
  `tests/test_resource_schedule.py::test_blockers_do_not_change_resource_schedule_timing`;
  `tests/test_resource_schedule.py::test_blocker_process_type_schedules_like_an_ordinary_process`.
- Blockers appear in summaries/resolvers for review:
  `tests/test_resource_queries.py::test_process_graph_dependency_only_contract_includes_cpm_status_and_blockers`.
- Deleting a blocked process deletes its tied blocker/resolver:
  `tests/test_service_mutations.py::test_delete_blocked_process_deletes_orphan_blocker_and_resolver_process`;
  `tests/test_service_mutations.py::test_retiring_process_deletes_childless_blocker_and_resolver_process`;
  `tests/test_sqlite_repository.py::test_sqlite_repository_deletes_legacy_childless_blocker_processes`;
  `tests/test_sqlite_repository.py::test_sqlite_repository_persists_delete_process_graph_cleanup`.

### Completedness

- Derived states are exactly `waiting`, `early_start`, `ready`, `started`,
  `due`, and `finished`:
  `tests/test_service_mutations.py::test_completedness_allowed_states_are_exact`.
- Lifecycle state/start/finish are projections, never persisted process facts:
  `tests/test_resource_api.py::test_started_projection_is_derived_only_from_process_role_pins`;
  `tests/test_sqlite_repository.py::test_sqlite_rows_exclude_computed_fields_and_snapshots_are_explicit`.
- No overdue/late/blocker completedness state:
  `tests/test_resource_queries.py::test_completedness_uses_ready_state_with_timezone_aware_as_of`;
  `tests/test_service_api.py::test_lifecycle_due_blocker_and_alias_commands_reject_invalid_inputs`.
- `waiting`, `early_start`, `ready`, `started`, `due`, and `finished`
  derivation rules from process pins and parent completion:
  `tests/test_schedule.py::test_unstarted_blocked_by_dependencies_does_not_mark_process_done`;
  `tests/test_resource_api.py::test_pre_ready_pin_derives_early_start_until_parent_is_finished`;
  `tests/test_service_mutations.py::test_completedness_ready_and_pin_time_derivations`;
  `tests/test_resource_api.py::test_process_role_pin_drives_start_progress_and_done_time`;
  `tests/test_resource_api.py::test_process_role_pin_forecast_controls_finish_and_resource_capacity`;
  `tests/test_resource_api.py::test_overdue_pin_uses_now_as_planning_lower_bound`.
- Verified finished implies pinned started and requires verified finish:
  `tests/test_resource_api.py::test_process_role_pin_invariants_require_start_and_normalize_final_forecast`.
- Derived start and finish are the sole process pin's pinned start and verified finish:
  `tests/test_service_mutations.py::test_completedness_ready_and_pin_time_derivations`.
- Non-started and non-finished states expose null start/finish respectively:
  `tests/test_resource_api.py::test_deleting_finished_pin_removes_forecast_verified_work_and_done_state`;
  `tests/test_resource_api.py::test_pre_ready_pin_derives_early_start_until_parent_is_finished`.
- A process cannot derive finished while any parent is unfinished:
  `tests/test_resource_api.py::test_process_role_cannot_be_verified_done_before_parent_processes_finish`;
  `tests/test_service_mutations.py::test_unresolved_blocking_blocker_parent_prevents_done_until_resolved`.
- Every process revision has exactly one role requirement; new multi-role
  process revisions are rejected, legacy missing-role and multi-role SQLite rows
  are repaired to one role, invalid historical revisions are rejected before
  persistence, and the SQLite schema rejects direct revision rows with zero or
  multiple role requirements:
  `tests/test_service_mutations.py::test_process_without_role_requirements_gets_default_josh_process_role`;
  `tests/test_service_mutations.py::test_process_with_multiple_role_requirements_is_rejected_without_write`;
  `tests/test_sqlite_repository.py::test_sqlite_repository_repairs_processes_missing_role_requirements`;
  `tests/test_sqlite_repository.py::test_sqlite_repository_repairs_multi_role_process_to_best_exact_resource_role`;
  `tests/test_sqlite_repository.py::test_sqlite_repository_repairs_every_revision_to_exactly_one_role`;
  `tests/test_sqlite_repository.py::test_sqlite_repository_rejects_persisting_historical_multi_role_revision`;
  `tests/test_sqlite_repository.py::test_sqlite_schema_rejects_revision_rows_without_exactly_one_role`.
- Role requirement effort is positive:
  `tests/test_service_api.py::test_role_requirement_validation_rejects_invalid_values`;
  `tests/test_resource_schedule.py::test_nonpositive_role_effort_fails_validation`.

### Dependency Graph

- Dependency graph is a DAG:
  `tests/test_schedule.py::test_dependency_cycles_are_rejected`;
  `tests/test_service_mutations.py::test_backdated_revision_rejects_future_effective_dependency_cycle`;
  `tests/test_service_mutations.py::test_batch_dependency_cycle_error_shape_and_atomicity`.
- Dependencies point predecessor to successor:
  `tests/test_schedule.py::test_schedule_projection_computes_critical_path_datetimes`;
  `tests/test_resource_queries.py::test_process_graph_dependency_only_contract_includes_cpm_status_and_blockers`.
- Dependency-only CPM is diagnostic, not slippage authority:
  `tests/test_resource_api.py::test_agent_context_query_returns_concise_project_management_json`;
  `tests/test_service_mutations.py::test_commit_project_state_extends_horizon_to_sparse_resource_capacity`.
- Terminal-symbol scope is induced ancestor subgraph:
  `tests/test_resource_api.py::test_agent_context_terminal_scope_filters_blockers_and_accepts_aliases`;
  `tests/test_ui_adapters.py::test_gantt_rows_use_terminal_ancestor_scope`.

### PM Evidence

- Slack PM runs start from service-prepared context:
  `tests/test_slack_integration.py::test_run_once_collects_messages_invokes_codex_persists_and_sends`;
  `tests/test_slack_integration.py::test_run_once_invokes_codex_with_continuity_when_no_new_evidence`.
- PM agents must answer prepared evidence items:
  `tests/test_slack_integration.py::test_codex_output_requires_evidence_line_yes_no_answer`;
  `tests/test_slack_integration.py::test_codex_output_must_cover_service_prepared_evidence_lines`.
- Evidence freshness is line-item-specific and blocker updates do not refresh
  unrelated evidence:
  `tests/test_resource_api.py::test_pm_markdown_context_includes_process_evidence_line_items`;
  `tests/test_resource_api.py::test_blocker_update_does_not_refresh_other_process_evidence_attributes`.
- PM `pin_data` evidence and stale evidence are service-marked for prioritization:
  `tests/test_resource_api.py::test_pm_markdown_context_includes_process_evidence_line_items`;
  `tests/test_resource_api.py::test_pm_markdown_context_includes_resource_pin_evidence_targets`.
- P0/P1/P2/P3 targets are 1/3/7/14 days:
  `tests/test_resource_api.py::test_pm_evidence_freshness_targets_follow_process_priority`.
- PM markdown context shape and evidence row format:
  `tests/test_resource_api.py::test_pm_markdown_context_follows_specified_project_context_shape`.

### Process Pins

- Process pins are the source of pinned/started state:
  `tests/test_resource_api.py::test_started_projection_is_derived_only_from_process_role_pins`;
  `tests/test_service_mutations.py::test_started_status_requires_started_process_role_pin`.
- A process cannot be pinned without resource, pinned start time, and forecast:
  `tests/test_resource_api.py::test_process_role_pin_invariants_require_start_and_normalize_final_forecast`;
  `tests/test_resource_api.py::test_process_role_pin_forecast_controls_finish_and_resource_capacity`.
- Verified finished requires verification and normalizes forecast to verified finish:
  `tests/test_resource_api.py::test_process_role_pin_invariants_require_start_and_normalize_final_forecast`;
  `tests/test_resource_api.py::test_process_role_pin_drives_start_progress_and_done_time`.
- Deleting a pin removes started/resource/forecast/verified state:
  `tests/test_resource_api.py::test_deleting_finished_pin_removes_forecast_verified_work_and_done_state`.
- Head-start pins may predate dependency readiness while not verified finished:
  `tests/test_resource_api.py::test_pre_ready_pin_derives_early_start_until_parent_is_finished`.
- A process cannot become verified finished while parents are unfinished:
  `tests/test_resource_api.py::test_process_role_cannot_be_verified_done_before_parent_processes_finish`.
- Process finish is parent-gated even after head-start forecast:
  `tests/test_resource_api.py::test_pre_ready_pin_derives_early_start_until_parent_is_finished`.
- Pinned resources are removed from planning capacity until forecast; unpinned
  same-role resources remain plannable from `now` subject to calendar:
  `tests/test_resource_api.py::test_process_role_pin_forecast_controls_finish_and_resource_capacity`;
  `tests/test_resource_api.py::test_process_role_pin_reserves_resource_capacity_until_forecast_finish`;
  `tests/test_resource_api.py::test_unpinned_same_role_resource_can_plan_from_now_while_pin_reserves_resource`.

### Resources and Scheduling

- Planning happens directly on atomic one-role processes:
  `tests/test_service_mutations.py::test_process_with_multiple_role_requirements_is_rejected_without_write`;
  `tests/test_sqlite_repository.py::test_sqlite_repository_repairs_multi_role_process_to_best_exact_resource_role`;
  `tests/test_resource_schedule.py::test_resource_schedule_returns_collapsed_process_rows_only`.
- Human-level deliverables with multiple roles are represented as multiple
  one-role processes or an explicit single replacement process:
  `tests/test_service_mutations.py::test_process_with_multiple_role_requirements_is_rejected_without_write`;
  `tests/test_service_mutations.py::test_collapse_subgraph_preserves_total_effort_hours_by_role_when_omitted`.
- Process graph rows are the scheduling surface:
  `tests/test_resource_api.py::test_process_graph_query_returns_lifecycle_windows_and_process_only_edges`;
  `tests/test_resource_schedule.py::test_resource_schedule_returns_collapsed_process_rows_only`.
- Resource-aware scheduling is driven by process effort, compatible resources,
  resource calendars, pins, and dependencies:
  `tests/test_resource_schedule.py::test_resource_critical_path_uses_hour_bucket_finish_not_business_day_duration`;
  `tests/test_resource_schedule.py::test_resource_allocation_uses_global_contention_ledger_without_overbooking`;
  `tests/test_resource_queries.py::test_resource_schedule_capacity_and_utilization_contracts`.
- No process target dates:
  `tests/test_service_mutations.py::test_target_datetime_commands_queries_and_fields_are_not_part_of_contract`;
  `tests/test_resource_api.py::test_target_history_query_is_removed_from_resource_api_contract`.
- `earliest_start_at` and `now` lower bounds:
  `tests/test_resource_schedule.py::test_resource_schedule_respects_process_earliest_start_at_constraint`;
  `tests/test_resource_schedule.py::test_commitment_planner_legal_actions_respect_earliest_start`;
  `tests/test_resource_api.py::test_unpinned_unstarted_process_planned_start_is_lower_bounded_by_now`.
- Calendar local time, holidays, recurrence, and no public horizon:
  `tests/test_calendar.py::test_resource_calendar_expands_local_time_to_utc_half_open_buckets`;
  `tests/test_calendar.py::test_resource_calendar_handles_dst_spring_forward_and_fall_back`;
  `tests/test_resource_schedule.py::test_resource_critical_path_respects_timezone_aware_resource_holidays`;
  `tests/test_resource_queries.py::test_resource_schedule_without_public_horizon_extends_to_required_work`.
- Resource focus, contiguity, one process per bucket, and capacity ceiling:
  `tests/test_resource_schedule.py::test_resource_work_session_switches_process_role_only_after_completion`;
  `tests/test_resource_schedule.py::test_ready_requirements_allocate_one_process_per_resource_hour_bucket`;
  `tests/test_resource_schedule.py::test_multi_role_resource_uses_one_capacity_ledger_across_roles`;
  `tests/test_resource_schedule.py::test_water_filling_uniformly_allocates_then_redistributes_for_unavailability`.
- MCTS owns selected schedules and does not delegate to greedy:
  `tests/test_resource_schedule.py::test_mcts_backend_rejects_greedy_only_constraints_instead_of_falling_back`;
  `tests/test_resource_schedule.py::test_rcpsp_commitment_counterexample_exact_dp_proves_mcts_optimal`;
  `tests/test_resource_schedule.py::test_projdash_commitment_counterexample_projection_is_exhaustively_proven`.
- Permanent infeasibility is structured:
  `tests/test_resource_queries.py::test_resource_schedule_rejects_missing_role_capacity_with_structured_error`.
- Resource-aware completion drives slippage:
  `tests/test_service_mutations.py::test_commit_project_state_extends_horizon_to_sparse_resource_capacity`.
- Child planned start uses `max(now, parent planned/verified finish)`:
  `tests/test_resource_api.py::test_child_planned_start_uses_max_of_parent_verified_finish_and_now`;
  `tests/test_resource_api.py::test_late_pin_pushes_downstream_dependency_schedule`.

### Slack and Outbox

- Teammate drafts are self-contained and avoid internal terms:
  `tests/test_slack_integration.py::test_codex_draft_rejects_internal_project_management_terms`.
- Slack and manual-note evidence ingestion/archive:
  `tests/test_slack_integration.py::test_run_once_collects_messages_invokes_codex_persists_and_sends`;
  `tests/test_slack_integration.py::test_run_once_includes_manual_notes_and_archives_them_after_success`;
  `tests/test_slack_integration.py::test_run_once_keeps_manual_notes_unreconciled_when_codex_fails`.
- Persisted Slack runs and orphan recovery:
  `tests/test_slack_service.py::test_slack_run_records_enforce_one_active_run_per_project`;
  `tests/test_ui_app_logic.py::test_slack_orphaned_run_can_be_marked_failed_to_unlock_ui`.
- Team channel drafts and exact outbox target forms:
  `tests/test_slack_service.py::test_slack_cursors_and_outbox_dedupe_and_status_transitions`;
  `tests/test_slack_service.py::test_slack_outbox_message_targets_are_exact_dm_or_channel_forms`.
- PM evidence claims and sent outbox audit proof:
  `tests/test_slack_integration.py::test_codex_output_requires_due_pm_evidence_claims`;
  `tests/test_slack_service.py::test_pm_communication_protocol_tracks_assignment_review_evidence`;
  `tests/test_slack_service.py::test_pm_evidence_requires_real_sent_slack_outbox_and_recurring_outbox_rows`.
- Draft body is `message_markdown` only:
  `tests/test_slack_integration.py::test_codex_drafts_are_markdown_only`.

### SQLite Storage

- SQLite is the durable local store:
  `tests/test_sqlite_repository.py::test_bootstrap_auto_storage_resolves_to_sqlite_and_rejects_other_storage`;
  `tests/test_ui_service_client.py::test_create_project_service_uses_sqlite_repository_for_sqlite_paths`;
  `tests/test_sqlite_repository.py::test_sqlite_repository_reloads_across_independent_services`.
- Typed JSON rows exclude computed schedule fields:
  `tests/test_sqlite_repository.py::test_sqlite_rows_exclude_computed_fields_and_snapshots_are_explicit`.
- SQLite preserves project facts, revisions, calendars, resources, blockers,
  schedule snapshots, command replay, and Slack state:
  `tests/test_sqlite_repository.py::test_sqlite_repository_persists_projection_and_command_replay`;
  `tests/test_sqlite_repository.py::test_sqlite_repository_round_trips_process_role_pins`;
  `tests/test_sqlite_repository.py::test_sqlite_repository_round_trips_process_evidence_line_items`;
  `tests/test_sqlite_repository.py::test_sqlite_rows_exclude_computed_fields_and_snapshots_are_explicit`;
  `tests/test_sqlite_repository.py::test_sqlite_repository_round_trips_slack_service_state`.
- Local databases and secrets are ignored:
  `tests/test_sqlite_repository.py::test_local_state_databases_and_secrets_are_gitignored`.

### Topology and Snapshots

- Slippage snapshots are created only by explicit commit:
  `tests/test_sqlite_repository.py::test_sqlite_rows_exclude_computed_fields_and_snapshots_are_explicit`;
  `tests/test_service_mutations.py::test_commit_project_state_records_slippage_points_by_terminal_scope`.
- Repeated snapshot commits are idempotent:
  `tests/test_service_mutations.py::test_schedule_snapshot_terminal_symbols_are_set_idempotent`.
- Snapshot fields include timestamp, terminal scope, basis, completion,
  convergence state, and note:
  `tests/test_service_mutations.py::test_commit_project_state_records_slippage_points_by_terminal_scope`;
  `tests/test_sqlite_repository.py::test_sqlite_rows_exclude_computed_fields_and_snapshots_are_explicit`.
- Replace/collapse topology rewrites reconnect and infer effort into one role
  requirement for the replacement process:
  `tests/test_service_mutations.py::test_replace_process_with_subgraph_default_alias_and_edge_reconnects`;
  `tests/test_service_mutations.py::test_replace_process_with_subgraph_infers_roots_and_leaves_from_topology`;
  `tests/test_service_mutations.py::test_collapse_subgraph_preserves_total_effort_hours_by_role_when_omitted`;
  `tests/test_service_mutations.py::test_collapse_subgraph_soft_retires_unions_edges_and_merges_requirements`.
- Deleting a process removes dependency references:
  `tests/test_service_mutations.py::test_delete_process_removes_successor_dependencies_and_process_facts`.

### UI

- UI service boundary:
  `tests/test_ui_service_client.py::test_create_project_service_uses_sqlite_repository_for_sqlite_paths`;
  `tests/test_ui_app_logic.py::test_load_context_omits_expensive_tab_queries`;
  `tests/test_ui_app_logic.py::test_schedule_section_loads_schedule_data_without_cost_or_agent_queries`.
- The removed Blockers tab is absent from main navigation:
  `tests/test_ui_app_logic.py::test_blockers_section_removed_from_main_navigation`.
- Gantt color, process-level rows, process pin overlays, marker counts, topological
  ordering, and step-line dependency connectors:
  `tests/test_ui_adapters.py::test_gantt_bar_color_reflects_completedness_states`;
  `tests/test_ui_adapters.py::test_gantt_rows_are_topological_and_include_process_role_pin_markers`;
  `tests/test_ui_adapters.py::test_gantt_rows_keep_newly_ready_children_close_to_parent`;
  `tests/test_ui_adapters.py::test_gantt_rows_compact_shared_child_dependencies_without_breaking_topology`;
  `tests/test_ui_app_logic.py::test_gantt_pin_marker_plot_uses_one_o_per_start_and_one_x_per_finish`;
  `tests/test_ui_app_logic.py::test_gantt_chart_renders_parent_rows_above_children`.

## Uncredited Tests For Later Review

The complete list of top-level `test_*` functions that are not credited as
direct proof for a named invariant is generated in
`docs/invariant_uncredited_test_functions.md`.

Current counts:

- Total top-level `test_*` functions: 458
- Credited as direct invariant proof: 129
- Uncredited for later review: 329

The uncredited set is intentionally conservative: these tests may still be
valuable as regression, API-shape, migration, formatting, tie-breaker, or
ergonomics coverage, but this audit does not rely on them as the direct proof
for any invariant in `docs/invariants.md`.
