"""
Mission orchestrator.

A mission is a named sequence of stages from mission_templates.yaml.
Each stage is just a normal Tank run, chained off the previous stage's
run via the same parent_run_id mechanism the manual "Continue from
run" picker uses — so prior-stage output flows into the next stage
automatically through run_chain.py.

This module never applies a patch and never bypasses the existing
patch-approval gate: it only ever creates new runs with status
'pending', which session_manager's normal queue worker picks up. The
fix-loop is bounded by max_fix_loops and tracked per-mission.
"""
import os
import re

import yaml

import config
import models

_templates: dict = {}


def _short_excerpt(text: str, max_chars: int = 900) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _run_failure_detail(run) -> str:
    pieces = []
    if run["error"]:
        pieces.append(str(run["error"]))
    log_path = run["log_path"]
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                pieces.append(_short_excerpt(f.read()[-2400:]))
        except OSError as exc:
            pieces.append(f"Could not read run log: {type(exc).__name__}: {exc}")
    return " | ".join(piece for piece in pieces if piece) or f"Run #{run['id']} ended {run['status']}"


def load_templates():
    global _templates
    try:
        with open(config.MISSION_TEMPLATES_FILE, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except FileNotFoundError:
        _templates = {}
        return
    _templates = doc.get("templates") or {}


def list_templates() -> list[tuple[str, str]]:
    """Return (id, label) pairs for UI dropdowns."""
    return [(tid, tpl.get("label") or tid) for tid, tpl in _templates.items()]


def get_template(template_id: str) -> dict | None:
    return _templates.get(template_id)


def _stage_by_name(template: dict, name: str) -> dict | None:
    for stage in template.get("stages", []):
        if stage["name"] == name:
            return stage
    return None


def _next_stage_name(template: dict, stage: dict) -> str | None:
    if "next_on_success" in stage:
        return stage["next_on_success"]
    stages = template.get("stages", [])
    names = [s["name"] for s in stages]
    idx = names.index(stage["name"])
    if idx + 1 < len(names):
        return names[idx + 1]
    return None


def _render_task(stage: dict, goal: str) -> str:
    return stage["task"].format(goal=goal).strip()


def _stage_provider(mission, stage: dict) -> str | None:
    """Stage YAML override, else mission-level provider from the start form."""
    if stage.get("provider"):
        return stage.get("provider")
    return mission["provider"]


def _create_stage_run(workspace, mission, stage: dict, parent_run_id: int | None) -> int:
    role = models.get_role_by_slug(workspace["id"], stage.get("role"))
    return models.create_run(
        workspace["id"],
        role["id"] if role else None,
        _render_task(stage, mission["goal"]),
        provider=_stage_provider(mission, stage),
        parent_run_id=parent_run_id,
        mission_id=mission["id"],
        stage_name=stage["name"],
    )


def start_mission(workspace_id: int, template_id: str, goal: str, provider: str | None = None) -> int:
    template = get_template(template_id)
    if not template:
        raise ValueError(f"Unknown mission template: {template_id}")
    stages = template.get("stages") or []
    if not stages:
        raise ValueError(f"Mission template '{template_id}' has no stages")

    workspace = models.get_workspace_by_id(workspace_id)
    if workspace is None:
        raise ValueError("Workspace not found")

    first_stage = stages[0]
    mission_id = models.create_mission(
        workspace_id, template_id, goal.strip(), first_stage["name"], provider=provider
    )
    mission = models.get_mission(mission_id)
    _create_stage_run(workspace, mission, first_stage, parent_run_id=None)
    return mission_id


def resume_mission(
    mission_id: int,
    from_run_id: int | None = None,
    provider: str | None = None,
) -> int:
    """
    Continue a failed or aborted mission from a chosen run, optionally
    with a new provider. Completed runs advance to the next stage;
    failed/cancelled/rejected runs retry that stage with prior context.
    """
    mission = models.get_mission(mission_id)
    if mission is None:
        raise ValueError("Mission not found")
    if mission["status"] not in ("failed", "aborted"):
        raise ValueError(f"Only failed or aborted missions can be resumed (status: {mission['status']})")

    runs = models.list_mission_runs(mission_id)
    if not runs:
        raise ValueError("Mission has no runs to resume from")

    for run in runs:
        if run["status"] in ("pending", "running", "awaiting_approval"):
            raise ValueError("Mission has an active run — cancel it before resuming")

    template = get_template(mission["template"])
    if template is None:
        raise ValueError("Mission template no longer exists")

    workspace = models.get_workspace_by_id(mission["workspace_id"])
    if workspace is None:
        raise ValueError("Workspace not found")

    if from_run_id:
        from_run = models.get_run(from_run_id)
        if from_run is None or from_run["mission_id"] != mission_id:
            raise ValueError("Selected run does not belong to this mission")
    else:
        from_run = runs[-1]

    stage = _stage_by_name(template, from_run["stage_name"])
    if stage is None:
        raise ValueError(f"Unknown stage: {from_run['stage_name']}")

    updates: dict = {"status": "running"}
    if provider:
        updates["provider"] = provider
    models.update_mission(mission_id, **updates)
    mission = models.get_mission(mission_id)

    if from_run["status"] == "done":
        next_name = _next_stage_name(template, stage)
        if next_name is None:
            raise ValueError(
                f"Run #{from_run['id']} already completed the final stage — pick an earlier run or start a new mission"
            )
        next_stage = _stage_by_name(template, next_name)
        if next_stage is None:
            raise ValueError(f"Next stage '{next_name}' does not exist")
        new_run_id = _create_stage_run(workspace, mission, next_stage, parent_run_id=from_run["id"])
        models.update_mission(
            mission_id,
            current_stage=next_stage["name"],
            note=f"Resumed from run #{from_run['id']} → stage '{next_stage['name']}' (run #{new_run_id})",
        )
        import session_manager

        session_manager.launch_pending()
        return new_run_id

    if from_run["status"] in ("failed", "cancelled", "rejected"):
        parent_run_id = from_run["id"]
        new_run_id = _create_stage_run(workspace, mission, stage, parent_run_id=parent_run_id)
        models.update_mission(
            mission_id,
            current_stage=stage["name"],
            note=f"Resumed from run #{from_run['id']} → retry '{stage['name']}' (run #{new_run_id})",
        )
        import session_manager

        session_manager.launch_pending()
        return new_run_id

    raise ValueError(f"Run #{from_run['id']} cannot be resumed (status: {from_run['status']})")


def _schedule_stage_run(
    workspace,
    mission: dict,
    stage: dict,
    parent_run_id: int | None,
    *,
    current_stage: str,
    note: str,
    fix_loop_count: int | None = None,
) -> int:
    """Create a pending stage run, keep mission running, and wake the queue worker."""
    new_run_id = _create_stage_run(workspace, mission, stage, parent_run_id)
    if "run #" not in note:
        note = f"{note} (run #{new_run_id})"
    updates: dict = {
        "status": "running",
        "current_stage": current_stage,
        "note": note,
    }
    if fix_loop_count is not None:
        updates["fix_loop_count"] = fix_loop_count
    models.update_mission(mission["id"], **updates)
    import session_manager

    session_manager.launch_pending()
    return new_run_id


def advance_mission(run_id: int) -> None:
    """
    Called whenever a run finishes (done/failed/rejected). No-op for
    runs that aren't part of a mission, or aren't in a terminal state.
    """
    run = models.get_run(run_id)
    if run is None or not run["mission_id"]:
        return
    if run["status"] not in ("done", "failed", "rejected", "cancelled"):
        return

    mission = models.get_mission(run["mission_id"])
    if mission is None:
        return
    if mission["template"] == "mad_scientist":
        import mad_scientist

        mad_scientist.advance_mission(run_id)
        return

    template = get_template(mission["template"])
    if template is None:
        if mission["status"] == "running":
            models.update_mission(mission["id"], status="failed", note="Mission template no longer exists")
        return

    stage = _stage_by_name(template, run["stage_name"])
    if stage is None:
        if mission["status"] == "running":
            models.update_mission(mission["id"], status="failed", note=f"Unknown stage: {run['stage_name']}")
        return

    workspace = models.get_workspace_by_id(run["workspace_id"])

    if run["status"] in ("rejected", "cancelled"):
        if mission["status"] != "running":
            return
        note = (
            f"Run cancelled at stage '{stage['name']}' - mission stopped."
            if run["status"] == "cancelled"
            else f"Patch rejected at stage '{stage['name']}' - mission stopped."
        )
        models.update_mission(mission["id"], status="aborted", note=note)
        return

    if run["status"] == "failed":
        on_fail = stage.get("on_fail", "stop")
        if isinstance(on_fail, str) and on_fail.startswith("jump:"):
            if mission["status"] not in ("running", "failed"):
                return
            max_loops = template.get("max_fix_loops", config.DEFAULT_MAX_FIX_LOOPS)
            if mission["fix_loop_count"] >= max_loops:
                detail = _run_failure_detail(run)
                models.update_mission(
                    mission["id"], status="failed",
                    note=(
                        f"Fix-loop limit reached ({max_loops}) after stage "
                        f"'{stage['name']}' failed. Last failure: {detail}"
                    ),
                )
                return
            jump_target = on_fail.split(":", 1)[1]
            next_stage = _stage_by_name(template, jump_target)
            if next_stage is None:
                models.update_mission(
                    mission["id"], status="failed",
                    note=f"on_fail jump target '{jump_target}' does not exist",
                )
                return
            attempt = mission["fix_loop_count"] + 1
            _schedule_stage_run(
                workspace,
                mission,
                next_stage,
                parent_run_id=run_id,
                current_stage=next_stage["name"],
                note=(
                    f"Stage '{stage['name']}' failed - retry {attempt}/{max_loops} "
                    f"via '{jump_target}'"
                ),
                fix_loop_count=attempt,
            )
            return

        if mission["status"] != "running":
            return
        models.update_mission(
            mission["id"], status="failed",
            note=f"Stage '{stage['name']}' failed and has no recovery path.",
        )
        return

    # status == "done"
    if mission["status"] != "running":
        return
    next_name = _next_stage_name(template, stage)
    if next_name is None:
        models.update_mission(mission["id"], status="done", current_stage=stage["name"])
        return

    next_stage = _stage_by_name(template, next_name)
    if next_stage is None:
        models.update_mission(
            mission["id"], status="failed",
            note=f"next stage '{next_name}' does not exist",
        )
        return

    new_run_id = _schedule_stage_run(
        workspace,
        mission,
        next_stage,
        parent_run_id=run_id,
        current_stage=next_stage["name"],
        note=f"Advanced to stage '{next_stage['name']}' (run queued)",
    )


load_templates()
