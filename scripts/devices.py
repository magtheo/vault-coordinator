#!/usr/bin/env python3
"""Device enrollment admin CLI (Kompakt Phase 4 / T-005).

Run on the server over SSH. Talks to the coordinator's admin API on
localhost using the admin token from config.yaml — the same authority
the middleware grants to /v1/admin/*.

Usage:
    python scripts/devices.py list
    python scripts/devices.py show <device_id>
    python scripts/devices.py approve <device_id> [--trust-class low] [--capabilities a,b]
    python scripts/devices.py revoke <device_id>
    python scripts/devices.py remove <device_id>

Approve defaults: trust_class=low, capabilities=all Class-1 reads
(today/inbox/task/project/chat/agent/note .read) per security.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config  # noqa: E402


def client(base_url: str, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )


def print_device(d: dict) -> None:
    print(f"  {d['device_id']}  {d['name']}")
    print(f"    status:       {d['status']}  (trust: {d['trust_class']})")
    print(f"    created:      {d['created_at']}")
    print(f"    approved:     {d['approved_at'] or '-'}")
    print(f"    last seen:    {d['last_seen'] or '-'}")
    if "has_token" in d:
        print(f"    token issued: {'yes' if d['has_token'] else 'no'}")
    print(f"    capabilities: {', '.join(d['capabilities']) or '(none)'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="all devices, newest first")
    p_show = sub.add_parser("show", help="one device")
    p_show.add_argument("device_id")
    p_approve = sub.add_parser("approve", help="pending → active (grants capabilities)")
    p_approve.add_argument("device_id")
    p_approve.add_argument("--trust-class", default="low", choices=["low", "medium", "admin"])
    p_approve.add_argument("--capabilities", default=None, help="comma-separated; default = all Class-1 reads")
    p_revoke = sub.add_parser("revoke", help="kill switch — device loses access immediately")
    p_revoke.add_argument("device_id")
    p_remove = sub.add_parser("remove", help="hard-delete the device row (test/stale devices)")
    p_remove.add_argument("device_id")

    args = parser.parse_args()
    cfg = load_config()
    if not cfg.auth_token:
        print("auth_token empty in config — coordinator auth disabled; CLI refusing.", file=sys.stderr)
        return 2

    base_url = "http://127.0.0.1:8650"
    with client(base_url, cfg.auth_token) as http:
        if args.cmd == "list":
            r = http.get("/v1/admin/devices")
            r.raise_for_status()
            devices = r.json()["devices"]
            if not devices:
                print("no devices enrolled.")
                return 0
            for d in devices:
                print_device(d)
            return 0

        if args.cmd == "show":
            r = http.get(f"/v1/admin/devices/{args.device_id}")
            r.raise_for_status()
            print_device(r.json())
            return 0

        if args.cmd == "approve":
            body: dict = {"trust_class": args.trust_class}
            if args.capabilities is not None:
                body["capabilities"] = [c.strip() for c in args.capabilities.split(",") if c.strip()]
            r = http.post(f"/v1/admin/devices/{args.device_id}/approve", json=body)
            r.raise_for_status()
            print("approved:")
            print_device(r.json())
            return 0

        if args.cmd == "revoke":
            r = http.post(f"/v1/admin/devices/{args.device_id}/revoke")
            r.raise_for_status()
            print("revoked:")
            print_device(r.json())
            return 0

        if args.cmd == "remove":
            r = http.delete(f"/v1/admin/devices/{args.device_id}")
            r.raise_for_status()
            print(f"deleted: {r.json()['deleted']}")
            return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
