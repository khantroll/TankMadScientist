# Tank Agent Status

> Shared handoff checkpoint for humans and AI agents. **GitHub/repository state is authoritative**; this file is a concise continuity aid, not a substitute for inspecting code, PRs, or CI. Never restart valid work because this checkpoint is stale.

Tank / TankMadScientist is a self-hosted multi-agent command center (Flask + SQLite + HTMX) with Mad Scientist graph missions. `main` is the recovered baseline described in `RECOVERY_NOTES.md`.

## Current checkpoint

- **Updated:** 2026-09-24T21:01:00Z
- **Updated by:** Cursor cloud agent
- **Canonical main (verified):** `618ac2b86881d94db43cfd63d52dd6b848454918` — recovered baseline (`RECOVERY_NOTES.md`)
- **Open PRs (verified):**
  - PR #1 — Recover local Tank graph orchestration and repository scouting (`local-recovered`) — earlier recovery snapshot; draft; do not merge
  - PR #2 — Reconstruct the Mad Scientist graph on the existing Tank schema (`cursor/tank-graph-recovery-c38e`) — preferred current graph-reconstruction line; draft; PR body reports the graph validator passing (reported by that PR, not re-run here); auth and bounded repository discovery still later; do not merge
- **Scope:** This PR only bootstraps `.agent/` continuity files. No application feature work.
- **Next action:** Wait for Jeffrey's additional Tank documentation before starting implementation. Do not merge PR #1 or #2 automatically. Prefer continuing PR #2 over restarting graph work. Do not start a third overlapping recovery PR.

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
