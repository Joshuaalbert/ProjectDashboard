# Agentic Development Process

Use this instruction when extending ProjectDashboard from a fresh context. The
goal is to preserve a design-first, test-first workflow while using sub-agents
for implementation and independent review.

## 1. Ground The Work

1. Read `AGENTS.md`, `LEARNINGS.md`, and the relevant design docs.
2. Inspect the current code paths before asking questions.
3. Identify whether the objective touches service API, LadybugDB storage,
   scheduling engine, UI, tests, or docs.
4. State the intended slice and the files or modules that are likely in scope.

Do not start implementation until the objective, success criteria, and API
surface are clear enough for another engineer to implement.

## 2. Design First

For non-trivial changes, create or update a short design note before code:

- Describe the user/agent workflow.
- Define command/query models, DTOs, and validation behavior.
- Define storage graph changes and migration/bootstrap needs.
- Define engine behavior, edge cases, and failure modes.
- Define acceptance tests.

Use sub-agents for design drafting when the scope spans multiple subsystems.
Use different sub-agents to cross-review design docs. A design review should
look for ambiguous API vocabulary, missing validation, hidden mutable state,
time-dependent behavior, graph consistency issues, and weak tests.

## 3. Ticket The Work

Break the accepted design into bounded tickets. Each ticket must specify:

- Goal and success criteria.
- Owned files/modules.
- Public API or schema changes.
- Required tests.
- Explicit out-of-scope items.

Avoid tickets that mix unrelated storage, engine, and UI changes unless the
change cannot be tested otherwise.

## 4. Test First

Before implementing behavior:

1. Add failing tests for the intended service, engine, or UI-facing contract.
2. Keep tests deterministic by passing timezone-aware `as_of` and `now`
   datetimes explicitly.
3. Use temporary LadybugDB databases for storage tests.
4. Assert structured Pydantic validation errors for bad agent JSON.

Tests should exercise behavior, not implementation details. For agent APIs,
test both Python model usage and JSON round trips.

## 5. Implement With Sub-Agents

Use implementation sub-agents for bounded work that can be done in parallel.
When delegating:

- Give each sub-agent a disjoint write scope.
- Tell them they are not alone in the codebase.
- Tell them not to revert edits made by others.
- Ask them to list changed files and tests run.

Keep orchestration high level: integrate results, resolve conflicts, and ensure
the whole slice still matches the design.

## 6. Cross-Review

Use different sub-agents for review than the ones that implemented the code.
Reviewers should check:

- Correctness against the design and tests.
- Pydantic validation quality and error clarity.
- LadybugDB graph consistency and transaction boundaries.
- Deterministic scheduling behavior.
- Whether UI code is a thin service client.
- Whether abstractions obey the DRY rule.

Address review findings with focused patches and rerun relevant tests.

## 7. Close The Slice

Before finalizing:

- Run `conda run -n projdash_py pytest`.
- Run `conda run -n projdash_py ruff check .`.
- Update docs/tickets if the implementation made a deliberate design choice.
- Summarize changed files, tests, and remaining risks.

Do not claim the full rewrite is complete unless service, engine, storage, UI,
and review phases are all implemented and tested.
