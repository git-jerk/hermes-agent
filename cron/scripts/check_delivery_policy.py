"""Local-only cron delivery policy checker.

Prints nothing when active cron jobs are local-only (or explicitly allowlisted).
When unallowlisted active jobs deliver to ``origin`` or Matrix, prints a compact
routing-only inventory.  Prompts and outputs are intentionally omitted.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cron.delivery_policy import audit_chat_delivery_jobs, format_chat_delivery_violations
from cron.jobs import list_jobs


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _load_allowlist(paths: list[str]) -> list[str]:
    ids: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser()
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            ids.append(stripped.split()[0])
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit active cron jobs for unallowlisted origin/Matrix delivery.")
    parser.add_argument("--allowlist", action="append", default=[], help="Allowlisted cron job id. Repeatable; comma-separated values are also accepted.")
    parser.add_argument("--allowlist-file", action="append", default=[], help="File containing allowlisted job ids, one per line. Comments start with #.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text when violations exist.")
    parser.add_argument("--fail-on-violation", action="store_true", help="Exit 1 when violations are found. Default exits 0 so local cron watchdogs can save the report without error-alert semantics.")
    args = parser.parse_args(argv)

    allowlist: list[str] = []
    allowlist.extend(_split_csv(os.environ.get("HERMES_CRON_DELIVERY_ALLOWLIST")))
    for item in args.allowlist:
        allowlist.extend(_split_csv(item))
    allowlist.extend(_load_allowlist(args.allowlist_file))

    violations = audit_chat_delivery_jobs(list_jobs(include_disabled=True), allowlist=allowlist)
    if not violations:
        return 0

    if args.json:
        print(json.dumps({"violations": violations}, indent=2, sort_keys=True))
    else:
        print(format_chat_delivery_violations(violations))
    return 1 if args.fail_on_violation else 0


if __name__ == "__main__":
    raise SystemExit(main())
