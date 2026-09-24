# Agent Project Memory

Tank (TankMadScientist) uses a tiny repository-native continuity protocol so work can move between ChatGPT, Cursor, Claude, Gemini, local terminals, and other agents without reconstructing project state from conversation history.

## Source of truth

**GitHub / the Git repository is always authoritative.**

`.agent/STATUS.md` and `.agent/ACTIVITY.jsonl` are advisory coordination artifacts only. They may be stale. An agent must never restart, overwrite, abandon, or duplicate valid branch/PR work merely because `STATUS.md` disagrees with GitHub.

## Start of a session

1. Inspect current GitHub/`main`, open PRs, branch heads, and CI.
2. Read `.agent/STATUS.md`.
3. Reconcile STATUS against GitHub. Prefer GitHub when they disagree.
4. Continue existing valid work rather than recreating it.

## During meaningful work

Append ACTIVITY checkpoints at natural transitions (task started, audit/plan chosen, meaningful commit, tests run, blocker, PR created/updated, task completed). Do not wait only for final completion, and do not turn status maintenance into a project of its own.

## End of a meaningful work unit

Update `.agent/STATUS.md` with the latest concise checkpoint and append one single-line JSON object to `.agent/ACTIVITY.jsonl`.

Recommended event fields:

```json
{"timestamp":"ISO-8601 timestamp","agent":"tool/agent name","event":"short-event-name","branch":"branch","head":"commit SHA if known","task":"what was attempted","changes":["important changes"],"verification":["tests/build/CI actually run"],"unresolved":["remaining issue"],"next":"exact next action"}
```

Fields may be omitted when genuinely unknown. Do not invent values. Do not commit secrets, huge logs, command dumps, or generated noise.

## Design intent

`STATUS.md` answers **“where should the next worker start?”**

`ACTIVITY.jsonl` answers **“what happened over time?”** (append-only)

The repository answers **“what is actually true?”**

This is deliberately lightweight. It can later feed a dashboard, API, MCP server, or other project-continuity service without making Tank depend on one AI vendor.
