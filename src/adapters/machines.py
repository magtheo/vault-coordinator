"""Machines adapter — live status pulls from hosts (jobs, tmux sessions).

Design: docs/design/vault-platform-extension.md §4 (machine repo).
Hosts are authoritative systems like any other: the coordinator pulls
read-only state and routes allowed writes through the forced-command
allowlist in `machine-status` (ADR 009).

Uniform transport: the same `machine-status` script runs everywhere —
locally via subprocess (with SSH_ORIGINAL_COMMAND set), remotely via ssh.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

STATUS_SCRIPT = os.path.expanduser("~/.local/bin/machine-status")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]


def _run_gate(alias: str | None, command: str, timeout: int = 20) -> str:
    """Run a machine-status gate command on a host. Returns stdout."""
    if not alias:
        env = {**os.environ, "SSH_ORIGINAL_COMMAND": command}
        r = subprocess.run([STATUS_SCRIPT], capture_output=True, text=True,
                           timeout=timeout, env=env)
    else:
        r = subprocess.run(["ssh", *SSH_OPTS, alias, command],
                           capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(
            f"gate rc={r.returncode} on {alias or 'localhost'}: "
            f"{r.stderr.strip()[:200]}")
    return r.stdout


def _localhost_status() -> dict[str, Any]:
    return json.loads(_run_gate(None, "status", timeout=15))


def pull_status(machine_cfg: dict[str, Any]) -> dict[str, Any]:
    alias = machine_cfg.get("ssh_alias") or None
    status = json.loads(_run_gate(alias, "status", timeout=20))
    status["name"] = machine_cfg["name"]
    return status


def stop_job(machine_cfg: dict[str, Any], job_name: str) -> str:
    alias = machine_cfg.get("ssh_alias") or None
    out = _run_gate(alias, f"stop-job {job_name}", timeout=30)
    return out.strip() or "stopped"


def job_log(machine_cfg: dict[str, Any], job_name: str, lines: int = 50) -> str:
    alias = machine_cfg.get("ssh_alias") or None
    return _run_gate(alias, f"job-log {job_name} {lines}", timeout=20)


def git_info(machine_cfg: dict[str, Any], path: str) -> dict[str, Any]:
    alias = machine_cfg.get("ssh_alias") or None
    out = _run_gate(alias, f"git-info {path}", timeout=20)
    return json.loads(out)
