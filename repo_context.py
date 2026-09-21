"""Select and read repository files for agent/advisory context."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import config

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build", "data",
}
SKIP_EXTENSIONS = {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".jpg", ".png", ".gif", ".zip"}
TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss",
    ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".sh", ".ps1",
    ".sql", ".rs", ".go", ".java", ".kt", ".rb", ".php", ".vue", ".svelte",
}

ANALYSIS_WORDS = re.compile(
    r"\b(audit|review|analyz|assess|evaluate|inspect|state|status|finding|report)\b",
    re.I,
)


def is_analysis_task(task: str) -> bool:
    return bool(ANALYSIS_WORDS.search(task))


def analysis_system_addendum() -> str:
    return """
This is an analysis/review task. You MUST deliver substantive findings from the repository
files provided — not a generic template or a checklist of what a human should look for.

Your response must:
- Cite specific files (and functions/lines when possible)
- List real issues, risks, or observations grounded in the file contents
- End with prioritized, actionable recommendations
- NEVER use bracket placeholders like [fill in] or empty project templates
- If prior run output is included below, build on those findings directly
- If the user references material not in the files (e.g. "previous audit"), say what is
  missing and analyze what you do have
"""


def _tokenize(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text)}


def list_repo_files(repo_path: str) -> list[str]:
    root = Path(repo_path)
    if not root.is_dir():
        return []
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if path.suffix.lower() in SKIP_EXTENSIONS:
                continue
            if path.suffix.lower() in TEXT_EXTENSIONS or path.suffix == "":
                files.append(rel)
    return sorted(files)


def select_repo_files(repo_path: str, task: str, cfg: dict) -> list[str]:
    """Pick task-relevant text files, with extra coverage for audit/review tasks."""
    max_files = int(cfg.get("max_context_files", 12))
    if is_analysis_task(task):
        max_files = max(max_files, int(cfg.get("max_context_files_analysis", 24)))

    all_files = list_repo_files(repo_path)
    if not all_files:
        return []

    task_tokens = _tokenize(task)
    analysis = is_analysis_task(task)
    if analysis and len(all_files) <= max_files:
        return all_files

    language_counts = _count_languages(all_files)
    primary_language = (
        max(language_counts, key=language_counts.get) if language_counts else None
    )
    lang_extensions = _LANG_EXTENSIONS.get(primary_language or "", set())

    scored = []
    for rel in all_files:
        score = 0
        lower = rel.lower()
        for token in task_tokens:
            if token in lower:
                score += 3
        if lower.endswith(("main.py", "app.py", "index.js", "index.ts")):
            score += 1
        if primary_language and Path(rel).suffix.lower() in lang_extensions:
            score += 2
        if analysis:
            if any(k in lower for k in ("audit", "report", "review", "finding", "state")):
                score += 5
            if lower == "readme.md" or lower.endswith("/readme.md"):
                score += 4
            if "/scripts/" in lower or lower.startswith("scripts/"):
                score += 2
        scored.append((score, rel))

    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = [rel for score, rel in scored if score > 0][:max_files]

    if analysis and len(selected) < max_files:
        for score, rel in scored:
            if rel not in selected:
                selected.append(rel)
            if len(selected) >= max_files:
                break

    if selected:
        return selected
    return all_files[:max_files]


def read_repo_files(repo_path: str, rel_paths: list[str], cfg: dict) -> dict[str, str]:
    max_bytes = int(cfg.get("max_file_bytes", 32000))
    root = Path(repo_path)
    contents = {}
    for rel in rel_paths:
        path = (root / rel).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            continue
        if not path.is_file():
            continue
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(data.encode("utf-8")) > max_bytes:
            data = data[:max_bytes] + "\n... [truncated by Tank]\n"
        contents[rel] = data
    return contents


_LANG_EXTENSIONS = {
    "powershell": {".ps1", ".psm1", ".psd1"},
    "python": {".py"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx"},
    "go": {".go"},
    "rust": {".rs"},
    "ruby": {".rb"},
    "php": {".php"},
    "java": {".java", ".kt"},
}


def _count_languages(all_files: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rel in all_files:
        ext = Path(rel).suffix.lower()
        for lang, extensions in _LANG_EXTENSIONS.items():
            if ext in extensions:
                counts[lang] = counts.get(lang, 0) + 1
    return counts


def _repo_markers(repo_path: Path) -> list[str]:
    markers = []
    for name in (
        "pyproject.toml", "pytest.ini", "requirements.txt", "setup.py",
        "package.json", "Pester.psd1", "Cargo.toml", "go.mod", "Makefile",
    ):
        if (repo_path / name).is_file():
            markers.append(name)
    return markers


def _npm_test_command(repo_path: Path) -> str | None:
    package_json = repo_path / "package.json"
    if not package_json.is_file():
        return None
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scripts = data.get("scripts") or {}
    if "test" in scripts:
        return "npm test"
    return None


def _has_pester_tests(all_files: list[str]) -> bool:
    return any(
        rel.lower().endswith(".tests.ps1") or ".tests.ps1" in rel.lower()
        for rel in all_files
    )


def resolve_powershell_executable() -> str | None:
    """Find pwsh or Windows PowerShell for running Pester on this machine."""
    if sys.platform == "win32":
        for name in ("pwsh", "powershell"):
            found = shutil.which(name)
            if found:
                return found
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        for subpath in (r"PowerShell\7\pwsh.exe", r"PowerShell\7-preview\pwsh.exe"):
            candidate = os.path.join(program_files, subpath)
            if os.path.isfile(candidate):
                return candidate
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        winps = os.path.join(
            system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
        )
        if os.path.isfile(winps):
            return winps
        return None

    return shutil.which("pwsh") or shutil.which("powershell")


def suggested_pester_command() -> str | None:
    """Build a Pester command using a PowerShell executable that exists locally."""
    exe = resolve_powershell_executable()
    if not exe:
        return None
    if exe.lower().endswith("powershell.exe"):
        return "powershell -NoProfile -ExecutionPolicy Bypass -Command Invoke-Pester"
    return "pwsh -NoProfile -Command Invoke-Pester"


_POWERSHELL_CMD_RE = re.compile(
    r"^(pwsh|pwsh\.exe|powershell|powershell\.exe)\s+(.*)$",
    re.I | re.S,
)


def prepare_test_execution(cmd: str) -> tuple[list[str] | str, bool]:
    """
    Return subprocess arguments for a test command.

    On Windows, resolve pwsh/powershell to a full executable path and avoid
    shell=True so cmd.exe does not need pwsh on PATH.
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return cmd, False

    if sys.platform != "win32":
        return cmd, True

    match = _POWERSHELL_CMD_RE.match(cmd)
    if not match:
        return cmd, True

    exe = resolve_powershell_executable()
    if not exe:
        return cmd, True

    tail = match.group(2).strip().split()
    args = [exe]
    lowered = [part.lower() for part in tail]
    if exe.lower().endswith("powershell.exe") and "-executionpolicy" not in lowered:
        args.extend(["-ExecutionPolicy", "Bypass"])
    args.extend(tail)
    return args, False


