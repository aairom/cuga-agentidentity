"""Pluggable LLM engines: Ollama, llama.cpp and OpenAI-compatible cloud services.

This module is UI-framework free. It provides:

* :class:`LLMEngine` - the abstraction (detect / validate / test_connection / build_model);
* three concrete engines plus an offline ``fake`` engine used by tests and as last resort;
* :class:`EngineManager` - detection, persisted UI choice, and the **resolution order**:

  1. an explicit selection (the UI override saved locally, then ``AI_LLM_ENGINE``);
  2. engines whose endpoint / credentials are configured or detected, in the order of
     ``AI_LLM_FALLBACK_ENGINES``;
  3. a hard-coded fallback (local Ollama), then the offline fake model as a last resort.

Secrets are :class:`pydantic.SecretStr` and are never logged, rendered or written to disk.
Every failure is a :class:`~agent_identity.errors.LLMEngineError` that carries a remediation hint.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import stat
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from .config import Settings
from .errors import EngineConnectionError, EngineMisconfigured, EngineUnavailable, LLMEngineError
from .logging_setup import get_logger
from .ui_helpers import probe_ollama

log = get_logger("llm")

# Hard-coded last-resort values (local Ollama). Everything else comes from configuration.
HARD_FALLBACK_ENGINE = "ollama"
HARD_FALLBACK_OLLAMA_URL = "http://localhost:11434"
HARD_FALLBACK_OLLAMA_MODEL = "granite4.2:latest"
FAKE_ENGINE = "fake"

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.containers.internal", "host.docker.internal"}  # noqa: S104


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------
_ORIGINAL_ENV: dict[str, str | None] = {}


def legacy_env(name: str) -> str:
    """Value of a legacy variable (OPENAI_BASE_URL / MODEL_NAME) as the user set it.

    :meth:`LLMEngine.apply_env` overwrites these variables to mirror the active engine, so the first
    value seen is remembered; otherwise switching to llama.cpp would corrupt the Ollama defaults.
    """
    _ORIGINAL_ENV.setdefault(name, os.environ.get(name))
    return _ORIGINAL_ENV[name] or ""


def root_url(url: str) -> str:
    """Strip whitespace, a trailing slash and a trailing ``/v1`` (native-API root)."""
    u = (url or "").strip().rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


def openai_url(url: str) -> str:
    """Return the OpenAI-compatible base URL (``<root>/v1``) for a local server URL."""
    return root_url(url) + "/v1"


def is_local_url(url: str) -> bool:
    """True when the URL points at this machine (no TLS needed, key optional)."""
    host = (urlparse(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.endswith(".local")


def same_model(a: str, b: str) -> bool:
    """Compare model names, treating ``name`` and ``name:latest`` as equal (Ollama convention)."""
    a, b = a.strip(), b.strip()
    return a == b or a == f"{b}:latest" or b == f"{a}:latest"


def _check_http_url(url: str, label: str, example: str) -> None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise EngineMisconfigured(f"{label} '{url}' is not a valid http(s) URL.",
                                  f"Use a full URL such as {example}.")


def _http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 5.0) -> tuple[int, Any]:
    """GET ``url`` and return ``(status, json_or_None)``. Network problems become engine errors.

    Kept as a module-level function so tests can replace it without opening sockets.
    """
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=False)
    except httpx.TimeoutException as exc:
        raise EngineConnectionError(f"Timed out after {timeout:g}s contacting {url}.",
                                    "Check that the server is running and not overloaded, or raise the timeout.") from exc
    except httpx.HTTPError as exc:
        raise EngineConnectionError(f"Cannot connect to {url} ({type(exc).__name__}).",
                                    "Check the URL and port, that the server is running, and any firewall/VPN.") from exc
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status_code, data


# --------------------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigField:
    """Describes one configuration input so the UI can render it generically."""

    name: str  # attribute of EngineConfig: base_url | model | api_key
    label: str
    required: bool = False
    secret: bool = False
    help: str = ""


@dataclass
class EngineConfig:
    """Connection details for one engine. ``api_key`` is a SecretStr (never rendered)."""

    engine: str
    base_url: str = ""
    model: str = ""
    api_key: SecretStr = field(default_factory=lambda: SecretStr(""))
    source: str = "default"  # ui | env | auto | fallback | default | fake

    def public_dict(self) -> dict[str, Any]:
        """Safe-to-display / safe-to-log representation (the key is masked)."""
        return {"engine": self.engine, "base_url": self.base_url, "model": self.model,
                "api_key": "(set)" if self.api_key.get_secret_value() else "(not set)", "source": self.source}

    def describe(self) -> str:
        return f"{self.engine}:{self.model or '?'} [{self.source}]"

    def with_updates(self, **changes: Any) -> "EngineConfig":
        data = {"engine": self.engine, "base_url": self.base_url, "model": self.model,
                "api_key": self.api_key, "source": self.source}
        data.update(changes)
        return EngineConfig(**data)


@dataclass
class TestResult:
    """Outcome of a connection test. ``remediation`` is empty when ``ok``."""

    __test__ = False  # not a pytest test class

    engine: str
    ok: bool
    message: str
    remediation: str = ""
    models: list[str] = field(default_factory=list)
    latency_ms: int = 0
    resolved_model: str = ""


@dataclass
class Detection:
    """What is known about an engine on this machine, without sending credentials anywhere."""

    engine: str
    installed: bool | None  # None = not applicable (cloud)
    running: bool
    endpoint: str = ""
    models: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.running:
            return "running"
        if self.installed:
            return "installed, not running"
        return "not detected"


@dataclass
class Resolution:
    """The engine chosen at startup, and why."""

    config: EngineConfig
    attempts: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_fake(self) -> bool:
        return self.config.engine == FAKE_ENGINE


# --------------------------------------------------------------------------------------
# Abstraction
# --------------------------------------------------------------------------------------
class LLMEngine(ABC):
    """One pluggable LLM backend."""

    engine_id: str = ""
    label: str = ""
    description: str = ""
    fields: tuple[ConfigField, ...] = ()
    selectable: bool = True

    # -- configuration ----------------------------------------------------------------
    @abstractmethod
    def auto_config(self, settings: Settings) -> EngineConfig | None:
        """Config derived from env/detection, or ``None`` if the engine is not usable as configured."""

    def form_defaults(self, settings: Settings) -> EngineConfig:
        """Values pre-filled in the UI form (never ``None``)."""
        return self.auto_config(settings) or EngineConfig(self.engine_id)

    @abstractmethod
    def detect(self, settings: Settings) -> Detection:
        """Cheap local detection (binary on PATH, port answering). Never sends credentials."""

    def validate(self, cfg: EngineConfig) -> None:
        """Raise :class:`EngineMisconfigured` when the configuration cannot work."""
        for f in self.fields:
            if f.required and not (cfg.api_key.get_secret_value() if f.secret else getattr(cfg, f.name, "")):
                raise EngineMisconfigured(f"{self.label}: '{f.label}' is required.",
                                          f"Fill in '{f.label}' in the engine settings or set the matching variable in .env.")

    # -- connectivity -----------------------------------------------------------------
    @abstractmethod
    def _probe(self, cfg: EngineConfig, timeout: float) -> TestResult:
        """Contact the engine; raise an :class:`LLMEngineError` or return a result."""

    def connect_hint(self, cfg: EngineConfig) -> str:
        """Engine-specific remediation for "cannot connect" (empty = keep the generic one)."""
        return ""

    def test_connection(self, cfg: EngineConfig, timeout: float = 5.0) -> TestResult:
        """Validate then contact the engine. Never raises: failures come back as ``ok=False``."""
        start = time.perf_counter()
        try:
            self.validate(cfg)
            result = self._probe(cfg, timeout)
        except LLMEngineError as exc:
            hint = self.connect_hint(cfg) if type(exc) is EngineConnectionError and "Cannot connect" in exc.message else ""
            result = TestResult(self.engine_id, False, exc.message, hint or exc.remediation)
        result.latency_ms = int((time.perf_counter() - start) * 1000)
        if result.ok:
            log.info("engine %s OK (%s, %d ms)", self.engine_id, cfg.model or "no model", result.latency_ms)
        else:
            log.warning("engine %s unavailable: %s Hint: %s", self.engine_id, result.message, result.remediation)
        return result

    # -- model ------------------------------------------------------------------------
    def build_model(self, cfg: EngineConfig, timeout: float = 300.0) -> Any:
        """Return a LangChain chat model for Cuga (OpenAI-compatible wire protocol)."""
        self.validate(cfg)
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise EngineUnavailable("langchain-openai is not installed.",
                                    "Run: uv pip install -p .venv/bin/python -r requirements.txt") from exc
        return ChatOpenAI(model=cfg.model, base_url=self.chat_base_url(cfg),
                          api_key=cfg.api_key.get_secret_value() or "not-needed",  # pragma: allowlist secret
                          timeout=timeout, temperature=0.1, max_retries=1)

    def chat_base_url(self, cfg: EngineConfig) -> str:
        return cfg.base_url

    def apply_env(self, cfg: EngineConfig) -> None:
        """Mirror the choice into the env vars Cuga reads for its own internal paths."""
        legacy_env("OPENAI_BASE_URL"), legacy_env("MODEL_NAME")  # remember the user's originals first
        os.environ["OPENAI_BASE_URL"] = self.chat_base_url(cfg)
        os.environ["MODEL_NAME"] = cfg.model
        os.environ["OPENAI_API_KEY"] = cfg.api_key.get_secret_value() or "not-needed"  # pragma: allowlist secret


# --------------------------------------------------------------------------------------
# Concrete engines
# --------------------------------------------------------------------------------------
class OllamaEngine(LLMEngine):
    engine_id = "ollama"
    label = "Ollama (local)"
    description = "Local Ollama daemon. Models are managed with `ollama pull`."
    fields = (ConfigField("base_url", "Base URL", True, help="e.g. http://localhost:11434"),
              ConfigField("model", "Model", True, help="e.g. granite4.2:latest"))

    def auto_config(self, settings: Settings) -> EngineConfig | None:
        url = settings.ollama_base_url or legacy_env("OPENAI_BASE_URL") or HARD_FALLBACK_OLLAMA_URL
        model = settings.ollama_model or legacy_env("MODEL_NAME") or HARD_FALLBACK_OLLAMA_MODEL
        explicit = bool(settings.ollama_base_url or settings.ollama_model)
        return EngineConfig(self.engine_id, root_url(url), model, source="env" if explicit else "auto")

    def detect(self, settings: Settings) -> Detection:
        cfg = self.auto_config(settings)
        assert cfg is not None
        reachable, models = probe_ollama(cfg.base_url, timeout=1.0)
        installed = bool(shutil.which("ollama")) or reachable
        notes = [] if installed else ["`ollama` binary not found on PATH and nothing answers on the endpoint."]
        return Detection(self.engine_id, installed, reachable, cfg.base_url, models, notes)

    def validate(self, cfg: EngineConfig) -> None:
        super().validate(cfg)
        _check_http_url(cfg.base_url, "Ollama base URL", "http://localhost:11434")

    def chat_base_url(self, cfg: EngineConfig) -> str:
        return openai_url(cfg.base_url)

    def connect_hint(self, cfg: EngineConfig) -> str:
        return f"Start Ollama (`ollama serve` or the desktop app) and check that {root_url(cfg.base_url)} is the right address."

    def _probe(self, cfg: EngineConfig, timeout: float) -> TestResult:
        status, data = _http_get(f"{root_url(cfg.base_url)}/api/tags", timeout=timeout)
        if status != 200 or not isinstance(data, dict):
            raise EngineConnectionError(f"{cfg.base_url} answered HTTP {status}, which is not an Ollama API.",
                                        "Point the base URL at the Ollama daemon (default http://localhost:11434), "
                                        "not at another service.")
        models = [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]
        if not any(same_model(cfg.model, m) for m in models):
            return TestResult(self.engine_id, False, f"Ollama is running but model '{cfg.model}' is not installed.",
                              f"Run `ollama pull {cfg.model}` or choose one of: {', '.join(models) or '(none installed)'}.",
                              models)
        return TestResult(self.engine_id, True, f"Ollama reachable, model '{cfg.model}' installed.", models=models,
                          resolved_model=cfg.model)


class LlamaCppEngine(LLMEngine):
    engine_id = "llamacpp"
    label = "llama.cpp (llama-server)"
    description = "llama.cpp HTTP server with its OpenAI-compatible API (`llama-server -m model.gguf --port N`)."
    fields = (ConfigField("base_url", "Server URL", True, help="e.g. http://localhost:8080"),
              ConfigField("model", "Model name", False, help="Optional: llama-server serves one model; leave empty to auto-detect."))
    binary_names = ("llama-server", "llama-cpp-server", "llama-cli")

    def _binary(self, settings: Settings) -> str:
        if settings.llamacpp_binary:
            p = Path(settings.llamacpp_binary).expanduser()
            return str(p) if p.is_file() and os.access(p, os.X_OK) else ""
        return next((w for n in self.binary_names if (w := shutil.which(n))), "")

    def _probe_urls(self, settings: Settings) -> list[str]:
        urls = [settings.llamacpp_base_url] if settings.llamacpp_base_url else []
        urls += [u.strip() for u in settings.llamacpp_probe_urls.split(",") if u.strip()]
        return list(dict.fromkeys(root_url(u) for u in urls))

    def _running_url(self, settings: Settings) -> tuple[str, list[str]]:
        for url in self._probe_urls(settings):
            try:
                status, data = _http_get(f"{url}/v1/models", timeout=1.0)
            except EngineConnectionError:
                continue
            if status == 200:
                return url, _model_ids(data)
            if status in (404, 405):  # older builds only expose /health
                try:
                    if _http_get(f"{url}/health", timeout=1.0)[0] == 200:
                        return url, []
                except EngineConnectionError:
                    continue
        return "", []

    def auto_config(self, settings: Settings) -> EngineConfig | None:
        url, models = self._running_url(settings)
        if not url:
            return None
        return EngineConfig(self.engine_id, url, settings.llamacpp_model or (models[0] if models else ""),
                            source="env" if settings.llamacpp_base_url else "auto")

    def form_defaults(self, settings: Settings) -> EngineConfig:
        urls = self._probe_urls(settings)
        return EngineConfig(self.engine_id, urls[0] if urls else "", settings.llamacpp_model)

    def detect(self, settings: Settings) -> Detection:
        binary = self._binary(settings)
        url, models = self._running_url(settings)
        notes = []
        if binary:
            notes.append(f"binary: {binary}")
        if not url:
            notes.append(f"no server answered on: {', '.join(self._probe_urls(settings)) or '(no URLs configured)'}")
        return Detection(self.engine_id, bool(binary) or bool(url), bool(url), url, models, notes)

    def start_command(self, model_path: str = "<model.gguf>", port: int = 8080) -> str:
        """Command line to start a local server (shown as a hint; this app never spawns it)."""
        return f"llama-server -m {model_path} --port {port}"

    def validate(self, cfg: EngineConfig) -> None:
        super().validate(cfg)
        _check_http_url(cfg.base_url, "llama.cpp server URL", "http://localhost:8080")

    def chat_base_url(self, cfg: EngineConfig) -> str:
        return openai_url(cfg.base_url)

    def connect_hint(self, cfg: EngineConfig) -> str:
        return (f"Start the server, e.g. `{self.start_command()}`, or fix the URL/port "
                f"(this setup may use 8080 or 9931; the URL is {cfg.base_url}).")

    def _probe(self, cfg: EngineConfig, timeout: float) -> TestResult:
        root = root_url(cfg.base_url)
        hint = f"Start the server, e.g. `{self.start_command()}`, or fix the URL/port (8080 and 9931 are common)."
        status, data = _http_get(f"{root}/v1/models", timeout=timeout)
        if status in (404, 405):
            status, data = _http_get(f"{root}/health", timeout=timeout)
        if status == 503:
            raise EngineUnavailable("llama.cpp is still loading the model.", "Wait for the model to finish loading, then retry.")
        if status != 200:
            raise EngineConnectionError(f"{cfg.base_url} answered HTTP {status}, which is not a llama.cpp server.", hint)
        models = _model_ids(data)
        chosen = cfg.model or (models[0] if models else "local-model")
        return TestResult(self.engine_id, True, f"llama.cpp server reachable ({', '.join(models) or 'model name not reported'}).",
                          models=models, resolved_model=chosen)


class CloudEngine(LLMEngine):
    engine_id = "cloud"
    label = "Cloud (OpenAI-compatible)"
    description = "Any hosted OpenAI-compatible API (OpenAI, Azure OpenAI gateway, OpenRouter, Groq, watsonx proxy...)."
    fields = (ConfigField("base_url", "Base URL", True, help="Provider endpoint ending in /v1, from the provider docs"),
              ConfigField("model", "Model", True, help="Model id as named by the provider"),
              ConfigField("api_key", "API key", True, secret=True, help="Kept in memory only; never written to disk"))

    def auto_config(self, settings: Settings) -> EngineConfig | None:
        key = settings.cloud_api_key
        if not (settings.cloud_base_url and settings.cloud_model and key.get_secret_value()):
            return None  # never guess a cloud endpoint or send a key somewhere unconfigured
        return EngineConfig(self.engine_id, settings.cloud_base_url.strip().rstrip("/"), settings.cloud_model, key, "env")

    def form_defaults(self, settings: Settings) -> EngineConfig:
        return EngineConfig(self.engine_id, settings.cloud_base_url, settings.cloud_model, settings.cloud_api_key)

    def detect(self, settings: Settings) -> Detection:
        cfg = self.auto_config(settings)
        notes = ["configured via environment" if cfg else "needs base URL, model and API key (AI_CLOUD_*)"]
        return Detection(self.engine_id, None, False, settings.cloud_base_url, [], notes)

    def validate(self, cfg: EngineConfig) -> None:
        _check_http_url(cfg.base_url, "Cloud base URL", "https://api.example.com/v1") if cfg.base_url else None
        if not cfg.base_url:
            raise EngineMisconfigured("Cloud: 'Base URL' is required.", "Copy the OpenAI-compatible endpoint from your provider's docs.")
        if not cfg.model:
            raise EngineMisconfigured("Cloud: 'Model' is required.", "Enter the model id exactly as the provider names it.")
        local = is_local_url(cfg.base_url)
        if urlparse(cfg.base_url).scheme != "https" and not local:
            raise EngineMisconfigured("Cloud: refusing to send an API key over plain http to a remote host.",
                                      "Use an https:// base URL.")
        if not cfg.api_key.get_secret_value() and not local:
            raise EngineMisconfigured("Cloud: 'API key' is required for a remote endpoint.",
                                      "Enter the key in the UI (kept in memory) or set AI_CLOUD_API_KEY in .env.")

    def connect_hint(self, cfg: EngineConfig) -> str:
        return "Check your internet/VPN/proxy access to the provider and that the base URL is correct (it normally ends in /v1)."

    def _probe(self, cfg: EngineConfig, timeout: float) -> TestResult:
        key = cfg.api_key.get_secret_value()
        headers = {"Authorization": f"Bearer {key}"} if key else None
        status, data = _http_get(f"{cfg.base_url.rstrip('/')}/models", headers, timeout)
        if status in (401, 403):
            raise EngineConnectionError(f"The provider rejected the credentials (HTTP {status}).",
                                        "Check the API key, its permissions/expiry and that it belongs to this base URL.")
        if status == 429:
            raise EngineConnectionError("The provider is rate limiting this key (HTTP 429).", "Wait a moment or check your quota.")
        if status in (404, 405):  # many gateways do not expose /models
            return TestResult(self.engine_id, True, "Endpoint reachable; it does not list models, so the model id is unverified.",
                              resolved_model=cfg.model)
        if status != 200:
            raise EngineConnectionError(f"The provider answered HTTP {status}.", "Check the base URL (it normally ends in /v1) and the provider status page.")
        models = _model_ids(data)
        if models and not any(same_model(cfg.model, m) for m in models):
            return TestResult(self.engine_id, True, f"Authenticated, but '{cfg.model}' is not in the provider's model list "
                              "(it may still work if the list is partial).", models=models, resolved_model=cfg.model)
        return TestResult(self.engine_id, True, "Authenticated and model available.", models=models, resolved_model=cfg.model)


def _model_ids(data: Any) -> list[str]:
    """Extract model ids from an OpenAI-style ``/v1/models`` payload."""
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
        return [str(m.get("id") or m.get("model") or m.get("name") or "") for m in items if isinstance(m, dict)]
    return []


class FakeEngine(LLMEngine):
    """Deterministic offline model: used by tests, the harness and as the last-resort fallback."""

    engine_id = FAKE_ENGINE
    label = "Offline fake model"
    description = "No LLM. Policies and identity checks still run; tool selection does not."
    selectable = False

    def auto_config(self, settings: Settings) -> EngineConfig | None:
        return EngineConfig(FAKE_ENGINE, "", "fake-model", source="fake")

    def detect(self, settings: Settings) -> Detection:
        return Detection(FAKE_ENGINE, None, True, "", [], ["always available"])

    def _probe(self, cfg: EngineConfig, timeout: float) -> TestResult:
        return TestResult(FAKE_ENGINE, True, "Offline fake model.", resolved_model="fake-model")

    def build_model(self, cfg: EngineConfig, timeout: float = 300.0) -> Any:
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage

        return GenericFakeChatModel(messages=itertools.repeat(AIMessage(content="(fake model: no LLM configured)")))

    def apply_env(self, cfg: EngineConfig) -> None:
        os.environ.setdefault("OPENAI_API_KEY", "fake")  # Cuga builds an LLM client eagerly  # pragma: allowlist secret


ENGINES: dict[str, LLMEngine] = {e.engine_id: e for e in (OllamaEngine(), LlamaCppEngine(), CloudEngine(), FakeEngine())}


def get_engine(engine_id: str) -> LLMEngine:
    """Return the engine for ``engine_id`` or raise :class:`EngineMisconfigured` with the valid ids."""
    try:
        return ENGINES[engine_id]
    except KeyError:
        valid = ", ".join(e.engine_id for e in ENGINES.values() if e.selectable)
        raise EngineMisconfigured(f"Unknown LLM engine '{engine_id}'.", f"Use one of: {valid}.") from None


# --------------------------------------------------------------------------------------
# Manager: detection, persisted UI choice, resolution order
# --------------------------------------------------------------------------------------
class EngineManager:
    """Detects engines, persists the UI selection and resolves the engine to use."""

    OVERRIDE_FILE = "llm_engine.json"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_path = Path(settings.state_dir) / self.OVERRIDE_FILE

    # ---- listing / detection ------------------------------------------------------
    def selectable(self) -> list[LLMEngine]:
        return [e for e in ENGINES.values() if e.selectable]

    def detect_all(self) -> dict[str, Detection]:
        return {e.engine_id: e.detect(self.settings) for e in self.selectable()}

    def fallback_order(self) -> list[str]:
        """Configured fallback order; unknown ids are ignored (with a warning) and the rest appended."""
        order: list[str] = []
        for raw in self.settings.llm_fallback_engines.split(","):
            eid = raw.strip().lower()
            if not eid:
                continue
            if eid not in ENGINES or not ENGINES[eid].selectable:
                log.warning("AI_LLM_FALLBACK_ENGINES: ignoring unknown engine '%s'", eid)
                continue
            if eid not in order:
                order.append(eid)
        return order + [e.engine_id for e in self.selectable() if e.engine_id not in order]

    # ---- persisted UI choice (no secrets on disk) -----------------------------------
    def save_override(self, cfg: EngineConfig) -> Path:
        """Persist engine/base_url/model. The API key is deliberately never written."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"engine": cfg.engine, "base_url": cfg.base_url, "model": cfg.model}
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(self.state_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - non-POSIX filesystems
            pass
        log.info("saved UI engine selection: %s", cfg.describe())
        return self.state_path

    def load_override(self) -> EngineConfig | None:
        """Read the saved UI choice. A corrupt/unknown file is ignored with a warning."""
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            engine = get_engine(str(data["engine"]))
            if not engine.selectable:
                raise EngineMisconfigured("not selectable")
            key = self.settings.cloud_api_key if engine.engine_id == "cloud" else SecretStr("")
            return EngineConfig(engine.engine_id, str(data.get("base_url", "")), str(data.get("model", "")), key, "ui")
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, LLMEngineError, OSError) as exc:
            log.warning("ignoring unreadable %s (%s). Delete it or re-save from the UI.", self.state_path, exc)
            return None

    def clear_override(self) -> bool:
        try:
            self.state_path.unlink()
            return True
        except FileNotFoundError:
            return False

    # ---- testing / resolution -------------------------------------------------------
    def test(self, cfg: EngineConfig, timeout: float = 5.0) -> TestResult:
        try:
            engine = get_engine(cfg.engine)
        except EngineMisconfigured as exc:
            return TestResult(cfg.engine, False, exc.message, exc.remediation)
        return engine.test_connection(cfg, timeout)

    def _explicit_id(self) -> str:
        eid = self.settings.llm_engine.strip().lower()
        return "" if eid in ("", "auto") else eid

    def resolve(self, timeout: float = 3.0) -> Resolution:
        """Pick the engine to start with. Order: UI choice, ``AI_LLM_ENGINE``, configured/detected
        engines in fallback order, hard-coded Ollama, offline fake. Every attempt is recorded."""
        if self.settings.llm_mode == "fake" or self._explicit_id() == FAKE_ENGINE:
            return Resolution(ENGINES[FAKE_ENGINE].auto_config(self.settings))  # type: ignore[arg-type]

        res = Resolution(EngineConfig(FAKE_ENGINE, source="fake"))
        candidates: list[tuple[str, EngineConfig]] = []
        if (ov := self.load_override()) is not None:
            candidates.append(("ui", ov))
        if self._explicit_id():
            try:
                candidates.append(("env", get_engine(self._explicit_id()).form_defaults(self.settings).with_updates(source="env")))
            except EngineMisconfigured as exc:
                res.warnings.append(str(exc))
                log.warning("AI_LLM_ENGINE: %s", exc)
        for eid in self.fallback_order():
            if (cfg := ENGINES[eid].auto_config(self.settings)) is not None:
                candidates.append(("env" if cfg.source == "env" else "auto", cfg))
        candidates.append(("fallback", EngineConfig(HARD_FALLBACK_ENGINE, HARD_FALLBACK_OLLAMA_URL, HARD_FALLBACK_OLLAMA_MODEL,
                                                    source="fallback")))

        seen: set[tuple[str, str, str]] = set()
        for origin, cfg in candidates:
            sig = (cfg.engine, root_url(cfg.base_url), cfg.model)
            if sig in seen:
                continue
            seen.add(sig)
            result = self.test(cfg, timeout)
            res.attempts.append({"engine": cfg.engine, "origin": origin, "ok": str(result.ok), "message": result.message,
                                 "remediation": result.remediation})
            if result.ok:
                res.config = cfg.with_updates(model=cfg.model or result.resolved_model)
                if res.attempts[:-1]:
                    res.config = res.config.with_updates(source="fallback")
                    res.warnings.append(f"Using {cfg.engine} after: " + "; ".join(
                        f"{a['engine']} ({a['message']})" for a in res.attempts[:-1]))
                    log.warning("engine fallback -> %s. %s", cfg.describe(), res.warnings[-1])
                log.info("LLM engine resolved: %s", res.config.describe())
                return res
        res.warnings.append("No LLM engine is reachable: running with the offline fake model. "
                            + (res.attempts[0]["remediation"] if res.attempts else ""))
        log.warning(res.warnings[-1])
        res.config = ENGINES[FAKE_ENGINE].auto_config(self.settings)  # type: ignore[assignment]
        return res


def config_from_form(engine_id: str, values: dict[str, str], settings: Settings) -> EngineConfig:
    """Build an :class:`EngineConfig` from raw UI inputs (strip, validate, fall back to env for the key).

    An empty API-key box means "use the key from the environment" so that a key set in ``.env``
    never has to be re-typed (or shown) in the UI. Raises :class:`EngineMisconfigured`.
    """
    engine = get_engine(engine_id)
    if not engine.selectable:
        raise EngineMisconfigured(f"Engine '{engine_id}' cannot be selected.", "Choose Ollama, llama.cpp or Cloud.")
    typed = (values.get("api_key") or "").strip()
    key = SecretStr(typed) if typed else (settings.cloud_api_key if engine_id == "cloud" else SecretStr(""))
    cfg = EngineConfig(engine_id, (values.get("base_url") or "").strip(), (values.get("model") or "").strip(), key, "ui")
    engine.validate(cfg)
    return cfg
