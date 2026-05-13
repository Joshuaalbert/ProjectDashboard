# Resource-Constrained Scheduling Tickets

These tickets implement `docs/resource_constrained_scheduling.md`. The sequence
is intentionally API-contract first: document the command/query surface before
adding tests or implementation, then keep every later ticket test-first inside
its write scope.

## Ticket 1: Agent API Documentation Contract

Goal: publish the agent-facing resource scheduling API contract before tests or
code depend on it.

Owned files:

- `docs/agent_json_dsl.md`

Public API documentation:

- Add project default currency fields plus resource command lists and field
  tables for roles, calendars, exceptions, resources, and process
  `role_requirements`.
- Add resource query list and field tables for `query_resource_schedule`,
  `query_utilization`, `query_costs`, `query_resource_capacity`, and
  `query_unallocated_requirements`.
- Document `CommandResult` and `QueryResult` response wrappers, including
  `entity_ids`, `data`, and the single authoritative `warnings` location.
- Document failed command/query result wrappers, shared `Error` shape,
  `ValidationError` shape, and atomic no-write behavior on validation,
  idempotency, cycle, and mixed-currency errors.
- Define process lifecycle enums, stored-vs-derived status behavior, blocker
  schema, blocked-process derivation, work-now windows, late-risk windows, and
  due-date history output for whole-project, target-process, and topology
  filtered scopes.
- Define command/query names and schemas for `set_process_status`,
  `set_process_due_at`, `set_project_due_at`, `clear_project_due_at`,
  `add_blocker`, `resolve_blocker`, `rename_process`,
  `add_process_aliases`, `batch_update_process_graph`,
  `replace_process_with_subgraph`, `collapse_subgraph`,
  `query_process_graph`, `query_blockers`, and
  `query_due_date_history`.
- Define process graph output with process nodes, dependency edges,
  dependency-only CPM dates, slack, status/blocker summaries, work/late-risk
  windows, and critical-path labels. Document that allocation slices are
  optional evidence only and never virtual graph nodes or edges.
- Define batch mutation and topology rewrite semantics: atomicity, idempotent
  replay, validation order, cycle error shape, replace-process root/leaf
  rewiring, soft-retirement fields and active-as-of query behavior, collapse
  external input/output unioning, attention weighted-sum conservation,
  `required_resource_count` merge conflicts, and rename/alias uniqueness.
- Define complete discriminated payload schemas for every batch operation and
  exact nested shapes for `replace_process_with_subgraph.processes`,
  `replace_process_with_subgraph.dependencies`, and
  `collapse_subgraph.new_process`, including required identity fields,
  result ids, validation constraints, and operation-level idempotency.
- Define the exact `entity_ids.operation_ids` per-operation result object
  schema for `batch_update_process_graph`, including `operation_index`,
  `operation_id`, operation discriminator, status enum, direct
  `revision_id`/`requirement_ids`/`edge_ids`/`alias_process_id` fields,
  categorized created/retired/removed/matched/candidate-only id maps, and
  validation/no-op reason fields.
- Define exact output shapes for allocation slices, unallocated requirements,
  capacity buckets, utilization aggregates, and cost
  aggregates.
- Standardize resource query options on `planning_granularity`; document that
  `bucket_size` is rejected.
- Document `include_allocation_slices` as a `query_resource_schedule` option
  and optional `query_process_graph` evidence flag only when resource-aware
  graph output is requested. Document `include_as_zero_capacity`,
  nullable partial/unallocated row `ready_at`, `starts_at`, and `ends_at`,
  downstream `predecessor_unallocated` behavior for null predecessor finishes,
  contiguous single-resource behavior, min/max daily allocation validation,
  capacity distribution into buckets, the mixed-currency rejection decision,
  and `required_roles_transition_mode`.
- Define the resource-aware critical path as one canonical process-level path,
  not all paths, including ordering and tie breakers after iterative
  resource-constrained finish convergence.
- Define split-allocation fairness as deterministic water-filling, including
  candidate tie breakers, `0.0001` hour rounding tolerance, adjacent-slice
  coalescing rules, and redistribution after daily caps or partial
  unavailability.
- Define process duration recomputation from assigned resource utilization to
  100% for partial availability, multiple roles, multiple resources per role,
  and multi-role resources sharing one capacity ledger.
- Define lifecycle `finished_at` behavior for `set_process_status`, including
  inferred done timestamps, explicit finished timestamps, reopen clearing,
  cancel behavior, and the distinction from scheduled/resource `ends_at`.
- Define project-total due-date semantics, including explicit
  `set_project_due_at`/`clear_project_due_at`, `current_project_due_at`, and
  separate `derived_project_due_at` evidence.
- Define `query_costs` scope, target-process aliases, topology filters,
  resource filters, role filters, grouping options, inactive role/resource
  filter behavior, horizon clipping, and structured mixed-currency errors.
- Define iterative convergence semantics for null starts/finishes, readiness
  maps, unallocated reason changes, allocation-slice fingerprint stability,
  and max-iteration warning/output behavior.
- Document that resource-aware criticality is a process-level dependency-chain
  summary after resource effects, not a resource-contention edge graph, and
  document the user-facing evidence fields for explaining resource delays.
