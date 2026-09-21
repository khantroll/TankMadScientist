"""
Spawns and supervises agent runs via pluggable providers.

Claude Code runs shell out to the CLI; OpenAI-compatible providers
(LM Studio, OpenRouter, etc.) stream chat completions over HTTP.

Output streams to a per-run log file under data/logs/, which the
dashboard tails for the live output panel.
"""
import json
import os
import signal
import subprocess
import threading
from datetime import datetime, timezone

import config
import local_agent
import mission
import models
import providers
import run_chain
from providers import ProviderError, RunContext

_active_lock = threading.Lock()
_active_runs: dict[int, dict] = {}


def _register_active_run(run_id: int, cancel_event: threading.Event) -> None:
    with _active_lock:
        _active_runs[run_id] = {"cancel": cancel_event, "proc": None}


def register_active_proc(run_id: int, proc: subprocess.Popen) -> None:
    with _active_lock:
        if run_id in _active_runs:
            _active_runs[run_id]["proc"] = proc


def _unregister_active_run(run_id: int) -> None:
    with _active_lock:
        _active_runs.pop(run_id, None)


def _terminate_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def launch_run(run_id):
    """Start a single pending run. Safe to call directly for a manual launch."""
    run = models.get_run(run_id)
    if run is None or run["status"] != "pending":
        return

    workspace = models.get_workspace_by_id(run["workspace_id"])
    repo_error = config.check_repo_path(workspace["repo_path"])
    if repo_error:
        log_path = os.path.join(config.LOGS_DIR, f"run-{run_id}.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"[tank] error: {repo_error}\n")
        models.update_run(
            run_id,
            status="failed",
            error=repo_error,
            log_path=log_path,
            finished_at=_now(),
        )
        mission.advance_mission(run_id)
        return

    role = models.get_role_by_id(run["role_id"])
    log_path = os.path.join(config.LOGS_DIR, f"run-{run_id}.log")
    provider_id = providers.resolve_provider(run["provider"])

    parent_run_id = run_chain.resolve_parent_run_id(run)
    prior_run_context = None
    if parent_run_id:
        prior_run_context = run_chain.build_prior_context(parent_run_id)
        if prior_run_context and not run["parent_run_id"]:
            models.update_run(run_id, parent_run_id=parent_run_id)

    cancel_event = threading.Event()

    # Build the role dict, then merge any per-step persona overrides written by
    # the crew orchestrator.  Overrides take precedence over the saved role so
    # the GUI builder can reshape goal / backstory / tools on a per-step basis.
    # prompt_library.compose_system_prompt reads these keys directly from the
    # dict, so no other files need to change.
    role_dict: dict = dict(role) if role else {}
    _persona_raw: str | None = None
    try:
        _persona_raw = run["persona_override"]
    except (IndexError, KeyError):
        pass
    if _persona_raw:
        try:
            _ov = json.loads(_persona_raw)
            for _k in ("goal", "backstory", "tools"):
                _v = (_ov.get(_k) or "").strip()
                if _v:
                    role_dict[_k] = _v
        except (ValueError, TypeError):
            pass

    ctx = RunContext(
        run_id=run_id,
        task=run["task"],
        system_prompt=role_dict.get("system_prompt"),
        workspace=dict(workspace),
        role=role_dict or None,
        log_path=log_path,
        provider_id=provider_id,
        prior_run_context=prior_run_context,
        parent_run_id=parent_run_id,
        mission_id=run["mission_id"],
        stage_name=run["stage_name"],
        cancel_event=cancel_event,
    )

    if parent_run_id:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"[tank] chained from run #{parent_run_id}\n")

    def _on_finished(returncode):
        current = models.get_run(run_id)
        if current and current["status"] == "cancelled":
            _unregister_active_run(run_id)
            return
        models.update_run(
            run_id,
            status="done" if returncode == 0 else "failed",
            finished_at=_now(),
        )
        mission.advance_mission(run_id)
        _unregister_active_run(run_id)

    _register_active_run(run_id, cancel_event)

    try:
        pid = providers.start_run(ctx, on_finished=_on_finished)
    except ProviderError as exc:
        models.update_run(
            run_id, status="failed", error=str(exc), finished_at=_now()
        )
        _unregister_active_run(run_id)
        mission.advance_mission(run_id)
        return

    models.update_run(
        run_id,
        status="running",
        provider=provider_id,
        pid=pid,
        log_path=log_path,
        started_at=_now(),
    )


def launch_pending():
    """Launch queued runs up to the configured parallelism cap."""
    available = config.MAX_PARALLEL_RUNS - models.count_running_runs()
    if available <= 0:
        return
    for run in models.list_pending_runs()[:available]:
        launch_run(run["id"])


