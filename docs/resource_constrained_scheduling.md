# Resource-Constrained Scheduling Design

## Purpose

ProjectDashboard currently computes a dependency-only schedule from append-only
process revisions. This design adds resource-constrained scheduling while
preserving the existing boundaries:

- `projdash.service` validates command/query payloads, owns graph consistency,
  and projects immutable read models.
- `projdash.engine` performs deterministic calendar expansion, allocation,
  utilization, critical-path, and cost calculations from read models.
- LadybugDB stores authoritative facts and append-only process revisions.
- The UI remains a client of service commands and queries.

The v1 scheduler does not mutate process facts while scheduling. Allocation
slices are computed query outputs unless a later baseline workflow explicitly
commits them as separate facts.

## Terms

- **CPM schedule**: dependency-only schedule from the existing engine. It ignores
  resource contention and remains the result of `query_schedule`.
- **Resource schedule**: schedule produced by applying role requirements,
  resource capacity, blockers, calendars, and contention to the CPM input.
- **Resource-aware critical path**: ordered process chain that controls the
  resource schedule finish after iterative resource-constrained convergence.
- **Requirement**: persisted role effort request owned by a specific
  `ProcessRevision`.
- **Allocation slice**: deterministic, computed allocation of part of a
  requirement to one resource over a half-open interval. It may be returned for
  debugging, utilization, and cost evidence, but it is not a graph node.
- **Contention ledger**: in-memory engine structure that records capacity
  buckets already consumed during one allocation pass.

## Process State, Blockers, And Graph Contracts

Resource scheduling depends on the same process graph that agents inspect
through dependency-only queries. The graph contract is process-level only:
nodes are processes, edges are persisted finish-to-start process dependencies,
and allocation slices are optional evidence/debug rows, not graph nodes or
edges.

### Lifecycle State

Stored process lifecycle status is the explicit project-manager fact:
`planned`, `in_progress`, `paused`, `done`, or `canceled`. The stored status is
changed only by process mutation commands and is not inferred from dates.
`blocked` is not a stored lifecycle status in v1.

`finished_at` is an optional lifecycle timestamp on a process, not a scheduling
timestamp. It records the project-manager assertion that work actually
finished. `set_process_status(status = "done")` sets `finished_at = edit_at`
when omitted; an explicit `finished_at` is allowed only when it is
timezone-aware and no later than `edit_at`. Reopening a done process to
`planned`, `in_progress`, or `paused` clears `finished_at`. Canceling a done
process preserves the existing `finished_at` for audit, but canceling an
unfinished process does not infer one. Resource schedule `ends_at` remains a
computed planning finish and never writes or replaces lifecycle `finished_at`.

Computed status is derived per query from stored status, unresolved blockers,
CPM windows, resource allocation state, and timezone-aware `now`/`as_of`.
Dependency-only graph and schedule queries may return `not_ready`, `ready`,
`work_now`, `late_risk`, `blocked`, `complete`, or `canceled`. Resource-aware
queries may additionally return `partial`, `unallocated`, and
`blocked_zero_capacity` through resource schedule rows. A due datetime passing
does not automatically set `done` or `canceled`; it produces late-risk/follow-up
state until an explicit lifecycle edit is recorded.

### Blockers

Blockers are append-only facts attached to one process:

| Field | Type | Rules |
| --- | --- | --- |
| `blocker_id` | string | Project-scoped stable id. |
| `project_id` | string | Owning project. |
| `process_id` | string | Blocked process. |
| `summary` | string | Non-empty summary. |
| `details` | string or null | Optional context. |
| `severity` | enum | `blocking`, `warning`, or `info`; only `blocking` affects blocked derivation. |
| `created_at` | aware datetime | Inclusive effective timestamp. |
| `resolved_at` | aware datetime or null | Null means unresolved. |
| `resolution` | string or null | Resolution note. |

A blocker is unresolved as of a query when `created_at <= as_of` and
`resolved_at` is null or greater than `as_of`. A process is blocked when it is
not `done` or `canceled` and has one or more unresolved `blocking` blockers.
`query_blockers` returns blocker rows plus `is_resolved_as_of`,
`is_blocking_as_of`, and the derived `blocked_process_ids` list. Resolved
blockers are omitted unless `include_resolved = true`.

### Work Windows And Late Risk

Dependency-only CPM computes timezone-aware early/late dates for every process:
`es_at`, `ef_at`, `ls_at`, and `lf_at`. `work_now_window` is `[es_at, ls_at)`.
It is active when the process is not done, canceled, or blocked and
`es_at <= now < ls_at`. `late_risk_window` is `[ls_at, lf_at)`. It is active
when the process is not done or canceled and `now >= ls_at`; unresolved blockers
keep the computed status as `blocked` while still exposing the late-risk window
for triage. If `es_at == ls_at`, the process enters late risk immediately at
`ls_at`.

All comparisons use timezone-aware instants. Query callers must pass `as_of`
for fact selection and `now` for window/status evaluation; tests must not read
wall-clock time.

### Due-Date History

Process due datetimes and explicit total-project due datetimes are mutable facts
audited as append-only history events. `set_project_due_at` stores a non-null
timezone-aware total due datetime. `clear_project_due_at` clears that explicit
project total due. The derived latest process due datetime is also exposed for
analysis, but it does not overwrite the explicit project total due fact. Every
mutation that changes a due datetime records a timezone-aware edit timestamp and
`before_due_at`/`after_due_at` values where applicable. Clearing a due date
stores `after_due_at = null`; setting the first due date stores
`before_due_at = null`.

`query_due_date_history` supports whole-project, target-process, and
topology-filtered scopes. Target-process scope returns only that process's due
events plus current due. Topology filters select ancestors, descendants, or both
from the dependency graph as of `as_of`; their `process_events` include only the
selected processes. `project_total_events` describes explicit total-project due
edits and derived total due changes caused by process due-date edits, unless
`include_project_total = false`. Query output distinguishes
`current_project_due_at` for the explicit fact from `derived_project_due_at` for
the latest selected process due datetime.

### Process Graph Output

`query_process_graph` is the graph-level contract for agents. Output contains:

- `nodes`: process nodes with id, canonical symbol, aliases, display name,
  duration, earliest-start constraint, current due datetime, stored status,
  lifecycle `finished_at`, computed status, blocker summary,
  dependency-only CPM fields, optional
  resource-aware fields, work-now window, and late-risk window.
- `edges`: persisted process dependencies with predecessor/successor ids and
  symbols. v1 dependency type is `finish_to_start`.
- `critical_path_process_ids`: process ids only.
- `schedule_basis`: `dependency_only` or `resource_aware`.
- `converged`: null for dependency-only output, otherwise the resource schedule
  convergence flag.

Dependency-only fields (`es_at`, `ef_at`, `ls_at`, `lf_at`, `slack_hours`, and
critical labels) are separate from resource-aware/converged fields
(`ready_at`, `starts_at`, `ends_at`, `resource_delay_hours`, resource-aware
slack, and `allocation_state`). Resource contention affects resource-aware
dates through the capacity ledger; it does not create synthetic graph edges.

## Authoritative Facts

### Project Resource Settings

Resource planning adds one project-level setting:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `default_currency` | string | yes | ISO 4217 code; defaults to `USD` on `create_project` when omitted. |

Resource commands default omitted `cost_currency` values from the owning
project. `query_costs.currency` also defaults to `default_currency`.

### Resource Calendar

A resource calendar defines timezone-aware availability. Calendars are project
owned and attached directly to resources. Calendar availability is expressed in
local civil time because people and vendors follow local work rules and
daylight-saving transitions.

Fields:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `calendar_id` | string | yes | Project-scoped stable id. |
| `project_id` | string | yes | Owning project. |
| `name` | string | yes | Unique among active calendars in a project. |
| `timezone` | string | yes | IANA timezone, for example `America/New_York`. |
| `weekly_windows` | list | yes | Recurring local half-open windows. |
| `exceptions` | list | no | Dated half-open overrides. |
| `active` | bool | yes | Default `true`; inactive calendars cannot be newly assigned. |

Weekly window fields:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `window_id` | string | yes | Unique within the calendar. |
| `weekday` | int | yes | `0` Monday through `6` Sunday. |
| `start_local_time` | string | yes | `HH:MM[:SS]`, inclusive. |
| `end_local_time` | string | yes | `HH:MM[:SS]`, exclusive and after start; no overnight windows in v1. |
| `capacity_hours` | number | yes | Finite, `>= 0`; normally equals window duration for one full-time resource. |

Exception fields:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `exception_id` | string | yes | Unique within the calendar. |
| `starts_at` | aware datetime | yes | Inclusive. |
| `ends_at` | aware datetime | yes | Exclusive and after `starts_at`. |
| `capacity_hours` | number | yes | Replacement capacity across the exception interval. |
| `reason` | string | no | Human note. |

Calendar rules:

- All expanded intervals are half-open: `[starts_at, ends_at)`.
- Stored datetimes are timezone-aware ISO strings. The offset is preserved, and
  expansion also stores the IANA timezone so future recurrences use local-time
  rules rather than a fixed offset.
- Weekly windows for the same weekday cannot overlap in local time.
- Exceptions override recurring windows only over their intersecting interval.
  They do not create capacity outside recurring weekly windows in v1. An
  exception with `capacity_hours = 0` closes capacity for that interval.
- Exceptions are allowed to overlap only when they have the same
  `capacity_hours`; conflicting overlaps are a service validation error.
- Calendar expansion returns UTC-normalized buckets with the original calendar
  id and local date for display.

DST behavior:

- Weekly windows are interpreted in calendar local time and converted to
  timezone-aware instants for each local date in the query horizon.
- During spring-forward gaps, nonexistent local instants are moved forward to
  the first valid instant. A window that collapses to zero duration emits no
  bucket.
- During fall-back folds, ambiguous local instants use the earlier occurrence
  for `start_local_time` and the later occurrence for `end_local_time`, so the
  local work window covers the full civil-time span.
- Bucket `capacity_hours` is capped by actual elapsed interval hours after DST
  conversion unless the window explicitly models more than one parallel unit of
  capacity.

