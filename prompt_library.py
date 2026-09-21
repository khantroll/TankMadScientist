"""
Centralised prompt composition for Tank agents.

All system-prompt text that was previously scattered across local_agent.py
and providers.py lives here.  Callers assemble a final string via
compose_system_prompt(); they NEVER concatenate prompt fragments themselves.

Output schema — five XML blocks emitted in this fixed order.
Every block is OMITTED entirely when its driving parameter is absent or empty,
keeping the payload clean and tight.

  <identity_and_role>        role instructions, goal, backstory
  <context_boundaries>       workspace name, repo path, ambient config  (opt.)
  <available_tools>          authorised tool names + descriptions        (opt.)
  <execution_instructions>   stage-specific or task-specific directives  (opt.)
  <output_format_guardrails> hard output-format contract + quality guard (always)

Public API
----------
compose_system_prompt(
    kind,
    role_text=None,
    task_modifier=None,
    role=None,
    tool_descriptions=None,
    context=None,           # NEW optional — {workspace_name, repo_path, ...}
) -> str

kind values
-----------
"coding_agent"   — local_agent: expects strict JSON response
"analysis"       — markdown report for audits / reviews
"advisory"       — openai_compatible streaming advisory

role (optional dict / sqlite3.Row converted to dict)
-----------------------------------------------------
  system_prompt  — free-text role instructions (role_text takes precedence)
  goal           — one-sentence objective (CrewAI-style)
  backstory      — context shaping the agent's perspective (CrewAI-style)
  tools          — JSON-encoded list of authorised tool names

tool_descriptions (optional dict[str, str])
-------------------------------------------
Maps authorised tool names → plain-text descriptions (from TOOL_REGISTRY in
local_agent.py).  When supplied, <available_tools> includes descriptions.
When omitted, falls back to a name-only list — backward-compatible default.

context (optional dict)
-----------------------
Arbitrary key/value pairs injected into <context_boundaries>.
Recognised keys: workspace_name, repo_path.  All other keys are emitted
verbatim.  Omit the block entirely by not passing this argument.

Roles that have none of the optional fields produce output identical to the
pre-role-dict behaviour, ensuring full backward compatibility.
"""

from __future__ import annotations

import json as _json


# ---------------------------------------------------------------------------
# Internal guardrail blocks — injected into <output_format_guardrails>.
# No outer <system> wrapper; the XML section tag provides the boundary.
# Language is strictly imperative throughout.
# ---------------------------------------------------------------------------

_CODING_AGENT_GUARDRAILS = """\
You are a coding agent operating inside a Tank workspace.
You WILL receive repository file contents and a user task description.

You MUST respond with a single, valid JSON object ONLY.
CRITICAL: No markdown code fences, no explanatory prose, no text of any kind \
outside the JSON object. Any response that is not parseable JSON will cause \
the run to FAIL immediately and unconditionally.

The JSON object MUST conform to this exact schema:

{
  "response_type": "plan" | "patch",
  "summary": "brief description of your response",
  "plan": "detailed output when response_type is plan, otherwise empty string",
  "patches": [
    {"path": "relative/path/from/repo/root", "content": "full new file content"}
  ],
  "post_actions": {
    "run_tests": false,
    "test_command": "",
    "run_git_diff": false
  }
}

CRITICAL CONSTRAINTS — You WILL comply with every rule below without exception:
- You MUST follow the repository's primary language and suggested test command \
as declared in the Project profile section of the user message. \
NEVER assume Python/pytest for PowerShell, Go, Rust, or any other stack.
- You MUST use response_type "patch" when proposing concrete file changes. \
Each patch entry REPLACES the entire file content — partial diffs are FORBIDDEN.
- You WILL use response_type "plan" ONLY for tasks that require planning \
before file edits — NEVER for audits, reviews, or analytical tasks.
- You MUST set post_actions.run_tests to true ONLY when you explicitly \
require Tank to execute the test suite after applying patches.
- You MUST set post_actions.run_git_diff to true ONLY when you explicitly \
require Tank to emit a git diff after apply.
- You WILL leave both post_action flags false unless they are demonstrably \
necessary for the specific task at hand.
- You MUST use only paths relative to the repository root. \
NEVER use ".." traversal or absolute paths in any patch entry."""

