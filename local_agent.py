"""
Tank-native local agent runner.

Workflow:
  1. Read selected repo files for context
  2. Send context + task to a local/hosted chat model
  3. Model returns a plan or file patches (JSON)
  4. Patches pause for user approval
  5. On approval, apply patches
  6. Run tests / git diff only when the model explicitly requests them
  7. Log everything
"""
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import config
import git_sync
import output_quality
import prompt_library
import repo_context

# Re-exported for any callers that import these names from local_agent directly.
LOCAL_AGENT_SYSTEM_PROMPT = prompt_library.LOCAL_AGENT_SYSTEM_PROMPT
ANALYSIS_REPORT_PROMPT = prompt_library.ANALYSIS_REPORT_PROMPT


def _log(log_path: str, message: str):
    if not log_path:
        return
    with open(log_path, "a", encoding="utf-8", buffering=1) as f:
        f.write(message)
        if not message.endswith("\n"):
            f.write("\n")


def _openai_api_key(cfg: dict) -> str:
    env_name = cfg.get("api_key_env")
    if env_name:
        key = config.read_env_secret(env_name)
        if key:
            return key
    return (cfg.get("api_key_default") or "").strip()


def _require_api_key(cfg: dict, provider_id: str) -> str:
    key = _openai_api_key(cfg)
    env_name = cfg.get("api_key_env", "API key env var")
    if not key:
        raise ValueError(
            f"{env_name} is not set. Set it in PowerShell before starting Tank, e.g. "
            f'$env:{env_name} = "sk-or-..."'
        )
    return key


def _http_error_message(exc: urllib.error.HTTPError, cfg: dict) -> str:
    detail = exc.read().decode("utf-8", errors="replace")
    if exc.code == 401:
        env_name = cfg.get("api_key_env", "API key env var")
        key = config.read_env_secret(env_name) if env_name else ""
        if key:
            return (
                f"HTTP 401 Unauthorized from {cfg.get('base_url', 'API')}. "
                f"{env_name} is set ({len(key)} chars) but Mistral rejected it — "
                "the key may be invalid, expired, or copied with extra characters. "
                "Create a new key at https://console.mistral.ai/api-keys/ "
                f"API response: {detail}"
            )
        return (
            f"HTTP 401 Unauthorized from {cfg.get('base_url', 'API')}. "
            f"Set {env_name} in the same terminal before starting Tank. "
            f"API response: {detail}"
        )
    return f"HTTP {exc.code}: {detail}"


def _usage_cost_usd(usage) -> float | None:
    if not isinstance(usage, dict):
        return None
    for key in ("cost_usd", "cost", "total_cost"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _usage_tokens(usage) -> int | None:
    """Return a reported total, or None when the provider omitted usage."""
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if isinstance(prompt, int) or isinstance(completion, int):
        prompt_n = prompt if isinstance(prompt, int) and not isinstance(prompt, bool) else 0
        completion_n = (
            completion if isinstance(completion, int) and not isinstance(completion, bool) else 0
        )
        return prompt_n + completion_n
    return None


def _add_cost(total: float, cost: float | None) -> float:
    if cost is None or cost <= 0:
        return total
    return total + cost


def _attach_usage(payload: dict, cost: float, tokens: int | None = None) -> dict:
    if cost > 0:
        payload["cost_usd"] = round(cost, 6)
    if tokens is not None:
        payload["total_tokens"] = int(tokens)
    return payload


def _call_chat_model(
    cfg: dict,
    system_prompt: str,
    user_message: str,
    json_mode: bool | None = None,
) -> tuple[str, float | None, int | None]:
    base_url = cfg.get("base_url", "http://127.0.0.1:1234/v1").rstrip("/")
    model = cfg.get("model")
    if not model:
        raise ValueError("local_agent provider is missing `model`")

    api_key = _require_api_key(cfg, cfg.get("label", "provider"))

    url = f"{base_url}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": cfg.get("temperature", 0.2),
    }
    use_json = cfg.get("json_mode", True) if json_mode is None else json_mode
    if use_json:
        body["response_format"] = {"type": "json_object"}

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "http://127.0.0.1:8742",
        "X-Title": "Tank",
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 600)) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ValueError(_http_error_message(exc, cfg)) from exc
    usage = payload.get("usage")
    return (
        payload["choices"][0]["message"]["content"],
        _usage_cost_usd(usage),
        _usage_tokens(usage),
    )


