# Tank Agent Status

> Shared handoff checkpoint for humans and AI agents. **GitHub/repository state is authoritative**; this file is a concise continuity aid, not a substitute for inspecting code, PRs, or CI. Never restart valid work because this checkpoint is stale.

Tank / TankMadScientist is a self-hosted multi-agent command center (Flask + SQLite + HTMX). North star: AI workflow compiler/runtime — Intent → DAG → Artifact. Mission Control must stay useful alone. Mad Scientist Lab plans; execution stays in Mission Control.

## Current checkpoint

- **Updated:** 2026-09-24T21:52:00Z
- **Updated by:** Cursor cloud agent
- **Canonical main (verified):** `bb2abef955507f67254f479a6a618c7db3ca827d` — squash-merge of PR #6. Jeffrey accepted the contract (PR #5, `0ff9f657`) and the PR #2 audit (PR #6). Source of truth remains `docs/MISSION_CONTROL_LAB_CONTRACT.md`. `docs/PR2_CONTRACT_AUDIT.md` is the accepted gap list.
- **Open drafts (verified, do not merge):**
  - PR #1 — parts bin (`local-recovered`, `712599406ee58761d6ea7cd7157ed7045573e53a`)
  - PR #2 — graph spine (`cursor/tank-graph-recovery-c38e`, `c9695e9762d2427d7b5f31053c37d27c184445ea`)
- **This work:** Draft PR #7 — https://github.com/khantroll/TankMadScientist/pull/7 — gap-fill plus thin-vertical scaffolding on the #2 line. Branch `cursor/graph-breakers-gap-fill-92b3`, implementation `4b40f28b9c5d0b58cf33019971342b052711aa01`, from PR #2 head with `main` `bb2abef` merged in. Not a third recovery/reconstruction of the graph. `mad_scientist_graph.py` and the `mad_scientist_*` tables stay the PR #2 scheduler and DDL, with columns and behavior added on top.
- **Ported from #1:** attempt stack on the graph card, stored stop reason, Approve/Reject only when `awaiting_approval` has a plan or patch payload, cyclic plans rejected before materialize, tester/reviewer success requires a real test/build/diff command and uses the tool exit code.
- **Breakers on the advance path:** per-step attempt cap default 3, mission spend cap default $5, token cap column default 500000, `BLOCKED_HUMAN` plus reason, repeated patch hash or error hash halts that step. A blocked step does not freeze an independent ready branch. Spend, token, and a stuck DAG move the mission to `BLOCKED_HUMAN`.
- **Token gap:** the cap is compared only after a provider reports tokens (`usage.total_tokens`, or prompt + completion). `local_agent` copies that onto the run payload. Providers that omit usage never increment `tokens_used` and do not trip the cap. That is not treated as zero.
- **Not in this slice:** Reuse → Adapt → Generate, crew confirmation, replacing providers.yaml or workspaces config, log excerpts in step prompts, overwriting fixer step task text, merging or closing #1 or #2.
- **Dogfood:** no live JWT-auth run was executed. Validators passed: `scripts/validate_mad_scientist_graph.py` and `scripts/validate_tester_stage_validation.py`. Manual dogfood steps are in the PR body.
- **Merged:** PR #3 (handoff), PR #4 (STATUS reconcile), PR #5 (contract), PR #6 (audit).
- **Next action:** Jeffrey reviews draft PR #7. Do not merge or close #1 or #2. After review, run the manual JWT-auth dogfood in a real repo. Lab creations still execute only as Mission Control attempts.

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
