"""Shared approval view helpers for run cards and Mad Scientist graph steps."""
from __future__ import annotations

import config
import models


def _field(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = default
    return value


def approval_state(run) -> dict:
    run_id = _field(run, "id")
    status = _field(run, "status")
    try:
        payload = models.get_run_payload(run_id)
    except (TypeError, ValueError):
        payload = None
    response_type = payload.get("response_type") if payload else None
    patches = payload.get("patches") if payload else None
    patch_paths = [
        str(patch.get("path")).strip()
        for patch in (patches or [])
        if str(patch.get("path") or "").strip()
    ]
    has_actionable_payload = (
        response_type == "plan"
        or (response_type == "patch" and bool(patches))
    )
    approval_available = status == "awaiting_approval" and has_actionable_payload
    debug = (
        "approval-render "
        f"run_id={run_id} "
        f"status={status} "
        f"provider={_field(run, 'provider', '')} "
        f"mission_id={_field(run, 'mission_id', '')} "
        f"crew_run_id={_field(run, 'crew_run_id', '')} "
        f"parent_run_id={_field(run, 'parent_run_id', '')} "
        f"has_payload={bool(payload)} "
        f"response_type={response_type or ''} "
        f"patch_count={len(patches or [])} "
        f"approval_controls={approval_available}"
    )
    return {
        "run_id": run_id,
        "effective_status": status,
        "has_pending_payload": bool(payload),
        "response_type": response_type,
        "patch_count": len(patches or []),
        "patch_paths": patch_paths,
        "approval_available": approval_available,
        "debug_comment": debug if config.DEBUG_APPROVAL_RENDER else None,
    }


def run_view(run) -> dict:
    view = dict(run)
    view["approval"] = approval_state(run)
    return view


def actionable_approval_runs(workspace_id: int, limit: int = 20) -> list[dict]:
    approvals = []
    for run in models.list_runs(workspace_id=workspace_id, limit=100):
        view = run_view(run)
        if view["approval"]["approval_available"]:
            approvals.append(view)
            if len(approvals) >= limit:
                break
    return approvals
