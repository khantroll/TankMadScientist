# Tank PM — Session Log
**Date:** 2026-06-24  
**Role sequence:** Senior Frontend Engineer → Senior UI/UX Developer → Principal AI Prompt Engineer

---

## Prompt 1 — Crew Hierarchy Dashboard UI

### Request summary
Render composite multi-agent "Crew" runs as collapsible parent tree nodes in the
workspace run list, with nested live-polling child-step cards, chevron toggles,
authorized-tool badges, and HTMX state preservation across polls.

### Files changed

#### `models.py`
- Added `list_child_runs(parent_run_id: int)` — returns direct children of a
  crew run ordered by creation time.

#### `app.py`
- Added `_crew_provider_ids() -> set` helper — builds the set of provider IDs
  whose `type == "crew"` from the live registry.
- Added `GET /runs/<int:run_id>/children` endpoint (`run_children`) — fetches
  child rows, resolves each child's role, calls
  `local_agent._authorized_tools(role)`, and passes `tools` to the template.
- Updated `run_list_partial`, `create_run`, and `workspace_detail` to pass
  `crew_provider_ids` to `run_list.html`.

#### `templates/partials/run_list.html`
- Builds `ns.crew_ids` and `ns.child_ids` Jinja2 namespace sets at top.
- Skips runs in `ns.child_ids` from the main loop (they render nested instead).
- Crew run cards get an amber `▶` chevron button wired to
  `toggleCrewChildren(runId)` and a `run-meta-crew-badge` pill.
- Added `#crew-children-{id}` div with `hx-preserve="true"` — survives every
  main-list poll intact, preserving open/closed state and inner polling.
- Inner `#crew-children-inner-{id}` fires `hx-trigger="load[, every 3s]"` against
  `/runs/{id}/children`.

#### `templates/partials/run_children.html` *(new)*
- Returns the innerHTML of `#crew-children-inner-{run_id}`.
- For active crews: re-injects a hidden polling `<div>` with `hx-trigger="every 3s"`;
  omitted when run is done, causing polling to stop organically.
- Each child row shows: step number chip, status badge, provider, tool badges
  (green outline), truncated task, view/cancel buttons.
- Child output slots use `#run-output-{child.id}` with `hx-preserve="true"`.

#### `static/css/style.css`
New rules: `.run-card--crew`, `.btn-icon`, `.crew-chevron` / `--open` rotation,
`.run-meta-crew-badge`, `.crew-children`, `.crew-child-list`, `.crew-child-run`
with connector dot, `.crew-step-num`, `.tool-badge` (green), `.crew-loading`.

#### `static/js/tank.js`
- `toggleCrewChildren(runId)` — flips display, rotates amber chevron.
- `htmx:afterSwap` on `#run-list` — re-syncs crew chevrons after every poll
  because the run-head is re-rendered but the preserved container retains its
  open/closed state.

#### `templates/base.html`
- CSS cache-bust: `?v=3 → ?v=4`; JS cache-bust: `?v=1 → ?v=2`.

---

## Prompt 2 — Compact Accordion Run List

### Request summary
Drastically reduce run card height by collapsing all output into a hidden
accordion section, showing only a single-row header per card (status dot,
provider, run ID, mission badge, truncated task, chevron). Preserve all
functional buttons inline. Maintain collapsed state across HTMX polls.

### Files changed

#### `templates/partials/run_list.html`
- Each card now has `.run-card--compact` and a single `.run-head--toggle` row
  (`onclick="toggleRunOutput(id)"`).
- Layout: `[status-dot][status-label][#id][provider][crew-badge?][mission-chip?]` |
  `[task…ellipsis]` | `[crew-chevron?][cancel×?][output-chevron▶]`.
- Old multi-row structure (meta row, task row, buttons row) removed.
- `cancel` button now uses `event.stopPropagation()` and `hx-on::after-request=
  "openRunOutput(id)"`.
- Output slot starts `style="display:none"`; `hx-preserve="true"` preserves
  its display state (open/closed) across every main-list poll.

#### `templates/partials/run_children.html`
- Child rows updated to the same compact single-row design.
- Output slot IDs unified to `#run-output-{child.id}` (was `#crew-child-output-*`)
  so `toggleRunOutput()`, scroll preservation, and auto-expand work without
  separate code paths.

