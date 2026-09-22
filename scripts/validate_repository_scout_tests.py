import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import local_agent
import repo_context


def main():
    with tempfile.TemporaryDirectory(prefix="tank_scout_tests_") as tmp:
        root = Path(tmp).resolve()
        (root / "pyproject.toml").write_text("[project]\nname = \"no-tests\"\n", encoding="utf-8")
        (root / "app.py").write_text("print('ok')\n", encoding="utf-8")

        profile = repo_context.detect_project_profile(str(root))
        assert profile["test_command"] is None
        assert profile["test_command_reason"] == "No automated test command detected."
        assert profile["static_validation_command"] == "python -m compileall -q ."
        assert repo_context.resolve_test_command(
            str(root),
            post_actions={"test_command": "pytest -q"},
            profile=profile,
        ) is None

        log = root / "static.log"
        code = local_agent._run_tests_tool(
            str(root),
            {"run_tests": True, "test_command": "pytest -q"},
            str(log),
            workspace={},
            profile=profile,
        )
        text = log.read_text(encoding="utf-8")
        assert code == 0
        assert "No automated test command detected." in text
        assert "running static validation: python -m compileall -q ." in text

        (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        pytest_profile = repo_context.detect_project_profile(str(root))
        assert pytest_profile["test_command"] == "pytest -q"
        assert repo_context.resolve_test_command(
            str(root),
            post_actions={"test_command": "pytest -q"},
            profile=pytest_profile,
        ) == "pytest -q"

    print("repository scout test-command validation passed")


if __name__ == "__main__":
    main()
