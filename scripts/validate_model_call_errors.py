#!/usr/bin/env python3
"""Local-agent model calls must log before the request and keep failures.

A crew or mission step used to stop after "[tank] read N file(s)" when the
chat/completions call timed out, returned bad JSON, or raised something the
runner did not catch. This script does not call a real model and does not
write providers.yaml or providers.local.yaml.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="tank-model-call-"))
os.environ["TANK_DATA_DIR"] = str(TMP / "data")
os.environ["TANK_PROVIDERS_LOCAL_FILE"] = str(TMP / "providers.local.yaml")
os.environ["TANK_WORKSPACES_FILE"] = str(TMP / "workspaces.yaml")
os.environ["TANK_PROVIDERS_FILE"] = str(ROOT / "providers.yaml")
os.environ["TANK_MISSION_TEMPLATES_FILE"] = str(ROOT / "mission_templates.yaml")

import local_agent  # noqa: E402
import log_format  # noqa: E402
import providers  # noqa: E402
from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

SECRET = "sk-test-secret-value"
FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok: {label}")
        return
    FAILURES.append(label)
    print(f"FAIL: {label}")


def _cfg(**extra) -> dict:
    cfg = {
        "label": "Mistral agent (Tank-controlled)",
        "model": "codestral-latest",
        "base_url": "https://api.mistral.ai/v1",
        "api_key_default": SECRET,
        "json_mode": True,
        "timeout": 12,
        "temperature": 0.15,
    }
    cfg.update(extra)
    return cfg


def _log_path(name: str) -> Path:
    path = TMP / name
    path.write_text("[tank] read 2 file(s): a.py, b.py\n", encoding="utf-8")
    return path


class _Body:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _call(log_path: Path, urlopen, cfg=None):
    cfg = _cfg() if cfg is None else cfg
    with patch("local_agent.urllib.request.urlopen", urlopen):
        return local_agent._call_chat_model(
            cfg,
            "system",
            "user",
            log_path=str(log_path),
        )


def _assert_secret_hidden(text: str, label: str) -> None:
    check(SECRET not in text, label)
    check("Authorization" not in text, f"{label} omits the auth header")


def test_success_logs_before_request() -> None:
    log_path = _log_path("success.log")
    seen = {}

    def urlopen(req, timeout=None):
        seen["before"] = log_path.read_text(encoding="utf-8")
        seen["timeout"] = timeout
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["auth"] = req.get_header("Authorization")
        return _Body(
            b'{"choices":[{"message":{"content":"{\\"ok\\": true}"}}],'
            b'"usage":{"total_tokens":4}}'
        )

    content, _cost, tokens = _call(log_path, urlopen)
    text = log_path.read_text(encoding="utf-8")
    check("calling model…" in seen["before"], "calling model is logged before the HTTP request")
    check("model call failed" not in seen["before"], "the pre-request log is not a failure line")
    check("codestral-latest" in seen["before"], "pre-request log names the model")
    check("https://api.mistral.ai/v1" in seen["before"], "pre-request log names the endpoint")
    check(seen["timeout"] == 12, "configured timeout is passed to urlopen")
    check(seen["url"].endswith("/chat/completions"), "request targets chat/completions")
    check(seen["body"]["response_format"] == {"type": "json_object"}, "json_mode still requests JSON")
    check(seen["auth"] == f"Bearer {SECRET}", "the request still sends the configured key")
    check(content == '{"ok": true}', "a normal completion is returned")
    check(tokens == 4, "reported token usage is kept")
    check("calling model…" in text, "success path keeps the calling model line")
    _assert_secret_hidden(text, "success log hides the API key")


def test_timeout_is_logged() -> None:
    log_path = _log_path("timeout.log")

    def urlopen(req, timeout=None):
        raise TimeoutError("The read operation timed out")

    try:
        _call(log_path, urlopen)
    except local_agent.ModelCallError as exc:
        message = str(exc)
    else:
        check(False, "timeout raises ModelCallError")
        return
    text = log_path.read_text(encoding="utf-8")
    call_at = text.find("calling model…")
    fail_at = text.find("[tank] model call failed:")
    check(call_at >= 0 and fail_at > call_at, "timeout log has calling model before the failure")
    check("timed out after 12s" in text, "timeout log names the wait")
    check("https://api.mistral.ai/v1" in message, "timeout error names the endpoint")
    check("read 2 file(s)" in text and fail_at > text.find("read 2 file(s)"), "failure is after the file list")
    _assert_secret_hidden(text, "timeout log hides the API key")


def test_bad_and_empty_json_are_logged() -> None:
    cases = {
        "bad-json.log": b"not-json",
        "empty-body.log": b"   ",
        "empty-message.log": b'{"choices":[{"message":{"content":"  "}}]}',
    }
    for name, payload in cases.items():
        log_path = _log_path(name)

        def urlopen(req, timeout=None, payload=payload):
            return _Body(payload)

        try:
            _call(log_path, urlopen)
        except local_agent.ModelCallError as exc:
            message = str(exc)
        else:
            check(False, f"{name} raises ModelCallError")
            continue
        text = log_path.read_text(encoding="utf-8")
        check("[tank] model call failed:" in text, f"{name} writes model call failed")
        check("calling model…" in text, f"{name} still logs calling model")
        check(message in text, f"{name} keeps the same error in the exception")
        _assert_secret_hidden(text, f"{name} hides the API key")
    bad = (TMP / "bad-json.log").read_text(encoding="utf-8")
    empty = (TMP / "empty-body.log").read_text(encoding="utf-8")
    blank = (TMP / "empty-message.log").read_text(encoding="utf-8")
    check("invalid JSON" in bad, "bad JSON names invalid JSON")
    check("empty body" in empty, "empty body names an empty body")
    check("empty message" in blank, "empty message names an empty message")


def test_http_error_redacts_key() -> None:
    log_path = _log_path("http.log")

    def urlopen(req, timeout=None):
        body = json.dumps({"error": f"rejected {SECRET}"}).encode("utf-8")
        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(body),
        )

    try:
        _call(log_path, urlopen)
    except local_agent.ModelCallError as exc:
        message = str(exc)
    else:
        check(False, "HTTP error raises ModelCallError")
        return
    text = log_path.read_text(encoding="utf-8")
    check("HTTP 401" in text, "HTTP error log includes the status")
    check("[redacted]" in text, "HTTP error log redacts the key echoed by the API")
    check(SECRET not in message, "HTTP error exception hides the API key")
    _assert_secret_hidden(text, "HTTP error log hides the API key")


def test_unexpected_exception_is_logged() -> None:
    log_path = _log_path("unexpected.log")

    def urlopen(req, timeout=None):
        raise RuntimeError(f"socket exploded {SECRET}")

    try:
        _call(log_path, urlopen)
    except local_agent.ModelCallError as exc:
        message = str(exc)
    else:
        check(False, "unexpected exception raises ModelCallError")
        return
    text = log_path.read_text(encoding="utf-8")
    check("unexpected RuntimeError" in text, "unexpected exception names the type")
    check("socket exploded" in text, "unexpected exception keeps the safe detail")
    check("[redacted]" in message, "unexpected exception redacts a key in the message")
    _assert_secret_hidden(text, "unexpected exception log hides the API key")


def _run_starter(log_path: Path, exc: BaseException) -> tuple[list[int], list[tuple]]:
    done = threading.Event()
    codes: list[int] = []
    updates: list[tuple] = []

    def on_finished(code):
        codes.append(code)
        done.set()

    def update_run(run_id, **fields):
        updates.append((run_id, fields))

    ctx = providers.RunContext(
        run_id=41,
        task="build jwt auth",
        system_prompt=None,
        workspace={"id": 1, "repo_path": str(TMP)},
        role=None,
        log_path=str(log_path),
        provider_id="mistral_agent",
    )
    with patch("local_agent.prepare_run", side_effect=exc):
        with patch("models.update_run", update_run):
            providers._start_local_agent(ctx, _cfg(), on_finished)
            check(done.wait(3), "local agent thread finishes instead of hanging")
    return codes, updates


def test_starter_records_unexpected_exception() -> None:
    log_path = _log_path("starter-unexpected.log")
    codes, updates = _run_starter(log_path, RuntimeError("builder exploded"))
    text = log_path.read_text(encoding="utf-8")
    check(codes == [1], "unexpected exception fails the run")
    check("[tank] error: builder exploded" in text, "unexpected exception is written to the run log")
    check(updates and updates[0][0] == 41, "unexpected exception updates the run row")
    check(
        updates and updates[0][1].get("error") == "builder exploded",
        "unexpected exception is stored on the run for the card",
    )


def test_starter_keeps_model_call_error() -> None:
    log_path = _log_path("starter-model.log")
    log_path.write_text(
        "[tank] read 2 file(s): a.py, b.py\n"
        "[tank] calling model… model=codestral-latest endpoint=https://api.mistral.ai/v1\n"
        "[tank] model call failed: timed out after 12s contacting https://api.mistral.ai/v1\n",
        encoding="utf-8",
    )
    message = "timed out after 12s contacting https://api.mistral.ai/v1"
    codes, updates = _run_starter(log_path, local_agent.ModelCallError(message))
    text = log_path.read_text(encoding="utf-8")
    check(codes == [1], "model call error fails the run")
    check(text.count("[tank] model call failed:") == 1, "model call failure is not duplicated")
    check(
        updates and updates[0][1].get("error") == message,
        "model call error is stored on the run for the card",
    )


def test_log_panel_leads_with_the_failure() -> None:
    raw = (
        "[tank] read 2 file(s): a.py, b.py\n"
        "[tank] calling model… model=codestral-latest endpoint=https://api.mistral.ai/v1\n"
        "[tank] model call failed: timed out after 12s contacting https://api.mistral.ai/v1\n"
    )
    html = log_format.format_run_output({"provider": "mistral_agent", "status": "failed"}, raw)
    note = html.find("run-outcome-note")
    files = html.find("read 2 file(s)")
    check(note >= 0 and (files < 0 or note < files), "formatted log shows the failure before the file list")
    check("timed out after 12s" in html, "formatted log includes the timeout text")
    check(SECRET not in html, "formatted log hides the API key")


def _templates():
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["url_for"] = lambda endpoint, **kwargs: f"/{endpoint}/{kwargs.get('run_id', '')}"
    return env


def test_cards_show_the_error() -> None:
    env = _templates()
    run = {
        "id": 41,
        "status": "failed",
        "error": "timed out after 12s contacting https://api.mistral.ai/v1",
        "provider": "mistral_agent",
        "task": "build jwt auth",
        "parent_run_id": None,
        "mission_id": None,
        "stage_name": None,
    }
    run_html = env.get_template("partials/run_list.html").render(
        runs=[run],
        crew_provider_ids=[],
    )
    check("timed out after 12s" in run_html, "run card shows the model-call error")
    check("run-card-error" in run_html, "run card marks the error")

    child_html = env.get_template("partials/run_children.html").render(
        children=[{"run": run, "tools": []}],
    )
    check("timed out after 12s" in child_html, "crew step card shows the model-call error")

    mission_html = env.get_template("partials/mission_list.html").render(
        resume_error=None,
        workspace={"slug": "jobagent"},
        mission_entries=[
            {
                "mission": {
                    "id": 7,
                    "status": "failed",
                    "template": "mad_scientist",
                    "provider": "mistral_agent",
                    "goal": "add jwt auth",
                    "current_stage": "builder",
                    "fix_loop_count": 0,
                    "note": "",
                },
                "runs": [],
                "synthesis": None,
                "graph": {
                    "status": "failed",
                    "summary": "jwt",
                    "spend_cap_usd": None,
                    "spend_usd": 0,
                    "token_cap": None,
                    "tokens_used": 0,
                    "tokens_reported": False,
                    "waiting_approvals": [],
                    "blocked_reason": "",
                    "can_retry_scout": False,
                    "steps": [
                        {
                            "display_status": "failed",
                            "name": "builder",
                            "role": "builder",
                            "depends_on": [],
                            "unresolved": [],
                            "evaluation": None,
                            "attempt_count": 1,
                            "approval_available": False,
                            "attempts": [
                                {
                                    "approval_available": False,
                                    "attempt_kind": "execute",
                                    "status": "failed",
                                    "run_id": 41,
                                }
                            ],
                            "approval_pending_without_payload": False,
                            "blocked_reason": "",
                            "failure_reason": "timed out after 12s contacting https://api.mistral.ai/v1",
                        }
                    ],
                },
            }
        ],
    )
    check("builder" in mission_html and "failed" in mission_html, "mission card still shows the failed builder step")
    check("timed out after 12s" in mission_html, "mission builder card shows the model-call error")


def main() -> None:
    test_success_logs_before_request()
    test_timeout_is_logged()
    test_bad_and_empty_json_are_logged()
    test_http_error_redacts_key()
    test_unexpected_exception_is_logged()
    test_starter_records_unexpected_exception()
    test_starter_keeps_model_call_error()
    test_log_panel_leads_with_the_failure()
    test_cards_show_the_error()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        raise SystemExit(1)
    print("model call error checks passed")


if __name__ == "__main__":
    main()