### Roles, Resources, And Costs

A role describes a skill or responsibility. A resource describes a person, team,
vendor, or machine that can supply capacity for one or more roles.

Role fields:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `role_id` | string | yes | Project-scoped stable id. |
| `project_id` | string | yes | Owning project. |
| `name` | string | yes | Unique among active roles in a project. |
| `active` | bool | yes | Inactive roles cannot be used by new revisions/resources. |

Resource fields:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `resource_id` | string | yes | Project-scoped stable id. |
| `project_id` | string | yes | Owning project. |
| `name` | string | yes | Unique among active resources in a project. |
| `role_ids` | list[string] | yes | Non-empty when `active = true`. |
| `calendar_id` | string | yes | Active calendar in the same project. |
| `available_from_at` | aware datetime | yes | Inclusive. |
| `available_until_at` | aware datetime | no | Exclusive and after `available_from_at`. |
| `cost_rate` | decimal string or number | yes | Finite, `>= 0`. |
| `cost_unit` | enum | yes | `hour`, `day`, `week`, or `fixed`. |
| `cost_currency` | string | yes | ISO 4217 code, default project currency if omitted by command. |
| `holidays` | list | no | Resource-local zero-capacity intervals. |
| `active` | bool | yes | Inactive resources have no schedulable capacity. |

Resource holiday fields:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `holiday_id` | string | no | Unique within the resource; generated when omitted by command. |
| `starts_at` | aware datetime | yes | Inclusive instant. |
| `ends_at` | aware datetime | yes | Exclusive and after `starts_at`. |
| `reason` | string | no | Human note. |

Resource holidays close capacity for only that resource after reusable calendar
windows and calendar exceptions have been applied. They do not change the
shared calendar and they do not create capacity outside weekly windows.

Cost accounting:

- Engine cost output uses decimal arithmetic and serializes amounts as strings
  with two decimal places unless the project later defines a currency exponent.
- ProjectDashboard does not convert currencies in v1. `query_costs` must use one
  requested currency. If allocated resources contributing to a cost query use
  more than one `cost_currency`, or any contributing resource currency differs
  from the requested currency, the query fails validation with a structured
  error; agents must run separate cost queries per currency.
- `hour`: `allocated_hours * cost_rate`.
- `day`: charge `cost_rate` once for each local calendar date with any
  allocation for that resource.
- `week`: charge `cost_rate * allocated_hours / available_hours_in_week` for
  each local calendar week touched by allocation. The denominator is expanded
  capacity for that resource in that week, clipped to the query horizon and
  availability interval.
- `fixed`: charge `cost_rate` once when the resource has any allocation in the
  query range.
- Costs are computed from allocation slices and current resource cost facts as of
  the query. Historical resource cost revisions are out of scope for v1.

Multi-role capacity sharing:

- A resource with multiple `role_ids` has one capacity ledger, not one capacity
  ledger per role. The resource may satisfy any of its active roles, but all
  role allocations consume the same bucket `remaining_hours`.
- In one bucket, a resource can work on more than one role only when the total
  allocated effort across all roles remains within the bucket's
  `capacity_hours` and any requirement-level daily cap residuals.
- Role utilization is attributed from allocation slices. Capacity is counted by
  resource first; role views must not multiply a multi-role resource's capacity
  once per role.

### Process Role Requirements

Process revisions currently contain `required_roles: dict[str, float]`. The new
model replaces ambiguous FTE-style values with effort-hour requirements.

Requirement fields:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `requirement_id` | string | yes | Stable id owned by a single `ProcessRevision`. |
| `revision_id` | string | yes | Owning process revision. |
| `project_id` | string | yes | Owning project, copied for validation/indexing. |
| `process_id` | string | yes | Process that owns the revision. |
| `role_id` | string | yes | Active role in same project at command time. |
| `effort_hours` | number | yes | Total role effort, finite and `> 0`. |
| `min_allocation_hours_per_day` | number | no | Useful-work lower bound per local resource day; finite and `>= 0`. |
| `max_allocation_hours_per_day` | number | no | Per-resource/process cap per local resource day; finite and `> 0`. |
| `required_resource_count` | int | yes | Default `1`; maximum number of resources that may work concurrently. |
| `allocation_policy` | enum | yes | `contiguous` or `split_allowed`. |

Effort-hour semantics:

- `effort_hours` is total work required for the role. It is not FTE and is not
  automatically multiplied by process duration.
- Process duration still matters for dependency-only CPM and for the fallback
  migration from `required_roles`; resource schedule finish is driven by
  allocated effort.
- A requirement is complete when fulfilled slice effort equals `effort_hours`
  within `0.0001` hours.
- `required_resource_count` is a concurrency ceiling, not a staffing minimum.
  If fewer eligible resources are available, the engine uses fewer and may
  report slower completion rather than infeasibility.
- `max_allocation_hours_per_day` limits each resource's work on that requirement
  by the resource's local date. If absent, the resource calendar capacity is the
  cap.
- `min_allocation_hours_per_day` prevents tiny fragments. The engine may use a
  shorter final allocation only for the last remaining effort on a requirement.
- When both daily bounds are supplied, service validation requires
  `min_allocation_hours_per_day <= max_allocation_hours_per_day`.
- `contiguous` is single-resource in v1, even when
  `required_resource_count > 1`. The scheduler chooses one eligible resource and
  requires one uninterrupted sequence for that resource, excluding non-working
  gaps. If no single eligible resource can satisfy the contiguous sequence, the
  requirement is unallocated with reason `contiguous_window_unavailable`.
- `split_allowed` permits allocation across buckets, days, and eligible
  resources.

Compatibility:

- During a transition, legacy `required_roles` may be mapped to requirements as
  `effort_hours = duration_business_days * 8 * value`.
- New commands should prefer `role_requirements`. A command must not set both
  `required_roles` and `role_requirements`.
- The service configuration flag `required_roles_transition_mode` controls
  legacy behavior:
  - `allow_legacy`: accept either `required_roles` or `role_requirements`.
  - `dual_write_warn`: accept either shape and return a wrapper warning when
    `required_roles` is used.
  - `require_role_requirements`: reject `required_roles` for resource-aware
    process revisions.

## Identity, Lifecycle, And Idempotency

All ids are opaque non-empty strings unless a command explicitly omits an
optional id and asks the service to generate one. Generated ids are stable only
in the command result.

Uniqueness:

- `role_id`, `resource_id`, and `calendar_id` are unique within a project.
- Active `Role.name`, `Resource.name`, and `ResourceCalendar.name` are unique
  within a project, case-sensitive in v1.
- `window_id` and `exception_id` are unique within a calendar.
- `requirement_id` is unique within a process revision.
- Computed `slice_id` is unique within one query result.

Idempotency:

- `CommandEnvelope.command_id` may be used by a repository implementation to
  deduplicate a retried command. Replaying the same command id with a different
  payload is an error.
- `create_role` is idempotent when `role_id` is supplied and all fields match
  the existing role. Name-only duplicate creation is rejected.
- `rename_role`, `set_calendar_active`, `set_resource_active`,
  `set_resource_roles`, and `set_resource_calendar` are no-ops when the target
  already has the requested value.
- `upsert_resource_calendar` replaces the full weekly-window set for the
  calendar id. Existing exceptions are preserved unless explicitly removed.
- `add_calendar_exception` is idempotent for the same `exception_id` and fields.
- `remove_calendar_exception` is idempotent when the exception is already
  absent.
- `upsert_resource` is idempotent for the same `resource_id` and equivalent
  fields; omitted optional ids are generated and returned in `entity_ids`.
- `upsert_process_revision` always appends a new process revision unless the
  command id is replayed exactly.

Lifecycle:

- Deactivation is preferred over deletion. Historical process revisions must
  remain queryable.
- Inactive roles cannot be added to resources or new process requirements.
- A role cannot be deactivated while active resources or current process
  revisions depend on it unless `force = true`; forced deactivation leaves
  historical requirements intact and current scheduling reports infeasibility.
- Inactive calendars cannot be assigned to active resources.
- Calendar activation is controlled only by `set_calendar_active`. A calendar
  cannot be deactivated while active resources use it unless `force = true`;
  forced deactivation makes those resources unschedulable. `active = false` in
  `upsert_resource_calendar` is allowed only for new or otherwise unreferenced
  calendars and must not bypass this force rule.
- Inactive resources retain historical identity but contribute no capacity.

Process symbols and aliases:

- Every active process has one canonical `process_symbol` unique within the
  project.
- Aliases are also unique within the project and resolve to exactly one active
  process.
- Commands may accept a process id or process symbol, but persistence stores
  ids. Alias resolution happens before validation.
- `rename_process` preserves `process_id`; by default the old canonical symbol
  becomes an alias for the same process.
- `add_process_aliases` adds aliases without changing the canonical symbol.

Batch graph mutation:

- `batch_update_process_graph` is the atomic mutation surface for multi-process
  dependency, role-requirement, and resource fact edits.
- Supported operations are `add_dependency`, `remove_dependency`,
  `add_role_requirement`, `remove_role_requirement`, `upsert_resource`,
  `set_resource_roles`, and `set_resource_calendar`.
- Every operation may include optional `operation_id` for caller correlation.
  When omitted, the service generates one in the result; generated operation
  ids are stable only for the stored command result and exact envelope replay.
- Validation order is payload shape, id/symbol/alias resolution, operation-local
  consistency, role/resource/calendar reference checks, candidate graph cycle
  validation, then append.
- The command writes nothing unless every operation validates.
- Exact command-id replay returns the original result. Reusing the same
  command id with a different payload is rejected. Existing dependency adds and
  absent dependency removes are operation-level no-ops; conflicting field values
  are errors.
