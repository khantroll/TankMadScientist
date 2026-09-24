# Tank Agent Status

> Shared handoff checkpoint for humans and AI agents. **GitHub/repository state is authoritative**; this file is a concise continuity aid, not a substitute for inspecting code, PRs, or CI. Never restart valid work because this checkpoint is stale.

Tank / TankMadScientist is a self-hosted multi-agent command center (Flask + SQLite + HTMX). North star: AI workflow compiler/runtime — Intent → DAG → Artifact; Mission Control must stay useful alone; Mad Scientist Lab plans and manages graphs; prefer continuing PR #2 over a third recovery PR.

## Current checkpoint

- **Updated:** 2026-09-24T21:03:52Z
- **Updated by:** Cursor cloud agent
- **Canonical main (verified):** `b40190f24594fd7692309a4b3f15482b333aa71d` — squash-merge of PR #3 onto the recovered baseline (`RECOVERY_NOTES.md`)
- **Open PRs (verified):** only #1 and #2; both draft; do not merge
  - PR #1 — Recover local Tank graph orchestration and repository scouting (`local-recovered`) — earlier recovery snapshot; draft; do not merge
  - PR #2 — Reconstruct the Mad Scientist graph on the existing Tank schema (`cursor/tank-graph-recovery-c38e`) — preferred graph line; draft; PR body reports the graph validator passing (reported by that PR, not re-run here); auth and bounded repository discovery still later; do not merge
- **Merged:** PR #3 — Add .agent STATUS handoff protocol — merged to `main` as `b40190f24594fd7692309a4b3f15482b333aa71d`
- **Docs:** Jeffrey delivered three AI recollection docs in chat (digested). Implementation remains paused until he says go.
- **Scope:** Continuity refresh only. No application feature work.
- **Next action:** Await Jeffrey's explicit go on the first implementation slice and on the #1 vs #2 merge strategy. Prefer PR #2 for graph work. Do not auto-merge #1 or #2. Do not start a third overlapping recovery PR.

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