def _extract_json(text: str) -> dict:
    text = text.strip()
    attempts = [text]
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        attempts.append(fence.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        snippet = text[start : end + 1]
        attempts.append(snippet)
        attempts.append(re.sub(r",\s*}", "}", snippet))
        attempts.append(re.sub(r",\s*]", "]", snippet))

    last_error = None
    for candidate in attempts:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    raise ValueError(f"Model response did not contain valid JSON: {last_error}")


def _default_post_actions() -> dict:
    return {
        "run_tests": False,
        "test_command": "",
        "run_git_diff": False,
    }


def _is_tester_context(stage_name: str | None = None, role: dict | None = None) -> bool:
    labels = [stage_name or ""]
    if role:
        labels.extend(str(role.get(key) or "") for key in ("slug", "name", "role", "goal"))
    combined = " ".join(labels).lower()
    return any(token in combined for token in ("tester", "test", "validator", "validation"))


def _is_reviewer_context(stage_name: str | None = None, role: dict | None = None) -> bool:
    labels = [stage_name or ""]
    if role:
        labels.extend(str(role.get(key) or "") for key in ("slug", "name", "role"))
    return "reviewer" in " ".join(labels).lower()


def verification_mode(stage_name: str | None = None, role: dict | None = None) -> str | None:
    """Tester rules win when a label matches both tester and reviewer."""
    if _is_tester_context(stage_name, role):
        return "tester"
    if _is_reviewer_context(stage_name, role):
        return "reviewer"
    return None


def _coerce_agent_response(
    raw: str,
    log_path: str,
    strict: bool = False,
    verification: str | None = None,
) -> dict:
    """Parse strict JSON from the model, or fall back to a plain-text plan.

    strict=True (used for mission stages) disables the plain-text
    fallback: any rejection from _validate_payload — invalid JSON or a
    detected meta-plan/checklist — propagates as a ValueError instead
    of being downgraded into a synthetic 'done' plan with post_actions
    silently zeroed out. A mission's fix-loop depends on seeing the
    real failure.
    """
    try:
        return _validate_payload(_extract_json(raw), verification=verification)
    except ValueError as exc:
        _log(log_path, f"[tank] JSON parse failed: {exc}\n")
        _log(log_path, f"--- raw model response ---\n{raw}\n--- end ---\n")
        if strict or not raw.strip():
            raise
        return {
            "response_type": "plan",
            "summary": "Model returned plain text instead of JSON",
            "plan": raw.strip(),
            "patches": [],
            "post_actions": _default_post_actions(),
        }


def _post_actions_view(post_actions: dict) -> dict:
    return {
        "run_tests": bool(post_actions.get("run_tests")),
        "test_command": (post_actions.get("test_command") or "").strip(),
        "run_git_diff": bool(post_actions.get("run_git_diff")),
    }


def _validate_payload(payload: dict, verification: str | None = None) -> dict:
    response_type = payload.get("response_type")
    allowed_types = {"plan", "patch", "result"} if verification else {"plan", "patch"}
    if response_type not in allowed_types:
        allowed = "', '".join(sorted(allowed_types))
        raise ValueError(f"response_type must be one of '{allowed}'")

    patches = payload.get("patches") or []
    if response_type == "patch" and not patches:
        raise ValueError("patch response must include at least one patch")

    post_actions = payload.get("post_actions") or {}
    actions = _post_actions_view(post_actions)
    summary = (payload.get("summary") or "").strip()
    has_test = actions["run_tests"] and bool(actions["test_command"])
    has_diff = actions["run_git_diff"]
    if verification and response_type in {"plan", "result"} and not patches:
        if verification == "tester":
            if not actions["run_tests"]:
                raise ValueError("tester response rejected: missing post_actions.run_tests=true")
            if not actions["test_command"]:
                raise ValueError("tester response rejected: missing post_actions.test_command")
        elif not (has_test or has_diff):
            raise ValueError(
                "reviewer response rejected: a test, build, or diff command is required"
            )
        if actions["run_tests"] and not actions["test_command"]:
            raise ValueError(
                f"{verification} response rejected: missing post_actions.test_command"
            )
        if not summary:
            raise ValueError(
                f"{verification} response rejected: summary must explain what will be validated"
            )
        return {
            "response_type": "plan",
            "summary": summary,
            "plan": payload.get("plan", ""),
            "patches": [],
            "post_actions": actions,
        }

    plan = (payload.get("plan") or "").strip()
    if response_type == "plan" and output_quality.looks_like_meta_plan(plan):
        raise ValueError("plan field is a procedure checklist, not a deliverable")

    return {
        "response_type": response_type,
        "summary": payload.get("summary", ""),
        "plan": payload.get("plan", ""),
        "patches": patches,
        "post_actions": actions,
    }


def _safe_repo_path(repo_path: str, rel_path: str) -> Path:
    root = Path(repo_path).resolve()
    target = (root / rel_path).resolve()
    target.relative_to(root)
    return target


def apply_patches(repo_path: str, patches: list[dict], log_path: str) -> None:
    for patch in patches:
        rel = patch.get("path", "").strip()
        if not rel:
            raise ValueError("Patch is missing path")
        target = _safe_repo_path(repo_path, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patch.get("content", ""), encoding="utf-8")
        _log(log_path, f"[tank] applied patch: {rel}\n")


# ---------------------------------------------------------------------------
# Tool implementations
# Each private function executes one post-action capability and returns an
# integer exit code (0 = success, non-zero = failure).
# ---------------------------------------------------------------------------

def _run_tests_tool(
    repo_path: str,
    post_actions: dict,
    log_path: str,
    workspace: dict | None = None,
    profile: dict | None = None,
) -> int:
    cmd = repo_context.resolve_test_command(
        repo_path, workspace=workspace, post_actions=post_actions, profile=profile
    )
    if not cmd:
        _log(log_path, "[tank] no test command configured or detected — skipping tests\n")
        return 0
    repo_error = config.check_repo_path(repo_path)
    if repo_error:
        _log(log_path, f"[tank] error: {repo_error}\n")
        return 1
    _log(log_path, f"[tank] running tests: {repo_context.format_test_command(cmd)}\n")
    run_target, use_shell = repo_context.prepare_test_execution(cmd)
    try:
        result = subprocess.run(
            run_target,
            cwd=repo_path,
            shell=use_shell,
            capture_output=True,
            text=True,
        )
    except NotADirectoryError:
        _log(log_path, f"[tank] error: repository path is not a valid directory: {repo_path}\n")
        return 1
    if result.stdout:
        _log(log_path, result.stdout)
    if result.stderr:
        _log(log_path, result.stderr)
    _log(log_path, f"[tank] test exit code: {result.returncode}\n")
    return result.returncode


def _run_git_diff_tool(
    repo_path: str,
    post_actions: dict,
    log_path: str,
    workspace: dict | None = None,
    profile: dict | None = None,
) -> int:
    _log(log_path, "[tank] running git diff\n")
    diff = git_sync.diff(repo_path)
    output = diff["stdout"] or diff["stderr"] or "(no diff)"
    _log(log_path, output + "\n")
    return 0 if (diff["ok"] or not diff["stderr"]) else 1


# ---------------------------------------------------------------------------
# Tool registry
# Maps the canonical tool name (used in role schema and prompt) to its
# metadata and executor.  Extend here to add new post-action capabilities.
#
# post_action_key: the field name in the model's post_actions JSON object
# description    : plain-text description injected into the system prompt
# fn             : executor matching (repo_path, post_actions, log_path,
#                  workspace=None, profile=None) -> int
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, dict] = {
    "run_tests": {
        "description": "Run the project test suite and report pass/fail results.",
        "post_action_key": "run_tests",
        "fn": _run_tests_tool,
    },
    "git_diff": {
        "description": "Show a git diff of all uncommitted changes since the last commit.",
        "post_action_key": "run_git_diff",
        "fn": _run_git_diff_tool,
    },
}


