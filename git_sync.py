"""Thin wrapper around the git CLI for per-workspace sync controls."""
import os
import subprocess


def _git_bin():
    return os.environ.get("TANK_GIT_BIN", "git")


def _run(repo_path, *args):
    if not repo_path or not os.path.isdir(repo_path):
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"Repository path does not exist: {repo_path}",
        }
    try:
        result = subprocess.run(
            [_git_bin(), "-C", repo_path, *args],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "stdout": "",
            "stderr": (
                "git not found on PATH. Install Git for Windows or set TANK_GIT_BIN "
                "to the full path of git.exe."
            ),
        }
    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def status(repo_path):
    return _run(repo_path, "status", "--short", "--branch")


def pull(repo_path):
    return _run(repo_path, "pull", "--ff-only")


def push(repo_path):
    return _run(repo_path, "push")


def diff(repo_path):
    return _run(repo_path, "diff")


def commit_all(repo_path, message):
    add_result = _run(repo_path, "add", "-A")
    if not add_result["ok"]:
        return add_result
    return _run(repo_path, "commit", "-m", message)