- Requirement mutations in one batch are coalesced per process. The service
  builds one candidate requirement set for each touched process, applies
  `add_role_requirement` and `remove_role_requirement` operations in list order
  against that candidate, and appends at most one new `ProcessRevision` per
  process after all operations validate. A remove sees earlier same-process
  adds/removes in the same batch. Adding an id already present in the candidate
  with identical fields is a matched no-op; different fields are a validation
  error. Removing an absent id is a no-op. Removing an existing requirement
  reserves a batch-local tombstone; re-adding the same id later in the same
  batch is allowed only with fields identical to the removed requirement and
  cancels the removal. To change fields, callers remove the old id and add a
  different `requirement_id`. If the final candidate equals the original
  selected revision, no revision is appended. Otherwise every requirement
  operation for that process reports the same appended `revision_id`; exact
  envelope replay returns those original ids without appending again.
- Cycle validation reports `code = "dependency_cycle"` with
  `operation_index`, attempted `edge`, `cycle_process_ids`, and
  `cycle_process_symbols` so agents can repair the exact dependency chain.
- The successful result contains `entity_ids.operation_ids`, a list of
  `BatchOperationResult` objects in request order, not a list of strings. Each
  object has stable keys: `operation_index`, `operation_id`, `action`, `status`,
  `revision_id`, `requirement_ids`, `edge_ids`, `alias_process_id`,
  `created_ids`, `retired_ids`, `removed_ids`, `matched_ids`,
  `candidate_only_ids`, `no_op_reason`, and `validation_reason`.
- Operation `status` is `applied` when the operation contributed to a persisted
  fact, `no_op` when the requested final state already matched or an idempotent
  symbolic remove targeted an absent edge/requirement, and `validated_only`
  when the operation affected only the batch-local candidate. Validation
  failures are not encoded as operation results; they return the shared
  `CommandErrorResult` and the whole batch writes nothing.
- The categorized id maps use stable plural arrays. `created_ids` contains
  `process_ids`, `edge_ids`, `requirement_ids`, `resource_ids`,
  `revision_ids`, `calendar_ids`, `blocker_ids`, `due_history_event_ids`, and
  `retirement_event_ids`; `retired_ids` contains `process_ids`, `edge_ids`, and
  `retirement_event_ids`; `removed_ids` contains `edge_ids`,
  `requirement_ids`, and `calendar_exception_ids`; `matched_ids` contains
  `process_ids`, `edge_ids`, `requirement_ids`, `resource_ids`,
  `calendar_ids`, and `revision_ids`; `candidate_only_ids` currently contains
  `requirement_ids`.
- Same-process coalesced requirement operations all report the final
  `revision_id` for that process: the appended revision when the final
  candidate changed, or the original selected revision when it did not.
  `requirement_ids` is the operation-local union of created, removed, matched,
  and candidate-only requirement ids. Requirement ids generated, added, and
  removed entirely inside one batch appear only in
  `candidate_only_ids.requirement_ids` and operation-local `requirement_ids`;
  they are excluded from top-level `entity_ids.requirement_ids`.
- Exact `command_id` replay returns the stored batch result unchanged,
  including operation statuses, generated `operation_id` values, revision ids,
  candidate-only ids, and all categorized id maps. A later command with a
  different `command_id` may produce operation-level `no_op` results using the
  current graph state.

Topology rewrite operations:

- `replace_process_with_subgraph` retires one active parent process and creates
  supplied child processes and internal dependencies in one atomic revision.
  Every external incoming edge to the parent is copied to each supplied root.
  Every external outgoing edge from the parent is copied from each supplied
  leaf. Roots and leaves must be children, child symbols must be unique, and the
  final graph must be acyclic. Parent-symbol alias preservation is controlled by
  `preserve_parent_symbol_as_alias`, which defaults to `true`. When true, the
  retired parent canonical symbol becomes an alias for exactly one created
  child; `parent_alias_target_symbol` defaults to the only child when there is
  one child and is required when multiple children are supplied. When
  `preserve_parent_symbol_as_alias = false`, `parent_alias_target_symbol` is
  forbidden and the parent symbol stops resolving for active identities after
  `retired_at`. The target symbol must name a supplied child process, and alias
  preservation fails atomically if it would collide with any final active symbol
  or alias other than the retired parent's own pre-rewrite identity.
- `collapse_subgraph` replaces a non-empty, weakly connected process set with
  one new process. External predecessors of any collapsed root become
  predecessors of the new process, and external successors of any collapsed leaf
  become successors of the new process. Duplicate external inputs/outputs are
  unioned, internal edges are removed, and the final graph must be acyclic.
  The new symbol must not collide with active symbols or aliases outside the
  collapsed set.
- Retiring a process is a topology fact, not a lifecycle status change. The
  persisted process projection stores `is_active`, `retired_at`,
  `retired_by_command_id`, and `retirement_reason`; active processes have
  `is_active = true` and null retirement fields. `retired_at` is the rewrite
  command's timezone-aware `edit_at`, `retired_by_command_id` is the envelope
  command id, and `retirement_reason` is `replace_process_with_subgraph` or
  `collapse_subgraph`. The process lifecycle `status` and `finished_at` remain
  independent actual-work facts and are not rewritten to `canceled`.
- Edges removed by replace/collapse rewiring are also soft-retired with the same
  active-interval behavior. Active graph projections select processes and edges
  that existed at `as_of` and have no `retired_at` or have `retired_at > as_of`.
  Historical visibility is obtained by querying an earlier `as_of`; v1 does not
  introduce virtual graph nodes/edges or an `include_retired` graph flag.
- Each retired process also records a `ProcessRetirementEvent` with
  `retirement_event_id`, `project_id`, `process_id`, `retired_at`,
  `retired_by_command_id`, `retirement_reason`, and `replacement_process_ids`.
  Rewrite command results return `retired_process_ids`,
  `retirement_event_ids`, `retired_edge_ids`, and the active replacement/child
  process and edge ids.
- After `retired_at`, retired process symbols and aliases no longer resolve for
  active commands or active graph scopes unless the rewrite explicitly assigns a
  string as an alias for a new active process. Historical queries before
  `retired_at` use the historical alias mapping. Commands that mutate process
  status, due dates, dependencies, aliases, or requirements require an active
  process identity as of `edit_at`.
- After a successful replace with `preserve_parent_symbol_as_alias = true`,
  active command/query identity resolution at timestamps `>= edit_at` resolves
  the old parent symbol to the target child. The command result includes
  `alias_process_id` equal to that child id. Exact command-id replay returns
  the same `alias_process_id` and writes nothing. A later command with a
  different `command_id` is a new edit: naming the old parent symbol targets the
  child when preservation is enabled, and fails active process resolution when
  preservation is disabled.
- Due-date history remains attached to stable retired process ids for audit.
  A target-process due-date history query by retired `process_id` is valid and
  returns the latest due fact as `current_due_at` as of the query cutoff.
  Whole-project and topology-filtered `derived_project_due_at` exclude processes
  retired as of `as_of`; historical `as_of` values before retirement include
  them. Retirement does not clear due facts or write due-date history events.
- Blockers attached to retired processes are retained and may be resolved by
  `blocker_id`, but after retirement they do not contribute to active
  `blocked_process_ids` or active graph computed status.
- Collapse infers the replacement duration from the dependency-only critical
  path through the selected subgraph. Effort-hour role requirements are summed
  by role only when the same-role requirements have identical
  `required_resource_count`, `min_allocation_hours_per_day`,
  `max_allocation_hours_per_day`, and `allocation_policy`. For a compatible
  same-role set, the replacement requirement uses
  `effort_hours = sum(effort_hours_i)` and copies the identical scheduling
  controls. Different `required_resource_count` values are a conflict, not an
  input to a formula; the service must not average, sum, maximize, or round
  counts because that silently changes the concurrency ceiling. Conflicts in
  counts, daily bounds, or allocation policy reject the command with
  `validation_error` / `collapse_role_requirement_conflict` unless explicit
  replacement role requirements are supplied.
- Legacy attention/FTE values are conserved with
  `sum(attention_i * duration_i) / subgraph_cp_duration`; zero inferred duration
  with non-zero attention is rejected unless explicit replacement role
  requirements are supplied. Legacy attention/FTE values do not derive
  `required_resource_count`; once converted to effort-hour requirements, the
  same count/bounds/policy compatibility rule applies. Mixed legacy
  attention/FTE and effort-hour requirements for the same role require explicit
  replacement requirements.
- Rewrites reject empty selections, duplicate symbols, roots/leaves outside the
  child set, active external alias collisions, references to retired processes
  outside the command, and any candidate graph cycle.

## Allocation Slice Identity

Requirement identity is persisted in LadybugDB because requirements are part of
the process revision. Slice identity is computed because allocation is a query
projection. Allocation slices are not graph nodes and do not create persisted or
computed graph edges in v1.

`slice_id` is deterministic from:

1. query action and schema version,
2. `project_id`,
3. `as_of`, `horizon_starts_at`, `horizon_ends_at`,
4. effective scheduling options,
5. `iteration`,
6. `process_id`,
7. `requirement_id`,
8. `role_id`,
9. `resource_id`,
10. `starts_at`,
11. `ends_at`,
12. ordinal index for adjacent same-resource fragments with identical
    timestamps.

Allocation slice fields:

| Field | Type | Rules |
| --- | --- | --- |
| `slice_id` | string | Deterministic within the query result. |
| `project_id` | string | Owning project. |
| `process_id` | string | Allocated process. |
| `requirement_id` | string | Fulfilled requirement. |
| `role_id` | string | Required role. |
| `resource_id` | string | Assigned resource. |
| `starts_at` | aware datetime | Inclusive UTC-normalized instant. |
| `ends_at` | aware datetime | Exclusive and after `starts_at`. |
| `effort_hours` | number | Work fulfilled by this slice. |
| `capacity_hours` | number | Capacity consumed from the resource ledger. |
| `cost_amount` | string or null | Non-authoritative display hint; null in v1 schedule output. |
| `cost_currency` | string or null | Resource ISO 4217 currency when known. |
| `iteration` | int | Final convergence iteration that produced the slice. |

`AllocationSlice.cost_amount` is nullable and not authoritative. Cost aggregation
does not read it; `query_costs` computes authoritative amounts from slice
allocated hours, current resource cost facts, and expanded calendar buckets.
The v1 resource schedule may set `cost_amount = null` even when cost facts are
available.

