"""Approval-gated recovery for missing third-party dependencies.

Installs are never automatic. A concrete missing-module error must be present,
the module must look installable (not a local project module), and a human must
approve the interpreter-scoped install before Tank retries the failed step.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
import arsenal
import local_agent
import mad_scientist
import mad_scientist_graph as graph
import models
import verification_display


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _quote_exe(path: str) -> str:
    text = (path or "").strip()
    if not text:
        return "python"
    if " " in text and not (text.startswith('"') and text.endswith('"')):
        return f'"{text}"'
    return text


def attach_install_offer(run_id: int, repo_path: str | None = None) -> dict | None:
    """If the run failed on an installable module, store an install_offer on the payload."""
    run = models.get_run(run_id)
    if run is None:
        return None
    workspace = models.get_workspace_by_id(run["workspace_id"])
    cwd = repo_path or (workspace["repo_path"] if workspace is not None else None)
    view = verification_display.verification_view(run, repo_path=cwd)
    if not view or not view.get("installable") or not view.get("missing_module"):
        return None
    offer = {
        "module": view["missing_module"],
        "interpreter": view.get("interpreter"),
        "install_command": view.get("install_command"),
        "cwd": view.get("cwd") or cwd,
        "original_command": view.get("command"),
        "classification": view.get("classification"),
        "status": "pending",
        "created_at": _now(),
        "run_id": run_id,
    }
    payload = models.get_run_payload(run_id) or {}
    payload["install_offer"] = offer
    if view.get("cwd") and not payload.get("verification_cwd"):
        payload["verification_cwd"] = view["cwd"]
    models.update_run(run_id, agent_payload=json.dumps(payload))
    return offer


def install_offer_for_step(step_id: int) -> dict | None:
    """Newest pending install offer among attempts on this step."""
    for attempt in reversed(graph.list_attempts(step_id)):
        run = models.get_run(attempt["run_id"])
        if run is None:
            continue
        payload = models.get_run_payload(run["id"]) or {}
        offer = payload.get("install_offer")
        if isinstance(offer, dict) and offer.get("status") in (None, "pending", "failed"):
            offer = dict(offer)
            offer["run_id"] = run["id"]
            offer["step_id"] = step_id
            return offer
        # Also derive an offer live if payload never got one.
        view = verification_display.verification_view(run)
        if view and view.get("installable") and view.get("missing_module"):
            return {
                "module": view["missing_module"],
                "interpreter": view.get("interpreter"),
                "install_command": view.get("install_command"),
                "cwd": view.get("cwd"),
                "original_command": view.get("command"),
                "classification": view.get("classification"),
                "status": "pending",
                "run_id": run["id"],
                "step_id": step_id,
            }
    return None


def _update_offer(run_id: int, **fields) -> dict | None:
    payload = models.get_run_payload(run_id) or {}
    offer = payload.get("install_offer")
    if not isinstance(offer, dict):
        offer = {}
    offer.update(fields)
    payload["install_offer"] = offer
    models.update_run(run_id, agent_payload=json.dumps(payload))
    return offer


def run_approved_install(
    mission_id: int,
    step_id: int,
    *,
    module: str | None = None,
) -> dict:
    """Install an approved package with the failing interpreter, then resume the step."""
    mission = models.get_mission(mission_id)
    if mission is None:
        raise ValueError("Mission not found")
    step = graph.get_step(step_id)
    if step is None:
        raise ValueError("Step not found")
    offer = install_offer_for_step(step_id)
    if offer is None:
        raise ValueError("No installable dependency offer on this step")
    module_name = (module or offer.get("module") or "").strip()
    if not module_name:
        raise ValueError("Missing module name")
    workspace = models.get_workspace_by_id(mission["workspace_id"])
    if workspace is None:
        raise ValueError("Workspace not found")
    repo_path = workspace["repo_path"]
    if not verification_display.is_installable_module(repo_path, module_name):
        raise ValueError(
            f"Refusing to install '{module_name}' — Tank reads it as a local "
            "project/import-context module, not a third-party package."
        )

    interpreter = (offer.get("interpreter") or "").strip()
    if not interpreter:
        view_run = models.get_run(offer["run_id"])
        view = verification_display.verification_view(view_run, repo_path=repo_path) or {}
        interpreter = (view.get("interpreter") or "python").strip()
    install_cmd = offer.get("install_command") or verification_display.build_install_command(
        interpreter, module_name
    )
    if not install_cmd:
        raise ValueError("Could not build an install command")

    run_id = int(offer["run_id"])
    run = models.get_run(run_id)
    log_path = run["log_path"] if run is not None else None
    if log_path:
        local_agent._log(
            log_path,
            f"\n[tank] approved dependency install: {install_cmd}\n"
            f"[tank] install cwd: {repo_path}\n",
        )

    exe = interpreter
    # Build argv without shell so spaces in Windows Python paths work.
    if exe.startswith('"') and exe.endswith('"'):
        exe = exe[1:-1]
    argv = [exe, "-m", "pip", "install", module_name]
    # Prefer PyPI name from the computed command when it differs.
    if install_cmd:
        parts = install_cmd.strip().split()
        if parts and parts[-1] and parts[-1] != module_name:
            argv[-1] = parts[-1]

    try:
        result = subprocess.run(
            argv,
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        message = local_agent.redact_command_output(str(exc))
        if log_path:
            local_agent._log(log_path, f"[tank] install failed: {message}\n")
        _update_offer(
            run_id,
            status="failed",
            finished_at=_now(),
            exit_code=1,
            output=message,
            approved=True,
            install_argv=argv,
        )
        raise ValueError(f"Install failed to start: {message}") from exc

    stdout = local_agent.redact_command_output(result.stdout or "")
    stderr = local_agent.redact_command_output(result.stderr or "")
    output = "\n".join(part for part in (stderr, stdout) if part).strip()
    if log_path:
        if output:
            local_agent._log(log_path, output + "\n")
        local_agent._log(log_path, f"[tank] install exit code: {result.returncode}\n")

    event = {
        "module": module_name,
        "interpreter": interpreter,
        "command": " ".join(argv),
        "cwd": repo_path,
        "exit_code": int(result.returncode),
        "output": output[:2000],
        "mission_id": mission_id,
        "step_id": step_id,
        "run_id": run_id,
        "approved": True,
        "finished_at": _now(),
    }

    if result.returncode != 0:
        _update_offer(run_id, status="failed", **event)
        arsenal.record_lesson(
            mission["workspace_id"],
            f"Failed to install {module_name} with {interpreter}: exit {result.returncode}",
            "environment",
            mission_id=mission_id,
            run_id=run_id,
            source="dependency_install",
            details=event,
        )
        snippet = local_agent.command_output_snippet(output) or f"exit {result.returncode}"
        raise ValueError(
            f"Install of {module_name} failed (exit {result.returncode}): {snippet}"
        )

    _update_offer(run_id, status="installed", **event)
    arsenal.record_lesson(
        mission["workspace_id"],
        f"Installed {module_name} into {interpreter}",
        "environment",
        mission_id=mission_id,
        run_id=run_id,
        source="dependency_install",
        details=event,
    )
    arsenal.record_lesson(
        mission["workspace_id"],
        f"Approved dependency install for verification: {module_name}",
        "verification",
        mission_id=mission_id,
        run_id=run_id,
        source="dependency_install",
        details=event,
    )
    # Resume the blocked/failed step with a fresh breaker epoch.
    if step["status"] != graph.BLOCKED_HUMAN:
        # Ensure the step is blocked so resume path can clear it, or reset directly.
        graph.block_step(step_id, f"Awaiting retry after installing {module_name}")
        graph.block_mission(mission_id, f"Awaiting retry after installing {module_name}")
        models.update_mission(mission_id, status=graph.BLOCKED_HUMAN, note=f"Installed {module_name}; resuming")
    new_run_id = mad_scientist.resume_blocked_step(mission_id, step_id=step_id)
    return {
        "ok": True,
        "module": module_name,
        "install_command": " ".join(argv),
        "exit_code": 0,
        "resumed_run_id": new_run_id,
        "event": event,
    }


def dismiss_install_offer(mission_id: int, step_id: int) -> None:
    offer = install_offer_for_step(step_id)
    if offer is None:
        raise ValueError("No install offer on this step")
    _update_offer(int(offer["run_id"]), status="dismissed", dismissed_at=_now())
    mission = models.get_mission(mission_id)
    if mission is not None and mission["parent_run_id"]:
        parent = models.get_run(mission["parent_run_id"])
        if parent is not None and parent["log_path"]:
            local_agent._log(
                parent["log_path"],
                f"\n[tank] dependency install dismissed for step #{step_id} "
                f"module={offer.get('module')}\n",
            )


def retry_without_install(mission_id: int, step_id: int) -> int:
    """Resume the step without installing; keep the offer marked dismissed."""
    offer = install_offer_for_step(step_id)
    if offer is not None:
        _update_offer(int(offer["run_id"]), status="retry_without_install", dismissed_at=_now())
    step = graph.get_step(step_id)
    if step is not None and step["status"] != graph.BLOCKED_HUMAN:
        graph.block_step(step_id, "Retry requested without installing missing dependency")
        graph.block_mission(mission_id, "Retry requested without installing missing dependency")
        models.update_mission(
            mission_id,
            status=graph.BLOCKED_HUMAN,
            note="Retry without install",
        )
    return mad_scientist.resume_blocked_step(mission_id, step_id=step_id)