- Define `AllocationSlice.cost_amount` as nullable and non-authoritative; cost
  queries must compute authoritative amounts from allocation slices, resource
  cost facts, and calendar buckets.
- Add compact JSON examples for:
  - creating a role,
  - upserting a calendar with weekly windows,
  - adding/removing an exception,
  - upserting a resource with cost fields,
  - appending a process revision with effort-hour role requirements,
  - querying resource schedule with allocation slices,
  - querying utilization,
  - querying costs.
- State that existing `query_schedule` and `query_critical_path` remain
  dependency-only, and that resource-aware criticality is returned by
  `query_resource_schedule`.

Acceptance checks:

- Examples use the exact field names and enum values from the design.
- Examples use timezone-aware `*_at` datetimes.
- Examples do not include persisted allocation slices or legacy JSON import.
- Query data examples do not include nested warning lists such as
  `data.warnings` or `cost_warnings`.
- Error examples use `ok = false`, shared `Error`, and `ValidationError`
  shapes with precise JSON locations.
- Batch/topology examples include operation discriminators and required
  identity fields.
- The DSL states that a multi-role resource's bucket capacity is shared across
  roles and must not be multiplied in role utilization.
- The DSL rejects `blocked` as stored process status and derives blocked state
  only from unresolved `blocking` blockers.
- Due-date history examples and schemas use timezone-aware mutation timestamps
  and before/after due datetimes where applicable.
- Due-date history distinguishes explicit project total due facts from derived
  project-total due evidence.
- Lifecycle documentation distinguishes `finished_at` from schedule `ends_at`.
- Cost query documentation covers process scope, topology scope, resource and
  role filters, grouping, inactive identities, and mixed-currency validation.
- Convergence documentation covers null-to-non-null, non-null-to-null, and
  null reason-change transitions plus allocation-slice stability.
- Convergence documentation defines the allocation-slice fingerprint as
  excluding both `slice_id` and `iteration`, including exactly process id,
  requirement id, role id, resource id, starts_at, ends_at, effort_hours,
  capacity_hours, and cost_currency, and excluding non-authoritative
  `cost_amount` because authoritative costs are recomputed from stable
  allocation fields, resource cost facts, and calendar buckets.
- Process graph outputs distinguish dependency-only CPM fields from
  resource-aware/converged fields and preserve the no-virtual-nodes rule.
- Topology rewrite documentation states that replace/collapse retire processes
  with `is_active`, `retired_at`, `retired_by_command_id`, and
  `retirement_reason` instead of changing lifecycle status.
- Topology rewrite documentation defines active graph, historical `as_of`,
  due-date history, blocker/status, edge, alias, and result-id behavior for
  retired process ids.
- Replace-process documentation defines
  `preserve_parent_symbol_as_alias` default `true`, requires
  `parent_alias_target_symbol` only when preservation is true and multiple
  children are supplied, forbids it when preservation is false, defaults it to
  the only child for one-child replacements, and defines `alias_process_id`,
  exact replay, later identity resolution, and collision/no-write behavior.
- Collapse documentation defines that compatible same-role requirements copy an
  identical `required_resource_count`, while differing counts require explicit
  replacement requirements and otherwise produce
  `collapse_role_requirement_conflict`.
- Batch role-requirement documentation defines one coalesced process revision
  per touched process, ordered candidate-set mutation, remove-against-candidate
  behavior, no-op/conflict rules, final no-change behavior, and
  `revision_id`/`requirement_ids` reporting for appended, matched, removed, and
  batch-local candidate-only ids.
- Batch result documentation defines exact replay behavior for
  `operation_ids`, same-process coalesced requirement revision reporting,
  candidate-only requirement ids, operation-level no-op statuses/reasons, and
  no nested validation failures in successful operation result entries.
- Resource schedule row documentation defines `ready_at` as nullable, with
  `ready_at = null` only when no predecessor-feasible ready time exists, and
  aligns convergence normalized maps, `predecessor_unallocated`,
  `first_feasible_starts_at`, partial rows, fully unallocated rows, and
  `blocked_zero_capacity` rows with that nullability.
- Cost query documentation defines `group_by` as one-dimensional
  `by_resource`/`by_process`/`by_role` projections plus a `time_series`
  cross-product of requested non-time dimensions and time buckets, serializes
  omitted `CostBucket` dimensions as `null`, keeps totals independent of
  grouping, clips buckets to the horizon, and rejects mixed currencies before
  grouping.

Out of scope:

- Pydantic model changes.
- Tests.
- Engine/storage implementation.

## Ticket 2: Resource Command And Query Models

Goal: add strict Pydantic vocabulary for resource planning without changing
engine behavior.

Owned files:

- `src/projdash/service/commands.py`
- `src/projdash/service/queries.py`
- `src/projdash/service/models.py`
- `tests/test_service_api.py`

Public API changes:

- Add command models:
  - `set_process_status`
  - `set_process_due_at`
  - `set_project_due_at`
  - `clear_project_due_at`
  - `add_blocker`
  - `resolve_blocker`
  - `rename_process`
  - `add_process_aliases`
  - `batch_update_process_graph`
  - `replace_process_with_subgraph`
  - `collapse_subgraph`
  - `set_project_default_currency`
  - `create_role`
  - `rename_role`
  - `deactivate_role`
  - `upsert_resource_calendar`
  - `set_calendar_active`
  - `add_calendar_exception`
  - `remove_calendar_exception`
  - `upsert_resource`
  - `set_resource_active`
  - `set_resource_roles`
  - `set_resource_calendar`
  - `upsert_process_revision.role_requirements`
