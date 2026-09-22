"""Persistent execution graph for Mad Scientist missions."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone

import config
import models
import repo_context
import run_approval

TEMPLATE_ID = "mad_scientist"
PARENT_PROVIDER = "mad_scientist"
SCOUT_STAGE = "Scout / Repo Cartographer"
MEMORY_KEY = "mad_scientist"

WRITE_ROLES = {"builder", "fixer", "integrator"}
VALID_ROLES = WRITE_ROLES | {"scout", "architect", "builder", "tester", "fixer", "integrator", "reviewer"}
ACTIVE_STEP_STATUSES = {"waiting", "ready", "queued", "running", "fixing", "retrying"}
ACTIVE_RUN_STATUSES = {"pending", "running", "awaiting_approval"}
TERMINAL_FAILURE_STATUSES = {"failed", "rejected", "cancelled"}

_GRAPH_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_parent_log(graph_mission, line: str) -> None:
    parent_id = graph_mission["parent_run_id"]
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


def _validate_plan(raw_plan: dict, default_provider: str | None, max_steps: int | None) -> dict:
    if not isinstance(raw_plan, dict):
        raise ValueError("Execution plan must be a JSON object")
    steps = raw_plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Execution plan must include a non-empty steps array")

    seen = set()
    normalized_steps = []
    limit = max_steps if max_steps and max_steps > 0 else None
    for item in steps:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("Every step needs a non-empty name")
        if name in seen:
            raise ValueError(f"Duplicate step name: {name}")
        seen.add(name)
        role = str(item.get("role") or "builder").strip().lower()
        if role not in VALID_ROLES:
            role = "builder"
        deps = item.get("depends_on") or []
        if not isinstance(deps, list):
            raise ValueError(f"Step '{name}' depends_on must be a list")
        dep_names = [str(dep).strip() for dep in deps if str(dep).strip()]
        if len(dep_names) != len(set(dep_names)):
            raise ValueError(f"Step '{name}' has duplicate depends_on entries")
        normalized_steps.append({
            "name": name,
            "role": role,
            "provider": str(item.get("provider") or default_provider or "").strip(),
            "task": str(item.get("task") or "").strip(),
            "depends_on": dep_names,
            "write_allowed": bool(item.get("write_allowed", role in WRITE_ROLES)),
            "success_criteria": [
                str(crit).strip()
                for crit in (item.get("success_criteria") or [])
                if str(crit).strip()
            ],
        })
        if limit and len(normalized_steps) >= limit:
            break

    if not normalized_steps:
        raise ValueError("Execution plan did not contain any usable steps")

    names = {step["name"] for step in normalized_steps}
    for step in normalized_steps:
        missing = [dep for dep in step["depends_on"] if dep not in names]
        if missing:
            raise ValueError(f"Step '{step['name']}' depends on missing step(s): {', '.join(missing)}")

    _assert_acyclic(normalized_steps)
    return {
        "summary": str(raw_plan.get("summary") or "").strip(),
        "project_profile": raw_plan.get("project_profile") or {},
        "steps": normalized_steps,
    }


def _assert_acyclic(steps: list[dict]) -> None:
    deps = {step["name"]: list(step.get("depends_on") or []) for step in steps}
    visiting = set()
    visited = set()

    def visit(name: str):
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"Cyclic dependency involving '{name}'")
        visiting.add(name)
        for dep in deps.get(name, []):
            visit(dep)
        visiting.remove(name)
        visited.add(name)

    for step_name in deps:
        visit(step_name)


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
{_memory_prompt(workspace["id"])}

Return the normal Tank local_agent JSON envelope. Its plan field MUST be a
string containing valid JSON with this schema:
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

Plan rules:
- Recommend at most {max_steps_text} execution steps after this scout.
- Use depends_on to express true DAG dependencies.
- Include builder/integrator steps when the goal requires implementation.
- Builder tasks must make concrete repo changes when implementation is needed.
- Tester/reviewer tasks must verify actual files changed by direct dependency steps.
- Scout, tester, and reviewer steps should have write_allowed false unless there is a specific reason.
""".strip()


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