def format_test_command(cmd: str) -> str:
    """Human-readable command string after platform normalization."""
    prepared, use_shell = prepare_test_execution(cmd)
    if not use_shell and isinstance(prepared, list):
        return subprocess.list2cmdline(prepared)
    return cmd


def _normalize_powershell_test_command(cmd: str) -> str:
    """If cmd asks for pwsh but only Windows PowerShell exists, rewrite it."""
    if sys.platform != "win32":
        return cmd
    match = _POWERSHELL_CMD_RE.match((cmd or "").strip())
    if not match:
        return cmd
    exe = resolve_powershell_executable()
    if not exe or not exe.lower().endswith("powershell.exe"):
        return cmd
    if match.group(1).lower().startswith("pwsh"):
        tail = match.group(2).strip()
        return f"powershell -NoProfile -ExecutionPolicy Bypass {tail}"
    return cmd


def detect_project_profile(repo_path: str) -> dict:
    """Infer primary language(s) and a suggested test command from the repo tree."""
    root = Path(repo_path)
    if not root.is_dir():
        return {
            "primary_language": "unknown",
            "language_counts": {},
            "markers": [],
            "test_command": None,
            "summary": "Repository path not found or not a directory.",
        }

    all_files = list_repo_files(repo_path)
    language_counts = _count_languages(all_files)
    markers = _repo_markers(root)
    has_pester = _has_pester_tests(all_files)

    primary_language = "unknown"
    if language_counts:
        primary_language = max(language_counts, key=language_counts.get)

    ps_count = language_counts.get("powershell", 0)
    py_count = language_counts.get("python", 0)

    test_command = None
    if "package.json" in markers:
        test_command = _npm_test_command(root)
    if test_command is None and ("pytest.ini" in markers or py_count > 0):
        test_command = "pytest -q"
    if test_command is None and (has_pester or (ps_count > py_count and ps_count > 0)):
        test_command = suggested_pester_command()
    if test_command is None and primary_language == "go" and "go.mod" in markers:
        test_command = "go test ./..."
    if test_command is None and primary_language == "rust" and "Cargo.toml" in markers:
        test_command = "cargo test"

    lang_bits = ", ".join(f"{lang} ({count})" for lang, count in sorted(language_counts.items()))
    marker_bits = ", ".join(markers) if markers else "none"
    summary = (
        f"Primary language: {primary_language}. "
        f"Source files: {lang_bits or 'none detected'}. "
        f"Project markers: {marker_bits}."
    )
    if has_pester:
        summary += " Pester test files detected."
    if test_command:
        summary += f" Suggested test command: {test_command}."

    return {
        "primary_language": primary_language,
        "language_counts": language_counts,
        "markers": markers,
        "has_pester_tests": has_pester,
        "test_command": test_command,
        "summary": summary,
    }


