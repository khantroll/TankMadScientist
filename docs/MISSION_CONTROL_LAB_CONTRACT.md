# Mission Control ↔ Mad Scientist Lab Contract

**Status:** Draft for Jeffrey's review.
**Scope:** Architecture contract only. This pull request does not change application code and does not merge recovery PRs #1 or #2.
**After accept:** This file is the source of truth for Mission Control and Mad Scientist Lab. Until Jeffrey accepts it, or explicitly says to build, application work waits.

`RECOVERY_NOTES.md` stays the recovery provenance. `README.md` stays the operator guide for the recovered baseline. This contract names the boundary that later graph and Lab work has to satisfy. It uses the baseline's words where those words already exist.

| Contract term | Baseline name |
|---|---|
| Mission | `mad_scientist_missions` for a Lab-planned graph; a YAML template run for a human-configured workflow |
| Step | `mad_scientist_steps` |
| Dependency edge | `mad_scientist_step_dependencies` |
| Attempt | `mad_scientist_attempts`, mapped to an ordinary Tank run |
| Role / Goblin | an agent role (`agent_roles`): one specialist |
| Crew | a crew provider config or a GUI crew |
| Workspace memory | `workspace_memory` |
| Approval | **Approve & Apply** or Reject on a proposed patch |

`parent_run_id` on a run stays compatibility metadata. The dependency edges are the graph.

## 1. North star

Tank is an AI workflow compiler and runtime:

**Intent → DAG → Artifact.**

A human states an intent. Tank compiles that intent into a directed acyclic graph of work and produces artifacts (plans, patches, test results, distilled facts). Complexity lives inside Tank. The human is the principal and the approver. The human should not be the orchestrator who wires every hop by hand.

## 2. Two personalities

Both personalities coexist. Turning Lab off must leave Mission Control intact.

### Mission Control

Mission Control is the human-configured command center. People define agents, crews, and workflows. Tank executes what they configured: queue a run, launch a crew, start a mission from a template, fire a schedule.

Mission Control must remain useful with Lab off. Someone who never opens Lab can still point roles at a workspace, launch crews, and run hand-authored workflows through the same runner, logs, and approval gates.

In the recovered baseline this is the dashboard: workspaces, roles, providers, runs, schedules, git sync, and the YAML missions in `mission_templates.yaml` (`plan → build → test → fix-loop`).

### Mad Scientist Lab

Lab is the planner and the crew factory. The human gives a goal. Lab:

- plans the DAG
- selects, adapts, or creates specialists
- selects, adapts, or creates **whole crews** (Reuse → Adapt → Generate covers crews, not only single roles)
- declares dependency edges
- decides when a failed step should be retried, inside the circuit breakers in section 9

Lab is the planner and crew factory. Execution stays in Mission Control. Lab does not own a private run loop.

In the recovered baseline, **Start Mad Scientist Mission** is the Lab entry point. Today's scout-then-graph behavior is an early slice of this personality, not the whole contract. `RECOVERY_NOTES.md` calls the broader idea an experience compiler: route a goal to a known workflow when one exists, otherwise generate one, execute it with bounded loops and approval gates, and keep the success as reusable experience. This contract is that idea with the execution boundary made explicit.

## 3. Execution boundary

This boundary is non-negotiable.

Whatever Lab creates — steps, roles, or crews — runs through Mission Control:

- Mission Control's runner (the same queue, worker, and provider adapters used for human-launched runs)
- approval gates
- logging
- workspace memory

Lab's own calls to a model are planning calls: propose a DAG, a role, or a crew. Execution of that plan is a Mission Control Attempt.

Lab does not apply patches, does not talk to providers in order to execute a step, and does not bypass approvals. Lab does not keep a side channel that mutates the repo, git state, or durable memory.

## 4. Core objects

**Mission.** One unit of work aimed at a goal. Either a human-configured workflow run or a Lab-planned graph. Holds status, caps, and the set of steps.

**Step.** A logical node in the mission DAG. Stable identity, instructions, an assigned role or crew, and a status such as pending, ready, running, succeeded, failed, or `BLOCKED_HUMAN`, plus approval-waiting when a patch is parked. Retries do not create a new Step.