def _log_excerpt(run, max_chars=1800) -> str:
    if not run["log_path"] or not os.path.exists(run["log_path"]):
        return ""
    with open(run["log_path"], encoding="utf-8", errors="replace") as f:
        text = f.read()
    return text[-max_chars:]


def _short_excerpt(text: str | None, max_chars=800) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _run_failure_detail(run) -> str:
    pieces = []
    if run["error"]:
        pieces.append(str(run["error"]))
    excerpt = _log_excerpt(run, max_chars=2400)
    if excerpt:
        pieces.append(_short_excerpt(excerpt, max_chars=900))
    return " | ".join(pieces) or f"Run #{run['id']} ended {run['status']} without captured output"


def build_dependency_context_for_step(step_id: int) -> tuple[str, int | None]:
    step = models.get_mad_scientist_step(step_id)
    deps = models.list_mad_scientist_step_dependencies(step_id)
    chunks = []
    newest_run_id = None
    for dep in deps:
        attempt = models.latest_successful_attempt_for_step(dep["id"])
        if attempt is None:
            continue
        dep_run = models.get_run(attempt["run_id"])
        if dep_run is None:
            continue
        newest_run_id = dep_run["id"]
        attempts = models.list_mad_scientist_step_attempts(dep["id"])
        failure_notes = [
            f"{a['attempt_type']} attempt #{a['attempt_number']} ended {a['status']}"
            for a in attempts
            if a["status"] in TERMINAL_FAILURE_STATUSES
        ]
        chunks.append(
            "\n".join([
                f"Dependency step: {dep['name']}",
                f"Role: {dep['role']}",
                f"Latest successful attempt run_id: {dep_run['id']}",
                f"Summary: {_run_summary(dep_run) or '(no structured summary)'}",
                f"Changed files: {', '.join(_payload_patch_paths(dep_run)) or '(none recorded)'}",
                f"Failure/fixer notes: {'; '.join(failure_notes) or '(none)'}",
                f"Log excerpt:\n{_log_excerpt(dep_run) or '(no log excerpt)'}",
            ])
        )
    if not chunks:
        return "No direct dependency context is available.", newest_run_id
    return "\n\n---\n\n".join(chunks), newest_run_id


def _step_task(graph_mission, step, dependency_context: str) -> str:
    criteria = "\n".join(
        f"- {item}" for item in json.loads(step["success_criteria_json"] or "[]")
    )
    write_allowed = bool(step["write_allowed"])
    role = step["role"]
    write_contract = (
        "You are allowed to propose concrete file changes. Do not only audit. "
        "Do not only produce a plan. If this step requires implementation, return "
        "response_type \"patch\" using the existing Tank local_agent schema."
        if write_allowed and role in WRITE_ROLES
        else "Do not propose file changes unless the task explicitly proves they are required. "
             "Prefer response_type \"plan\" with concrete verification findings."
    )
    memory = _memory_prompt(graph_mission["workspace_id"])
    return f"""
Mad Scientist Mission step: {step["name"]}
Role: {role}
Write allowed: {str(write_allowed).lower()}

Original user goal:
{graph_mission["goal"]}

Direct dependency context:
{dependency_context}

Workspace memory:
{memory}

Task:
{step["task"]}

Success criteria:
{criteria or "- Complete the step with repository-grounded evidence."}

Execution requirements:
- Use the direct dependency context above; do not invent global mission context.
- {write_contract}
- Tester/reviewer steps must verify actual files changed by dependency steps.
- Return a complete Tank local_agent JSON response and no markdown outside JSON.
""".strip()