Graph edges:

- Persisted edges connect `ProcessRevision -> RoleRequirement -> Role` and
  `Resource -> Role` / `Resource -> ResourceCalendar`.
- Computed allocation slices do not create graph edges in v1.
- Resource contention is represented by capacity consumed in the global ledger
  and by the resulting process `starts_at`, `ends_at`, and
  `resource_delay_hours`, not by synthetic graph edges.

## Engine Read Model

The service projects a `ResourceScheduleInput`:

| Field | Type | Rules |
| --- | --- | --- |
| `project_id` | string | Required. |
| `project_start_at` | aware datetime | Project planning start. |
| `as_of` | aware datetime | Revision cutoff. |
| `now` | aware datetime | Status/reference time only. |
| `processes` | list | Latest process revisions as of `as_of`. |
| `dependencies` | list[edge] | Validated acyclic process graph. |
| `role_requirements` | list | Requirements for selected revisions. |
| `roles` | list | Current role facts. |
| `resources` | list | Current resource facts. |
| `calendars` | list | Current calendars and exceptions. |
| `blockers` | list | Unresolved blockers as of query. |
| `options` | object | Scheduling options below. |

Options:

| Field | Type | Default | Rules |
| --- | --- | --- | --- |
| `planning_granularity` | enum | `hour` | `hour` in v1; later `day` may be added. |
| `horizon_starts_at` | aware datetime | required | Inclusive. |
| `horizon_ends_at` | aware datetime | required | Exclusive. |
| `max_iterations` | int | `20` | Positive, capped by service config. |
| `convergence_tolerance_hours` | number | `0` | Non-negative. |
| `blocked_policy` | enum | `exclude` | `exclude`, `include_as_zero_capacity`, `include_normally`. |

Response shaping options accepted by `query_resource_schedule` and by
resource-aware `query_process_graph` when that query explicitly requests
resource evidence:

| Field | Type | Default | Rules |
| --- | --- | --- | --- |
| `include_allocation_slices` | bool | `false` | Includes computed allocation slices in schedule or graph output when true. |

`query_utilization`, `query_costs`, and `query_unallocated_requirements`
compute any needed allocation slices internally and reject
`include_allocation_slices`.

All datetimes are timezone-aware. Tests pass explicit `as_of` and `now`.
`planning_granularity` is the only bucket-size option name for resource
queries. `bucket_size` is not accepted in v1.

Warnings:

- `CommandResult.warnings` and `QueryResult.warnings` are the only
  authoritative warning locations.
- Query `data` objects must not include `warnings`, `cost_warnings`, or other
  warning aliases.
- Warnings use the shared object shape `{code, message, severity, details}`.

## Scheduling Algorithm

The engine uses a stable heuristic, not an optimizer. It must return the same
result for the same read model and options.

### Calendar Expansion

For each active resource:

1. Clip the query horizon to `[available_from_at, available_until_at)`.
2. Expand the resource calendar into local weekly intervals that intersect the
   clipped horizon.
3. Apply exceptions by replacing recurring capacity over intersecting
   half-open intervals.
4. Apply resource-local holidays as zero-capacity half-open intervals.
5. Split intervals into `planning_granularity` buckets in UTC. Buckets keep
   `calendar_id`, `resource_id`, `starts_at`, `ends_at`, `capacity_hours`,
   `available_hours`, `local_date`, and `local_week`.
6. Drop zero-capacity buckets after exception and holiday application.

Capacity distribution:

- `available_hours` is the actual elapsed bucket duration after timezone/DST
  conversion and clipping.
- When a calendar window or exception interval is split across multiple
  buckets, `capacity_hours` is distributed in proportion to each bucket's
  elapsed overlap:
  `interval_capacity_hours * overlap_elapsed_hours / interval_elapsed_hours`.
- A full one-hour bucket inside an eight-hour one-person window therefore gets
  `1.0` `capacity_hours`; a half-hour clipped bucket gets `0.5`.
- If a window intentionally models parallel capacity, for example
  `capacity_hours = 16` over an eight-hour interval, each full hour bucket gets
  `2.0` `capacity_hours`.
- `remaining_hours` starts equal to `capacity_hours`; `allocated_hours` starts
  at `0`.

### Global Contention Ledger

The ledger is rebuilt from scratch on each allocation iteration.

Ledger key:

```text
(resource_id, bucket_starts_at, bucket_ends_at)
```

Ledger value:

| Field | Meaning |
| --- | --- |
| `capacity_hours` | Expanded bucket capacity. |
| `remaining_hours` | Capacity not yet allocated in this iteration. |
| `allocations` | Slice ids consuming the bucket in allocation order. |
| `local_date` | Resource-local date for daily caps/costs. |
| `local_week` | Resource-local ISO week for weekly costs. |

All resource contention is resolved through this one global ledger. A process
cannot allocate capacity that another ready process already consumed in the
same iteration.

The ledger key deliberately does not include `role_id`. For multi-role
resources, every role allocation for the same resource and bucket draws down the
same `remaining_hours`. A correct scheduler result never has
`allocated_hours > capacity_hours + 0.0001` for any bucket; tiny residuals below
`0.0001` hours are rounded to zero in comparisons and output clamping.

### Ready Queue Ordering

The scheduler maintains a ready queue of requirements whose process dependency
predecessors are complete in the current iteration and whose process is allowed
by `blocked_policy`.

Queue sort key:

1. earliest dependency/resource-constrained start,
2. smaller dependency-only latest finish from CPM,
3. dependency topological index,
4. process id,
5. requirement id.

Eligible resources for a requirement are sorted by:

1. earliest bucket with remaining capacity at or after the requirement ready
   time,
2. lower projected cost for the next bucket,
3. resource id.

### Deterministic Fair Allocation

`split_allowed` uses deterministic water-filling within each bucket instead of
fixed percentage increments.

For one ready requirement and one bucket interval:

1. Build candidate resource buckets that can fill the requirement role, overlap
   the current bucket interval, have ledger `remaining_hours > 0.0001`, have
   daily cap residual above `0.0001`, and are at or after the requirement
   `ready_at`.
2. Sort candidates by the eligible resource sort key above, then by bucket
   `starts_at`, bucket `ends_at`, and `resource_id`. Select at most
   `required_resource_count` candidates.
3. For each selected candidate, compute headroom as the minimum of ledger
   `remaining_hours`, requirement daily cap residual for that resource local
   date, candidate bucket `capacity_hours`, and remaining requirement effort.
   Partial availability is already reflected by fractional `capacity_hours`.
4. Let bucket demand be the smaller of remaining requirement effort and total
   selected headroom. Raise all unfrozen selected candidates by the same
   allocation amount until either bucket demand is exhausted or one or more
   candidates hit their headroom. Freeze capped candidates, subtract their
   assigned effort, and redistribute the remaining demand evenly across the
   unfrozen candidates. Repeat until demand is exhausted or all candidates are
   frozen.
5. If a tie remains after equal water-fill arithmetic, assign the last
   sub-`0.0001` hour residual to the lowest sorted `resource_id` and clamp every
   bucket's final `remaining_hours` to zero when its absolute value is below
   `0.0001`.

This means identical eligible resources receive uniform allocations for the
same requirement and bucket. When one candidate is unavailable or capped, its
unused share is deterministically redistributed across the remaining candidates
without exceeding any candidate headroom.

`contiguous` does not use water-filling in v1 because it is single-resource.
The scheduler selects the first eligible resource by the same resource sort key
and searches that resource for one uninterrupted working sequence, excluding
non-working gaps.

### Iterative Convergence

Exact algorithm:

1. Compute CPM schedule and topological order from selected process revisions.
2. Expand calendars for the query horizon.
3. Initialize `previous_state_by_process` from CPM readiness, start, and finish
   times clipped to the horizon start when necessary.
4. For iteration `1..max_iterations`:
   - Clear the global contention ledger to full expanded capacity.
   - Set every process `ready_at` to the later of project start,
     `earliest_start_at`, dependency delay, and blocker policy effect when all
     dependency predecessors have non-null finishes. If any dependency
     predecessor has `ends_at = null`, set successor `ready_at = null`.
   - Repeatedly build the ready queue from unallocated requirements whose
     process dependencies have allocated finishes in this iteration.
   - Pop the first ready requirement and allocate effort over eligible resource
     buckets using the ledger, per-day caps, concurrency ceiling, allocation
     policy, and half-open intervals.
   - When all requirements for a process are fulfilled, set process finish to
     the max requirement finish. A process with no requirements uses its CPM
     finish after dependency/resource-constrained starts are applied.
   - Record unallocated requirements with structured reasons instead of
     dropping them.
   - Recompute dependent process ready times from predecessor finishes produced
     in this iteration.
   - After the queue is empty, compare the normalized process state with
     `previous_state_by_process`.
5. Converged when every comparable process `ready_at`, `starts_at`, and
   `ends_at` changes by at most `convergence_tolerance_hours`, allocation
   states match, sorted unallocated reasons match, and the allocation-slice
   fingerprint is stable.
6. If converged, emit the current slices and metadata.
7. If not converged, set `previous_state_by_process` to the current normalized
   state and continue.
8. If `max_iterations` is reached, emit the final iteration with
   `converged = false` and warning `max_iterations_reached`.

Comparable state:

- The normalized state is keyed by process id and contains `ready_at`,
  `starts_at`, `ends_at`, `allocation_state`, sorted unallocated requirement
  reasons, and an allocation fingerprint derived from final slice fields except
  `slice_id`, `iteration`, and `cost_amount`.
- The allocation fingerprint includes exactly `process_id`, `requirement_id`,
  `role_id`, `resource_id`, `starts_at`, `ends_at`, `effort_hours`,
  `capacity_hours`, and `cost_currency`, sorted by those fields in that order
  so ordering is deterministic and normalized independent of iteration.
  `cost_amount` is excluded because it is nullable, non-authoritative display
  evidence; cost queries recompute authoritative amounts from stable allocation
  fields, resource cost facts, and expanded calendar buckets.
