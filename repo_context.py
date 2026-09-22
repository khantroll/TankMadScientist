"""Select and read repository files for agent/advisory context."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import config

PROJECT_MARKER_FILES = {
    ".git", "pyproject.toml", "requirements.txt", "package.json",
    "Cargo.toml", "go.mod", "composer.json", "pom.xml", "build.gradle",
    "pytest.ini", "tox.ini", "noxfile.py", "Makefile",
}
PROJECT_MARKER_SUFFIXES = {".sln", ".csproj"}
MAX_ROOT_SEARCH_DEPTH = 4
MAX_ROOT_SEARCH_DIRS = 250

SKIP_DIRS = {
    ".git", ".vs", ".idea", ".vscode", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", "bin", "obj", "target", "coverage", "data",
    ".cache", "cache", "tmp", "temp",
}
SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".jpg", ".jpeg", ".png",
    ".gif", ".webp", ".mp4", ".mov", ".avi", ".zip", ".7z", ".rar", ".tar",
    ".gz", ".iso", ".msi", ".whl", ".suo", ".sqlite", ".db", ".pkl",
}
TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss",
    ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".sh", ".ps1",
    ".sql", ".rs", ".go", ".java", ".kt", ".rb", ".php", ".vue", ".svelte",
    ".cs", ".fs", ".sln", ".csproj", ".fsproj",
}
SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss",
    ".sh", ".ps1", ".sql", ".rs", ".go", ".java", ".kt", ".rb", ".php",
    ".vue", ".svelte", ".cs", ".fs",
}
CONFIG_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".sln", ".csproj", ".fsproj"}
SENSITIVE_NAME_RE = re.compile(
    r"(^|[/\\._-])(cookie|cookies|secret|secrets|credential|credentials|token|tokens|apikey|api_key|auth|session|env)([/\\._-]|$)",
    re.I,
)
JUNK_NAME_RE = re.compile(
    r"(^|[/\\])(.vs|dist|build|cache|tmp|temp|logs?|diagnostics?)([/\\]|$)|"
    r"(\.log$|\.diag$|\.slnx$|installer|install-cache|metadata|cache|diagnostic)",
    re.I,
)
README_LINK_RE = re.compile(r"[\w./\\-]+\.(?:py|js|ts|tsx|jsx|ps1|sh|cs|go|rs|rb|php|java|kt|md)", re.I)
PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))", re.M)
MAKE_TEST_RE = re.compile(r"^(?:\.PHONY:\s*)?test\s*:", re.M)

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


def _path_tokens(rel: str) -> set[str]:
    return _tokenize(Path(rel).stem.replace("_", " ").replace("-", " "))


def _workspace_root(repo_path: str) -> Path | None:
    try:
        root = Path(repo_path).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        root = root.resolve()
    except (OSError, RuntimeError):
        return None
    return root


def _is_project_marker(path: Path) -> bool:
    name = path.name
    return name in PROJECT_MARKER_FILES or path.suffix.lower() in PROJECT_MARKER_SUFFIXES


def _markers_in_dir(path: Path) -> list[str]:
    markers = []
    try:
        for child in path.iterdir():
            if _is_project_marker(child):
                markers.append(child.name)
    except OSError:
        return []
    return sorted(markers)


def _marker_score(markers: list[str]) -> int:
    score = 0
    for marker in markers:
        if marker == ".git":
            score += 6
        elif marker in {"pyproject.toml", "package.json", "Cargo.toml", "go.mod", "composer.json", "pom.xml", "build.gradle"}:
            score += 4
        elif marker.endswith((".sln", ".csproj")):
            score += 4
        else:
            score += 2
    return score


def _is_drive_root(path: Path) -> bool:
    return path.parent == path or str(path) == path.anchor


def discover_repo_root(repo_path: str) -> dict:
    selected = _workspace_root(repo_path)
    if selected is None:
        return {
            "repo_root": None,
            "selected_path": repo_path,
            "confidence": "low",
            "markers_found": [],
            "candidates": [],
            "error": "Selected path is invalid or cannot be resolved.",
        }
    if selected.is_file():
        selected = selected.parent
    if not selected.is_dir():
        return {
            "repo_root": None,
            "selected_path": str(selected),
            "confidence": "low",
            "markers_found": [],
            "candidates": [],
            "error": "Selected path is not a directory.",
        }

    candidates = []
    current = selected
    distance = 0
    while True:
        markers = _markers_in_dir(current)
        if markers:
            candidates.append({
                "path": str(current),
                "markers": markers,
                "distance": distance,
                "marker_score": _marker_score(markers),
                "score": _marker_score(markers) - distance,
                "direction": "up",
            })
        if current.parent == current:
            break
        current = current.parent
        distance += 1

    # Do not crawl an entire drive/root folder when the user selects too broad a path.
    if not candidates and not _is_drive_root(selected):
        scanned = 0
        queue = [(selected, 0)]
        while queue and scanned < MAX_ROOT_SEARCH_DIRS:
            path, depth = queue.pop(0)
            scanned += 1
            markers = _markers_in_dir(path)
            if markers:
                candidates.append({
                    "path": str(path),
                    "markers": markers,
                    "distance": depth,
                    "marker_score": _marker_score(markers),
                    "score": _marker_score(markers) - depth,
                    "direction": "down",
                })
            if depth >= MAX_ROOT_SEARCH_DEPTH:
                continue
            try:
                children = sorted(
                    [child for child in path.iterdir() if child.is_dir() and child.name not in SKIP_DIRS],
                    key=lambda item: item.name.lower(),
                )
            except OSError:
                children = []
            queue.extend((child, depth + 1) for child in children)

    if not candidates:
        return {
            "repo_root": None,
            "selected_path": str(selected),
            "confidence": "low",
            "markers_found": [],
            "candidates": [],
            "error": "No project root marker found. Select a project folder containing .git, package/project metadata, or a build file.",
        }

    candidates.sort(key=lambda item: (item["distance"], -item["marker_score"], item["path"].lower()))
    best = candidates[0]
    confidence = "high" if best["marker_score"] >= 4 else "medium"
    return {
        "repo_root": best["path"],
        "selected_path": str(selected),
        "confidence": confidence,
        "markers_found": best["markers"],
        "candidates": candidates[:12],
        "error": None,
    }


def _repo_root_or_selected(repo_path: str) -> Path | None:
    discovery = discover_repo_root(repo_path)
    root = discovery.get("repo_root")
    if root:
        return Path(root)
    return None


def _safe_repo_file(root: Path, file_path: str | os.PathLike) -> Path | None:
    try:
        candidate = Path(file_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        candidate.relative_to(root)
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None


def _is_binary_sample(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    control_bytes = set(range(0, 9)) | set(range(14, 32))
    return any(byte in control_bytes for byte in data)


def _is_sensitive_path(rel: str) -> bool:
    return bool(SENSITIVE_NAME_RE.search(rel))


def _is_junk_path(rel: str) -> bool:
    return bool(JUNK_NAME_RE.search(rel))


def _is_source_path(rel: str) -> bool:
    return Path(rel).suffix.lower() in SOURCE_EXTENSIONS


def _is_text_candidate(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in TEXT_EXTENSIONS or suffix == ""


def _is_entrypoint(rel: str) -> bool:
    name = Path(rel).name.lower()
    stem = Path(rel).stem.lower()
    return (
        name in {"main.py", "app.py", "cli.py", "server.py", "index.js", "index.ts", "program.cs"}
        or stem in {"main", "app", "cli", "server", "runner", "archiver", "scraper"}
        or stem.endswith(("_archiver", "_scraper", "_cli", "_main"))
    )


def _read_small_text(path: Path, limit: int = 120000) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _readme_references(root: Path, all_files: list[str]) -> set[str]:
    by_name = {Path(rel).name.lower(): rel for rel in all_files}
    refs = set()
    for rel in all_files:
        name = Path(rel).name.lower()
        if name not in {"readme.md", "readme.txt"} and not name.startswith("readme."):
            continue
        path = _safe_repo_file(root, rel)
        if path is None:
            continue
        text = _read_small_text(path)
        for match in README_LINK_RE.findall(text):
            normalized = match.replace("\\", "/").lstrip("./").lower()
            if normalized in {item.lower() for item in all_files}:
                refs.add(next(item for item in all_files if item.lower() == normalized))
            elif Path(normalized).name in by_name:
                refs.add(by_name[Path(normalized).name])
    return refs


def _import_references(root: Path, selected: list[str], all_files: list[str]) -> set[str]:
    py_by_stem = {Path(rel).stem: rel for rel in all_files if Path(rel).suffix.lower() == ".py"}
    refs = set()
    for rel in selected:
        path = _safe_repo_file(root, rel)
        if path is None or Path(rel).suffix.lower() != ".py":
            continue
        text = _read_small_text(path)
        for module_a, module_b in PY_IMPORT_RE.findall(text):
            module = (module_a or module_b).split(".")[0]
            if module in py_by_stem:
                refs.add(py_by_stem[module])
    return refs


def _mtime_score(root: Path, rel: str) -> float:
    path = _safe_repo_file(root, rel)
    if path is None:
        return 0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def _truncated_text(path: Path, limit: int, original_bytes: int) -> str:
    half = max(1024, limit // 2)
    with open(path, "rb") as f:
        head = f.read(half)
        if original_bytes > half:
            f.seek(max(0, original_bytes - half))
            tail = f.read(half)
        else:
            tail = b""
    head_text = head.decode("utf-8", errors="replace")
    tail_text = tail.decode("utf-8", errors="replace")
    marker = (
        "\n\n... [Tank truncated middle of oversized file; "
        f"original_bytes={original_bytes} limit={limit}] ...\n\n"
    )
    return (
        f"[Tank context note: oversized file included as truncated excerpt; "
        f"original_bytes={original_bytes} limit={limit}]\n"
        f"{head_text}{marker}{tail_text}"
    )


def _estimate_tokens(files: dict[str, str]) -> int:
    return max(0, sum(len(text) for text in files.values()) // 4)


def list_repo_files(repo_path: str) -> list[str]:
    root = _repo_root_or_selected(repo_path)
    if root is None or not root.is_dir():
        return []
    files = []
    try:
        walker = os.walk(root)
        for dirpath, dirnames, filenames in walker:
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                path = _safe_repo_file(root, Path(dirpath) / name)
                if path is None:
                    continue
                rel = path.relative_to(root).as_posix()
                files.append(rel)
    except OSError:
        return sorted(files)
    return sorted(files)


def select_repo_files(repo_path: str, task: str, cfg: dict) -> list[str]:
    """Pick task-relevant text files, with extra coverage for audit/review tasks."""
    max_files = int(cfg.get("max_context_files", 12))
    if is_analysis_task(task):
        max_files = max(max_files, int(cfg.get("max_context_files_analysis", 24)))

    root = _repo_root_or_selected(repo_path)
    all_files = list_repo_files(repo_path)
    if not all_files:
        return []

    task_tokens = _tokenize(task)
    analysis = is_analysis_task(task)
    language_counts = _count_languages(all_files)
    primary_language = (
        max(language_counts, key=language_counts.get) if language_counts else None
    )
    lang_extensions = _LANG_EXTENSIONS.get(primary_language or "", set())
    readme_refs = _readme_references(root, all_files) if root is not None else set()
    mtimes = {rel: _mtime_score(root, rel) for rel in all_files} if root is not None else {}
    newest = max(mtimes.values()) if mtimes else 0

    scored = []
    for rel in all_files:
        score = 0
        lower = rel.lower()
        suffix = Path(rel).suffix.lower()
        if _is_sensitive_path(rel):
            score -= 100
        if suffix in SKIP_EXTENSIONS or not _is_text_candidate(Path(rel)):
            score -= 100
        if _is_junk_path(rel):
            score -= 20
        if _is_source_path(rel):
            score += 8
        elif suffix in CONFIG_EXTENSIONS:
            score += 3
        for token in task_tokens:
            if token in lower:
                score += 8
        if task_tokens and task_tokens.intersection(_path_tokens(rel)):
            score += 12
        if _is_entrypoint(rel):
            score += 12
        if primary_language and Path(rel).suffix.lower() in lang_extensions:
            score += 5
        if rel in readme_refs:
            score += 8
        if newest and mtimes.get(rel, 0) > 0:
            age_ratio = mtimes[rel] / newest
            if age_ratio > 0.98:
                score += 3
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
    import_refs = _import_references(root, selected, all_files) if root is not None else set()
    for rel in sorted(import_refs):
        if rel not in selected:
            selected.append(rel)
        if len(selected) >= max_files:
            break

    if analysis and len(selected) < max_files:
        for score, rel in scored:
            if rel not in selected:
                selected.append(rel)
            if len(selected) >= max_files:
                break

    if selected:
        return selected
    return [
        rel for rel in all_files
        if not _is_sensitive_path(rel)
        and Path(rel).suffix.lower() not in SKIP_EXTENSIONS
        and _is_text_candidate(Path(rel))
        and not _is_junk_path(rel)
    ][:max_files]


def repo_file_count(repo_path: str) -> int:
    return len(list_repo_files(repo_path))


def collect_repo_files(repo_path: str, rel_paths: list[str], cfg: dict) -> dict:
    max_bytes = int(cfg.get("max_file_bytes", 32000))
    root = _repo_root_or_selected(repo_path)
    contents = {}
    skipped = []
    truncated = []
    full = []
    discovered = int(cfg.get("_discovered_count") or len(rel_paths or []))
    selected_count = len(rel_paths or [])
    if root is None or not root.is_dir():
        return {
            "files": contents,
            "skipped": [{
                "path": str(repo_path),
                "reason": "project root could not be discovered or is not readable",
            }],
            "root": str(root or repo_path),
            "discovered": discovered,
            "selected": selected_count,
            "read": 0,
            "included_full_count": 0,
            "included_truncated_count": 0,
            "skipped_count": 1,
            "top_included": [],
            "context_estimate_tokens": 0,
        }

    for rel in rel_paths:
        path = _safe_repo_file(root, rel)
        if path is None:
            skipped.append({"path": str(rel), "reason": "path is outside workspace or invalid"})
            continue
        display_path = path.relative_to(root).as_posix()
        if path.suffix.lower() in SKIP_EXTENSIONS:
            skipped.append({"path": display_path, "reason": "binary or unsupported file extension"})
            continue
        if _is_sensitive_path(display_path):
            size = path.stat().st_size if path.exists() else 0
            skipped.append({"path": display_path, "reason": f"sensitive file skipped; size={size}"})
            continue
        if _is_junk_path(display_path) and not _is_source_path(display_path):
            skipped.append({"path": display_path, "reason": "diagnostic/cache/output file skipped"})
            continue
        try:
            if not path.is_file():
                skipped.append({"path": display_path, "reason": "not a file"})
                continue
            size = path.stat().st_size
            sample = path.read_bytes()[: min(size, 4096)]
            if _is_binary_sample(sample):
                skipped.append({"path": display_path, "reason": "binary content detected"})
                continue
            if size > max_bytes:
                if _is_source_path(display_path) and not _is_junk_path(display_path):
                    data = _truncated_text(path, max_bytes, size)
                    truncated.append({
                        "path": display_path,
                        "original_bytes": size,
                        "limit": max_bytes,
                    })
                else:
                    skipped.append({"path": display_path, "reason": f"file exceeds max_file_bytes ({size} > {max_bytes})"})
                    continue
            else:
                data = path.read_text(encoding="utf-8", errors="replace")
                full.append(display_path)
        except (OSError, UnicodeError) as exc:
            skipped.append({"path": display_path, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        contents[display_path] = data
    return {
        "files": contents,
        "skipped": skipped,
        "truncated": truncated,
        "root": str(root),
        "discovered": discovered,
        "selected": selected_count,
        "read": len(contents),
        "included_full_count": len(full),
        "included_truncated_count": len(truncated),
        "skipped_count": len(skipped),
        "top_included": list(contents.keys())[:8],
        "context_estimate_tokens": _estimate_tokens(contents),
    }


def read_repo_files(repo_path: str, rel_paths: list[str], cfg: dict) -> dict[str, str]:
    return collect_repo_files(repo_path, rel_paths, cfg)["files"]


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
        "tox.ini", "noxfile.py", "package.json", "composer.json",
        "Pester.psd1", "Cargo.toml", "go.mod", "Makefile",
        "pom.xml", "build.gradle",
    ):
        if (repo_path / name).is_file():
            markers.append(name)
    try:
        for child in repo_path.iterdir():
            if child.suffix.lower() in {".sln", ".csproj"}:
                markers.append(child.name)
    except OSError:
        pass
    return markers


def _npm_test_command(repo_path: Path) -> str | None:
    package_json = _safe_repo_file(repo_path, "package.json")
    if package_json is None:
        return None
    if not package_json.is_file():
        return None
    try:
        data = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    scripts = data.get("scripts") or {}
    if "test" in scripts:
        return "npm test"
    return None


def _npm_build_command(repo_path: Path) -> str | None:
    package_json = _safe_repo_file(repo_path, "package.json")
    if package_json is None or not package_json.is_file():
        return None
    try:
        data = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    scripts = data.get("scripts") or {}
    if "build" in scripts:
        return "npm run build"
    return None


def _pyproject_has_pytest(repo_path: Path) -> bool:
    path = _safe_repo_file(repo_path, "pyproject.toml")
    if path is None or not path.is_file():
        return False
    text = _read_small_text(path)
    return "pytest" in text.lower() or "[tool.pytest" in text.lower()


def _requirements_has_pytest(repo_path: Path) -> bool:
    path = _safe_repo_file(repo_path, "requirements.txt")
    if path is None or not path.is_file():
        return False
    text = _read_small_text(path)
    return bool(re.search(r"(^|\n)\s*pytest(?:[<>=~!\s]|\n|$)", text, re.I))


def _makefile_has_test(repo_path: Path) -> bool:
    path = _safe_repo_file(repo_path, "Makefile")
    return bool(path and path.is_file() and MAKE_TEST_RE.search(_read_small_text(path)))


def _detect_test_command(root: Path, markers: list[str], language_counts: dict[str, int], has_pester: bool) -> tuple[str | None, str, str | None]:
    primary_language = max(language_counts, key=language_counts.get) if language_counts else "unknown"
    py_count = language_counts.get("python", 0)
    ps_count = language_counts.get("powershell", 0)
    test_command = None
    test_reason = "No automated test command detected."

    if "package.json" in markers:
        test_command = _npm_test_command(root)
        if test_command:
            test_reason = "package.json scripts.test detected."
    if test_command is None and (
        "pytest.ini" in markers
        or "tox.ini" in markers
        or "noxfile.py" in markers
        or _pyproject_has_pytest(root)
        or _requirements_has_pytest(root)
    ):
        test_command = "pytest -q"
        test_reason = "pytest configuration or dependency detected."
    if test_command is None and (has_pester or (ps_count > py_count and ps_count > 0)):
        test_command = suggested_pester_command()
        if test_command:
            test_reason = "PowerShell/Pester tests detected."
    if test_command is None and primary_language == "go" and "go.mod" in markers:
        test_command = "go test ./..."
        test_reason = "go.mod detected."
    if test_command is None and primary_language == "rust" and "Cargo.toml" in markers:
        test_command = "cargo test"
        test_reason = "Cargo.toml detected."
    if test_command is None and "composer.json" in markers:
        test_command = "composer test"
        test_reason = "composer.json detected; using composer test."
    if test_command is None and _makefile_has_test(root):
        test_command = "make test"
        test_reason = "Makefile test target detected."
    if test_command is None and any(marker.endswith((".sln", ".csproj")) for marker in markers):
        test_command = "dotnet test"
        test_reason = ".NET solution/project marker detected."

    static_command = None
    if test_command is None:
        if py_count > 0:
            static_command = "python -m compileall -q ."
        elif language_counts.get("javascript", 0) or language_counts.get("typescript", 0):
            static_command = _npm_build_command(root)
    return test_command, test_reason, static_command


def _entry_points(all_files: list[str]) -> list[str]:
    return [rel for rel in all_files if _is_entrypoint(rel)][:12]


def _dependency_files(all_files: list[str]) -> list[str]:
    names = {
        "requirements.txt", "pyproject.toml", "package.json", "Cargo.toml",
        "go.mod", "composer.json", "pom.xml", "build.gradle", "tox.ini",
        "noxfile.py", "Makefile",
    }
    return [
        rel for rel in all_files
        if Path(rel).name in names or Path(rel).suffix.lower() in {".sln", ".csproj", ".fsproj"}
    ][:20]


def repository_scout(repo_path: str, cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    discovery = discover_repo_root(repo_path)
    root_text = discovery.get("repo_root")
    if not root_text:
        return {
            "repo_root": None,
            "confidence": discovery["confidence"],
            "markers_found": discovery["markers_found"],
            "languages": {},
            "entry_points": [],
            "dependency_files": [],
            "test_command": None,
            "test_command_reason": discovery.get("error") or "No project root marker found.",
            "static_validation_command": None,
            "files_read": 0,
            "files_skipped": {"ignored": 0, "binary": 0, "too_large": 0, "outside_root": 0, "sensitive": 0},
            "discovered_files": 0,
            "top_included_files": [],
            "candidates": discovery["candidates"],
            "error": discovery.get("error"),
        }

    root = Path(root_text)
    all_files = list_repo_files(str(root))
    markers = _repo_markers(root)
    languages = _count_languages(all_files)
    has_pester = _has_pester_tests(all_files)
    test_command, test_reason, static_command = _detect_test_command(root, markers, languages, has_pester)
    sample_limit = int(cfg.get("manifest_sample_files", 200))
    collection = collect_repo_files(
        str(root),
        all_files[:sample_limit],
        {"max_file_bytes": int(cfg.get("max_file_bytes", 32000)), "_discovered_count": len(all_files)},
    )
    skipped_by_reason = {"ignored": 0, "binary": 0, "too_large": 0, "outside_root": 0, "sensitive": 0}
    for skipped in collection["skipped"]:
        reason = skipped["reason"]
        if "sensitive" in reason:
            skipped_by_reason["sensitive"] += 1
        elif "binary" in reason or "unsupported" in reason:
            skipped_by_reason["binary"] += 1
        elif "max_file_bytes" in reason:
            skipped_by_reason["too_large"] += 1
        elif "outside workspace" in reason:
            skipped_by_reason["outside_root"] += 1
        else:
            skipped_by_reason["ignored"] += 1
    return {
        "repo_root": str(root),
        "confidence": discovery["confidence"],
        "markers_found": markers,
        "languages": languages,
        "entry_points": _entry_points(all_files),
        "dependency_files": _dependency_files(all_files),
        "test_command": test_command,
        "test_command_reason": test_reason,
        "static_validation_command": static_command,
        "files_read": collection["read"],
        "files_skipped": skipped_by_reason,
        "context_estimate_tokens": collection["context_estimate_tokens"],
        "discovered_files": len(all_files),
        "top_included_files": collection["top_included"],
        "candidates": discovery["candidates"],
        "error": None,
    }


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
    discovery = discover_repo_root(repo_path)
    root = Path(discovery["repo_root"]) if discovery.get("repo_root") else None
    if root is None or not root.is_dir():
        return {
            "primary_language": "unknown",
            "language_counts": {},
            "markers": [],
            "test_command": None,
            "test_command_reason": discovery.get("error") or "Repository path not found or not a directory.",
            "static_validation_command": None,
            "repository_scout": repository_scout(repo_path),
            "summary": discovery.get("error") or "Repository path not found or not a directory.",
        }

    all_files = list_repo_files(str(root))
    language_counts = _count_languages(all_files)
    markers = _repo_markers(root)
    has_pester = _has_pester_tests(all_files)

    primary_language = "unknown"
    if language_counts:
        primary_language = max(language_counts, key=language_counts.get)

    test_command, test_reason, static_command = _detect_test_command(
        root, markers, language_counts, has_pester
    )

    lang_bits = ", ".join(f"{lang} ({count})" for lang, count in sorted(language_counts.items()))
    marker_bits = ", ".join(markers) if markers else "none"
    summary = (
        f"Repository root: {root} ({discovery['confidence']} confidence). "
        f"Primary language: {primary_language}. "
        f"Source files: {lang_bits or 'none detected'}. "
        f"Project markers: {marker_bits}."
    )
    if has_pester:
        summary += " Pester test files detected."
    if test_command:
        summary += f" Suggested test command: {test_command}."
    else:
        summary += " No automated test command detected."
        if static_command:
            summary += f" Static validation available: {static_command}."

    return {
        "repo_root": str(root),
        "confidence": discovery["confidence"],
        "primary_language": primary_language,
        "language_counts": language_counts,
        "markers": markers,
        "has_pester_tests": has_pester,
        "test_command": test_command,
        "test_command_reason": test_reason,
        "static_validation_command": static_command,
        "repository_scout": repository_scout(str(root)),
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
        if detected:
            if _looks_wrong_test_command(model_cmd, profile):
                return detected
            return model_cmd
        return None

    return detected


def format_project_profile_section(
    profile: dict,
    workspace_test_command: str | None = None,
) -> str:
    lines = ["Project profile (detected from repository — trust this over assumptions):"]
    lines.append(profile.get("summary", ""))
    scout = profile.get("repository_scout")
    if scout:
        lines.append("Repository Scout manifest:")
        lines.append(json.dumps(scout, indent=2, sort_keys=True))
    if workspace_test_command:
        lines.append(f"Workspace test_command override: {workspace_test_command}")
    elif profile.get("test_command"):
        lines.append(
            "Use the suggested test command above when running tests unless the repo "
            "files clearly show a different command."
        )
    else:
        lines.append("No automated test command detected.")
        if profile.get("static_validation_command"):
            lines.append(f"Use static validation instead: {profile['static_validation_command']}")
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