def _create_attempt_run(graph_mission, step, attempt_type: str, parent_context_run_id: int | None) -> int:
    workspace = models.get_workspace_by_id(graph_mission["workspace_id"])
    dependency_context, newest_dep_run_id = build_dependency_context_for_step(step["id"])
    parent_id = parent_context_run_id or newest_dep_run_id
    task = _step_task(graph_mission, step, dependency_context)
    role = models.get_role_by_slug(workspace["id"], step["role"])
    attempts = models.list_mad_scientist_step_attempts(step["id"])
    attempt_number = len(attempts) + 1
    run_id = models.create_run(
        workspace["id"],
        role["id"] if role else None,
        task,
        provider=step["provider"] or graph_mission["provider"],
        parent_run_id=parent_id,
        mission_id=graph_mission["legacy_mission_id"],
        stage_name=step["name"],
        crew_run_id=graph_mission["parent_run_id"],
    )
    models.create_mad_scientist_attempt(
        graph_mission["id"],
        step["id"],
        run_id,
        attempt_number,
        attempt_type,
        parent_context_run_id=parent_id,
    )
    models.update_mad_scientist_step(
        step["id"],
        status="queued" if attempt_type == "initial" else attempt_type,
        blocked_reason=None,
    )
    _append_parent_log(
        graph_mission,
        f"[tank] attempt created: step={step['name']} type={attempt_type} "
        f"attempt={attempt_number} run=#{run_id} parent_context=#{parent_id or 'none'}",
    )
    return run_id


def _store_plan(graph_mission, plan: dict) -> bool:
    step_ids: dict[str, int] = {}
    for step in plan["steps"]:
        step_id = models.create_mad_scientist_step(
            graph_mission["id"],
            step["name"],
            step["role"],
            step.get("provider") or graph_mission["provider"],
            step["task"],
            step["write_allowed"],
            step.get("success_criteria") or [],
        )
        step_ids[step["name"]] = step_id
        _append_parent_log(graph_mission, f"[tank] graph step stored: {step['name']} role={step['role']}")

    for step in plan["steps"]:
        for dep_name in step.get("depends_on") or []:
            models.create_mad_scientist_dependency(
                graph_mission["id"],
                step_ids[step["name"]],
                step_ids[dep_name],
            )
            _append_parent_log(graph_mission, f"[tank] graph edge stored: {dep_name} -> {step['name']}")
    return True


def _dependencies_done(step) -> bool:
    deps = models.list_mad_scientist_step_dependencies(step["id"])
    return all(dep["status"] == "completed" for dep in deps)


def _ready_steps(graph_mission) -> list:
    ready = []
    for step in models.list_mad_scientist_steps(graph_mission["id"]):
        if step["status"] not in ("waiting", "ready"):
            continue
        if _dependencies_done(step):
            ready.append(step)
    return ready


def _active_attempts(graph_mission) -> list:
    return [
        attempt for attempt in models.list_mad_scientist_attempts(graph_mission["id"])
        if attempt["status"] in ACTIVE_RUN_STATUSES
    ]


def _all_steps_completed(graph_mission) -> bool:
    steps = models.list_mad_scientist_steps(graph_mission["id"])
    return bool(steps) and all(step["status"] == "completed" for step in steps)


def _block_mission(graph_mission, reason: str, status: str = "blocked") -> None:
    models.update_mad_scientist_mission(graph_mission["id"], status=status, blocked_reason=reason)
    if graph_mission["legacy_mission_id"]:
        updates = {"status": "failed", "note": reason}
        if "Scout" in reason or "scout" in reason:
            updates["current_stage"] = SCOUT_STAGE
        models.update_mission(graph_mission["legacy_mission_id"], **updates)
    if graph_mission["parent_run_id"]:
        models.update_run(graph_mission["parent_run_id"], status="failed", finished_at=_now())
    _append_parent_log(graph_mission, f"[tank] blocked DAG: {reason}")


def _schedule_ready_steps(graph_mission) -> list[int]:
    queued = []
    for step in _ready_steps(graph_mission):
        run_id = _create_attempt_run(graph_mission, step, "initial", None)
        queued.append(run_id)
    if queued:
        models.update_mad_scientist_mission(
            graph_mission["id"],
            status="running",
            blocked_reason=None,
        )
        if graph_mission["legacy_mission_id"]:
            models.update_mission(
                graph_mission["legacy_mission_id"],
                status="running",
                current_stage="DAG running",
                note=f"Queued {len(queued)} ready graph step(s)",
            )
        _append_parent_log(graph_mission, f"[tank] ready steps queued: {', '.join(f'#{r}' for r in queued)}")
        import session_manager

        session_manager.launch_pending()
    return queued


