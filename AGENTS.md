# Agent Notes (ProjectDashboard)

- Read `LEARNINGS.md` before making changes.
- This repo is being rewritten from a root-level Streamlit prototype into a
  package that agents can use as a durable project-management tool.

## Current Repo Layout

- Legacy reference code: `old_code/`
  - Previous Streamlit app: `old_code/app.py`
  - Previous shell entrypoint: `old_code/main.sh`
  - Previous modules: `old_code/timelines/...`
- New package: `src/projdash/`
  - `service/`: Pydantic API, command/query handling, persistence adapters.
  - `engine/`: pure scheduling, critical-path, date, blocker, and resource logic.
  - `ui/`: Streamlit UI that calls the service instead of owning state.
- Tests: `tests/`
- Reusable development workflow: `instructions/agentic_development_process.md`
- Dependencies: `requirements.txt`
- Packaging: `pyproject.toml` with a `src/` layout.

## Run Things

Use the local conda environment:

```bash
conda run -n projdash_py ...
```

Common commands:

```bash
conda run -n projdash_py pip install -e .
conda run -n projdash_py pytest
conda run -n projdash_py ruff check .
conda run -n projdash_py python -m projdash.service.bootstrap --db projdash.lbug
```

The app entrypoint is:

```bash
./main.sh
```

`main.sh` initializes the durable LadybugDB service database and then runs
Streamlit.

## System Dependencies

Graph rendering requires Graphviz. On Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y graphviz libgraphviz-dev pkg-config
```

`libgraphviz-dev` is required for `pygraphviz` builds on arm64.

## Architecture Direction

- Durable storage is LadybugDB v1.
- The service supports multiple projects in one database.
- Project plan changes are append-only revisions.
- Agents should interact through Python service APIs or validated JSON
  command/query envelopes.
- The UI is a client of the service and should not mutate persistence directly.
- Keep legacy JSON import/export out of v1 unless explicitly reintroduced.

## Code Style Guidelines

- Imports: stdlib, third-party, local; one import per line; absolute package
  imports; no `*`.
- Formatting: 4 spaces; no tabs; wrap long lines around 88-100 chars.
- Types: use Python 3.10+ syntax and type public APIs/non-trivial internals.
- Naming: modules/functions/vars `snake_case`; classes `PascalCase`;
  constants `UPPER_SNAKE_CASE`.
- Public APIs should have concise Google style docstrings.
- Add comments only where they clarify non-obvious decisions or algorithms.
- Validation belongs in Pydantic models and service command handling.
- Raise specific exceptions with useful context; do not use bare `except`.
- All API and persistence timestamps must be timezone-aware `datetime` values,
  not bare dates or naive datetimes. Use `*_at` names for moments such as
  `start_at`, `effective_at`, `due_at`, `as_of`, and `now`.
- Keep scheduling/resource analysis pure and deterministic; pass `now`/`as_of`
  explicitly in tests instead of relying on wall-clock time.

## DRY Rule

Only extract a helper when the common code is non-trivial, more than five lines,
and the abstraction is clear. Prefer linearly readable code over premature
abstraction.

## Tests

- This is a test-first project.
- Add or update tests before implementing behavior.
- Use deterministic fixtures; isolate time, filesystem, and database state.
- Prefer small local fixtures or `NamedTuple` fixtures when test data needs a
  stable schema.
- For storage tests, use temporary LadybugDB databases.

## Agent Workflow

- For substantial work, follow `instructions/agentic_development_process.md`.
- Use sub-agents to draft designs/tickets and separate sub-agents to review
  them before implementation.
- Use implementation sub-agents for bounded write scopes and separate review
  sub-agents for cross-review.
- Do not edit or rewrite `old_code/` unless the user explicitly asks; it is a
  reference copy of the prior implementation.

## Repo Hygiene

- Do not commit secrets or local state databases.
- Do not add editor-specific settings unless asked.
- Keep generated build artifacts and local DB files out of PRs.
