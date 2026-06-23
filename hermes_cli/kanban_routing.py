"""Kanban collaboration routing helpers.

Centralizes the local policy for routing routine Kanban work to subscription-
backed Claude Code lanes while reserving Codex for critical adversarial/safety
review.  The helpers are intentionally heuristic and conservative: explicit
assignee choices from a caller are preserved by card creators, while generated
or omitted assignees can be normalized to the configured lanes.
"""
from __future__ import annotations

from typing import Any, Optional

IMPLEMENTATION_ASSIGNEE = "claude-code"
REVIEW_ASSIGNEE = "claude-review"
CRITICAL_REVIEW_ASSIGNEE = "codexworker"

_ROUTINE_REVIEW_TERMS = (
    "review",
    "review-required",
    "code review",
    "independent review",
    "verify",
    "verification",
    "validate",
    "qa",
    "quality check",
    "audit",
    "challenge",
)

_CRITICAL_REVIEW_TERMS = (
    "critical",
    "adversarial",
    "safety",
    "security",
    "red-team",
    "red team",
    "exploit",
    "credential",
    "secret",
    "payment",
    "funds",
    "trading",
    "order-boundary",
    "order boundary",
    "withdraw",
    "deposit",
    "r3",
)

_IMPLEMENTATION_TERMS = (
    "implement",
    "implementation",
    "build",
    "fix",
    "patch",
    "repair",
    "research",
    "synthesis",
    "synthesize",
    "fan-in",
    "summarize",
    "write",
)


def _clean_assignee(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def routing_policy(config: Optional[dict] = None) -> dict[str, str]:
    """Return effective collaboration routing assignees.

    Reads ``kanban.collaboration_routing`` first, then
    ``kanban.board_defaults.routing_policy``, then falls back to the local
    defaults.  Missing/blank fields inherit defaults so callers always get a
    complete policy.
    """
    policy: dict[str, Any] = {}
    kanban_cfg = (config or {}).get("kanban", {}) if isinstance(config, dict) else {}
    if isinstance(kanban_cfg, dict):
        collab = kanban_cfg.get("collaboration_routing")
        board_defaults = kanban_cfg.get("board_defaults")
        board_policy = (
            board_defaults.get("routing_policy")
            if isinstance(board_defaults, dict)
            else None
        )
        if isinstance(collab, dict) and collab.get("enabled", True) is not False:
            policy.update(collab)
        elif isinstance(board_policy, dict) and board_policy.get("enabled", True) is not False:
            policy.update(board_policy)

    implementation = _clean_assignee(policy.get("implementation_assignee")) or IMPLEMENTATION_ASSIGNEE
    review = _clean_assignee(policy.get("review_assignee")) or REVIEW_ASSIGNEE
    synthesis = _clean_assignee(policy.get("synthesis_assignee")) or implementation
    critical = _clean_assignee(policy.get("critical_review_assignee")) or CRITICAL_REVIEW_ASSIGNEE
    return {
        "implementation_assignee": implementation,
        "review_assignee": review,
        "synthesis_assignee": synthesis,
        "critical_review_assignee": critical,
    }


def _text_for(title: str | None, body: str | None) -> str:
    return f"{title or ''}\n{body or ''}".casefold()


def is_review_like(title: str | None, body: str | None) -> bool:
    text = _text_for(title, body)
    return any(term in text for term in _ROUTINE_REVIEW_TERMS)


def is_critical_review_like(title: str | None, body: str | None) -> bool:
    text = _text_for(title, body)
    if any(
        phrase in text
        for phrase in (
            "no critical safety",
            "no critical or safety",
            "no safety boundary",
            "not critical",
            "non-critical",
        )
    ):
        return False
    return is_review_like(title, body) and any(term in text for term in _CRITICAL_REVIEW_TERMS)


def is_synthesis_like(title: str | None, body: str | None) -> bool:
    text = _text_for(title, body)
    return "synthesis" in text or "synthesize" in text or "fan-in" in text or "fan in" in text


def route_for_task(title: str | None, body: str | None, *, config: Optional[dict] = None) -> str:
    """Choose the default lane for an unassigned generated/card-created task."""
    policy = routing_policy(config)
    if is_critical_review_like(title, body):
        return policy["critical_review_assignee"]
    if is_review_like(title, body):
        return policy["review_assignee"]
    if is_synthesis_like(title, body):
        return policy["synthesis_assignee"]
    return policy["implementation_assignee"]


def normalize_generated_assignee(
    assignee: Any,
    *,
    title: str | None,
    body: str | None,
    default_assignee: str,
    valid_names: set[str],
    config: Optional[dict] = None,
) -> str:
    """Normalize a generated decomposer assignee under the routing policy.

    The decomposer LLM's assignee is not a human override.  It may therefore be
    corrected when it sends routine work to the critical ``codexworker`` lane.
    Unknown choices and unusable configured policy lanes fall back to
    ``default_assignee``.
    """
    policy = routing_policy(config)
    chosen = _clean_assignee(assignee)
    if not chosen:
        chosen = route_for_task(title, body, config=config)
    elif chosen == policy["critical_review_assignee"] and not is_critical_review_like(title, body):
        # Reserve Codexworker for critical adversarial/safety review only.
        chosen = route_for_task(title, body, config=config)

    if chosen not in valid_names:
        return default_assignee
    return chosen


def card_creator_assignee(
    assignee: Any,
    *,
    title: str | None,
    body: str | None,
    config: Optional[dict] = None,
) -> str:
    """Return an assignee for direct card creation.

    Explicit caller-supplied assignees are preserved.  Only omitted/blank
    assignees are routed by policy.
    """
    explicit = _clean_assignee(assignee)
    if explicit:
        return explicit
    return route_for_task(title, body, config=config)


def instructions(config: Optional[dict] = None) -> str:
    policy = routing_policy(config)
    return (
        "Routing policy: routine offline implementation, repair, research, and "
        f"synthesis/fan-in -> `{policy['implementation_assignee']}`; routine "
        f"review/verification/QA -> `{policy['review_assignee']}`; reserve "
        f"`{policy['critical_review_assignee']}` only for critical adversarial "
        "or safety/security review. Preserve explicit human-specified assignees."
    )


def external_lane_description(name: str, config: Optional[dict] = None) -> str:
    policy = routing_policy(config)
    if name == policy["implementation_assignee"]:
        return (
            "external Claude Code background lane using claude --bg; use for "
            "routine offline implementation, repair, research, and synthesis/fan-in"
        )
    if name == policy["review_assignee"]:
        return (
            "external Claude Code background review lane using claude --bg; use "
            "for routine independent review, verification, QA, and challenge"
        )
    if name == policy["critical_review_assignee"]:
        return (
            "critical adversarial/safety review lane; reserve for high-risk "
            "security, credential, trading/funds, or hard safety boundary review"
        )
    return "external Claude Code background lane using claude --bg"
