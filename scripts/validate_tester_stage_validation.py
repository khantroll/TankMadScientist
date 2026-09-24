#!/usr/bin/env python3
"""Tester and reviewer responses must name a real command.

A model summary that says the checks passed is rejected. When a command
does run, the tool exit code is the result.
"""
from __future__ import annotations

import json
import shlex
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import local_agent


def _tester_payload(test_command=None):
    if test_command is None:
        test_command = f"{shlex.quote(sys.executable)} -c \"print('ok')\""
    return {
        "response_type": "plan",
        "summary": "Run the project's test suite against the current repository state.",
        "plan": "",
        "patches": [],
        "post_actions": {
            "run_tests": True,
            "test_command": test_command,
            "run_git_diff": False,
        },
    }


def _assert_rejected(payload, message_part, verification=None):
    try:
        local_agent._validate_payload(payload, verification=verification)
    except ValueError as exc:
        assert message_part in str(exc), str(exc)
        return
    raise AssertionError("payload was accepted unexpectedly")


def main() -> None:
    accepted = local_agent._validate_payload(_tester_payload(), verification="tester")
    assert accepted["response_type"] == "plan"
    assert accepted["post_actions"]["run_tests"] is True
    assert accepted["post_actions"]["test_command"]
    assert accepted["patches"] == []

    result_payload = _tester_payload()
    result_payload["response_type"] = "result"
    accepted_result = local_agent._validate_payload(result_payload, verification="tester")
    assert accepted_result["response_type"] == "plan"

    missing_command = _tester_payload("")
    _assert_rejected(missing_command, "missing post_actions.test_command", verification="tester")

    missing_run_tests = _tester_payload()
    missing_run_tests["post_actions"]["run_tests"] = False
    _assert_rejected(missing_run_tests, "missing post_actions.run_tests=true", verification="tester")

    summary_only = _tester_payload()
    summary_only["summary"] = "All tests passed."
    summary_only["post_actions"] = {
        "run_tests": False,
        "test_command": "",
        "run_git_diff": False,
    }
    _assert_rejected(summary_only, "missing post_actions.run_tests=true", verification="tester")

    reviewer_summary = {
        "response_type": "plan",
        "summary": "Looks good, I would pass this.",
        "plan": "No command was run.",
        "patches": [],
        "post_actions": {"run_tests": False, "test_command": "", "run_git_diff": False},
    }
    _assert_rejected(
        reviewer_summary,
        "a test, build, or diff command is required",
        verification="reviewer",
    )
    reviewer_diff = dict(reviewer_summary)
    reviewer_diff["post_actions"] = {
        "run_tests": False,
        "test_command": "",
        "run_git_diff": True,
    }
    accepted_diff = local_agent._validate_payload(reviewer_diff, verification="reviewer")
    assert accepted_diff["post_actions"]["run_git_diff"] is True

    empty_builder = {
        "response_type": "plan",
        "summary": "Run tests",
        "plan": "",
        "patches": [],
        "post_actions": {
            "run_tests": False,
            "test_command": "",
            "run_git_diff": False,
        },
    }
    _assert_rejected(empty_builder, "procedure checklist", verification=None)

    assert local_agent.verification_mode("Check", {"slug": "tester"}) == "tester"
    assert local_agent.verification_mode("Check", {"slug": "reviewer"}) == "reviewer"
    assert local_agent.verification_mode("Build", {"slug": "builder"}) is None

    with tempfile.TemporaryDirectory(prefix="tank_tester_validation_") as tmp:
        temp_root = Path(tmp).resolve()
        pass_log = temp_root / "pass.log"
        pass_payload = _tester_payload(
            f"{shlex.quote(sys.executable)} -c \"print('PASSOUT')\""
        )
        pass_code = local_agent.run_post_actions(
            str(temp_root),
            pass_payload["post_actions"],
            str(pass_log),
            workspace={"test_command": pass_payload["post_actions"]["test_command"]},
            profile={},
        )
        assert pass_code == 0, pass_code
        pass_text = pass_log.read_text(encoding="utf-8")
        assert "PASSOUT" in pass_text
        assert "[tank] test exit code: 0" in pass_text

        fail_log = temp_root / "fail.log"
        fail_command = (
            f"{shlex.quote(sys.executable)} -c "
            "\"import sys; print('FAILOUT'); print('FAILERR', file=sys.stderr); sys.exit(5)\""
        )
        fail_payload = _tester_payload(fail_command)
        fail_code = local_agent.run_post_actions(
            str(temp_root),
            fail_payload["post_actions"],
            str(fail_log),
            workspace={"test_command": fail_command},
            profile={},
        )
        assert fail_code == 5, fail_code
        fail_text = fail_log.read_text(encoding="utf-8")
        assert "[tank] test exit code: 5" in fail_text
        assert "FAILOUT" in fail_text
        assert "FAILERR" in fail_text

        raw = json.dumps(_tester_payload())
        coerced = local_agent._coerce_agent_response(
            raw,
            str(temp_root / "coerce.log"),
            strict=True,
            verification="tester",
        )
        assert coerced["post_actions"]["run_tests"] is True

    print("tester stage validation passed")


if __name__ == "__main__":
    main()
