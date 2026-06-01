## The project manager agent flow

This describes the flow that the project manager agent performs repeatedly. It's the same flow over and over again, thus
it's one flow that is triggered. It is intented to increasingly asymptote to optimal project management.

## Logic steps

1. Analyse the current information.

The service is used to compute a context with calculated insights which the agent considers:

- What is the entire process graph state?

  ```
  Dependencies:
  A -> B
  B -> C, D
  E

  Processes:
  B | Name | State | Description | Started 10.4 days ago / Done 2 days ago | role_eng:5, role_ds:3 | blocker_A
  A | Name | State | Description | Started 15 days ago / Done 10.4 days ago | role_eng:8 | unblocked
  C | Name | State | Description | Started 1.5 days ago | role_eng:2, role_ds:6, role_qa:2 | blocker_A, blocker_B
  D | Name | State | Description | Planned start in 2 days / Planned duration | role_eng:1, role_ds:1, role_qa:7 | unblocked
  E | Name | State | Description | Earliest Start in 5 days / Planned start in 5 days | role_eng:1, role_ds:1, role_qa:7 | unblocked

  Blockers:
  blocker_A | Name | State | Description
  blocker_B | Name | State | Description
  ```

- Which processes have been most recently added?
  We capture the parents and children of the process and the role requirements of each mentioned. We show the most
  recently added processes, since these likely only have rough estimates. We exclude completed processes (
  marked done).
  Example if process B was recented added at datetime X.
  ```
  D added on X (Y days ago)
  A added on Z (W days ago)
  ...
  ```

- Which top-3 processes are least recently modified?
  This is the same as the above but sorted by least recently modified.

- Which processes have the most schedule buffer?
  Buffer is the schedule window width minus actual planned elapsed duration.
  Critical path doesn't exist any more.

  ```
    E | Buffer: 4.2 days
    D | Buffer: 1.5 days
    A | Buffer: 0 days
    B | Buffer: 0 days
    ...
  ```

- For each process what is the sensitivity to each role?
  This is the derivative of the make span w.r.t. adding a role-hour to a process. So, for each process-role, add 1 hour,
  measure make span, report difference. Positive means it increases make span (longer project), negative means it
  decreases make span (shorter project). This is a measure of projected completion impact.
  ```
  A-role_eng: 0.5 days
  A-role_ds: 0.2 days
  B-role_eng: -0.3 days
  B-role_ds: 0.4 days
  ...
  ```

- Which processes are most likely to be started later than expected?
  Report all processes that should be started by now (planned start < now) but are not yet started.
  Report all process that will start next, who has a parent that was started late (or is not started but should be).

- What should/is each member be working on?
  For each team member, list the processes they are assigned to, ordered by priority. This list should match the
  per-resource schedule expanders: process name/symbol, status, planned start and finish, planned assignment role,
  active pins, finish forecasts, known blockers, and definition of done. The recurring flow tracks whether each member
  has received the complete current list and whether anything in the list has changed since the last outbound message.
  ```
  member_J:
    - Process A | Started 2 days ago, expected done in 3 days
    - Process B | Start overdue by 1 day, expected done in 5 days
  ...
  ```

- What blocker resolution ownership and immediate demand exists?
  For each open blocker, include the resolution owner when known and the
  processes immediately waiting on the blocked process. From those processes,
  derive the roles plus pinned or role-eligible resources that need the blocker
  resolved.

- What is the slippage timeline?
  For the whole project and each milestone, list entries of
  `(commit_datetime, estimated_done_datetime)` from committed schedule snapshots.
  Explaining why slippage happened is handled in continuity rather than
  generated programmatically for now.

- What PM communication obligations are due?
  The service computes protocol obligations from the current resource/process
  plan and persisted PM communication evidence. It lists due process updates
  before planned start, overdue check-ins, in-progress check-ins, full
  assignment-list reviews, message acknowledgments, and project-update notices.
  Sent Slack outbox rows are the proof mechanism: each satisfying message has
  `pm_evidence_claims`, and once sent the outbox id is recorded as evidence.

2. Fetch all new information from team members, and the collective team.

Each team member's DM is fetched since the last fetch cursor location.
The team channel is fetched since the last fetch cursor location. Let us refer to this channel as the
`team_project_management_channel`. All projects should define the name of this channel and invite the project manager
agent to it.

3. Load the previous continuity note.

4. Load the TOM (theory of mind) of each team member.
   This is a structured JSON capturing for each team member:

- Which process-roles they think they should be working on over the next 7 days.
- What blockers they need to resolve for each process-role.
- How much time they think they need to complete the process-roles.
- Which other team members they think are depending on them for their completion each process-role.
- How much they think each process-role affects the projected completion date.
- How likely do they think it is that they will need to extend the estimation for each process-role.

Each entry is a 3-tuple of our estimate of their inner mind thoughts, associated evidence referencing the information we
have collected about them, and our appraisal of their thoughts.

Example:

