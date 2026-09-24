"""Approval view for a run that is waiting on a person.

Controls are offered only when the run is awaiting_approval and the stored
payload is a plan or a patch with at least one patch. An empty waiting
status does not render Approve or Reject.
"""
from __future__ import annotations

import models


def _field(row, key, default=None):
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def approval_state(run) -> dict:
    run_id = _field(run, "id")
    status = _field(run, "status")
    payload = None
    if run_id is not None:
        try:
            payload = models.get_run_payload(run_id)
        except (TypeError, ValueError):
            payload = None
    response_type = payload.get("response_type") if payload else None
    patches = payload.get("patches") if payload else None
    patch_paths = [
        str(patch.get("path")).strip()
        for patch in (patches or [])
        if isinstance(patch, dict) and str(patch.get("path") or "").strip()
    ]
    has_actionable_payload = response_type == "plan" or (
        response_type == "patch" and bool(patches)
    )
    approval_available = status == "awaiting_approval" and has_actionable_payload
    return {
        "run_id": run_id,
        "effective_status": status,
        "has_pending_payload": bool(payload),
        "response_type": response_type,
        "patch_count": len(patches or []),
        "patch_paths": patch_paths,
        "approval_available": approval_available,
    }