#### `static/css/style.css`
New rules: `.run-head--toggle` (pointer cursor, hover tint), `.status-dot` +
`--*` variants (solid 8 px circles), `.status-label` + `--*` variants
(10 px uppercase mono), `.rh-dim` / `.rh-provider` (muted metadata),
`.run-mission-chip` (amber bordered), `.run-head-task-wrap` + `.run-task--compact`
(flex-fill + `text-overflow: ellipsis`), `.run-head-right`, `.output-chevron`,
`.btn-xs`, and an `awaiting_approval` amber-glow selector.

#### `static/js/tank.js` *(full rewrite)*
Reorganised into three named sections with JSDoc comments:

1. **Scroll preservation** (unchanged logic, now skips hidden slots via
   `el.clientHeight === 0` guard).

2. **Run-output accordion**
   - `_setRunOutputOpen(runId, open)` — central helper: sets
     `slot.style.display`, rotates `#output-chevron-{id}` via `.crew-chevron--open`,
     toggles `.output-open` on the card, fires a one-shot HTMX GET for completed
     runs whose slot is still empty.
   - `toggleRunOutput(runId)` — reads current display state, calls
     `_setRunOutputOpen` to flip it.
   - `openRunOutput(runId)` — forces open; called by cancel/approve callbacks.

3. **Crew-children tree** — `toggleCrewChildren(runId)` unchanged.

4. **`htmx:afterSwap` on `#run-list`** — after each poll:
   - Restores scroll in visible output slots.
   - Syncs `#output-chevron-*` from preserved slot display states.
   - Syncs `#crew-chevron-*` from preserved crew-children container states.
   - Auto-expands any `awaiting_approval` card whose slot has content but is
     still hidden; tracked per run in `_autoExpandedRuns` (Set) to prevent
     fighting the user after they manually collapse.

#### `templates/base.html`
- CSS: `?v=4 → ?v=5`; JS: `?v=2 → ?v=3`.

---

## Prompt 3 — Prompt Library XML Schema Rewrite

### Request summary
Rewrite `compose_system_prompt` to emit five strictly-ordered, omit-if-empty
XML blocks following production prompt-engineering standards, using imperative
language (`You MUST`, `You WILL`, `CRITICAL`) throughout. Maintain exact
backward compatibility across call signatures.

### Files changed

#### `prompt_library.py` *(full rewrite)*

**New internal guardrail constants** (no outer `<system>` wrapper, imperative register):

| Constant | Kind | Content highlights |
|---|---|---|
| `_CODING_AGENT_GUARDRAILS` | `"coding_agent"` | JSON-only schema, CRITICAL constraints, no-`.."` path rule |
| `_ANALYSIS_GUARDRAILS` | `"analysis"` | STRICTLY FORBIDDEN list, REQUIRED SECTIONS mandate |
| `_ADVISORY_GUARDRAILS` | `"advisory"` | CRITICAL CONSTRAINTS, cite-specific-files mandate |

`_QUALITY_GUARD` updated to full imperative language: "WILL be REJECTED immediately".

**`compose_system_prompt` five-block schema:**

| # | XML tag | Emitted when |
|---|---|---|
| 1 | `<identity_and_role>` | any of `role_text`, `role.system_prompt`, `role.goal`, `role.backstory` non-empty |
| 2 | `<context_boundaries>` | `context` dict provided with at least one value |
| 3 | `<available_tools>` | `role.tools` parses to a non-empty list |
| 4 | `<execution_instructions>` | `task_modifier` is non-empty |
| 5 | `<output_format_guardrails>` | **Always** — guardrails constant + quality guard |

**New `context` parameter** — optional `dict | None = None`. Recognised keys:
`workspace_name`, `repo_path`; arbitrary extras emitted verbatim. Default `None`
means all five existing call-sites in `local_agent.py` and `providers.py` are
unaffected with zero changes.

**Public aliases** now derived from live calls so they never drift:
```python
LOCAL_AGENT_SYSTEM_PROMPT = compose_system_prompt("coding_agent")
ANALYSIS_REPORT_PROMPT    = compose_system_prompt("analysis")
ADVISORY_SYSTEM_PROMPT    = compose_system_prompt("advisory")
```

**10-assertion test suite** written, run, and all assertions passed; test file
deleted after verification.

---

## Cumulative file inventory (all 3 prompts)

| File | Status |
|---|---|
| `models.py` | Modified |
| `app.py` | Modified |
| `templates/partials/run_list.html` | Modified (twice) |
| `templates/partials/run_children.html` | Created → Modified |
| `templates/base.html` | Modified (twice) |
| `static/css/style.css` | Modified (twice) |
| `static/js/tank.js` | Modified (twice) |
| `prompt_library.py` | Fully rewritten |