def _finalize_if_done_or_blocked(graph_mission, terminal_run=None) -> bool:
    graph_mission = models.get_mad_scientist_mission(graph_mission["id"])
    if _all_steps_completed(graph_mission) and not _active_attempts(graph_mission):
        models.update_mad_scientist_mission(graph_mission["id"], status="done", blocked_reason=None)
        if graph_mission["legacy_mission_id"]:
            models.update_mission(
                graph_mission["legacy_mission_id"],
                status="done",
                current_stage="done",
                note="Mad Scientist graph complete",
            )
        if graph_mission["parent_run_id"]:
            models.update_run(graph_mission["parent_run_id"], status="done", finished_at=_now())
        _store_memory(graph_mission, terminal_run)
        _append_parent_log(graph_mission, "[tank] final mission status: done")
        return True

    if not _active_attempts(graph_mission) and not _ready_steps(graph_mission):
        remaining = [
            step["name"] for step in models.list_mad_scientist_steps(graph_mission["id"])
            if step["status"] != "completed"
        ]
        if remaining:
            _block_mission(
                graph_mission,
                f"No progress possible; remaining step(s) blocked or waiting: {', '.join(remaining)}",
            )
            _store_memory(graph_mission, terminal_run)
            return True
    return False


def _store_memory(graph_mission, terminal_run) -> None:
    plan = json.loads(graph_mission["plan_json"] or "{}")
    profile = plan.get("project_profile") or {}
    prior = models.get_workspace_memory(graph_mission["workspace_id"], MEMORY_KEY) or {}
    test_commands = profile.get("test_commands") or []
    changed_paths = []
    successful_patterns = []
    pitfalls = list(profile.get("implementation_risks") or prior.get("known_pitfalls") or [])
    for attempt in models.list_mad_scientist_attempts(graph_mission["id"]):
        run = models.get_run(attempt["run_id"])
        if run is None:
            continue
        for path in _payload_patch_paths(run):
            if path not in changed_paths:
                changed_paths.append(path)
        summary = _run_summary(run)
        if attempt["status"] == "done" and summary:
            pattern = f"{attempt['step_name'] or attempt['attempt_type']}: {summary}"
            if pattern not in successful_patterns:
                successful_patterns.append(pattern)
        if attempt["status"] in TERMINAL_FAILURE_STATUSES:
            pitfall = f"{attempt['step_name'] or attempt['attempt_type']} {attempt['attempt_type']} ended {attempt['status']}"
            if pitfall not in pitfalls:
                pitfalls.append(pitfall)
    for pattern in prior.get("prior_successful_patterns") or []:
        if pattern not in successful_patterns:
            successful_patterns.append(pattern)
    models.upsert_workspace_memory(graph_mission["workspace_id"], MEMORY_KEY, {
        "project_profile": profile,
        "test_command": (test_commands[0] if test_commands else prior.get("test_command")),
        "important_paths": profile.get("important_paths") or prior.get("important_paths") or [],
        "changed_paths": changed_paths or prior.get("changed_paths") or [],
        "prior_successful_patterns": successful_patterns[:20],
        "known_pitfalls": pitfalls[:20],
        "last_mission_run_id": terminal_run["id"] if terminal_run else None,
        "updated_at": _now(),
    })


