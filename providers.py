"""
Pluggable agent backends.

Supported provider types:
  claude_code       — Claude Code CLI (full external coding agent)
  openai_compatible — Chat/log-only advisory mode
  local_agent       — Tank-controlled agent with repo tools + approval flow
"""
import json
import os
import re
import subprocess
import tempfile
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
_file_default_provider: str = "claude_code"


_ALLOWED_API_KEY_DEFAULT = "lm-studio"
_SAFE_PROVIDER_TYPES = ("claude_code", "openai_compatible", "local_agent")
_ADDABLE_PROVIDER_TYPES = ("openai_compatible", "local_agent")
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BOOL_FIELDS = ("include_repo_context", "json_mode")
_INT_FIELDS = (
    "max_context_files",
    "max_context_files_analysis",
    "max_file_bytes",
    "timeout",
)
_FLOAT_FIELDS = ("temperature",)
_CREW_TEXT_FIELDS = ("label", "process")
_LEAF_TEXT_FIELDS = ("label", "model", "base_url", "api_key_env")
_GROUP_ORDER = (
    ("local_agent", "Tank-controlled agents"),
    ("openai_compatible", "Advisory / chat-only"),
    ("claude_code", "Claude Code"),
    ("crew", "Crews"),
)
_TYPE_LABELS = {
    "local_agent": "Tank-controlled agent",
    "openai_compatible": "Advisory / chat-only",
    "claude_code": "Claude Code",
    "crew": "Crew",
}
_FIELD_LABELS = {
    "label": "Label",
    "model": "Model",
    "base_url": "Base URL",
    "api_key_env": "API key env var",
    "include_repo_context": "Include repo context",
    "json_mode": "JSON mode",
    "max_context_files": "Max context files",
    "max_context_files_analysis": "Max context files (analysis)",
    "max_file_bytes": "Max file bytes",
    "temperature": "Temperature",
    "timeout": "Timeout (seconds)",
    "process": "Process",
}


class ProviderConfigError(ValueError):
    """Invalid Configure AI input. Safe to show in the page."""


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


def providers_local_path() -> str:
    """Path of the gitignored local overlay next to the tracked providers file."""
    override = os.environ.get("TANK_PROVIDERS_LOCAL_FILE", "").strip()
    if override:
        return config.normalize_path(override)
    path = config.PROVIDERS_FILE
    root, ext = os.path.splitext(path)
    if root.endswith(".local"):
        return path
    return root + ".local" + (ext or ".yaml")


def _local_file_is_distinct(local_path: str | None = None) -> bool:
    local_path = local_path or providers_local_path()
    if not local_path:
        return False
    return os.path.abspath(local_path) != os.path.abspath(config.PROVIDERS_FILE)