- Add query models:
  - `query_process_graph`
  - `query_blockers`
  - `query_due_date_history`
  - `query_resource_schedule`
  - `query_utilization`
  - `query_costs`
  - `query_resource_capacity`
  - `query_unallocated_requirements`
- Add nested models for weekly windows, exceptions, role requirements,
  allocation-slice output shapes, `ResourceScheduleRow` with nullable
  `ready_at`, process criticality, utilization aggregates, and cost aggregates
  where service result typing needs them.
- Add `BatchOperationResult` and categorized id-map result models for
  `batch_update_process_graph.entity_ids.operation_ids`, including stable keys
  for operation correlation, statuses, direct ids, created/retired/removed/
  matched/candidate-only ids, and no-op/validated-only reasons.
- Add enums:
  - `ProcessLifecycleStatus`: `planned`, `in_progress`, `paused`, `done`,
    `canceled`
  - `ComputedProcessStatus`: `not_ready`, `ready`, `work_now`, `late_risk`,
    `blocked`, `complete`, `canceled`, `partial`, `unallocated`,
    `blocked_zero_capacity`
  - `BlockerSeverity`: `blocking`, `warning`, `info`
  - `CostUnit`: `hour`, `day`, `week`, `fixed`
  - `AllocationPolicy`: `contiguous`, `split_allowed`
  - `BlockedSchedulingPolicy`: `exclude`, `include_as_zero_capacity`,
    `include_normally`
  - `PlanningGranularity`: `hour`
- Add decimal-compatible cost fields: `cost_rate`, `cost_currency`, nullable
  non-authoritative allocation-slice cost amounts, and authoritative cost-query
  amount strings.
- Add `default_currency` to the project-facing command/query models that expose
  project resource settings.
- Add lifecycle `finished_at` to process-facing result models and status
  command validation where the DSL allows it.
- Add explicit project-total due fields to due-date command/query result models:
  `current_project_due_at` and `derived_project_due_at`.
- Add shared error result models and validation error item models for command
  and query failures.

Required tests:

- JSON round trips for every new command and query family.
- Extra fields fail validation in every new envelope.
- Naive datetimes fail validation for every `*_at` field.
- `blocked` fails validation as a stored process lifecycle status.
- `set_process_due_at` accepts nullable `due_at`, but rejects non-null naive
  due datetimes and naive `edit_at`.
- `set_project_due_at` requires timezone-aware `due_at` and `edit_at`;
  `clear_project_due_at` requires timezone-aware `edit_at`; both reject naive
  datetimes and produce due-date history result ids.
- `query_due_date_history` distinguishes explicit `current_project_due_at` from
  `derived_project_due_at` and includes explicit project-total due events plus
  derived audit events when requested.
- `set_process_status(status = done)` infers `finished_at = edit_at` when
  omitted, accepts explicit aware `finished_at <= edit_at`, rejects naive or
  future `finished_at`, clears `finished_at` on reopen, and keeps lifecycle
  `finished_at` separate from resource schedule `ends_at`.
- Blocker commands reject invalid severities and require timezone-aware
  `created_at`/`resolved_at`.
- Process symbols and aliases reject empty strings and duplicate aliases in one
  request.
- `replace_process_with_subgraph` validates
  `preserve_parent_symbol_as_alias` defaulting, conditional
  `parent_alias_target_symbol` requirements, forbidden target symbols when
  preservation is false, and `alias_process_id` result typing.
- Invalid enum values fail validation.
- Negative capacity, effort, cost, and tolerance values fail validation.
- Non-positive `effort_hours`, `required_resource_count`, and
  `max_iterations` fail validation.
- `horizon_ends_at <= horizon_starts_at` fails validation.
- `min_allocation_hours_per_day > max_allocation_hours_per_day` fails
  validation when both fields are supplied.
- `bucket_size` fails validation on resource queries; `planning_granularity` is
  the accepted field.
- `required_roles_transition_mode` is covered:
  `allow_legacy` accepts either legacy or new role requirements but not both,
  `dual_write_warn` returns a wrapper warning for legacy input, and
  `require_role_requirements` rejects legacy `required_roles`.
- Generated/default fields match the design defaults:
  `required_resource_count = 1`, `allocation_policy = split_allowed`,
  `blocked_policy = exclude`, and `planning_granularity = hour`.
- `query_resource_schedule` and resource-aware `query_process_graph` default
  `include_allocation_slices = false`; utilization, cost, and unallocated
  queries reject that field.
- `query_process_graph.include_resource_fields = true` requires a bounded
  resource horizon; dependency-only graph requests reject resource horizon
  fields and `include_allocation_slices`.
- Result wrappers expose warnings only at `CommandResult.warnings` and
  `QueryResult.warnings`; query data models reject warning aliases.
- Failed command/query wrappers expose one `error`; validation failures expose
  `validation_errors` with `loc`, `msg`, `type`, optional `input`, and `ctx`.
- Process graph result models allow allocation slices only as optional evidence
  and do not model allocation slices as nodes or edges.
