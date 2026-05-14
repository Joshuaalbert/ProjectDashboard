# Agent JSON DSL

The DSL uses explicit command and query envelopes so agents can validate JSON
before mutating project state. This file is the agent-facing contract for
resource scheduling commands and queries; design docs and tickets must not
introduce conflicting field names.

## Result Wrappers

Commands return either `CommandResult` or `CommandErrorResult`. The success
shape is:

| Field | Type | Rules |
| --- | --- | --- |
| `command_id` | string | Echoes or generates the envelope id. |
| `ok` | bool | `true` when the command was applied or idempotently replayed. |
| `entity_ids` | object | Created or affected ids keyed by entity name. |
| `warnings` | list[Warning] | The only authoritative warning location. |

Failed commands return:

| Field | Type | Rules |
| --- | --- | --- |
| `command_id` | string | Echoes or generates the envelope id when parseable; otherwise generated for tracing. |
| `ok` | bool | Always `false`. |
| `error` | `Error` | One structured error. |
| `warnings` | list[Warning] | Usually empty; never contains validation failures. |

Commands are atomic unless a command explicitly states that it is a no-op.
Validation errors, replay conflicts, graph cycles, and resource/currency
conflicts write no facts and return `ok = false`.

Queries return either `QueryResult` or `QueryErrorResult`. The success shape is:

| Field | Type | Rules |
| --- | --- | --- |
| `query_id` | string | Echoes or generates the envelope id. |
| `ok` | bool | `true` when data was produced. |
| `data` | object | Query-specific result payload. |
| `warnings` | list[Warning] | The only authoritative warning location. |

Failed queries return:

| Field | Type | Rules |
| --- | --- | --- |
| `query_id` | string | Echoes or generates the envelope id when parseable; otherwise generated for tracing. |
| `ok` | bool | Always `false`. |
| `error` | `Error` | One structured error. |
| `warnings` | list[Warning] | Usually empty; warnings are not a substitute for validation errors. |

`Warning` shape:

| Field | Type | Rules |
| --- | --- | --- |
| `code` | string | Stable machine code, for example `max_iterations_reached`. |
| `message` | string | Human-readable summary. |
| `severity` | enum | `info`, `warning`, or `error`. |
| `details` | object | Optional structured context; empty object when unused. |

`Error` shape:

| Field | Type | Rules |
| --- | --- | --- |
| `code` | string | Stable machine code, for example `validation_error`, `dependency_cycle`, `idempotency_conflict`, `not_found`, `ambiguous_process_symbol`, or `mixed_currency`. |
| `message` | string | Human-readable summary. |
| `details` | object | Error-specific structured context; empty object when unused. |
| `validation_errors` | list[ValidationError] | Present only for `code = "validation_error"`; omitted for other errors. |

`ValidationError` shape:

| Field | Type | Rules |
| --- | --- | --- |
| `loc` | list[string or int] | JSON path from the envelope root, for example `["command", "operations", 2, "role_id"]`. |
| `msg` | string | Human-readable validation failure. |
| `type` | string | Stable validation code, for example `missing`, `extra_forbidden`, `datetime_timezone`, `enum`, `greater_than`, or `project_scope`. |
| `input` | any | Optional rejected value when safe to echo. |
| `ctx` | object | Optional structured constraints such as `{"gt": 0}`; empty object when unused. |

Query `data` objects must not include `warnings`, `cost_warnings`, or other
warning aliases.

## Command Envelope

```json
{
  "command_id": "optional uuid",
  "command": {
    "action": "create_project",
    "project_id": "optional stable project id",
    "name": "Launch Plan",
    "start_at": "2026-05-13T09:00:00-04:00",
    "default_currency": "USD"
  }
}
```

Commands create or update authoritative facts. Extra fields are rejected.

Initial project/process actions:

- `create_project`
- `set_project_default_currency`
- `set_project_due_at`
- `clear_project_due_at`
- `upsert_process_revision`
- `set_process_status`
- `set_process_due_at`
- `add_blocker`
- `resolve_blocker`
- `rename_process`
- `add_process_aliases`
- `batch_update_process_graph`
- `replace_process_with_subgraph`
- `collapse_subgraph`

Process lifecycle and graph actions:

| Action | Required fields | Optional fields | Result `entity_ids` |
| --- | --- | --- | --- |
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

Stored process status is the explicit project-manager lifecycle:
`planned`, `in_progress`, `paused`, `done`, or `canceled`. `blocked` is not a
stored status in v1; it is derived from unresolved blocker facts. Computed
schedule status is returned separately and may be `not_ready`, `ready`,
`work_now`, `late_risk`, `blocked`, `complete`, `canceled`, `partial`,
`unallocated`, or `blocked_zero_capacity`, depending on the query shape.

All lifecycle, blocker, alias, topology, and due-date mutation commands require
a timezone-aware `edit_at`, `created_at`, or `resolved_at` timestamp. Naive
datetimes are rejected. `set_process_due_at.due_at` is nullable to clear a due
date; non-null values must be timezone-aware. Due-date history records include
the mutation timestamp plus `before_due_at` and `after_due_at` when the
mutation changed a due datetime.

Project total due dates are explicit mutable project facts in v1, not only a
derived maximum from process due dates. `set_project_due_at` stores a non-null
timezone-aware `due_at`; `clear_project_due_at` stores `after_due_at = null`.
`query_due_date_history.current_project_due_at` returns the explicit current
project due datetime. `project_total_events` includes explicit mutations with
`mutation_action = "set_project_due_at"` or `"clear_project_due_at"` and also
includes derived audit events with
`mutation_action = "derived_project_due_at_changed"` when process due-date
edits change the derived project-total due value. Derived events are audit
evidence only and do not overwrite the explicit project due fact.

`set_process_status` lifecycle finish semantics:

- `finished_at` is a lifecycle fact, distinct from resource schedule
  `ends_at`. It records when the project manager says work actually finished.
- Setting `status = "done"` without `finished_at` sets `finished_at = edit_at`.
- Setting `status = "done"` with explicit `finished_at` is allowed when
  `finished_at` is timezone-aware and `finished_at <= edit_at`.
- Setting `status` from `done` to any non-done status reopens the process and
  clears `finished_at` unless the requested status is `canceled`; canceling a
  done process preserves `finished_at` for audit.
- Setting `status = "canceled"` never infers `finished_at`; any supplied
  non-null `finished_at` is rejected unless the previous status was `done` and
  the command preserves the existing value.
- Setting non-done, non-canceled statuses rejects non-null `finished_at`.
- Exact replay is idempotent. A different `finished_at` for the same status
  change is a new command, not an operation-level no-op.

`Blocker` shape:

| Field | Type | Rules |
| --- | --- | --- |
| `blocker_id` | string | Project-scoped stable id. |
| `project_id` | string | Owning project. |
| `process_id` | string | Blocked process. |
| `process_symbol` | string | Current canonical symbol at query time. |
| `summary` | string | Non-empty human summary. |
| `details` | string or null | Optional longer context. |
| `severity` | enum | `blocking`, `warning`, or `info`; only `blocking` derives blocked state. |
| `created_at` | aware datetime string | Inclusive effective timestamp. |
| `resolved_at` | aware datetime string or null | Null means unresolved. |
| `resolution` | string or null | Required only by local policy, not by v1 schema. |

