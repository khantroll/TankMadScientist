"""Mission debrief derived from existing mission history.

Only surfaces fields Tank can actually derive from missions, steps, attempts,
run payloads, Pattern Synthesis, evaluations, and workspace memory.
"""
from __future__ import annotations

import mad_scientist_graph as graph
import models
import pattern_synthesis
import verification_display


def _payload_paths(run) -> list[str]:
    payload = models.get_run_payload(run["id"]) or {}
    paths = []
    for patch in payload.get("patches") or []:
        if not isinstance(patch, dict):
            continue
        path = str(patch.get("path") or "").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _synthesis_counts(mission_id: int) -> dict:
    view = pattern_synthesis.public_view(mission_id) or {}
    specialists = view.get("specialists") or []
    generated_specs = sum(1 for item in specialists if item.get("action") == "generate")
    reused_specs = sum(1 for item in specialists if item.get("action") == "reuse")
    adapted_specs = sum(1 for item in specialists if item.get("action") == "adapt")
    crew_action = view.get("action")
    return {
        "specialists_generated": generated_specs,
        "specialists_reused": reused_specs,
        "specialists_adapted": adapted_specs,
        "crew_action": crew_action,
        "crew_name": view.get("name"),
        "crew_rationale": view.get("rationale") or "",
        "rejected_crew": None,
        "synthesis": view,
    }


def build_debrief(mission_id: int) -> dict | None:
    mission = models.get_mission(mission_id)
    if mission is None:
        return None
    view = graph.mission_view(mission_id)
    runs = list(reversed(models.list_mission_runs(mission_id, limit=200)))
    synthesis = _synthesis_counts(mission_id)
    pub = synthesis.get("synthesis") or {}
    if pub.get("action") == "generate":
        closest = pub.get("closest_rejected")
        if isinstance(closest, dict):
            synthesis["rejected_crew"] = closest.get("name") or closest.get("id")
        elif closest:
            synthesis["rejected_crew"] = str(closest)
        if not synthesis["rejected_crew"]:
            for spec in pub.get("specialists") or []:
                if spec.get("closest_rejected"):
                    rejected = spec.get("closest_rejected")
                    if isinstance(rejected, dict):
                        synthesis["rejected_crew"] = rejected.get("name") or rejected.get("id")
                    else:
                        synthesis["rejected_crew"] = str(rejected)
                    break

    completed = []
    failed_or_blocked = []
    files_created = []
    files_modified = []
    verification_attempts = []
    blockers = []
    circuit_events = []
    pending_approvals = []
    pending_installs = []

    if view:
        pending_approvals = list(view.get("waiting_approvals") or [])
        pending_installs = list(view.get("pending_installs") or [])
        if view.get("blocked_reason"):
            blockers.append(view["blocked_reason"])
        for step in view.get("steps") or []:
            status = step.get("status")
            display = step.get("display_status")
            entry = {
                "name": step.get("name"),
                "role": step.get("role"),
                "status": status,
                "display_status": display,
                "attempt_count": step.get("attempt_count") or 0,
                "blocked_reason": step.get("blocked_reason") or "",
                "failure_reason": step.get("failure_reason") or "",
            }
            if status == "done":
                completed.append(entry)
            if status in ("failed", "rejected", "cancelled") or display == "blocked":
                failed_or_blocked.append(entry)
            if step.get("blocked_reason"):
                reason = step["blocked_reason"]
                if reason not in blockers:
                    blockers.append(reason)
                if "repeated" in reason.lower() or "attempt cap" in reason.lower():
                    circuit_events.append({
                        "step": step.get("name"),
                        "reason": reason,
                        "attempt_count": step.get("attempt_count") or 0,
                    })
            for attempt in step.get("attempts") or []:
                run = models.get_run(attempt["run_id"])
                if run is None:
                    continue
                details = verification_display.verification_view(run)
                if details and (details.get("command") or details.get("exit_code") is not None):
                    verification_attempts.append({
                        "step": step.get("name"),
                        "run_id": attempt["run_id"],
                        "attempt_kind": attempt.get("attempt_kind"),
                        "status": attempt.get("status"),
                        **details,
                    })
                if attempt.get("error") and "repeated" in (attempt.get("error") or "").lower():
                    circuit_events.append({
                        "step": step.get("name"),
                        "reason": attempt["error"],
                        "run_id": attempt["run_id"],
                    })

    for run in runs:
        for path in _payload_paths(run):
            # Local agent apply writes whole-file content; treat as modified/created
            # without inventing git status. Prefer "modified" once seen.
            if path not in files_modified and path not in files_created:
                if run["status"] == "done":
                    files_modified.append(path)
                else:
                    files_created.append(path)
            elif path in files_created and run["status"] == "done":
                files_created.remove(path)
                if path not in files_modified:
                    files_modified.append(path)

    # Files from patches that were approved/applied end up as done runs.
    # Keep a single files_touched list for the UI when create vs modify is ambiguous.
    files_touched = []
    for path in files_modified + files_created:
        if path not in files_touched:
            files_touched.append(path)

    memory = models.get_workspace_memory(mission["workspace_id"], "mad_scientist") or {}
    lessons = list(memory.get("known_pitfalls") or [])[:12]
    patterns = list(memory.get("prior_successful_patterns") or [])[:12]

    crews_generated = 1 if synthesis.get("crew_action") == "generate" else 0
    crews_reused = 1 if synthesis.get("crew_action") == "reuse" else 0
    crews_adapted = 1 if synthesis.get("crew_action") == "adapt" else 0

    status = mission["status"]
    if view and view.get("status"):
        status = view["status"]

    show = status in (
        "done",
        "failed",
        "aborted",
        "cancelled",
        graph.BLOCKED_HUMAN,
        "BLOCKED_HUMAN",
    ) or bool(pending_approvals) or bool(failed_or_blocked)

    return {
        "mission_id": mission_id,
        "objective": mission["goal"],
        "status": status,
        "note": mission["note"] or "",
        "show": show,
        "completed_steps": completed,
        "failed_or_blocked_steps": failed_or_blocked,
        "files_touched": files_touched,
        "files_modified": files_modified,
        "files_created": files_created,
        "files_deleted": [],  # not tracked in current schema
        "specialists_generated": synthesis["specialists_generated"],
        "specialists_reused": synthesis["specialists_reused"],
        "specialists_adapted": synthesis["specialists_adapted"],
        "crews_generated": crews_generated,
        "crews_reused": crews_reused,
        "crews_adapted": crews_adapted,
        "crew_name": synthesis.get("crew_name"),
        "crew_rationale": synthesis.get("crew_rationale"),
        "rejected_crew": synthesis.get("rejected_crew"),
        "verification_attempts": verification_attempts,
        "blockers": blockers,
        "circuit_events": circuit_events,
        "lessons": lessons,
        "successful_patterns": patterns,
        "pending_approvals": pending_approvals,
        "pending_approval_count": len(pending_approvals),
        "pending_installs": pending_installs,
        "summary": (view or {}).get("summary") or "",
    }
