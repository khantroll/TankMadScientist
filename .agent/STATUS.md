# Tank Agent Status

> Shared handoff checkpoint for humans and AI agents. **GitHub/repository state is authoritative**; this file is a concise continuity aid, not a substitute for inspecting code, PRs, or CI. Never restart valid work because this checkpoint is stale.

Tank / TankMadScientist is a self-hosted multi-agent command center (Flask + SQLite + HTMX). North star: AI workflow compiler/runtime — Intent → DAG → Artifact. Mission Control must stay useful alone. Mad Scientist Lab plans; execution stays in Mission Control.

## Current checkpoint

- **Updated:** 2026-09-24T23:21:00Z
- **Updated by:** Cursor cloud agent
- **Canonical main (verified):** `9f041ca3124f0f8e5c5f465eed19f042bce48a78` — squash-merge of PR #7 ("Fill contract gaps on the PR #2 graph"), merged 2026-09-24T23:09:12Z. If this checkpoint disagrees with GitHub, GitHub wins.
- **Lineage on main:** handoff PR #3 (`b40190f`) and STATUS reconcile PR #4 (`d27e6e5`); contract PR #5 (`0ff9f657`); audit PR #6 (`bb2abef`); gap-fill PR #7 (`9f041ca`). Source of truth remains `docs/MISSION_CONTROL_LAB_CONTRACT.md`. `docs/PR2_CONTRACT_AUDIT.md` is the accepted gap list that #7 filled on the PR #2 spine. Lab plans; Mission Control executes.
- **Open drafts (verified, do not merge or close):**
  - PR #1 — parts bin (`local-recovered`, `712599406ee58761d6ea7cd7157ed7045573e53a`)
  - PR #2 — graph spine (`cursor/tank-graph-recovery-c38e`, `c9695e9762d2427d7b5f31053c37d27c184445ea`)
- #7 landed the gap-fill onto `main`. #1 and #2 themselves are still open drafts. Their fate is undecided.
- **Now on main from #7:** attempt stack on the graph card; stored stop reason and `BLOCKED_HUMAN`; Approve/Reject only when `awaiting_approval` has a plan or patch payload; cyclic plans rejected before materialize; tester/reviewer success requires a real test/build/diff command and uses the tool exit code. Breakers: 3 attempts per step, mission spend cap $5, token cap 500000 only when a provider reports usage, repeated patch hash or error hash. A blocked step does not freeze an independent ready sibling. Missing provider usage is not treated as zero tokens.
- **Not in #7:** Pattern Synthesis (Reuse → Adapt → Generate) and crew confirmation; live JWT-auth dogfood in a real repo. Lab creations still execute only as Mission Control attempts.
- **Next action:**
  1. **Pattern Synthesis** — the Mad Scientist invent path: Reuse → Adapt → Generate (contract section 6), plus crew confirmation before a generated crew becomes a durable default. Not started.
  2. **JWT-auth dogfood** — run a JWT-auth-style mission in a real repo as the thin-vertical acceptance bar (contract section 12). #7 shipped the gates and breakers; no live dogfood was executed.
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
