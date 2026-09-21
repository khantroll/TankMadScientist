"""
CrewAI-style sequential orchestration engine for Tank.

Translates a 'crew' provider config into a chain of sub-runs, each
dispatched through the normal provider registry and lifecycle, then
waits for completion before creating the next link in the chain.

Provider config schema (providers.yaml)
----------------------------------------
  my_crew:
    type: crew
    label: "My Crew"
    process: sequential       # only supported strategy; more may follow
    agents:
      - provider: mistral_agent        # required — must exist in the registry
        task_suffix: "extra guidance"  # optional, appended to the crew task
        role_slug: security-auditor    # optional, overrides the crew run's role
      - provider: local_agent

Thread model
------------
run_sequential_crew() is always called from the daemon thread already
spawned by _start_crew() in providers.py.  It blocks that single thread
with time.sleep() polling between sub-runs.  The Flask request thread
and the queue worker are never touched — sub-runs created here go straight
through session_manager.launch_run() rather than the pending-run queue so
that the crew controls sequencing.

Import topology (no circular dependencies)
-------------------------------------------
  providers  ->  orchestrator  (deferred, inside _start_crew's thread)
  orchestrator                 (imports models / session_manager deferred,
                                inside function bodies)
  session_manager  ->  providers  (already the case; no new cycle added)

The TYPE_CHECKING guard keeps the RunContext annotation available for
static analysis without creating a runtime import from providers.
"""
from __future__ import annotations

import json
import time
import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from providers import RunContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled", "rejected"})
_SUCCESS_STATUS = "done"
_APPROVAL_STATUS = "awaiting_approval"

_POLL_INTERVAL_SECONDS: float = 2.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CrewOrchestrationError(Exception):
    """Raised when the crew configuration is invalid or execution cannot proceed."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_crew_log(log_path: str, line: str) -> None:
    """Append a line to the crew run's log file (independent of providers)."""
    with open(log_path, "a", encoding="utf-8", buffering=1) as fh:
        fh.write(line)
        if not line.endswith("\n"):
            fh.write("\n")


def _parse_agents(cfg: dict) -> list[dict]:
    """Validate and return the agents list from a crew provider config."""
    agents = cfg.get("agents") or []
    if not agents:
        raise CrewOrchestrationError(
            "Crew provider config has no 'agents' list. "
            "Add at least one entry under 'agents:' in providers.yaml."
        )
    for i, agent in enumerate(agents):
        if not agent.get("provider"):
            raise CrewOrchestrationError(
                f"agents[{i}] is missing the required 'provider' field."
            )
    return agents


def _wait_for_run(
    run_id: int,
    cancel_event: threading.Event,
    log_path: str,
    poll_interval: float = _POLL_INTERVAL_SECONDS,
) -> str:
    """
    Block the calling thread until run_id reaches a terminal status.

    Returns the final status string ("done", "failed", "cancelled",
    "rejected").  Logs a one-time notice when the sub-run enters
    awaiting_approval so the user knows human action is required.
    """
    import models

    noted_approval = False
    while not cancel_event.is_set():
        run = models.get_run(run_id)
        if run is None:
            return "failed"
        status = run["status"]
        if status in _TERMINAL_STATUSES:
            return status
        if status == _APPROVAL_STATUS and not noted_approval:
            _write_crew_log(
                log_path,
                f"[tank] crew: sub-run #{run_id} is awaiting_approval — "
                "approve or reject it in the run list to continue the sequence.\n",
            )
            noted_approval = True
        time.sleep(poll_interval)

    return "cancelled"


# ---------------------------------------------------------------------------
# Public orchestration entry point
# ---------------------------------------------------------------------------

