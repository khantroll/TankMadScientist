# Tank Agent Status

> Shared handoff checkpoint for humans and AI agents. **GitHub/repository state is authoritative**; this file is a concise continuity aid, not a substitute for inspecting code, PRs, or CI. Never restart valid work because this checkpoint is stale.

Tank / TankMadScientist is a self-hosted multi-agent command center (Flask + SQLite + HTMX). North star: AI workflow compiler/runtime — Intent → DAG → Artifact. Mission Control must stay useful alone. Mad Scientist Lab plans; execution stays in Mission Control.

## Current checkpoint

- **Updated:** 2026-09-24T23:51:00Z
- **Updated by:** Cursor cloud agent
- **Canonical main (verified with `gh` before this branch):** `ed5bead687359679513c6523fa12200d07144978` — `STATUS: reconcile after PR #7 merge (#8)`. Gap-fill on main remains PR #7 `9f041ca3124f0f8e5c5f465eed19f042bce48a78`. If this checkpoint disagrees with GitHub, GitHub wins.
- **Lineage on main (full merge SHAs):** handoff PR #3 `b40190f24594fd7692309a4b3f15482b333aa71d`; STATUS reconcile PR #4 `d27e6e530be2b1210a8f1aad19031705ed7a1817`; contract PR #5 `0ff9f657e45d7f33b7bf66464ef7e6cb4f7fab85`; audit PR #6 `bb2abef955507f67254f479a6a618c7db3ca827d`; gap-fill PR #7 `9f041ca3124f0f8e5c5f465eed19f042bce48a78`; STATUS reconcile PR #8 `ed5bead687359679513c6523fa12200d07144978`. Source of truth remains `docs/MISSION_CONTROL_LAB_CONTRACT.md`. `docs/PR2_CONTRACT_AUDIT.md` row 19 is the Pattern Synthesis gap this branch addresses. Lab plans; Mission Control executes.
- **Open drafts (verified, do not merge or close):**
  - PR #1 — parts bin (`local-recovered`, `712599406ee58761d6ea7cd7157ed7045573e53a`)
  - PR #2 — graph spine (`cursor/tank-graph-recovery-c38e`, `c9695e9762d2427d7b5f31053c37d27c184445ea`)
- **This work:** Pattern Synthesis (contract §6) on branch `cursor/pattern-synthesis-4c56`, branched from `main` at `ed5bead`. Draft PR opening against `main`. Not a third graph-recovery line. `mad_scientist_graph.py` and the `mad_scientist_*` scheduler/DDL stay the PR #2 shape from #7, plus a `synthesis_json` column.
- **What landed:** Lab inspects roles and crews, then reuses, adapts (narrowed prompt/tools/provider, with the change recorded), or generates. A generated crew carries name, roles, tools, provider preferences, and a rationale naming the closest rejected crew or role. It can run for the current mission immediately. **Confirm as reusable** writes it to workspace memory key `pattern_synthesis`. **Use for this mission only** leaves it off that key. Attempts are still `models.create_run` plus `mad_scientist_attempts`. Human YAML missions and crew launch are unchanged.
- **Verified:** `scripts/validate_pattern_synthesis.py`, `scripts/validate_mad_scientist_graph.py`, and `scripts/validate_tester_stage_validation.py` passed. A local demo page showed the two confirmation buttons; confirming removed them and recorded the durable sentence.
- **Not in this slice:** live JWT-auth dogfood in a real repo. #1 and #2 were not merged or closed.
- **Next action:**
  1. Jeffrey reviews the Pattern Synthesis draft PR. On a Lab mission card, **Confirm as reusable** or **Use for this mission only** is how a generated crew is confirmed.
  2. **JWT-auth dogfood** — run a JWT-auth-style mission in a real repo (contract section 12). Still not executed.
  3. **Decide the fate of drafts #1 and #2** (keep, close, or fold). Do not merge or close them until that decision.

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
