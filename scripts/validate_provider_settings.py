#!/usr/bin/env python3
"""Configure AI: non-secret saves, local overlay, grouped pickers, probe text.

Does not call a model. Uses a copy of providers.yaml so the tracked file
in the repo is left untouched.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="tank-providers-"))
REPO = TMP / "repo"
REPO.mkdir()
(REPO / "README.md").write_text("provider settings validation\n", encoding="utf-8")
WORKSPACES = TMP / "workspaces.yaml"
WORKSPACES.write_text("workspaces: []\n", encoding="utf-8")
PROVIDERS = TMP / "providers.yaml"
LOCAL = TMP / "providers.local.yaml"
shutil.copyfile(ROOT / "providers.yaml", PROVIDERS)

REAL_PROVIDERS = ROOT / "providers.yaml"
REAL_HASH = hashlib.sha256(REAL_PROVIDERS.read_bytes()).hexdigest()

os.environ["TANK_DATA_DIR"] = str(TMP / "data")
os.environ["TANK_WORKSPACES_FILE"] = str(WORKSPACES)
os.environ["TANK_PROVIDERS_FILE"] = str(PROVIDERS)
os.environ["TANK_PROVIDERS_LOCAL_FILE"] = str(LOCAL)
os.environ["TANK_MISSION_TEMPLATES_FILE"] = str(ROOT / "mission_templates.yaml")
os.environ.pop("TANK_DEFAULT_PROVIDER", None)
os.environ.pop("TANK_TEST_MISSING_KEY", None)

import yaml  # noqa: E402
from werkzeug.datastructures import MultiDict  # noqa: E402

import config  # noqa: E402
import providers  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok: {label}")
        return
    FAILURES.append(label)
    print(f"FAIL: {label}")


def tracked() -> dict:
    return yaml.safe_load(PROVIDERS.read_text(encoding="utf-8")) or {}


def form_for(provider_id: str, **overrides) -> dict:
    row = next(item for item in providers.provider_catalog() if item["id"] == provider_id)
    changes = {}
    for field in row["fields"]:
        value = overrides[field["name"]] if field["name"] in overrides else field["value"]
        if field["kind"] == "bool":
            changes[field["name"]] = bool(value)
        else:
            changes[field["name"]] = "" if value is None else value
    changes["type"] = overrides.get("type", row["type"])
    return changes


def main() -> None:
    providers.load_providers()
    original = yaml.safe_load((ROOT / "providers.yaml").read_text(encoding="utf-8"))
    original_agents = original["providers"]["crew_powershell_expert"]["agents"]

    groups = providers.grouped_for_ui(include_crews=True)
    labels = [group["label"] for group in groups]
    check(labels[:4] == [
        "Tank-controlled agents",
        "Advisory / chat-only",
        "Claude Code",
        "Crews",
    ], "dropdown groups distinguish agent, advisory, Claude Code, and crews")
    leaf_ids = [
        opt["id"]
        for group in providers.grouped_for_ui(include_crews=False)
        for opt in group["options"]
    ]
    check("mistral_agent" in leaf_ids, "leaf picker includes mistral_agent")
    check("crew_powershell_expert" not in leaf_ids, "leaf picker omits crew parents")
    check(all(group["id"] != "crew" for group in providers.grouped_for_ui(False)), "leaf groups have no crew bucket")

    catalog = {row["id"]: row for row in providers.provider_catalog()}
    check(catalog["mistral_agent"]["is_default"], "catalog marks the file default")
    check(catalog["mistral_agent"]["model"] == "codestral-latest", "catalog shows the model")
    check(catalog["mistral_agent"]["base_url"] == "https://api.mistral.ai/v1", "catalog shows the base URL")
    check(catalog["openrouter"]["api_key_env"] == "OPENROUTER_API_KEY", "catalog shows the env var name")
    field_names = {field["name"] for field in catalog["openrouter"]["fields"]}
    check("api_key" not in field_names and "api_key_default" not in field_names, "catalog fields are non-secret")

    providers.update_provider("mistral_agent", form_for("mistral_agent", model="codestral-latest-test"))
    saved = tracked()
    check(saved["providers"]["mistral_agent"]["model"] == "codestral-latest-test", "save writes the model")
    check("api_key" not in saved["providers"]["mistral_agent"], "save does not write api_key")
    check(
        saved["providers"]["crew_powershell_expert"]["agents"] == original_agents,
        "editing one provider keeps crew agents",
    )
    check(
        saved["providers"]["lmstudio_qwen"].get("api_key_default") == "lm-studio",
        "lm-studio default stays in the tracked file",
    )

    try:
        providers.update_provider("mistral_agent", form_for(
            "mistral_agent", api_key_env="sk-or-secret-value"
        ))
        check(False, "rejects an API key pasted into api_key_env")
    except providers.ProviderConfigError:
        check(True, "rejects an API key pasted into api_key_env")
    check("sk-or-secret-value" not in PROVIDERS.read_text(encoding="utf-8"), "rejected key is not in providers.yaml")

    try:
        providers.add_provider("Crew", "Nope", "crew", "m", "http://127.0.0.1:9/v1", "OPENROUTER_API_KEY")
        check(False, "rejects a new crew from the add form")
    except providers.ProviderConfigError:
        check(True, "rejects a new crew from the add form")

    providers.add_provider(
        "probe_local",
        "Probe local",
        "openai_compatible",
        "test-model",
        "http://127.0.0.1:9/v1",
        "TANK_TEST_MISSING_KEY",
    )
    added = tracked()["providers"]["probe_local"]
    check(added["type"] == "openai_compatible", "add writes openai_compatible")
    check("api_key" not in added and "api_key_default" not in added, "add writes no secret fields")
    ok, message = providers.format_probe_result(providers.probe_provider("probe_local"))
    check((not ok) and message.startswith("Fail") and "TANK_TEST_MISSING_KEY" in message, "probe reports a missing key")

    providers.set_default_provider("probe_local")
    check(tracked()["default_provider"] == "probe_local", "set default writes providers.yaml")
    check(providers.get_default_provider() == "probe_local", "set default reloads the registry")

    os.environ["TANK_DEFAULT_PROVIDER"] = "claude_code"
    providers.load_providers()
    info = providers.default_provider_info()
    check(info["env_override"] and info["effective"] == "claude_code", "env default overrides the file")
    check(info["file"] == "probe_local", "file default is still reported")
    os.environ.pop("TANK_DEFAULT_PROVIDER", None)
    providers.load_providers()

    LOCAL.write_text(yaml.safe_dump({
        "providers": {
            "probe_local": {
                "model": "from-local",
                "api_key_default": "sk-local-secret-value",
            }
        }
    }), encoding="utf-8")
    providers.load_providers()
    merged = {row["id"]: row for row in providers.provider_catalog()}["probe_local"]
    check(merged["model"] == "from-local", "local overlay wins for model")
    check(merged["has_key_fallback"] and not merged["key_fallback_is_public"], "local key default is hidden")
    check("sk-local-secret-value" not in repr(merged), "catalog does not include the local secret")
    providers.update_provider("probe_local", form_for("probe_local", model="after-save"))
    check(tracked()["providers"]["probe_local"]["model"] == "after-save", "save updates the tracked model")
    check("sk-local-secret-value" not in PROVIDERS.read_text(encoding="utf-8"), "local secret stays out of tracked yaml")
    local_doc = yaml.safe_load(LOCAL.read_text(encoding="utf-8"))
    check(local_doc["providers"]["probe_local"]["api_key_default"] == "sk-local-secret-value", "local secret is preserved")
    check(local_doc["providers"]["probe_local"]["model"] == "after-save", "save updates the local overlay model")

    ok, message = providers.format_probe_result(providers.probe_provider("crew_powershell_expert"))
    check((not ok) and "leaf provider" in message, "probe explains crew parents")

    bad_dir = TMP / "bad"
    bad_dir.mkdir()
    bad = bad_dir / "providers.yaml"
    bad.write_text("providers:\n  leaked:\n    api_key_default: sk-real-key\n", encoding="utf-8")
    config.PROVIDERS_FILE = str(bad)
    os.environ["TANK_PROVIDERS_LOCAL_FILE"] = str(TMP / "missing-local.yaml")
    try:
        providers.load_providers()
        check(False, "tracked api_key_default other than lm-studio is rejected")
    except RuntimeError as exc:
        check("providers.local.yaml" in str(exc), "tracked key error points at the local file")
    finally:
        config.PROVIDERS_FILE = str(PROVIDERS)
        os.environ["TANK_PROVIDERS_LOCAL_FILE"] = str(LOCAL)
        providers.load_providers()

    import models  # noqa: E402
    import app as tank_app  # noqa: E402

    client = tank_app.app.test_client()
    page = client.get("/config/ai")
    body = page.get_data(as_text=True)
    check(page.status_code == 200, "Configure AI page returns 200")
    check("Configure AI" in body and "How to set an API key" in body, "page documents env keys")
    check("providers.local.yaml" in body, "page documents the local override")
    check("sk-local-secret-value" not in body, "page does not render the local secret")
    check("after-save" in body and "missing" in body, "page shows the model and key status")

    home = client.get("/").get_data(as_text=True)
    check("Configure AI" in home, "nav links Configure AI")
    check('label="Crews"' not in home, "workspace create picker omits crew parents")
    check('label="Tank-controlled agents"' in home, "workspace create picker groups agents")

    schedules = client.get("/schedules").get_data(as_text=True)
    check('label="Crews"' in schedules, "schedule picker keeps crews in their own group")
    check('label="Advisory / chat-only"' in schedules, "schedule picker groups advisory providers")

    slug = models.create_workspace("Picker", str(REPO), slug="picker", default_provider="local_qwen")
    workspace = client.get(f"/workspaces/{slug}").get_data(as_text=True)
    check(workspace.count('label="Crews"') == 1, "launch form includes crews; mission and Mad Scientist do not")
    check('id="mission_provider"' in workspace and 'id="mad_provider"' in workspace, "mission and Mad Scientist pickers render")

    ws = models.get_workspace(slug)
    step = client.get(f"/partials/crew-step-row?workspace_id={ws['id']}").get_data(as_text=True)
    check('name="agent_providers[]"' in step, "crew step still posts a provider")
    check('label="Crews"' not in step, "crew step picker is leaf providers only")
    check('data-key-missing="1"' in step or 'data-key-missing="0"' in step, "crew step options carry key status")

    probe = client.post("/config/ai/providers/probe_local/probe")
    probe_body = probe.get_data(as_text=True)
    check(probe.status_code == 200 and "probe-fail" in probe_body, "probe returns a failure fragment")
    check("Fail" in probe_body and "sk-local-secret-value" not in probe_body, "probe fragment does not include the local secret")

    saved_page = client.post(
        "/config/ai/providers/probe_local",
        data=MultiDict([
            ("shown", "label"),
            ("shown", "model"),
            ("shown", "base_url"),
            ("shown", "api_key_env"),
            ("label", "Probe local"),
            ("model", "saved-from-form"),
            ("base_url", "http://127.0.0.1:9/v1"),
            ("api_key_env", "TANK_TEST_MISSING_KEY"),
            ("type", "openai_compatible"),
        ]),
        follow_redirects=True,
    )
    check(saved_page.status_code == 200, "save redirects back to Configure AI")
    check("saved-from-form" in saved_page.get_data(as_text=True), "saved model shows up after reload")
    check(providers.get_provider_type("probe_local") == "openai_compatible", "reload_config kept the new provider")

    check(
        hashlib.sha256(REAL_PROVIDERS.read_bytes()).hexdigest() == REAL_HASH,
        "repo providers.yaml was not modified",
    )

    if FAILURES:
        print(f"\n{len(FAILURES)} failed")
        raise SystemExit(1)
    print("\nall provider settings checks passed")


if __name__ == "__main__":
    main()
