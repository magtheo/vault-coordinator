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


class AppConfig(BaseModel):
    coordinator: CoordinatorConfig
    vikunja: VikunjaConfig
    radicale: RadicaleConfig
    ntfy: NtfyConfig
    repos: list[RepoConfig] = []
    machines: list[MachineConfig] = []
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
