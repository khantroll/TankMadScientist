"""
Pluggable agent backends.

Supported provider types:
  claude_code       — Claude Code CLI (full external coding agent)
  openai_compatible — Chat/log-only advisory mode
  local_agent       — Tank-controlled agent with repo tools + approval flow
"""
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

import yaml

import config
import local_agent
import output_quality
import prompt_library
import repo_context


@dataclass(frozen=True)
class RunContext:
    run_id: int
    task: str
    system_prompt: str | None
    workspace: dict
    role: dict | None
    log_path: str
    provider_id: str
    prior_run_context: str | None = None
    parent_run_id: int | None = None
    mission_id: int | None = None
    stage_name: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


class ProviderError(Exception):
    pass


_registry: dict[str, dict] = {}
_default_provider: str = "claude_code"


_ALLOWED_API_KEY_DEFAULT = "lm-studio"


def _reject_tracked_api_key_defaults(path: str, registry: dict) -> None:
    """Refuse a real key pasted into the tracked providers.yaml."""
    if os.path.basename(path) != "providers.yaml":
        return
    leaked = []
    for provider_id, cfg in registry.items():
        if not isinstance(cfg, dict):
            continue
        default = cfg.get("api_key_default")
        if default is None:
            continue
        text = str(default).strip()
        if text and text != _ALLOWED_API_KEY_DEFAULT:
            leaked.append(str(provider_id))
    if not leaked:
        return
    names = ", ".join(sorted(leaked))
    raise RuntimeError(
        "providers.yaml has api_key_default set for "
        f"{names}. That file is tracked. Move the key to an environment "
        "variable (api_key_env) or to providers.local.yaml. The only literal "
        f"default allowed in providers.yaml is {_ALLOWED_API_KEY_DEFAULT!r}."
    )


def load_providers():
    """Load provider definitions from providers.yaml (or env override)."""
    global _registry, _default_provider
    path = config.PROVIDERS_FILE
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except FileNotFoundError:
        _registry = {
            "claude_code": {"type": "claude_code", "label": "Claude Code"},
        }
        _default_provider = "claude_code"
        return

    registry = doc.get("providers") or {}
    _reject_tracked_api_key_defaults(path, registry)
    _registry = registry
    _default_provider = (
        os.environ.get("TANK_DEFAULT_PROVIDER")
        or doc.get("default_provider")
        or "claude_code"
    )


def list_providers() -> list[tuple[str, str]]:
    """Return (id, label) pairs for UI dropdowns."""
    return [
        (pid, cfg.get("label") or pid)
        for pid, cfg in _registry.items()
    ]


def provider_key_status(provider_id: str) -> dict:
    """Report whether a provider's API key env var is set (not whether it is valid)."""
    cfg = _registry.get(provider_id) or {}
    env_name = cfg.get("api_key_env")
    if env_name:
        key = config.read_env_secret(env_name)
        return {"env": env_name, "set": bool(key), "length": len(key)}
    default = (cfg.get("api_key_default") or "").strip()
    return {"env": None, "set": bool(default), "length": len(default)}


def probe_provider(provider_id: str) -> dict:
    """Lightweight auth check against the provider API."""
    cfg = _provider_cfg(provider_id)
    ptype = cfg.get("type", "claude_code")
    if ptype == "claude_code":
        import shutil
        found = shutil.which(config.CLAUDE_BIN)
        return {"ok": bool(found), "detail": config.CLAUDE_BIN if found else "CLI not on PATH"}

    base_url = cfg.get("base_url", "").rstrip("/")
    if not base_url:
        return {"ok": False, "detail": "no base_url configured"}

    try:
        key = _require_api_key(cfg)
    except ProviderError as exc:
        return {"ok": False, "detail": str(exc)}

    url = f"{base_url}/models"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"ok": True, "status": resp.status}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "detail": detail}
    except urllib.error.URLError as exc:
        return {"ok": False, "detail": str(exc.reason)}


def get_default_provider() -> str:
    return _default_provider


def resolve_provider(provider_id: str | None) -> str:
    if provider_id and provider_id in _registry:
        return provider_id
    if _default_provider in _registry:
        return _default_provider
    if _registry:
        return next(iter(_registry))
    return "claude_code"


def provider_label(provider_id: str | None) -> str:
    if not provider_id:
        return get_default_provider()
    cfg = _registry.get(provider_id)
    return cfg.get("label", provider_id) if cfg else provider_id


def _provider_cfg(provider_id: str) -> dict:
    cfg = _registry.get(provider_id)
    if not cfg:
        raise ProviderError(f"Unknown provider: {provider_id}")
    return cfg


