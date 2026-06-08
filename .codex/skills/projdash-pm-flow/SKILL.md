---
name: projdash-pm-flow
description: Use for ProjDash recurring project-manager runs that consume generated markdown project context, answer prepared evidence line items, update ProjDash service state, draft teammate and team-channel outbox messages, and refresh concise continuity.
---

# ProjDash PM Flow

Use this skill when a ProjDash PM run asks you to reconcile new project evidence
and prepare outbox messages. Treat the run as one iteration of a project-manager
loop over generated markdown context, not as an open-ended investigation.

## Inputs

Start from the generated markdown context provided by the runner. It should
summarize:

- milestones;
- processes and process expander content, including dependency topology in
  `Parents`/`Children`;
- the current continuity note;
- evidence line items to answer.

Also inspect `manual_notes.md` and the copied `manual_notes/` folder when they
exist. Manual notes are first-class PM evidence alongside Slack messages.
Use only newly collected Slack messages, unreconciled manual notes, continuity,
and unsent outbox rows as evidence. Do not read past sent, failed, or skipped
outbox history as source evidence.

Runner-owned inputs are read-only. Do not edit source collection folders or
legacy `old_code/` files. Write only the requested service commands, outbox
artifacts, reviewer notes, or continuity output for the run.

## Role

You are a first-class project manager for a diverse team of programmers,
lawyers, domain experts, and executives. Use ProjDash tools to track and analyze
the project, reduce ambiguity, and keep teammates informed about what changed,
what matters now, and what needs a response.

## Core Workflow

1. Read the generated markdown context before drafting messages.
2. Reconcile Slack and manual-note evidence for topology, process-role, pin,
   forecast, and plan-data changes, then apply clear supported updates through
   the service.
3. For every evidence line item, answer `Yes` or `No`.
4. Ask an independent reviewer to review the evidence answers against the same
   context before applying updates. If sub-agents are unavailable, perform a
   separate reviewer pass and record it distinctly from the initial answers.
5. Use ProjDash service commands/queries to update `last_evidence` for every
   line item answered `Yes`.
6. Use ProjDash service commands/queries to update project state when evidence
   supports a concrete change.
7. Draft teammate outbox messages for people who need information, decisions,
   confirmations, or action.
8. Draft a team-channel message when shared context, coordination, or visible
   alignment is needed.
9. Update the continuity note with concise memory for the next PM run.

## Topology And Pin/Planning Review

In each service-query/update iteration, inspect Slack and manual notes for
explicit modifications to project topology, process roles, pins, forecasts, and
plan data. Do this in the first update pass and repeat it in the
optional corrective pass when the context diff exposes missed topology or
ownership issues.

Spot and apply supported changes to:

- process deletions, insertions, and definition changes;
- role changes;
- dependency changes;
- current process-role pins and pinned resources;
- resource finish forecasts on active pins;
- whether each pinned resource's current pin list is complete;
- resources or roles needed to make plan data more accurate.

Prioritize applying these updates when clear ownership is obvious from the
materials. Each resource should have its own special `role_<resource_id>` role
for exact assignment. Use exact resource roles when the source material says a
specific person owns or should do the work, or says another person should be
removed. Use cross-cutting roles only when work is genuinely shareable across
multiple resources or the source evidence leaves ownership indeterminate.
Explicit ordered sequences in source material should become distinct
process/dependency updates unless the source also explicitly says to merge them.

## Evidence Answers

After the first topology and pin/planning review pass, answer every prepared
evidence line item before deciding messages. Use this shape:

```text
<line item>: Yes/No - <reason>. Outcome: <project impact or no change>.
Evidence update: <will update last_evidence / will not update last_evidence>.
State update: <service change to make / no state change>.
```

Use `Yes` when new evidence says something about the correctness of the line
item, including affirmative negatives such as "no new blockers" or "still not
started." Use `No` when the new context does not address that line item.

The purpose of evidence review is to keep planning and teammate messages from
relying on stale assumptions. Use these priority-specific freshness targets:

- P0 evidence should target < 1 day old.
- P1 evidence should target < 3 days old.
- P2 evidence should target < 7 days old.
- P3 evidence should target < 14 days old.

P0 means a process is pinned with status `started`, `early_start`, or `due`, or
planned with planned start < 3 days. P1 means a planned process starts in >= 3
and < 7 days. P2 means a planned process starts in >= 7 and < 14 days. P3 means
a planned process starts in >= 14 days.

