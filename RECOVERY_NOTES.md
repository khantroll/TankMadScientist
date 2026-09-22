# Local recovery update — 2026-09-22

The `local-recovered` branch supersedes the missing-source assessment below. Main at `618ac2b86881d94db43cfd63d52dd6b848454918` remains the older preservation baseline. The recovered ZIP supplies the graph implementation, its validator, repository scouting, and approval UI. See `LOCAL_RECOVERY_REPORT.md` for evidence, sanitization, and validation limitations. The older notes below describe the September 21 snapshot, not the current branch. Their claim that the old tree contained no logs was inaccurate: main included `data/logs/run-115.log`, removed here.

---

# Tank â€” Mad Scientist Recovery Notes

Recovered 2026-09-21 from the latest surviving project archive and individual files retained from the June 2026 recovery work.

## What this snapshot is

This repository is a **best recoverable baseline**, not a claim that it is the final desktop copy.

The surviving full archive (`app (2).zip`) was uploaded on 2026-06-28 around 16:08 UTC. Later conversation/project artifacts document an architectural refactor completed later that day. The final surviving README describes that later design, but not every post-refactor source file survived independently in the file library.

The recovered tree therefore contains:

- the latest surviving complete application archive;
- the later surviving `README.md` describing the intended graph-based Mad Scientist architecture;
- the later surviving `session_manager.py` where available;
- existing Tank modules for Flask/SQLite/HTMX, providers, local agents, missions, crews, repo context, scheduling and git integration;
- no runtime SQLite database, caches, logs, API keys, or machine-specific workspace paths.

## Authoritative intended Mad Scientist design (June 28, 2026)

The final documented design moved Mad Scientist from a simple parent/child run chain to a durable graph source of truth:

1. `mad_scientist_missions` â€” mission-level graph state.
2. `mad_scientist_steps` â€” logical planned steps.
3. `mad_scientist_step_dependencies` â€” DAG edges.
4. `mad_scientist_attempts` â€” maps ordinary Tank runs to attempts against logical steps.
5. `parent_run_id` remains compatibility metadata only, not the graph source of truth.
6. Ready steps are scheduled when all direct dependencies complete.
7. A step with multiple dependencies receives context from **all direct dependency steps**.
8. Failed implementation steps get bounded fixer attempts and retries on the same logical step.
9. Workspace memory persists project profile facts, important paths, changed paths, successful summaries, and pitfalls.
10. UI should expose DAG/step state, failures, approvals and retry state.

The final documented validation entry point was intended to be:

`python scripts/validate_mad_scientist_graph.py`

That post-refactor validator and `mad_scientist_graph.py` are **not present in the surviving full archive**. Do not silently assume the older `mad_scientist.py` implements the final graph contract.

## Known final-state issue from the June 28 work

A later test/debug session showed a synchronization defect: child runs could be `awaiting_approval` while their logical Mad Scientist steps still displayed `queued`. The intended next fix was to synchronize run/step state, expose approval controls in the graph UI, add a mission status banner/resume behavior, and validate those transitions.

## June 29 follow-up direction

The next architectural priority identified after the Mad Scientist work was **Repository Discovery / Intelligence**. Tank had attempted to scan too broad a filesystem scope. The intended subsystem should:

- detect repository roots using markers such as `.git`, `pyproject.toml`, `requirements.txt`, and `package.json`;
- aggressively ignore binaries, caches, generated artifacts and secrets;
- detect language/framework/test tooling;
- show repository root, framework/language, test command, ignored paths and estimated context size before agents run;
- give every agent the same bounded repository manifest instead of allowing accidental whole-drive scanning.

## Recovery strategy for future work

Treat the existing source as the executable baseline and this document + the final README as the specification delta. Before adding features, first reconstruct the missing graph refactor with tests. Avoid rewriting the whole application.

Suggested sequence:

1. Establish tests around current baseline behavior.
2. Reconstruct durable graph tables/models and migrations.
3. Reconstruct `mad_scientist_graph.py` scheduling and dependency-context fan-in.
4. Wire graph state into `app.py`, providers/session completion callbacks, and approval transitions.
5. Rebuild graph UI partial and status synchronization.
6. Rebuild `scripts/validate_mad_scientist_graph.py` as deterministic validation.
7. Implement bounded Repository Discovery / Intelligence before allowing autonomous repo scouting.
8. Only then resume broader Experience Compiler / reusable workflow ambitions.

## Product intent

Mad Scientist was conceived as a general-purpose **experience compiler**: take a human goal, route it to a known reusable agent/workflow when one exists, otherwise generate a workflow, execute/test it with bounded loops and approval gates, and preserve the successful workflow as reusable experience. Local Qwen-first execution was preferred where practical, with stronger providers available as configured alternatives.