A blocker is unresolved as of a query when `created_at <= as_of` and
`resolved_at` is null or after `as_of`. A process is derived as blocked when it
is not stored as `done` or `canceled` and has at least one unresolved
`blocking` blocker. Resolving an already resolved blocker with the same
`resolved_at` and `resolution` is idempotent; resolving it with different
values is a replay conflict unless the command id is a different explicit edit.

`batch_update_process_graph.operations` is an ordered list of discriminated
objects. Supported operation actions are `add_dependency`, `remove_dependency`,
`add_role_requirement`, `remove_role_requirement`, `upsert_resource`,
`set_resource_roles`, and `set_resource_calendar`. The batch is atomic: the
service validates every referenced symbol/id, role/resource/calendar reference,
operation payload, and the final candidate dependency graph before writing any
fact. Validation order is payload shape, identity and alias resolution, local
operation consistency, resource/role/calendar rules, final graph cycle check,
then append. Exact command-id replay returns the original result; the same
command id with different payload is rejected. Operation-level idempotency is
permitted for already-present dependency adds and already-absent dependency
removes, but conflicting field values are errors.

Cycle errors use this shape:

```json
{
  "code": "dependency_cycle",
  "message": "Dependency update would create a process cycle.",
  "details": {
    "operation_index": 2,
    "edge": {"predecessor_symbol": "design", "successor_symbol": "ship"},
    "cycle_process_ids": ["process-design", "process-build", "process-design"],
    "cycle_process_symbols": ["design", "build", "design"]
  }
}
```

Batch operation result ids are grouped by plural keys in
`entity_ids`: `process_ids`, `edge_ids`, `requirement_ids`, and
`resource_ids`. Batches that append coalesced process revisions also include
`revision_ids`. Removed ids are still returned as affected ids. The service
also returns `operation_ids`, a list with one `BatchOperationResult` object per
operation in request order. Despite the historical field name,
`operation_ids` is not a list of strings.

`BatchOperationResult` has stable JSON keys:

| Field | Type | Rules |
| --- | --- | --- |
| `operation_index` | int | Zero-based index in `operations`. |
| `operation_id` | string | Caller-supplied operation id when present on the operation; otherwise generated for this result and stable on exact envelope replay. |
| `action` | string | Operation discriminator, for example `add_dependency`. |
| `status` | enum | `applied`, `no_op`, or `validated_only`. |
| `revision_id` | string or null | Final selected revision for same-process requirement operations; appended revision when persisted, original selected revision when final candidate is unchanged; null for non-requirement operations. |
| `requirement_ids` | list[string] | Operation-relevant requirement ids, including created, removed, matched, or candidate-only ids. |
| `edge_ids` | list[string] | Operation-relevant dependency edge ids, including created, removed, or matched ids. |
| `alias_process_id` | string or null | Present only for operations or future batch forms that assign an alias target; null otherwise. |
| `created_ids` | object | Stable plural arrays: `process_ids`, `edge_ids`, `requirement_ids`, `resource_ids`, `revision_ids`, `calendar_ids`, `blocker_ids`, `due_history_event_ids`, and `retirement_event_ids`; empty arrays when unused. |
| `retired_ids` | object | Stable plural arrays: `process_ids`, `edge_ids`, and `retirement_event_ids`; empty arrays when unused. |
| `removed_ids` | object | Stable plural arrays: `edge_ids`, `requirement_ids`, and `calendar_exception_ids`; empty arrays when unused. |
| `matched_ids` | object | Stable plural arrays: `process_ids`, `edge_ids`, `requirement_ids`, `resource_ids`, `calendar_ids`, and `revision_ids`; empty arrays when unused. |
| `candidate_only_ids` | object | Stable plural arrays for ids that existed only in the validated batch candidate and were not persisted; currently `requirement_ids`. |
| `no_op_reason` | string or null | Stable reason when `status = "no_op"`; null otherwise. |
| `validation_reason` | string or null | Stable reason when `status = "validated_only"`; null otherwise. |

`status = "applied"` means the operation contributed to a persisted fact in
the committed batch. `status = "no_op"` means the requested final state already
matched, or an idempotent remove targeted an already-absent dependency or
requirement by symbolic identity. `status = "validated_only"` means the
operation was valid and affected the batch-local candidate, but no persisted
fact in the final batch carries that operation's id change; the common case is
a generated requirement added and removed entirely within one batch.

Stable `no_op_reason` values are `dependency_already_present`,
`dependency_already_absent`, `requirement_already_present`,
`requirement_already_absent`, `resource_already_equivalent`,
`resource_roles_already_set`, `resource_calendar_already_set`, and
`final_candidate_unchanged`. Stable `validation_reason` values are
`candidate_add_then_remove`, `candidate_remove_then_readd`, and
`candidate_change_cancelled`. Validation failures are not represented inside a
successful `operation_ids` entry; a failed batch returns `ok = false` with the
shared `Error` shape and writes no facts.

Operation-level idempotency/replay is independent from envelope
`command_id` replay:

- Exact envelope replay by `command_id` returns the original result and writes
  nothing. For batch commands this means the stored `operation_ids` objects,
  generated `operation_id` values, candidate-only ids, and operation statuses
  are returned unchanged.
- Reusing a `command_id` with a different payload returns
  `code = "idempotency_conflict"` and writes nothing.
- A later command with a different `command_id` may contain operation-level
  no-ops documented below; no-op operations still appear in `operation_ids`.
- Operation-level idempotency never creates duplicate dependencies,
  requirements, resources, aliases, blockers, or due-date history events.
- Conflicting values for an existing explicit id are validation errors unless
  that operation is documented as a full replacement.

Topology command payload schemas:

`batch_update_process_graph`:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `action` | literal | yes | `batch_update_process_graph`. |
| `project_id` | string | yes | Owning project. |
| `edit_at` | aware datetime | yes | Mutation timestamp. |
| `operations` | list[BatchOperation] | yes | Non-empty ordered list; each item is discriminated by `action`. |
| `idempotency_key` | string | no | Optional caller key within this command payload; does not replace envelope `command_id`. |

Result ids include `operation_ids` plus any affected `process_ids`,
`edge_ids`, `requirement_ids`, `resource_ids`, and `revision_ids` when
coalesced requirement mutations append process revisions.

`replace_process_with_subgraph`:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `action` | literal | yes | `replace_process_with_subgraph`. |
| `project_id` | string | yes | Owning project. |
| `edit_at` | aware datetime | yes | Mutation timestamp. |
| `process_id` or `process_symbol` | string | yes | Exactly one active parent identity. |
| `processes` | list[SubgraphProcessCommand] | yes | Non-empty child process list. |
| `dependencies` | list[SubgraphDependencyCommand] | yes | Internal child dependencies; may be empty only when there is one child. |
| `root_symbols` | list[string] | yes | Non-empty supplied child symbols receiving external incoming edges. |
| `leaf_symbols` | list[string] | yes | Non-empty supplied child symbols feeding external outgoing edges. |
| `preserve_parent_symbol_as_alias` | bool | no | Default `true`. When true, the retired parent canonical symbol is assigned as an alias to one created child process. When false, the parent symbol is not preserved for active identity resolution. |
| `parent_alias_target_symbol` | string | conditional | Required when `preserve_parent_symbol_as_alias = true` and more than one child process is supplied; defaults to the only child when there is exactly one child; forbidden when `preserve_parent_symbol_as_alias = false`. Must name one supplied child `process_symbol`. |