- Resource schedule result models allow `ready_at = null` only for rows with no
  predecessor-feasible ready time; partial and ordinary unallocated rows with
  feasible dependencies keep non-null `ready_at`, and
  `first_feasible_starts_at` is null for `predecessor_unallocated`.
- Batch operation result model tests cover stable `operation_ids` object keys,
  status enum values, direct `revision_id`/`requirement_ids`/`edge_ids`/
  `alias_process_id` fields, categorized id maps, candidate-only ids, and
  no-op/validated-only reason fields.
- Due-date history result models include timezone-aware `edit_at`,
  `before_due_at`, and `after_due_at` when present for total-project,
  target-process, and topology-filter scopes.

Out of scope:

- Repository persistence.
- Graph existence validation.
- Engine allocation.

## Ticket 3: Calendar Engine

Goal: implement pure timezone-aware resource calendar expansion.

Owned files:

- `src/projdash/engine/calendar.py`
- `tests/test_calendar.py`

Public API changes:

- Add dataclass read models for resource calendars, weekly windows, exceptions,
  and expanded capacity buckets.
- Add a pure function that expands one calendar over a bounded timezone-aware
  half-open horizon.
- Return UTC-normalized buckets that retain `calendar_id`, `resource_id` when
  supplied, `starts_at`, `ends_at`, `capacity_hours`, `available_hours`,
  `local_date`, and `local_week`.
- Distribute interval `capacity_hours` into `planning_granularity` buckets
  proportionally by elapsed overlap after timezone conversion and clipping.

Required tests:

- Weekly windows expand in the calendar timezone and use `[starts_at, ends_at)`
  semantics.
- A bucket ending exactly at the query start is excluded; a bucket starting
  exactly at the query end is excluded.
- Daylight-saving spring-forward gaps move nonexistent local instants forward
  and drop collapsed windows.
- Daylight-saving fall-back folds cover the full civil-time span.
- Exceptions override recurring capacity only over intersecting intervals and
  do not add capacity outside recurring weekly windows.
- Split windows distribute capacity proportionally; clipped half-hour portions
  of one-person windows produce `0.5` capacity hours, and parallel-capacity
  windows scale proportionally.
- Zero-capacity exceptions close capacity.
- Conflicting overlapping exceptions are rejected.
- Overlapping weekly windows are rejected.
- Invalid horizons are rejected.
- Expansion is deterministic and does not read wall-clock time.

Out of scope:

- Service graph validation.
- Allocation across resources.
- Cost aggregation.

## Ticket 4: Resource Repository Boundary

Goal: store and project resource planning facts through the repository protocol
and in-memory implementation.

Owned files:

- `src/projdash/service/repository.py`
- `src/projdash/service/service.py`
- `src/projdash/service/models.py`
- `tests/test_service_api.py`

Public API changes:

- Extend `ProjectRepository` with role, resource, calendar, exception, and role
  requirement methods plus project `default_currency` access.
- Extend `InMemoryProjectRepository` with deterministic storage for the same
  facts.
- Add service handling for resource commands with graph validation.
- Add service handling for process lifecycle, blocker, due-date, alias, batch
  graph mutation, replace-process-with-subgraph, and collapse-subgraph commands.
- Add a `ResourceScheduleInput` projection containing:
  - latest process revisions as of `as_of`,
  - persisted role requirements owned by those revisions,
  - active/current roles, resources, calendars, windows, and exceptions,
  - unresolved blockers,
  - scheduling options.
- Every active resource must reference its own active calendar directly.
- Implement command idempotency behavior where the repository can persist
  `command_id` payload hashes.
- Add due-date history projection methods that return process-scoped and
  project-total events as of a timezone-aware cutoff.

Required tests:

- Cross-project role, resource, process, calendar, and requirement references
  are rejected.
- Active resources require at least one active role and an active calendar.
- Inactive roles cannot be assigned to resources or new requirements.
- Referenced roles/calendars cannot be deactivated without `force = true`;
  calendar deactivation is handled by `set_calendar_active`, not by bypassing
  checks through `upsert_resource_calendar(active = false)`.
- Forced deactivation preserves historical facts and causes scheduling
  projections to report infeasibility rather than losing references.
- Omitted resource `cost_currency` defaults from project `default_currency`.
- `create_role`, `add_calendar_exception`, and `upsert_resource` are
  idempotent for supplied ids and equivalent fields.
- Reusing the same `command_id` with a different payload is rejected.
- Exact replay of batch/topology commands returns the original result without
  duplicate edges, aliases, blockers, or due-date history events.
- Role/resource/calendar active-name uniqueness is enforced.
- Window and exception id uniqueness is enforced within a calendar.
- Process revisions own their role requirements; historical `as_of` queries
  select the correct revision and requirement ids.
- Projection does not mutate persisted process facts.
- Stored lifecycle status round-trips as `planned`, `in_progress`, `paused`,
  `done`, or `canceled`; blocked state is derived only from unresolved
  `blocking` blockers.
- `query_blockers` derives blocked process ids from unresolved blockers as of
  the supplied `as_of` and excludes resolved blockers by default.
- Due-date history records timezone-aware edit timestamps and before/after due
  datetimes for process and total-project scopes.
- `rename_process` preserves `process_id`, enforces symbol uniqueness, and can
  keep the old symbol as a unique alias.