def run_sequential_crew(
    ctx: "RunContext",
    cfg: dict,
    on_finished: Callable[[int], None],
) -> None:
    """
    Execute a sequential crew: create and run each agent in order, chaining
    output via parent_run_id, then call on_finished(exit_code).

    Must be called from within a daemon thread (e.g., the thread spawned by
    _start_crew in providers.py).  Blocks that thread for the duration.

    Context threading
    -----------------
    Sub-run 1 receives parent_run_id = ctx.parent_run_id (the crew run's own
    parent, already terminal) so that session_manager / run_chain build the
    same prior-run context the crew run itself would have used.  Sub-runs 2+
    receive parent_run_id of their immediate predecessor (terminal by the
    time each is created), giving every step a natural context chain.

    Neither mission_id nor stage_name is propagated — mission.advance_mission
    is scoped to the crew run itself, not its internal sub-steps.
    """
    import models

    try:
        agents = _parse_agents(cfg)
    except CrewOrchestrationError as exc:
        _write_crew_log(ctx.log_path, f"[tank] crew error: {exc}\n")
        on_finished(1)
        return

    process = cfg.get("process", "sequential")
    workspace_id = ctx.workspace["id"]

    _write_crew_log(
        ctx.log_path,
        f"[tank] crew: process={process} agents={len(agents)} "
        f"task={ctx.task[:72]!r}\n",
    )

    # Default role for sub-runs: inherit from the crew run.
    default_role_id: int | None = ctx.role.get("id") if ctx.role else None

    # Tool authorization note
    # -----------------------
    # Each sub-run's tool constraints are enforced through its role, which is
    # stored in the DB and resolved by session_manager.launch_run when it
    # builds the RunContext.  The full chain is:
    #   role.tools (DB)  →  RunContext.role  →
    #   prompt_library.compose_system_prompt (## Available Tools block)  →
    #   local_agent.run_post_actions / finalize_approved_run (auth filter)
    # This orchestrator does not need to thread tools explicitly; it only
    # needs to choose the correct role_id for each step (see role_slug below).
    # A log line per step surfaces any active restriction for observability.

    # Sub-run 1 chains from the crew run's own parent (or None); sub-runs 2+
    # chain from their immediate predecessor once it is terminal.
    prev_run_id: int | None = ctx.parent_run_id
    final_code = 0

    for i, agent_cfg in enumerate(agents):
        step_num = i + 1

        if ctx.cancel_event.is_set():
            _write_crew_log(
                ctx.log_path,
                f"[tank] crew: cancelled before step {step_num}\n",
            )
            on_finished(1)
            return

        provider_id: str = agent_cfg["provider"]

        # Allow each step to override the role via role_slug.
        step_role_id = default_role_id
        role_slug = (agent_cfg.get("role_slug") or "").strip()
        resolved_role_row = None
        if role_slug:
            resolved_role_row = models.get_role_by_slug(workspace_id, role_slug)
            if resolved_role_row:
                step_role_id = resolved_role_row["id"]
            else:
                _write_crew_log(
                    ctx.log_path,
                    f"[tank] crew: step {step_num} role_slug={role_slug!r} "
                    "not found — using crew run's role\n",
                )

        # Surface the active tool restriction (if any) for this step.
        import local_agent as _la
        _step_role_dict = (
            dict(resolved_role_row) if resolved_role_row
            else (ctx.role or {})
        )
        _step_tools = _la._authorized_tools(_step_role_dict)
        if _step_tools is not None:
            _write_crew_log(
                ctx.log_path,
                f"[tank] crew: step {step_num} authorized tools: {_step_tools}\n",
            )

        # Build the task: base crew task + optional per-step suffix.
        task = ctx.task
        suffix = (agent_cfg.get("task_suffix") or "").strip()
        if suffix:
            task = f"{task}\n\n{suffix}"

        # Build a persona_override JSON blob if the GUI provided custom
        # goal / backstory / tools for this step.  These override the saved
        # role's values inside session_manager.launch_run so prompt_library
        # injects them into <identity_and_role> and <available_tools>.
        _custom_goal      = (agent_cfg.get("custom_goal")      or "").strip()
        _custom_backstory = (agent_cfg.get("custom_backstory")  or "").strip()
        _custom_tools     = (agent_cfg.get("custom_tools")      or "").strip()
        _persona_override: str | None = None
        if _custom_goal or _custom_backstory or _custom_tools:
            _po: dict = {}
            if _custom_goal:
                _po["goal"] = _custom_goal
            if _custom_backstory:
                _po["backstory"] = _custom_backstory
            if _custom_tools:
                _po["tools"] = _custom_tools
            _persona_override = json.dumps(_po)
            _write_crew_log(
                ctx.log_path,
                f"[tank] crew: step {step_num} persona override: "
                + ", ".join(f"{k}={v[:32]!r}" for k, v in _po.items())
                + "\n",
            )

        # Create the sub-run.  For step 1, prev_run_id is the crew's own
        # parent (already terminal, passes _validate_parent_run).  For later
        # steps the predecessor is done by the time we reach here.
        try:
            sub_run_id = models.create_run(
                workspace_id=workspace_id,
                role_id=step_role_id,
                task=task,
                provider=provider_id,
                parent_run_id=prev_run_id,
                persona_override=_persona_override,
                crew_run_id=ctx.run_id,
            )
        except Exception as exc:
            _write_crew_log(
                ctx.log_path,
                f"[tank] crew: step {step_num} failed to create sub-run: {exc}\n",
            )
            on_finished(1)
            return

        _write_crew_log(
            ctx.log_path,
            f"[tank] crew: step {step_num}/{len(agents)} "
            f"provider={provider_id} run=#{sub_run_id}\n",
        )

        # Dispatch directly — bypasses the pending queue so we control
        # sequencing.  The queue worker will skip this run (already running).
        import session_manager as _sm
        _sm.launch_run(sub_run_id)

        # Block until the sub-run reaches a terminal state.
        final_status = _wait_for_run(sub_run_id, ctx.cancel_event, ctx.log_path)

        _write_crew_log(
            ctx.log_path,
            f"[tank] crew: step {step_num} finished status={final_status}\n",
        )

        # If the crew was cancelled while waiting, also cancel the sub-run
        # (it may still be running if cancel happened mid-poll).
        if ctx.cancel_event.is_set() or final_status == "cancelled":
            _sm.cancel_run(sub_run_id)
            on_finished(1)
            return

        if final_status != _SUCCESS_STATUS:
            final_code = 1
            _write_crew_log(
                ctx.log_path,
                f"[tank] crew: step {step_num} {final_status} — stopping sequence\n",
            )
            break

        # Next step chains from this one.
        prev_run_id = sub_run_id

    _write_crew_log(
        ctx.log_path,
        f"[tank] crew: sequence complete (exit {final_code})\n",
    )
    on_finished(final_code)
