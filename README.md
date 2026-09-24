# Tank

A self-hosted command center for running several AI agents in parallel,
organized by workspace — your own version of the "Tank" concept, built
on the stack you already use (Flask + SQLite + HTMX), not a copy of
anyone's product.

## What it does

- **Workspaces** = a project directory + git remote + a set of agent
  roles, defined in `workspaces.yaml` (git-trackable, like your other
  configs) and synced into SQLite on startup.
- **Roles** = a name + system prompt, e.g. "Risk reviewer" or
  "UI critic". Purely a convenience for repeating the same kind of
  task against a workspace.
- **Providers** = pluggable backends defined in `providers.yaml`:
  - **Claude Code** — full external coding agent with repo tools
  - **OpenAI-compatible** — chat/log-only advisory mode (LM Studio, OpenRouter)
  - **Local agent** — Tank-controlled Qwen runner: reads repo files, proposes
    patches, waits for your approval, applies changes, and runs tests/git diff
    only when the model explicitly requests them
- **Runs** = one task, queued in SQLite, picked up by a background
  worker. Claude Code runs shell out to the CLI; API providers stream
  chat completions to a per-run log file that the dashboard tails live
  via HTMX polling.
- **Schedules** = cron expressions (APScheduler) that just create a
  pending run on trigger — scheduled jobs go through the exact same
  path as manually-launched ones.
- **Git sync** — pull / push / status buttons per workspace.

A hard cap (`TANK_MAX_PARALLEL_RUNS`, default 3) limits how many
agents run at once across all workspaces, so you don't accidentally
fork-bomb your own machine.

## Requirements

- Python 3.10 or newer
- For **Claude Code** runs: [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)
  on your `PATH` (or set `TANK_CLAUDE_BIN`)
- For **LM Studio** / hosted APIs: a running OpenAI-compatible endpoint
  and any API keys your provider needs (see `providers.yaml`)
- `git` on your `PATH` if you use the git sync buttons

## Installing

From the project directory, use a virtual environment (recommended) or
any Python install you already have:

```bash
cd tank
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

Or install the package in editable mode (adds a `tank` command on your PATH):

```bash
pip install -e .
```

## Running

```bash
python app.py
# or, after pip install -e .
tank
```

Then open http://127.0.0.1:8742.

The included `workspaces.yaml` has example paths using `~/projects/...`
— edit them to point at your real repos, then hit **Reload config** on
the dashboard, or just restart the app. Paths support `~` and
environment variables (e.g. `%USERPROFILE%\projects\foo` on Windows).

## Configuration

All via environment variables (see `config.py`):

| Variable | Default | Purpose |
|---|---|---|
| `TANK_DATA_DIR` | `./data` | SQLite DB + run logs |
| `TANK_WORKSPACES_FILE` | `./workspaces.yaml` | workspace/role definitions |
| `TANK_PROVIDERS_FILE` | `./providers.yaml` | model/provider backends |
| `TANK_DEFAULT_PROVIDER` | from `providers.yaml` | override default provider id |
| `TANK_MISSION_TEMPLATES_FILE` | `./mission_templates.yaml` | mission stage definitions |
| `TANK_DEFAULT_MAX_FIX_LOOPS` | `3` | fallback fix-loop retry cap |
| `TANK_CLAUDE_BIN` | `claude` | path to the Claude Code CLI |
| `TANK_MAX_PARALLEL_RUNS` | `3` | global concurrency cap |
| `TANK_MAX_PARALLEL_RUNS_PER_MISSION` | `0` | extra Mad Scientist cap per mission; `0` disables it |
| `TANK_POLL_INTERVAL` | `2` | seconds between queue checks |
| `TANK_HOST` / `TANK_PORT` | `127.0.0.1` / `8742` | bind address |

## Providers

Backends are defined in `providers.yaml`. Three are included out of the box:

| Provider id | Type | Use case |
|---|---|---|
| `claude_code` | Claude Code CLI | Full external coding agent |
| `lmstudio_qwen` | OpenAI-compatible | Advisory chat/log-only via LM Studio |
| `local_qwen` | Local agent | Tank-controlled Qwen via LM Studio |
| `openrouter` | OpenAI-compatible | Advisory chat/log-only via OpenRouter |
| `openrouter_agent` | Local agent | Tank-controlled agent via OpenRouter |
| `mistral` | OpenAI-compatible | Advisory chat/log-only via Mistral API |
| `mistral_agent` | Local agent | Tank-controlled agent via Mistral API (Codestral) |

### Local agent workflow

When you select a `local_agent` provider (e.g. `local_qwen` or `openrouter_agent`):

1. Tank reads the most relevant files from the workspace repo
2. File context + your task are sent to the model
3. The model returns a **plan** (logged and done) or **patches** (paused for approval)
4. You review the proposed file changes and click **Approve & apply** or **Reject**
5. On approval, Tank applies the patches
6. Tests and `git diff` run **only if the model set** `post_actions.run_tests` or
   `post_actions.run_git_diff` to `true` in its JSON response — Tank does not
   run them otherwise

Example `local_agent` entries (already in `providers.yaml`):

```yaml
local_qwen:
  type: local_agent
  label: Qwen local agent (Tank-controlled)
  base_url: http://127.0.0.1:1234/v1
  model: qwen/qwen3.5-35b-a3b
  api_key_env: TANK_LMSTUDIO_API_KEY
  api_key_default: lm-studio
  max_context_files: 12
  max_file_bytes: 32000

