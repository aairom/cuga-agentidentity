"""Unit tests for the LLM engine abstraction (all HTTP is mocked: no network, no real keys)."""

from __future__ import annotations

import json
import os

import pytest
from pydantic import SecretStr

from agent_identity import llm_engines as le
from agent_identity.config import Settings
from agent_identity.errors import EngineConnectionError, EngineMisconfigured, LLMEngineError
from agent_identity.llm_engines import (
    ENGINES, CloudEngine, EngineConfig, EngineManager, LlamaCppEngine, OllamaEngine, config_from_form, get_engine,
    is_local_url, openai_url, root_url, same_model)

OLLAMA_TAGS = {"models": [{"name": "granite4.2:latest"}, {"name": "llama3.2:latest"}]}


def settings(tmp_path, **kw) -> Settings:
    base = dict(state_dir=str(tmp_path / "state"), output_dir=str(tmp_path), llm_mode="ollama")
    base.update(kw)
    return Settings(_env_file=None, **base)


class FakeHttp:
    """Replacement for ``llm_engines._http_get``: maps URL substrings to (status, json) or an exception."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes, self.calls = routes, []

    def __call__(self, url, headers=None, timeout=5.0):
        self.calls.append((url, headers))
        for frag, out in self.routes.items():
            if frag in url:
                if isinstance(out, Exception):
                    raise out
                return out
        raise EngineConnectionError(f"Cannot connect to {url}", "start it")


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Legacy env vars are remembered process-wide; reset that memory and the vars for every test."""
    monkeypatch.setattr(le, "_ORIGINAL_ENV", {})
    for k in ("OPENAI_BASE_URL", "MODEL_NAME"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture()
def http(monkeypatch):
    def install(routes):
        fake = FakeHttp(routes)
        monkeypatch.setattr(le, "_http_get", fake)
        monkeypatch.setattr(le, "probe_ollama",
                            lambda url, timeout=1.0: (True, [m["name"] for m in OLLAMA_TAGS["models"]]) if "11434" in url else (False, []))
        return fake
    return install


# ---- helpers ---------------------------------------------------------------------------
def test_url_helpers():
    assert root_url(" http://h:1/v1/ ") == "http://h:1"
    assert openai_url("http://h:1") == "http://h:1/v1"
    assert is_local_url("http://localhost:8080") and not is_local_url("https://api.openai.com/v1")
    assert same_model("granite4.2", "granite4.2:latest") and not same_model("a", "b")


def test_unknown_engine_error_has_remediation():
    with pytest.raises(EngineMisconfigured) as e:
        get_engine("nope")
    assert "ollama" in e.value.remediation and "Hint" in str(e.value)


# ---- Ollama ----------------------------------------------------------------------------
def test_ollama_ok_and_model_missing(http):
    http({"/api/tags": (200, OLLAMA_TAGS)})
    eng = OllamaEngine()
    ok = eng.test_connection(EngineConfig("ollama", "http://localhost:11434/v1", "granite4.2"))
    assert ok.ok and "granite4.2:latest" in ok.models
    bad = eng.test_connection(EngineConfig("ollama", "http://localhost:11434", "mistral"))
    assert not bad.ok and "ollama pull mistral" in bad.remediation


def test_ollama_wrong_service_and_down(http):
    http({"/api/tags": (200, "not-a-dict")})
    r = OllamaEngine().test_connection(EngineConfig("ollama", "http://localhost:1", "m"))
    assert not r.ok and "not an Ollama API" in r.message
    http({})
    r = OllamaEngine().test_connection(EngineConfig("ollama", "http://localhost:1", "m"))
    assert not r.ok and r.remediation


def test_ollama_validation():
    r = OllamaEngine().test_connection(EngineConfig("ollama", "localhost:11434", "m"))
    assert not r.ok and "valid http(s) URL" in r.message
    r = OllamaEngine().test_connection(EngineConfig("ollama", "", "m"))
    assert not r.ok and "required" in r.message


# ---- llama.cpp -------------------------------------------------------------------------
def test_llamacpp_models_and_health_fallback(http):
    http({"/v1/models": (200, {"data": [{"id": "qwen.gguf"}]})})
    r = LlamaCppEngine().test_connection(EngineConfig("llamacpp", "http://localhost:8080", ""))
    assert r.ok and r.resolved_model == "qwen.gguf"
    http({"/v1/models": (404, None), "/health": (200, {"status": "ok"})})
    r = LlamaCppEngine().test_connection(EngineConfig("llamacpp", "http://localhost:8080", "mine"))
    assert r.ok and r.resolved_model == "mine"


def test_llamacpp_loading_down_and_wrong_port(http):
    http({"/v1/models": (503, None)})
    r = LlamaCppEngine().test_connection(EngineConfig("llamacpp", "http://localhost:8080", ""))
    assert not r.ok and "loading" in r.message
    http({})
    r = LlamaCppEngine().test_connection(EngineConfig("llamacpp", "http://localhost:8080", ""))
    assert not r.ok and "llama-server" in r.remediation
    http({"/v1/models": (500, None)})
    r = LlamaCppEngine().test_connection(EngineConfig("llamacpp", "http://localhost:8080", ""))
    assert not r.ok and "HTTP 500" in r.message


def test_llamacpp_detection_probes_both_ports(http, tmp_path):
    fake = http({"9931/v1/models": (200, {"data": [{"id": "m"}]})})
    d = LlamaCppEngine().detect(settings(tmp_path))
    assert d.running and d.endpoint == "http://localhost:9931" and d.models == ["m"]
    assert any("8080" in c[0] for c in fake.calls)  # tried 8080 first
    http({})
    d = LlamaCppEngine().detect(settings(tmp_path))
    assert not d.running and d.status == "not detected" and "8080" in " ".join(d.notes)


def test_llamacpp_binary_detection(tmp_path, http):
    http({})
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    d = LlamaCppEngine().detect(settings(tmp_path, llamacpp_binary=str(binary)))
    assert d.installed and not d.running and d.status == "installed, not running"


# ---- Cloud -----------------------------------------------------------------------------
def cloud(url="https://api.example.com/v1", model="m1", key="sk-secret"):  # pragma: allowlist secret
    return EngineConfig("cloud", url, model, SecretStr(key))


def test_cloud_requires_https_key_and_model():
    for cfg, text in [(cloud(url="http://api.example.com/v1"), "plain http"), (cloud(key=""), "API key"),
                      (cloud(model=""), "Model"), (cloud(url=""), "Base URL")]:
        r = CloudEngine().test_connection(cfg)
        assert not r.ok and text in r.message, (text, r.message)
    # local gateways may use http and no key
    assert CloudEngine().auto_config(Settings(_env_file=None)) is None


def test_cloud_auth_models_and_rate_limit(http):
    fake = http({"/models": (200, {"data": [{"id": "m1"}]})})
    r = CloudEngine().test_connection(cloud())
    assert r.ok and fake.calls[0][1] == {"Authorization": "Bearer sk-secret"}  # pragma: allowlist secret
    assert "sk-secret" not in r.message  # pragma: allowlist secret
    assert CloudEngine().test_connection(cloud(model="other")).message.startswith("Authenticated, but")
    http({"/models": (401, None)})
    r = CloudEngine().test_connection(cloud())
    assert not r.ok and "rejected" in r.message and "API key" in r.remediation
    http({"/models": (429, None)})
    assert "rate limiting" in CloudEngine().test_connection(cloud()).message
    http({"/models": (404, None)})
    assert CloudEngine().test_connection(cloud()).ok
    http({"/models": (500, None)})
    assert not CloudEngine().test_connection(cloud()).ok


def test_secret_never_in_public_dict_repr_or_logs(caplog):
    cfg = cloud()
    assert "sk-secret" not in json.dumps(cfg.public_dict()) and "(set)" in cfg.public_dict()["api_key"]  # pragma: allowlist secret
    assert "sk-secret" not in repr(cfg) and "sk-secret" not in cfg.describe()  # pragma: allowlist secret


def test_build_model_uses_engine_settings():
    m = CloudEngine().build_model(cloud(), timeout=7)
    assert m.model_name == "m1" and str(m.openai_api_base) == "https://api.example.com/v1"
    m = OllamaEngine().build_model(EngineConfig("ollama", "http://localhost:11434", "granite4.2:latest"))
    assert str(m.openai_api_base) == "http://localhost:11434/v1"
    with pytest.raises(EngineMisconfigured):
        CloudEngine().build_model(cloud(key=""))


def test_apply_env_mirrors_choice(monkeypatch):
    for k in ("OPENAI_BASE_URL", "MODEL_NAME", "OPENAI_API_KEY"):
        monkeypatch.setenv(k, "x")
    CloudEngine().apply_env(cloud())
    assert os.environ["OPENAI_BASE_URL"] == "https://api.example.com/v1" and os.environ["MODEL_NAME"] == "m1"


# ---- config_from_form --------------------------------------------------------------------
def test_config_from_form_env_key_fallback_and_validation(tmp_path):
    s = settings(tmp_path, cloud_api_key=SecretStr("env-key"))  # pragma: allowlist secret
    cfg = config_from_form("cloud", {"base_url": " https://x.test/v1 ", "model": "m", "api_key": ""}, s)
    assert cfg.base_url == "https://x.test/v1" and cfg.api_key.get_secret_value() == "env-key" and cfg.source == "ui"
    typed = config_from_form("cloud", {"base_url": "https://x.test/v1", "model": "m", "api_key": " typed "}, s)
    assert typed.api_key.get_secret_value() == "typed"
    with pytest.raises(EngineMisconfigured):
        config_from_form("cloud", {"base_url": "https://x.test/v1", "model": "m"}, settings(tmp_path))
    with pytest.raises(EngineMisconfigured):
        config_from_form("fake", {}, s)


# ---- manager: persistence ----------------------------------------------------------------
def test_override_roundtrip_never_stores_key(tmp_path):
    mgr = EngineManager(settings(tmp_path, cloud_api_key=SecretStr("env-key")))  # pragma: allowlist secret
    path = mgr.save_override(cloud(key="typed-secret"))  # pragma: allowlist secret
    raw = path.read_text()
    assert "secret" not in raw and "typed" not in raw and oct(path.stat().st_mode & 0o777) == "0o600"
    loaded = mgr.load_override()
    assert loaded.engine == "cloud" and loaded.model == "m1" and loaded.source == "ui"
    assert loaded.api_key.get_secret_value() == "env-key"  # re-read from env, not from disk
    assert mgr.clear_override() and not mgr.clear_override() and mgr.load_override() is None


def test_corrupt_override_is_ignored(tmp_path, caplog):
    mgr = EngineManager(settings(tmp_path))
    mgr.state_path.parent.mkdir(parents=True)
    for bad in ("{not json", json.dumps({"engine": "bogus"}), json.dumps({"engine": "fake"}), "[]"):
        mgr.state_path.write_text(bad)
        assert mgr.load_override() is None


# ---- manager: resolution priority --------------------------------------------------------
def test_resolve_fake_mode(tmp_path):
    r = EngineManager(settings(tmp_path, llm_mode="fake")).resolve()
    assert r.is_fake
    assert EngineManager(settings(tmp_path, llm_engine="fake")).resolve().is_fake


def test_resolve_auto_picks_ollama(http, tmp_path):
    http({"/api/tags": (200, OLLAMA_TAGS)})
    r = EngineManager(settings(tmp_path)).resolve()
    assert r.config.engine == "ollama" and r.config.source in ("auto", "env") and not r.warnings


def test_resolve_priority_ui_then_env_then_detected(http, tmp_path):
    http({"/api/tags": (200, OLLAMA_TAGS), "/v1/models": (200, {"data": [{"id": "gguf"}]})})
    s = settings(tmp_path, llm_engine="ollama", llamacpp_base_url="http://localhost:8080")
    mgr = EngineManager(s)
    # 2) explicit env beats detected llama.cpp
    assert mgr.resolve().config.engine == "ollama"
    # 1) saved UI choice beats env
    mgr.save_override(EngineConfig("llamacpp", "http://localhost:8080", "gguf"))
    r = mgr.resolve()
    assert r.config.engine == "llamacpp" and r.config.source == "ui"


def test_resolve_falls_back_when_selection_down(http, tmp_path):
    http({"/api/tags": (200, OLLAMA_TAGS)})  # llama.cpp down
    s = settings(tmp_path, llm_engine="llamacpp", llamacpp_base_url="http://localhost:8080")
    r = EngineManager(s).resolve()
    assert r.config.engine == "ollama" and r.config.source == "fallback"
    assert r.attempts[0]["engine"] == "llamacpp" and r.attempts[0]["ok"] == "False"
    assert "Using ollama after" in r.warnings[0]


def test_resolve_hardcoded_fallback_then_fake(http, tmp_path):
    http({})  # nothing reachable at all
    r = EngineManager(settings(tmp_path)).resolve()
    assert r.is_fake and "No LLM engine is reachable" in r.warnings[-1]
    assert r.attempts and r.attempts[-1]["engine"] == "ollama"  # hard-coded fallback was tried last


def test_resolve_unknown_engine_warns(http, tmp_path):
    http({"/api/tags": (200, OLLAMA_TAGS)})
    r = EngineManager(settings(tmp_path, llm_engine="bogus")).resolve()
    assert r.config.engine == "ollama" and "Unknown LLM engine" in r.warnings[0]


def test_cloud_only_used_when_fully_configured(http, tmp_path):
    http({"/api/tags": EngineConnectionError("down", "up"), "/models": (200, {"data": [{"id": "m"}]})})
    s = settings(tmp_path, llm_fallback_engines="cloud,ollama", cloud_base_url="https://api.example.com/v1",
                 cloud_model="m", cloud_api_key=SecretStr("k"))  # pragma: allowlist secret
    assert EngineManager(s).resolve().config.engine == "cloud"


def test_fallback_order_ignores_unknown_and_appends_rest(tmp_path, caplog):
    order = EngineManager(settings(tmp_path, llm_fallback_engines="cloud, bogus ,cloud,fake")).fallback_order()
    assert order == ["cloud", "ollama", "llamacpp"]


def test_detect_all_lists_every_selectable_engine(http, tmp_path):
    http({"/api/tags": (200, OLLAMA_TAGS)})
    det = EngineManager(settings(tmp_path)).detect_all()
    assert set(det) == {"ollama", "llamacpp", "cloud"} and det["ollama"].running and det["cloud"].status == "not detected"


def test_test_connection_never_raises_on_unknown_engine(tmp_path):
    r = EngineManager(settings(tmp_path)).test(EngineConfig("bogus"))
    assert not r.ok and r.remediation


def test_all_registered_engines_have_ids():
    assert set(ENGINES) == {"ollama", "llamacpp", "cloud", "fake"}
    assert issubclass(EngineConnectionError, LLMEngineError)