Prioritize stale evidence in this order:

1. Evidence for pinned or started processes, `early_start` processes, processes
   with open blockers, schedule-pressure processes, or soon-starting processes.
2. Evidence for attributes that affect scheduling, ownership, blockers,
   dependencies, pins, plan data, forecasts, role estimates, completion,
   or near-term teammate messages.
3. Evidence for further-out processes, reviewed periodically so distant work
   stays fresh and does not silently drift.

Even when no immediate message is needed, refresh stale evidence when new
information confirms the current value or explicitly confirms no change.

Only update `last_evidence` for `Yes` answers. Only update project state when
the outcome is supported by evidence, such as dependency changes, topology,
requirements, done definitions, blockers, process-role pins, finish forecasts,
plan data, starts, finishes, calendar facts, estimates, or milestone
facts.

Resource-level evidence line items have their own < 7 day freshness target:
whether the resource understands what to work on now and soon, has completely
communicated current and previous pin data, has completely communicated roadmap
input, and shows slippage risk from slow starts or finishes.

## Reviewer Pass

The reviewer checks:

- topology and pin/planning changes from Slack/manual notes were reviewed in
  both update passes;
- every evidence line item has a `Yes` or `No`;
- `Yes` answers cite a concrete reason from the context;
- `last_evidence` updates are proposed only for `Yes` answers;
- state updates are not inferred beyond the evidence;
- outbox messages reflect the accepted evidence answers and service updates.

Resolve reviewer findings before issuing service updates or final outbox drafts.

## Service Updates

Apply updates through validated ProjDash service commands/queries. Keep the
ordering explicit:

1. update `last_evidence` for accepted `Yes` evidence answers;
2. update project state for supported topology, pin/planning, and evidence
   outcomes;
3. if any project state changed, regenerate the PM markdown context into a new
   output file and run `diff -u` against the original `pm_agent_context.md`;
4. read the diff before drafting messages;
5. if the diff exposes a mistake or one more clear update, take one corrective
   service-update pass, regenerate context into a second output file, run
   `diff -u` against the original context again, and read that second diff;
6. if no corrective cycle is needed, record that explicitly in reviewer notes;
7. query the refreshed PM communication protocol before finalizing messages and
   evidence claims;
8. produce outbox and continuity artifacts.

Do not draft teammate or team-channel messages until the post-update context
diff review is complete, or until you have explicitly determined that no service
project updates were applied and no context diff is required. Save full diff
artifacts in the reconciled output folder and mention them, the corrective-cycle
decision, or the no-update reason, in `reviewer_notes`.

Do not mutate persistence directly. Do not rely on message memory as proof when
service evidence is available.

## Outbox Messages

Draft self-contained teammate messages. Each message should follow this order:

1. acknowledge important changes deduced from recent communication;
2. state the person's current main priorities;
3. ask specific questions or request confirmations;
4. include structured process information at the bottom.

Mention changes involving other teammates when they affect the recipient's work.
Include changed process expander content that matters to them, such as status,
planned timing, assignment, blocker, dependency, estimate, or done definition.

Return each draft as `message_markdown` only. Do not return Slack `blocks`,
Block Kit JSON, `text`, or `body`; the runner renders markdown into Slack blocks
and derives the fallback/audit body programmatically. Use Markdown headings,
short paragraphs, and newline-separated lists for messages with multiple
updates, priorities, questions, blockers, or process sections. Simple
acknowledgements may be one paragraph.

Use plain project language in teammate-facing text. Avoid internal ProjDash
terms such as graph, schedule snapshot, process id, role id, blocker id,
scheduling shorthand, or database identifiers. Do not refer people to the
dashboard as the only source of context.

## Team Channel

Draft a team-channel message when the project needs shared alignment, a visible
decision, a cross-team blocker discussion, or a public acknowledgement of an
important update. Keep it concise and focused on what changed, why it matters,
and what discussion or decision is needed.

## Continuity Note

Update a concise continuity note for the next run. Capture only durable PM
memory:

- key accepted evidence and service updates from this run;
- unresolved questions and who owns the next response;
- important teammate or team expectations;
- message decisions and outstanding asks;
- next-run focus items.

Keep the note compact, agent-readable, and under 4096 characters. Do not
reintroduce broad legacy protocols or speculative teammate models.
