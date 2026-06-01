# Phase 1 Service Rewrite Tickets

## Ticket 1: Repo Instructions And Scaffold

- Fix `AGENTS.md`.
- Add `instructions/agentic_development_process.md`.
- Move prior app into `old_code/`.
- Add `pyproject.toml`, `src/projdash`, and `tests`.

## Ticket 2: Pydantic DSL Foundation

- Define command/query envelopes.
- Add JSON round-trip tests.
- Return structured command/query results.

## Ticket 3: Service Repository Boundary

- Add repository protocol.
- Implement deterministic in-memory repository.
- Add SQLite adapter bootstrap and schema boundary.

## Ticket 4: Engine Foundation

- Implement weekday-only calendar utilities.
- Implement schedule projection and critical path on read models.
- Test explicit status versus computed schedule status.

## Ticket 5: Streamlit Client Skeleton

- Add a thin UI entrypoint under `src/projdash/ui`.
- Keep UI writes behind service commands.
- Preserve full dashboard rebuild for a later phase.
