# Tank Agent Status

> Shared handoff checkpoint for humans and AI agents. **GitHub/repository state is authoritative**; this file is a concise continuity aid, not a substitute for inspecting code, PRs, or CI. Never restart valid work because this checkpoint is stale.

Tank / TankMadScientist is a self-hosted multi-agent command center (Flask + SQLite + HTMX). North star: AI workflow compiler/runtime — Intent → DAG → Artifact; Mission Control must stay useful alone; Mad Scientist Lab plans and manages graphs; prefer continuing PR #2 over a third recovery PR.

## Current checkpoint

- **Updated:** 2026-09-24T21:12:00Z
- **Updated by:** Cursor cloud agent
- **Canonical main (verified):** `d27e6e530be2b1210a8f1aad19031705ed7a1817` — squash-merge of PR #4 onto the handoff protocol
- **Open PRs (verified):** #1, #2, and #5, all draft. Do not merge #1 or #2. This contract PR does not merge them.
  - PR #1 — Recover local Tank graph orchestration and repository scouting (`local-recovered`) — parts bin; draft; do not merge
  - PR #2 — Reconstruct the Mad Scientist graph on the existing Tank schema (`cursor/tank-graph-recovery-c38e`) — preferred graph spine; draft; PR body reports the graph validator passing (reported by that PR, not re-run here); do not merge
  - PR #5 — Freeze the Mission Control and Mad Scientist Lab contract (`cursor/mission-control-lab-contract-b9d2`) — docs-only draft for Jeffrey's review
- **Architecture contract:** `docs/MISSION_CONTROL_LAB_CONTRACT.md` (PR #5) is the draft source of truth for Mission Control vs Mad Scientist Lab. Docs only.
- **Merged:** PR #3 (handoff protocol) and PR #4 (STATUS reconcile after #3).
- **Scope:** Docs-only contract. No application feature work.
- **Next action:** Contract draft open for Jeffrey review. After accept, audit PR #2 against this contract and fill gaps from #1; then the thin vertical observability/execution slice. Implementation of app code still waits on Jeffrey accepting this contract (or explicit "build"). Do not merge #1 or #2. Do not start a third overlapping recovery PR.

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