Result ids include created child `process_ids`, retired parent
`retired_process_ids`, `retirement_event_ids`, created or matched active
`edge_ids`, `retired_edge_ids`, and `alias_process_id` when
`preserve_parent_symbol_as_alias = true`. `alias_process_id` is the active
child process id that receives the retired parent symbol as an alias; it is
omitted when preservation is false.

`collapse_subgraph`:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `action` | literal | yes | `collapse_subgraph`. |
| `project_id` | string | yes | Owning project. |
| `edit_at` | aware datetime | yes | Mutation timestamp. |
| `process_symbols` | list[string] | yes | Non-empty, unique after alias resolution, weakly connected active processes. |
| `new_process` | CollapseNewProcessCommand | yes | Replacement process shape below. |
| `role_conflict_policy` | enum | no | Default `reject`; only `reject` in v1 unless explicit replacement requirements are supplied. |

Result ids include replacement `process_id`, `retired_process_ids`,
`retirement_event_ids`, created or matched active `edge_ids`,
`retired_edge_ids`, and generated `requirement_ids`.

Every batch operation may include optional `operation_id`. It is unique within
one batch when supplied, echoed in the corresponding `operation_ids` entry, and
used only for caller correlation; idempotency still belongs to the envelope
`command_id` plus operation semantics below.

Batch operation payload schemas:

`add_dependency`:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `action` | literal | yes | `add_dependency`. |
| `predecessor_process_id` or `predecessor_process_symbol` | string | yes | Exactly one predecessor identity. |
| `successor_process_id` or `successor_process_symbol` | string | yes | Exactly one successor identity. |
| `dependency_type` | enum | no | Default `finish_to_start`; only value accepted in v1. |
| `edge_id` | string | no | Generated when omitted; if supplied, must be unique or match the same edge. |

Adding an already-present predecessor/successor edge with the same
`dependency_type` is a no-op and returns the existing `edge_id`. Reusing
`edge_id` for a different edge is an error.

`remove_dependency`:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `action` | literal | yes | `remove_dependency`. |
| `edge_id` | string | no | Either `edge_id` or predecessor/successor identities are required. |
| `predecessor_process_id` or `predecessor_process_symbol` | string | no | Required when `edge_id` is omitted. |
| `successor_process_id` or `successor_process_symbol` | string | no | Required when `edge_id` is omitted. |
| `dependency_type` | enum | no | Default `finish_to_start`; only value accepted in v1. |

Removing an already-absent edge is a no-op when the edge is identified by
predecessor/successor identities. Removing by an unknown `edge_id` is a
`not_found` error because ids are exact identities.

`add_role_requirement`:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `action` | literal | yes | `add_role_requirement`. |
| `process_id` or `process_symbol` | string | yes | Exactly one process identity. |
| `requirement` | RoleRequirementCommand | yes | Same shape and validation as `role_requirements` items. |

The operation appends a new process revision for the target process with the
added requirement. Supplying an existing `requirement_id` with identical fields
for the current revision is a no-op; different fields are an error.

`remove_role_requirement`:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `action` | literal | yes | `remove_role_requirement`. |
| `process_id` or `process_symbol` | string | yes | Exactly one process identity. |
| `requirement_id` | string | yes | Requirement on the current selected process revision. |

Removing an already-absent `requirement_id` from the current revision is a
no-op. Removing the last requirement is allowed; the process then has no
resource effort in resource scheduling.

When one `batch_update_process_graph` command contains multiple
`add_role_requirement` or `remove_role_requirement` operations for the same
process, the batch builds one in-memory candidate requirement set per process
and appends at most one new `ProcessRevision` for that process. Requirement
operations are evaluated in list order against the candidate set, seeded from
the current selected revision as of `edit_at`; they do not append one revision
per operation. A remove targets the candidate set after all earlier operations
for that process in the same batch, not the original revision alone.

No-op checks also use the candidate set. Adding a requirement whose
`requirement_id` is already present in the candidate with identical fields is a
matched no-op; the same id with different fields is a validation error.
Removing an id absent from the candidate is a no-op. Removing an existing
requirement records a batch-local tombstone; re-adding the same id later in the
same batch is allowed only when the fields are identical to the removed
requirement and cancels the removal. To change requirement fields, callers must
remove the old id and add a different `requirement_id`.

After every operation validates, each touched process candidate is compared
with its original selected revision. If the final requirement set is unchanged,
no process revision is appended. If it changed, the service appends exactly one
new process revision for that process containing the final requirement set.
The top-level `entity_ids.revision_ids` contains only appended revision ids.
Top-level `entity_ids.requirement_ids` contains persisted requirement ids that
were added, removed, or matched by requirement operations; a generated
requirement id that was added and removed entirely within the same batch is
reported only in that operation's `operation_ids` entry and is not persisted.
Every same-process requirement operation entry reports the same final
`revision_id` when a revision was appended, or the original selected
`revision_id` when the final candidate was unchanged. Exact envelope replay
returns the original revision and requirement ids without appending another
revision.

For same-process requirement operation results, `created_ids.requirement_ids`
contains ids newly persisted in an appended final revision,
`removed_ids.requirement_ids` contains ids present in the original selected
revision but absent from the final candidate, `matched_ids.requirement_ids`
contains ids that were already present with identical fields, and
`candidate_only_ids.requirement_ids` contains generated or supplied ids that
were added and removed before the final candidate was persisted. If the final
candidate equals the original selected revision, operations that only cancelled
within the candidate use `status = "validated_only"`; operations that matched
the original state without changing the candidate use `status = "no_op"`.

`upsert_resource`:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `action` | literal | yes | `upsert_resource`. |
| `resource` | UpsertResourceCommand | yes | Same fields and validation as top-level `upsert_resource` except `project_id` is inherited from the batch. |

This operation is idempotent for the same `resource_id` and equivalent fields.
Different fields for the same `resource_id` replace the current resource fact
only where top-level `upsert_resource` allows replacement; otherwise they are
validation errors.

`set_resource_roles`:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `action` | literal | yes | `set_resource_roles`. |
| `resource_id` | string | yes | Resource in the same project. |
| `role_ids` | list[string] | yes | Non-empty when the resource remains active; active roles only. |

Setting the same role list is a no-op. Duplicate `role_ids` are rejected.

`set_resource_calendar`:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `action` | literal | yes | `set_resource_calendar`. |
| `resource_id` | string | yes | Resource in the same project. |
| `calendar_id` | string | yes | Active calendar in the same project. |

Setting the same calendar is a no-op.

`replace_process_with_subgraph.processes` is a non-empty list of
`SubgraphProcessCommand` objects:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `process_symbol` | string | yes | New active canonical symbol unique in the final project graph. |
| `name` | string | yes | Non-empty display name. |
| `duration_hours` | number | yes | Finite and `>= 0`. |
| `earliest_start_at` | aware datetime or null | no | Nullable persisted constraint. |
| `due_at` | aware datetime or null | no | Nullable process due datetime. |
| `status` | enum | no | Default `planned`; cannot be `blocked`. |
| `finished_at` | aware datetime or null | no | Allowed only when `status = "done"` and `finished_at <= edit_at`; default `edit_at` when done and omitted. |
| `aliases` | list[string] | no | Unique aliases; must not collide with final active symbols or aliases. |
| `role_requirements` | list[RoleRequirementCommand] | no | Defaults to empty; validates like process revision requirements. |

