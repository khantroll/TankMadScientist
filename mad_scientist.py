"""Mad Scientist Mission v0.2 orchestration.

This module turns one broad goal into a scout run followed by generated,
sequential child runs. It never edits repository files directly; all writes
still happen through normal provider runs and the existing approval flow.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone

import config
import mad_scientist_graph as graph
import models
import pattern_synthesis
import repo_context

TEMPLATE_ID = "mad_scientist"
PARENT_PROVIDER = "mad_scientist"
SCOUT_STAGE = "Scout / Repo Cartographer"
MEMORY_KEY = "mad_scientist"

WRITE_ROLES = {"builder", "fixer", "integrator"}
VALID_ROLES = WRITE_ROLES | {"scout", "architect", "tester", "reviewer"}
TERMINAL_FAILURE_STATUSES = {"failed", "rejected", "cancelled"}
ACTIVE_STATUSES = {"pending", "running", "awaiting_approval"}
_DAG_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_parent_log(mission, line: str) -> None:
    parent_id = mission["parent_run_id"]
    if not parent_id:
        return
    parent = models.get_run(parent_id)
    if parent is None:
        return
    log_path = parent["log_path"] or os.path.join(config.LOGS_DIR, f"run-{parent_id}.log")
    with open(log_path, "a", encoding="utf-8", buffering=1) as f:
        f.write(line)
        if not line.endswith("\n"):
            f.write("\n")
    if not parent["log_path"]:
        models.update_run(parent_id, log_path=log_path)


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    last_error = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    raise ValueError(f"Could not parse execution-plan JSON: {last_error}")


def _scout_output(run) -> str:
    payload = models.get_run_payload(run["id"])
    if payload:
        plan = payload.get("plan")
        if plan:
            return plan
        summary = payload.get("summary")
        if summary:
            return summary
    if run["log_path"] and os.path.exists(run["log_path"]):
        with open(run["log_path"], encoding="utf-8", errors="replace") as f:
            return f.read()
    return ""


def _validate_plan(raw_plan: dict, default_provider: str, max_steps: int | None) -> dict:
    if not isinstance(raw_plan, dict):
        raise ValueError("Execution plan must be a JSON object")
    steps = raw_plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Execution plan must include a non-empty steps array")

    normalized = {
        "summary": str(raw_plan.get("summary") or "").strip(),
        "project_profile": raw_plan.get("project_profile") or {},
        "steps": [],
    }

    seen = set()
    limit = max_steps if max_steps and max_steps > 0 else None
    for item in steps:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        role = str(item.get("role") or "builder").strip().lower()
        if role not in VALID_ROLES:
            role = "builder"
        depends_on = item.get("depends_on") or []
        if not isinstance(depends_on, list):
            depends_on = []
        normalized["steps"].append({
            "name": name,
            "role": role,
            "provider": str(item.get("provider") or default_provider or "").strip(),
            "task": str(item.get("task") or "").strip(),
            "depends_on": [str(dep).strip() for dep in depends_on if str(dep).strip()],
            "write_allowed": bool(item.get("write_allowed", role in WRITE_ROLES)),
            "success_criteria": [
                str(crit).strip()
                for crit in (item.get("success_criteria") or [])
                if str(crit).strip()
            ],
        })
        seen.add(name)
        if limit and len(normalized["steps"]) >= limit:
            break

    if not normalized["steps"]:
        raise ValueError("Execution plan did not contain any usable steps")
    graph.assert_acyclic(normalized["steps"])
    return normalized


def _ordered_steps(plan: dict) -> list[dict]:
    steps = plan.get("steps") or []
    by_name = {step["name"]: step for step in steps}
    emitted: list[dict] = []
    emitted_names = set()

    while len(emitted) < len(steps):
        progressed = False
        for step in steps:
            if step["name"] in emitted_names:
                continue
            deps = [dep for dep in step.get("depends_on", []) if dep in by_name]
            if all(dep in emitted_names for dep in deps):
                emitted.append(step)
                emitted_names.add(step["name"])
                progressed = True
        if not progressed:
            for step in steps:
                if step["name"] not in emitted_names:
                    emitted.append(step)
                    emitted_names.add(step["name"])
            break
    return emitted


def _step_by_name(plan: dict, name: str | None) -> dict | None:
    if not name:
        return None
    for step in plan.get("steps") or []:
        if step.get("name") == name:
            return step
    return None


def _memory_prompt(workspace_id: int) -> str:
    memory = models.get_workspace_memory(workspace_id, MEMORY_KEY)
    if not memory:
        return "No prior workspace memory has been recorded for mad_scientist runs."
    return json.dumps(memory, indent=2, sort_keys=True)


def _scout_task(workspace, goal: str, provider: str | None, max_steps: int | None) -> str:
    profile = repo_context.detect_project_profile(workspace["repo_path"])
    all_files = repo_context.list_repo_files(workspace["repo_path"])
    files_preview = "\n".join(f"- {path}" for path in all_files[:160])
    max_steps_text = max_steps if max_steps and max_steps > 0 else 6
    memory = _memory_prompt(workspace["id"])
    provider_text = provider or workspace["default_provider"] or "default provider"

    return f"""
You are the Scout / Repo Cartographer for a Tank Mad Scientist Mission.

User goal:
{goal}

Repository path:
{workspace["repo_path"]}

Detected project profile:
{json.dumps(profile, indent=2, sort_keys=True)}

Known repository files:
{files_preview or "(no text files detected)"}

Workspace memory from prior mad_scientist runs:
{memory}

Produce a real execution plan grounded in this repository. Inspect the provided
profile and file list. Do not return a generic checklist.