def start_mission(
    workspace_id: int,
    goal: str,
    provider: str | None = None,
    max_steps: int | None = None,
    max_fix_loops: int | None = None,
) -> int:
    workspace = models.get_workspace_by_id(workspace_id)
    if workspace is None:
        raise ValueError("Workspace not found")
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("Goal is required")
    max_steps = max_steps if max_steps and max_steps > 0 else 6
    max_fix_loops = max_fix_loops if max_fix_loops is not None else config.DEFAULT_MAX_FIX_LOOPS

    parent_id = models.create_synthetic_parent_run(
        workspace_id,
        f"Mad Scientist Mission: {goal}",
        PARENT_PROVIDER,
    )
    parent_log = os.path.join(config.LOGS_DIR, f"run-{parent_id}.log")
    models.update_run(parent_id, status="running", log_path=parent_log, started_at=_now())

    legacy_id = models.create_mission(
        workspace_id,
        TEMPLATE_ID,
        goal,
        SCOUT_STAGE,
        provider=provider,
        parent_run_id=parent_id,
        max_steps=max_steps,
        max_fix_loops=max_fix_loops,
    )
    graph_id = models.create_mad_scientist_mission(
        workspace_id,
        goal,
        legacy_mission_id=legacy_id,
        parent_run_id=parent_id,
        provider=provider,
        max_steps=max_steps,
        max_fix_loops=max_fix_loops,
    )
    graph_mission = models.get_mad_scientist_mission(graph_id)
    _append_parent_log(graph_mission, f"[tank] graph mission created: g#{graph_id} legacy=m#{legacy_id}")

    scout_run_id = models.create_run(
        workspace_id,
        None,
        _scout_task(workspace, goal, provider, max_steps),
        provider=provider,
        mission_id=legacy_id,
        stage_name=SCOUT_STAGE,
        crew_run_id=parent_id,
    )
    models.create_mad_scientist_attempt(
        graph_id,
        None,
        scout_run_id,
        1,
        "scout",
        parent_context_run_id=None,
    )
    _append_parent_log(graph_mission, f"[tank] scout attempt created: run=#{scout_run_id}")
    import session_manager

    session_manager.launch_pending()
    return legacy_id


def retry_scout(graph_mission_id: int) -> int:
    graph_mission = models.get_mad_scientist_mission(graph_mission_id)
    if graph_mission is None:
        raise ValueError("Mad Scientist graph mission not found")
    if graph_mission["status"] not in ("failed", "blocked"):
        raise ValueError("Only failed or blocked scout missions can be retried")
    if models.list_mad_scientist_steps(graph_mission_id):
        raise ValueError("Scout retry is only available before graph steps are created")

    active_scout = [
        attempt for attempt in models.list_mad_scientist_attempts(graph_mission_id)
        if attempt["attempt_type"] == "scout" and attempt["status"] in ACTIVE_RUN_STATUSES
    ]
    if active_scout:
        raise ValueError("A scout attempt is already active")

    workspace = models.get_workspace_by_id(graph_mission["workspace_id"])
    if workspace is None:
        raise ValueError("Workspace not found")
    parent_run_id = graph_mission["parent_run_id"]
    if parent_run_id:
        models.update_run(parent_run_id, status="running", finished_at=None)

    scout_attempts = [
        attempt for attempt in models.list_mad_scientist_attempts(graph_mission_id)
        if attempt["attempt_type"] == "scout"
    ]
    run_id = models.create_run(
        graph_mission["workspace_id"],
        None,
        _scout_task(
            workspace,
            graph_mission["goal"],
            graph_mission["provider"],
            graph_mission["max_steps"],
        ),
        provider=graph_mission["provider"],
        mission_id=graph_mission["legacy_mission_id"],
        stage_name=SCOUT_STAGE,
        crew_run_id=parent_run_id,
    )
    models.create_mad_scientist_attempt(
        graph_mission_id,
        None,
        run_id,
        len(scout_attempts) + 1,
        "scout",
        parent_context_run_id=None,
    )
    models.update_mad_scientist_mission(
        graph_mission_id,
        status="planning",
        blocked_reason=None,
    )
    if graph_mission["legacy_mission_id"]:
        models.update_mission(
            graph_mission["legacy_mission_id"],
            status="running",
            current_stage=SCOUT_STAGE,
            note=f"Retrying scout run #{run_id}",
        )
    graph_mission = models.get_mad_scientist_mission(graph_mission_id)
    _append_parent_log(graph_mission, f"[tank] scout retry created: run=#{run_id}")
    import session_manager

    session_manager.launch_pending()
    return run_id


