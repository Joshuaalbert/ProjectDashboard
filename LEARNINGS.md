# Repository of learnings from agents

Add relevant things we learned about the project, and how to do certain things. If a preexisting "learning" becomes
contradictory, it must be updated to keep this files consistent. This file shall be read by agents to help them not
repeat mistakes specific to this project. Keep learnings compact.

---

- Slack teammate draft messages must be self-contained for people without
  ProjDash/dashboard access. Avoid internal scheduling/tool terms such as graph,
  LS/LF, ES/EF, slack, critical path, schedule snapshot, process id, role id, or
  blocker id; translate them into plain project context and requested action.
- Slack PM runs should start from `query_pm_markdown_context`, which prepares
  milestones, flat process context with `Parents`/`Children`, continuity, and
  evidence line items. Codex must answer every service-prepared evidence line
  item Yes/No before drafting teammate or team-channel messages.
- Slack continuity notes are now compact handoff notes, not theory-of-mind or
  checklist payloads. Keep durable memory, unresolved questions, message
  decisions, and next-run focus only.
- Process evidence freshness is line-item-specific. Updating blockers should
  not make done criteria, plan data, or pin data stale unless that underlying
  value changed.
- PM evidence review prioritizes stale evidence for current, blocked,
  late/active, or soon-starting processes first, then schedule/message-impacting
  attributes, while periodically refreshing further-out work.
- PM evidence freshness targets are priority-specific: P0 < 1 day old, P1 < 3
  days old, P2 < 7 days old, and P3 < 14 days old. P0 means pinned with status
  `started`, `early_start`, or `due`, or planned start < 3 days; P1 means
  planned start in >= 3 and < 7 days; P2 means planned start in >= 7 and < 14
  days; P3 means planned start >= 14 days.
- PM Slack runs must include both collected Slack messages and unreconciled
  manual notes as evidence; successful runs archive manual notes under
  `reconciled/manual_notes/<run_id>`, while failed runs leave originals
  unreconciled.
- PM Slack prompt evidence should not include past sent, failed, or skipped
  outbox history. Agents should see newly collected Slack messages,
  unreconciled manual notes, continuity, and unsent draft outbox rows only.
- PM markdown context prefixes stale evidence line items with `*`; the stale
  marker is service-rendered from line-item freshness rules.
- PM Slack runs that apply project updates must regenerate PM markdown context
  before drafting messages and inspect a unified diff against the original.
  They may take one corrective service-update/diff cycle when the first diff
  exposes a mistake or one more clear update.
- Slack UI runs are guarded by persisted `SlackRun` rows. If a run is active in
  storage but there is no in-process worker, treat it as interrupted/orphaned
  and provide a UI recovery path before starting a new run.
- Slack team-channel drafts are first-class outbox targets, not fake teammate
  DMs. Use them when shared alignment, a visible decision, or cross-team
  coordination is needed.
- PM Slack cadence compliance should be driven by service evidence, not
  memory. Due obligations are listed in `pm_communication_protocol.json`;
  accepted drafts must include `pm_evidence_claims`, and sent outbox ids become
  the auditable proof.
- Slack PM agents must return draft message content as `message_markdown` only.
  The runner derives the fallback/audit body and renders Slack Block Kit blocks
  programmatically.
- Process pins replace the old historical focus-window model. Because each
  process has exactly one role requirement, a pin records the resource,
  pinned-at time, and forecast finish for the process; process start is derived
  from pins, and a process becomes verified finished only after resource
  verification.
- Process pins may predate dependency readiness as head-start work. A process
  cannot become verified finished while any parent process is unfinished.
- Resource schedules treat active pins as fixed process completions and remove
  the pinned resource from plannable capacity until the latest active forecast
  finish, using `now` as the lower bound when a forecast is overdue.
- Selecting `mcts`/`alphazero` means the commitment-window planner owns the
  schedule and uses the heuristic only as its prior/rollout policy; it must not
  silently delegate the whole plan to the greedy backend.
- If a done process has no role requirement, no process pin, or a verified pin
  that does not cover the role-hour estimate, treat it as evidence debt. The
  warning should explicitly name the missing role requirement when present, then
  verify the pin or adjust effort hours to match the work actually verified.
- Slack PM runs must not turn role eligibility or scheduled role allocation into
  named teammate ownership. Use process-attribute evidence to verify status,
  schedule, estimates, done definition, dependencies, pins, forecasts, and
  blockers; ask neutral owner-confirmation questions when pin or direct teammate
  evidence does not confirm ownership.
- Slack PM drafts with process updates or complete assignment lists should use
  service-generated `message_artifact.message_markdown` from
  `query_pm_communication_protocol`; agents should not author Block Kit.
- Slack PM markdown context is a service contract: keep milestone
  terminal-process/make-span lines explicit, dependency topology in each
  process's `Parents`/`Children`, flat process fields with concise definitions,
  and evidence rows in the `<process>.<attribute> last modified ... last
  evidence ...` shape.
- Slack PM runs must review topology and assignment changes in the initial
  update pass, then again only if the context diff triggers the optional
  corrective cycle. Clear owner-specific work should use resource-specific exact
  roles, preferring `role_<resource_id>` and falling back to
  `role_<project>_<resource_id>` on cross-project ID collisions; use
  cross-cutting roles only for genuinely shareable or indeterminate ownership.
- Process graph completedness is derived as exactly one of `waiting`,
  `early_start`, `ready`, `started`, `due`, or `finished`. Do not expose a
  separate blocker state or encode blockers or lateness risk as completedness
  statuses; blockers are visible through blocker summaries and resolver
  dependencies.
- Do not persist process lifecycle state on `ProcessRecord`: no process-level
  status, started time, or finished time. Query-facing process state and
  start/finish times are derived from process pins and parent readiness.
- Blockers are single-concept items backed by `process_type="blocker"` resolver
  processes named `resolve-<blocker_name>`. Active blocker resolver dependencies
  must be maintained automatically and must not be removable while unresolved.
- Blockers are one-to-one with their referenced process. Do not share a blocker
  or blocker resolver dependency across multiple blocked processes.
- Blocker metadata is not a planning primitive. Planning should only see normal
  process mechanics: blocker resolver processes, dependency edges, roles,
  resources, calendars, and statuses.
- Use `delete_process` for true process removal. It hard-deletes the process,
  removes dependency references to it, and deletes a blocker/resolver only when
  the resolver process has no remaining child processes.
- PM context process sections are flat, one-role summaries: `Type`, `Mode`,
  `Status`, `Role requirement`, `Effort hours`, `Sensitivity`, `Definition`,
  `Parents`, and `Children`, followed by planned timing/assigned resource when
  unpinned or pin start/forecast/verified finish when pinned. Do not reintroduce
  nested process-role subsections or spent/remaining-hour fields.