- Null values are comparable values for `ready_at`, `starts_at`, and `ends_at`.
  Null-to-non-null and non-null-to-null transitions are always changes. Two
  null `ready_at` values compare equal only when `allocation_state` and sorted
  unallocated reasons also match. Two null finishes compare equal only when
  `allocation_state` and sorted unallocated reasons also match.
- Partial rows compare populated `ready_at`, the first allocated `starts_at`,
  and `ends_at = null`. Fully unallocated and blocked-zero-capacity rows whose
  dependencies are feasible compare `starts_at = null`, `ends_at = null`, and
  populated `ready_at`. Successor rows blocked by a predecessor with
  `ends_at = null` compare `ready_at = null`, `starts_at = null`, and
  `ends_at = null`.
- Reason changes such as `no_calendar_capacity` to `horizon_exhausted`, or a
  new `predecessor_unallocated` reason, prevent convergence even when finish
  values remain null.
- Allocation-slice stability is part of convergence because unchanged process
  finish times can still hide unstable resource assignment. The fingerprint uses
  the exact included fields above and intentionally ignores both computed
  `slice_id` and final output `iteration`.
- Reaching `max_iterations` is not a query validation error. The service returns
  `ok = true`, final iteration output, `converged = false`, and a wrapper
  warning `max_iterations_reached`; unstable requirements may also report
  `iteration_not_converged` in `unallocated_requirements`.

Allocation specifics:

- Allocation consumes the smallest supported granularity bucket or the remaining
  effort if smaller.
- A bucket can produce at most one slice per `(requirement_id, resource_id)`.
  Adjacent slices for the same process, requirement, role, resource, iteration,
  cost currency, and cost attribution basis are coalesced when the previous
  `ends_at` equals the next `starts_at` and no ledger, local-date cap, or
  blocked-policy boundary would be hidden by the merge.
- Dependency finish, requirement finish, and process finish are all the
  exclusive `ends_at` of the latest relevant slice/interval.
- Blocked processes with `exclude` are reported as unallocated with reason
  `blocked`, `allocation_state = "unallocated"`, and null `starts_at`/`ends_at`.
  `include_as_zero_capacity` keeps the blocked process visible but emits no
  capacity and no allocation slices for it. Its
  schedule row has `allocation_state = "blocked_zero_capacity"`, `ready_at`
  populated when dependency predecessors have non-null finishes, and
  `starts_at`/`ends_at` set to `null`. `include_normally` ignores blocker
  status for planning.
- For dependency propagation, any predecessor with `ends_at = null` is
  incomplete. Successors that depend on that predecessor do not allocate in the
  same result and report unallocated reason `predecessor_unallocated` with
  `ready_at = null`, `starts_at = null`, `ends_at = null`, and
  `first_feasible_starts_at = null`. This applies to ordinary unallocated
  predecessors, blocked predecessors under `exclude`, and blocked predecessors
  under `include_as_zero_capacity`.

Process duration recomputation:

- Resource schedule duration is recomputed from assigned allocation slices on
  every iteration. The engine does not stretch or shrink persisted process
  duration facts.
- Each requirement reaches 100% utilization when cumulative assigned slice
  `effort_hours` equals its `effort_hours` within `0.0001` hours. Assigned
  slices consume their selected resource bucket at 100% of the slice amount;
  partial availability appears as fractional bucket capacity and therefore
  slower effort burn.
- Multiple resources for the same role contribute additively through
  water-filled slices, bounded by `required_resource_count`, daily caps, and the
  shared resource ledger.
- Multiple role requirements on the same process progress concurrently when
  their resources are available. The process `starts_at` is the earliest slice
  start across all requirements, and the process `ends_at` is the latest
  requirement completion time. A process is complete only after every role
  requirement reaches 100%.
- If a single multi-role resource is assigned to more than one role requirement
  for the same process in the same bucket, those role slices still share that
  resource's one ledger bucket. The combined allocation cannot exceed the
  bucket capacity, so one role may extend the process duration by consuming
  capacity the other role could otherwise use.
- Successor `ready_at` values in the next queue build use the recomputed
  process finishes from the current iteration. Partial and unallocated
  processes keep `ends_at = null`, so successors have `ready_at = null` and do
  not enter the allocation queue.

### Resource-Aware Critical Path

`query_resource_schedule` returns process-level resource-aware criticality
alongside the schedule. A future dedicated `query_resource_critical_path` can
reuse the same payload.

The engine derives criticality after iterative resource-constrained finish
convergence. It uses final process `ready_at`, `starts_at`, `ends_at`, CPM
metadata, and persisted process dependency edges. Resource contention changes
process finish times through the global capacity ledger; the critical path does
not introduce synthetic resource or allocation edges.

User-facing semantics and limitations:

- `critical_path_process_ids` explains the dependency chain that gates the final
  resource-constrained finish after resource effects have changed process
  `ends_at` values. It is not a resource-contention explanation graph.
- "Process-level" means every item in the path is an authored process node.
  Resource calendars, resource holidays, allocation slices, and shared-capacity
  contention influence the selected process finish times, but they are not
  materialized as nodes in the returned critical path.
- Resource contention can make a process critical even when no predecessor
  gates it; in that case the path may contain only that delayed process.
- To explain which resource caused delay, users and agents must inspect
  `allocation_slices`, `resource_delay_hours`, utilization buckets, and
  unallocated requirements. v1 does not return "resource A delayed process B"
  edges or enumerate alternate near-critical resource-contention chains.

Criticality rules:

- The terminal process is the scheduled project finish, defined as the maximum
  non-null process finish in the resource schedule. If no process has a non-null
  finish, `critical_path_process_ids` is empty.
- v1 returns one canonical critical path, not all possible critical paths.
- If several processes share the latest finish within
  `convergence_tolerance_hours`, choose the smallest dependency topological
  index, then lexicographically smallest `process_id`.
- Backward traversal considers only persisted process dependency predecessors
  of the selected process. A predecessor gates the selected process when its
  resource-constrained finish is within `convergence_tolerance_hours` of the
  selected process `ready_at`.
- If several predecessors gate the selected process, choose by smaller slack,
  then dependency topological index, then lexicographically smallest
  `process_id`.
- Stop traversal when no dependency predecessor gates the selected process. A
  resource-delayed process with no gating predecessor is therefore a one-process
  critical path; its delay is still visible through `resource_delay_hours`.
- Output includes only `critical_path_process_ids`, ordered from earliest
  gating process to scheduled project finish.
- The existing dependency-only `query_critical_path` remains CPM-based unless a
  separate compatibility decision changes it.

### Infeasibility

The engine returns structured infeasibility when the stored graph is valid but
the plan cannot currently be scheduled:

| Reason | Meaning |
| --- | --- |
| `missing_role` | Requirement references no active role in projected input. |
| `no_eligible_resource` | No active resource can fill the required role. |
| `no_calendar_capacity` | Eligible resources have no capacity in horizon. |
| `blocked` | Blocked policy excludes the process. |
| `predecessor_unallocated` | A dependency predecessor has null `ends_at`, so this process cannot become ready. |
| `horizon_exhausted` | Horizon ends before remaining effort can be allocated. |
| `contiguous_window_unavailable` | Contiguous policy cannot be satisfied. |
| `iteration_not_converged` | Final iteration still changed beyond tolerance. |

Hard validation errors belong in service commands and queries. Engine
infeasibility belongs in query output.

## Command Vocabulary

Commands use the existing `CommandEnvelope`:

| Envelope field | Type | Rules |
| --- | --- | --- |
| `command_id` | UUID | Optional; generated when omitted. Used for idempotency. |
| `command` | discriminated object | Discriminator is `action`. Extra fields forbidden. |

Command field tables:

| Action | Required fields | Optional fields | Result `entity_ids` |
| --- | --- | --- | --- |
| `create_project` | existing required fields | `project_id`, `default_currency` default `USD` | `project_id` |
| `set_project_default_currency` | `project_id`, `default_currency` | none | `project_id` |
| `set_project_due_at` | `project_id`, `due_at`, `edit_at` | none | `project_id`, `due_history_event_id` |
| `clear_project_due_at` | `project_id`, `edit_at` | none | `project_id`, `due_history_event_id` |
| `set_process_status` | `project_id`, `process_id` or `process_symbol`, `status`, `edit_at` | `finished_at` nullable | `process_id`, `lifecycle_event_id` |
| `set_process_due_at` | `project_id`, `process_id` or `process_symbol`, `edit_at` | `due_at` nullable | `process_id`, `due_history_event_id` |
| `add_blocker` | `project_id`, `process_id` or `process_symbol`, `summary`, `created_at` | `blocker_id`, `details`, `severity` default `blocking` | `blocker_id` |
| `resolve_blocker` | `project_id`, `blocker_id`, `resolved_at` | `resolution` | `blocker_id` |
| `rename_process` | `project_id`, `process_id` or `process_symbol`, `new_symbol`, `edit_at` | `keep_old_symbol_as_alias` default `true` | `process_id` |
| `add_process_aliases` | `project_id`, `process_id` or `process_symbol`, `aliases`, `edit_at` | none | `process_id` |
| `batch_update_process_graph` | `project_id`, `edit_at`, `operations` | `idempotency_key` | affected process and edge ids |
| `replace_process_with_subgraph` | `project_id`, `edit_at`, `process_id` or `process_symbol`, `processes`, `dependencies`, `root_symbols`, `leaf_symbols` | `preserve_parent_symbol_as_alias` default `true`, `parent_alias_target_symbol` | created process, retired process, retirement event, and edge ids |
| `collapse_subgraph` | `project_id`, `edit_at`, `process_symbols`, `new_process` | `role_conflict_policy` default `reject` | replacement process, retired process, retirement event, requirement, and edge ids |
| `create_role` | `project_id`, `name` | `role_id` | `role_id` |
| `rename_role` | `project_id`, `role_id`, `name` | none | `role_id` |
| `deactivate_role` | `project_id`, `role_id` | `force` default `false` | `role_id` |
| `upsert_resource_calendar` | `project_id`, `name`, `timezone`, `weekly_windows` | `calendar_id`, `active` | `calendar_id` |
| `set_calendar_active` | `project_id`, `calendar_id`, `active` | `force` default `false` | `calendar_id` |
| `add_calendar_exception` | `project_id`, `calendar_id`, `starts_at`, `ends_at`, `capacity_hours` | `exception_id`, `reason` | `exception_id` |
| `remove_calendar_exception` | `project_id`, `calendar_id`, `exception_id` | none | `exception_id` |
| `upsert_resource` | `project_id`, `name`, `role_ids`, `calendar_id`, `available_from_at`, `cost_rate`, `cost_unit` | `resource_id`, `available_until_at`, `cost_currency`, `holidays`, `active` | `resource_id` |
| `set_resource_active` | `project_id`, `resource_id`, `active` | none | `resource_id` |
| `set_resource_roles` | `project_id`, `resource_id`, `role_ids` | none | `resource_id` |
| `set_resource_calendar` | `project_id`, `resource_id`, `calendar_id` | none | `resource_id` |
| `upsert_process_revision` | existing required fields | `role_requirements`; mutually exclusive with `required_roles` after transition | `process_id`, `revision_id` |