def _complete_scout(graph_mission, run) -> bool:
    raw_output = _scout_output(run)
    try:
        parsed = _extract_json(raw_output)
        if "steps" not in parsed and isinstance(parsed.get("plan"), str):
            parsed = _extract_json(parsed["plan"])
        plan = _validate_plan(parsed, graph_mission["provider"], graph_mission["max_steps"])
    except ValueError as exc:
        excerpt = _short_excerpt(raw_output)
        reason = f"Scout did not return valid execution-plan JSON. Parse error: {exc}"
        if excerpt:
            reason += f" Response excerpt: {excerpt}"
        _append_parent_log(graph_mission, f"[tank] scout parse error: {reason}")
        if raw_output:
            _append_parent_log(graph_mission, "--- raw scout output excerpt ---")
            _append_parent_log(graph_mission, raw_output[:4000])
            _append_parent_log(graph_mission, "--- end raw scout output excerpt ---")
        _block_mission(graph_mission, reason)
        return True

    models.update_mad_scientist_mission(
        graph_mission["id"],
        status="planned",
        summary=plan.get("summary"),
        plan_json=json.dumps(plan, indent=2, sort_keys=True),
        blocked_reason=None,
    )
    if graph_mission["legacy_mission_id"]:
        models.update_mission(
            graph_mission["legacy_mission_id"],
            current_stage="planned",
            note=f"Generated graph plan: {plan.get('summary') or 'ready'}",
        )
    graph_mission = models.get_mad_scientist_mission(graph_mission["id"])
    _append_parent_log(graph_mission, f"[tank] plan parsed: {plan.get('summary')}")
    _store_plan(graph_mission, plan)
    queued = _schedule_ready_steps(graph_mission)
    if not queued:
        _block_mission(graph_mission, "Generated DAG has no ready root steps")
    return True


def _handle_step_done(graph_mission, step, attempt, run) -> bool:
    if attempt["attempt_type"] == "fixer":
        parent_id = run["id"]
        retry_run_id = _create_attempt_run(graph_mission, step, "retry", parent_id)
        models.update_mad_scientist_mission(
            graph_mission["id"],
            fix_loop_count=graph_mission["fix_loop_count"] + 1,
        )
        _append_parent_log(
            graph_mission,
            f"[tank] retry created: step={step['name']} retry_run=#{retry_run_id} fixer_run=#{run['id']}",
        )
        import session_manager

        session_manager.launch_pending()
        return True

    models.update_mad_scientist_step(step["id"], status="completed", blocked_reason=None)
    _append_parent_log(graph_mission, f"[tank] graph step completed: {step['name']} run=#{run['id']}")
    queued = _schedule_ready_steps(graph_mission)
    if not queued:
        _finalize_if_done_or_blocked(graph_mission, terminal_run=run)
    return True


def _handle_step_failed(graph_mission, step, attempt, run) -> bool:
    detail = _run_failure_detail(run)
    if run["status"] in ("rejected", "cancelled"):
        reason = f"Step '{step['name']}' {attempt['attempt_type']} attempt ended {run['status']}. {detail}"
        models.update_mad_scientist_step(step["id"], status="failed", blocked_reason=reason)
        _block_mission(graph_mission, reason, status="failed")
        _store_memory(graph_mission, run)
        return True

    max_loops = graph_mission["max_fix_loops"]
    if max_loops is None:
        max_loops = config.DEFAULT_MAX_FIX_LOOPS
    if attempt["attempt_type"] == "fixer" or graph_mission["fix_loop_count"] >= max_loops:
        reason = (
            f"Step '{step['name']}' failed and fix-loop limit is exhausted. "
            f"Last failure: {detail}"
        )
        models.update_mad_scientist_step(step["id"], status="failed", blocked_reason=reason)
        _block_mission(graph_mission, reason, status="failed")
        _store_memory(graph_mission, run)
        return True

    fixer_task = (
        f"The prior attempt for logical step '{step['name']}' failed. Diagnose the "
        "failure using the direct prior run context, then propose the smallest concrete "
        "repo changes needed to unblock this same logical step. Return patch JSON when "
        "file changes are needed."
    )
    original_task = step["task"]
    models.update_mad_scientist_step(
        step["id"],
        status="fixing",
        task=f"{fixer_task}\n\nOriginal logical step task:\n{original_task}",
    )
    fixer_step = models.get_mad_scientist_step(step["id"])
    fixer_run_id = _create_attempt_run(graph_mission, fixer_step, "fixer", run["id"])
    models.update_mad_scientist_step(step["id"], task=original_task, status="fixing")
    _append_parent_log(
        graph_mission,
        f"[tank] fixer attempt created: step={step['name']} fixer_run=#{fixer_run_id} failed_run=#{run['id']}",
    )
    import session_manager

    session_manager.launch_pending()
    return True


