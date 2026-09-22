# Tank / Mad Scientist local recovery

## Recommendation

Adopt this branch as the recovered development-source baseline after review. It materially completes the graph design already documented in main. Do not treat it as a production-ready or installable release: wheel packaging is incomplete and live providers, restart recovery, concurrency, and full approval flows still need integration testing. Main is not overwritten.

## Provenance and lineage

Baseline: `khantroll/TankMadScientist` main `618ac2b86881d94db43cfd63d52dd6b848454918` (2026-09-21), preceded by `a45a684ad3554c4ae8e3b193e2b1de69bf78d9b7`.

Input: `app (4).zip`, SHA-256 `5b2403ee529c13f246a0283e0339f330ad2f613c18bc2523cf1dce87a1b1a85c`.

Of 50 candidate files, 19 match main byte-for-byte, 20 differ, and 11 are new at their archive paths. The new `app/services/__init__.py` is the same service placeholder relocated from main's extra nesting, not new functionality. Main-only recovery notes are preserved and updated.

The unchanged README already describes the graph tables and validator that main lacks; the local source implements that design. Together with shared identical modules and later repository-scout changes, this strongly supports a later snapshot of the same project. No Git metadata is present in the ZIP, so exact ancestry and originating commits cannot be proved. ZIP timestamps have no timezone and are not Git dates: graph 2026-06-28 16:34:14; models 2026-06-28 13:48:52; app 2026-06-28 16:12:38; repo_context 2026-08-31 19:52:28; local_agent 2026-08-31 19:52:34. The prior conversation's September 1 wording is not the literal ZIP date.

## Meaningful differences

| File | Main bytes | ZIP bytes | Change |
|---|---:|---:|---|
| mad_scientist_graph.py | absent | 38,295 | Durable DAG planning, acyclicity checks, ready-step scheduling, fan-in context, bounded fixer/retry attempts, scout retries, memory, graph views |
| models.py | 22,916 | 33,172 | Four graph tables and CRUD/query helpers; UTF-8 config reading |
| repo_context.py | 15,242 | 37,920 | Repository root discovery, bounded/sensitive-file filtering, source priority, manifest/scout information, test-command detection |
| local_agent.py | 20,477 | 25,857 | Tester response validation, discovered-root execution, context diagnostics and static validation fallback |
| app.py | 27,069 | 29,704 | Graph/retry routes, approval state and alerts, grouped provider controls |
| session_manager.py | 12,118 | 13,614 | Provider failure logging and terminal crew-parent finalization |
| providers.py | 19,904 | 22,318 | Grouped providers, repository-root integration, broader exception reporting |
| mission.py | 11,489 | 12,448 | Graph advancement integration and richer failure details |
| mad_scientist.py | 27,450 | 27,857 | Public start entry point delegates to graph implementation |
| run_approval.py | absent | 2,365 | Shared actionable approval state/view helpers |

Five validation scripts and three UI partials are new. Existing dashboard/workspace/schedule/run templates and CSS integrate graph/approval/provider UI. `orchestrator.py`, `prompt_library.py`, `run_chain.py`, scheduler, CLI, and several other core modules are byte-identical. The packaging module list adds the graph module but misses other needed modules.

## Sanitization

Archive contains 339 files: 50 candidates retained for review; 170 runtime-data files, 80 temporary-test files, 35 bytecode files, and 4 backups excluded. Runtime database, run logs, generated workspaces, cookies, installer fixtures, and caches were not copied into the branch. Legitimate validation scripts remain; excluded test workspaces are generated fixtures, not the validation suite.

Replaced private workspace definitions with an empty default and portable `workspaces.example.yaml`. Removed four machine-specific crew definitions from providers while keeping all seven generic provider definitions and their environment-variable credential references. Default is local_qwen. The LM Studio dummy key is a documented local placeholder, not a credential. Added ignores for runtime/private/build artifacts. Also removed main's surviving log, backups, and duplicate service nesting. No actual credential values were identified by the source scan; pattern scanning is not a guarantee that every possible secret format is detectable. Excluded runtime/private files were not republished.

## Validation

Python 3.14.2 on Windows; Flask 3.0.3, APScheduler 3.10.4, PyYAML 6.0.3, setuptools 82.0.1. The project pins PyYAML 6.0.1, so the exact pinned environment was not validated.

All five recovered scripts passed, covering DAG dependencies/fan-in/fixer retry/scout failure handling, source-context priority, root discovery, test-command detection, and tester-stage validation. The graph script stubs YAML and provider launch; it does not prove live model operation. All recovered Python files parse. All Jinja templates compile. Six test-client routes return 200: dashboard, version, demo workspace, approval alert, graph partial, schedules. Repeated initialization over main's schema creates all four graph tables successfully. This uses a fresh synthetic database, not the private recovered database.

Wheel build succeeds with no dependency resolution, but inspection confirms missing run_approval.py, prompt_library.py, orchestrator.py, tank_cli.py, templates and YAML configuration. Installed-wheel operation is therefore not validated and is expected to fail. Run the recovered source checkout for development; repair packaging before distribution.

No recovered validation failures occurred. No live API calls, credential use, external repository modifications, deployment, or merge were performed. Additional code-review concern: the retry-scout route resolves the workspace slug but does not check that graph_mission_id belongs to that workspace. Keep this for follow-up review before broader use. Persistent state does not by itself establish automatic recovery of in-flight runs after a process restart.
