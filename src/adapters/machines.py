"""Machines adapter — live status pulls from hosts (jobs, tmux sessions).

Design: docs/design/vault-platform-extension.md §4 (machine repo).
Hosts are authoritative systems like any other: the coordinator pulls
read-only state and routes the single allowed write (stop-job) through
the forced-command allowlist in `machine-status` (ADR 009).
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

STATUS_SCRIPT = "~/.local/bin/machine-status"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]


def _localhost_status() -> dict[str, Any]:
    import os

    script = os.path.expanduser(STATUS_SCRIPT)
    r = subprocess.run([script], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"status script rc={r.returncode}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def _ssh_status(alias: str) -> dict[str, Any]:
    r = subprocess.run(
        ["ssh", *SSH_OPTS, alias, "status"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    # The forced command ignores the requested command; rc 111 = refused
    if r.returncode != 0:
        raise RuntimeError(f"ssh {alias} rc={r.returncode}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def pull_status(machine_cfg: dict[str, Any]) -> dict[str, Any]:
    """Pull status for one machine config {name, ssh_alias}.

    ssh_alias empty/None → localhost (subprocess, no ssh).
    """
    alias = machine_cfg.get("ssh_alias")
    status = _localhost_status() if not alias else _ssh_status(alias)
    status["name"] = machine_cfg["name"]
    return status


def stop_job(machine_cfg: dict[str, Any], job_name: str) -> str:
    """Route a stop-job command to the owning host. Returns result text."""
    alias = machine_cfg.get("ssh_alias")
    if not alias:
        r = subprocess.run(
            ["systemctl", "--user", "stop", f"job-{job_name}.service"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError(f"stop rc={r.returncode}: {r.stderr.strip()[:200]}")
        return "stopped"

    r = subprocess.run(
        ["ssh", *SSH_OPTS, alias, f"stop-job {job_name}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ssh stop rc={r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout.strip() or "stopped"