def advance_mission(run_id: int) -> bool:
    with _GRAPH_LOCK:
        return _advance_mission_locked(run_id)


def _advance_mission_locked(run_id: int) -> bool:
    attempt = models.get_mad_scientist_attempt_by_run(run_id)
    if attempt is None:
        return False
    run = models.get_run(run_id)
    if run is None or run["status"] not in ("done", "failed", "rejected", "cancelled"):
        return True
    graph_mission = models.get_mad_scientist_mission(attempt["mission_id"])
    if graph_mission is None or graph_mission["status"] in ("done", "failed", "blocked"):
        return True

    models.update_mad_scientist_attempt(attempt["id"], status=run["status"])
    _append_parent_log(
        graph_mission,
        f"[tank] attempt completed: run=#{run_id} type={attempt['attempt_type']} status={run['status']}",
    )
    attempt = models.get_mad_scientist_attempt_by_run(run_id)

    if attempt["attempt_type"] == "scout":
        if run["status"] == "done":
            return _complete_scout(graph_mission, run)
        detail = _run_failure_detail(run)
        _append_parent_log(
            graph_mission,
            f"[tank] scout failed: run=#{run_id} status={run['status']} detail={detail}",
        )
        _block_mission(
            graph_mission,
            f"Scout attempt ended {run['status']}. {detail}",
            status="failed",
        )
        return True

    step = models.get_mad_scientist_step(attempt["step_id"])
    if step is None:
        _block_mission(graph_mission, f"Attempt #{attempt['id']} has no logical step", status="failed")
        return True
    if run["status"] == "done":
        return _handle_step_done(graph_mission, step, attempt, run)
    return _handle_step_failed(graph_mission, step, attempt, run)


