# PM Flow Redesign Note

Captured on 2026-05-24 from developer direction.

The current ProjDash PM flow should be replaced, not incrementally tuned. Delete
the existing PM-flow skill content and replace it with a fresh approach. The
current 18 PM checklist points should be deleted. The theory-of-mind subsystem
should also be deleted.

The new flow should put more work into programmatically preparing simple
questions for the PM agent to answer before it crafts outbox messages. The
current agent has too much analytical work to do and runs too long.

Prepare a markdown context for the agent that describes the current project:

- milestones;
- processes, including dependency topology in `Parents`/`Children`;
- continuity note;
- evidence.

Use the process content shown under resource expanders as the basis for process
content, with additional fields shown in the example below.

```markdown
# Milestones
## first-day-live
- Terminal processes: C, D
- Optimal make span: 30 July, 2026 (100.4 days)
## reach-next-goal
- Terminal processes: J,P,T
- Optimal make span: 30 August, 2026 (131.4 days)

# Processes
Each process has exactly one process role. Mode `planned` means the process
role is unpinned and scheduler-planned. Mode `pinned` means a resource has
started the role and planning uses the pin start plus finish forecast or
verified finish.

## P3 | moc-live-small-test | Run 100-share live manual/MOC smoke test

Type: normal
Mode: pinned
Status: started
Role requirement: Trading Systems (role_trading_systems) | req-moc-live-test
Effort hours: 6 hours
Sensitivity: -1 hr
Definition: Done means a small 100-share live test validates live order sending
and the difference between certification and live behavior before production MOC
operation.
Parents: {execute-canadian-capital-contribution, itg-certification, itg-sor-setup}
Children: {one-symbol-moc-day}

Pinned to: Josh (res_balex_josh)
Pinned started: 2026-06-12 15:30 UTC (0 days ago)
Forecasted finish: 2026-06-18 16:00 UTC (in 6 days)

# Continuity note
...

# Evidence
Go through each line item and give a "Yes" or "No" if new evidence regarding
that line item is available (including affirmative negatives), followed by a
short reason and outcome and if you'll record updated evidence. For example:
"Yes, X is no longer pinned to this, so I will unpin them at the timestamp of
their message and update evidence.", or "No, user did not talk about blockers,
so I will not touch blockers or evidence", or "Yes, they explicitly said no new
blockers, so I will not touch blockers and will update evidence".

## Process Evidence
Staleness targets: P0 < 1 day, P1 < 3 days, P2 < 7 days, P3 < 14 days
where P0=planned with planned start in the past or pinned, P1=planned with
planned start > 0 days < 3 days, P2=planned with planned start > 3 days < 7
days, P3=planned with planned start > 7 days.

`blockers`: do we have accurate knowledge of things blocking the process's
normal completion as well as ensuring those blocker-type processes are getting
pinned with a timely forecasted resolution?
`done_criteria`: do we have accurate objective description of the process's
done criteria?
`plan_data`: do we have accurate knowledge of the role needed and effort hours
needed for complete execution, as well as the parents and children are correctly
identified?
`pin_data`: do we have accurate knowledge of whether someone is pinned working
on it, and if so when they started and their finish forecast, and once they
should be done, verification of when it was successfully completed?

A.plan_data last modified 10.5 days ago, last evidence that it's correct
2.4 days ago.
A.blockers ...
A.pin_data ...
A.done_criteria ...

## Resource Evidence
Staleness targets: < 7 days.

`understands_plan`: do they have accurate knowledge of that they should be
working on now and in the near future?
`complete_pin_communication`: have they communicated complete pin data for
things that have worked on previously as well as presently?
`complete_planning_communication`: have they completely communicated their input
for the project roadmap, so that our plan is the best it could be, not
introducing extra risk, and it's not out of date?
`slippage_risk`: have they been slow to start or complete things they should do?

Ada.understands_plan ...
...
```

Store evidence lists for both processes and resources in the database. Track
`last_modified` for the underlying value, such as when blockers were last
updated, and `last_evidence`, meaning when evidence about the correctness of
that value was last deduced from data.

The PM agent instructions should become:

1. You are a first-class project manager of a diverse team of programmers,
   lawyers, domain experts, and executives, and we are equipping you with tools
   to help you track and analyse teams and projects.
2. Carefully answer all evidence line items.
3. Have an independent sub-agent review your answers given the same context.
4. Use the service query to update the `last_evidence` of line items identified
   as "Yes".
5. Use the service query list to update aspects of the project based on the
   outcomes, including project topology and dependency structure, requirements,
   definitions, blockers, process-role pins, forecasts, and anything else
   that comes up.
6. Craft outbox messages to each team member, informing them of relevant things,
   including anything related to other team members that might impact them.
   Explicitly acknowledge things deduced. Inform each person of their main
   priority. List any of their process expander content that changed. Ask any
   questions needed. General structure: important changes based on last
   communications -> priorities -> questions -> structured information at the
   bottom.
7. Craft a team-channel message as needed to maintain cohesion and highlight
   important topics for shared discussion.
8. Update the continuity note as memory to optimize future PM work.
