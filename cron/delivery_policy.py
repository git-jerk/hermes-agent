"""Cron delivery policy helpers.

The scheduler still supports explicit chat delivery, but routine jobs should be
local-only unless a user explicitly asked for a chat report or alert.  This
module provides the small, dependency-light audit used by the local checker and
by tests so future cron surfaces do not silently reintroduce Matrix/origin noise.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

CHAT_DELIVERY_PREFIXES = ("matrix:",)
CHAT_DELIVERY_VALUES = {"origin", "matrix"}


def normalize_deliver_parts(deliver: Any) -> list[str]:
    """Return normalized delivery tokens from a stored cron ``deliver`` value."""
    if deliver is None or deliver == "":
        return ["local"]
    if isinstance(deliver, (list, tuple, set)):
        raw = ",".join(str(part) for part in deliver)
    else:
        raw = str(deliver)
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    return parts or ["local"]


def is_active_job(job: Mapping[str, Any]) -> bool:
    """Whether a job can currently fire without being explicitly resumed."""
    if not job.get("enabled", True):
        return False
    state = str(job.get("state") or "scheduled").lower()
    return state not in {"paused", "completed", "disabled"}


def is_chat_delivery_part(part: str) -> bool:
    normalized = part.strip().lower()
    return normalized in CHAT_DELIVERY_VALUES or normalized.startswith(CHAT_DELIVERY_PREFIXES)


def audit_chat_delivery_jobs(
    jobs: Iterable[Mapping[str, Any]],
    *,
    allowlist: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return active jobs with unallowlisted origin/Matrix delivery.

    Only routing metadata is returned: job id/name/state/deliver.  Prompts and
    outputs are intentionally omitted because cron jobs may contain PHI,
    trading/client context, or other private details.
    """
    allowed = {str(item).strip() for item in (allowlist or []) if str(item).strip()}
    violations: list[dict[str, Any]] = []
    for job in jobs:
        job_id = str(job.get("id") or job.get("job_id") or "")
        if not job_id or job_id in allowed or not is_active_job(job):
            continue
        parts = normalize_deliver_parts(job.get("deliver", "local"))
        chat_parts = [part for part in parts if is_chat_delivery_part(part)]
        if not chat_parts:
            continue
        violations.append(
            {
                "id": job_id,
                "name": str(job.get("name") or job_id),
                "state": str(job.get("state") or "scheduled"),
                "deliver": ",".join(parts),
                "chat_delivery": chat_parts,
            }
        )
    return violations


def format_chat_delivery_violations(violations: Sequence[Mapping[str, Any]]) -> str:
    """Format violations for a local-only checker; empty string means pass."""
    if not violations:
        return ""
    lines = [
        "Active cron jobs with unallowlisted Matrix/origin delivery:",
    ]
    for item in violations:
        lines.append(
            f"- {item.get('id')}: {item.get('name')} "
            f"(state={item.get('state')}, deliver={item.get('deliver')})"
        )
    lines.append(
        "Routine cron stdout should stay local; use an allowlist only for explicit user-requested alerts/reports."
    )
    return "\n".join(lines)