def graph_entries_for_workspace(workspace_id: int) -> list[dict]:
    def _attempt_view(attempt) -> dict:
        run = models.get_run(attempt["run_id"]) if attempt["run_id"] else None
        status = run["status"] if run is not None else attempt["status"]
        approval = run_approval.approval_state(run) if run is not None else None
        failure_reason = None
        if run is not None and status in ("failed", "rejected", "cancelled"):
            failure_reason = _run_failure_detail(run)
        return {
            "id": attempt["id"],
            "run_id": attempt["run_id"],
            "attempt_number": attempt["attempt_number"],
            "attempt_type": attempt["attempt_type"],
            "status": status,
            "effective_status": status,
            "recorded_status": attempt["status"],
            "parent_context_run_id": attempt["parent_context_run_id"],
            "created_at": attempt["created_at"],
            "completed_at": attempt["completed_at"],
            "approval": approval,
            "approval_available": bool(approval and approval["approval_available"]),
            "failure_reason": failure_reason,
        }

    def _ordered_step_views(graph_mission, deps_by_step: dict[int, list[dict]]) -> list[dict]:
        raw_steps = models.list_mad_scientist_steps(graph_mission["id"])
        by_id = {step["id"]: step for step in raw_steps}
        ordered = []
        emitted = set()

        while len(ordered) < len(raw_steps):
            progressed = False
            for step in raw_steps:
                if step["id"] in emitted:
                    continue
                deps = deps_by_step.get(step["id"], [])
                if all(dep["id"] in emitted for dep in deps):
                    ordered.append(step)
                    emitted.add(step["id"])
                    progressed = True
            if not progressed:
                for step in raw_steps:
                    if step["id"] not in emitted:
                        ordered.append(step)
                        emitted.add(step["id"])
                break

        views = []
        for step in ordered:
            deps = deps_by_step.get(step["id"], [])
            dep_names = [dep["name"] for dep in deps]
            waiting_on = [
                dep["name"] for dep in deps
                if dep["status"] != "completed"
            ]
            attempts = [
                _attempt_view(attempt)
                for attempt in models.list_mad_scientist_step_attempts(step["id"])
            ]
            latest = attempts[-1] if attempts else None

            if step["status"] == "waiting" and not waiting_on and not attempts:
                display_status = "ready"
            elif latest and latest["status"] in ("pending", "running", "awaiting_approval"):
                display_status = latest["status"]
            else:
                display_status = step["status"]

            views.append({
                "id": step["id"],
                "name": step["name"],
                "role": step["role"],
                "provider": step["provider"],
                "status": step["status"],
                "display_status": display_status,
                "blocked_reason": step["blocked_reason"],
                "dependencies": dep_names,
                "waiting_on": waiting_on,
                "attempt_count": len(attempts),
                "latest_attempt": latest,
                "attempts": attempts,
            })
        return views

    entries = []
    for graph_mission in models.list_mad_scientist_missions(workspace_id):
        deps = models.list_mad_scientist_dependencies(graph_mission["id"])
        deps_by_step = {}
        for dep in deps:
            deps_by_step.setdefault(dep["step_id"], []).append({
                "id": dep["depends_on_step_id"],
                "name": dep["depends_on_name"],
                "status": models.get_mad_scientist_step(dep["depends_on_step_id"])["status"],
            })

        parent_run = models.get_run(graph_mission["parent_run_id"]) if graph_mission["parent_run_id"] else None
        all_attempts = models.list_mad_scientist_attempts(graph_mission["id"])
        scout_attempts = [
            _attempt_view(attempt)
            for attempt in all_attempts
            if attempt["attempt_type"] == "scout"
        ]
        step_views = _ordered_step_views(graph_mission, deps_by_step)
        awaiting_approval_attempts = [
            view["latest_attempt"]
            for view in step_views
            if view["latest_attempt"] and view["latest_attempt"]["status"] == "awaiting_approval"
        ]
        actionable_approval_attempts = [
            attempt for attempt in awaiting_approval_attempts
            if attempt.get("approval_available")
        ]
        display_status = graph_mission["status"]
        status_message = None
        if awaiting_approval_attempts:
            display_status = "awaiting_approval"
            count = len(actionable_approval_attempts) or len(awaiting_approval_attempts)
            status_message = (
                f"This mission is paused until you approve or reject proposed patches. "
                f"{count} run{'s are' if count != 1 else ' is'} awaiting approval."
            )
        entries.append({
            "mission": {
                "id": graph_mission["id"],
                "goal": graph_mission["goal"],
                "status": graph_mission["status"],
                "display_status": display_status,
                "status_message": status_message,
                "awaiting_approval_count": len(actionable_approval_attempts) or len(awaiting_approval_attempts),
                "summary": graph_mission["summary"],
                "blocked_reason": graph_mission["blocked_reason"],
                "provider": graph_mission["provider"],
                "created_at": graph_mission["created_at"],
                "updated_at": graph_mission["updated_at"],
                "parent_run_id": graph_mission["parent_run_id"],
                "parent_run_status": parent_run["status"] if parent_run is not None else None,
            },
            "scout_attempts": scout_attempts,
            "latest_scout_attempt": scout_attempts[-1] if scout_attempts else None,
            "awaiting_approval_attempts": awaiting_approval_attempts,
            "actionable_approval_attempts": actionable_approval_attempts,
            "steps": step_views,
        })
    return entries
