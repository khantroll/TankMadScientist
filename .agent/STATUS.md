# Tank Agent Status

> Shared handoff checkpoint for humans and AI agents. **GitHub/repository state is authoritative**; this file is a concise continuity aid, not a substitute for inspecting code, PRs, or CI. Never restart valid work because this checkpoint is stale.

Tank / TankMadScientist is a self-hosted multi-agent command center (Flask + SQLite + HTMX). North star: AI workflow compiler/runtime — Intent → DAG → Artifact. Mission Control must stay useful alone. Mad Scientist Lab plans; execution stays in Mission Control.

## Current checkpoint

- **Updated:** 2026-09-25T00:12:00Z
- **Updated by:** Cursor cloud agent
- **Canonical main (verified with `gh`):** `96a49f1ff710ad9e2544dd0741e789f35c8426ae`. Commit title: `Add Lab pattern synthesis with v1 crew confirmation (#9)`. Squash-merge of PR #9 from branch `cursor/pattern-synthesis-4c56`, authored by cloud agent `bc-1e54ee57-2273-5586-930a-d87b152a4c56`, merged 2026-09-25T00:08:31Z. If this checkpoint disagrees with GitHub, GitHub wins.
- **Lineage on main (full merge SHAs):** handoff PR #3 `b40190f24594fd7692309a4b3f15482b333aa71d`; STATUS reconcile PR #4 `d27e6e530be2b1210a8f1aad19031705ed7a1817`; contract PR #5 `0ff9f657e45d7f33b7bf66464ef7e6cb4f7fab85`; audit PR #6 `bb2abef955507f67254f479a6a618c7db3ca827d`; gap-fill PR #7 `9f041ca3124f0f8e5c5f465eed19f042bce48a78`; STATUS reconcile PR #8 `ed5bead687359679513c6523fa12200d07144978`; Pattern Synthesis PR #9 `96a49f1ff710ad9e2544dd0741e789f35c8426ae`. Source of truth remains `docs/MISSION_CONTROL_LAB_CONTRACT.md`. Lab plans; Mission Control executes.
- **Open drafts (verified, do not merge or close):**
  - PR #1 — parts bin (`local-recovered`, `712599406ee58761d6ea7cd7157ed7045573e53a`)
  - PR #2 — graph spine (`cursor/tank-graph-recovery-c38e`, `c9695e9762d2427d7b5f31053c37d27c184445ea`)
- Pattern Synthesis (contract §6 / audit row 19) is on `main` via merged PR #9. This checkpoint is not awaiting Pattern Synthesis PR review. Do not re-open that work. #1 and #2 remain open drafts; their fate is undecided. Do not start a third graph-recovery line.
- **Now on main from #9:** Lab staffs specialists and whole crews with Reuse → Adapt → Generate. A generated crew may run for the current mission immediately and becomes durable workspace memory only after **Confirm as reusable** (key `pattern_synthesis`). **Use for this mission only** leaves it off that key. Steps remain Mission Control attempts. Human YAML missions and **Launch crew** are unchanged.
- **Still open:** live JWT-auth-style dogfood in a real repo (not run in #9). Fate of drafts #1 and #2.
- **Next action:**
  1. **JWT-auth-style dogfood** in a real repo — acceptance bar for the invent path (PR #9 checklist and contract §12). Still not executed.
     - Workspace points at a real repo; Mad Scientist goal is JWT-auth style.
     - The card shows reuse, or generate with a rationale naming the closest rejected crew or role.
     - If generate, **Confirm as reusable** or **Use for this mission only**.
     - Each step is a normal Mission Control run/attempt.
     - Patches stop at Approve / Reject.
     - Tester success needs a real test, build, or diff exit code.
     - Contract §12 also requires a multi-step graph, concurrent independent ready branches, and the attempt stack (including a retry or fixer) on the same step.
  2. **Decide the fate of drafts #1 and #2** (keep, close as superseded, or further cherry-picks). No auto-merge. Do not merge or close them until that decision.

## Handoff protocol

Every agent or human making meaningful project changes should:

1. Inspect current Git/GitHub state first (`main`, open PRs, branch heads, CI).
2. Read this file and reconcile it against GitHub; prefer GitHub when they disagree.
3. Continue existing valid branches/PRs; do not restart, overwrite, abandon, or duplicate work because STATUS is stale.
4. Do the work, appending ACTIVITY checkpoints at meaningful transitions when practical.
5. Before handing off, update **Current checkpoint** with agent/tool, timestamp, verified facts, unresolved issues, and exact next action.
6. Append one JSON object as a single line to `.agent/ACTIVITY.jsonl`.
7. Commit these handoff changes with the work when practical.

## Rules

- Never claim work is complete solely because an agent said so; verify repository state.
- Distinguish **observed**, **reported**, and **verified** facts when they differ.
- Do not overwrite useful unresolved context with a generic "done."
- Keep this file concise. Detailed history belongs in `ACTIVITY.jsonl`, commits, PRs, and issues.
- If multiple agents are active, re-check the remote before updating this file to avoid clobbering a newer handoff.
- Do not commit secrets, huge logs, command dumps, or generated output into `.agent/`.