openrouter_agent:
  type: local_agent
  label: OpenRouter agent (Tank-controlled)
  base_url: https://openrouter.ai/api/v1
  model: qwen/qwen-2.5-72b-instruct
  api_key_env: OPENROUTER_API_KEY
  max_context_files: 12
  max_file_bytes: 32000
```

Pick a provider when queuing a run or schedule. Set a workspace-level
default with `default_provider` in `workspaces.yaml`, or a global
default with `default_provider` in `providers.yaml`.

For advisory-only chat, use `lmstudio_qwen`, `openrouter`, or `mistral`.
For the patch-approval agent workflow, use `local_qwen`, `openrouter_agent`, or `mistral_agent`.

Set API keys in your environment before starting Tank:

```powershell
# OpenRouter
$env:OPENROUTER_API_KEY = "sk-or-..."

# Mistral (https://console.mistral.ai/api-keys)
$env:MISTRAL_API_KEY = "..."
```

To persist on Windows: System Properties → Environment Variables → User → New.

**Note:** `openai_compatible` providers return chat text only. `local_agent`
providers can propose and apply file patches (with your approval) but are not
as capable as Claude Code for complex multi-step agent work.

## Missions

A mission turns one goal into a sequence of stages — by default
`plan → build → test → fix-loop`, defined in `mission_templates.yaml`.
Each stage is just a normal run, chained off the previous stage's run
the same way the manual "Continue from run" picker works, so the
plan's output flows into the builder, the builder's patch flows into
the tester, and so on automatically.

Start one from a workspace page: pick a template, describe the goal,
hit **Start mission**. Tank then runs stages on its own — except:

**Every patch still pauses for your approval, including every
fix-loop attempt.** A mission never writes to disk without you
clicking Approve. If the tester stage fails, Tank automatically queues
a `fixer` stage chained off the failure, but that fixer's patch waits
for approval exactly like any other — the loop can propose changes
on its own, it cannot apply them on its own.

The fix-loop is bounded by `max_fix_loops` (per-template, defaults to
`TANK_DEFAULT_MAX_FIX_LOOPS`, default `3`). Once exhausted, the mission
stops with `status: failed` and an explanatory note rather than
retrying forever.

The `tester` stage is deterministic, not model-judged: its task tells
the model to request `post_actions.run_tests` rather than propose
patches, and Tank runs that exact command and uses its real exit code
to decide pass/fail. (This required one change to `local_agent.py`'s
existing `is_analysis_task` leniency: that heuristic is great for
manual audit/review roles where a slightly-off response should still
produce a useful report, but a mission's tester/builder/fixer stages
need real done/failed signal to drive the fix-loop, so mission-linked
runs always go through the strict patch/plan schema — see the comment
above `if ctx.mission_id is None and repo_context.is_analysis_task(...)`
in `local_agent.py` if you want the full reasoning.)

Add your own template by adding an entry to `mission_templates.yaml`.
The one non-obvious rule: `next_on_success` should be set explicitly
on any stage whose YAML list position isn't also its real successor —
see the comment block at the top of that file. The included template
already hits this case (`tester`'s `next_on_success: null` stops the
mission on success, rather than falling through to the recovery-only
`fixer` stage that's listed after it).

### Mad Scientist missions

The workspace page also includes **Start Mad Scientist Mission**. This
mode does not use a fixed YAML stage list. Tank creates a compatibility
parent run, queues a first `Scout / Repo Cartographer` attempt, then
parses the scout's structured JSON execution plan into durable graph
rows owned by `mad_scientist_graph.py`:

- `mad_scientist_missions` stores the graph mission, including an optional
  `spend_cap_usd` and the spend accumulated from provider-reported
  `cost_usd`
- `mad_scientist_steps` stores logical planned steps and their status
- `mad_scientist_step_dependencies` stores DAG edges
- `mad_scientist_attempts` maps normal Tank runs to attempts against a
  logical step (`scout`, `execute`, `fixer`, or `retry`)
- `evaluations` stores a pass/fail critique when a tester or reviewer
  attempt finishes

`parent_run_id` remains compatibility metadata. Generated steps run from
the graph. Tank queues every ready logical step whose dependency edges
point only to completed steps. The global `TANK_MAX_PARALLEL_RUNS` cap
still decides how many attempt runs actually execute at once. Set
`TANK_MAX_PARALLEL_RUNS_PER_MISSION` when one wide mission should not
occupy every slot. The mission form's Parallel steps field stores
`mad_scientist_missions.max_parallel_steps` for that mission only; leave
it blank for no extra limit. When both are set, the smaller one applies. When a step has multiple dependencies, Tank sets
`run.parent_run_id` to the newest completed dependency run, and injects
localized context from **all direct dependency steps** into the task.

Step status follows the attempt run, including `awaiting_approval`, so
the graph does not stay `queued` while a patch is waiting. Approve and
reject controls for that run are on the graph row.

If a generated step fails, Tank creates a bounded fixer attempt on the
**same** logical step, then a retry attempt on that step after the fixer
succeeds. The cap comes from the form's Fix loops field, falling back to
`TANK_DEFAULT_MAX_FIX_LOOPS`. A spend cap stops further scheduling once
recorded cost reaches it. Providers that do not report usage cost do not
increment spend.

Mad Scientist runs also update local `workspace_memory` in SQLite with
project profile facts, important paths, changed paths, successful step
summaries, and known pitfalls. Future Mad Scientist scouts for the same
workspace receive that memory in their prompt.

A process restart marks leftover `running` rows failed and advances their
missions through the normal failure path. `awaiting_approval` rows are
left in place. That sweep does not auto-resume the interrupted run.

Run `python scripts/validate_mad_scientist_graph.py` for a lightweight
SQLite-only validation of graph storage, dependency scheduling, localized
dependency context, fixer/retry attempts, spend cap, approval sync,
restart sweep, and workspace ownership checks.

## Production deployment

Tank is a plain Flask app. For a long-running server behind a reverse
proxy, use a single gunicorn worker (the queue worker thread and
in-process scheduler live in one process):

```bash
pip install -e ".[production]"
gunicorn -w 1 -b 127.0.0.1:8742 app:app
```

Raise `TANK_MAX_PARALLEL_RUNS` for more agent throughput rather than
adding gunicorn workers. Put nginx, Caddy, or another reverse proxy in
front if you expose it beyond localhost.

## Extending it

This is a working skeleton, not a finished product. Things you'll
probably want next:

- **MCP wiring**: `workspace.mcp_config_path` is passed to Claude Code
  via `--mcp-config`. API providers ignore it.
- **Auth**: there isn't any. Fine on localhost/behind a VPN; add
  Flask-Login or put it behind your SSO/reverse-proxy auth before
  exposing it. Git push/pull and local-agent patch approval should stay
  on your own machine until that auth exists.
- **Structured log parsing**: runs currently store raw
  `stream-json` lines. Parsing those into distinct tool-call /
  text-block UI elements (instead of one raw text blob) is the
  natural next step once you're using this daily.
- **Per-role default tasks**: `agent_roles.default_task` exists in
  the schema and YAML but isn't wired into the UI yet — would let you
  one-click-launch a role's standard task instead of typing it out.