You MUST return the normal Tank local_agent JSON envelope with:
- response_type: "plan"
- summary: a concise plan summary
- plan: a STRING containing valid JSON for this exact execution-plan schema:
  {{
    "summary": "...",
    "project_profile": {{
      "project_type": "...",
      "important_paths": ["..."],
      "entry_points": ["..."],
      "test_commands": ["..."],
      "implementation_risks": ["..."]
    }},
    "steps": [
      {{
        "name": "...",
        "role": "scout|architect|builder|tester|fixer|integrator|reviewer",
        "provider": "{provider_text}",
        "task": "...",
        "depends_on": ["optional prior step names"],
        "write_allowed": true,
        "success_criteria": ["..."]
      }}
    ]
  }}
- patches: []
- post_actions: {{"run_tests": false, "test_command": "", "run_git_diff": false}}

Plan rules:
- Recommend at most {max_steps_text} execution steps after this scout.
- Include builder/integrator steps when the goal requires implementation.
- Builder tasks must make concrete repo changes when implementation is needed.
- Tester/reviewer tasks must verify actual files changed by prior steps.
- Scout, tester, and reviewer steps should have write_allowed false unless there
  is a specific reason to allow writes.
""".strip()


def _step_task(goal: str, step: dict, previous_names: list[str]) -> str:
    criteria = "\n".join(f"- {item}" for item in step.get("success_criteria") or [])
    prior = ", ".join(previous_names) if previous_names else "the scout output"
    role = step["role"]
    write_allowed = bool(step.get("write_allowed"))

    write_contract = (
        "You are allowed to propose concrete file changes. Do not only audit. "
        "Do not only produce a plan. If this step requires implementation, return "
        "response_type \"patch\" using the existing Tank local_agent schema. Each "
        "patch must contain the full replacement content for the file."
        if write_allowed and role in WRITE_ROLES
        else "Do not propose file changes unless the task explicitly proves they are required. "
             "Prefer response_type \"plan\" with concrete verification findings."
    )

    verify_contract = (
        "Verify the actual files changed by prior steps with a real command. "
        "Set post_actions.run_tests true and post_actions.test_command to a test "
        "or build command, or set post_actions.run_git_diff true. Tank completes "
        "this step only when that command's exit code is 0. A summary that says "
        "the checks passed does not count."
        if role in {"tester", "reviewer"}
        else ""
    )

    return f"""
Mad Scientist Mission step: {step["name"]}
Role: {role}
Write allowed: {str(write_allowed).lower()}

Original user goal:
{goal}

This step depends on: {prior}

Task:
{step["task"]}

Success criteria:
{criteria or "- Complete the step with repository-grounded evidence."}