def get_provider_type(provider_id: str | None) -> str | None:
    if not provider_id:
        return None
    cfg = _registry.get(provider_id)
    return cfg.get("type") if cfg else None


def _append_log(log_path: str, text: str):
    """Append text to a log without forcing newlines (for streamed tokens)."""
    if not text:
        return
    with open(log_path, "a", encoding="utf-8", buffering=1) as f:
        f.write(text)


def _write_log_line(log_path: str, line: str):
    with open(log_path, "a", encoding="utf-8", buffering=1) as f:
        f.write(line)
        if not line.endswith("\n"):
            f.write("\n")


def _effective_task(ctx: RunContext) -> str:
    if not ctx.prior_run_context:
        return ctx.task
    return (
        f"{ctx.task}\n\n"
        "---\n"
        "Prior run output (continue from this):\n"
        f"{ctx.prior_run_context}"
    )


def _build_claude_command(ctx: RunContext) -> list[str]:
    cmd = [
        config.CLAUDE_BIN,
        "-p", _effective_task(ctx),
        "--output-format", "stream-json",
        "--permission-mode", "acceptEdits",
    ]
    if ctx.system_prompt:
        cmd += ["--append-system-prompt", ctx.system_prompt]
    mcp_path = ctx.workspace.get("mcp_config_path")
    if mcp_path:
        cmd += ["--mcp-config", mcp_path]
    return cmd


def _run_claude_code(ctx: RunContext) -> subprocess.Popen:
    repo_error = config.check_repo_path(ctx.workspace.get("repo_path"))
    if repo_error:
        raise NotADirectoryError(repo_error)
    cmd = _build_claude_command(ctx)
    return subprocess.Popen(
        cmd,
        cwd=ctx.workspace["repo_path"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def _openai_api_key(cfg: dict) -> str:
    env_name = cfg.get("api_key_env")
    if env_name:
        key = config.read_env_secret(env_name)
        if key:
            return key
    return (cfg.get("api_key_default") or "").strip()


def _require_api_key(cfg: dict) -> str:
    key = _openai_api_key(cfg)
    env_name = cfg.get("api_key_env", "API key env var")
    if not key:
        raise ProviderError(
            f"{env_name} is not set. Set it in PowerShell before starting Tank, e.g. "
            f'$env:{env_name} = "sk-or-..."'
        )
    return key


# Re-exported alias so any caller that imported ADVISORY_SYSTEM_PROMPT from
# providers directly continues to work without change.
ADVISORY_SYSTEM_PROMPT = prompt_library.ADVISORY_SYSTEM_PROMPT


def _repo_context_message(ctx: RunContext, cfg: dict, task: str) -> str:
    repo = ctx.workspace.get("repo_path")
    if not repo or not cfg.get("include_repo_context", True):
        return repo_context.build_context_message(
            task, {}, prior_run_context=ctx.prior_run_context
        )
    profile = repo_context.detect_project_profile(repo)
    selected = repo_context.select_repo_files(repo, task, cfg)
    files = repo_context.read_repo_files(repo, selected, cfg)
    _write_log_line(
        ctx.log_path,
        f"[tank] read {len(files)} file(s) for context: {', '.join(files) or '(none)'}\n",
    )
    return repo_context.build_context_message(
        task,
        files,
        prior_run_context=ctx.prior_run_context,
        project_profile=profile,
        workspace_test_command=ctx.workspace.get("test_command"),
    )


def _openai_messages(ctx: RunContext, cfg: dict) -> list[dict]:
    # cfg may provide a custom system_prompt override; if not, use the
    # advisory base.  Either way, compose_system_prompt adds the quality
    # guard and wraps the role text cleanly.
    base = cfg.get("system_prompt") or None
    if base:
        # Caller supplied a fully custom base — still wrap with role/guard.
        system = prompt_library.compose_system_prompt(
            "advisory",
            role_text="\n\n".join(
                filter(None, [ctx.system_prompt,
                               repo_context.analysis_system_addendum()
                               if repo_context.is_analysis_task(ctx.task) else None])
            ) or None,
            role=ctx.role,
        )
        # Prepend the custom base ahead of the library base.
        system = base.rstrip() + "\n\n" + system
    else:
        task_mod = (
            repo_context.analysis_system_addendum()
            if repo_context.is_analysis_task(ctx.task)
            else None
        )
        system = prompt_library.compose_system_prompt(
            "advisory",
            role_text=ctx.system_prompt,
            task_modifier=task_mod,
            role=ctx.role,
        )

    user_content = _repo_context_message(ctx, cfg, ctx.task)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


def _openai_analysis_messages(ctx: RunContext, cfg: dict) -> list[dict]:
    """Messages for advisory analysis — uses the markdown report prompt."""
    system = prompt_library.compose_system_prompt(
        "analysis",
        role_text=ctx.system_prompt,
        role=ctx.role,
    )
    user_content = _repo_context_message(ctx, cfg, ctx.task)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


def _complete_openai(ctx: RunContext, cfg: dict, messages: list[dict]) -> str:
    base_url = cfg.get("base_url", "http://127.0.0.1:1234/v1").rstrip("/")
    model = cfg.get("model")
    url = f"{base_url}/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": cfg.get("temperature", 0.15),
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_require_api_key(cfg)}",
        "HTTP-Referer": "http://127.0.0.1:8742",
        "X-Title": "Tank",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=cfg.get("timeout", 600)) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