`role_requirements` command item:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `requirement_id` | string | no | Generated when omitted. |
| `role_id` | string | yes | Active role in same project. |
| `effort_hours` | number | yes | Finite and `> 0`. |
| `min_allocation_hours_per_day` | number | no | Finite and `>= 0`; must be `<= max_allocation_hours_per_day` when both are supplied. |
| `max_allocation_hours_per_day` | number | no | Finite and `> 0`. |
| `required_resource_count` | int | no | Default `1`; must be `> 0`. |
| `allocation_policy` | enum | no | Default `split_allowed`; `split_allowed` or `contiguous`. |

Example command:

```json
{
  "command": {
    "action": "upsert_resource",
    "project_id": "project-alpha",
    "resource_id": "resource-ada",
    "name": "Ada",
    "role_ids": ["role-engineer"],
    "calendar_id": "calendar-nyc",
    "available_from_at": "2026-05-13T09:00:00-04:00",
    "cost_rate": "125.00",
    "cost_unit": "hour",
    "cost_currency": "USD",
    "active": true
  }
}
```

Example command result:

```json
{
  "command_id": "00000000-0000-4000-8000-000000000001",
  "ok": true,
  "entity_ids": {
    "resource_id": "resource-ada"
  },
  "warnings": []
}
```

## Query Vocabulary

Queries use the existing `QueryEnvelope`:

| Envelope field | Type | Rules |
| --- | --- | --- |
| `query_id` | UUID | Optional; generated when omitted. |
| `query` | discriminated object | Discriminator is `action`. Extra fields forbidden. |

Query field tables:

| Action | Required fields | Optional fields | Data shape |
| --- | --- | --- | --- |
| `query_schedule` | `project_id`, `as_of`, `now` | `scope` | dependency-only CPM schedule graph |
| `query_critical_path` | `project_id`, `as_of`, `now` | `scope` | dependency-only critical path and slack |
| `query_process_graph` | `project_id`, `as_of`, `now` | `scope`, `include_resource_fields`, `horizon_starts_at`, `horizon_ends_at`, shared resource query options, `include_allocation_slices` | process graph nodes/edges with CPM and optional resource-aware fields |
| `query_blockers` | `project_id`, `as_of` | `process_ids`, `process_symbols`, `include_resolved` default `false` | blocker rows and blocked process ids |
| `query_due_date_history` | `project_id`, `as_of` | `scope`, `target_process_id`, `target_process_symbol`, `include_project_total` default `true` | due-date history events |
| `query_project_catalog` | `project_id` | none | current role, calendar, and resource catalogs |
| `query_resource_schedule` | `project_id`, `as_of`, `now`, `horizon_starts_at`, `horizon_ends_at` | `include_allocation_slices`, `planning_granularity`, `max_iterations`, `convergence_tolerance_hours`, `blocked_policy` | schedule rows, optional allocation slices, process criticality, convergence |
| `query_utilization` | `project_id`, `as_of`, `now`, `horizon_starts_at`, `horizon_ends_at` | `planning_granularity`, `max_iterations`, `convergence_tolerance_hours`, `blocked_policy` | utilization aggregates |
| `query_costs` | `project_id`, `as_of`, `now`, `horizon_starts_at`, `horizon_ends_at` | `scope`, `target_process_id`, `target_process_symbol`, `resource_ids`, `role_ids`, `planning_granularity`, `currency`, `group_by`, `max_iterations`, `convergence_tolerance_hours`, `blocked_policy` | cost aggregates |
| `query_resource_capacity` | `project_id`, `as_of`, `horizon_starts_at`, `horizon_ends_at` | `resource_ids`, `role_ids`, `planning_granularity` | expanded capacity buckets |
| `query_unallocated_requirements` | `project_id`, `as_of`, `now`, `horizon_starts_at`, `horizon_ends_at` | `planning_granularity`, `max_iterations`, `convergence_tolerance_hours`, `blocked_policy` | unallocated requirement list |

Exact output shapes are published first in `docs/agent_json_dsl.md`; this
section mirrors that contract.

`query_resource_schedule.data`:

| Field | Type |
| --- | --- |
| `project_id` | string |
| `as_of` | aware datetime string |
| `now` | aware datetime string |
| `horizon_starts_at` | aware datetime string |
| `horizon_ends_at` | aware datetime string |
| `planning_granularity` | enum |
| `processes` | list[ResourceScheduleRow] |
| `allocation_slices` | list[AllocationSlice], empty unless requested |
| `critical_path_process_ids` | list[string] |
| `unallocated_requirements` | list[UnallocatedRequirement] |
| `converged` | bool |
| `iteration_count` | int |
| `convergence` | `ConvergenceData` |

`ResourceScheduleRow`:

| Field | Type | Rules |
| --- | --- | --- |
| `process_id` | string | Required. |
| `name` | string | Required. |
| `ready_at` | aware datetime string or null | Earliest resource-aware start candidate when dependency predecessors have feasible non-null finishes; null when no predecessor-feasible ready time exists. |
| `starts_at` | aware datetime string or null | First allocated slice start; null when no capacity was allocated. |
| `ends_at` | aware datetime string or null | Completed process finish; null for partial or fully unallocated rows. |
| `dependency_only_starts_at` | aware datetime string | CPM start. |
| `dependency_only_ends_at` | aware datetime string | CPM finish. |
| `resource_delay_hours` | number | `0` when `ends_at` is null. |
| `allocation_state` | enum | `complete`, `partial`, `unallocated`, or `blocked_zero_capacity`. |
| `status` | string | PM/process status. |
| `finished_at` | aware datetime string or null | Lifecycle completion timestamp, distinct from computed schedule `ends_at`. |
| `requirement_ids` | list[string] | Requirements owned by the selected revision. |

For a complete row, `ready_at`, `starts_at`, and `ends_at` are all non-null and
`starts_at >= ready_at`. For a partial row, `ready_at` and `starts_at` are
non-null, `starts_at` is the earliest allocated slice start, and `ends_at` is
`null`. For a fully unallocated row whose dependencies are feasible, both
`starts_at` and `ends_at` are `null` and `ready_at` records the candidate
start. For a successor row blocked only by a predecessor with `ends_at = null`,
`ready_at`, `starts_at`, and `ends_at` are all `null`; the row's
unallocated requirement reason is `predecessor_unallocated`.

`ConvergenceData` contains `converged`, `iteration_count`, `max_iterations`,
`tolerance_hours`, `changed_process_ids`, `reason_changes`, and
`allocation_fingerprint_changed`. It is the structured evidence for the
normalized convergence comparison described in the scheduling algorithm.

`UnallocatedRequirement`:

| Field | Type |
| --- | --- |
| `project_id` | string |
| `process_id` | string |
| `requirement_id` | string |
| `role_id` | string |
| `reason` | enum: `missing_role`, `no_eligible_resource`, `no_calendar_capacity`, `blocked`, `predecessor_unallocated`, `horizon_exhausted`, `contiguous_window_unavailable`, `iteration_not_converged` |
| `message` | string |
| `remaining_effort_hours` | number |
| `allocated_effort_hours` | number |
| `eligible_resource_ids` | list[string] |
| `first_feasible_starts_at` | aware datetime string or null |

`first_feasible_starts_at` is the first resource bucket start that could have
accepted work after dependency readiness and resource eligibility are applied.
It is null when no such bucket exists, when blockers exclude the process from
allocation, or when the row is dependency-blocked by a predecessor with
`ends_at = null`. For `predecessor_unallocated`, both
`ResourceScheduleRow.ready_at` and `first_feasible_starts_at` are null.

`query_resource_capacity.data`:

| Field | Type |
| --- | --- |
| `project_id` | string |
| `as_of` | aware datetime string |
| `horizon_starts_at` | aware datetime string |
| `horizon_ends_at` | aware datetime string |
| `planning_granularity` | enum |
| `buckets` | list[CapacityBucket] |

`CapacityBucket`:

| Field | Type |
| --- | --- |
| `resource_id` | string |
| `calendar_id` | string |
| `starts_at` | aware datetime string |
| `ends_at` | aware datetime string |
| `capacity_hours` | number |
| `available_hours` | number |
| `allocated_hours` | number |
| `remaining_hours` | number |
| `role_ids` | list[string] |
| `local_date` | string |
| `local_week` | string |

Example schedule query:

```json
{
  "query": {
    "action": "query_resource_schedule",
    "project_id": "project-alpha",
    "as_of": "2026-05-13T12:00:00+00:00",
    "now": "2026-05-13T12:00:00+00:00",
    "horizon_starts_at": "2026-05-13T00:00:00+00:00",
    "horizon_ends_at": "2026-05-20T00:00:00+00:00",
    "include_allocation_slices": true,
    "planning_granularity": "hour",
    "max_iterations": 20,
    "convergence_tolerance_hours": 0,
    "blocked_policy": "exclude"
  }
}
```

Example schedule response:

```json
{
  "query_id": "00000000-0000-4000-8000-000000000101",
  "ok": true,
  "data": {
    "project_id": "project-alpha",
    "as_of": "2026-05-13T12:00:00+00:00",
    "now": "2026-05-13T12:00:00+00:00",
    "horizon_starts_at": "2026-05-13T00:00:00+00:00",
    "horizon_ends_at": "2026-05-20T00:00:00+00:00",
    "planning_granularity": "hour",
    "processes": [
      {
        "process_id": "process-api",
        "name": "Build API",
        "ready_at": "2026-05-13T13:00:00+00:00",
        "starts_at": "2026-05-13T13:00:00+00:00",
        "ends_at": "2026-05-14T17:00:00+00:00",
        "dependency_only_starts_at": "2026-05-13T13:00:00+00:00",
        "dependency_only_ends_at": "2026-05-13T21:00:00+00:00",
        "resource_delay_hours": 20,
        "allocation_state": "complete",
        "status": "planned",
        "finished_at": null,
        "requirement_ids": ["req-api-eng"]
      }
    ],
    "allocation_slices": [
      {
        "slice_id": "slice-001",
        "project_id": "project-alpha",
        "process_id": "process-api",
        "requirement_id": "req-api-eng",
        "role_id": "role-engineer",
        "resource_id": "resource-ada",
        "starts_at": "2026-05-13T13:00:00+00:00",
        "ends_at": "2026-05-13T21:00:00+00:00",
        "effort_hours": 8,
        "capacity_hours": 8,
        "cost_amount": null,
        "cost_currency": "USD",
        "iteration": 2
      }
    ],
    "critical_path_process_ids": ["process-api"],
    "unallocated_requirements": [],
    "converged": true,
    "iteration_count": 2,
    "convergence": {
      "converged": true,
      "iteration_count": 2,
      "max_iterations": 20,
      "tolerance_hours": 0,
      "changed_process_ids": [],
      "reason_changes": [],
      "allocation_fingerprint_changed": false
    }
  },
  "warnings": []
}
```

`query_utilization.data`:

| Field | Type |
| --- | --- |
| `project_id` | string |
| `as_of` | aware datetime string |
| `horizon_starts_at` | aware datetime string |
| `horizon_ends_at` | aware datetime string |
| `planning_granularity` | enum |
| `by_resource` | list[ResourceUtilization] |
| `by_role` | list[RoleUtilization] |
| `time_series` | list[UtilizationBucket] |
| `overallocated_buckets` | list[CapacityBucket], normally empty |

`ResourceUtilization` contains `resource_id`, `capacity_hours`,
`available_hours`, `allocated_hours`, `remaining_hours`, and
`utilization_ratio`. `RoleUtilization` contains `role_id`,
`demanded_effort_hours`, `fulfilled_effort_hours`, and
`unallocated_effort_hours`. `UtilizationBucket` contains `starts_at`,
`ends_at`, `resource_id`, `role_ids`, `capacity_hours`, `allocated_hours`, and
`utilization_ratio`.

`query_costs.data`:

| Field | Type |
| --- | --- |
| `project_id` | string |
| `as_of` | aware datetime string |
| `horizon_starts_at` | aware datetime string |
| `horizon_ends_at` | aware datetime string |
| `currency` | string |
| `total_cost` | string |
| `by_resource` | list[ResourceCost] |
| `by_process` | list[ProcessCost] |
| `by_role` | list[RoleCost] |
| `time_series` | list[CostBucket] |

Cost row shapes all include `currency` and `cost_amount`. `ResourceCost`
includes `resource_id`, `cost_unit`, and `allocated_hours`. `ProcessCost`
includes `process_id` and `allocated_hours`. `RoleCost` includes `role_id` and
`allocated_hours`. The `by_resource`, `by_process`, and `by_role` arrays are
one-dimensional projections populated only when their dimension appears in
`group_by`; omitted grouping lists are returned as empty lists and are not
multi-dimensional cross-product outputs.

`time_series` is populated only when `group_by` contains `time`. It is grouped
by the exact cross-product of requested non-time dimensions
(`resource`, `process`, `role`) plus each non-empty horizon bucket. The service
does not generate zero-cost Cartesian rows. `CostBucket` contains `starts_at`,
`ends_at`, nullable `resource_id`, nullable `process_id`, nullable `role_id`,
`allocated_hours`, `currency`, and `cost_amount`. Omitted dimensions are
serialized as `null`, not omitted and not as aggregate labels. Time buckets use
the same `planning_granularity` as scheduling, are clipped to the required
half-open horizon, and split allocation slices by elapsed overlap. `total_cost`
always reflects the filtered allocation set regardless of grouping.

`query_costs` filters first select an effective process scope, then run the
resource schedule for that scope and required half-open horizon, then filter
the final allocation slices by `resource_ids` and `role_ids`. `scope` supports
the same project, target-process, and topology-filter shapes as process
queries. `target_process_id` and `target_process_symbol` are compatibility
aliases for target-process scope and are mutually exclusive with `scope`.
Resource and role filters must be non-empty when supplied. Unknown ids are
validation errors; inactive role/resource ids owned by the project remain valid
filters because v1 deactivates rather than deletes facts. Inactive resources
produce no current capacity and normally contribute zero cost unless a later
resource-history projection explicitly says otherwise.

`query_costs` uses allocation slices as allocation evidence but ignores
`AllocationSlice.cost_amount`. Bucketed costs must sum, after decimal rounding
rules, to the matching filtered total for the same grouping. Hourly costs are
prorated by allocated hours in the bucket. Daily costs are prorated across
buckets for the resource local date by allocated hours on that date. Weekly
costs use the documented weekly denominator and are split across buckets by
allocated hours in the clipped local week. Fixed costs are charged once per
resource with any allocation in the filtered query range and prorated across
that resource's contributing buckets by allocated hours. If allocated resources
contributing to the query use more than one `cost_currency`, or any
contributing resource currency differs from the requested `currency`, the query
fails validation with a structured error `mixed_currency` before grouping. v1
does not convert currencies and does not downgrade this to a warning.

`query_unallocated_requirements.data` contains `project_id`, `as_of`,
`horizon_starts_at`, `horizon_ends_at`, `planning_granularity`, and
`unallocated_requirements`.

Example cost response fragment:

```json
{
  "project_id": "project-alpha",
  "as_of": "2026-05-13T12:00:00+00:00",
  "horizon_starts_at": "2026-05-13T00:00:00+00:00",
  "horizon_ends_at": "2026-05-20T00:00:00+00:00",
  "currency": "USD",
  "total_cost": "1000.00",
  "by_resource": [
    {
      "resource_id": "resource-ada",
      "cost_unit": "hour",
      "allocated_hours": 8,
      "currency": "USD",
      "cost_amount": "1000.00"
    }
  ],
  "by_process": [],
  "by_role": [],
  "time_series": []
}
```

Existing `query_schedule` remains dependency-only. Agents must be able to choose
between CPM schedule and resource-constrained schedule.

## LadybugDB Graph Rewrite

The durable graph adds project-scoped resource planning facts.

Node tables:

- `Project(project_id, name, start_at, default_currency, ...)`
- Existing `Process` projections include topology retirement fields
  `is_active`, `retired_at`, `retired_by_command_id`, and
  `retirement_reason`; lifecycle `status` remains separate.
- `ProcessRetirementEvent(retirement_event_id, project_id, process_id,
  retired_at, retired_by_command_id, retirement_reason,
  replacement_process_ids)`
- `Role(role_id, project_id, name, active)`
- `Resource(resource_id, project_id, name, calendar_id, available_from_at,
  available_until_at, cost_rate, cost_unit, cost_currency, active)`
- `ResourceCalendar(calendar_id, project_id, name, timezone, active)`
- `CalendarWeeklyWindow(window_id, calendar_id, weekday, start_local_time,
  end_local_time, capacity_hours)`
- `CalendarException(exception_id, calendar_id, starts_at, ends_at,
  capacity_hours, reason)`
- `RoleRequirement(requirement_id, revision_id, project_id, process_id,
  role_id, effort_hours, min_allocation_hours_per_day,
  max_allocation_hours_per_day, required_resource_count, allocation_policy)`

Relationship tables:

- `HAS_ROLE(FROM Project TO Role)`
- `HAS_RESOURCE(FROM Project TO Resource)`
- `HAS_CALENDAR(FROM Project TO ResourceCalendar)`
- `HAS_WINDOW(FROM ResourceCalendar TO CalendarWeeklyWindow)`
- `HAS_EXCEPTION(FROM ResourceCalendar TO CalendarException)`
- `CAN_FILL(FROM Resource TO Role)`
- `USES_CALENDAR(FROM Resource TO ResourceCalendar)`
- `REQUIRES_ROLE(FROM ProcessRevision TO RoleRequirement)`
- `REQUIREMENT_ROLE(FROM RoleRequirement TO Role)`
- Existing process dependency edges include active-interval fields equivalent to
  `is_active`, `retired_at`, `retired_by_command_id`, and `retirement_reason`
  so replace/collapse rewires can hide old edges from active graph projections
  without deleting historical edges.

Rewrite operations:

- Creating/updating roles, resources, calendars, windows, and exceptions mutates
  current resource-planning facts.
- Process planning remains append-only through `ProcessRevision`; each revision
  owns its own `RoleRequirement` nodes.
- Replacing a process revision's role requirements creates new requirement
  nodes for the new revision and never edits old requirement nodes.
- Deleting a role or calendar is a soft deactivation when referenced by
  historical revisions or resources.
- Resource capability changes are current facts for the resource. If historical
  auditability is required later, introduce `ResourceRevision`.
- Query projections choose the latest process revision as of `as_of` and the
  active process/edge graph as of `as_of`; retired processes remain visible by
  querying a historical `as_of` before their `retired_at`. Resource facts use
  the current resource graph unless a future resource-history feature is added.

## Validation Rules

Pydantic command/query validation:

- All moments are timezone-aware datetimes and use `*_at` names.
- Lifecycle statuses are limited to `planned`, `in_progress`, `paused`,
  `done`, and `canceled`; `blocked` is derived from blocker facts and rejected
  as stored status.
