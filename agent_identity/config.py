"""Configuration management.

All values come from environment variables (optionally a ``.env`` file); nothing
is hard-coded. See ``.env.example`` for the full list.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings (prefix ``AI_``)."""

    model_config = SettingsConfigDict(env_prefix="AI_", env_file=".env", extra="ignore")

    # Identity backend
    identity_backend: Literal["mock", "verify"] = "mock"
    verify_tenant_url: str = ""
    verify_admin_client_id: str = ""
    verify_admin_client_secret: SecretStr = SecretStr("")
    mock_issuer: str = "https://mock-verify.local"
    token_ttl_seconds: int = 300
    # Defence in depth: also check agent status at the resource server, because
    # suspending an agent does NOT revoke tokens that were already issued.
    strict_status_check: bool = True

    # LLM
    llm_mode: Literal["ollama", "fake"] = "ollama"
    # "function_calling" = native tool calls (best for small local models);
    # "codeact" = the model writes Python that calls tools in Cuga's sandbox.
    execution_mode: Literal["function_calling", "codeact"] = "function_calling"

    # LLM engine selection (see agent_identity/llm_engines.py and Docs/LLM-Engines.md).
    # "" / "auto" = resolve automatically; otherwise one of ollama | llamacpp | cloud | fake.
    llm_engine: str = ""
    # Order tried when the selected engine is unreachable, and for auto-detection.
    llm_fallback_engines: str = "ollama,llamacpp,cloud"
    llm_timeout: float = 300.0
    # Per-engine connection details. Empty = not explicitly configured.
    ollama_base_url: str = ""
    ollama_model: str = ""
    llamacpp_base_url: str = ""
    llamacpp_model: str = ""
    llamacpp_binary: str = ""  # optional path to a local llama-server binary (detection only)
    # Where to look for a running llama.cpp server when no URL is configured (comma separated).
    llamacpp_probe_urls: str = "http://localhost:8080,http://localhost:9931"
    cloud_base_url: str = ""
    cloud_model: str = ""
    cloud_api_key: SecretStr = SecretStr("")

    # App
    log_level: str = "INFO"
    output_dir: str = "output"
    state_dir: str = ".cuga_state"
    ui_port: int = 8601


# Defaults for the OpenAI-compatible endpoint Cuga uses (local Ollama).
_LLM_ENV_DEFAULTS = {
    "OPENAI_API_KEY": "ollama",
    "OPENAI_BASE_URL": "http://localhost:11434/v1",
    "MODEL_NAME": "granite4.2:latest",
    "AGENT_SETTING_CONFIG": "settings.ollama.toml",
    "LOGURU_LEVEL": "WARNING",
}


def apply_llm_env() -> None:
    """Populate the environment variables Cuga reads, without overriding user values."""
    for key, value in _LLM_ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load ``.env`` (if any) and return the cached :class:`Settings`."""
    load_dotenv(override=False)
    return Settings()