def _run_advisory_analysis(ctx: RunContext, cfg: dict) -> int:
    base_url = cfg.get("base_url", "http://127.0.0.1:1234/v1").rstrip("/")
    model = cfg.get("model")
    if not model:
        raise ProviderError(f"Provider {ctx.provider_id} is missing `model`")

    messages = _openai_analysis_messages(ctx, cfg)
    _write_log_line(
        ctx.log_path,
        f"[tank] provider={ctx.provider_id} model={model} endpoint={base_url}\n",
    )
    _write_log_line(ctx.log_path, "[tank] analysis mode: markdown report\n")

    try:
        text = _complete_openai(ctx, cfg, messages)
        if output_quality.looks_like_meta_plan(text):
            _write_log_line(ctx.log_path, "[tank] meta-plan detected — retrying\n")
            messages[-1]["content"] += "\n\n" + output_quality.RETRY_NUDGE
            text = _complete_openai(ctx, cfg, messages)
        _append_log(ctx.log_path, text)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        _write_log_line(ctx.log_path, f"[tank] HTTP {exc.code}: {detail}\n")
        return 1
    except urllib.error.URLError as exc:
        _write_log_line(ctx.log_path, f"[tank] connection error: {exc.reason}\n")
        return 1

    _write_log_line(ctx.log_path, "\n[tank] run complete\n")
    return 0


def _stream_openai_compatible(ctx: RunContext, cfg: dict) -> int:
    base_url = cfg.get("base_url", "http://127.0.0.1:1234/v1").rstrip("/")
    model = cfg.get("model")
    if not model:
        raise ProviderError(f"Provider {ctx.provider_id} is missing `model`")

    url = f"{base_url}/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": _openai_messages(ctx, cfg),
        "stream": True,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_require_api_key(cfg)}",
        "HTTP-Referer": "http://127.0.0.1:8742",
        "X-Title": "Tank",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    _write_log_line(
        ctx.log_path,
        f"[tank] provider={ctx.provider_id} model={model} endpoint={base_url}\n",
    )

    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 600)) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = (
                    chunk.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content")
                )
                if delta:
                    _append_log(ctx.log_path, delta)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        _write_log_line(ctx.log_path, f"[tank] HTTP {exc.code}: {detail}\n")
        return 1
    except urllib.error.URLError as exc:
        _write_log_line(ctx.log_path, f"[tank] connection error: {exc.reason}\n")
        return 1

    _write_log_line(ctx.log_path, "\n[tank] run complete\n")
    return 0


# ---------------------------------------------------------------------------
# Private runner functions — one per provider type.
# Each has the same signature: (ctx, cfg, on_finished) -> int | None
# and is responsible for spawning a daemon thread and returning a PID (or
# None).  on_finished(exit_code) must be called exactly once from within
# the thread when the run is complete.
# ---------------------------------------------------------------------------

def _start_claude_code(
    ctx: RunContext,
    cfg: dict,
    on_finished: Callable[[int], None],
) -> int:
    try:
        proc = _run_claude_code(ctx)
    except FileNotFoundError as exc:
        raise ProviderError(
            f"Claude Code CLI not found ({config.CLAUDE_BIN!r} is not on PATH). "
            "Install Claude Code, set TANK_CLAUDE_BIN to its path, or choose a "
            "different provider for this workspace (e.g. mistral_agent in workspaces.yaml)."
        ) from exc
    except NotADirectoryError as exc:
        raise ProviderError(str(exc)) from exc

    import session_manager

    session_manager.register_active_proc(ctx.run_id, proc)

    def _watch():
        try:
            with open(ctx.log_path, "a", buffering=1) as f:
                for line in proc.stdout:
                    if ctx.cancel_event.is_set():
                        break
                    f.write(line)
        finally:
            on_finished(proc.wait())

    threading.Thread(target=_watch, daemon=True).start()
    return proc.pid