`replace_process_with_subgraph.dependencies` is a list of
`SubgraphDependencyCommand` objects:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `predecessor_symbol` | string | yes | Must name one supplied child process. |
| `successor_symbol` | string | yes | Must name one supplied child process. |
| `dependency_type` | enum | no | Default `finish_to_start`; only value accepted in v1. |
| `edge_id` | string | no | Generated when omitted; unique within the final graph. |

`root_symbols` and `leaf_symbols` are non-empty lists of supplied child
symbols. Duplicate roots/leaves are rejected. A child may be both a root and a
leaf only when valid for the internal dependency graph.

`collapse_subgraph.new_process` has this exact shape:

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `process_symbol` | string | yes | New active canonical symbol unique outside the collapsed set and unique after aliases are applied. |
| `name` | string | yes | Non-empty display name. |
| `duration_hours` | number | no | Optional explicit replacement; if omitted, inferred from the dependency-only critical path through the collapsed set. |
| `earliest_start_at` | aware datetime or null | no | Defaults to earliest non-null selected constraint, or null if none. |
| `due_at` | aware datetime or null | no | Defaults to latest selected due datetime, or null if none. |
| `status` | enum | no | Default `planned`; cannot be `blocked`. |
| `finished_at` | aware datetime or null | no | Same lifecycle validation as `set_process_status`; usually null for collapsed planning nodes. |
| `aliases` | list[string] | no | Unique aliases; may include selected process symbols only when not ambiguous. |
| `role_requirements` | list[RoleRequirementCommand] | no | Explicit replacement requirements. When omitted, effort-hour requirements are summed by role subject to conflict rules below. |

`collapse_subgraph.process_symbols` must be non-empty, unique after alias
resolution, and weakly connected in the active dependency graph. The command
returns the replacement `process_id`, `retired_process_ids`,
`retirement_event_ids`, created or matched active `edge_ids`,
`retired_edge_ids`, and generated `requirement_ids`.

Topology rewrites use soft retirement instead of lifecycle cancellation. A
retired process keeps its lifecycle `status` and `finished_at` unchanged; status
continues to mean actual work state, not graph membership. The persisted process
projection includes `is_active`, `retired_at`, `retired_by_command_id`, and
`retirement_reason`, where `retired_at` is the rewrite command's timezone-aware
`edit_at`, `retired_by_command_id` is the envelope command id, and
`retirement_reason` is `replace_process_with_subgraph` or `collapse_subgraph`.
Active processes have `is_active = true` and null retirement fields. Dependency
edges retired by rewiring use the same active interval model and retain their
ids for historical query results.

`ProcessRetirementEvent` rows contain `retirement_event_id`, `project_id`,
`process_id`, `retired_at`, `retired_by_command_id`, `retirement_reason`, and
`replacement_process_ids`. For replace, `replacement_process_ids` is the list of
created child process ids; for collapse, it is the single replacement process
id. Command `entity_ids` expose the stable ids directly: replace returns
`process_ids`, `retired_process_ids`, `retirement_event_ids`, `edge_ids`,
`retired_edge_ids`, and optional `alias_process_id`; collapse returns
`process_id`, `retired_process_ids`, `retirement_event_ids`, `edge_ids`,
`retired_edge_ids`, and `requirement_ids`.

Active graph queries select processes and edges active as of `as_of`: a process
or edge is active when it existed at `as_of` and has no `retired_at` or has
`retired_at > as_of`. `query_schedule`, `query_critical_path`,
`query_process_graph`, resource schedules, and topology scopes return only that
active-as-of graph; v1 does not add an `include_retired` graph option. Historical
visibility is by querying an earlier `as_of`. A target-process scope or command
that names a retired process after its `retired_at` fails identity resolution
unless the command is an audit operation that explicitly permits retired ids,
such as `query_due_date_history` by `target_process_id` or resolving an existing
blocker by `blocker_id`.

Due-date history remains attached to the stable retired process id. A
target-process due-date history query by retired `process_id` is valid for
audit and returns that process's due events through `as_of`; `current_due_at` is
the latest due fact for that process as of the query cutoff. Whole-project and
topology-filtered `derived_project_due_at` exclude processes retired as of
`as_of`, while a historical `as_of` before retirement includes them. Retirement
does not write a due-date event and does not clear the retired process's due
fact.

Blockers attached to a retired process are retained for audit and can be
resolved by `blocker_id`, but after `retired_at` they do not contribute to
active `blocked_process_ids` or active graph `computed_status`. Commands that
set lifecycle status, process due dates, dependencies, aliases, or role
requirements require an active process identity as of the command `edit_at`.
Aliases resolve only to active processes as of the command/query timestamp.
When a process is retired, its canonical symbol and aliases stop resolving after
`retired_at` unless a rewrite explicitly assigns one of those strings as an
alias for a new active process; historical queries before `retired_at` still
resolve the retired process's historical symbols and aliases.

`replace_process_with_subgraph` retires the selected parent process from the
active graph and adds the supplied child processes and internal dependencies in
one atomic revision. Every external incoming edge to the parent is rewired to
each `root_symbols` child. Every external outgoing edge from the parent is
rewired from each `leaf_symbols` child. Roots and leaves must be supplied
children, child symbols must be unique, and the resulting graph must remain
acyclic. Parent-symbol alias preservation is deterministic: the command
defaults `preserve_parent_symbol_as_alias = true`; when true, the retired
parent canonical symbol becomes an alias for exactly one created child. With
one child, `parent_alias_target_symbol` defaults to that child. With multiple
children, `parent_alias_target_symbol` is required and must name one supplied
child `process_symbol`. When `preserve_parent_symbol_as_alias = false`,
`parent_alias_target_symbol` is forbidden and the parent symbol stops resolving
for active commands after `retired_at`. If preserving the alias would collide
with any final active canonical symbol or alias other than the retired parent's
own pre-rewrite identity, the command fails with no writes.

After a successful rewrite with preservation enabled, later active command and
query identity resolution treats the retired parent symbol as an alias of the
target child at timestamps `>= edit_at`; historical resolution before
`retired_at` still sees the retired parent. `alias_process_id` in the result is
the target child id. Exact command-id replay returns the original result,
including `alias_process_id`, and writes nothing. A later command with a
different `command_id` is not a replay: resolving the old parent symbol after
the rewrite targets the child when preservation is enabled, and fails active
process resolution when preservation is disabled.

`collapse_subgraph` replaces a non-empty, weakly connected process set with one
new process. External predecessors of any collapsed root become predecessors of
the new process. External successors of any collapsed leaf become successors of
the new process. Internal edges are removed. External input/output duplicate
edges are unioned. The new process symbol must not collide with any active
symbol or alias outside the collapsed set. The collapsed dependency-only
duration is the critical-path duration inferred within the subgraph. Effort-hour
role requirements are auto-merged by role only when their scheduling controls
are identical. For each role, compatible omitted-replacement requirements produce
one replacement requirement with `effort_hours = sum(effort_hours_i)`, the same
`min_allocation_hours_per_day`, the same `max_allocation_hours_per_day`, the
same `allocation_policy`, and the same `required_resource_count` as every
selected requirement for that role. `required_resource_count` is not averaged,
summed, maximized, or rounded; differing counts are a conflict because they
change the concurrency ceiling. Differing daily bounds or allocation policies
are the same class of conflict. A conflict rejects the command with
`ok = false`, `error.code = "validation_error"`, and a `ValidationError` whose
`type = "collapse_role_requirement_conflict"` and `ctx` identifies `role_id`,
the conflicting `field`, `values`, `process_ids`, and `requirement_ids`. The
caller must supply explicit replacement `role_requirements` to collapse such a
set. This preserves resource-time conservation by summing effort exactly and
prevents implicit changes to scheduling invariants.

