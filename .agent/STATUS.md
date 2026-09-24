# Tank Agent Status

> Shared handoff checkpoint for humans and AI agents. **GitHub/repository state is authoritative**; this file is a concise continuity aid, not a substitute for inspecting code, PRs, or CI. Never restart valid work because this checkpoint is stale.

Tank / TankMadScientist is a self-hosted multi-agent command center (Flask + SQLite + HTMX). North star: AI workflow compiler/runtime — Intent → DAG → Artifact. Mission Control must stay useful alone. Mad Scientist Lab plans; execution stays in Mission Control.

## Current checkpoint

- **Updated:** 2026-09-24T21:25:01Z
- **Updated by:** Cursor cloud agent
- **Canonical main (verified):** `0ff9f657e45d7f33b7bf66464ef7e6cb4f7fab85` — squash-merge of PR #5. Jeffrey accepted the Mission Control ↔ Mad Scientist Lab contract. Source of truth is `docs/MISSION_CONTROL_LAB_CONTRACT.md`. The file header still says draft; GitHub merge state wins.
- **Open PRs (verified):** #1, #2, and #6, all draft. Do not merge #1 or #2. Do not open a third recovery PR.
  - PR #6 — Audit PR #2 against Mission Control ↔ Lab contract (`cursor/pr2-contract-audit-a9d7`) — this docs-only draft
  - PR #1 — Recover local Tank graph orchestration and repository scouting (`local-recovered`, `712599406ee58761d6ea7cd7157ed7045573e53a`) — parts bin; draft; do not merge
  - PR #2 — Reconstruct the Mad Scientist graph on the existing Tank schema (`cursor/tank-graph-recovery-c38e`, `c9695e9762d2427d7b5f31053c37d27c184445ea`) — preferred graph spine; draft; graph validator reported passing by that PR, not re-run in this audit; do not merge
- **Audit:** `docs/PR2_CONTRACT_AUDIT.md` scores PR #2 against the contract: 8 aligned, 10 partial, 5 missing. Gap-fill comes from #1 onto the #2 line.
- **Merged:** PR #3 (handoff protocol), PR #4 (STATUS reconcile), PR #5 (contract, `0ff9f657`).
- **Scope:** Docs-only audit plus this handoff. No application code, no schema migration.
- **Next action:** Jeffrey reviews `docs/PR2_CONTRACT_AUDIT.md`. Then gap-fill from #1 into the #2 line (or the thin JWT-auth vertical with the observability minimum). Lab creations still execute via Mission Control. Still no auto-merge of #1 or #2.

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
