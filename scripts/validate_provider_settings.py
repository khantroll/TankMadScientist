#!/usr/bin/env python3
"""Configure AI: non-secret saves, local API keys, grouped pickers, probe text.

Does not call a model. Uses a copy of providers.yaml so the tracked file
in the repo is left untouched. Fake keys in this script are not written
to the repo's providers.yaml or providers.local.yaml.
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
for _env_name in (
    "TANK_DEFAULT_PROVIDER",
    "TANK_TEST_MISSING_KEY",
    "TANK_LMSTUDIO_API_KEY",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "TANK_ADDED_KEY_ENV",
    "TANK_HTTP_ADDED_KEY",
):
    os.environ.pop(_env_name, None)

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
    check(merged["key_set"] and merged["key_source"] == "stored", "local key counts as set on the card")
    check(merged["has_local_key"], "catalog records a local key without the value")
    check("sk-local-secret-value" not in repr(merged), "catalog does not include the local secret")
    lm_status = providers.provider_key_status("lmstudio_qwen")
    check(
        (not lm_status["set"]) and lm_status["source"] != "stored",
        "tracked lm-studio dummy does not count as a saved key",
    )
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
    check("stored locally" in body, "page treats a local key as set")
    check('type="password"' in body and 'name="api_key"' in body, "page has a password field for the key")
    check('value="sk-local-secret-value"' not in body, "password field is not prefilled with the stored key")

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
        yaml.safe_load(LOCAL.read_text(encoding="utf-8"))["providers"]["probe_local"]["api_key_default"]
        == "sk-local-secret-value",
        "form save that omits the key field leaves the stored key",
    )

    doc = yaml.safe_load(LOCAL.read_text(encoding="utf-8"))
    doc["providers"]["probe_local"].pop("api_key_default", None)
    LOCAL.write_text(yaml.safe_dump(doc), encoding="utf-8")
    providers.load_providers()
    ok, message = providers.format_probe_result(providers.probe_provider("probe_local"))
    check((not ok) and "is not set" in message, "probe reports a missing key before Configure AI saves one")

    entered = "sk-configure-ai-test-key"
    keyed = client.post(
        "/config/ai/providers/probe_local",
        data=MultiDict([
            ("shown", "label"),
            ("shown", "model"),
            ("shown", "base_url"),
            ("shown", "api_key_env"),
            ("label", "Probe local"),
            ("model", "keyed-model"),
            ("base_url", "http://127.0.0.1:9/v1"),
            ("api_key_env", "TANK_TEST_MISSING_KEY"),
            ("type", "openai_compatible"),
            ("api_key", entered),
        ]),
        follow_redirects=True,
    )
    keyed_body = keyed.get_data(as_text=True)
    check(keyed.status_code == 200, "key save redirects back to Configure AI")
    check(entered not in keyed_body, "Configure AI does not echo the saved key")
    check("stored only in providers.local.yaml" in keyed_body, "save notice says the key is stored locally")
    check("without an environment variable or a restart" in keyed_body, "save notice says reload is enough")
    check(entered not in PROVIDERS.read_text(encoding="utf-8"), "tracked providers.yaml does not contain the Configure AI key")
    local_doc = yaml.safe_load(LOCAL.read_text(encoding="utf-8"))
    check(
        local_doc["providers"]["probe_local"]["api_key_default"] == entered,
        "Configure AI key is written to providers.local.yaml",
    )
    check(
        tracked()["providers"]["probe_local"].get("api_key_default") in (None, "lm-studio"),
        "tracked probe provider has no saved secret",
    )
    status = providers.provider_key_status("probe_local")
    check(status["set"] and status["source"] == "stored", "reloaded status treats the local key as set")
    ok, message = providers.format_probe_result(providers.probe_provider("probe_local"))
    check("is not set" not in message, "reload makes Test stop saying the key is not set")
    check(entered not in message, "Test text does not include the key")
    probe = client.post("/config/ai/providers/probe_local/probe")
    probe_body = probe.get_data(as_text=True)
    check(
        probe.status_code == 200 and "is not set" not in probe_body and entered not in probe_body,
        "Test fragment uses the reloaded key without echoing it",
    )

    blank = client.post(
        "/config/ai/providers/probe_local",
        data=MultiDict([
            ("shown", "label"),
            ("shown", "model"),
            ("shown", "base_url"),
            ("shown", "api_key_env"),
            ("label", "Probe local"),
            ("model", "blank-kept-key"),
            ("base_url", "http://127.0.0.1:9/v1"),
            ("api_key_env", "TANK_TEST_MISSING_KEY"),
            ("type", "openai_compatible"),
            ("api_key", ""),
        ]),
        follow_redirects=True,
    )
    blank_body = blank.get_data(as_text=True)
    check(blank.status_code == 200 and entered not in blank_body, "blank key save does not echo the stored key")
    local_doc = yaml.safe_load(LOCAL.read_text(encoding="utf-8"))
    check(
        local_doc["providers"]["probe_local"]["api_key_default"] == entered,
        "blank key field does not erase the stored key",
    )
    check(
        local_doc["providers"]["probe_local"]["model"] == "blank-kept-key",
        "blank key save still updates other local fields",
    )
    check(entered not in PROVIDERS.read_text(encoding="utf-8"), "blank key save does not copy the key into tracked yaml")
    check(
        tracked()["providers"]["probe_local"]["model"] == "blank-kept-key",
        "blank key save still writes the non-secret edit to tracked yaml",
    )

    os.environ["TANK_TEST_MISSING_KEY"] = "sk-env-wins-test-key"
    providers.load_providers()
    status = providers.provider_key_status("probe_local")
    check(status["set"] and status["source"] == "env" and status["local"], "an env var that is set wins over the stored key")
    resolved = providers._openai_api_key(providers._provider_cfg("probe_local"))
    check(resolved == "sk-env-wins-test-key", "runtime resolution uses the env var when it is set")
    import local_agent
    resolved_agent = local_agent._openai_api_key(providers._provider_cfg("probe_local"))
    check(resolved_agent == "sk-env-wins-test-key", "Tank-controlled runs also prefer the env var")
    os.environ.pop("TANK_TEST_MISSING_KEY", None)
    providers.load_providers()
    resolved = providers._openai_api_key(providers._provider_cfg("probe_local"))
    check(resolved == entered, "runtime resolution falls back to the local key after reload")

    other = "sk-openrouter-configure-ai-test"
    changes = form_for("openrouter")
    changes["api_key"] = other
    changes["model"] = "openrouter-model-after-key"
    providers.update_provider("openrouter", changes)
    check(other not in PROVIDERS.read_text(encoding="utf-8"), "key for an existing provider stays out of providers.yaml")
    check(
        tracked()["providers"]["openrouter"]["model"] == "openrouter-model-after-key",
        "non-secret edit still lands in the tracked file",
    )
    local_doc = yaml.safe_load(LOCAL.read_text(encoding="utf-8"))
    check(local_doc["providers"]["openrouter"]["api_key_default"] == other, "new local entry holds the key")
    check("model" not in local_doc["providers"]["openrouter"], "a first key save does not copy other fields into the local entry")
    changes = form_for("openrouter")
    changes["api_key"] = "   "
    changes["model"] = "openrouter-model-blank"
    providers.update_provider("openrouter", changes)
    local_doc = yaml.safe_load(LOCAL.read_text(encoding="utf-8"))
    check(local_doc["providers"]["openrouter"]["api_key_default"] == other, "whitespace key field leaves the stored key")
    check(other not in PROVIDERS.read_text(encoding="utf-8"), "later field save still leaves the key out of tracked yaml")

    lm_secret = "sk-lmstudio-override-test"
    changes = form_for("lmstudio_qwen")
    changes["api_key"] = lm_secret
    providers.update_provider("lmstudio_qwen", changes)
    check(
        tracked()["providers"]["lmstudio_qwen"].get("api_key_default") == "lm-studio",
        "tracked lm-studio dummy stays in providers.yaml",
    )
    check(lm_secret not in PROVIDERS.read_text(encoding="utf-8"), "lmstudio override key is not in the tracked file")
    local_doc = yaml.safe_load(LOCAL.read_text(encoding="utf-8"))
    check(
        local_doc["providers"]["lmstudio_qwen"]["api_key_default"] == lm_secret,
        "lmstudio override key is stored only in the local file",
    )

    try:
        providers.update_provider("crew_powershell_expert", {
            "label": "Powershell_expert",
            "process": "sequential",
            "api_key": "sk-crew-should-not-save",
        })
        check(False, "crew save rejects an API key")
    except providers.ProviderConfigError:
        check(True, "crew save rejects an API key")
    check(
        "sk-crew-should-not-save" not in LOCAL.read_text(encoding="utf-8")
        and "sk-crew-should-not-save" not in PROVIDERS.read_text(encoding="utf-8"),
        "rejected crew key is not written",
    )

    added_secret = "sk-added-provider-test-key"
    providers.add_provider(
        "added_with_key",
        "Added with key",
        "local_agent",
        "added-model",
        "http://127.0.0.1:9/v1",
        "TANK_ADDED_KEY_ENV",
        api_key=added_secret,
    )
    check(added_secret not in PROVIDERS.read_text(encoding="utf-8"), "add form key is not written to tracked providers.yaml")
    check("api_key_default" not in tracked()["providers"]["added_with_key"], "added provider tracked entry has no api_key_default")
    local_doc = yaml.safe_load(LOCAL.read_text(encoding="utf-8"))
    check(local_doc["providers"]["added_with_key"]["api_key_default"] == added_secret, "add form key is in the local file")
    check(providers.provider_key_status("added_with_key")["source"] == "stored", "added provider key counts as set")

    http_secret = "sk-http-added-test-key"
    added_page = client.post(
        "/config/ai/providers",
        data={
            "provider_id": "http_added_key",
            "label": "HTTP added",
            "type": "openai_compatible",
            "model": "http-model",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key_env": "TANK_HTTP_ADDED_KEY",
            "api_key": http_secret,
        },
        follow_redirects=True,
    )
    added_body = added_page.get_data(as_text=True)
    check(added_page.status_code == 200 and http_secret not in added_body, "add page does not echo the key")
    check(http_secret not in PROVIDERS.read_text(encoding="utf-8"), "HTTP add leaves the key out of tracked providers.yaml")
    local_doc = yaml.safe_load(LOCAL.read_text(encoding="utf-8"))
    check(local_doc["providers"]["http_added_key"]["api_key_default"] == http_secret, "HTTP add writes the key to the local file")

    page = client.get("/config/ai").get_data(as_text=True)
    saved_secrets = (entered, other, lm_secret, added_secret, http_secret, "sk-env-wins-test-key")
    check(
        all(secret not in page for secret in saved_secrets),
        "Configure AI HTML does not contain a saved key",
    )
    check('type="password"' in page, "reloaded page still offers the password field")
    check(LOCAL.stat().st_mode & 0o077 == 0, "local providers file is not group or world readable")

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