def _authorized_tools(role: dict | None) -> list[str] | None:
    """
    Return the authorized tools list from a role dict, or None if unrestricted.

    None  → no filtering; all tools are permitted (backward-compat default).
    []    → role explicitly restricts to zero tools.
    [..] → only these tool names may execute.
    """
    if not role:
        return None
    raw = role.get("tools")
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if str(t).strip()]
        except (json.JSONDecodeError, TypeError):
            pass
        return [t.strip() for t in raw.split(",") if t.strip()]
    return None


def run_post_actions(
    repo_path: str,
    post_actions: dict,
    log_path: str,
    workspace: dict | None = None,
    profile: dict | None = None,
    authorized_tools: list[str] | None = None,
) -> int:
    """
    Run follow-up actions only when the model explicitly requested them.

    authorized_tools
        When None (default), all tools in TOOL_REGISTRY are permitted —
        identical to pre-registry behaviour, ensuring backward compatibility.
        When a list is supplied, only tools whose name appears in that list
        may execute; any others requested by the model are silently skipped
        with a log notice.
    """
    code = 0
    any_ran = False

    for tool_name, tool in TOOL_REGISTRY.items():
        if not post_actions.get(tool["post_action_key"]):
            continue
        if authorized_tools is not None and tool_name not in authorized_tools:
            _log(
                log_path,
                f"[tank] tool '{tool_name}' is not authorized for this role — skipping\n",
            )
            continue
        result = tool["fn"](
            repo_path, post_actions, log_path,
            workspace=workspace, profile=profile,
        )
        any_ran = True
        code = code or result

    if not any_ran:
        _log(log_path, "[tank] no post-apply actions requested by model\n")

    return code


