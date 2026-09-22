import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import repo_context


def main():
    with tempfile.TemporaryDirectory(prefix="tank_scout_parent_") as tmp:
        parent = Path(tmp).resolve()
        project = parent / "real_project"
        nested = project / "src" / "pkg"
        nested.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname = \"real-project\"\n", encoding="utf-8")
        (project / "README.md").write_text("Entry: src/pkg/main.py\n", encoding="utf-8")
        (nested / "main.py").write_text("print('hello')\n", encoding="utf-8")
        (project / "cookies.txt").write_text("session=secret\n", encoding="utf-8")
        (project / "image.png").write_bytes(b"\x89PNG\r\n")

        discovery = repo_context.discover_repo_root(str(nested))
        assert Path(discovery["repo_root"]) == project
        assert discovery["confidence"] == "high"

        profile = repo_context.detect_project_profile(str(nested))
        assert Path(profile["repo_root"]) == project
        assert profile["test_command"] is None
        assert profile["test_command_reason"] == "No automated test command detected."
        assert profile["static_validation_command"] == "python -m compileall -q ."

        (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        pytest_profile = repo_context.detect_project_profile(str(nested))
        assert pytest_profile["test_command"] == "pytest -q"

        scout = repo_context.repository_scout(str(nested))
        assert Path(scout["repo_root"]) == project
        assert "src/pkg/main.py" in scout["entry_points"]
        assert scout["files_skipped"]["sensitive"] >= 1
        assert scout["files_skipped"]["binary"] >= 1

        broad = repo_context.discover_repo_root(Path(parent).anchor)
        assert broad["repo_root"] is None
        assert broad["error"]

    print("repository scout validation passed")


if __name__ == "__main__":
    main()
