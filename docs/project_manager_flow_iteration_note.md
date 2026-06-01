# Project Manager Flow Iteration Note

Date: 2026-05-22

This iteration extends the recurring Slack project-manager flow around team-level
communication, machine-readable continuity, blocker ownership, and slippage
history.

Required changes:

- Add a team-channel outbox target. Team-channel messages are not teammate DMs
  and must have their own outbox target and a team-level theory-of-mind entry.
- Add machine-readable follow-up fields for cadence, next follow-up time,
  do-not-message-before time, escalation time, and escalation channel.
- Extend theory of mind for each teammate and the team with commitments and
  outstanding items.
- Add required continuity sections for a commitment ledger, RAID register,
  outbound-message review, and cumulative outstanding items.
- Add blocker resolution ownership. Context generation should include the owner
  and derive who needs the blocker resolved from the blocked process and the
  blocker process's immediate successor processes.
- Add slippage timelines for the whole project and for each milestone. Each
  timeline entry is a pair of commit datetime and estimated done datetime.
- Do not add stakeholder support in this iteration; stakeholders remain internal
  teammates for now.
- Update the flow skill so every run checks which last outbound asks were not
  addressed by new inbound evidence and carries cumulative outstanding items in
  continuity.

The implementation should keep teammate and channel messages self-contained for
people who do not have access to ProjDash or its scheduling UI.