def _analysis_summary(markdown: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:240]
    return "Analysis report"


def _prepare_analysis_run(
    ctx, cfg: dict, files: dict, extra_system_prompt: str | None, profile: dict
) -> dict:
    """Markdown report path for audits, reviews, and status tasks."""
    _role = getattr(ctx, "role", None)
    authorized = _authorized_tools(_role)
    tool_descs = (
        {n: TOOL_REGISTRY[n]["description"] for n in authorized if n in TOOL_REGISTRY}
        if authorized is not None else None
    ) or None
    system_prompt = prompt_library.compose_system_prompt(
        "analysis",
        role_text=extra_system_prompt,
        role=_role,
        tool_descriptions=tool_descs,
    )

    user_message = repo_context.build_context_message(
        ctx.task,
        files,
        prior_run_context=ctx.prior_run_context,
        project_profile=profile,
        workspace_test_command=ctx.workspace.get("test_command"),
    )

    _log(ctx.log_path, "[tank] analysis mode: markdown report\n")
    cost = 0.0
    tokens = None
    raw, call_cost, call_tokens = _call_chat_model(
        cfg, system_prompt, user_message, json_mode=False
    )
    cost = _add_cost(cost, call_cost)
    if call_tokens is not None:
        tokens = (tokens or 0) + call_tokens

    if output_quality.looks_like_meta_plan(raw):
        _log(ctx.log_path, "[tank] meta-plan detected — retrying with stricter instructions\n")
        raw, call_cost, call_tokens = _call_chat_model(
            cfg,
            system_prompt,
            user_message + "\n\n" + output_quality.RETRY_NUDGE,
            json_mode=False,
        )
        cost = _add_cost(cost, call_cost)
        if call_tokens is not None:
            tokens = (tokens or 0) + call_tokens

    if output_quality.looks_like_meta_plan(raw):
        _log(ctx.log_path, "[tank] warning: response may still be low quality\n")

    payload = {
        "response_type": "plan",
        "summary": _analysis_summary(raw),
        "plan": raw.strip(),
        "patches": [],
        "post_actions": _default_post_actions(),
        "files_read": list(files.keys()),
    }
    _log(ctx.log_path, f"\n=== Summary ===\n{payload['summary']}\n")
    _log(ctx.log_path, f"\n=== Report ===\n{payload['plan']}\n")
    return _attach_usage(payload, cost, tokens)