def _looks_wrong_test_command(cmd: str, profile: dict) -> bool:
    cmd_lower = cmd.lower()
    primary = profile.get("primary_language")
    if primary == "powershell" and ("pytest" in cmd_lower or "python -m pytest" in cmd_lower):
        return True
    if primary == "python" and "invoke-pester" in cmd_lower:
        return True
    return False


def resolve_test_command(
    repo_path: str,
    workspace: dict | None = None,
    post_actions: dict | None = None,
    profile: dict | None = None,
) -> str | None:
    """Pick the test command: workspace override, then repo detection, then model."""
    if workspace and workspace.get("test_command"):
        return workspace["test_command"]

    profile = profile or detect_project_profile(repo_path)
    detected = profile.get("test_command")
    if detected:
        detected = _normalize_powershell_test_command(detected)

    model_cmd = (post_actions or {}).get("test_command")
    if model_cmd:
        model_cmd = _normalize_powershell_test_command(model_cmd)
        if detected and _looks_wrong_test_command(model_cmd, profile):
            return detected
        return model_cmd

    return detected


def format_project_profile_section(
    profile: dict,
    workspace_test_command: str | None = None,
) -> str:
    lines = ["Project profile (detected from repository — trust this over assumptions):"]
    lines.append(profile.get("summary", ""))
    if workspace_test_command:
        lines.append(f"Workspace test_command override: {workspace_test_command}")
    elif profile.get("test_command"):
        lines.append(
            "Use the suggested test command above when running tests unless the repo "
            "files clearly show a different command."
        )
    else:
        lines.append(
            "No test command could be detected automatically — inspect the repository "
            "for scripts, CI config, or README instructions before choosing one."
        )
    return "\n".join(lines)


def build_context_message(
    task: str,
    files: dict[str, str],
    prior_run_context: str | None = None,
    project_profile: dict | None = None,
    workspace_test_command: str | None = None,
) -> str:
    parts = [f"User task:\n{task}\n"]
    if project_profile:
        parts.append(format_project_profile_section(project_profile, workspace_test_command))
        parts.append("")
    if prior_run_context:
        parts.append(
            "Prior run output (continue from this — build on these findings, do not repeat a generic template):\n"
        )
        parts.append(f"--- prior run ---\n{prior_run_context}\n")
    if files:
        parts.append(
            "Repository files (analyze these — base your answer on their actual contents):\n"
        )
        for rel, content in files.items():
            parts.append(f"--- {rel} ---\n{content}\n")
    else:
        parts.append("No repository files were loaded.\n")
    return "\n".join(parts)
