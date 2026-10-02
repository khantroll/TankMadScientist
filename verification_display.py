"""Lightweight verification failure presentation helpers.

These classify failures heuristically for the UI and distinguish local project
modules from installable third-party packages. They do not install packages.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import models

_MISSING_MODULE_RE = re.compile(
    r"(?:modulenotfounderror:\s*)?no module named ['\"]?([A-Za-z_][\w.]*)['\"]?",
    re.I,
)
_NODE_MODULE_RE = re.compile(
    r"(?:cannot find module|err_module_not_found)[^'\"]*['\"]([^'\"]+)['\"]",
    re.I,
)

# Clearly third-party / tooling packages Tank may offer to install.
_KNOWN_THIRD_PARTY = {
    "pytest", "flask", "django", "fastapi", "uvicorn", "gunicorn",
    "requests", "httpx", "numpy", "pandas", "sqlalchemy", "pydantic",
    "click", "yaml", "pyyaml", "jinja2", "werkzeug", "apscheduler",
    "black", "ruff", "mypy", "coverage", "pytest_cov", "pytest-cov",
    "flask_jwt_extended", "flask-jwt-extended", "pyjwt", "jwt",
    "bcrypt", "passlib", "celery", "redis", "boto3", "aiohttp",
    "starlette", "typer", "rich", "httpie", "selenium", "playwright",
}

# Names that almost always mean local project / import-context issues.
_LOCAL_DENYLIST = {
    "app", "src", "main", "server", "wsgi", "asgi", "manage",
    "tests", "test", "conftest", "application", "backend", "frontend",
    "api", "core", "lib", "utils", "helpers", "models", "views",
    "routes", "controllers", "services", "config", "settings",
    "extensions", "blueprints", "auth", "users", "profile",
}

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

_CLASSIFY_LABELS = {
    "missing_dependency": "missing dependency",
    "environment_failure": "environment failure",
    "code_failure": "code / test failure",
    "import_context_failure": "import / execution context",
    "unknown": "unknown",
}


def _field(row, key, default=None):
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def extract_missing_module(text: str) -> str | None:
    """Return the top-level missing module name, if the error names one."""
    blob = text or ""
    match = _MISSING_MODULE_RE.search(blob)
    if match:
        return match.group(1).split(".")[0]
    match = _NODE_MODULE_RE.search(blob)
    if match:
        name = match.group(1).strip()
        if name.startswith("."):
            return None
        return name.split("/")[0].split(".")[0]
    return None


def _dep_manifest_names(repo_path: str | None) -> set[str]:
    names: set[str] = set()
    if not repo_path:
        return names
    root = Path(repo_path)
    for rel in (
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "dev-requirements.txt",
    ):
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            pkg = re.split(r"[<>=!~;\[]", line, maxsplit=1)[0].strip().lower()
            pkg = pkg.replace("-", "_")
            if pkg:
                names.add(pkg)
                names.add(pkg.replace("_", "-"))
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for match in re.finditer(r'["\']([A-Za-z0-9_.-]+)["\']', text):
            pkg = match.group(1).lower().replace("-", "_")
            if pkg and not pkg.endswith(".toml"):
                names.add(pkg)
                names.add(pkg.replace("_", "-"))
    return names


def local_module_paths(repo_path: str | None, module: str) -> list[str]:
    """Paths under the repo that would satisfy `import module`."""
    if not repo_path or not module:
        return []
    root = Path(repo_path)
    candidates = [
        root / f"{module}.py",
        root / module / "__init__.py",
        root / "src" / f"{module}.py",
        root / "src" / module / "__init__.py",
    ]
    found = []
    for path in candidates:
        if path.is_file():
            try:
                found.append(str(path.relative_to(root)))
            except ValueError:
                found.append(str(path))
    return found


def module_imported_in_repo(repo_path: str | None, module: str, limit: int = 8) -> list[str]:
    """Return a few source files that import the module."""
    if not repo_path or not module:
        return []
    root = Path(repo_path)
    pattern = re.compile(
        rf"^\s*(?:from\s+{re.escape(module)}\b|import\s+{re.escape(module)}\b)",
        re.M,
    )
    hits = []
    skip_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", "dist", "build"}
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in skip_dirs]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = Path(dirpath) / filename
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if pattern.search(text):
                    try:
                        hits.append(str(path.relative_to(root)))
                    except ValueError:
                        hits.append(str(path))
                    if len(hits) >= limit:
                        return hits
    except OSError:
        return hits
    return hits


def repo_layout_snapshot(repo_path: str | None, limit: int = 40) -> list[str]:
    """Shallow layout listing for fixer / UI context."""
    if not repo_path:
        return []
    root = Path(repo_path)
    if not root.is_dir():
        return []
    entries = []
    skip = {".git", ".venv", "venv", "__pycache__", "node_modules", ".tox"}
    try:
        for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if child.name in skip:
                continue
            if child.is_dir():
                entries.append(child.name + "/")
            else:
                entries.append(child.name)
            if len(entries) >= limit:
                break
    except OSError:
        return entries
    return entries


def is_local_project_module(repo_path: str | None, module: str) -> bool:
    """True when the missing module looks like local project code, not a package."""
    if not module:
        return False
    name = module.strip()
    lowered = name.lower().replace("-", "_")
    if lowered in _LOCAL_DENYLIST:
        return True
    if local_module_paths(repo_path, name) or local_module_paths(repo_path, lowered):
        return True
    if module_imported_in_repo(repo_path, name) or module_imported_in_repo(repo_path, lowered):
        # Imported elsewhere in the project → treat as local/app code.
        # Exception: if it is also a known third-party dep listed in manifests,
        # prefer dependency (e.g. `import flask` while flask is missing).
        manifests = _dep_manifest_names(repo_path)
        if lowered in _KNOWN_THIRD_PARTY or lowered in manifests or name.lower() in manifests:
            if not local_module_paths(repo_path, name):
                return False
        return True
    return False


def is_installable_module(repo_path: str | None, module: str) -> bool:
    """True only when Tank may offer an approval-gated install for this module."""
    if not module:
        return False
    if is_local_project_module(repo_path, module):
        return False
    lowered = module.lower().replace("-", "_")
    if lowered in _LOCAL_DENYLIST:
        return False
    manifests = _dep_manifest_names(repo_path)
    if lowered in _KNOWN_THIRD_PARTY or module.lower() in _KNOWN_THIRD_PARTY:
        return True
    if lowered in manifests or module.lower() in manifests or module.lower().replace("_", "-") in manifests:
        return True
    return False


def classify_verification_failure(
    text: str,
    command: str | None = None,
    *,
    repo_path: str | None = None,
) -> str:
    """Return Tank's interpretation of a verification failure.

    Values:
      missing_dependency | environment_failure | code_failure |
      import_context_failure | unknown
    """
    blob = f"{command or ''}\n{text or ''}".lower()
    if not blob.strip():
        return "unknown"
    module = extract_missing_module(f"{command or ''}\n{text or ''}")
    if module:
        if is_local_project_module(repo_path, module):
            return "import_context_failure"
        if is_installable_module(repo_path, module):
            return "missing_dependency"
        # Unknown module with ModuleNotFoundError: prefer import/context over install.
        return "import_context_failure"
    if any(marker in blob for marker in (
        "is not recognized as an internal or external command",
        "command not found",
        ": not found",
        "no such file or directory",
    )):
        return "missing_dependency"
    if any(marker in blob for marker in _ENV_MARKERS):
        return "environment_failure"
    if "pytest" in blob and any(marker in blob for marker in ("failed", "error", "traceback")):
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
    if len(parts) >= 3 and parts[1] == "-m":
        return parts[0]
    lowered = parts[0].lower()
    if "python" in lowered or lowered.endswith(".exe") or "/" in parts[0] or "\\" in parts[0]:
        return parts[0]
    return parts[0]


def build_install_command(interpreter: str | None, module: str) -> str | None:
    if not module:
        return None
    pkg = module
    lowered = module.lower().replace("-", "_")
    if lowered == "flask_jwt_extended":
        pkg = "Flask-JWT-Extended"
    elif lowered in {"pyyaml", "yaml"}:
        pkg = "PyYAML"
    elif lowered == "pytest_cov":
        pkg = "pytest-cov"
    exe = (interpreter or "").strip() or "python"
    if " " in exe and not (exe.startswith('"') and exe.endswith('"')):
        exe = f'"{exe}"'
    return f"{exe} -m pip install {pkg}"


def verification_view(run, *, repo_path: str | None = None) -> dict | None:
    """Structured verification details for a run, or None when not applicable."""
    if run is None:
        return None
    run_id = _field(run, "id")
    payload = models.get_run_payload(run_id) if run_id is not None else None
    post = (payload or {}).get("post_actions") or {}
    command = str(
        (payload or {}).get("verification_command_resolved")
        or post.get("test_command")
        or ""
    ).strip()
    exit_code = (payload or {}).get("tool_exit_code")
    error = str(_field(run, "error") or "").strip()
    status = _field(run, "status")
    cwd = str(
        (payload or {}).get("verification_cwd")
        or repo_path
        or ""
    ).strip() or None
    if cwd is None and run_id is not None:
        workspace = models.get_workspace_by_id(_field(run, "workspace_id"))
        if workspace is not None:
            cwd = workspace["repo_path"]
            repo_path = repo_path or cwd
    ran_tests = bool(post.get("run_tests")) and bool(command or post.get("test_command"))
    if not ran_tests and exit_code is None and not (
        error and "verification" in error.lower()
    ):
        # Still show import errors that mention modules even without post_actions.
        if not extract_missing_module(error):
            return None
    kind = None
    module = extract_missing_module(error) or extract_missing_module(command)
    if status in ("failed", "rejected") or (isinstance(exit_code, int) and exit_code != 0):
        kind = classify_verification_failure(error, command, repo_path=repo_path or cwd)
    installable = bool(
        kind == "missing_dependency"
        and module
        and is_installable_module(repo_path or cwd, module)
    )
    interpreter = _interpreter_from_command(command) or None
    install_command = None
    if installable:
        install_command = build_install_command(interpreter, module)
    local_paths = local_module_paths(repo_path or cwd, module) if module else []
    diagnosis = pytest_collection_path_hint(error, repo_path or cwd)
    if not diagnosis and kind == "import_context_failure" and module:
        if local_paths:
            diagnosis = (
                f"Local module '{module}' exists at {', '.join(local_paths)}. "
                "Fix the test import or pytest rootdir/pythonpath — do not pip install it."
            )
        else:
            diagnosis = (
                f"Local module '{module}' is missing from the repo root. Create/fix the "
                "application module or correct the test import — do not pip install it."
            )
    return {
        "command": command or str(post.get("test_command") or "").strip() or None,
        "cwd": cwd,
        "interpreter": interpreter,
        "exit_code": exit_code if isinstance(exit_code, int) else None,
        "snippet": error or None,
        "classification": kind,
        "classification_label": _CLASSIFY_LABELS.get(kind) if kind else None,
        "missing_module": module,
        "installable": installable,
        "install_command": install_command,
        "local_module_paths": local_paths,
        "layout": repo_layout_snapshot(repo_path or cwd, limit=24),
        "import_sites": module_imported_in_repo(repo_path or cwd, module) if module else [],
        "diagnosis": diagnosis,
    }


def pytest_collection_path_hint(text: str, repo_path: str | None) -> str | None:
    """Detect pytest nodeids like `TankSacrifice/tests` that imply a wrong rootdir."""
    if not text or not repo_path:
        return None
    repo_name = Path(repo_path).name
    if not repo_name:
        return None
    pattern = re.compile(
        rf"\b{re.escape(repo_name)}[\\/](tests|test)(?:[\\/]\S*)?",
        re.I,
    )
    match = pattern.search(text)
    if not match:
        return None
    return (
        f"Pytest reported collection path '{match.group(0)}', which usually means "
        f"its rootdir is the parent of '{repo_name}'. Tank's workspace cwd should be "
        f"the repo root (where app.py / tests/ live). Prefer `pytest -q` or "
        f"`python -m pytest -q` without a '{repo_name}/…' path prefix, and keep "
        f"`import app` resolving from the repo root. Do not pip install '{repo_name}' "
        f"or 'app'."
    )


def fixer_failure_context(run, *, repo_path: str | None = None, max_chars: int = 3500) -> str:
    """Compact, high-signal failure context for the fixer task/prior context."""
    view = verification_view(run, repo_path=repo_path) or {}
    lines = [
        "Verification failure context for the fixer:",
        f"- run_id: {_field(run, 'id')}",
        f"- status: {_field(run, 'status')}",
        f"- classification: {view.get('classification_label') or view.get('classification') or 'unknown'}",
    ]
    if view.get("command"):
        lines.append(f"- command: {view['command']}")
    if view.get("cwd"):
        lines.append(f"- working_directory: {view['cwd']}")
    if view.get("interpreter"):
        lines.append(f"- interpreter: {view['interpreter']}")
    if view.get("exit_code") is not None:
        lines.append(f"- exit_code: {view['exit_code']}")
    if view.get("missing_module"):
        lines.append(f"- missing_module: {view['missing_module']}")
        if view.get("installable"):
            lines.append(
                "- note: this looks like an external package; prefer an install offer "
                "over rewriting imports unless the import itself is wrong."
            )
        else:
            lines.append(
                "- note: this looks like a local project/import/execution-context issue. "
                "Do NOT treat it as a pip package to install. Inspect tests, app layout, "
                "and working directory. Make the smallest correct change."
            )
    if view.get("diagnosis"):
        lines.append(f"- diagnosis: {view['diagnosis']}")
    if view.get("local_module_paths"):
        lines.append("- local paths for that module: " + ", ".join(view["local_module_paths"]))
    if view.get("import_sites"):
        lines.append("- imported from: " + ", ".join(view["import_sites"][:6]))
    if view.get("layout"):
        lines.append("- repo layout (top level): " + ", ".join(view["layout"]))
    error = str(_field(run, "error") or "").strip()
    if error:
        lines.append("- error:\n" + error[:1200])
    text = "\n".join(lines)
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text
