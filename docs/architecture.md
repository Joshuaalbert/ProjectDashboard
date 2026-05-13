# ProjectDashboard Rewrite Architecture

ProjectDashboard is moving from a Streamlit app that owns JSON state into a
service-first package that agents and humans can both use.

## Boundaries

- `projdash.service` owns Pydantic command/query models, validation, command
  dispatch, and persistence adapters.
- `projdash.engine` owns pure calculations: business days, graph projection,
  critical path, blocker/due-date followups, and resource analysis.
- `projdash.ui` owns Streamlit rendering and calls the service for all reads and
  writes.
- `old_code/` is reference material only.

## Durable Model

LadybugDB is the v1 durable store because projects are naturally graph-shaped.
The primary graph concepts are:

- `Project`
- `Process`
- `ProcessRevision`
- `Role`
- `Resource`
- `Blocker`
- `AssumptionNote`

Important relationships:

- project owns processes, roles, resources, blockers, and notes
- process has append-only revisions
- process depends on process
- process revision requires role
- resource can fill role
- blocker blocks process

Computed scheduling fields are query outputs, not authoritative facts.

All service API and storage moments are timezone-aware datetimes. The API uses
`*_at` names such as `start_at`, `effective_at`, `due_at`, `as_of`, and `now`.
The LadybugDB adapter stores ISO datetime strings so offsets are preserved.

## API Direction

Agents use typed Python models or JSON envelopes. Commands mutate facts and
queries compute projections. Batch commands should be transactional once the
LadybugDB repository is fully wired.

The first implementation slice includes a complete in-memory repository for
deterministic tests and a LadybugDB adapter boundary for durable storage.