def _read_yaml_doc(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    if not isinstance(doc, dict):
        raise ProviderConfigError(f"{os.path.basename(path)} must be a YAML mapping")
    providers = doc.get("providers")
    if providers is None:
        doc["providers"] = {}
    elif not isinstance(providers, dict):
        raise ProviderConfigError(f"{os.path.basename(path)} providers must be a mapping")
    return doc


def _write_yaml_doc(path: str, doc: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".providers-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(
                doc,
                handle,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
                width=4096,
            )
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if os.path.abspath(path) == os.path.abspath(providers_local_path()):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _merge_provider_docs(base: dict, overlay: dict) -> dict:
    """Overlay local provider fields and default_provider onto the tracked doc."""
    merged = dict(base)
    providers = dict(base.get("providers") or {})
    for provider_id, cfg in (overlay.get("providers") or {}).items():
        if isinstance(cfg, dict) and isinstance(providers.get(provider_id), dict):
            combined = dict(providers[provider_id])
            combined.update(cfg)
            providers[provider_id] = combined
        else:
            providers[provider_id] = cfg
    merged["providers"] = providers
    if overlay.get("default_provider"):
        merged["default_provider"] = overlay["default_provider"]
    return merged


def _load_local_overlay(path: str) -> dict:
    if not _local_file_is_distinct(path) or not os.path.exists(path):
        return {}
    return _read_yaml_doc(path)


def load_providers():
    """Load provider definitions from providers.yaml plus providers.local.yaml."""
    global _registry, _default_provider, _file_default_provider
    path = config.PROVIDERS_FILE
    try:
        doc = _read_yaml_doc(path)
    except FileNotFoundError:
        doc = {
            "default_provider": "claude_code",
            "providers": {
                "claude_code": {"type": "claude_code", "label": "Claude Code"},
            },
        }

    registry = doc.get("providers") or {}
    _reject_tracked_api_key_defaults(path, registry)
    local_doc = _load_local_overlay(providers_local_path())
    if local_doc:
        doc = _merge_provider_docs(doc, local_doc)
        registry = doc.get("providers") or {}

    _registry = registry
    _file_default_provider = doc.get("default_provider") or "claude_code"
    _default_provider = (
        os.environ.get("TANK_DEFAULT_PROVIDER")
        or _file_default_provider
        or "claude_code"
    )


def list_providers() -> list[tuple[str, str]]:
    """Return (id, label) pairs for UI dropdowns."""
    return [
        (pid, cfg.get("label") or pid)
        for pid, cfg in _registry.items()
    ]


def provider_key_status(provider_id: str) -> dict:
    """Report whether a usable API key is available (not whether it is valid).

    An environment variable wins when it is actually set. Otherwise a stored
    api_key_default other than the public lm-studio dummy counts as set.
    That stored value comes from the gitignored local overlay; the tracked
    file is not allowed to carry a real key. The status never includes the
    secret itself.
    """
    cfg = _registry.get(provider_id) or {}
    env_name = str(cfg.get("api_key_env") or "").strip() or None
    env_key = config.read_env_secret(env_name) if env_name else ""
    stored = str(cfg.get("api_key_default") or "").strip()
    local = bool(stored) and stored != _ALLOWED_API_KEY_DEFAULT
    if env_key:
        return {
            "env": env_name,
            "set": True,
            "length": len(env_key),
            "source": "env",
            "local": local,
        }
    if local or (stored and not env_name):
        return {
            "env": env_name,
            "set": True,
            "length": len(stored),
            "source": "stored",
            "local": True,
        }
    return {
        "env": env_name,
        "set": False,
        "length": 0,
        "source": "",
        "local": False,
    }


def default_provider_info() -> dict:
    """Effective default, the file default, and whether an env var wins."""
    env = os.environ.get("TANK_DEFAULT_PROVIDER") or ""
    return {
        "effective": _default_provider,
        "file": _file_default_provider,
        "env": env,
        "env_override": bool(env),
    }


def _looks_like_secret(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    if lowered.startswith("sk-") or lowered.startswith("sk_"):
        return True
    return False


def _display_label(label: str, ptype: str) -> str:
    low = label.lower()
    if ptype == "crew" and "crew" not in low:
        return f"{label} · crew"
    if ptype == "openai_compatible" and "advisory" not in low and "chat" not in low:
        return f"{label} · advisory"
    if ptype == "local_agent" and "agent" not in low and "tank" not in low:
        return f"{label} · Tank-controlled"
    if ptype == "claude_code" and "claude" not in low:
        return f"{label} · Claude Code"
    return label


def _provider_option(provider_id: str, cfg: dict) -> dict:
    ptype = cfg.get("type") or "claude_code"
    key = provider_key_status(provider_id)
    needs_key = ptype in ("openai_compatible", "local_agent")
    env = key.get("env") or ""
    fallback = bool(str(cfg.get("api_key_default") or "").strip())
    missing = needs_key and not key.get("set")
    return {
        "id": provider_id,
        "label": _display_label(cfg.get("label") or provider_id, ptype),
        "type": ptype,
        "key_env": env,
        "key_missing": missing,
        "key_fallback": bool(fallback and missing),
    }


def grouped_for_ui(include_crews: bool = True) -> list[dict]:
    """Provider options grouped for Model dropdowns."""
    buckets = {key: [] for key, _label in _GROUP_ORDER}
    other = []
    for provider_id, cfg in _registry.items():
        if not isinstance(cfg, dict):
            continue
        ptype = cfg.get("type") or "claude_code"
        if ptype == "crew" and not include_crews:
            continue
        option = _provider_option(provider_id, cfg)
        if ptype in buckets:
            buckets[ptype].append(option)
        else:
            other.append(option)
    groups = []
    for key, label in _GROUP_ORDER:
        if key == "crew" and not include_crews:
            continue
        if buckets[key]:
            groups.append({"id": key, "label": label, "options": buckets[key]})
    if other:
        groups.append({"id": "other", "label": "Other", "options": other})
    return groups


def _field_spec(name: str, kind: str, value) -> dict:
    return {
        "name": name,
        "kind": kind,
        "value": value,
        "label": _FIELD_LABELS.get(name, name.replace("_", " ")),
    }


def _field_kind(name: str, value) -> str:
    if name in _BOOL_FIELDS or isinstance(value, bool):
        return "bool"
    if name in _FLOAT_FIELDS or isinstance(value, float):
        return "float"
    if name in _INT_FIELDS or isinstance(value, int):
        return "int"
    return "text"


def _editable_fields(cfg: dict) -> list[dict]:
    ptype = cfg.get("type") or "claude_code"
    if ptype == "crew":
        return [
            _field_spec("label", "text", cfg.get("label") or ""),
            _field_spec("process", "text", cfg.get("process") or "sequential"),
        ]
    fields = [
        _field_spec("label", "text", cfg.get("label") or ""),
        _field_spec("model", "text", cfg.get("model") or ""),
        _field_spec("base_url", "text", cfg.get("base_url") or ""),
        _field_spec("api_key_env", "text", cfg.get("api_key_env") or ""),
    ]
    shown = {field["name"] for field in fields}
    hidden = shown | {"type", "agents", "api_key", "api_key_default"}
    for name, value in cfg.items():
        if name in hidden or isinstance(value, (dict, list)):
            continue
        fields.append(_field_spec(name, _field_kind(name, value), value))
    return fields


def _local_provider_ids() -> set[str]:
    local_doc = _load_local_overlay(providers_local_path())
    providers = local_doc.get("providers") or {}
    return {pid for pid, cfg in providers.items() if isinstance(cfg, dict)}


def provider_catalog() -> list[dict]:
    """Non-secret view of every provider for the Configure AI page."""
    local_ids = _local_provider_ids()
    rows = []
    for provider_id, cfg in _registry.items():
        if not isinstance(cfg, dict):
            continue
        ptype = cfg.get("type") or "claude_code"
        key = provider_key_status(provider_id)
        fallback = str(cfg.get("api_key_default") or "").strip()
        needs_key = ptype in ("openai_compatible", "local_agent")
        agents = cfg.get("agents") or []
        rows.append({
            "id": provider_id,
            "type": ptype,
            "type_label": _TYPE_LABELS.get(ptype, ptype),
            "label": cfg.get("label") or provider_id,
            "model": cfg.get("model") or "",
            "base_url": cfg.get("base_url") or "",
            "api_key_env": cfg.get("api_key_env") or "",
            "process": cfg.get("process") or "",
            "agent_count": len(agents) if isinstance(agents, list) else 0,
            "is_default": provider_id == _default_provider,
            "needs_key": needs_key,
            "key_set": bool(key.get("set")),
            "key_source": key.get("source") or "",
            "has_local_key": bool(key.get("local")),
            "has_key_fallback": bool(fallback),
            "key_fallback_is_public": fallback == _ALLOWED_API_KEY_DEFAULT,
            "editable_type": ptype in _SAFE_PROVIDER_TYPES,
            "local_override": provider_id in local_ids,
            "fields": _editable_fields(cfg),
        })
    return rows


def _assign_field(cfg: dict, key: str, value) -> None:
    if key in _BOOL_FIELDS or isinstance(value, bool):
        if isinstance(value, str):
            cfg[key] = value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            cfg[key] = bool(value)
        return
    if key in _INT_FIELDS:
        text = str(value).strip() if value is not None else ""
        if text == "":
            cfg.pop(key, None)
            return
        try:
            number = int(text)
        except (TypeError, ValueError):
            raise ProviderConfigError(f"{_FIELD_LABELS.get(key, key)} must be a whole number.") from None
        if number < 0:
            raise ProviderConfigError(f"{_FIELD_LABELS.get(key, key)} must be zero or greater.")
        cfg[key] = number
        return
    if key in _FLOAT_FIELDS:
        text = str(value).strip() if value is not None else ""
        if text == "":
            cfg.pop(key, None)
            return
        try:
            cfg[key] = float(text)
        except (TypeError, ValueError):
            raise ProviderConfigError(f"{_FIELD_LABELS.get(key, key)} must be a number.") from None
        return

    text = str(value or "").strip()
    label = _FIELD_LABELS.get(key, key)
    if _looks_like_secret(text):
        raise ProviderConfigError(
            f"{label} looks like an API key. Set the key in the environment named by api_key_env."
        )
    if key == "api_key_env":
        if not text:
            cfg.pop(key, None)
            return
        if not _ENV_NAME_RE.match(text):
            raise ProviderConfigError(
                "API key env must be an environment variable name, not the key itself."
            )
        cfg[key] = text
        return
    if key == "base_url" and text and not (
        text.startswith("http://") or text.startswith("https://")
    ):
        raise ProviderConfigError("Base URL must start with http:// or https://.")
    if key == "label" and not text:
        raise ProviderConfigError("Label is required.")
    if text:
        cfg[key] = text
    else:
        cfg.pop(key, None)


def _apply_changes(current: dict, changes: dict, *, creating: bool = False) -> dict:
    cfg = dict(current or {})
    ptype = "claude_code" if creating else (cfg.get("type") or "claude_code")
    if creating or "type" in changes:
        new_type = str(changes.get("type") or ptype).strip()
        if ptype == "crew" and new_type != "crew":
            raise ProviderConfigError(
                "Crew providers stay type crew. Edit steps from the workspace crew builder."
            )
        if ptype != "crew":
            if new_type not in _SAFE_PROVIDER_TYPES:
                raise ProviderConfigError(f"Unsupported provider type: {new_type}")
            cfg["type"] = new_type
            ptype = new_type
    if ptype == "crew":
        allowed = set(_CREW_TEXT_FIELDS)
    else:
        allowed = set(_LEAF_TEXT_FIELDS) | set(_BOOL_FIELDS) | set(_INT_FIELDS) | set(_FLOAT_FIELDS)
        for key in changes:
            if key in cfg and key not in {"type", "agents", "api_key", "api_key_default"}:
                allowed.add(key)
    for key, value in changes.items():
        if key == "type" or key not in allowed:
            continue
        _assign_field(cfg, key, value)
    if not str(cfg.get("label") or "").strip():
        raise ProviderConfigError("Label is required.")
    if ptype in _ADDABLE_PROVIDER_TYPES:
        if not str(cfg.get("model") or "").strip():
            raise ProviderConfigError("Model is required for this provider type.")
        if not str(cfg.get("base_url") or "").strip():
            raise ProviderConfigError("Base URL is required for this provider type.")
    if ptype == "crew" and not str(cfg.get("process") or "").strip():
        cfg["process"] = "sequential"
    return cfg


def _sanitize_tracked(cfg: dict) -> dict:
    """Drop secrets before writing the tracked providers file."""
    clean = dict(cfg)
    clean.pop("api_key", None)
    default = clean.get("api_key_default")
    if default is not None and str(default).strip() != _ALLOWED_API_KEY_DEFAULT:
        clean.pop("api_key_default", None)
    return clean


def _normalize_submitted_api_key(value) -> str | None:
    """Return a key to store, or None when the stored key should be left alone.

    A missing or blank field means "do not change the stored key." The
    returned string is never logged by this helper.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "\n" in text or "\r" in text or "\x00" in text:
        raise ProviderConfigError("API key must be a single line.")
    if len(text) > 4096:
        raise ProviderConfigError("API key is too long.")
    return text


def _value_contains_secret(value, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and secret in key) or _value_contains_secret(item, secret)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_value_contains_secret(item, secret) for item in value)
    return False


def _refuse_secret_in_tracked(doc: dict, secret: str | None) -> None:
    """Abort before a real key can be written into the tracked providers file."""
    if not secret or secret == _ALLOWED_API_KEY_DEFAULT:
        return
    if _value_contains_secret(doc, secret):
        raise ProviderConfigError(
            "Refusing to save because that API key would be written to providers.yaml. "
            "Keys are stored only in providers.local.yaml."
        )


def _write_local_api_key(provider_id: str, api_key: str) -> None:
    """Persist api_key_default in the gitignored local overlay only."""
    path = providers_local_path()
    if not _local_file_is_distinct(path):
        raise ProviderConfigError(
            "Refusing to store an API key because the local providers file "
            "is the same as the tracked providers file."
        )
    if os.path.exists(path):
        doc = _read_yaml_doc(path)
    else:
        doc = {"providers": {}}
    providers_map = doc.setdefault("providers", {})
    current = providers_map.get(provider_id)
    if current is None:
        updated = {}
    elif isinstance(current, dict):
        updated = dict(current)
    else:
        raise ProviderConfigError(
            f"{os.path.basename(path)} entry for {provider_id} must be a mapping."
        )
    updated.pop("api_key", None)
    updated["api_key_default"] = api_key
    providers_map[provider_id] = updated
    _write_yaml_doc(path, doc)


def _tracked_doc() -> tuple[str, dict]:
    path = config.PROVIDERS_FILE
    if os.path.exists(path):
        return path, _read_yaml_doc(path)
    return path, {"default_provider": _file_default_provider, "providers": {}}


def _local_doc_if_present() -> tuple[str, dict | None]:
    path = providers_local_path()
    if not _local_file_is_distinct(path) or not os.path.exists(path):
        return path, None
    return path, _read_yaml_doc(path)


def update_provider(provider_id: str, changes: dict) -> None:
    """Update provider fields, store a filled API key locally, and reload.

    A blank or omitted api_key leaves any key already in providers.local.yaml
    untouched. OpenAI-compatible and Tank-controlled providers read that key
    from the reloaded registry, so this process does not need a restart.
    """
    if provider_id not in _registry:
        raise ProviderConfigError(f"Unknown provider: {provider_id}")
    submitted = dict(changes)
    new_key = _normalize_submitted_api_key(submitted.pop("api_key", None))
    submitted.pop("api_key_default", None)
    current_type = (_registry.get(provider_id) or {}).get("type") or "claude_code"
    effective_type = str(submitted.get("type") or current_type).strip() or current_type
    if new_key and effective_type not in ("openai_compatible", "local_agent"):
        raise ProviderConfigError("This provider type does not use an API key.")

    tracked_path, tracked_doc = _tracked_doc()
    local_path, local_doc = _local_doc_if_present()
    tracked_providers = tracked_doc.setdefault("providers", {})
    local_providers = (local_doc or {}).get("providers") or {}
    in_tracked = isinstance(tracked_providers.get(provider_id), dict)
    in_local = isinstance(local_providers.get(provider_id), dict)
    if not in_tracked and not in_local:
        raise ProviderConfigError(f"Unknown provider: {provider_id}")
    tracked_updated = None
    local_updated = None
    if in_tracked:
        tracked_updated = _sanitize_tracked(
            _apply_changes(tracked_providers[provider_id], submitted)
        )
    if in_local:
        local_updated = _apply_changes(local_providers[provider_id], submitted)
    if tracked_updated is not None:
        tracked_providers[provider_id] = tracked_updated
        _refuse_secret_in_tracked(tracked_doc, new_key)
        _write_yaml_doc(tracked_path, tracked_doc)
    if local_updated is not None and local_doc is not None:
        local_providers[provider_id] = local_updated
        local_doc["providers"] = local_providers
        _write_yaml_doc(local_path, local_doc)
    if new_key:
        _write_local_api_key(provider_id, new_key)
    load_providers()


def add_provider(
    provider_id: str,
    label: str,
    provider_type: str,
    model: str,
    base_url: str,
    api_key_env: str,
    api_key: str = "",
) -> None:
    """Add an OpenAI-compatible or local_agent provider to the tracked file.

    A filled api_key is written only to providers.local.yaml.
    """
    provider_id = (provider_id or "").strip()
    if not _PROVIDER_ID_RE.match(provider_id):
        raise ProviderConfigError(
            "Id must be a lowercase slug: start with a letter, then letters, numbers, or underscores."
        )
    if provider_id in _registry:
        raise ProviderConfigError(f"Provider {provider_id} already exists.")
    provider_type = (provider_type or "").strip()
    if provider_type not in _ADDABLE_PROVIDER_TYPES:
        raise ProviderConfigError("New providers must be openai_compatible or local_agent.")
    new_key = _normalize_submitted_api_key(api_key)
    cfg = _sanitize_tracked(_apply_changes({}, {
        "type": provider_type,
        "label": label,
        "model": model,
        "base_url": base_url,
        "api_key_env": api_key_env,
    }, creating=True))
    path, doc = _tracked_doc()
    doc.setdefault("providers", {})[provider_id] = cfg
    if not doc.get("default_provider"):
        doc["default_provider"] = _file_default_provider or provider_id
    _refuse_secret_in_tracked(doc, new_key)
    _write_yaml_doc(path, doc)
    if new_key:
        _write_local_api_key(provider_id, new_key)
    load_providers()


def set_default_provider(provider_id: str) -> None:
    """Write default_provider into the tracked file and reload."""
    provider_id = (provider_id or "").strip()
    if provider_id not in _registry:
        raise ProviderConfigError(f"Unknown provider: {provider_id}")
    path, doc = _tracked_doc()
    doc["default_provider"] = provider_id
    _write_yaml_doc(path, doc)
    local_path, local_doc = _local_doc_if_present()
    if local_doc is not None and local_doc.get("default_provider"):
        local_doc["default_provider"] = provider_id
        _write_yaml_doc(local_path, local_doc)
    load_providers()


def format_probe_result(result: dict) -> tuple[bool, str]:
    """Turn a probe dict into a short pass/fail sentence."""
    ok = bool(result.get("ok"))
    detail = str(result.get("detail") or "").strip().replace("\n", " ")
    if len(detail) > 240:
        detail = detail[:237] + "..."
    if ok:
        status = result.get("status")
        if status:
            return True, f"Pass — endpoint responded (HTTP {status})."
        if detail:
            return True, f"Pass — {detail} is available."
        return True, "Pass."
    if " is not set" in detail:
        env = detail.split(" is not set", 1)[0].strip()
        return False, f"Fail — {env} is not set."
    status = result.get("status")
    if status and detail:
        return False, f"Fail — HTTP {status}: {detail}"
    if detail:
        return False, f"Fail — {detail}"
    return False, "Fail — probe did not succeed."


def probe_provider(provider_id: str) -> dict:
    """Lightweight auth check against the provider API."""
    cfg = _provider_cfg(provider_id)
    ptype = cfg.get("type", "claude_code")
    if ptype == "crew":
        return {
            "ok": False,
            "detail": "Crews do not have their own endpoint. Test a leaf provider used by a step.",
        }
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
            return _scrub_secret({"ok": True, "status": resp.status}, key)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return _scrub_secret({"ok": False, "status": exc.code, "detail": detail}, key)
    except urllib.error.URLError as exc:
        return _scrub_secret({"ok": False, "detail": str(exc.reason)}, key)


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


def list_crew_providers() -> list[dict]:
    """Return configured crew providers. This only reads the registry."""
    crews = []
    for pid, cfg in _registry.items():
        if (cfg or {}).get("type") != "crew":
            continue
        agents = []
        for agent in cfg.get("agents") or []:
            if isinstance(agent, dict):
                agents.append(dict(agent))
        crews.append({
            "id": pid,
            "label": cfg.get("label") or pid,
            "process": cfg.get("process") or "sequential",
            "agents": agents,
        })
    return crews


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


def _scrub_secret(result: dict, secret: str) -> dict:
    """Remove a resolved API key from a probe payload before it is shown or logged."""
    if not secret or secret == _ALLOWED_API_KEY_DEFAULT:
        return result
    cleaned = dict(result)
    detail = cleaned.get("detail")
    if isinstance(detail, str) and secret in detail:
        cleaned["detail"] = detail.replace(secret, "[redacted]")
    return cleaned


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
            f"{env_name} is not set. Save a key on Configure AI, or set "
            f"{env_name} in the environment before starting Tank."
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
            mode = local_agent.verification_mode(ctx.stage_name, ctx.role)
            code = 0
            ran = False
            if post_actions.get("run_tests") or post_actions.get("run_git_diff"):
                auth = local_agent._authorized_tools(ctx.role)
                code = local_agent.run_post_actions(
                    ctx.workspace["repo_path"],
                    post_actions,
                    ctx.log_path,
                    workspace=ctx.workspace,
                    authorized_tools=auth,
                )
                ran = True
                payload["tool_exit_code"] = code
                local_agent.log_run_outcome(ctx.log_path, code)
            current = models.get_run(ctx.run_id)
            if current and current["status"] == "cancelled":
                on_finished(1)
                return
            command = (post_actions.get("test_command") or "").strip()
            has_command = (
                (bool(post_actions.get("run_tests")) and bool(command))
                or bool(post_actions.get("run_git_diff"))
            )
            if mode and not (ran and has_command and code == 0):
                error = (
                    f"{mode} cannot complete on a model summary. "
                    "A real test, build, or diff command must run and exit 0."
                )
                if ran and code != 0:
                    error = f"Verification command exited {code}"
                models.update_run(
                    ctx.run_id,
                    agent_payload=json.dumps(payload),
                    status="failed",
                    error=error,
                )
                on_finished(1)
                return
            models.update_run(
                ctx.run_id,
                agent_payload=json.dumps(payload),
                status="done" if code == 0 else "failed",
                error=None if code == 0 else f"Verification command exited {code}",
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