```json
{
  "member_J": {
    "A-role_eng": {
      "blockers_thought": [
        "blocker_A is blocking me because I need input from member_K to complete task A, but member_K is blocked by blocker_A and cannot provide the input until blocker_A is resolved.",
        "member_J explicitly discussed this with member_K in team_project_management_channel.",
        "I think member_J understands the blockers for A well."
      ],
      "time_estimate_thought": [
        "I think I need 5 more days to complete A.",
        "member_J said before that they need 10 days, and they started 5 days ago.",
        "I think member_J's time estimate for A is unreasonable, because they are always late."
      ],
      "dependency_thought": [
          "I think member_L is depending on me for A because they need the output of A to start their work on process C.",
          "member_L asked member_J for an update and said they are blocked until they finish.",
          "I think member_J understands the dependencies for A well, because they confirmed member_L's message."
      ],
      "sensitivity_thought": [
          "I think A-role_eng is not very critical to the project.",
          "member_A said they are also working on other non-critical items.",
          "I think member_J underestimates the sensitivity of A-role_eng, because they are not aware of how many other processes depend on A."
      ],
      "extension_thought": [
          "I think member_J is likely to need to extend their estimation for A-role_eng, because they have already extended it twice and they are still not done.",
          "member_J said in the team channel that they are struggling with a specific blocker for A.",
          "I think member_J is likely to need to extend their estimation for A-role_eng, because they are not making good progress and they are facing blockers."
      ]
    }
  }
}
```

5. Decide what processes information is stale, i.e. too long since any update on it.

6. Decide with aspects of TOM are most worrying/unacceptable.

7. Decide what assigned-process list changes each team member needs to receive.
   Every mapped internal teammate should receive a complete ordered list at least once. After that, send concise
   updates when items are added, removed, or changed in status, timing, blocker, role, assignment, or done definition.
   If an item is removed, explicitly say it is no longer needed from them. Tell teammates they can ask any time for a
   complete up-to-date list of their tasks and status.

8. Satisfy the programmatic PM communication protocol.
   Every obligation marked `due=true` in `pm_communication_protocol.json` must
   be covered by a draft message with matching `pm_evidence_claims`. A no-message
   decision cannot satisfy a due protocol obligation. Process-specific cadence
    obligations normally need both the cadence evidence type and
    `process_full_update`. Once sent, the outbox id is stored as proof for audit.

9. Verify process-role pins.
   Started state is derived from the first process-role pin, not from a free-standing status edit. The agent must check
   whether active pins, verified finished pins, and finish forecasts are consistent with new evidence. If a teammate says
   they switched tasks, unpin the prior process-role at the switch time and pin the new process-role with a forecast if
   the teammate supplied one. Done still requires a verified finished pin; planned hours running out is not proof of done.

10. Decide what we should say to the team as a whole.

11. Decide what we should say to each team member in their DMs.

12. Write new DMs to each team member, and a message to the team channel when
    shared context, coordination, visibility, or a channel request for context
    needs a response. The team channel has its own outbox target and its own team
    theory-of-mind entity; it is not represented as a teammate DM.
    Outbox rows store a complete plain-text `body` for fallback/audit and may
    also store Slack Block Kit `blocks` using `mrkdwn` for readable rendering.
    Slack does not render arbitrary HTML. Generated draft hashes cover both the
    fallback body and the visible block payload.
    For assigned-process messages, use the same content model as the UI resource
    expanders. When using blocks, group content into clear sections such as
    `New`, `Updated`, `Removed`, `Reminders`, or complete-list sections like
    `Active focus`, `Needs attention`, `Upcoming`, and `Later`.

13. Write new TOM's, updating the old, based on what you've sent them, and what you've observed.
    Each teammate TOM and the team TOM includes machine-readable follow-up
    fields for cadence, next follow-up, do-not-message-before, escalation time,
    and escalation channel. Each teammate TOM also includes machine-readable
    `assignment_list_state`: whether a complete current list was sent, previous
    and current content hashes, current assigned process symbols, added/updated/
    removed process symbols, and whether the teammate was told they can request
    a current task/status list any time. TOM also includes commitments and
    outstanding items.

14. Have a sub-agent review your deductions and messages.

15. Send the messages.

16. Write a new continuity note, based on the above, in your own words helping the agent with the above work next time.
    The continuity note must include:
    - one TOM entry for every mapped teammate;
    - one team TOM entry for the team entity and configured project channel;
    - answers to all 18 PM checklist points;
    - a commitment ledger;
    - a RAID register for risks, assumptions, issues, and dependencies;
    - an outbound-message review comparing the last outbound messages against
      new inbound evidence, including assignment-list review entries for each
      mapped teammate;
    - cumulative outstanding items carried forward until addressed.

Each run must determine what from the last outbound messages was not addressed.
Those unresolved asks remain in continuity as cumulative outstanding items.
People or organizations mentioned in evidence who are not mapped Slack
teammates are external resources by default when they participate in
process-roles or decisions. Mapped Slack teammates are internal resources. If a
mapping is removed, the resource becomes external; if a mapping is added, the
resource becomes internal.