Legacy attention/FTE requirements, when present during transition, are conserved
by weighted sum:
`collapsed_attention = sum(attention_i * duration_i) / subgraph_cp_duration`.
If the inferred duration is zero and any legacy attention is non-zero, the
command is rejected unless explicit replacement `role_requirements` are
provided. Legacy attention/FTE values do not derive
`required_resource_count`; once legacy values are mapped to effort-hour
requirements, the same compatibility rule above applies. Mixed legacy
attention/FTE and effort-hour requirements for the same role are rejected unless
explicit replacement `role_requirements` are provided.

Process symbols are unique among active processes and aliases in one project.
`rename_process` changes the canonical symbol while preserving `process_id`.
When `keep_old_symbol_as_alias = true`, the old symbol resolves to the same
process. `add_process_aliases` adds additional unique aliases. Alias resolution
is exact, project-scoped, and fails on ambiguity; commands that accept
`process_symbol` resolve aliases before validation and persist ids, not aliases.

Resource planning actions:

| Action | Required fields | Optional fields | Result `entity_ids` |
| --- | --- | --- | --- |
| `create_role` | `project_id`, `name` | `role_id` | `role_id` |
| `rename_role` | `project_id`, `role_id`, `name` | none | `role_id` |
| `deactivate_role` | `project_id`, `role_id` | `force` default `false` | `role_id` |
| `upsert_resource_calendar` | `project_id`, `name`, `timezone`, `weekly_windows` | `calendar_id`, `active` default `true` | `calendar_id` |
| `set_calendar_active` | `project_id`, `calendar_id`, `active` | `force` default `false` | `calendar_id` |
| `add_calendar_exception` | `project_id`, `calendar_id`, `starts_at`, `ends_at`, `capacity_hours` | `exception_id`, `reason` | `exception_id` |
| `remove_calendar_exception` | `project_id`, `calendar_id`, `exception_id` | none | `exception_id` |
| `upsert_resource` | `project_id`, `name`, `role_ids`, `calendar_id`, `available_from_at`, `cost_rate`, `cost_unit` | `resource_id`, `available_until_at`, `cost_currency`, `holidays`, `active` default `true` | `resource_id` |
| `set_resource_active` | `project_id`, `resource_id`, `active` | none | `resource_id` |
| `set_resource_roles` | `project_id`, `resource_id`, `role_ids` | none | `resource_id` |
| `set_resource_calendar` | `project_id`, `resource_id`, `calendar_id` | none | `resource_id` |

`create_project.project_id` is optional; when omitted, the service generates an
id. UI bootstrapping and agents that need stable references should provide it.
`create_project.default_currency` is optional and defaults to `USD`.
`set_project_default_currency` requires `project_id` and `default_currency`.
Currencies are ISO 4217 codes.

`set_calendar_active(active = false)` requires `force = true` when active
resources use the calendar. Forced deactivation preserves the calendar fact and
makes those resources unschedulable. `upsert_resource_calendar(active = false)`
is allowed only for new or otherwise unreferenced calendars and must not bypass
the same force rule.

Calendar weekly window:

| Field | Type | Rules |
| --- | --- | --- |
| `window_id` | string | Optional; generated when omitted. |
| `weekday` | int | `0` Monday through `6` Sunday. |
| `start_local_time` | string | `HH:MM[:SS]`, inclusive. |
| `end_local_time` | string | Exclusive and after start. |
| `capacity_hours` | number | Finite and `>= 0`. |

Calendar exceptions replace recurring capacity only where the exception interval
intersects an existing weekly window. They do not create capacity outside
recurring windows in v1. Use another weekly window for planned recurring
capacity.

Resource holidays:

| Field | Type | Rules |
| --- | --- | --- |
| `holiday_id` | string | Optional; generated when omitted. Supplied ids must be unique within the resource. |
| `starts_at` | aware datetime | Inclusive. |
| `ends_at` | aware datetime | Exclusive and after `starts_at`. |
| `reason` | string | Optional. |

Holidays are resource-local zero-capacity intervals. Use timezone-aware
datetimes for full-day local holidays, for example local midnight to the next
local midnight. Holidays close only that resource's capacity after reusable
calendar windows and calendar exceptions are applied.

Process `role_requirements` item:

| Field | Type | Rules |
| --- | --- | --- |
| `requirement_id` | string | Optional; generated when omitted. |
| `role_id` | string | Active role in the same project. |
| `effort_hours` | number | Finite and `> 0`. |
| `min_allocation_hours_per_day` | number | Optional, finite and `>= 0`. |
| `max_allocation_hours_per_day` | number | Optional, finite and `> 0`. |
| `required_resource_count` | int | Optional, default `1`, must be `> 0`. |
| `allocation_policy` | enum | Optional, default `split_allowed`; one of `split_allowed`, `contiguous`. |

When both daily bounds are supplied,
`min_allocation_hours_per_day <= max_allocation_hours_per_day`.
`contiguous` is single-resource in v1; `required_resource_count` still caps
concurrency for `split_allowed` but does not make a contiguous requirement use
multiple resources.

For `split_allowed`, the scheduler allocates same-bucket work with
deterministic water-filling. It selects at most `required_resource_count`
eligible resources by earliest capacity, lower projected cost, bucket times,
and `resource_id`; then it distributes bucket demand evenly until a resource
hits remaining bucket capacity, daily cap residual, partial availability, or
remaining effort. Capped or unavailable shares are redistributed across the
remaining selected resources. Comparisons and final clamping use a `0.0001`
hour tolerance.

Legacy `required_roles` transition is controlled by service configuration
`required_roles_transition_mode`:

| Mode | Behavior |
| --- | --- |
| `allow_legacy` | Accept `required_roles` or `role_requirements`, but not both. |
| `dual_write_warn` | Accept either shape and return a wrapper warning when `required_roles` is used. |
| `require_role_requirements` | Reject `required_roles`; require `role_requirements` for resource-aware revisions. |

## Query Envelope

```json
{
  "query_id": "optional uuid",
  "query": {
    "action": "query_schedule",
    "project_id": "project id",
    "as_of": "2026-05-13T09:00:00-04:00",
    "now": "2026-05-13T09:00:00-04:00"
  }
}
```

Initial dependency-only queries:

- `get_project`
- `query_schedule`
- `query_critical_path`
- `query_process_graph`
- `query_blockers`
- `query_due_date_history`
- `query_project_catalog`

Process query field tables:

| Action | Required fields | Optional fields | Data shape |
| --- | --- | --- | --- |
| `query_schedule` | `project_id`, `as_of`, `now` | `scope` | `DependencyScheduleData` |
| `query_critical_path` | `project_id`, `as_of`, `now` | `scope` | `CriticalPathData` |
| `query_process_graph` | `project_id`, `as_of`, `now` | `scope`, `include_resource_fields` default `false`, `horizon_starts_at`, `horizon_ends_at`, shared resource query options, `include_allocation_slices` default `false` | `ProcessGraphData` |
| `query_blockers` | `project_id`, `as_of` | `process_ids`, `process_symbols`, `include_resolved` default `false` | `BlockerData` |
| `query_due_date_history` | `project_id`, `as_of` | `scope`, `target_process_id`, `target_process_symbol`, `include_project_total` default `true` | `DueDateHistoryData` |
| `query_project_catalog` | `project_id` | none | `ProjectCatalogData` |

`query_project_catalog` returns the current role, calendar, and resource
catalogs for forms and agent-side lookup. It is intentionally not schedule
aware; use schedule/resource queries for time-varying projections.

`scope` is optional and defaults to the whole active process graph. Supported
values are `{"type": "project"}`,
`{"type": "target_process", "process_id": "..."}`,
`{"type": "target_process", "process_symbol": "..."}`, and
`{"type": "topo_filter", "root_process_symbols": [...], "direction":
"ancestors"|"descendants"|"ancestors_and_descendants"}`. Topology filters use
the dependency graph selected by `as_of` after alias resolution.

When `query_process_graph.include_resource_fields = true`, the request must
also provide `horizon_starts_at` and `horizon_ends_at` and may use the shared
resource query options. When it is false, resource horizon fields and
`include_allocation_slices` are rejected.

`DependencyScheduleData` contains `project_id`, `as_of`, `now`, `nodes`,
`edges`, and `critical_path_process_ids`. It is dependency-only CPM output.
Every node includes process `duration_hours`, `es_at`, `ef_at`, `ls_at`,
`lf_at`, `slack_hours`, `status`, `finished_at`, `computed_status`,
`blocker_summary`,
`work_now_window`, `late_risk_window`, `due_at`, and `criticality_label`.
`es_at`, `ef_at`, `ls_at`, and `lf_at` are timezone-aware datetimes.

`ProcessGraphData`:

| Field | Type | Rules |
| --- | --- | --- |
| `project_id` | string | Required. |
| `as_of` | aware datetime string | Revision cutoff. |
| `now` | aware datetime string | Status/reference time. |
| `schedule_basis` | enum | `dependency_only` or `resource_aware`. |
| `converged` | bool or null | Null for dependency-only output. |
| `nodes` | list[ProcessGraphNode] | Process nodes only. |
| `edges` | list[ProcessGraphEdge] | Persisted process dependencies only. |
| `critical_path_process_ids` | list[string] | Process ids only. |
| `allocation_slices` | list[AllocationSlice] | Empty unless resource-aware debug evidence is explicitly requested. |

`ProcessGraphNode`:

| Field | Type | Rules |
| --- | --- | --- |
| `process_id` | string | Stable id. |
| `process_symbol` | string | Canonical unique symbol. |
| `aliases` | list[string] | Unique aliases for this process. |
| `name` | string | Display name. |
| `duration_hours` | number | Dependency-only process duration. |
| `earliest_start_at` | aware datetime string or null | Persisted constraint, when present. |
| `due_at` | aware datetime string or null | Current due datetime. |
| `status` | enum | Stored PM status. |
| `finished_at` | aware datetime string or null | Lifecycle completion timestamp, distinct from scheduled/resource `ends_at`. |
| `computed_status` | enum | Derived status as of `now`. |
| `blocker_summary` | object | `unresolved_count`, `blocking_count`, `blocker_ids`. |
| `dependency_only` | object | `es_at`, `ef_at`, `ls_at`, `lf_at`, `slack_hours`, `criticality_label`. |
| `resource_aware` | object or null | Resource-converged starts/finishes, slack, delay, and allocation state when requested. |
| `work_now_window` | object | `starts_at`, `ends_at`, `active` based on dependency-only ES/LS. |
| `late_risk_window` | object | `starts_at`, `ends_at`, `active` based on dependency-only LS/LF. |

`ProcessGraphEdge` contains `edge_id`, `project_id`,
`predecessor_process_id`, `successor_process_id`, predecessor/successor
symbols, and `dependency_type = "finish_to_start"` in v1.

Dependency-only CPM fields are authoritative for `query_schedule`,
`query_critical_path`, and `ProcessGraphNode.dependency_only`. Resource-aware
fields are populated only by resource-aware queries after iterative convergence
or final non-converged iteration. Resource contention is reflected through
resource-aware process dates and delay fields; it must not appear as virtual
nodes or edges. Allocation slices may be returned only as optional
evidence/debug output and are never graph nodes or graph edges.

Work-now and late-risk windows use timezone-aware `as_of`/`now` comparisons
against dependency-only CPM dates. `work_now_window = [es_at, ls_at)` and is
active when the process is not done, canceled, or blocked and
`es_at <= now < ls_at`. `late_risk_window = [ls_at, lf_at)` and is active when
the process is not done or canceled and `now >= ls_at`; unresolved blocking
blockers keep `computed_status = "blocked"` while still exposing the late-risk
window. When `es_at == ls_at`, the process enters late risk immediately at
`ls_at`.

`BlockerData` contains `project_id`, `as_of`, `blockers`, and
`blocked_process_ids`. Each blocker uses the `Blocker` shape above plus
`is_resolved_as_of` and `is_blocking_as_of` booleans. By default resolved
blockers are omitted.

`DueDateHistoryData`:

| Field | Type | Rules |
| --- | --- | --- |
| `project_id` | string | Required. |
| `as_of` | aware datetime string | Inclusive history cutoff. |
| `scope` | object | Effective scope after defaults and alias resolution. |
| `target_process_id` | string or null | Populated for target-process scope. |
| `process_events` | list[DueDateHistoryEvent] | Events for scoped processes. |
| `project_total_events` | list[DueDateHistoryEvent] | Total-project due changes when requested. |
| `current_due_at` | aware datetime string or null | Current due for target scope when singular. |
| `current_project_due_at` | aware datetime string or null | Current explicit total-project due datetime. |
| `derived_project_due_at` | aware datetime string or null | Derived latest process due datetime for the effective scope. |

`DueDateHistoryEvent` contains `event_id`, `project_id`, `process_id` nullable
for total-project events, `process_symbol` nullable, `mutation_action`,
`edit_at`, `before_due_at`, `after_due_at`, and `command_id`. `edit_at`,
`before_due_at`, and `after_due_at` are timezone-aware when non-null.
For total-project scope, events include explicit project due-date edits and
derived total due changes caused by scoped process due-date mutations. For
`topo_filter` and `target_process` scopes, `process_events` includes only
processes selected by that scope; `project_total_events` still describes the
whole project unless `include_project_total = false`.

Resource planning queries:

| Action | Required fields | Optional fields | Data shape |
| --- | --- | --- | --- |
| `query_resource_schedule` | `project_id`, `as_of`, `now`, `horizon_starts_at`, `horizon_ends_at` | shared resource query options plus schedule output options | `ResourceScheduleData` |
| `query_utilization` | `project_id`, `as_of`, `now`, `horizon_starts_at`, `horizon_ends_at` | shared resource query options | `UtilizationData` |
| `query_costs` | `project_id`, `as_of`, `now`, `horizon_starts_at`, `horizon_ends_at` | shared resource query options plus `scope`, `target_process_id`, `target_process_symbol`, `resource_ids`, `role_ids`, `currency`, `group_by` | `CostData` |
| `query_resource_capacity` | `project_id`, `as_of`, `horizon_starts_at`, `horizon_ends_at` | `resource_ids`, `role_ids`, `planning_granularity` | `CapacityData` |
| `query_unallocated_requirements` | `project_id`, `as_of`, `now`, `horizon_starts_at`, `horizon_ends_at` | shared resource query options | `UnallocatedData` |