_ANALYSIS_GUARDRAILS = """\
You are a senior engineer performing a structured code review inside Tank.
You WILL receive repository file contents in the user message.

You MUST write your COMPLETE analysis deliverable as markdown.
CRITICAL: Do NOT produce JSON. Markdown output only — no exceptions.

STRICTLY FORBIDDEN — Your response WILL be rejected immediately if it contains:
- Numbered procedure checklists of steps you intend to perform \
("1. Review...", "2. Check...")
- Empty template sections with bracket placeholders \
like [Project Name] or [Insert finding here]
- Preamble phrases such as "Certainly!", "Sure!", or any description of \
what you would do rather than evidence drawn from the actual code

REQUIRED SECTIONS — You MUST include all three of the following in order:

## Executive summary
2–4 sentences grounded exclusively in what you actually read in the provided files.

## Findings
Each finding MUST name a specific file (e.g. scripts/Foo.ps1) and describe \
actual observed code behaviour, security risks, or logic gaps. \
You WILL quote or paraphrase real code from the repository to support each finding.

## Recommendations
Numbered, prioritised, and immediately actionable fixes tied directly \
to the findings above.

Write your complete report now. Do not defer or summarise."""

_ADVISORY_GUARDRAILS = """\
You are an advisory assistant operating inside a Tank workspace.
You WILL receive repository file contents alongside the user's task.

CRITICAL CONSTRAINTS — You MUST comply with every rule below without exception:
- You MUST deliver substantive, actionable output grounded exclusively \
in the files provided.
- You WILL cite specific files, and functions or line numbers wherever possible.
- For audits and reviews: You MUST enumerate concrete findings first, \
then provide prioritised recommendations.
- You MUST NEVER respond with bracket placeholders like [fill in] \
or empty project template shells.
- When prior run output is included in the context, \
you WILL build directly on those findings rather than starting over.
- When the user references material not present in the provided files or prior \
output, you MUST explicitly state what is missing and analyse what you do have."""


# ---------------------------------------------------------------------------
# Quality guard — appended inside <output_format_guardrails> for all kinds.
# Placed last so it is the most recent instruction in the model's context.
# ---------------------------------------------------------------------------

_QUALITY_GUARD = """\
CRITICAL EVALUATION CRITERIA:
Your response WILL be automatically evaluated. It WILL be REJECTED immediately if it:
- Is a procedure checklist rather than a deliverable containing actual findings
- Contains three or more bracket placeholders such as [fill in here] or [your text]
- Describes what you would do rather than producing the complete deliverable

You MUST produce your complete, final deliverable now. Do not summarise. Do not defer."""


# ---------------------------------------------------------------------------
# Public composition function
# ---------------------------------------------------------------------------