def approve_run(run_id):
    """Apply an approved local-agent patch (or acknowledge a plan) and advance."""
    run = models.get_run(run_id)
    if run is None or run["status"] != "awaiting_approval":
        return False

    payload = models.get_run_payload(run_id)
    if not payload:
        return False

    response_type = payload.get("response_type")

    # Plan-type responses require no file changes — acknowledgement just marks done.
    if response_type == "plan":
        if run["log_path"]:
            local_agent._log(run["log_path"], "\n[tank] plan acknowledged by user\n")
        models.update_run(run_id, status="done", finished_at=_now())
        mission.advance_mission(run_id)
        return True

    if response_type != "patch":
        return False

    workspace = models.get_workspace_by_id(run["workspace_id"])

    # Build the role dict and merge any per-step persona override (same logic
    # as launch_run).  This ensures that dynamic GUI crew sub-runs — which may
    # have role_id=NULL but carry custom tools in persona_override — correctly
    # restrict (or leave unrestricted) the tool authorization during apply.
    role = models.get_role_by_id(run["role_id"])
    role_dict: dict = dict(role) if role else {}
    _persona_raw: str | None = None
    try:
        _persona_raw = run["persona_override"]
    except (IndexError, KeyError):
        pass
    if _persona_raw:
        try:
            _ov = json.loads(_persona_raw)
            for _k in ("goal", "backstory", "tools"):
                _v = (_ov.get(_k) or "").strip()
                if _v:
                    role_dict[_k] = _v
        except (ValueError, TypeError):
            pass

    authorized = local_agent._authorized_tools(role_dict or None)
    try:
        code = local_agent.finalize_approved_run(
            workspace["repo_path"], payload, run["log_path"],
            workspace=dict(workspace), authorized_tools=authorized,
        )
        models.update_run(
            run_id,
            status="done" if code == 0 else "failed",
            finished_at=_now(),
        )
    except Exception as exc:
        local_agent._log(run["log_path"], f"[tank] apply failed: {exc}\n")
        models.update_run(
            run_id, status="failed", error=str(exc), finished_at=_now()
        )
    mission.advance_mission(run_id)
    return True


def cancel_run(run_id):
    """Stop a pending, running, or awaiting-approval run."""
    run = models.get_run(run_id)
    if run is None or run["status"] not in ("pending", "running", "awaiting_approval"):
        return False

    with _active_lock:
        active = _active_runs.get(run_id)

    if active:
        active["cancel"].set()
        proc = active.get("proc")
        if proc is not None:
            _terminate_proc(proc)
    elif run["pid"]:
        try:
            os.kill(run["pid"], signal.SIGTERM)
        except OSError:
            pass

    if run["log_path"]:
        local_agent._log(run["log_path"], "\n[tank] run cancelled by user\n")

    models.update_run(run_id, status="cancelled", finished_at=_now(), pid=None)
    _unregister_active_run(run_id)
    mission.advance_mission(run_id)
    return True


def reject_run(run_id):
    run = models.get_run(run_id)
    if run is None or run["status"] != "awaiting_approval":
        return False

    local_agent._log(run["log_path"], "\n[tank] patch rejected by user\n")
    models.update_run(run_id, status="rejected", finished_at=_now())
    mission.advance_mission(run_id)
    return True


def tail_log(log_path, max_lines=None):
    if not log_path or not os.path.exists(log_path):
        return ""
    max_lines = max_lines or config.LOG_TAIL_LINES
    with open(log_path, "r", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-max_lines:])


def launch_crew_builder(run_id: int, crew_cfg: dict) -> None:
    """
    Launch a dynamically-configured sequential crew run built via the GUI.

    Bypasses the provider registry entirely — `crew_cfg` is already fully
    specified by the caller (app.py's launch_crew endpoint).  The lifecycle
    mirrors launch_run: cancel_event registered, status → 'running', daemon
    thread drives orchestrator.run_sequential_crew, _on_finished handles
    the terminal transition.

    Guarded against the queue worker picking up the pending run between
    create_adhoc_crew_run() and this call: the first thing we do is mark
    the run 'running' and register the cancel_event, so launch_run() will
    return immediately if it fires in that tiny window.
    """
    run = models.get_run(run_id)
    if run is None or run["status"] != "pending":
        return

    workspace = models.get_workspace_by_id(run["workspace_id"])
    repo_error = config.check_repo_path(workspace["repo_path"])
    if repo_error:
        log_path = os.path.join(config.LOGS_DIR, f"run-{run_id}.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"[tank] error: {repo_error}\n")
        models.update_run(
            run_id,
            status="failed",
            error=repo_error,
            log_path=log_path,
            finished_at=_now(),
        )
        return

    log_path = os.path.join(config.LOGS_DIR, f"run-{run_id}.log")
    cancel_event = threading.Event()

    ctx = RunContext(
        run_id=run_id,
        task=run["task"],
        system_prompt=None,
        workspace=dict(workspace),
        role=None,
        log_path=log_path,
        provider_id="crew_builder",
        cancel_event=cancel_event,
    )

    def _on_finished(returncode):
        current = models.get_run(run_id)
        if current and current["status"] == "cancelled":
            _unregister_active_run(run_id)
            return
        models.update_run(
            run_id,
            status="done" if returncode == 0 else "failed",
            finished_at=_now(),
        )
        mission.advance_mission(run_id)
        _unregister_active_run(run_id)

    # Register cancel_event BEFORE updating status so cancel_run() can
    # always find the event in _active_runs if it fires concurrently.
    _register_active_run(run_id, cancel_event)
    models.update_run(
        run_id,
        status="running",
        log_path=log_path,
        started_at=_now(),
    )

    def _watch():
        import orchestrator
        orchestrator.run_sequential_crew(ctx, crew_cfg, _on_finished)

    threading.Thread(target=_watch, daemon=True).start()


def queue_worker_loop(stop_event):
    """Run forever (in a background thread) picking up pending runs."""
    while not stop_event.is_set():
        launch_pending()
        stop_event.wait(config.POLL_INTERVAL_SECONDS)
