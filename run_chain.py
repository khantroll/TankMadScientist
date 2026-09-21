"""Resolve and format output from prior runs for chained tasks."""
import os
import re

import config
import local_agent
import models
import output_quality
import repo_context

CHAINABLE_STATUSES = models.CHAINABLE_PARENT_STATUSES

PRIOR_RUN_HINT = re.compile(
    r"\b(previous|prior|last)\s+(run|audit|review|task|output|report|analysis)\b"
    r"|\bbased on (the )?(previous|prior|last)\b"
    r"|\bfrom (the )?(previous|prior|last)\s+(run|audit|review)\b"
    r"|\bcontinue (from|with) (the )?(previous|prior|last)\b",
    re.I,
)


def task_implies_prior_run(task: str) -> bool:
    return bool(PRIOR_RUN_HINT.search(task))


def _truncate_bytes(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    trimmed = data[-max_bytes:].decode("utf-8", errors="replace")
    return f"... [prior run output truncated]\n{trimmed}"


def _strip_log_noise(log: str) -> str:
    lines = []
    skip_section = False
    for line in log.splitlines():
        if skip_section:
            if "--- end ---" in line:
                skip_section = False
            continue
        if line.startswith("[tank]") or line.startswith("==="):
            continue
        if "--- raw model response ---" in line:
            skip_section = True
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def extract_run_output(run, max_bytes: int | None = None) -> str | None:
    """Pull the substantive deliverable from a completed run."""
    max_bytes = max_bytes or config.CHAIN_RUN_MAX_BYTES
    parts = []

    payload = models.get_run_payload(run["id"])
    if payload:
        summary = (payload.get("summary") or "").strip()
        if summary:
            parts.append(f"Summary:\n{summary}")
        plan = (payload.get("plan") or "").strip()
        if plan:
            parts.append(f"Output:\n{plan}")
        elif payload.get("response_type") == "patch":
            patch_paths = [
                p.get("path", "?") for p in (payload.get("patches") or [])
            ]
            if patch_paths:
                parts.append(f"Proposed patches: {', '.join(patch_paths)}")

    log_path = run["log_path"]
    has_structured = bool(payload and (payload.get("plan") or "").strip())
    if log_path and os.path.exists(log_path) and not has_structured:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            cleaned = _strip_log_noise(f.read())
        if cleaned:
            parts.append(f"Log output:\n{cleaned}")

    text = "\n\n".join(parts).strip()
    if not text:
        return None
    return _truncate_bytes(text, max_bytes)


def build_prior_context(parent_run_id: int, max_bytes: int | None = None) -> str | None:
    run = models.get_run(parent_run_id)
    if run is None:
        return None
    output = extract_run_output(run, max_bytes=max_bytes)
    if not output:
        return None
    if output_quality.looks_like_meta_plan(output):
        header = (
            f"Prior run #{run['id']} ({run['status']}) — NOTE: prior output was a checklist, "
            f"not real findings. Prefer the repository files below over this prior output.\n"
            f"Prior task: {run['task']}\n"
        )
    else:
        header = (
            f"Prior run #{run['id']} ({run['status']}, provider={run['provider'] or 'unknown'})\n"
            f"Prior task: {run['task']}\n"
        )
    return f"{header}\n{output}"


def resolve_parent_run_id(run) -> int | None:
    """Pick the parent run for a queued run (explicit selection or task hint)."""
    parent_id = run["parent_run_id"]
    if parent_id:
        parent = models.get_run(parent_id)
        if (
            parent
            and parent["workspace_id"] == run["workspace_id"]
            and parent["status"] in CHAINABLE_STATUSES
            and parent["id"] != run["id"]
        ):
            return parent_id
        return None
    if task_implies_prior_run(run["task"]):
        return models.get_latest_chainable_run_id(
            run["workspace_id"], before_run_id=run["id"]
        )
    return None
