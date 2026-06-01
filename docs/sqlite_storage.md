# SQLite Storage

ProjectDashboard uses SQLite for durable local service storage. The repository
keeps the service contract unchanged: commands stage changes in an
`InMemoryProjectRepository` and commit through `replace_with`, while committed
state is stored as typed JSON rows in `repository_entity`.

## Startup

`./main.sh` sets:

```bash
PROJDASH_STORAGE="${PROJDASH_STORAGE:-sqlite}"
PROJDASH_DB_PATH="${PROJDASH_DB_PATH:-projdash.sqlite}"
```

## Storage Shape

The adapter preserves project facts, process revisions, calendars, resources,
blockers, schedule snapshots, command replay records, and Slack
configuration/token/run/outbox fields.
