# Audit: PR #2 against the Mission Control ↔ Lab contract

**Audited:** 2026-09-24.
**Source of truth:** `docs/MISSION_CONTROL_LAB_CONTRACT.md` on `main` at `0ff9f657` (squash-merge of PR #5). Jeffrey accepted that contract. The file header still says "Draft for Jeffrey's review"; this audit follows the merged GitHub state.
**Spine:** draft PR #2, head `c9695e9762d2427d7b5f31053c37d27c184445ea` (`cursor/tank-graph-recovery-c38e`). Base is recovered baseline `618ac2b`, so the branch does not yet contain `.agent/` or this contract.
**Parts bin:** draft PR #1, head `712599406ee58761d6ea7cd7157ed7045573e53a` (`local-recovered`). Not a second spine.
**This document:** audit only. No application code, no schema migration, no merge of #1 or #2, no third recovery PR.

PR #2's own body reports `scripts/validate_mad_scientist_graph.py` passing. That run was not repeated here.

## How to read the checklist

**Aligned** means the PR #2 tree already behaves the way the contract requires.
**Partial** means the shape is there and a real gap remains.
**Missing** means PR #2 does not implement the requirement. PR #1 does not fill every missing row; several gaps exist in both drafts.

Counts: **8 aligned, 10 partial, 5 missing** (23 requirements).

| # | Contract requirement | PR #2 | Where |
|---|---|---|---|
| 1 | Mission Control stays useful with Lab off (YAML missions, crews, same runner) | Aligned | `mission.py` and `mission_templates.yaml` unchanged. Lab is the existing Start Mad Scientist Mission path in `app.py` / `mad_scientist.py`. |
| 2 | Lab plans; execution uses Mission Control's queue, worker, and provider adapters | Aligned | Graph steps call `models.create_run` then `session_manager.launch_pending` (`mad_scientist.py` `_create_step_attempt`, `_schedule_ready_graph_steps`). |
| 3 | Lab does not apply patches or keep a side channel for repo, git, or durable memory | Aligned | Patches stay on the run payload and the existing approve/reject routes. Graph code does not call apply. |
| 4 | Mission, Step, dependency edge, Attempt on `mad_scientist_*`; `parent_run_id` is compatibility only | Aligned | `models.py` tables `mad_scientist_missions`, `mad_scientist_steps`, `mad_scientist_step_dependencies`, `mad_scientist_attempts`. Scheduling is in `mad_scientist_graph.py`. |
| 5 | Fixer and retry are further Attempts on the same Step, not new DAG nodes | Aligned | `_advance_graph_locked` records `attempt_kind` `fixer` or `retry` on the failed step id. The hand-authored YAML `fixer` stage is untouched. |
| 6 | Ready steps run concurrently, capped by `TANK_MAX_PARALLEL_RUNS` and an optional tighter per-mission cap | Aligned | `ready_steps` plus `_effective_mission_cap` (`max_parallel_steps` and `TANK_MAX_PARALLEL_RUNS_PER_MISSION`). Global cap unchanged. |
| 7 | Step status follows the run, including `awaiting_approval` (June 28 desync) | Aligned | `mad_scientist_graph.py` `sync_run_status`. Startup sweep in `session_manager.py` fails leftover `running` rows and leaves `awaiting_approval` alone. |
| 8 | Patch path stays sandboxed draft, then human Approve & Apply or Reject | Aligned | Baseline gate kept. Graph card posts `approve_run_patch` / `reject_run_patch` (`templates/partials/mission_list.html`). Button label is "Approve", not "Approve & apply". |
| 9 | Intent → DAG → Artifact, with a durable distilled artifact for downstream steps | Partial | Plan is stored (`materialize_plan`, `missions.plan_json`). Downstream context is `run_chain.extract_run_output` (summary/plan, or a log fallback). No artifact record. `evaluations` stores a tester/reviewer verdict. |
| 10 | Role / Goblin is one specialist Lab may reuse, adapt, or generate | Partial | Steps carry a role slug from a fixed `VALID_ROLES` set and bind `agent_roles` by slug (`role_slug_for_step`). Unknown roles become `builder`. No adapt or generate. |
| 11 | Workspace memory has durable facts vs mission ephemera | Partial | `_store_memory` writes project profile, paths, summaries, and pitfalls under `workspace_memory` key `mad_scientist` when a graph mission ends. Step prompts do not include that blob. Ephemera are not separated from durable facts. |
| 12 | A step receives the goal, its instructions, durable facts, and distilled artifacts from direct parents only | Partial | `dependency_context` fans in every direct parent, not only the newest, and sets `parent_run_id` to the newest completed dependency run. Durable facts are omitted. If the parent has no structured plan, the prompt can include log text. |
| 13 | Dependency edges are a DAG; cycles are rejected | Partial | Edges are the readiness source (`ready_steps`). `_validate_plan` does not reject cycles. `_ordered_steps` appends whatever it cannot order. A cycle later fails the mission as unsatisfied dependencies. |
| 14 | Max attempts per Step default 3, then `BLOCKED_HUMAN` | Partial | Default is `TANK_DEFAULT_MAX_FIX_LOOPS` (3) via `missions.max_fix_loops`, but the counter is mission-wide `fix_loop_count`, not per step. Exhaustion or a failed fixer sets mission `failed`, not `BLOCKED_HUMAN`. |
| 15 | Mission cost cap target $5, then `BLOCKED_HUMAN` | Partial | Optional `spend_cap_usd` / `spend_usd` and provider `cost_usd` (`charge_run_spend`, `spend_block_reason`). No $5 default. Crossing the cap fails the mission. |
| 16 | Independent test, build, or diff gates success | Partial | `local_agent.py` still runs `post_actions.run_tests` and uses the exit code when the model asks. Graph tester/reviewer text only says to request tests. `record_evaluation_if_applicable` treats run status `done` as pass. |
| 17 | Observability without reading a raw provider log: DAG, step state, attempt stack, waiting approvals, why blocked | Partial | `mission_view` and `mission_list.html` show summary, spend, step status, `depends_on`, unresolved names, evaluation verdict, and Approve/Reject. They do not list the attempt stack or a block reason distinct from the mission note. |
| 18 | Provider-agnostic adapters, local-first bias for routine work, escalation for harder work | Partial | `providers.yaml` and adapters stay pluggable. PR #2 adds a tracked-key guard (`api_key_default` other than `lm-studio` rejected). No runner policy implements local-first vs escalation. |
| 19 | Reuse → Adapt → Generate for roles and whole crews, with v1 confirmation before a generated crew becomes a durable default | Missing | Human crew launch remains (`app.py` `launch_crew`, workspace role check). Lab never selects, adapts, or generates a crew, and never asks for confirmation. |
| 20 | Token cap target 500,000, then `BLOCKED_HUMAN` | Missing | No token column, counter, or stop. |
| 21 | Same patch hash or error hash twice on a Step halts retries | Missing | No hash stored or compared. |
| 22 | `BLOCKED_HUMAN` is its own state, with a reason (attempt, cost, token, or repeated hash), separate from running and from approval-waiting | Missing | Statuses in use include `planned`, `queued`, `running`, `awaiting_approval`, `done`, `failed`, `fixer_succeeded`. No `BLOCKED_HUMAN`. |
| 23 | Thin vertical: a JWT-auth style mission plans a multi-step graph, runs independent ready branches, shows a retry or fixer on the same step, gates on a real test/build/diff, and parks the patch at Approve & Apply | Missing | Not started. Correctly out of PR #2's reconstruction scope. This is the next build, not a third recovery line. |

## What PR #2 already is

PR #2 is the right spine for sections 3–5 and 8 of the contract:

- One graph mission row per Tank mission (`mission_id` unique), not a second mission identity.
- Steps, edges, and attempts. Each attempt's `run_id` is unique.
- Ready-step scheduling with direct-dependency fan-in.
- Fixer then retry on that same step, still bounded by the existing fix-loop setting.
- Approval status copied onto the step.
- Optional spend accounting, an `evaluations` row, a per-mission parallel cap, WAL plus busy timeout, startup recovery, and a workspace ownership check on scout retry and crew role selection (`app.py`).

Lab in this tree is still scout-then-graph. That matches the contract's description of today's entry point. It is not the crew factory.

## Recommended take-from-#1

Port behavior onto the PR #2 schema. Do not replace `mad_scientist_graph.py` or the `mad_scientist_*` DDL.

1. **Attempt stack in the graph UI.** `templates/partials/mad_scientist_graph.html` (Attempts column: count, `attempt_type`, status, run link) and the view built by `graph_entries_for_workspace` in PR #1 `mad_scientist_graph.py` (about line 853). PR #2 `mission_view` returns the current step only. Fold the stack into that view and `templates/partials/mission_list.html`. PR #2 attempts are ordered by id; PR #1's `attempt_number` column is optional.
2. **A stored reason when automation stops.** PR #1 `models.py` `blocked_reason` on `mad_scientist_missions` and `mad_scientist_steps`, filled by `_block_mission` and `_handle_step_failed`. Use the text as the `BLOCKED_HUMAN` reason. Do not copy the status word `blocked` or the `legacy_mission_id` mission shape.
3. **Actionable approval controls.** `run_approval.py` `approval_state` (only offer controls when `awaiting_approval` has a plan or a patch payload) and `templates/partials/approval_controls.html` plus `templates/partials/approval_alert.html`. PR #2 shows Approve/Reject whenever the step status is `awaiting_approval`.
4. **Reject cyclic plans.** `_assert_acyclic` in PR #1 `mad_scientist_graph.py` (about lines 144–162). Call it from PR #2 `_validate_plan` before `materialize_plan`.
5. **Tester success requires a real command.** PR #1 `local_agent.py` `_is_tester_context` and the `tester_context` branch of `_validate_payload` (reject a tester plan/result unless `post_actions.run_tests` is true and `test_command` is set). Proof script: `scripts/validate_tester_stage_validation.py`. Supporting detection, not the whole file: `repo_context.py` `detect_project_profile` / `resolve_test_command` / `repository_scout`, with `scripts/validate_repository_scout.py`. That is what lets a JWT-auth tester gate on an exit code instead of the model's report.

## Explicit non-takes

These conflict with the contract or duplicate PR #2.

- **PR #1's graph runtime.** `mad_scientist_graph.py` functions `advance_mission`, `_schedule_ready_steps`, `_store_plan`, `_complete_scout`, `_handle_step_done`, `_handle_step_failed`. That is a second scheduler: no spend cap, no per-mission parallel cap, no `sync_run_status` for `awaiting_approval`, and no startup sweep. Replacing PR #2's module would drop the spine.
- **PR #1 graph DDL.** `legacy_mission_id`, `plan_json` and caps living on the graph mission, attempts keyed by graph mission id, no `spend_usd`, `max_parallel_steps`, `cost_usd`, or `evaluations`. Dual-merging `models.py` forks the contract's tables.
- **Log excerpts in the step prompt.** `build_dependency_context_for_step` appends a log excerpt and failure notes from every direct parent. Section 7 keeps full logs and raw streams out of the prompt. PR #2's `extract_run_output` is closer; do not replace it with the PR #1 excerpt.
- **Fixer text written onto the step row.** PR #1 `_handle_step_failed` overwrites `mad_scientist_steps.task` for the fixer, then restores it. The step's instructions should stay stable; the fixer task belongs on the attempt, which is what PR #2 `task_override` already does.
- **`providers.yaml` replacement, `workspaces.yaml`, and `workspaces.example.yaml`.** Sanitized local config, not a contract gap. Easy to clobber the baseline provider list.
- **Human crew UI restyle.** `templates/partials/crew_step_row.html` and the large `static/css/style.css` crew/graph block. Both drafts already launch crews through Mission Control. The restyle does not add Reuse → Adapt → Generate or v1 confirmation.
- **Cleanup deletions.** `session_manager.bak`, `data/logs/run-115.log`, `templates/partials/run_children.bak`, `run_list.bak`, `run_list.bak2`. Not behavior.
- **`mission.py` log excerpts inside mission notes.** Useful diagnostics, and they paste raw log text into the human-facing note. Leave them until observability has a real block reason.
- **Anything #1 does not have either.** Token cap, repeated patch/error hash, `BLOCKED_HUMAN`, crew confirmation, and the JWT vertical are absent from both drafts. Do not hunt for them in #1.

## Proposed first implementation slice

After Jeffrey reviews this audit, build the contract's thin vertical on the PR #2 line. Do not merge #1 or #2. Do not open another recovery PR. Bring `main` (`0ff9f657`) into `cursor/tank-graph-recovery-c38e` as part of that work so the branch contains the contract.

The slice is one JWT-auth style coding mission (section 12), plus the observability minimum (section 11), with Lab creations still executing only as Mission Control attempts (section 3).

1. Cherry-pick the five behaviors above onto PR #2's tables and `mission_view`. The graph card must show the plan, which steps are ready / running / failed / blocked, the attempt stack on a step (including fixer and retry), and approvals that are actually waiting.
2. Add the smallest schema the breakers need so that dogfood can stop cleanly: per-step attempt count default 3, mission cost default $5, token cap 500,000, and `BLOCKED_HUMAN` with a reason. Enforce them in the PR #2 advance path before queueing another attempt. Repeated-hash halt can land in the same pass if it stays a column plus a compare; it is not a reason to widen the slice into a crew factory.
3. Require tester steps to run a real test, build, or diff and to fail on the tool's exit code. A model's "passed" summary does not complete the step.
4. Dogfood the mission in a real repo: plan a multi-step graph from the goal, run two independent ready steps at once under the existing caps, show one fixer or retry on the same step, and leave the patch at Approve & Apply.
5. Leave Reuse → Adapt → Generate and generated-crew confirmation for after that bar passes. Human crews keep launching through Mission Control unchanged.

Until that review, application work stays stopped.