- `add_process_aliases` rejects aliases that collide with active symbols or
  aliases and resolves aliases unambiguously in later commands.
- `batch_update_process_graph` is atomic across dependency, role-requirement,
  and resource operations; when one operation fails, no operation writes.
- Every batch operation model has a discriminator, exact required identity
  fields, result id expectations, extra-field rejection, and documented
  operation-level no-op/idempotency behavior.
- `batch_update_process_graph` returns one stable `operation_ids` result object
  per operation with documented status, direct ids, categorized id maps,
  candidate-only ids, no-op/validated-only reasons, and exact replay preserving
  the stored operation results.
- Multiple same-process `add_role_requirement` and
  `remove_role_requirement` operations in one batch append at most one process
  revision, apply in operation order to the candidate requirement set, let
  removes see earlier same-batch adds/removes, and report one final
  `revision_id` per touched process in operation results.
- Batch role-requirement tests cover add-then-remove final no-change, remove of
  absent id no-op, duplicate identical add no-op, duplicate conflicting add
  validation error, remove then identical re-add cancellation, remove then
  changed re-add validation error, and exact command-id replay without a second
  revision.
- Batch dependency updates report meaningful cycle errors with operation index,
  attempted edge, process ids, and process symbols.
- `replace_process_with_subgraph` rewires every parent predecessor to each root
  and every leaf to each parent successor, enforces the explicit parent-symbol
  alias preservation rules, and validates the final graph is acyclic.
- `replace_process_with_subgraph` preserves the retired parent symbol as an
  alias by default, defaults the alias target to the only child for one-child
  replacements, requires `parent_alias_target_symbol` for multi-child
  preservation, forbids that field when preservation is false, returns
  `alias_process_id` only when preservation occurs, and rejects alias collisions
  without writes.
- After `replace_process_with_subgraph`, active identity resolution at
  timestamps `>= edit_at` maps the old parent symbol to the target child when
  preservation is enabled and fails active resolution when disabled; exact
  command-id replay returns the original `alias_process_id` and appends no new
  child, alias, edge, or retirement facts.
- `replace_process_with_subgraph` persists soft-retirement fields for the parent
  and retired edges, returns `retired_process_ids`, `retirement_event_ids`, and
  `retired_edge_ids`, and does not change the parent's lifecycle status.
- After `replace_process_with_subgraph`, active graph queries at
  `as_of >= retired_at` omit the parent and retired edges; historical queries at
  `as_of < retired_at` still show the parent and original edges.
- `collapse_subgraph` unions external inputs/outputs into one replacement
  process, removes internal edges, preserves summed effort-hour requirements,
  and applies legacy attention weighted-sum conservation when legacy
  requirements are present.
- `collapse_subgraph` persists soft-retirement fields for selected processes and
  retired edges, returns `retired_process_ids`, `retirement_event_ids`, and
  `retired_edge_ids`, and does not change lifecycle statuses.
- After `collapse_subgraph`, active graph queries at `as_of >= retired_at` show
  only the replacement and rewired external edges; historical queries at
  `as_of < retired_at` still show the original selected processes and internal
  edges.
- Collapse auto-merge tests cover compatible same-role requirements with
  identical `required_resource_count`, daily bounds, and allocation policy:
  effort hours are summed exactly and the identical count is copied.
- Collapse conflict tests cover same-role requirements with different
  `required_resource_count` values and assert no writes plus
  `validation_error` / `collapse_role_requirement_conflict` unless explicit
  replacement requirements are supplied.
- Topology rewrite tests validate the exact
  `replace_process_with_subgraph.processes`,
  `replace_process_with_subgraph.dependencies`, and
  `collapse_subgraph.new_process` nested schemas, including lifecycle
  `finished_at`, role requirements, aliases, roots/leaves, retired ids, and
  created/matched edge ids.
- Project-total due mutation persists explicit due facts, clears them, records
  timezone-aware history events, and does not confuse explicit totals with
  derived latest-process due evidence.

Out of scope:

- LadybugDB durable implementation beyond schema bootstrap.
- Resource-constrained engine logic.

## Ticket 5: LadybugDB Resource Schema

Goal: add durable graph tables and relationships for resource planning facts.

Owned files:

- `src/projdash/service/ladybug_repository.py`
- `tests/test_service_api.py` or a dedicated storage test module

Public API changes:

- Add schema statements for:
  - `Project.default_currency` migration/update if the existing project table
    does not already include it
  - `Role`
  - `Resource`
  - `ResourceCalendar`
  - `CalendarWeeklyWindow`
  - `CalendarException`
  - `RoleRequirement`
  - `ProcessAlias`
  - `Blocker`
  - `DueDateHistoryEvent`
- Add relationship tables:
  - `HAS_ROLE`
  - `HAS_RESOURCE`
  - `HAS_CALENDAR`
  - `HAS_WINDOW`
  - `HAS_EXCEPTION`
  - `CAN_FILL`
  - `USES_CALENDAR`
  - `REQUIRES_ROLE`
  - `REQUIREMENT_ROLE`
  - `HAS_ALIAS`
  - `HAS_BLOCKER`
  - `HAS_DUE_DATE_EVENT`
- Include resource cost fields, project default currency, and calendar/role
  active flags in schema.
