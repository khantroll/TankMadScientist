"""Lightweight verification failure presentation helpers.

These classify failures heuristically for the UI. They do not install packages
or change execution behavior.
"""
from __future__ import annotations

import os
import re
import shlex

import models


_MISSING_DEP_MARKERS = (
    "no module named",
    "modulenotfounderror",
    "cannot find module",
    "could not find a version that satisfies",
    "is not recognized as an internal or external command",
    "command not found",
    ": not found",
    "no such file or directory",
)

_ENV_MARKERS = (
    "permission denied",
    "access is denied",
    "not a directory",
    "python was not found",
    "unable to create process",
    "dll load failed",
)

_CODE_MARKERS = (
    "assertionerror",
    "failed",
    "error:",
    "traceback",
    "exception:",
)


def _field(row, key, default=None):
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def classify_verification_failure(text: str, command: str | None = None) -> str:
    """Return Tank's interpretation of a verification failure.

    Values: missing_dependency | environment_failure | code_failure | unknown
    """
    blob = f"{command or ''}\n{text or ''}".lower()
    if not blob.strip():
        return "unknown"
    if any(marker in blob for marker in _MISSING_DEP_MARKERS):
        return "missing_dependency"
    if any(marker in blob for marker in _ENV_MARKERS):
        return "environment_failure"
    # Prefer code failure when pytest actually ran and reported failures.
    if "pytest" in blob and any(marker in blob for marker in ("failed", "error", "traceback")):
        if "no module named pytest" in blob or "no module named 'pytest'" in blob:
            return "missing_dependency"
        return "code_failure"
    if "traceback" in blob or "assertionerror" in blob:
        return "code_failure"
    if any(marker in blob for marker in _CODE_MARKERS) and "exited" in blob:
        return "code_failure"
    return "unknown"


def _split_command(command: str) -> list[str]:
    text = (command or "").strip()
    if not text:
        return []
    # Windows paths with spaces (C:\Program Files\...) break posix shlex.
    if re.search(r"[A-Za-z]:\\", text) or ".exe" in text.lower():
        match = re.match(
            r'^("([^"]+)"|(\S*\.exe)|(.+?\.exe))\s+-m\s+(\S+)(.*)$',
            text,
            flags=re.IGNORECASE,
        )
        if match:
            exe = match.group(2) or match.group(3) or match.group(4)
            module = match.group(5)
            rest = (match.group(6) or "").strip()
            parts = [exe, "-m", module]
            if rest:
                parts.extend(rest.split())
            return parts
        # Fall back: split on " -m " for interpreter extraction.
        if " -m " in text:
            left, right = text.split(" -m ", 1)
            left = left.strip().strip('"')
            right_parts = right.split()
            return [left, "-m", *right_parts]
    try:
        return shlex.split(text, posix=os.name != "nt")
    except ValueError:
        return text.split()


def _interpreter_from_command(command: str) -> str:
    parts = _split_command(command)
    if not parts:
        return ""
    # Prefer the executable before -m pytest / -m unittest.
    if len(parts) >= 3 and parts[1] == "-m":
        return parts[0]
    lowered = parts[0].lower()
    if "python" in lowered or lowered.endswith(".exe") or "/" in parts[0] or "\\" in parts[0]:
        return parts[0]
    return parts[0]


_CLASSIFY_LABELS = {
    "missing_dependency": "missing dependency",
    "environment_failure": "environment failure",
    "code_failure": "code failure",
    "unknown": "unknown",
}


def verification_view(run) -> dict | None:
    """Structured verification details for a run, or None when not applicable."""
    if run is None:
        return None
    run_id = _field(run, "id")
    payload = models.get_run_payload(run_id) if run_id is not None else None
    post = (payload or {}).get("post_actions") or {}
    command = str(post.get("test_command") or "").strip()
    exit_code = (payload or {}).get("tool_exit_code")
    error = str(_field(run, "error") or "").strip()
    status = _field(run, "status")
    ran_tests = bool(post.get("run_tests")) and bool(command)
    if not ran_tests and exit_code is None and not (
        error and "verification" in error.lower()
    ):
        return None
    kind = None
    if status in ("failed", "rejected") or (isinstance(exit_code, int) and exit_code != 0):
        kind = classify_verification_failure(error, command)
    snippet = error
    if error.lower().startswith("verification command exited"):
        # Keep the full card text; UI can show command/exit separately.
        pass
    return {
        "command": command or None,
        "interpreter": _interpreter_from_command(command) or None,
        "exit_code": exit_code if isinstance(exit_code, int) else None,
        "snippet": snippet or None,
        "classification": kind,
        "classification_label": _CLASSIFY_LABELS.get(kind) if kind else None,
    }