Shared resource query options:

| Field | Type | Default | Rules |
| --- | --- | --- | --- |
| `planning_granularity` | enum | `hour` | Only `hour` in v1. Replaces `bucket_size`; `bucket_size` is not accepted. |
| `max_iterations` | int | `20` | Positive and capped by service config. |
| `convergence_tolerance_hours` | number | `0` | Finite and `>= 0`. |
| `blocked_policy` | enum | `exclude` | `exclude`, `include_as_zero_capacity`, or `include_normally`. |

Schedule output options are accepted by `query_resource_schedule` and by
resource-aware `query_process_graph` when that query explicitly asks for
resource evidence:

| Field | Type | Default | Rules |
| --- | --- | --- | --- |
| `include_allocation_slices` | bool | `false` | Returns `allocation_slices` when true; false returns an empty list without changing schedule timing or critical path ids. |

`query_utilization`, `query_costs`, and `query_unallocated_requirements` may
compute allocation slices internally, but they must reject
`include_allocation_slices` as an extra field.

`query_costs` filter inputs:

| Field | Type | Default | Rules |
| --- | --- | --- | --- |
| `scope` | scope object | `{"type": "project"}` | Same scope shape as process queries. Limits contributing process allocations after topology filters are resolved as of `as_of`. |
| `target_process_id` | string | none | Deprecated alias for `scope = {"type": "target_process", "process_id": ...}`; mutually exclusive with `scope` and `target_process_symbol`. |
| `target_process_symbol` | string | none | Deprecated alias for target-process scope after alias resolution; mutually exclusive with `scope` and `target_process_id`. |
| `resource_ids` | list[string] | all resources | Non-empty when supplied. Filters contributing allocation slices to these resources. Unknown ids are `not_found`; inactive resources are allowed as filters and simply contribute no rows when they have no schedulable capacity. |
| `role_ids` | list[string] | all roles | Non-empty when supplied. Filters contributing allocation slices to these roles. Unknown ids are `not_found`; inactive roles are allowed as filters for historical identity and may contribute no current demand. |
| `currency` | ISO 4217 string | project `default_currency` | All contributing resource currencies must match. |
| `group_by` | list[enum] | `["resource", "process", "role", "time"]` | Subset of `resource`, `process`, `role`, `time`; omitted groupings return empty lists. |

Filtering is applied to final allocation slices after the resource schedule is
computed for the effective process scope and horizon. The time horizon is
always the required half-open `[horizon_starts_at, horizon_ends_at)` interval.
`resource_ids` and `role_ids` are filters, not grouping requests. Deactivated
roles/resources are not deleted in v1; their ids remain valid filters when the
project owns them. Because v1 has current resource facts rather than resource
history revisions, inactive resources have no capacity and normally contribute
zero cost unless a later historical snapshot feature changes the projection
contract.

`include_as_zero_capacity` keeps blocked processes in dependency propagation but
does not allocate slices. When their dependency predecessors have non-null
finishes, their resource schedule rows have
`allocation_state = "blocked_zero_capacity"`, `ready_at` populated, and
`starts_at`/`ends_at` set to `null`.

For dependency propagation, a predecessor with `ends_at = null` is incomplete.
With `blocked_policy = exclude`, a blocked predecessor is `unallocated` with
reason `blocked` and null `starts_at`/`ends_at`; successors that depend on it do
not allocate and report unallocated reason `predecessor_unallocated`. Those
successor rows have `ready_at = null`, `starts_at = null`, and `ends_at = null`
because no deterministic predecessor-feasible ready time exists. With
`blocked_policy = include_as_zero_capacity`, the blocked predecessor uses
`blocked_zero_capacity` and has the same downstream effect. With
`include_normally`, blockers do not affect allocation and successors use the
computed predecessor `ends_at`.

## Resource Output Shapes

`ResourceScheduleData`:

| Field | Type |
| --- | --- |
| `project_id` | string |
| `as_of` | aware datetime string |
| `now` | aware datetime string |
| `horizon_starts_at` | aware datetime string |
| `horizon_ends_at` | aware datetime string |
| `planning_granularity` | enum |
| `processes` | list[ResourceScheduleRow] |
| `allocation_slices` | list[AllocationSlice], empty unless `include_allocation_slices = true` |
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
| `finished_at` | aware datetime string or null | Lifecycle completion timestamp; not inferred from resource schedule `ends_at`. |
| `requirement_ids` | list[string] | Requirements owned by the selected revision. |

For a complete row, `ready_at`, `starts_at`, and `ends_at` are all non-null and
`starts_at >= ready_at`. For a partial row, `ready_at` and `starts_at` are
non-null, `starts_at` is the first allocated slice start, and `ends_at` is
`null`. For a fully unallocated row whose dependencies are feasible, `ready_at`
is non-null and both `starts_at` and `ends_at` are `null`. For a successor
blocked only by a predecessor with `ends_at = null`, all three fields are
`null` and the row reports `predecessor_unallocated` through
`unallocated_requirements`. `resource_delay_hours` is `0` whenever `ends_at` is
null.

`AllocationSlice`:

| Field | Type |
| --- | --- |
| `slice_id` | string |
| `project_id` | string |
| `process_id` | string |
| `requirement_id` | string |
| `role_id` | string |
| `resource_id` | string |
| `starts_at` | aware datetime string |
| `ends_at` | aware datetime string |
| `effort_hours` | number |
| `capacity_hours` | number |
| `cost_amount` | string or null |
| `cost_currency` | string or null |
| `iteration` | int |

`AllocationSlice` is a computed allocation record for debug, utilization, and
cost evidence. It is not a graph node and is not persisted in v1.
`AllocationSlice.cost_amount` is nullable and is not authoritative in v1.
`query_resource_schedule` may return `null`; `query_costs` ignores this field
and computes authoritative cost from allocated hours, resource cost facts, and
expanded calendar buckets. `cost_currency` is the resource currency when known
and `null` only when no resource currency can be attributed.

Adjacent fragments are coalesced only when process, requirement, role,
resource, iteration, cost currency, cost attribution basis, and timestamps are
continuous and the merge would not hide a ledger, local daily cap, or
blocked-policy boundary.

`query_resource_schedule` returns one canonical resource-aware critical path,
not all possible critical paths. The path is process-level only and is derived
after iterative resource-constrained finish convergence. Resource contention
affects each process `ends_at` through the capacity ledger; it is not represented
as graph edges. When several terminal processes finish at the same latest
`ends_at`, choose the one with the smallest dependency topological index, then
lexicographically smallest `process_id`. Backward traversal follows persisted
process dependency edges whose predecessor finish gates the selected process
ready time within `convergence_tolerance_hours`, choosing by smaller slack,
then dependency topological index, then lexicographically smallest
`process_id`. If a resource-delayed process has no gating predecessor, the path
contains that process only.

