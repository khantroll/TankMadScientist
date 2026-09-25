#!/usr/bin/env python3
"""Bare pytest runs without pytest on PATH, and failures show a short snippet.

Covers command resolution (venv python, python -m pytest, pytest already on
PATH) and the failed-run card text that used to be only
"Verification command exited N".
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="tank-pytest-"))
REPO = TMP / "repo"
REPO.mkdir()
(REPO / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
FAIL_SCRIPT = REPO / "fail_verify.py"
FAIL_SCRIPT.write_text(
    "import os, sys\n"
    "sys.stderr.write('EXAMPLE_API_KEY=' + os.environ['EXAMPLE_API_KEY'] + '\\n')\n"
    "sys.stderr.write(\"'pytest' is not recognized as an internal or external command,\\n\")\n"
    "sys.stderr.write('operable program or batch file.\\n')\n"
    "sys.stderr.write('Bearer supersecrettokenvalue\\n')\n"
    "sys.exit(1)\n",
    encoding="utf-8",
)
WORKSPACES = TMP / "workspaces.yaml"
WORKSPACES.write_text(
    "workspaces:\n"
    "  - slug: jobagent\n"
    "    name: JobAgent\n"
    f"    repo_path: {REPO}\n"
    "    default_provider: local_qwen\n"
    "    roles: []\n",
    encoding="utf-8",
)

os.environ["TANK_DATA_DIR"] = str(TMP)
os.environ["TANK_WORKSPACES_FILE"] = str(WORKSPACES)
os.environ["TANK_PROVIDERS_FILE"] = str(ROOT / "providers.yaml")
os.environ["TANK_MISSION_TEMPLATES_FILE"] = str(ROOT / "mission_templates.yaml")
os.environ["TANK_MAX_PARALLEL_RUNS"] = "0"
os.environ["TANK_MAX_PARALLEL_RUNS_PER_MISSION"] = "0"

sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import app  # noqa: E402
import config  # noqa: E402
import local_agent  # noqa: E402
import mad_scientist  # noqa: E402
import mad_scientist_graph as graph  # noqa: E402
import models  # noqa: E402
import repo_context  # noqa: E402
import session_manager  # noqa: E402

FAILURES: list[str] = []
SECRET = "sk-tank-pytest-card-secret-9f3a"


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok: {label}")
        return
    FAILURES.append(label)
    print(f"FAIL: {label}")


@contextmanager
def hide_pytest():
    real = repo_context.shutil.which

    def which(cmd, *args, **kwargs):
        if cmd == "pytest":
            return None
        return real(cmd, *args, **kwargs)

    with patch.object(repo_context.shutil, "which", which):
        yield


@contextmanager
def show_pytest(path: str = "/usr/bin/pytest"):
    real = repo_context.shutil.which

    def which(cmd, *args, **kwargs):
        if cmd == "pytest":
            return path
        return real(cmd, *args, **kwargs)

    with patch.object(repo_context.shutil, "which", which):
        yield


def write_exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_command_resolution() -> None:
    with tempfile.TemporaryDirectory(prefix="tank_pytest_cmd_") as tmp:
        root = Path(tmp)
        (root / "mod.py").write_text("x = 1\n", encoding="utf-8")

        with hide_pytest():
            rewritten = repo_context.normalize_bare_pytest_command("pytest -q", str(root))
            check("-m pytest -q" in rewritten, "bare pytest -q becomes python -m pytest -q")
            check(
                not rewritten.strip().lower().startswith("pytest"),
                "rewritten command does not invoke bare pytest",
            )
            check(
                repo_context.normalize_bare_pytest_command(rewritten, str(root)) == rewritten,
                "python -m pytest rewrite is idempotent",
            )
            check(
                repo_context.normalize_bare_pytest_command("pytest", str(root)).endswith("-m pytest"),
                "bare pytest with no args becomes python -m pytest",
            )
            quoted = repo_context.normalize_bare_pytest_command('"pytest" -q', str(root))
            check("-m pytest -q" in quoted, 'quoted "pytest" -q is rewritten')
            exe = repo_context.normalize_bare_pytest_command("pytest.EXE -q tests/test_auth.py", str(root))
            check(
                exe.endswith("-m pytest -q tests/test_auth.py"),
                "pytest.exe keeps its arguments",
            )
            dotted = repo_context.normalize_bare_pytest_command("./pytest -q", str(root))
            check("-m pytest -q" in dotted, "./pytest is treated as a bare pytest command")
            shown = repo_context.format_test_command("pytest -q", str(root))
            check("-m pytest -q" in shown, "logged test command shows the rewritten invocation")
            profile = repo_context.detect_project_profile(str(root))
            check(
                "-m pytest -q" in (profile.get("test_command") or ""),
                "a Python repo suggests python -m pytest when pytest is not on PATH",
            )
            resolved = repo_context.resolve_test_command(
                str(root),
                workspace={"test_command": "pytest -q"},
                profile={},
            )
            check(
                resolved is not None and "-m pytest -q" in resolved,
                "workspace test_command pytest -q is rewritten",
            )
            model = repo_context.resolve_test_command(
                str(root),
                post_actions={"test_command": "pytest -q tests/test_auth.py -k jwt"},
                profile={"primary_language": "python", "test_command": "pytest -q"},
            )
            check(
                model is not None and model.endswith("-m pytest -q tests/test_auth.py -k jwt"),
                "model-supplied pytest -q keeps its arguments",
            )

        with show_pytest():
            kept = repo_context.normalize_bare_pytest_command("pytest -q", str(root))
            check(kept == "pytest -q", "pytest -q stays unchanged when pytest is on PATH")
            profile = repo_context.detect_project_profile(str(root))
            check(
                profile.get("test_command") == "pytest -q",
                "a Python repo keeps suggesting pytest -q when pytest is on PATH",
            )

        check(
            repo_context.normalize_bare_pytest_command("python -m pytest -q", str(root))
            == "python -m pytest -q",
            "python -m pytest -q is left unchanged",
        )
        check(
            repo_context.normalize_bare_pytest_command("py -m pytest -q", str(root))
            == "py -m pytest -q",
            "py -m pytest -q is left unchanged",
        )
        check(
            repo_context.normalize_bare_pytest_command("/usr/bin/pytest -q", str(root))
            == "/usr/bin/pytest -q",
            "a path to pytest is left unchanged",
        )
        check(
            repo_context.normalize_bare_pytest_command("npm test", str(root)) == "npm test",
            "npm test is left unchanged",
        )
        check(
            repo_context.normalize_bare_pytest_command("go test ./...", str(root)) == "go test ./...",
            "go test is left unchanged",
        )
        pester = repo_context.resolve_test_command(
            str(root),
            post_actions={"test_command": "pytest -q"},
            profile={
                "primary_language": "powershell",
                "test_command": "pwsh -NoProfile -Command Invoke-Pester",
            },
        )
        check(
            pester == "pwsh -NoProfile -Command Invoke-Pester",
            "a PowerShell repo does not get a pytest rewrite",
        )

        venv_python = root / ".venv" / "bin" / "python"
        other_python = root / "venv" / "bin" / "python"
        write_exe(venv_python, "#!/bin/sh\necho VENV_PYTEST_OK\nexit 0\n")
        write_exe(other_python, "#!/bin/sh\necho OTHER_VENV\nexit 0\n")
        found = repo_context.find_venv_python(root)
        check(found == venv_python, ".venv python wins over venv python")
        with show_pytest():
            preferred = repo_context.normalize_bare_pytest_command("pytest -q", str(root))
        check(str(venv_python) in preferred and preferred.endswith("-m pytest -q"), ".venv python is used even when pytest is on PATH")
        check(
            repo_context.normalize_bare_pytest_command("python -m pytest -q", str(root))
            == "python -m pytest -q",
            "an explicit python -m pytest command is not redirected into the venv",
        )

        win_only = root / "win-layout"
        win_python = win_only / ".venv" / "Scripts" / "python.exe"
        win_python.parent.mkdir(parents=True)
        win_python.write_text("", encoding="utf-8")
        check(
            repo_context.find_venv_python(win_only) == win_python,
            "Windows Scripts/python.exe is found when bin/python is absent",
        )
        win_cmd = repo_context.normalize_bare_pytest_command("pytest -q", str(win_only))
        check("python.exe" in win_cmd and "-m pytest -q" in win_cmd, "Windows venv python runs python -m pytest -q")

        spaced = root / "repo with spaces"
        spaced_python = spaced / ".venv" / "bin" / "python"
        write_exe(spaced_python, "#!/bin/sh\necho SPACED_VENV_OK\nexit 0\n")
        spaced_cmd = repo_context.normalize_bare_pytest_command("pytest -q", str(spaced))
        check(shlex.quote(str(spaced_python)) in spaced_cmd, "a venv path with spaces is shell-quoted")
        check(spaced_cmd.endswith("-m pytest -q"), "quoted venv command still passes pytest args")

        with patch.object(repo_context.sys, "platform", "win32"):
            quoted_win = repo_context._quote_exe(r"C:\Program Files\Python\python.exe")
        check(
            quoted_win == subprocess.list2cmdline([r"C:\Program Files\Python\python.exe"]),
            "Windows interpreter paths use cmd quoting",
        )


def test_error_snippet() -> None:
    os.environ["EXAMPLE_API_KEY"] = SECRET
    try:
        raw = (
            "noise line\n" * 30
            + f"EXAMPLE_API_KEY={SECRET}\n"
            + "'pytest' is not recognized as an internal or external command,\n"
            + "operable program or batch file.\n"
            + "Bearer supersecrettokenvalue\n"
        )
        snippet = local_agent.command_output_snippet(raw)
        check(SECRET not in snippet, "snippet redacts the API key value")
        check("supersecrettokenvalue" not in snippet, "snippet redacts a bearer token")
        check("is not recognized as an internal or external command" in snippet, "snippet keeps the command-not-found line")
        check("operable program or batch file" in snippet, "snippet keeps the rest of the Windows shell message")
        check("Bearer [redacted]" in snippet, "bearer token is marked redacted")
        check("noise line" not in snippet, "snippet prefers the shell error over earlier stdout")
        message = local_agent.verification_failure_text(1, raw)
        check(
            message.startswith("Verification command exited 1: "),
            "verification error starts with the exit code and a snippet",
        )
        check(SECRET not in message, "verification error does not contain the API key")
        plain = local_agent.verification_failure_text(1, "")
        check(plain == "Verification command exited 1", "missing output keeps the exit-code sentence")
        tail = "\n".join(f"line {i}" for i in range(40)) + "\nFAILED test_auth.py::test_jwt"
        check(
            "FAILED test_auth.py::test_jwt" in local_agent.command_output_snippet(tail),
            "a pytest failure summary at the end of stdout is kept",
        )
        encoded = json.dumps({"tool_exit_code": local_agent.CommandResult(1, SECRET)})
        check(encoded == '{"tool_exit_code": 1}', "command result stores as an integer exit code")
        check(local_agent.CommandResult(5, "x") == 5, "command result compares equal to its exit code")
    finally:
        os.environ.pop("EXAMPLE_API_KEY", None)


def test_pytest_on_path_still_runs() -> None:
    with tempfile.TemporaryDirectory(prefix="tank_pytest_path_") as tmp:
        root = Path(tmp)
        bin_dir = root / "bin"
        write_exe(bin_dir / "pytest", "#!/bin/sh\necho PYTEST_ON_PATH_OK\nexit 0\n")
        project = root / "project"
        write_exe(project / ".venv" / "bin" / "python", "#!/bin/sh\necho VENV_PYTEST_OK\nexit 0\n")
        (project / "mod.py").write_text("x = 1\n", encoding="utf-8")
        empty = root / "no-venv"
        empty.mkdir()
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
        try:
            kept = repo_context.normalize_bare_pytest_command("pytest -q", str(empty))
            check(kept == "pytest -q", "a real pytest executable on PATH keeps pytest -q")
            log_path = root / "path.log"
            code = local_agent.run_post_actions(
                str(empty),
                {"run_tests": True, "test_command": "pytest -q", "run_git_diff": False},
                str(log_path),
                workspace={"test_command": "pytest -q"},
                profile={},
            )
            text = log_path.read_text(encoding="utf-8")
            check(int(code) == 0, "pytest on PATH exits 0 through the original command")
            check("PYTEST_ON_PATH_OK" in text, "pytest on PATH is the program that runs")
            check(
                "[tank] running tests: pytest -q" in text,
                "the log shows the bare pytest command when it is on PATH",
            )

            venv_log = root / "venv.log"
            code = local_agent.run_post_actions(
                str(project),
                {"run_tests": True, "test_command": "pytest -q", "run_git_diff": False},
                str(venv_log),
                workspace={"test_command": "pytest -q"},
                profile={},
            )
            venv_text = venv_log.read_text(encoding="utf-8")
            check(int(code) == 0, "venv python -m pytest exits 0")
            check("VENV_PYTEST_OK" in venv_text, "the workspace venv python is what runs")
            check("PYTEST_ON_PATH_OK" not in venv_text, "PATH pytest is not used when a venv python exists")
            check("-m pytest -q" in venv_text, "venv invocation is python -m pytest -q")
        finally:
            os.environ["PATH"] = old_path


def test_missing_pytest_uses_module() -> None:
    with tempfile.TemporaryDirectory(prefix="tank_pytest_missing_") as tmp:
        root = Path(tmp)
        (root / "readme.txt").write_text("patient repo\n", encoding="utf-8")
        (root / "jwt_auth.py").write_text("def issue():\n    return 'token'\n", encoding="utf-8")
        log_path = root / "missing.log"
        with hide_pytest():
            code = local_agent.run_post_actions(
                str(root),
                {"run_tests": True, "test_command": "pytest -q", "run_git_diff": False},
                str(log_path),
                workspace={"test_command": "pytest -q"},
                profile={},
            )
        text = log_path.read_text(encoding="utf-8")
        check(int(code) != 0, "python -m pytest fails when the patient repo has no tests or no pytest module")
        check("-m pytest -q" in text, "missing pytest on PATH runs python -m pytest -q")
        check("[tank] running tests: pytest -q" not in text, "missing pytest on PATH does not launch bare pytest")
        message = local_agent.verification_failure_text(code)
        check(
            message.startswith(f"Verification command exited {int(code)}: "),
            "module invocation failure includes an output snippet",
        )
        lowered = message.lower()
        check(
            "no module named" in lowered or "no tests ran" in lowered or "collected" in lowered or "error" in lowered,
            "module invocation snippet describes the pytest result",
        )


def test_failed_card_shows_snippet() -> None:
    models.init_db()
    ws = models.get_workspace("jobagent")
    check(ws is not None, "jobagent workspace is loaded")
    if ws is None:
        return
    run_id = models.create_run(ws["id"], None, "Apply JWT auth", provider="local_qwen")
    log_path = os.path.join(config.LOGS_DIR, f"run-{run_id}.log")
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAIL_SCRIPT))}"
    payload = {
        "response_type": "patch",
        "summary": "Add JWT checks",
        "plan": "",
        "patches": [{"path": "jwt_auth.py", "content": "def issue():\n    return 'token'\n"}],
        "post_actions": {
            "run_tests": True,
            "test_command": command,
            "run_git_diff": False,
        },
    }
    models.update_run(
        run_id,
        status="awaiting_approval",
        log_path=log_path,
        agent_payload=json.dumps(payload),
    )
    os.environ["EXAMPLE_API_KEY"] = SECRET
    try:
        approved = session_manager.approve_run(run_id)
    finally:
        os.environ.pop("EXAMPLE_API_KEY", None)
    check(approved is True, "approving the JWT patch run completes")
    finished = models.get_run(run_id)
    error = finished["error"] or ""
    log_text = Path(log_path).read_text(encoding="utf-8")
    check(finished["status"] == "failed", "failed verification marks the run failed")
    check(
        "is not recognized as an internal or external command" in error,
        "run error includes the command-not-found line",
    )
    check("operable program or batch file" in error, "run error includes the Windows shell detail")
    check(SECRET not in error, "run error redacts the API key")
    check("supersecrettokenvalue" not in error, "run error redacts the bearer token")
    check(SECRET not in log_text, "run log redacts the API key")
    check("supersecrettokenvalue" not in log_text, "run log redacts the bearer token")
    check((REPO / "jwt_auth.py").is_file(), "the approved patch is applied before verification")

    mission_id = mad_scientist.start_mission(
        ws["id"],
        "Add JWT auth",
        provider="local_qwen",
        max_steps=3,
        max_fix_loops=1,
    )
    scout = [
        run for run in models.list_mission_runs(mission_id)
        if run["stage_name"] == mad_scientist.SCOUT_STAGE
    ][0]
    scout_error = "Verification command exited 1: pytest: not found"
    models.update_run(
        scout["id"],
        status="failed",
        error=scout_error,
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(scout["id"])
    view = graph.mission_view(mission_id)
    scout_step = next(step for step in view["steps"] if step["role"] == "scout")
    check(scout_step["failure_reason"] == scout_error, "failed graph step exposes the run error")

    page = app.app.test_client().get("/workspaces/jobagent")
    body = page.get_data(as_text=True)
    check(page.status_code == 200, "workspace page renders")
    check('class="run-card-error"' in body, "failed run card shows the error")
    check(
        "is not recognized as an internal or external command" in body,
        "failed run card includes the command-not-found snippet",
    )
    check("pytest: not found" in body, "failed Mad Scientist step shows the verification snippet")
    check(SECRET not in body, "workspace page does not render the API key")
    check("supersecrettokenvalue" not in body, "workspace page does not render the bearer token")


def main() -> None:
    test_command_resolution()
    test_error_snippet()
    test_pytest_on_path_still_runs()
    test_missing_pytest_uses_module()
    test_failed_card_shows_snippet()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s)")
        raise SystemExit(1)
    print("pytest invocation validation passed")


if __name__ == "__main__":
    main()