def _start_openai_compatible(
    ctx: RunContext,
    cfg: dict,
    on_finished: Callable[[int], None],
) -> None:
    def _watch():
        if ctx.cancel_event.is_set():
            on_finished(1)
            return
        try:
            if repo_context.is_analysis_task(ctx.task):
                code = _run_advisory_analysis(ctx, cfg)
            else:
                code = _stream_openai_compatible(ctx, cfg)
        except ProviderError as exc:
            _write_log_line(ctx.log_path, f"[tank] error: {exc}\n")
            code = 1
        if ctx.cancel_event.is_set():
            on_finished(1)
            return
        on_finished(code)

    threading.Thread(target=_watch, daemon=True).start()
    return None


def _start_local_agent(
    ctx: RunContext,
    cfg: dict,
    on_finished: Callable[[int], None],
) -> None:
    def _watch():
        import models

        if ctx.cancel_event.is_set():
            on_finished(1)
            return
        try:
            payload = local_agent.prepare_run(
                ctx, cfg, extra_system_prompt=ctx.system_prompt
            )
        except (ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            _write_log_line(ctx.log_path, f"[tank] error: {exc}\n")
            on_finished(1)
            return

        if ctx.cancel_event.is_set():
            on_finished(1)
            return

        if payload["response_type"] == "plan":
            post_actions = payload.get("post_actions") or {}
            code = 0
            if post_actions.get("run_tests") or post_actions.get("run_git_diff"):
                auth = local_agent._authorized_tools(ctx.role)
                code = local_agent.run_post_actions(
                    ctx.workspace["repo_path"],
                    post_actions,
                    ctx.log_path,
                    workspace=ctx.workspace,
                    authorized_tools=auth,
                )
                local_agent.log_run_outcome(ctx.log_path, code)
            current = models.get_run(ctx.run_id)
            if current and current["status"] == "cancelled":
                on_finished(1)
                return
            models.update_run(
                ctx.run_id,
                agent_payload=json.dumps(payload),
                status="done" if code == 0 else "failed",
            )
            on_finished(code)
            return

        current = models.get_run(ctx.run_id)
        if current and current["status"] == "cancelled":
            on_finished(1)
            return
        models.update_run(
            ctx.run_id,
            agent_payload=json.dumps(payload),
            status="awaiting_approval",
        )
        import mad_scientist_graph as graph

        graph.sync_run_status(ctx.run_id)

    threading.Thread(target=_watch, daemon=True).start()
    return None


def _start_crew(
    ctx: RunContext,
    cfg: dict,
    on_finished: Callable[[int], None],
) -> None:
    """
    Delegate to orchestrator.run_sequential_crew for CrewAI-style execution.

    Imports orchestrator inside the thread to avoid a module-level circular
    dependency (providers -> orchestrator -> session_manager -> providers).
    """
    def _watch():
        import orchestrator
        orchestrator.run_sequential_crew(ctx, cfg, on_finished)

    threading.Thread(target=_watch, daemon=True).start()
    return None


# ---------------------------------------------------------------------------
# Provider runner registry
# Maps provider type strings → runner functions.
# Adding a new provider type only requires adding an entry here plus a
# corresponding runner function; start_run itself never needs to change.
# ---------------------------------------------------------------------------

_PROVIDER_RUNNERS: dict[str, Callable[[RunContext, dict, Callable[[int], None]], int | None]] = {
    "claude_code": _start_claude_code,
    "openai_compatible": _start_openai_compatible,
    "local_agent": _start_local_agent,
    "crew": _start_crew,
}


def start_run(
    ctx: RunContext,
    on_finished: Callable[[int], None],
) -> int | None:
    """
    Dispatch a provider run to the appropriate runner via _PROVIDER_RUNNERS.

    Returns a PID for subprocess providers, or None for in-process providers.
    Calls on_finished(exit_code) when the run completes.
    """
    cfg = _provider_cfg(ctx.provider_id)
    ptype = cfg.get("type", "claude_code")
    runner = _PROVIDER_RUNNERS.get(ptype)
    if runner is None:
        raise ProviderError(f"Unsupported provider type: {ptype!r}")
    return runner(ctx, cfg, on_finished)


load_providers()