The critical path is a user-facing dependency-chain summary after resource
effects have changed process finish times. It does not explain resource
contention as edges, does not list alternate near-critical contention chains,
and should be paired with `allocation_slices`, `resource_delay_hours`,
utilization buckets, and unallocated requirements when users need to understand
which resource caused delay.

`ConvergenceData`:

| Field | Type | Rules |
| --- | --- | --- |
| `converged` | bool | Same value as top-level `converged`. |
| `iteration_count` | int | Final emitted iteration count. |
| `max_iterations` | int | Effective validated cap. |
| `tolerance_hours` | number | Effective finish/start/readiness tolerance. |
| `changed_process_ids` | list[string] | Processes that changed in the final comparison when not converged; empty when converged. |
| `reason_changes` | list[object] | Entries with `process_id`, `requirement_id` nullable, `before_reason` nullable, and `after_reason` nullable. |
| `allocation_fingerprint_changed` | bool | Whether final allocation-slice identity fields changed from the previous iteration. |

Convergence compares a normalized state for every selected process:
`ready_at`, `starts_at`, `ends_at`, `allocation_state`, sorted unallocated
requirement reasons, and an allocation fingerprint built from slice fields
except `slice_id`, `iteration`, and `cost_amount`. The allocation fingerprint
includes exactly `process_id`, `requirement_id`, `role_id`, `resource_id`,
`starts_at`, `ends_at`, `effort_hours`, `capacity_hours`, and
`cost_currency`, sorted by those fields in that order so ordering is
deterministic and normalized independent of iteration. `cost_amount` is
excluded because it is nullable, non-authoritative display evidence; cost
queries recompute authoritative amounts from stable allocation fields,
resource cost facts, and expanded calendar buckets. Null values are explicit
comparable states for all three datetime fields. A null-to-non-null or
non-null-to-null transition is always a change. Two rows with null `ready_at`
compare equal only when `allocation_state` and sorted unallocated reasons also
match. Two null finish values compare equal only when the process
`allocation_state` and sorted unallocated reasons also match. Reason changes,
ready/start changes beyond tolerance, and allocation fingerprint changes
prevent convergence even when finish maps are unchanged. Partial rows compare
`ready_at` and `starts_at` normally and `ends_at = null`. Fully unallocated and
blocked-zero-capacity rows whose dependencies are feasible compare both
`starts_at`/`ends_at` as null with populated `ready_at`; predecessor-blocked
successor rows compare `ready_at`, `starts_at`, and `ends_at` as null.

When `max_iterations` is reached, the query returns `ok = true` with the final
iteration result, `converged = false`, and a wrapper warning with
`code = "max_iterations_reached"` and `severity = "warning"`. The engine also
adds `iteration_not_converged` unallocated requirements only for requirements
whose remaining effort or dependency readiness is unstable in the final
comparison. The iteration cap is not a validation error unless
`max_iterations` itself is invalid.

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

`CapacityData`:

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

When a calendar interval is split into buckets, `capacity_hours` is distributed
proportionally by elapsed overlap after timezone conversion and clipping.
`available_hours` is the elapsed bucket duration, `allocated_hours` is consumed
capacity, and `remaining_hours = capacity_hours - allocated_hours`.

For a multi-role resource, this bucket capacity is shared across all `role_ids`;
it is not duplicated per role. Normal scheduler output must not exceed
`allocated_hours <= capacity_hours + 0.0001`.

Resource schedule process duration is recomputed from assigned allocation
slices each iteration. A role requirement is complete when assigned
`effort_hours` reaches 100% of the requirement within `0.0001` hours; partial
availability contributes fractional bucket capacity, multiple resources for one
role add their assigned slices, and multiple roles complete independently. A
process completes at the latest completed role requirement, and a multi-role
resource assigned to more than one role still consumes one shared resource
bucket across those roles.

`UtilizationData`:

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
| `overallocated_buckets` | list[CapacityBucket] |

`ResourceUtilization` contains `resource_id`, `capacity_hours`,
`available_hours`, `allocated_hours`, `remaining_hours`, and
`utilization_ratio`. `RoleUtilization` contains `role_id`,
`demanded_effort_hours`, `fulfilled_effort_hours`, and
`unallocated_effort_hours`. `UtilizationBucket` contains `starts_at`,
`ends_at`, `resource_id`, `role_ids`, `capacity_hours`, `allocated_hours`, and
`utilization_ratio`.

`CostData`:

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
one-dimensional breakdowns; they are populated only when their dimension is
present in `group_by`, and omitted grouping lists are returned as empty lists.
They are not multi-dimensional cross-product arrays.

`time_series` is populated only when `group_by` contains `time`. It is grouped
by the exact cross-product of requested non-time dimensions
(`resource`, `process`, `role`) plus each horizon bucket that receives
contributing allocation evidence. The service does not generate zero-cost
Cartesian rows. For example, `group_by = ["time"]` returns one row per
non-empty time bucket aggregated across all resources, processes, and roles;
`group_by = ["resource", "process", "time"]` returns one row per non-empty
`(resource_id, process_id, starts_at, ends_at)` key aggregated across roles.

`CostBucket` has stable keys: `starts_at`, `ends_at`, nullable `resource_id`,
nullable `process_id`, nullable `role_id`, `allocated_hours`, `currency`, and
`cost_amount`. Omitted dimensions are serialized as `null`, not omitted and not
as an aggregate label. `starts_at` and `ends_at` are always present
timezone-aware bucket bounds. Time buckets use the same `planning_granularity`
as scheduling, are clipped to the half-open requested horizon, and split
allocation slices by elapsed overlap. `total_cost` always reflects the filtered
allocation set regardless of grouping, and every populated breakdown is a
projection of that same filtered set.

Bucketed cost attribution must sum exactly, after decimal rounding rules, to
the matching filtered total for the same grouping. Hourly costs are prorated by
allocated hours in the bucket. Daily costs are prorated across buckets for the
resource local date by allocated hours on that date. Weekly costs use the same
weekly proration basis as totals and are split across buckets by allocated
hours in the clipped local week. Fixed costs are charged once per resource with
any allocation in the filtered query range and prorated across that resource's
contributing buckets by allocated hours. Mixed-currency validation happens
before grouping, so no `CostBucket` ever mixes currencies.

Costs are not converted in v1. `query_costs.currency` defaults to the project
`default_currency`. If allocated resources contributing to the cost query use
more than one `cost_currency`, or any contributing resource currency differs
from the requested currency, `query_costs` fails with `ok = false`,
`error.code = "mixed_currency"`, and `details.resource_currencies` keyed by
resource id; agents must query and aggregate each currency separately. Cost
queries do not use `AllocationSlice.cost_amount`.

`UnallocatedData`:

| Field | Type |
| --- | --- |
| `project_id` | string |
| `as_of` | aware datetime string |
| `horizon_starts_at` | aware datetime string |
| `horizon_ends_at` | aware datetime string |
| `planning_granularity` | enum |
| `unallocated_requirements` | list[UnallocatedRequirement] |

## Semantics

Explicit PM status and computed schedule status are separate. All moments must
be timezone-aware datetimes and use `*_at` field names. A due datetime can pass
without the project manager validating completion; the service should return a
follow-up state instead of silently marking the process done.

Existing `query_schedule` and `query_critical_path` stay dependency-only.
Resource-aware criticality is returned by `query_resource_schedule`.