- Include timezone-aware blocker and due-date history timestamps plus nullable
  before/after due datetimes.
- Include process and dependency-edge soft-retirement fields:
  `is_active`, `retired_at`, `retired_by_command_id`, and
  `retirement_reason`.
- Include `ProcessRetirementEvent` storage with `retirement_event_id`,
  `process_id`, `retired_at`, `retired_by_command_id`, `retirement_reason`, and
  `replacement_process_ids`.

Required tests:

- Bootstrap creates all new node and relationship tables in a temporary
  LadybugDB database when LadybugDB is available.
- Bootstrap remains idempotent.
- ISO datetime fields preserve timezone offsets.
- Resource cost fields round-trip without float-only assumptions.
- Project `default_currency` round-trips and supplies omitted resource
  currencies.
- Role requirements are linked to `ProcessRevision`, not directly to `Process`.
- Historical process revisions retain their own requirement nodes.
- Process aliases are unique within a project and resolve to one process.
- Blocker `created_at`/`resolved_at` and due-date history `edit_at`,
  `before_due_at`, and `after_due_at` preserve timezone offsets.
- Process and dependency-edge `retired_at` values preserve timezone offsets, and
  active graph projections select rows using the documented active-as-of rule.
- Process retirement events round-trip the retired process ids, replacement
  process ids, timezone-aware `retired_at`, command id, and retirement reason.

Out of scope:

- Full LadybugDB command/query persistence if the adapter is still only a
  bootstrap boundary.
- Persisted allocation slices.

## Ticket 6: Resource Allocation Engine

Goal: compute deterministic resource-constrained schedules and computed
allocation slices from projected read models.

Owned files:

- `src/projdash/engine/schedule.py`
- new `src/projdash/engine/resources.py` if the allocation code is large enough
- `tests/test_schedule.py`

Public API changes:

- Add `ResourceScheduleInput`, `ProcessRoleRequirementInput`, `ResourceInput`,
  `RoleInput`, `ResourceCapacityBucket`, `AllocationSlice`,
  `UnallocatedRequirement`, and `ResourceScheduleResult` dataclasses.
- Add or extend process graph read models that expose dependency-only CPM dates
  (`es_at`, `ef_at`, `ls_at`, `lf_at`), slack, work-now windows, late-risk
  windows, blocker summary, stored status, computed status, and critical labels.
- Add `compute_resource_schedule` that returns:
  - dependency-only schedule rows,
  - resource-constrained process rows,
  - allocation slices,
  - process-level resource-aware critical path fields,
  - unallocated requirements,
  - convergence metadata.
- Match the exact `ResourceScheduleRow`, `AllocationSlice`, and
  `UnallocatedRequirement` shapes in
  `docs/agent_json_dsl.md`.
- Implement the global contention ledger keyed by
  `(resource_id, bucket_starts_at, bucket_ends_at)`.
- Ensure the ledger is shared across all roles for a multi-role resource; do
  not key capacity by role or multiply capacity by `role_ids`.
- Implement ready queue ordering and eligible resource tie breakers exactly as
  specified in the design.
- Implement deterministic water-filling for `split_allowed` allocation,
  including `0.0001` hour comparison tolerance, redistribution after caps or
  unavailable buckets, and documented adjacent-slice coalescing.
- Recompute process resource duration every iteration from assigned slice
  utilization to 100%, including partial availability, multiple roles, and
  multiple resources per role.
- Implement iterative convergence using normalized readiness/start/finish maps,
  allocation states, unallocated reason maps, and allocation-slice fingerprint
  stability, with explicit handling for null values.

Required tests:

- A shared resource delays lower-priority work through the contention ledger.
- A shared resource delay propagates to downstream dependencies.
- A predecessor with null `ends_at`, including blocked predecessors under
  `exclude` or `include_as_zero_capacity`, prevents successors from allocating
  and reports downstream unallocated reason `predecessor_unallocated` with
  successor `ready_at = null`, `starts_at = null`, `ends_at = null`, and
  `first_feasible_starts_at = null`.
- Ready queue tie breakers are deterministic when processes become ready at
  the same time.
- Multiple eligible resources are selected deterministically by earliest
  capacity, then projected cost, then resource id.
- Resource availability intervals clip calendar capacity.
- Process `earliest_start_at`, dependency delays, and blockers are respected.
- `blocked_policy = include_as_zero_capacity` emits a row with
  `allocation_state = blocked_zero_capacity`, populated `ready_at` when
  dependencies are feasible, null `starts_at`/`ends_at`, and no allocation
  slices for the blocked process.
- `split_allowed` can allocate across multiple days/resources.
- Identical eligible resources receive uniform same-bucket allocation through
  water-filling.
- Water-filling redistributes a capped or partially unavailable resource's
  share across the remaining selected resources without exceeding headroom.
- A process with partial resource availability, multiple roles, and multiple
  resources per role recomputes `starts_at`/`ends_at` from assigned effort
  reaching 100%, not from persisted CPM duration.
- `contiguous` is single-resource in v1 and reports
  `contiguous_window_unavailable` when no single eligible resource can satisfy
  the uninterrupted sequence, even when `required_resource_count > 1`.
- `required_resource_count` limits concurrent resources but does not require
  that many resources to exist.