- Blocker severity is limited to `blocking`, `warning`, and `info`.
- Due-date mutations require timezone-aware `edit_at`; non-null `due_at` values
  must be timezone-aware.
- Project total due-date mutations use `set_project_due_at` and
  `clear_project_due_at`; derived process-total due values are query evidence,
  not implicit project due mutations.
- `set_process_status(status = "done")` may infer `finished_at = edit_at`;
  explicit lifecycle `finished_at` must be timezone-aware and no later than
  `edit_at`; non-done statuses reject new non-null `finished_at`.
- Calendar timezone is a valid IANA timezone name.
- Weekly windows use valid weekdays and non-overlapping local time ranges.
- Exception `ends_at` is after `starts_at`.
- Capacity, effort, costs, and tolerance values are finite and non-negative;
  effort requirements must be positive.
- Cost units, allocation policies, blocked policies, and planning granularities
  are enumerated.
- `bucket_size` is rejected; resource queries use `planning_granularity`.
- `min_allocation_hours_per_day <= max_allocation_hours_per_day` when both
  fields are supplied.
- Id fields are non-empty strings.
- Extra fields are rejected.
- Legacy `required_roles` validation follows `required_roles_transition_mode`.
- Process symbols and aliases are non-empty, project-scoped unique strings.
- Batch operation lists are non-empty and each operation uses its own
  discriminator with extra fields rejected.

Service graph validation:

- Referenced project, role, resource, calendar, process, and revision records
  must exist and belong to the same project.
- Active resources must have at least one active role and a valid active
  calendar.
- Process role requirements cannot reference inactive roles unless historical
  query projection is reading an old revision.
- Deactivation rules from lifecycle section are enforced.
- Process dependency cycle validation remains unchanged and must run with the
  candidate revision requirements.
- Batch and topology rewrite commands validate the final candidate graph once
  after all operation-local checks and before any facts are written.
- Topology rewrites preserve external input/output dependency semantics by
  root/leaf rewiring and enforce the explicit parent-symbol alias preservation
  rules.

Query validation:

- `as_of`, `now`, `horizon_starts_at`, and `horizon_ends_at` are
  timezone-aware.
- `horizon_ends_at` is after `horizon_starts_at`.
- Query horizon is bounded.
- `max_iterations` is positive and capped by service configuration.
- `convergence_tolerance_hours` is non-negative.
- `query_costs` validates process scope, optional resource and role filters,
  grouping options, and mixed-currency inputs instead of converting or warning.
- `query_process_graph`, `query_schedule`, `query_critical_path`,
  `query_blockers`, and `query_due_date_history` reject ambiguous process
  symbols/aliases and apply topology filters after selecting facts as of
  `as_of`.
- `query_process_graph.include_resource_fields = true` requires
  `horizon_starts_at` and `horizon_ends_at`; when false, resource horizon
  fields and `include_allocation_slices` are rejected.

## Acceptance Tests

Acceptance tests should be added before implementation and cover:

- JSON round trips for every new command and query envelope reject extra fields
  and naive datetimes.
- Stored process lifecycle status rejects `blocked`; blocked computed state is
  derived from unresolved `blocking` blockers.
- `query_blockers` returns blocked process ids and respects `include_resolved`
  as of a timezone-aware `as_of`.
- Work-now and late-risk windows derive from dependency-only ES/LS and explicit
  timezone-aware `now`, including zero-slack processes.
- Due-date history returns timezone-aware mutation events with
  `before_due_at`/`after_due_at` for whole-project, target-process, and
  topology-filtered scopes.
- Project total due-date tests cover explicit `set_project_due_at` and
  `clear_project_due_at`, query `current_project_due_at`, and separate
  `derived_project_due_at` history evidence.
- Lifecycle tests cover `set_process_status` inferred and explicit
  `finished_at`, reopen clearing behavior, cancel behavior, and the distinction
  between lifecycle `finished_at` and resource schedule `ends_at`.
- `query_process_graph` returns process nodes, persisted dependency edges, CPM
  dates, slack, blocker summaries, status fields, and critical labels without
  virtual allocation nodes or edges.
- Command idempotency rejects same `command_id` with different payload and
  accepts exact replay without duplicate facts.
- Batch dependency/role/resource mutations are atomic, idempotent on exact
  replay, validate in the documented order, and return structured cycle errors.
- Batch mutation results return one stable `operation_ids` result object per
  operation with operation index/id/discriminator, status, direct
  `revision_id`/`requirement_ids`/`edge_ids`/`alias_process_id`, categorized
  created/retired/removed/matched/candidate-only id maps, and no-op or
  validated-only reasons.
- Batch role-requirement mutations with multiple same-process operations append
  at most one coalesced process revision, apply operations in documented order
  against the candidate requirement set, cover add/remove cancellation and
  conflict cases, report final `revision_id`/`requirement_ids`, and replay
  without appending another revision.
- `replace_process_with_subgraph` preserves external dependency semantics by
  parent-to-root and leaf-to-child rewiring.
- `replace_process_with_subgraph` tests cover
  `preserve_parent_symbol_as_alias` default `true`, one-child target defaulting,
  required `parent_alias_target_symbol` for multi-child preservation, forbidden
  target symbols when preservation is false, `alias_process_id` result
  behavior, active identity resolution after `retired_at`, historical identity
  resolution before `retired_at`, exact replay, and alias collision no-write
  errors.
- After `replace_process_with_subgraph`, active graph queries at
  `as_of >= retired_at` omit the retired parent and retired parent edges, while a
  historical `as_of < retired_at` still returns the parent and original edges;
  lifecycle status is not rewritten to `canceled`.
- `collapse_subgraph` unions external inputs/outputs, rejects invalid
  selections, and conserves effort/legacy attention requirements using the
  inferred critical-path subgraph duration.
- After `collapse_subgraph`, active graph queries at `as_of >= retired_at` show
  the replacement process and rewired external edges only, while a historical
  `as_of < retired_at` still shows the selected original processes and internal
  edges.
- Collapse auto-merge tests cover same-role requirements with compatible
  identical `required_resource_count`, daily bounds, and allocation policy:
  effort hours are summed exactly and the identical count is copied.
- Collapse conflict tests cover same-role requirements with different
  `required_resource_count` values and assert a no-write
  `validation_error` / `collapse_role_requirement_conflict` unless explicit
  replacement requirements are supplied.
- Process renames and aliases preserve stable process ids, enforce unique
  symbols/aliases, and resolve aliases unambiguously in later commands.
- Role, resource, calendar, window, exception, and requirement uniqueness rules
  are enforced.
- Calendar expansion respects IANA timezones, DST spring-forward gaps,
  fall-back folds, dated exceptions, and half-open interval boundaries.
- Scheduler integration covers at least two resources using different IANA
  timezones with non-identical weekly schedules, proving that allocation,
  dependency propagation, and bucket output remain deterministic across local
  civil-time boundaries.
- Service rejects overlapping weekly windows, conflicting exceptions, invalid
  horizons, and cross-project role/resource/calendar references.
- A process revision with effort-hour role requirements projects to engine read
  models without mutating persisted process facts.
- Historical `as_of` process revisions keep their own role requirements.
- A shared resource delays lower-priority work and downstream dependencies
  through the global contention ledger.
- Ready queue tie breakers produce deterministic allocation slices
  across multiple eligible resources.
- Water-filling gives uniform same-bucket allocation to identical eligible
  resources for one requirement.
- Water-filling redistributes capacity after one candidate hits a daily cap,
  partial availability limit, or unavailable bucket.
- Multi-role resources share one capacity ledger across roles; role utilization
  does not multiply capacity by role count.
- No scheduler-produced capacity bucket exceeds 100% utilization:
  `allocated_hours <= capacity_hours + 0.0001`.
- Process duration is recomputed from assigned resource utilization to 100% for
  partial availability, multiple roles, and multiple resources per role.
- Computed `slice_id` values are stable for identical query inputs and change
  when material scheduling options or intervals change.
- Infeasible role requirements are reported as unallocated requirements with
  structured reasons.
- Successors of any predecessor with `ends_at = null` report
  `predecessor_unallocated` with `ready_at = null`, `starts_at = null`,
  `ends_at = null`, and `first_feasible_starts_at = null`.
- Iterative scheduling converges by normalized readiness/start/finish maps,
  allocation states, unallocated reason maps, and allocation-slice fingerprint
  stability.
- Iterative scheduling convergence covers null-to-non-null, non-null-to-null,
  and null-with-reason-change transitions for starts, finishes, readiness,
  allocation states, and allocation-slice fingerprints.
- Max-iteration cap tests assert `ok = true`, final iteration output,
  `converged = false`, wrapper warning `max_iterations_reached`, and
  `iteration_not_converged` unallocated reasons only where instability remains.
- Resource-aware critical path is process-level, derived after iterative finish
  convergence, and is distinct from dependency-only CPM when resources delay
  work.
- Utilization queries aggregate capacity, allocated, available, demanded,
  fulfilled, and unallocated hours by resource and role.
- Cost queries compute hourly, daily, weekly prorated, and fixed resource costs
  from the same allocation slices with deterministic decimal totals.
- Cost query tests cover process/project/topology scope, target process aliases,
  resource filters, role filters, grouping selection, inactive resource/role
  filters, horizon clipping, and structured mixed-currency errors.
- Cost grouping tests assert that `by_resource`, `by_process`, and `by_role`
  are one-dimensional projections, `time_series` is the cross-product of
  selected non-time dimensions plus non-empty time buckets, omitted
  `CostBucket` dimensions are `null`, zero Cartesian rows are not emitted, and
  bucketed hourly/daily/weekly/fixed costs sum to the filtered totals after
  rounding.
- `query_resource_capacity` returns expanded resource buckets clipped to
  resource availability and query horizon.
- LadybugDB bootstrap creates all new graph tables and relationships and
  remains idempotent.

## Out Of Scope

- Persisted baseline allocations.
- Resource history with append-only resource revisions.
- Optimized/constraint-solver scheduling.
- Probabilistic scheduling.
- UI rebuilds beyond calling the new service queries.
- Legacy JSON import/export.
