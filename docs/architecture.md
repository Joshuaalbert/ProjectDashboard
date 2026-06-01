# ProjectDashboard Rewrite Architecture

ProjectDashboard is moving from a Streamlit app that owns JSON state into a
service-first package that agents and humans can both use.

## Boundaries

- `projdash.service` owns Pydantic command/query models, validation, command
  dispatch, and persistence adapters.
- `projdash.engine` owns pure calculations: graph projection, critical path,
  blocker followups, and resource analysis.
- `projdash.ui` owns Streamlit rendering and calls the service for all reads and
  writes.
- `old_code/` is reference material only.

## Durable Model

SQLite is the durable service store. Project facts are persisted as typed JSON
rows, while scheduling fields remain computed query outputs rather than
authoritative facts. The primary concepts are:

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

All service API and storage moments are timezone-aware datetimes. The API uses
`*_at` names such as `start_at`, `effective_at`, `as_of`, `now`, lifecycle
anchors, calendar exceptions, holidays, allocation slices, and schedule
snapshots.

## API Direction

Agents use typed Python models or JSON envelopes. Commands mutate facts and
queries compute projections. Batch commands are staged in memory and committed
through the repository in a single replacement step.

The first implementation slice includes a complete in-memory repository for
deterministic tests and a SQLite adapter for durable storage.