- `min_allocation_hours_per_day` prevents tiny fragments except for final
  remaining effort.
- Partial process rows set non-null `ready_at`, set `starts_at` to the first
  allocated slice, and set `ends_at` to `null`; fully unallocated rows with
  feasible dependencies set non-null `ready_at` and null `starts_at`/`ends_at`;
  predecessor-blocked successor rows set all three schedule datetimes to
  `null`.
- Missing role/resource/capacity/horizon cases produce structured unallocated
  requirements.
- Computed `slice_id` values are stable for identical query inputs and unique in
  a result.
- Allocation slices expose nullable, non-authoritative `cost_amount`; schedule
  tests do not treat it as the source of truth for cost.
- Adjacent same-resource fragments are coalesced when allowed by the design.
- Multi-role resources share one bucket ledger across role requirements; total
  allocation across roles cannot exceed the resource bucket.
- Scheduler-produced buckets never exceed 100% utilization:
  `allocated_hours <= capacity_hours + 0.0001`.
- Resource scheduling integration uses multiple IANA timezones with
  non-identical weekly schedules and proves deterministic allocation across the
  resulting UTC buckets.
- Iteration cap returns `converged = false`, final iteration output, and a
  `max_iterations_reached` warning in `QueryResult.warnings`.
- Convergence tests cover complete null-to-non-null and non-null-to-null finish
  transitions, partial rows with non-null starts and null finishes, unchanged
  null finishes with changed unallocated reasons, readiness/start changes,
  allocation-state changes, and allocation-slice fingerprint changes.
- Convergence tests compare nullable `ready_at` explicitly, including
  null-to-non-null, non-null-to-null, two null `ready_at` values with unchanged
  `predecessor_unallocated` reasons, and two null `ready_at` values with reason
  changes.
- Convergence tests assert that a schedule with identical allocations but
  different allocation-slice `iteration` values converges because `iteration`
  is excluded from the fingerprint.
- Max-iteration tests assert `ok = true`, final iteration output,
  `converged = false`, wrapper warning `max_iterations_reached`, and
  `iteration_not_converged` reasons only for still-unstable requirements.
- Resource-aware critical path returns one canonical process-level path ordered
  from earliest gating process to project finish, applies documented tie
  breakers, and does not emit contention or allocation graph edges.
- Dependency-only `query_critical_path` expectations remain unchanged.
- Dependency-only graph output labels critical and non-critical nodes from CPM
  slack, while resource-aware output keeps converged fields separate.
- Work-now and late-risk windows are derived from ES/LS with explicit
  timezone-aware `now`; tests cover `ES < now < LS`, `now >= LS`, and
  `ES == LS` zero-slack behavior.
- Blocked processes keep blocker-derived computed status separate from stored
  lifecycle status.
- Allocation slices may appear only as optional evidence/debug rows and never
  as process graph nodes or dependency edges.

Out of scope:

- Service query handlers.
- Cost/utilization aggregation.
- Persisting slices.

## Ticket 7: Resource Query Handlers

Goal: expose resource schedule and unallocated requirement projections through
the service query boundary.

Owned files:

- `src/projdash/service/service.py`
- `src/projdash/service/results.py` if typed result helpers are added
- `tests/test_service_api.py`
- `tests/test_schedule.py` for service-level integration tests as needed

Public API changes:

- Implement `query_resource_schedule`.
- Implement `query_process_graph`.
- Implement `query_blockers`.
- Implement `query_due_date_history`.
- Implement `query_resource_capacity`.
- Implement `query_unallocated_requirements`.
- Preserve existing `query_schedule` and `query_critical_path` behavior as
  dependency-only.
- Return JSON-serializable response shapes from the design.
- Use wrapper-only warnings and the exact output shapes from
  `docs/agent_json_dsl.md`.

Required tests:

- `query_resource_schedule` returns `QueryResult.data` with process rows,
  requested allocation slices, critical path fields, unallocated
  requirements, and convergence metadata.
- `query_process_graph` returns process nodes/edges, dependency-only CPM
  fields, slack, status/blocker summaries, work-now windows, late-risk windows,
  critical labels, and optional resource-aware fields without virtual nodes.
- Resource-aware `query_process_graph` requires the same bounded horizon and
  resource options as resource schedule queries; dependency-only graph requests
  reject resource-only fields.
- `query_blockers` returns unresolved blockers and blocked process ids as of
  the supplied timezone-aware `as_of`; resolved blockers appear only when
  requested.
- `query_due_date_history` returns process and project-total due-date events
  for whole-project, target-process, and topology-filtered scopes with
  timezone-aware edit/before/after datetimes.
- `query_due_date_history` returns explicit `current_project_due_at` separately
  from `derived_project_due_at` and includes explicit and derived project-total
  history events according to `include_project_total`.
- Returned process rows cover complete, partial, unallocated, and
  `blocked_zero_capacity` states with the documented nullable `ready_at`,
  `starts_at`, and `ends_at` behavior, including
  `predecessor_unallocated` rows with null `ready_at` and null
  `first_feasible_starts_at`.
- `include_allocation_slices = false` returns an empty slice list without changing
  schedule timing.
- Utilization, cost, and unallocated query handlers reject
  `include_allocation_slices`.