Execution requirements:
- Build on the prior run context Tank includes below.
- {write_contract}
- {verify_contract}
- Return a complete Tank local_agent JSON response and no markdown outside JSON.
""".strip()


def _fixer_step(failed_step: dict | None, failed_run, attempt: int, max_loops: int) -> dict:
    failed_name = failed_run["stage_name"] or "previous step"
    original_task = (failed_step or {}).get("task") or failed_run["task"]
    provider = (failed_step or {}).get("provider") or failed_run["provider"]
    return {
        "name": f"Fix {failed_name} (attempt {attempt})",
        "role": "fixer",
        "provider": provider,
        "task": (
            f"The prior generated step '{failed_name}' failed. Diagnose the failure using "
            "the prior run context Tank provides, then make the smallest concrete repo "
            "changes needed to unblock the original step. Do not only audit and do not "
            "return a generic plan when implementation is required.\n\n"
            f"Original failed task:\n{original_task}\n\n"
            f"This is fix attempt {attempt} of {max_loops}. Return patch JSON through "
            "the normal Tank local_agent schema when file changes are needed."
        ),
        "depends_on": [failed_name],
        "write_allowed": True,
        "success_criteria": [
            "The specific failure from the prior run is addressed.",
            "Any proposed patches are minimal and directly related to the failed step.",
            "Relevant tests or diff checks are requested when useful.",
        ],
    }


def _attempt_staffing(workspace, mission, step: dict) -> tuple[int | None, str | None, str | None]:
    """Role, provider, and persona for one Mission Control attempt.

    Lab staffing is read from the mission synthesis record. The run itself
    is still created with models.create_run.
    """
    provider = step.get("provider") or mission["provider"]
    binding = pattern_synthesis.binding_for_step(mission["id"], step)
    if binding is None:
        role = models.get_role_by_slug(workspace["id"], role_slug_for_step(step))
        return (role["id"] if role else None, provider, None)
    if binding.get("provider"):
        provider = binding["provider"]
    role_id = None
    if binding.get("use_saved_role"):
        role = models.get_role_by_slug(
            workspace["id"], binding.get("slug") or role_slug_for_step(step)
        )
        role_id = role["id"] if role else None
    return role_id, provider, binding.get("persona_override")


def _create_child_run(workspace, mission, step: dict, parent_run_id: int | None) -> int:
    task = _step_task(mission["goal"], step, _completed_step_names(mission))
    role_id, provider, persona = _attempt_staffing(workspace, mission, step)
    return models.create_run(
        workspace["id"],
        role_id,
        task,
        provider=provider,
        parent_run_id=parent_run_id,
        mission_id=mission["id"],
        stage_name=step["name"],
        persona_override=persona,
        crew_run_id=mission["parent_run_id"],
    )


def _log_pattern_synthesis(mission, synthesis: dict) -> None:
    crew = (synthesis or {}).get("crew") or {}
    action = crew.get("action")
    if action in (None, "", "none"):
        return
    _append_parent_log(
        mission,
        "[tank] pattern synthesis "
        f"{action} crew '{crew.get('name')}' "
        f"confirmation={crew.get('confirmation')}",
    )
    if crew.get("rationale"):
        _append_parent_log(mission, f"[tank] pattern synthesis rationale: {crew['rationale']}")
    for change in crew.get("changes") or []:
        _append_parent_log(mission, f"[tank] pattern synthesis change: {change}")


def role_slug_for_step(step: dict) -> str:
    role = step.get("role")
    return role if role in VALID_ROLES else ""


def _completed_step_names(mission) -> list[str]:
    names = []
    for run in models.list_mission_runs(mission["id"]):
        if run["status"] == "done" and run["stage_name"]:
            names.append(run["stage_name"])
    return names


def _active_step_names(mission) -> set[str]:
    return {
        run["stage_name"]
        for run in models.list_mission_runs(mission["id"])
        if run["status"] in ACTIVE_STATUSES and run["stage_name"]
    }


def _active_runs(mission) -> list:
    return [
        run for run in models.list_mission_runs(mission["id"])
        if run["status"] in ACTIVE_STATUSES
    ]


def _planned_step_names(plan: dict) -> set[str]:
    return {step["name"] for step in plan.get("steps") or [] if step.get("name")}


def _ready_steps(plan: dict, mission) -> list[dict]:
    completed = set(_completed_step_names(mission))
    active = _active_step_names(mission)
    ready = []
    for step in _ordered_steps(plan):
        name = step["name"]
        if name in completed or name in active:
            continue
        deps = [dep for dep in step.get("depends_on", []) if dep]
        if all(dep in completed or dep == SCOUT_STAGE for dep in deps):
            ready.append(step)
    return ready


def _latest_done_run_for_stage(mission_id: int, stage_name: str | None):
    if not stage_name:
        return None
    matches = [
        run for run in models.list_mission_runs(mission_id)
        if run["stage_name"] == stage_name and run["status"] == "done"
    ]
    return matches[-1] if matches else None


def _latest_done_run(mission_id: int):
    matches = [
        run for run in models.list_mission_runs(mission_id)
        if run["status"] == "done"
    ]
    return matches[-1] if matches else None


def _dependency_parent_run_id(mission, step: dict, fallback_run_id: int | None = None) -> int | None:
    fixer_prefix = f"Fix {step['name']} "
    fixer_runs = [
        run for run in models.list_mission_runs(mission["id"])
        if run["status"] == "done" and str(run["stage_name"] or "").startswith(fixer_prefix)
    ]
    if fixer_runs:
        return fixer_runs[-1]["id"]
    deps = [dep for dep in step.get("depends_on", []) if dep and dep != SCOUT_STAGE]
    for dep in reversed(deps):
        dep_run = _latest_done_run_for_stage(mission["id"], dep)
        if dep_run is not None:
            return dep_run["id"]
    scout_run = _latest_done_run_for_stage(mission["id"], SCOUT_STAGE)
    if scout_run is not None:
        return scout_run["id"]
    return fallback_run_id


def _schedule_ready_steps(workspace, mission, plan: dict, fallback_run_id: int | None = None) -> list[int]:
    queued = []
    for step in _ready_steps(plan, mission):
        parent_id = _dependency_parent_run_id(mission, step, fallback_run_id=fallback_run_id)
        child_id = _create_child_run(workspace, mission, step, parent_run_id=parent_id)
        queued.append(child_id)
        _append_parent_log(
            mission,
            f"[tank] queued ready DAG step: {step['name']} "
            f"provider={step.get('provider')} run=#{child_id} parent=#{parent_id or 'none'}",
        )
    return queued


def _planned_steps_done(plan: dict, mission) -> bool:
    completed = set(_completed_step_names(mission))
    return _planned_step_names(plan).issubset(completed)


def _terminal_runs_for_mission(mission_id: int) -> list:
    return [
        run for run in models.list_mission_runs(mission_id)
        if run["status"] in ("done", "failed", "rejected", "cancelled")
    ]


def start_mission(
    workspace_id: int,
    goal: str,
    provider: str | None = None,
    max_steps: int | None = None,
    max_fix_loops: int | None = None,
    spend_cap_usd: float | None = None,
    max_parallel_steps: int | None = None,
    token_cap: int | None = None,
) -> int:
    workspace = models.get_workspace_by_id(workspace_id)
    if workspace is None:
        raise ValueError("Workspace not found")
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("Goal is required")

    max_steps = max_steps if max_steps and max_steps > 0 else 6
    max_fix_loops = max_fix_loops if max_fix_loops is not None else config.DEFAULT_MAX_FIX_LOOPS
    if spend_cap_usd is not None and spend_cap_usd < 0:
        raise ValueError("Spend cap cannot be negative")
    if max_parallel_steps is not None and max_parallel_steps < 1:
        raise ValueError("Max parallel steps must be at least 1")

    parent_id = models.create_synthetic_parent_run(
        workspace_id,
        f"Mad Scientist Mission: {goal}",
        PARENT_PROVIDER,
    )
    parent_log = os.path.join(config.LOGS_DIR, f"run-{parent_id}.log")
    models.update_run(
        parent_id,
        status="running",
        log_path=parent_log,
        started_at=_now(),
    )

    mission_id = models.create_mission(
        workspace_id,
        TEMPLATE_ID,
        goal,
        SCOUT_STAGE,
        provider=provider,
        parent_run_id=parent_id,
        max_steps=max_steps,
        max_fix_loops=max_fix_loops,
    )
    mission = models.get_mission(mission_id)
    scout_task = _scout_task(workspace, goal, provider, max_steps)
    graph_id, scout_step_id = graph.open_graph(
        mission_id,
        workspace_id,
        goal,
        SCOUT_STAGE,
        scout_task,
        provider,
        spend_cap_usd=spend_cap_usd,
        max_parallel_steps=max_parallel_steps,
        max_attempts=max_fix_loops,
        token_cap=token_cap,
    )
    stored = graph.get_by_mission_id(mission_id)
    _append_parent_log(mission, f"[tank] mad_scientist mission started: m#{mission_id} graph=#{graph_id}")
    _append_parent_log(mission, f"[tank] goal: {goal}")
    if stored is not None and stored["spend_cap_usd"] is not None:
        _append_parent_log(mission, f"[tank] spend cap: ${float(stored['spend_cap_usd']):.4f}")
    if stored is not None and stored["token_cap"] is not None:
        _append_parent_log(
            mission,
            f"[tank] token cap: {int(stored['token_cap'])} "
            "(enforced when a provider reports tokens)",
        )
    if stored is not None:
        _append_parent_log(
            mission,
            f"[tank] attempts per step: {stored['max_attempts']}",
        )

    scout_run_id = models.create_run(
        workspace_id,
        None,
        scout_task,
        provider=provider,
        mission_id=mission_id,
        stage_name=SCOUT_STAGE,
        crew_run_id=parent_id,
    )
    graph.record_attempt(scout_step_id, scout_run_id, "scout")
    _append_parent_log(mission, f"[tank] scout run id: #{scout_run_id}")

    import session_manager

    session_manager.launch_pending()
    return mission_id


def _payload_patch_paths(run) -> list[str]:
    payload = models.get_run_payload(run["id"])
    if not payload:
        return []
    return [
        str(patch.get("path")).strip()
        for patch in (payload.get("patches") or [])
        if str(patch.get("path") or "").strip()
    ]


def _run_summary(run) -> str | None:
    payload = models.get_run_payload(run["id"])
    if payload and payload.get("summary"):
        return str(payload["summary"]).strip()
    return None


def _store_memory(workspace_id: int, mission, plan: dict, terminal_run) -> None:
    profile = plan.get("project_profile") or {}
    prior = models.get_workspace_memory(workspace_id, MEMORY_KEY) or {}
    test_commands = profile.get("test_commands") or []
    runs = _terminal_runs_for_mission(mission["id"])
    changed_paths = []
    successful_patterns = []
    pitfalls = list(profile.get("implementation_risks") or prior.get("known_pitfalls") or [])
    for run in runs:
        for path in _payload_patch_paths(run):
            if path not in changed_paths:
                changed_paths.append(path)
        summary = _run_summary(run)
        if run["status"] == "done" and summary:
            pattern = f"{run['stage_name']}: {summary}"
            if pattern not in successful_patterns:
                successful_patterns.append(pattern)
        if run["status"] in TERMINAL_FAILURE_STATUSES:
            pitfall = f"{run['stage_name']} ended as {run['status']}"
            if pitfall not in pitfalls:
                pitfalls.append(pitfall)

    for pattern in prior.get("prior_successful_patterns") or []:
        if pattern not in successful_patterns:
            successful_patterns.append(pattern)

    memory = {
        "project_profile": profile,
        "test_command": (test_commands[0] if test_commands else prior.get("test_command")),
        "important_paths": profile.get("important_paths") or prior.get("important_paths") or [],
        "changed_paths": changed_paths or prior.get("changed_paths") or [],
        "prior_successful_patterns": successful_patterns[:20],
        "known_pitfalls": pitfalls[:20],
        "last_mission_run_id": terminal_run["id"] if terminal_run else None,
        "updated_at": _now(),
    }
    models.upsert_workspace_memory(workspace_id, MEMORY_KEY, memory)


def _advance_mission_locked(run_id: int) -> bool:
    run = models.get_run(run_id)
    if run is None or not run["mission_id"]:
        return False
    mission = models.get_mission(run["mission_id"])
    if mission is None or mission["template"] != TEMPLATE_ID:
        return False
    if graph.has_graph(mission["id"]):
        return _advance_graph_locked(run, mission)
    if run["status"] not in ("done", "failed", "rejected", "cancelled"):
        return True
    if mission["status"] != "running":
        return True

    workspace = models.get_workspace_by_id(run["workspace_id"])
    if workspace is None:
        models.update_mission(mission["id"], status="failed", note="Workspace not found")
        return True

    if run["status"] == "failed" and run["stage_name"] != SCOUT_STAGE:
        plan = json.loads(mission["plan_json"] or "{}")
        failed_step = _step_by_name(plan, run["stage_name"])
        if failed_step is None and str(run["stage_name"] or "").startswith("Fix "):
            note = f"Fixer step '{run['stage_name']}' failed - mission stopped."
            models.update_mission(mission["id"], status="failed", note=note)
            _append_parent_log(mission, f"[tank] {note}")
            models.update_run(mission["parent_run_id"], status="failed", finished_at=_now())
            _store_memory(workspace["id"], mission, plan, run)
            return True

        max_loops = mission["max_fix_loops"]
        if max_loops is None:
            max_loops = config.DEFAULT_MAX_FIX_LOOPS
        if mission["fix_loop_count"] >= max_loops:
            note = (
                f"Fix-loop limit reached ({max_loops}) after step "
                f"'{run['stage_name']}' failed."
            )
            models.update_mission(mission["id"], status="failed", note=note)
            _append_parent_log(mission, f"[tank] {note}")
            models.update_run(mission["parent_run_id"], status="failed", finished_at=_now())
            _store_memory(workspace["id"], mission, plan, run)
            return True

        attempt = mission["fix_loop_count"] + 1
        fixer = _fixer_step(failed_step, run, attempt, max_loops)
        child_id = _create_child_run(workspace, mission, fixer, parent_run_id=run_id)
        models.update_mission(
            mission["id"],
            fix_loop_count=attempt,
            current_stage=fixer["name"],
            note=(
                f"Step '{run['stage_name']}' failed - queued fixer "
                f"{attempt}/{max_loops} (run #{child_id})"
            ),
        )
        _append_parent_log(
            mission,
            f"[tank] step failed: {run['stage_name']} run=#{run_id}; "
            f"queued fixer attempt {attempt}/{max_loops} run=#{child_id}",
        )
        import session_manager

        session_manager.launch_pending()
        return True

    if run["status"] in ("rejected", "cancelled"):
        plan = json.loads(mission["plan_json"] or "{}") if mission["plan_json"] else {}
        note = f"Step '{run['stage_name']}' ended with status {run['status']}."
        models.update_mission(mission["id"], status="failed", note=note)
        _append_parent_log(mission, f"[tank] {note}")
        models.update_run(mission["parent_run_id"], status="failed", finished_at=_now())
        _store_memory(workspace["id"], mission, plan, run)
        return True

    if run["status"] == "failed":
        note = f"Step '{run['stage_name']}' ended with status failed."
        models.update_mission(mission["id"], status="failed", note=note)
        _append_parent_log(mission, f"[tank] {note}")
        models.update_run(mission["parent_run_id"], status="failed", finished_at=_now())
        return True

    _append_parent_log(
        mission,
        f"[tank] step finished: {run['stage_name']} run=#{run_id} status={run['status']}",
    )

    if run["stage_name"] == SCOUT_STAGE:
        try:
            parsed = _extract_json(_scout_output(run))
            if "steps" not in parsed and isinstance(parsed.get("plan"), str):
                parsed = _extract_json(parsed["plan"])
            plan = _validate_plan(parsed, mission["provider"], mission["max_steps"])
        except ValueError as exc:
            raw = _scout_output(run)
            note = f"Scout output could not be parsed: {exc}"
            models.update_mission(mission["id"], status="failed", note=note)
            _append_parent_log(mission, f"[tank] {note}")
            _append_parent_log(mission, "--- raw scout output ---")
            _append_parent_log(mission, raw[:12000])
            _append_parent_log(mission, "--- end raw scout output ---")
            models.update_run(mission["parent_run_id"], status="failed", finished_at=_now())
            return True

        models.update_mission(
            mission["id"],
            plan_json=json.dumps(plan, indent=2, sort_keys=True),
            note=f"Generated plan: {plan.get('summary') or 'ready'}",
        )
        mission = models.get_mission(mission["id"])
        _append_parent_log(mission, f"[tank] generated plan summary: {plan.get('summary')}")
        for step in _ordered_steps(plan):
            _append_parent_log(
                mission,
                f"[tank] plan step: {step['name']} role={step['role']} provider={step.get('provider')}",
            )
        if plan.get("steps"):
            _log_pattern_synthesis(mission, pattern_synthesis.bind_plan(workspace, mission, plan))
        queued = _schedule_ready_steps(workspace, mission, plan, fallback_run_id=run_id)
        if not queued:
            if _planned_steps_done(plan, mission):
                models.update_mission(mission["id"], status="done", note="Scout produced no execution steps")
                models.update_run(mission["parent_run_id"], status="done", finished_at=_now())
                _store_memory(workspace["id"], mission, plan, run)
            else:
                note = "Generated DAG has no runnable steps; check missing or cyclic depends_on entries."
                models.update_mission(mission["id"], status="failed", note=note)
                _append_parent_log(mission, f"[tank] {note}")
                models.update_run(mission["parent_run_id"], status="failed", finished_at=_now())
                _store_memory(workspace["id"], mission, plan, run)
            return True
        models.update_mission(
            mission["id"],
            current_stage="parallel DAG running",
            note=f"Queued {len(queued)} ready DAG step(s)",
        )
        import session_manager

        session_manager.launch_pending()
        return True

    plan = json.loads(mission["plan_json"] or "{}")
    queued = _schedule_ready_steps(workspace, mission, plan, fallback_run_id=run_id)
    if _planned_steps_done(plan, mission) and not _active_runs(mission):
        models.update_mission(mission["id"], status="done", note="Mad Scientist mission complete")
        _append_parent_log(mission, "[tank] mad_scientist mission complete")
        models.update_run(mission["parent_run_id"], status="done", finished_at=_now())
        _store_memory(workspace["id"], mission, plan, run)
        return True

    if queued:
        models.update_mission(
            mission["id"],
            current_stage="parallel DAG running",
            note=f"Queued {len(queued)} ready DAG step(s)",
        )
    else:
        active = _active_runs(mission)
        if active:
            models.update_mission(
                mission["id"],
                current_stage="parallel DAG running",
                note=f"Waiting on {len(active)} active DAG step(s)",
            )
        else:
            note = "Generated DAG is blocked; remaining steps have unsatisfied dependencies."
            models.update_mission(mission["id"], status="failed", note=note)
            _append_parent_log(mission, f"[tank] {note}")
            models.update_run(mission["parent_run_id"], status="failed", finished_at=_now())
            _store_memory(workspace["id"], mission, plan, run)
            return True
    import session_manager

    session_manager.launch_pending()
    return True


def _revive_swept_parent(mission) -> None:
    parent_id = mission["parent_run_id"]
    if not parent_id:
        return
    parent = models.get_run(parent_id)
    if parent is None or parent["status"] != "failed":
        return
    import session_manager

    if parent["error"] != session_manager.INTERRUPTED_ERROR:
        return
    models.update_run(parent_id, status="running", error=None, finished_at=None)


def _fail_graph_mission(mission, plan: dict, run, note: str, step_id: int | None = None) -> None:
    models.update_mission(mission["id"], status="failed", note=note)
    graph.set_status(mission["id"], "failed", blocked_reason=note)
    if step_id is not None:
        graph.note_stop_reason(mission["id"], note, step_id=step_id)
    _append_parent_log(mission, f"[tank] {note}")
    if mission["parent_run_id"]:
        models.update_run(mission["parent_run_id"], status="failed", finished_at=_now(), error=note)
    _store_memory(mission["workspace_id"], mission, plan, run)


def _block_graph_mission(mission, plan: dict, run, reason: str, step_id: int | None = None) -> None:
    """Stop the mission for a person. The graph status is BLOCKED_HUMAN."""
    models.update_mission(mission["id"], status=graph.BLOCKED_HUMAN, note=reason)
    graph.block_mission(mission["id"], reason)
    if step_id is not None:
        graph.block_step(step_id, reason)
    _append_parent_log(mission, f"[tank] BLOCKED_HUMAN: {reason}")
    if mission["parent_run_id"]:
        models.update_run(
            mission["parent_run_id"],
            status="failed",
            finished_at=_now(),
            error=reason,
        )
    _store_memory(mission["workspace_id"], mission, plan, run)


def _complete_graph_mission(mission, plan: dict, run, note: str = "Mad Scientist mission complete") -> None:
    models.update_mission(mission["id"], status="done", current_stage="done", note=note)
    graph.set_status(mission["id"], "done")
    _append_parent_log(mission, "[tank] mad_scientist mission complete")
    if mission["parent_run_id"]:
        models.update_run(mission["parent_run_id"], status="done", finished_at=_now())
    _store_memory(mission["workspace_id"], mission, plan, run)


def _create_step_attempt(
    workspace,
    mission,
    step_row,
    parent_run_id: int | None,
    kind: str,
    task_override: str | None = None,
) -> int:
    step = graph.step_public_dict(step_row)
    context = graph.dependency_context(step_row["id"])
    task = task_override or _step_task(mission["goal"], step, step.get("depends_on") or [])
    if context:
        task = (
            f"{task}\n\nLocalized context from all direct dependency steps:\n{context}"
        )
    role_id, provider, persona = _attempt_staffing(workspace, mission, step)
    run_id = models.create_run(
        workspace["id"],
        role_id,
        task,
        provider=provider,
        parent_run_id=parent_run_id,
        mission_id=mission["id"],
        stage_name=step["name"],
        persona_override=persona,
        crew_run_id=mission["parent_run_id"],
    )
    graph.record_attempt(step_row["id"], run_id, kind)
    return run_id


def _effective_mission_cap(graph_row) -> int:
    """Tighter of the mission column and TANK_MAX_PARALLEL_RUNS_PER_MISSION.

    Null or 0 on either side means that side adds no extra limit. The global
    TANK_MAX_PARALLEL_RUNS cap still applies when runs are launched.
    """
    caps = []
    per_mission = None
    if graph_row is not None:
        per_mission = graph_row["max_parallel_steps"]
    if per_mission:
        caps.append(int(per_mission))
    if config.MAX_PARALLEL_RUNS_PER_MISSION > 0:
        caps.append(config.MAX_PARALLEL_RUNS_PER_MISSION)
    return min(caps) if caps else 0


def _schedule_ready_graph_steps(
    workspace,
    mission,
    graph_row,
    fallback_run_id: int | None = None,
) -> list[int]:
    cap = _effective_mission_cap(graph_row)
    inflight = graph.count_inflight(graph_row["id"])
    room = None if cap <= 0 else max(0, cap - inflight)
    queued = []
    ready = graph.ready_steps(graph_row["id"])
    for step in ready:
        if room is not None and len(queued) >= room:
            _append_parent_log(
                mission,
                f"[tank] mission concurrency cap ({cap}) left "
                f"{step['name']} planned",
            )
            break
        parent_id = graph.newest_dependency_run_id(step["id"]) or fallback_run_id
        child_id = _create_step_attempt(
            workspace, mission, step, parent_id, "execute"
        )
        queued.append(child_id)
        _append_parent_log(
            mission,
            f"[tank] queued ready DAG step: {step['name']} "
            f"provider={step['provider']} run=#{child_id} parent=#{parent_id or 'none'}",
        )
    return queued


def _graph_plan(mission) -> dict:
    if not mission["plan_json"]:
        return {}
    try:
        return json.loads(mission["plan_json"])
    except json.JSONDecodeError:
        return {}


def _continue_graph(workspace, mission, graph_row, plan: dict, run, fallback_run_id: int | None) -> bool:
    """Queue newly ready steps, or finish the mission when the DAG is idle."""
    _revive_swept_parent(mission)
    graph_row = graph.get_by_mission_id(mission["id"]) or graph_row
    if graph.all_steps_done(graph_row["id"]) and graph.count_inflight(graph_row["id"]) == 0:
        _complete_graph_mission(mission, plan, run)
        return True

    reason = graph.mission_stop_reason(graph_row)
    if reason:
        _block_graph_mission(mission, plan, run, reason)
        return True

    queued = _schedule_ready_graph_steps(
        workspace, mission, graph.get_by_mission_id(mission["id"]), fallback_run_id
    )
    graph_row = graph.get_by_mission_id(mission["id"])
    if graph.all_steps_done(graph_row["id"]) and graph.count_inflight(graph_row["id"]) == 0:
        _complete_graph_mission(mission, plan, run)
        return True
    if queued:
        models.update_mission(
            mission["id"],
            current_stage="parallel DAG running",
            note=f"Queued {len(queued)} ready DAG step(s)",
        )
    elif graph.count_inflight(graph_row["id"]) > 0:
        waiting = graph.count_inflight(graph_row["id"])
        models.update_mission(
            mission["id"],
            current_stage="parallel DAG running",
            note=f"Waiting on {waiting} active DAG step(s)",
        )
    else:
        blocked = [
            step for step in graph.list_steps(graph_row["id"])
            if step["status"] == graph.BLOCKED_HUMAN
        ]
        if blocked:
            reason = blocked[-1]["blocked_reason"] or "A step is BLOCKED_HUMAN."
            _block_graph_mission(mission, plan, run, reason)
            return True
        note = "Generated DAG is blocked; remaining steps have unsatisfied dependencies."
        _fail_graph_mission(mission, plan, run, note)
        return True
    import session_manager

    session_manager.launch_pending()
    return True


def _downgrade_unverified_success(run):
    """A tester or reviewer 'passed' summary is a failed attempt."""
    if run is None or run["status"] != "done":
        return run
    attempt = graph.get_attempt_by_run(run["id"])
    step_row = graph.get_step(attempt["step_id"]) if attempt is not None else None
    reason = graph.verification_failure_reason(run, step_row)
    if not reason:
        return run
    models.update_run(run["id"], status="failed", error=reason)
    graph.sync_run_status(run["id"])
    return models.get_run(run["id"])


def _halt_before_next_attempt(workspace, mission, plan, run, step_row, fallback_run_id) -> bool:
    """Apply breakers before queueing another attempt on this step.

    Returns True when the caller must not queue that attempt.
    """
    graph_row = graph.get_by_mission_id(mission["id"])
    reason = graph.mission_stop_reason(graph_row)
    if reason:
        _block_graph_mission(mission, plan, run, reason)
        return True
    attempt = graph.get_attempt_by_run(run["id"])
    if attempt is not None:
        reason = graph.repeated_mistake_reason(step_row["id"], attempt["id"])
        if reason:
            graph.block_step(step_row["id"], reason)
            models.update_mission(mission["id"], note=reason)
            _append_parent_log(mission, f"[tank] BLOCKED_HUMAN: {reason}")
            _continue_graph(workspace, mission, graph_row, plan, run, fallback_run_id)
            return True
    cap = graph.attempt_cap(graph_row)
    count = graph.count_attempts(step_row["id"])
    if count >= cap:
        reason = (
            f"Attempt cap reached on step '{step_row['name']}' ({count} of {cap})."
        )
        graph.block_step(step_row["id"], reason)
        models.update_mission(mission["id"], note=reason)
        _append_parent_log(mission, f"[tank] BLOCKED_HUMAN: {reason}")
        _continue_graph(workspace, mission, graph_row, plan, run, fallback_run_id)
        return True
    return False


def _advance_graph_locked(run, mission) -> bool:
    graph.sync_run_status(run["id"])
    run = _downgrade_unverified_success(run)
    graph.charge_run_spend(run["id"])
    if run["status"] in ("done", "failed", "rejected", "cancelled"):
        graph.record_attempt_usage(run["id"])
        graph.record_evaluation_if_applicable(run["id"])
    if run["status"] not in ("done", "failed", "rejected", "cancelled"):
        return True
    if mission["status"] == graph.BLOCKED_HUMAN:
        graph_row = graph.get_by_mission_id(mission["id"])
        if (
            graph_row is not None
            and graph.all_steps_done(graph_row["id"])
            and graph.count_inflight(graph_row["id"]) == 0
        ):
            _complete_graph_mission(mission, _graph_plan(mission), run)
        return True
    if mission["status"] != "running":
        return True

    workspace = models.get_workspace_by_id(run["workspace_id"])
    if workspace is None:
        models.update_mission(mission["id"], status="failed", note="Workspace not found")
        graph.set_status(mission["id"], "failed")
        return True

    attempt = graph.get_attempt_by_run(run["id"])
    if attempt is None:
        return True
    step_row = graph.get_step(attempt["step_id"])
    if step_row is None:
        return True
    kind = attempt["attempt_kind"]
    plan = _graph_plan(mission)
    graph_row = graph.get_by_mission_id(mission["id"])

    if run["status"] in ("rejected", "cancelled"):
        note = f"Step '{step_row['name']}' ended with status {run['status']}."
        _fail_graph_mission(mission, plan, run, note, step_id=step_row["id"])
        return True

    if run["status"] == "failed":
        if kind == "scout" or step_row["name"] == SCOUT_STAGE:
            note = f"Step '{step_row['name']}' ended with status failed."
            _fail_graph_mission(mission, plan, run, note, step_id=step_row["id"])
            return True

        if _halt_before_next_attempt(
            workspace, mission, plan, run, step_row, run["id"]
        ):
            return True

        max_loops = graph.attempt_cap(graph.get_by_mission_id(mission["id"]))
        attempt_n = mission["fix_loop_count"] + 1
        fixer = _fixer_step(graph.step_public_dict(step_row), run, attempt_n, max_loops)
        child_id = _create_step_attempt(
            workspace,
            mission,
            step_row,
            run["id"],
            "fixer",
            task_override=fixer["task"],
        )
        models.update_mission(
            mission["id"],
            fix_loop_count=attempt_n,
            current_stage=step_row["name"],
            note=(
                f"Step '{step_row['name']}' failed - queued fixer "
                f"{attempt_n}/{max_loops} (run #{child_id})"
            ),
        )
        _append_parent_log(
            mission,
            f"[tank] step failed: {step_row['name']} run=#{run['id']}; "
            f"queued fixer attempt {attempt_n}/{max_loops} run=#{child_id}",
        )
        _revive_swept_parent(mission)
        import session_manager

        session_manager.launch_pending()
        return True

    _append_parent_log(
        mission,
        f"[tank] step finished: {step_row['name']} run=#{run['id']} "
        f"status={run['status']} kind={kind}",
    )

    if kind == "scout":
        if not any(step["position"] > 0 for step in graph.list_steps(graph_row["id"])):
            try:
                parsed = _extract_json(_scout_output(run))
                if "steps" not in parsed and isinstance(parsed.get("plan"), str):
                    parsed = _extract_json(parsed["plan"])
                plan = _validate_plan(parsed, mission["provider"], mission["max_steps"])
                graph.materialize_plan(graph_row["id"], plan)
            except ValueError as exc:
                raw = _scout_output(run)
                note = f"Scout output could not be parsed: {exc}"
                models.update_mission(mission["id"], status="failed", note=note)
                graph.set_status(mission["id"], "failed", blocked_reason=note)
                _append_parent_log(mission, f"[tank] {note}")
                _append_parent_log(mission, "--- raw scout output ---")
                _append_parent_log(mission, raw[:12000])
                _append_parent_log(mission, "--- end raw scout output ---")
                if mission["parent_run_id"]:
                    models.update_run(
                        mission["parent_run_id"], status="failed", finished_at=_now()
                    )
                return True
            models.update_mission(
                mission["id"],
                plan_json=json.dumps(plan, indent=2, sort_keys=True),
                note=f"Generated plan: {plan.get('summary') or 'ready'}",
            )
            mission = models.get_mission(mission["id"])
            _append_parent_log(mission, f"[tank] generated plan summary: {plan.get('summary')}")
            for step in plan.get("steps") or []:
                _append_parent_log(
                    mission,
                    f"[tank] plan step: {step['name']} role={step['role']} "
                    f"provider={step.get('provider')}",
                )
        if not (plan.get("steps") or []):
            _complete_graph_mission(
                mission, plan, run, note="Scout produced no execution steps"
            )
            return True
        mission = models.get_mission(mission["id"])
        _log_pattern_synthesis(mission, pattern_synthesis.bind_plan(workspace, mission, plan))
        return _continue_graph(workspace, mission, graph_row, plan, run, run["id"])

    if kind == "fixer":
        if _halt_before_next_attempt(
            workspace, mission, plan, run, step_row, run["id"]
        ):
            return True
        child_id = _create_step_attempt(
            workspace, mission, step_row, run["id"], "retry"
        )
        models.update_mission(
            mission["id"],
            current_stage=step_row["name"],
            note=f"Fixer for '{step_row['name']}' succeeded - queued retry run #{child_id}",
        )
        _append_parent_log(
            mission,
            f"[tank] fixer succeeded for {step_row['name']}; queued retry run=#{child_id}",
        )
        _revive_swept_parent(mission)
        import session_manager

        session_manager.launch_pending()
        graph_row = graph.get_by_mission_id(mission["id"])
        return _continue_graph(workspace, mission, graph_row, plan, run, run["id"])

    return _continue_graph(workspace, mission, graph_row, plan, run, run["id"])


def retry_scout(mission_id: int) -> int:
    """Queue another scout attempt after the scout itself failed."""
    mission = models.get_mission(mission_id)
    if mission is None or mission["template"] != TEMPLATE_ID:
        raise ValueError("Mad Scientist mission not found")
    view = graph.mission_view(mission_id)
    if not view or not view["can_retry_scout"]:
        raise ValueError("Scout cannot be retried for this mission")
    workspace = models.get_workspace_by_id(mission["workspace_id"])
    if workspace is None:
        raise ValueError("Workspace not found")

    graph_row = graph.get_by_mission_id(mission_id)
    scout_step = next(
        step for step in graph.list_steps(graph_row["id"]) if step["role"] == "scout"
    )
    models.update_mission(
        mission_id,
        status="running",
        current_stage=SCOUT_STAGE,
        note="Retrying scout",
    )
    graph.set_status(mission_id, "running")
    if mission["parent_run_id"]:
        parent = models.get_run(mission["parent_run_id"])
        if parent is not None and parent["status"] in ("failed", "done", "cancelled"):
            models.update_run(
                mission["parent_run_id"],
                status="running",
                error=None,
                finished_at=None,
            )
    mission = models.get_mission(mission_id)
    scout_run_id = models.create_run(
        workspace["id"],
        None,
        scout_step["task"],
        provider=mission["provider"],
        mission_id=mission_id,
        stage_name=SCOUT_STAGE,
        crew_run_id=mission["parent_run_id"],
    )
    graph.record_attempt(scout_step["id"], scout_run_id, "scout")
    _append_parent_log(mission, f"[tank] retrying scout run id: #{scout_run_id}")
    import session_manager

    session_manager.launch_pending()
    return scout_run_id


def advance_mission(run_id: int) -> bool:
    with _DAG_LOCK:
        return _advance_mission_locked(run_id)
