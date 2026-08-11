"""Capability model — determines available operations per entity type.

Capabilities are enforced in the coordinator, not just the frontend.
The coordinator rejects any operation not in the entity's capability set
with HTTP 403.
"""
from __future__ import annotations

# ─── Capability definitions per entity type ────────────────────────────

_CAPABILITIES: dict[str, set[str]] = {
    "vikunja_task": {
        "edit",
        "complete",
        "reopen",
        "schedule",
        "set_deadline",
        "set_priority",
        "set_project",
    },
    "repo_task": {
        "schedule",
        "open_source",
    },
    "calendar_event": set(),  # events are managed through schedule endpoints
    "project": set(),
}

# Operations that require a capability check
_PROTECTED_OPERATIONS = {
    "edit", "complete", "reopen", "set_deadline",
    "set_priority", "set_project", "open_source",
}


def get_capabilities(entity_type: str) -> dict[str, bool]:
    """Return a capability dict for API responses."""
    caps = _CAPABILITIES.get(entity_type, set())
    all_caps = _PROTECTED_OPERATIONS | {"schedule"}
    return {cap: cap in caps for cap in all_caps}


def can_perform(entity_type: str, operation: str) -> bool:
    """Check if an entity type supports an operation.

    Raises ValueError with a descriptive message if not supported,
    so routers can convert to HTTP 403.
    """
    caps = _CAPABILITIES.get(entity_type, set())
    if operation in caps:
        return True
    if operation in _PROTECTED_OPERATIONS:
        return False
    # Unknown operations are rejected
    return False


class CapabilityError(Exception):
    """Raised when an operation is not supported for an entity type."""

    def __init__(self, entity_type: str, operation: str):
        self.entity_type = entity_type
        self.operation = operation
        super().__init__(
            f"Operation '{operation}' is not supported for entity type '{entity_type}'"
        )


def enforce(entity_type: str, operation: str) -> None:
    """Enforce that an operation is allowed for an entity type.

    Raises CapabilityError if not supported.
    """
    if not can_perform(entity_type, operation):
        raise CapabilityError(entity_type, operation)