- `query_resource_capacity` returns expanded buckets filtered by resource ids
  and role ids with `planning_granularity`; `bucket_size` is rejected.
- Service-level schedule/capacity integration covers resources in multiple
  IANA timezones with non-identical weekly schedules.
- `query_unallocated_requirements` returns the same unallocated requirements as
  `query_resource_schedule`.
- Query data does not contain nested warning fields.
- Query horizon validation rejects unbounded or inverted ranges.
- Existing `query_schedule` tests still pass without resource fields.

Out of scope:

- Utilization and cost aggregation.
- UI rendering.

## Ticket 8: Utilization And Cost Queries

Goal: expose resource utilization and cost projections from allocation slices.

Owned files:

- `src/projdash/engine/resources.py`
- `src/projdash/service/service.py`
- `tests/test_schedule.py`
- `tests/test_service_api.py`

Public API changes:

- Implement `query_utilization`.
- Implement `query_costs`.
- Add pure aggregation helpers that consume allocation slices and expanded
  capacity buckets.
- Return JSON-serializable resource, role, process, and time-series aggregates.
- Support `query_costs` project, target-process, and topology scopes; resource
  filters; role filters; `group_by`; inactive role/resource filter identities;
  and horizon-clipped time buckets.
- Use `planning_granularity` for time buckets; do not accept `bucket_size`.

Required tests:

- Utilization aggregates capacity, allocated, available, and utilization ratio
  by resource.
- Role utilization aggregates demanded, fulfilled, and unallocated effort.
- Role utilization for multi-role resources derives from allocation slices and
  does not multiply shared resource capacity by role count.
- Overallocated bucket output remains empty for normal scheduler output.
- No normal utilization time-series bucket reports more than 100% allocation
  beyond the `0.0001` hour scheduler tolerance.
- Hourly cost units compute `allocated_hours * cost_rate`.
- Daily cost units charge once per local resource date with any allocation.
- Weekly cost units prorate by allocated hours over available hours in the
  clipped local week.
- Fixed cost units charge once per resource with any allocation in the query
  range.
- Multiple currencies reject aggregation with a structured error; v1 does not
  convert currencies and does not use warnings for this case. The rejection is
  based on contributing resources' `cost_currency`, not
  `AllocationSlice.cost_amount`.
- `query_costs.currency` defaults to project `default_currency`.
- `query_costs` accepts process scope, target process aliases, topology filters,
  resource filters, role filters, and `group_by`; omitted grouping lists are
  empty while `total_cost` still reflects the filtered allocation set.
- `query_costs.group_by` tests cover `["time"]`,
  `["resource", "process", "time"]`, and omitted dimensions: `time_series`
  rows are grouped by selected dimension/time keys only, omitted
  `CostBucket.resource_id`, `process_id`, or `role_id` values are `null`, zero
  rows are not generated, and `by_*` arrays remain one-dimensional projections.
- Cost bucket tests cover hourly, daily, weekly, and fixed costs summing back
  to filtered totals after rounding, horizon clipping of buckets, resource and
  role filters before grouping, target/topology scopes before scheduling, and
  mixed-currency rejection before any bucket rows are returned.
- Unknown resource/role filters fail validation; inactive project-owned
  resource/role filters are accepted and produce zero rows when they contribute
  no allocations.
- Decimal totals are deterministic and serialized as strings.
- Cost and utilization queries use the same allocation slices as
  `query_resource_schedule` for identical inputs.
- Cost queries ignore non-authoritative `AllocationSlice.cost_amount` and compute
  from allocated hours, resource cost facts, and expanded calendar buckets.

Out of scope:

- Persisting allocation slices.
- Visual charts.

## Ticket 9: Streamlit Resource Client Skeleton

Goal: add thin UI calls for resource views after service APIs exist.

Owned files:

- `src/projdash/ui/app.py`
- targeted UI tests if a Streamlit test harness exists

Public API changes:

- UI reads resource schedule, utilization, capacity, unallocated requirements,
  and cost projections through service queries only.
- UI writes roles, calendars, and resources through service commands only.
- UI does not mutate repository or LadybugDB state directly.

Required tests:

- Smoke test or manual verification that the UI imports and starts.
- No direct persistence mutation in UI code.
- Existing non-resource schedule view still works.

Out of scope:

- Full visual dashboard parity with `old_code/`.
- Editing `old_code/`.
- Persisted baseline allocation workflow.

## Sequencing

Implement in order:

1. Ticket 1 updates API documentation before tests or code lock in field names.
2. Ticket 2 adds strict command/query models and validation tests.
3. Ticket 3 proves timezone calendar behavior in the pure engine.
4. Ticket 4 wires validated facts into the repository boundary.
5. Ticket 5 updates durable LadybugDB schema bootstrap.
6. Ticket 6 computes resource schedules, allocation slices, and process-level
   resource-aware critical paths.
7. Ticket 7 exposes schedule/capacity/unallocated query handlers.
8. Ticket 8 exposes utilization and cost queries.
9. Ticket 9 adds a thin Streamlit client after the service surface is stable.

Tickets 3 and 5 can proceed in parallel after Ticket 2 if write scopes remain
disjoint. Ticket 6 should wait for Ticket 4's read model and Ticket 3's calendar
expansion. Tickets 7 and 8 should wait for Ticket 6.
