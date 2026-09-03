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


class CalendarEntry(BaseModel):
    """V-065a calendars registry entry — one Radicale collection.

    `writable` is the authz flag: the API rejects writes (403) to any
    calendar not marked writable — read-only replicas (SA) are protected
    by enforcement, not convention (AGENTS.md principle 5, revised).
    `provision` MKCOLs the collection at startup if missing (new writable
    calendars only; existing collections and replicas are never touched).
    """

    id: str                      # stable registry id: "time-blocks", "personal", "sa"
    collection: str              # Radicale collection path segment
    display_name: str
    symbol: str = "▪"            # e-ink marker shown beside events
    writable: bool = False
    provision: bool = False


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



class VaultConfig(BaseModel):
    root: str = "~/Documents/Vault"
    git_name: str = "vault-coordinator"
    git_email: str = "vault-coordinator@localhost"


class AIConfig(BaseModel):
    base_url: str = "https://api.z.ai/api/coding/paas/v4"
    model: str = "glm-5.1"
    api_key: str = ""               # via ${GLM_API_KEY} in config.yaml
    summary_ttl_seconds: int = 600


class ChatConfig(BaseModel):
    """Kompakt chat assistant backend (Phase 7). Localhost Hermes API
    server by default — OpenAI-compatible, no key required."""
    base_url: str = "http://127.0.0.1:8642/v1"
    model: str = "claude-sonnet-4"
    api_key: str = ""
    timeout_s: float = 90.0
    max_history: int = 20
    system_prompt: str = ""         # empty → src.llm.DEFAULT_SYSTEM_PROMPT
    workspace_timeout_s: float = 180.0  # V-063: settle ceiling for workspace turns


class VoiceConfig(BaseModel):
    """Server-side STT (Phase 11, V-059). faster-whisper on CPU int8 —
    Pascal GPU not worth the CT2 CUDA fight for 5–60 s clips."""
    model_size: str = "small"   # tiny|base|small|medium — small = Norwegian floor
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = 8
    default_language: str = ""  # "" = auto-detect per clip; "no" forces Norwegian
    max_upload_bytes: int = 26_214_400  # 25 MiB — ~27 min of opus, way past the cap below
    max_duration_s: float = 120.0


class NoteBucket(BaseModel):
    """Sorter registry entry — deterministic rules only, no LLM in the
    routing decision (D028 v2). Key is the `00 - Inbox/<key>.md` slug;
    name is the human H1 written on first use."""

    key: str
    name: str
    aliases: list[str] = []
    keywords: list[str] = []


class NotesConfig(BaseModel):
    """Vault-wide notes surface (Phase 12 / D028 v2 — file-authoritative).

    The include list doubles as the pipeline's PARA scope: RepoTasks
    (machine status), Templates, and attachments are NOT user notes and
    stay out of the index. Empty `buckets` (the default) means every
    scratchpad section lands in `unsorted.md` — the honest cold-start
    for new vaults; structure emerges from use, never blocks it.
    """

    include_dirs: list[str] = [
        "00 - Inbox",
        "01 - Daily",
        "02 - Projects",
        "03 - Areas",
        "04 - Knowledge",
        "05 - Archive",
    ]
    root_files: list[str] = ["scratchpad.md"]
    max_title_chars: int = 200
    max_body_chars: int = 10_000

    # V-060b sorter pipeline
    sorter_enabled: bool = True
    tidy_enabled: bool = True
    sweeps: list[str] = ["07:00", "12:00", "17:00", "22:00"]
    sweep_timezone: str = "Europe/Oslo"
    buckets: list[NoteBucket] = []
    tidy_max_chars: int = 6_000


class WarrenBackendConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8660"
    token: str = ""                # ${WARREN_API_TOKEN} in config.yaml
    enabled: bool = False


class OpenCodeBackendConfig(BaseModel):
    base_url: str = "http://127.0.0.1:14096"
    enabled: bool = False
    # zai-coding-plan catalog (Aug 2026): glm-4.7, glm-5-turbo, glm-5.2.
    # glm-5.1 was REMOVED from the catalog — do not default to it.
    provider_id: str = "zai-coding-plan"
    model_id: str = "glm-5.2"


class HermesBackendConfig(BaseModel):
    """V-073/D033: the Hermes Agent API server (gateway `api_server`) as a
    third backend — the same process that powers the chat surface
    (ChatConfig.base_url). Executor-grade: full tool access, own provider
    credentials. Localhost-only behind the coordinator, like the others."""
    base_url: str = "http://127.0.0.1:8642"
    token: str = ""                 # bearer optional when api_server auth is off
    enabled: bool = False


class AgentsProjectMapping(BaseModel):
    """Per-repo backend bindings (repo id → backend-native handles).

    Warren keeps its own registry (prj_… from POST /projects {gitUrl});
    OpenCode binds sessions to a checkout directory. The coordinator owns
    this mapping (D025: general at the boundary, specific in adapters).
    """

    warren_project_id: str = ""    # prj_…; empty = not registered with warren
    opencode_directory: str = ""   # absolute path; empty = not bound


class AgentsConfig(BaseModel):
    """Phase 8: agent dispatch surface (V-052).

    enabled=False keeps /v1/agents + /v1/agent-runs fail-closed (501)
    exactly as before — flipping the flag is the only deploy step.
    """

    enabled: bool = False
    default_backend: str = "opencode"
    warren: WarrenBackendConfig = WarrenBackendConfig()
    opencode: OpenCodeBackendConfig = OpenCodeBackendConfig()
    hermes: HermesBackendConfig = HermesBackendConfig()
    projects: dict[str, AgentsProjectMapping] = {}  # repo id → mapping
    # V-057 agent-loop watcher
    poll_seconds: float = 5.0
    notify_ntfy: bool = True


class WorkspacesConfig(BaseModel):
    """T-022c: workspace registry — repo picker for agents (and later chat).

    Autodiscovery scans each root's immediate children for git repos once
    per process (restart-only refresh, by design). agents.projects entries
    with an opencode_directory are always explicit members and win ref
    collisions. ``deny`` lists refs (slug form, e.g. "kompakt-interface")
    to exclude from discovery.
    """

    roots: list[str] = []
    deny: list[str] = []


class AppConfig(BaseModel):
    coordinator: CoordinatorConfig
    vikunja: VikunjaConfig
    radicale: RadicaleConfig
    ntfy: NtfyConfig
    repos: list[RepoConfig] = []
    machines: list[MachineConfig] = []
    machines_poll_seconds: int = 30
    ics_subscriptions: list[IcsSubscription] = []
    # V-065a: explicit calendars registry. Empty (default) → derived from
    # radicale.calendar + ics_subscriptions in src.calendars (back-compat).
    calendars: list[CalendarEntry] = []
    vault: VaultConfig = VaultConfig()
    notes: NotesConfig = NotesConfig()
    ai: AIConfig = AIConfig()
    chat: ChatConfig = ChatConfig()
    voice: VoiceConfig = VoiceConfig()
    agents: AgentsConfig = AgentsConfig()
    workspaces: WorkspacesConfig = WorkspacesConfig()
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