def compose_system_prompt(
    kind: str,
    role_text: str | None = None,
    task_modifier: str | None = None,
    role: dict | None = None,
    tool_descriptions: dict[str, str] | None = None,
    context: dict | None = None,
) -> str:
    """
    Build a complete system prompt from up to five XML-delimited blocks.

    Output order (blocks are omitted entirely when their inputs are absent/empty):

      1. <identity_and_role>        role text, goal, backstory
      2. <context_boundaries>       workspace/path ambient config  (requires context=)
      3. <available_tools>          authorised tool list            (requires role.tools)
      4. <execution_instructions>   stage/task directives           (requires task_modifier=)
      5. <output_format_guardrails> hard output contract + quality guard  (ALWAYS present)

    Parameters
    ----------
    kind : "coding_agent" | "analysis" | "advisory"
    role_text : optional free-text role instructions (takes precedence over
                role["system_prompt"] when both are supplied)
    task_modifier : optional extra instructions for this specific task type
    role : optional full role dict (from DB via dict(sqlite3.Row)).
           Reads system_prompt (fallback when role_text is None), goal,
           backstory, and tools.  Fields absent or None are silently skipped.
    tool_descriptions : optional {tool_name: description} mapping from
           TOOL_REGISTRY in local_agent.py.  When provided, <available_tools>
           renders each tool with its description and a strict permission note.
           When None, falls back to a name-only bullet list — backward compat.
    context : optional dict of ambient workspace / path parameters to inject
           into <context_boundaries>.  Recognised keys: workspace_name,
           repo_path.  Any extra keys are emitted verbatim.
           Block is omitted when this argument is None or empty.

    Returns
    -------
    A single string ready to use as the system message.
    """
    # Select the kind-appropriate guardrails content.
    if kind == "coding_agent":
        _guardrails = _CODING_AGENT_GUARDRAILS
    elif kind == "analysis":
        _guardrails = _ANALYSIS_GUARDRAILS
    elif kind == "advisory":
        _guardrails = _ADVISORY_GUARDRAILS
    else:
        raise ValueError(
            f"Unknown prompt kind {kind!r}. "
            "Expected 'coding_agent', 'analysis', or 'advisory'."
        )

    parts: list[str] = []

    # ── Block 1: <identity_and_role> ─────────────────────────────────────────
    # Aggregate role text, goal, and backstory.  Block is omitted when all
    # three are absent or blank.
    effective_role_text = role_text
    if effective_role_text is None and role is not None:
        effective_role_text = (role.get("system_prompt") or "").strip() or None

    identity_lines: list[str] = []
    if effective_role_text:
        identity_lines.append(effective_role_text.strip())

    if role is not None:
        goal      = (role.get("goal")      or "").strip()
        backstory = (role.get("backstory") or "").strip()
        if goal:
            identity_lines.append(f"Goal: {goal}")
        if backstory:
            identity_lines.append(f"Backstory: {backstory}")

    if identity_lines:
        parts.append(
            "<identity_and_role>\n"
            + "\n\n".join(identity_lines)
            + "\n</identity_and_role>"
        )

    # ── Block 2: <context_boundaries> ────────────────────────────────────────
    # Renders ambient workspace / path context when the caller provides it.
    # Block is omitted entirely when context is None or resolves to no lines.
    if context:
        ctx_lines: list[str] = []
        _KNOWN = {"workspace_name", "repo_path"}
        if context.get("workspace_name"):
            ctx_lines.append(f"Workspace: {context['workspace_name']}")
        if context.get("repo_path"):
            ctx_lines.append(f"Repository path: {context['repo_path']}")
        for k, v in context.items():
            if k not in _KNOWN and v:
                ctx_lines.append(f"{k}: {v}")
        if ctx_lines:
            parts.append(
                "<context_boundaries>\n"
                + "\n".join(ctx_lines)
                + "\n</context_boundaries>"
            )

    # ── Block 3: <available_tools> ────────────────────────────────────────────
    # Parse the role's tools list regardless of storage format (list, JSON
    # string, or comma-separated string).  Block is omitted when the role
    # carries no tools or the parsed list is empty.
    if role is not None:
        raw_tools = role.get("tools")
        if raw_tools:
            if isinstance(raw_tools, str):
                try:
                    tool_list = _json.loads(raw_tools)
                except (_json.JSONDecodeError, TypeError):
                    tool_list = [t.strip() for t in raw_tools.split(",") if t.strip()]
            else:
                tool_list = list(raw_tools)

            if tool_list:
                if tool_descriptions:
                    # Rich format: each tool name + its plain-text description.
                    # Tools absent from tool_descriptions are still listed by
                    # name so the model has a complete authorisation picture.
                    tool_lines: list[str] = [
                        "You WILL use ONLY the following authorised tools. "
                        "Invoking any tool not listed here is STRICTLY FORBIDDEN.\n"
                    ]
                    for t in tool_list:
                        desc = tool_descriptions.get(str(t))
                        if desc:
                            tool_lines.append(f"**{t}** — {desc}")
                        else:
                            tool_lines.append(f"**{t}**")
                    parts.append(
                        "<available_tools>\n"
                        + "\n".join(tool_lines)
                        + "\n</available_tools>"
                    )
                else:
                    # Backward-compat: name-only bullet list, no descriptions.
                    parts.append(
                        "<available_tools>\n"
                        "You WILL use ONLY the following authorised tools:\n"
                        + "\n".join(f"- {t}" for t in tool_list)
                        + "\n</available_tools>"
                    )

    # ── Block 4: <execution_instructions> ────────────────────────────────────
    # Stage-specific or task-specific directives.  Omitted when absent/blank.
    if task_modifier and task_modifier.strip():
        parts.append(
            "<execution_instructions>\n"
            + task_modifier.strip()
            + "\n</execution_instructions>"
        )

    # ── Block 5: <output_format_guardrails> ──────────────────────────────────
    # Always present.  Combines the kind-specific output contract with the
    # universal quality guard so the format rules are the final instruction
    # the model sees before it generates its response.
    parts.append(
        "<output_format_guardrails>\n"
        + _guardrails
        + "\n\n"
        + _QUALITY_GUARD
        + "\n</output_format_guardrails>"
    )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public constants — derived from compose_system_prompt so they always
# reflect the current composition logic.  Other modules may import these
# by name for backward compatibility; they remain pure strings.
# ---------------------------------------------------------------------------

LOCAL_AGENT_SYSTEM_PROMPT: str = compose_system_prompt("coding_agent")
ANALYSIS_REPORT_PROMPT: str    = compose_system_prompt("analysis")
ADVISORY_SYSTEM_PROMPT: str    = compose_system_prompt("advisory")
