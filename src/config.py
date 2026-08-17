"""Configuration loader for Vault Coordinator."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class CoordinatorConfig(BaseModel):
    port: int = 8650
    database: str = "coordinator.db"


class VikunjaConfig(BaseModel):
    url: str
    token: str = ""


class RadicaleConfig(BaseModel):
    url: str
    username: str
    password: str
    calendar: str = "vault-time-blocks"


class NtfyConfig(BaseModel):
    url: str
    topic: str
    username: str = ""
    password: str = ""


class RepoConfig(BaseModel):
    id: str
    name: str
    path: str


class MachineConfig(BaseModel):
    name: str                      # display name ("laptop", "server")
    ssh_alias: str = ""            # empty → localhost (subprocess, no ssh)


class IcsSubscription(BaseModel):
    """External read-only ICS feed replicated into a Radicale calendar.

    The feed (e.g. Google Calendar secret iCal URL) is the source of truth;
    the Radicale collection is a one-way replica owned by this worker.
    """
    name: str                      # stable id (job id, state key) — e.g. "sa-calendar"
    url: str                       # secret ICS feed URL
    collection: str                # Radicale collection path segment
    display_name: str = ""         # calendar display name (MKCOL)
    color: str = "#3F51B5"         # calendar color (MKCOL)
    interval_seconds: int = 3600   # poll interval
    confirm_delay_seconds: int = 45  # debounce delay between confirm fetches
    verify_every_n_runs: int = 24  # force full rewrite every N runs (drift heal)
    mass_delete_guard: float = 0.5 # skip deletions if feed shrinks below this fraction



class AIConfig(BaseModel):
    base_url: str = "https://api.z.ai/api/coding/paas/v4"
    model: str = "glm-5.1"
    api_key: str = ""               # via ${GLM_API_KEY} in config.yaml
    summary_ttl_seconds: int = 600


class AppConfig(BaseModel):
    coordinator: CoordinatorConfig
    vikunja: VikunjaConfig
    radicale: RadicaleConfig
    ntfy: NtfyConfig
    repos: list[RepoConfig] = []
    machines: list[MachineConfig] = []
    machines_poll_seconds: int = 30
    ics_subscriptions: list[IcsSubscription] = []
    ai: AIConfig = AIConfig()
    auth_token: str = ""           # bearer token; empty disables auth (dev)
    sync_interval_seconds: int = 300


def _expand_vars(value: str) -> str:
    """Expand ${ENV_VAR} references in string values."""
    def replacer(match):
        var_name = match.group(1)
        return os.environ.get(var_name, "")
    return re.sub(r"\$\{(\w+)\}", replacer, value)


def _walk_expand(obj: Any) -> Any:
    """Recursively expand env vars in all string values."""
    if isinstance(obj, str):
        return _expand_vars(obj)
    if isinstance(obj, dict):
        return {k: _walk_expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_expand(item) for item in obj]
    return obj


_config: AppConfig | None = None


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load config from YAML, expanding ${ENV_VAR} references."""
    global _config
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy config.example.yaml to config.yaml."
        )

    with open(path) as f:
        raw = yaml.safe_load(f)

    expanded = _walk_expand(raw)
    _config = AppConfig(**expanded)
    return _config


def get_config() -> AppConfig:
    """Get the loaded config. Must call load_config() first."""
    if _config is None:
        raise RuntimeError("Config not loaded. Call load_config() first.")
    return _config