**Attempt.** One execution of a Step. Maps to one ordinary Tank run. A Step has an ordered attempt stack. Fixer passes and retries are further Attempts on that same Step.

**Dependency edge.** A directed edge from a parent Step to a child Step. The child becomes ready when every direct parent has succeeded. Edges are the source of truth for ordering.

**Artifact.** A durable product of an Attempt: a plan, a sandboxed draft patch, a test or build report, a distilled summary. Downstream steps receive distilled artifacts.

**Approval.** A human decision on a proposed mutation, especially a patch. The path is sandboxed draft, then dry-run verify, then human **Approve & Apply** or Reject. The workspace changes only on Approve & Apply. The baseline already pauses every patch, including fix-loop attempts, for that click.

**Crew.** A named group of roles with tools and provider preferences, launchable as a unit. Lab may reuse, adapt, or generate a whole crew. A generated crew becomes a default reusable asset only after the v1 confirmation rule in section 6.

**Role / Goblin.** One specialist: name, system prompt, tools, and provider preference. The recovered code stores this as an agent role. **Goblin** is the product name for that same specialist. A crew is made of roles. Lab may reuse, adapt, or generate roles, and may generate the crew that binds them.

**Workspace memory.** Facts Tank keeps for a workspace, in two layers:

- **Durable facts** outlive a mission: project profile, important paths, language and test command, known pitfalls, and crews or roles a human has accepted as reusable. The baseline already stores project profile facts, important paths, changed paths, successful summaries, and pitfalls in `workspace_memory`.
- **Mission ephemera** stay with the mission or the attempt: raw provider streams, full logs, scratch plans, and unaccepted crew drafts. Ephemera enter the next mission only as a distilled artifact or as an explicit durable fact.

## 5. Step vs Attempt

Retries and fixers attach new Attempts to the same Step.

The conceptual DAG stays the plan: the steps that were supposed to happen. The attempt stack shows how many times Tank tried. A fixer is an Attempt (or a short sequence of Attempts) whose job is to repair the Step that failed.

Lab must not add Fixer nodes that pollute the conceptual DAG.

A human-authored Mission Control template may still name a recovery stage when a person wrote that workflow. The YAML `fixer` stage in `mission_templates.yaml` is that kind of hand-authored stage. It is not permission for Lab to splice a new DAG node every time a generated step fails. The graph rule in `README.md` and `RECOVERY_NOTES.md` — bounded fixer attempts and retries on the same logical step — is this attempt-stack rule.

## 6. Reuse → Adapt → Generate

When Lab needs a specialist or a crew, it prefers, in order:

1. **Reuse** an existing role or crew that already fits.
2. **Adapt** an existing role or crew (narrow the prompt, tools, or provider preference) and record what changed.
3. **Generate** a new role, or a new whole crew, only when reuse and adapt fit poorly.

A generated crew must carry:

- a name
- its roles
- its tools
- its provider preferences
- a short rationale: why not reuse X? Name the closest existing crew or role that was rejected.

**v1 confirmation.** A newly generated crew requires human confirmation before it becomes a default reusable asset in workspace memory. The product may later limit that prompt to the first N generated crews; until N is set, confirm each new crew. Until confirmed, the crew may run for the current mission only.

## 7. Context fan-in

A Step receives only:

- the mission goal
- that step's own instructions
- durable workspace facts
- distilled artifacts from its **direct** parent steps

A step with several parents gets localized context from all of those direct parents, which is the rule already described for Mad Scientist graph missions in `README.md`. Indirect ancestors contribute only when a parent has already distilled them into an artifact.

Full upstream logs, raw provider streams, and sibling-branch transcripts stay out of the step prompt.

## 8. Parallelism

The scheduler runs Steps that are READY because every dependency succeeded. Independent branches may run concurrently.

Mission Control still caps concurrency. Today that cap is `TANK_MAX_PARALLEL_RUNS` (default 3). A later per-mission cap may tighten it further. Parallelism is a property of the DAG plus those caps. It is not a reason for Lab to run its own workers.