def prepare_run(ctx, cfg: dict, extra_system_prompt: str | None = None) -> dict:
    """
    Phase 1: gather context, call model, return payload for approval or completion.
    """
    repo_path = ctx.workspace["repo_path"]
    profile = repo_context.detect_project_profile(repo_path)
    selected = repo_context.select_repo_files(repo_path, ctx.task, cfg)
    files = repo_context.read_repo_files(repo_path, selected, cfg)

    _log(ctx.log_path, f"[tank] local_agent provider={ctx.provider_id}\n")
    _log(ctx.log_path, f"[tank] project profile: {profile['summary']}\n")
    _log(ctx.log_path, f"[tank] read {len(files)} file(s): {', '.join(files) or '(none)'}\n")

    if ctx.mission_id is None and repo_context.is_analysis_task(ctx.task):
        # Advisory/audit-style roles intentionally relax into a markdown
        # report here. Mission stages never do — the orchestrator depends
        # on the strict response_type/post_actions schema below to decide
        # whether to advance, pause for approval, or fix-loop, and a
        # mission's free-text goal could otherwise trip this heuristic by
        # accident (e.g. a goal containing the word "review").
        return _prepare_analysis_run(ctx, cfg, files, extra_system_prompt, profile)

    _role = getattr(ctx, "role", None)
    authorized = _authorized_tools(_role)
    tool_descs = (
        {n: TOOL_REGISTRY[n]["description"] for n in authorized if n in TOOL_REGISTRY}
        if authorized is not None else None
    ) or None
    system_prompt = prompt_library.compose_system_prompt(
        "coding_agent",
        role_text=extra_system_prompt,
        role=_role,
        tool_descriptions=tool_descs,
    )

    user_message = repo_context.build_context_message(
        ctx.task,
        files,
        prior_run_context=ctx.prior_run_context,
        project_profile=profile,
        workspace_test_command=ctx.workspace.get("test_command"),
    )
    raw, cost, tokens = _call_chat_model(cfg, system_prompt, user_message)
    _log(ctx.log_path, "[tank] model response received\n")
    mode = verification_mode(ctx.stage_name, getattr(ctx, "role", None))

    try:
        payload = _coerce_agent_response(
            raw,
            ctx.log_path,
            strict=ctx.mission_id is not None,
            verification=mode,
        )
    except ValueError:
        if ctx.mission_id is not None:
            _log(ctx.log_path, "[tank] invalid/rejected model response in mission stage — failing run\n")
            raise
        _log(ctx.log_path, "[tank] invalid model response — retrying analysis mode\n")
        return _prepare_analysis_run(ctx, cfg, files, extra_system_prompt, profile)

    if payload["response_type"] == "plan" and output_quality.looks_like_meta_plan(payload.get("plan", "")):
        if ctx.mission_id is not None:
            _log(ctx.log_path, "[tank] meta-plan in mission stage — failing run\n")
            raise ValueError("Model returned a lazy meta-plan instead of doing the work")
        _log(ctx.log_path, "[tank] meta-plan in JSON — retrying analysis mode\n")
        return _prepare_analysis_run(ctx, cfg, files, extra_system_prompt, profile)
    payload["files_read"] = list(files.keys())

    if payload["post_actions"]["run_tests"]:
        resolved = repo_context.resolve_test_command(
            repo_path,
            workspace=ctx.workspace,
            post_actions=payload["post_actions"],
            profile=profile,
        )
        if resolved:
            payload["post_actions"]["test_command"] = resolved
            if ctx.stage_name == "tester":
                _log(ctx.log_path, f"[tank] tester stage — using test command: {resolved}\n")

    _log(ctx.log_path, f"\n=== Summary ===\n{payload['summary']}\n")
    if payload["response_type"] == "plan":
        _log(ctx.log_path, f"\n=== Plan ===\n{payload['plan']}\n")
    else:
        _log(ctx.log_path, "\n=== Proposed patches (awaiting approval) ===\n")
        for patch in payload["patches"]:
            rel = patch.get("path", "?")
            preview = (patch.get("content") or "")[:500]
            _log(ctx.log_path, f"\n--- {rel} ---\n{preview}\n")
            if len(patch.get("content") or "") > 500:
                _log(ctx.log_path, "... [truncated in log preview]\n")

    requested = []
    if payload["post_actions"]["run_tests"]:
        requested.append("tests")
    if payload["post_actions"]["run_git_diff"]:
        requested.append("git diff")
    if requested:
        _log(ctx.log_path, f"\n[tank] model requested post-apply: {', '.join(requested)}\n")

    return _attach_usage(payload, cost or 0.0, tokens)


def log_run_outcome(log_path: str, exit_code: int) -> None:
    """Emit a final [tank] line that reflects success vs failure (matches run status)."""
    if exit_code == 0:
        _log(log_path, "\n[tank] run finished successfully\n")
    else:
        _log(
            log_path,
            f"\n[tank] run finished with failures (exit code {exit_code}) - marked failed\n",
        )


def finalize_approved_run(
    repo_path: str,
    payload: dict,
    log_path: str,
    workspace: dict | None = None,
    authorized_tools: list[str] | None = None,
) -> int:
    """Phase 2: apply approved patches and run model-requested follow-ups."""
    _log(log_path, "\n[tank] patch approved - applying\n")
    apply_patches(repo_path, payload.get("patches") or [], log_path)
    profile = repo_context.detect_project_profile(repo_path)
    code = run_post_actions(
        repo_path, payload.get("post_actions") or {}, log_path,
        workspace=workspace, profile=profile,
        authorized_tools=authorized_tools,
    )
    log_run_outcome(log_path, code)
    return code
