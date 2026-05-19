# SQLite Storage

ProjectDashboard can use either the original LadybugDB repository or the newer
SQLite repository. SQLite is the default for local app startup because it avoids
the Ladybug whole-snapshot graph rewrite and supports faster row-level commits.

## Startup

`./main.sh` sets:

```bash
PROJDASH_STORAGE="${PROJDASH_STORAGE:-sqlite}"
PROJDASH_DB_PATH="${PROJDASH_DB_PATH:-projdash.sqlite}"
```

Set `PROJDASH_STORAGE=ladybug` and `PROJDASH_DB_PATH=projdash.lbug` to run the
old backend.

## Migration

Bootstrap can copy an existing LadybugDB projection into SQLite:

```bash
python -m projdash.service.bootstrap --storage sqlite --db projdash.sqlite \
  --migrate-from-ladybug projdash.lbug
```

The migration reads the Ladybug projection, writes a SQLite database, copies the
command replay cache, and does not delete or modify the `.lbug` source. If a
non-empty SQLite target already exists, migration is skipped unless
`--force-migration` is used; forced migration backs up the existing target
first.

## Storage Shape

The SQLite repository keeps the service contract unchanged: commands still stage
changes in an `InMemoryProjectRepository` and commit through `replace_with`.
Committed state is stored as typed JSON rows in `repository_entity`, with
incremental upsert/delete of changed rows. The adapter preserves project facts,
process revisions, calendars, resources, blockers, schedule snapshots, command
replay records, and Slack configuration/token/run/outbox fields.