## 9. Safety and circuit breakers

These limits are schema-shaped. The mission record should be able to store them, and the runner enforces them. They are not prompt advice.

- **Max attempts per Step.** Default **3**, the same default as `TANK_DEFAULT_MAX_FIX_LOOPS`. When the budget is exhausted the Step becomes **`BLOCKED_HUMAN`**. Automation stops on that step until a person extends the budget, edits the plan, or abandons the mission.
- **Mission cost and token caps.** Lab UI targets, configurable per mission: **$5** and **500,000 tokens**. Crossing either cap stops new attempts and moves the mission to **`BLOCKED_HUMAN`**. These are the documented targets. The recovered baseline does not yet claim to enforce both.
- **Repeated mistake.** The same patch hash, or the same error hash, on a Step twice halts retries and sets **`BLOCKED_HUMAN`**. Tank does not spend the rest of the attempt budget repeating one failure.
- **Patch path.** Sandboxed draft patch, then dry-run verify, then human **Approve & Apply**. Lab never applies the patch.
- **Independent verification.** Tests, builds, and diffs gate success. A model's report that it passed does not count. The baseline tester stage already uses the real exit code of `post_actions.run_tests` for this kind of signal.

**`BLOCKED_HUMAN`** means Tank has stopped and is waiting for a person, and the record says why: attempt cap, cost cap, token cap, or a repeated patch or error hash. It is separate from a step that is running, and separate from an approval waiting on a patch the human can accept or reject.

## 10. Provider routing

Tank is provider-agnostic. Backends stay pluggable (`providers.yaml` and the provider adapters). There is no hard-coded single-vendor wrapper around the product.

Routing bias is local-first for routine work — Scout, classification, and small fixes — and escalation for harder work (large design, risky edits, weak local results) to a stronger configured provider. "Local Qwen-first where practical" in `RECOVERY_NOTES.md` is this bias, not a permanent binding to one model id. A person or a crew can still pin a provider.

## 11. Observability minimum

The human can see, without reading a raw provider log:

- the plan / DAG
- which steps are ready, running, failed, or blocked
- the attempt stack on a step
- approvals that are waiting on them
- why a step or mission is blocked

Raw provider streams may exist for debugging. The UX is the list above.

Step status follows the run, including approval waiting. The June 28 defect in `RECOVERY_NOTES.md` — a child run in `awaiting_approval` while the logical step still showed `queued` — violates this section.

## 12. Thin vertical acceptance

Broader Lab ambition waits until one coding mission passes this bar. The named dogfood example is a **JWT-auth style** mission: add or fix a small auth slice in a real repo. That mission must:

- plan a multi-step graph from the goal
- run steps concurrently when the DAG has independent ready branches
- show the attempt stack, including a retry or fixer attempt on the same step
- gate success on an independent test, build, or diff
- keep the patch behind Approve & Apply

Until that bar passes, defer the wider Lab surface.

## 13. Recovery PR stance

- Continue draft **PR #2** (`cursor/tank-graph-recovery-c38e`, "Reconstruct the Mad Scientist graph on the existing Tank schema") as the graph spine.
- Treat draft **PR #1** (`local-recovered`, "Recover local Tank graph orchestration and repository scouting") as a parts bin. Mine it when #2 is missing a piece.
- Do not blind-merge #1 and #2 together.
- Do not open a third overlapping recovery PR.
- This contract pull request does not merge #1 or #2.

After Jeffrey accepts this contract, the next implementation step is to audit PR #2 against it, fill gaps from #1, and then build the thin vertical observability and execution slice.

## 14. Non-goals for now

- A repo-wide move to ideal `control_center/` and `lab/` package layouts. The boundary in this contract is behavioral. Directories can follow later.
- Multi-domain Lab (travel and anything else beyond the coding dogfood) before the thin vertical passes.
- Silent unbounded crew sprawl. Generated crews stay on the current mission until a human confirms them (section 6).

## What this pull request does not do

- It does not change application code, tests, or schema.
- It does not merge PR #1 or PR #2.
- It does not start the thin vertical. Implementation waits until Jeffrey accepts this contract or explicitly says to build.
