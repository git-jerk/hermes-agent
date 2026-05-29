"""OpenAI Codex (ChatGPT Pro) upstream adapter with cap-aware account routing.

Forwards to the ChatGPT-account Codex backend (``chatgpt.com/backend-api/codex``)
on behalf of OpenClaw agents, so they never hold the OAuth refresh_token. Tokens
are resolved per-account from the canonical 1Password store (vault ``AI-Allowed``,
items ``OpenAI - jeremiahkear@<address>``) via ``lib.codex_tokens`` — the same
library Hermes's own resolver and ``codex-token-get.py`` read from. See
``workspace/references/codex-oauth-broker-policy-2026-05-29.md`` (Phase 3).

Two things make this more than a bearer swap:

1. **Cloudflare allow-list.** The Codex backend sits behind a Cloudflare layer
   that 403s (``cf-mitigated: challenge``) any request that doesn't advertise a
   first-party ``originator``/``User-Agent`` and the ``ChatGPT-Account-ID`` that
   matches the bearer's account. Because this adapter chooses the account
   (cap-aware routing), it must inject the account-id derived from whichever
   token it attaches — a pass-through proxy that trusted client headers would
   send account A's id with account B's token after a failover. The required
   header set mirrors ``agent.auxiliary_client._codex_cloudflare_headers``.

2. **Cap-aware routing.** The original incident this proxy prevents was both Pro
   accounts (hotmail + gmail) burning to a 429 in parallel. On a 429 the adapter
   puts the offending account in a cooldown and fails the request over to the
   other account; subsequent requests stick to the healthy account until the
   cooldown expires. ``hermes_cli.proxy.server`` already calls
   ``get_retry_credential`` on a 401/429 and re-sends the in-flight request.

Refresh safety: refreshing rotates the refresh_token at OpenAI, and a concurrent
refresh of the same account by another Hermes process is the exact race that
caused the 2026-05-27 ``token_invalidated`` cascade. This adapter only refreshes
under ``hermes_cli.auth._auth_store_lock`` — the same lock Hermes's chat-path
refresher holds across both the OpenAI rotation and the 1P mirror — so the two
paths are fully serialized. The hot path (token still valid) is a lock-free 1P
read.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional

from hermes_cli.auth import DEFAULT_CODEX_BASE_URL, _auth_store_lock
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential

logger = logging.getLogger(__name__)

# Relative paths (under the proxy's /v1 mount) the Codex backend serves.
# A request to http://127.0.0.1:<port>/v1/responses forwards to
# https://chatgpt.com/backend-api/codex/responses. Anything else gets a 404.
_ALLOWED_PATHS: FrozenSet[str] = frozenset({"/responses", "/conversations"})

# 1P account keys understood by lib.codex_tokens, and the JWT profile-email that
# identifies each (used to map a bearer back to its account after a failure).
_ACCOUNTS = ("hotmail", "gmail")
_EMAIL_TO_ACCOUNT = {
    "jeremiahkear@hotmail.com": "hotmail",
    "jeremiahkear@gmail.com": "gmail",
}

# Canonical 1P token library lives on the OpenClaw side; import lazily and
# memoize. Mirrors the sys.path bootstrap in hermes_cli.auth._read_codex_tokens_from_1p.
_LAWS_GUARDS_PATH = "/Users/minimiah/.openclaw/workspace/LAWS_GUARDS"
_codex_tokens_mod = None


def _codex_tokens():
    """Return the ``lib.codex_tokens`` module, importing it on first use."""
    global _codex_tokens_mod
    if _codex_tokens_mod is None:
        import sys

        if _LAWS_GUARDS_PATH not in sys.path:
            sys.path.insert(0, _LAWS_GUARDS_PATH)
        from lib import codex_tokens as ct  # type: ignore

        _codex_tokens_mod = ct
    return _codex_tokens_mod


class CodexAdapter(UpstreamAdapter):
    """Proxy upstream for the ChatGPT-account Codex backend with failover."""

    auth_hint = (
        "verify 1P codex tokens: "
        "python3 /Users/minimiah/.openclaw/workspace/scripts/codex-token-get.py --format jwt-email"
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # account -> epoch seconds at which its rate-limit cooldown ends
        self._cooldowns: Dict[str, float] = {}

        prio = os.getenv("HERMES_CODEX_PROXY_PRIORITY", "hotmail,gmail")
        ordered = tuple(a.strip() for a in prio.split(",") if a.strip() in _ACCOUNTS)
        self._priority = ordered or ("hotmail", "gmail")

        self._base_url = (
            os.getenv("HERMES_CODEX_BASE_URL", "").strip().rstrip("/")
            or DEFAULT_CODEX_BASE_URL
        )
        self._refresh_skew = int(os.getenv("HERMES_CODEX_PROXY_REFRESH_SKEW_SECONDS", "120"))
        self._cooldown_seconds = int(os.getenv("HERMES_CODEX_PROXY_COOLDOWN_SECONDS", "900"))
        self._refresh_lock_timeout = float(
            os.getenv("HERMES_CODEX_PROXY_REFRESH_LOCK_TIMEOUT", "25")
        )

    # ------------------------------------------------------------------ contract

    @property
    def name(self) -> str:
        return "codex"

    @property
    def display_name(self) -> str:
        return "OpenAI Codex (ChatGPT Pro)"

    @property
    def allowed_paths(self) -> FrozenSet[str]:
        return _ALLOWED_PATHS

    def is_authenticated(self) -> bool:
        ct = _codex_tokens()
        found = False
        for account in self._priority:
            try:
                tokens = ct.read_tokens(account)
            except Exception as exc:
                # 1P CSW unreachable: be optimistic so a transient outage at
                # boot doesn't make launchd's KeepAlive thrash. Real failures
                # surface per-request (and recover when CSW returns).
                logger.warning("codex proxy: 1P read failed during auth check (%s); assuming ok", exc)
                return True
            if tokens and tokens.get("access_token"):
                found = True
        return found

    def get_credential(self) -> UpstreamCredential:
        with self._lock:
            last_exc: Optional[Exception] = None
            for account in self._account_order():
                try:
                    token = self._resolve_token(account)
                except Exception as exc:
                    last_exc = exc
                    logger.warning("codex proxy: credential resolution failed for %s: %s", account, exc)
                    continue
                return self._credential(account, token)
            raise RuntimeError(
                f"no usable codex account (tried {list(self._account_order())}): {last_exc}"
            )

    def get_retry_credential(
        self,
        *,
        failed_credential: UpstreamCredential,
        status_code: int,
    ) -> Optional[UpstreamCredential]:
        if status_code not in {401, 429}:
            return None

        with self._lock:
            failed_account = self._account_for_token(failed_credential.bearer)
            if status_code == 429 and failed_account:
                self._cooldowns[failed_account] = time.time() + self._cooldown_seconds
                logger.info(
                    "codex proxy: %s returned 429; cooling down %ds and failing over",
                    failed_account, self._cooldown_seconds,
                )
            elif status_code == 401:
                # Not a rate limit — don't cool down, just try the other account
                # (a fresh-but-rejected token usually means that account's
                # refresh_token was revoked; re-auth is Hermes's job, not ours).
                logger.info(
                    "codex proxy: %s returned 401; failing over to alternate account",
                    failed_account or "unknown",
                )

            for account in self._account_order():
                if account == failed_account:
                    continue
                try:
                    token = self._resolve_token(account)
                except Exception as exc:
                    logger.warning("codex proxy: retry resolution failed for %s: %s", account, exc)
                    continue
                cred = self._credential(account, token)
                if cred.bearer != failed_credential.bearer:
                    logger.info(
                        "codex proxy: retrying on %s after %s on %s",
                        account, status_code, failed_account or "unknown",
                    )
                    return cred
            return None

    # ------------------------------------------------------------------ internals

    def _account_order(self) -> List[str]:
        """Accounts to try, best first: not-in-cooldown (by priority), then
        cooled-down (soonest-to-recover first, as a best-effort last resort)."""
        now = time.time()
        available = [a for a in self._priority if self._cooldowns.get(a, 0.0) <= now]
        cooled = [a for a in self._priority if self._cooldowns.get(a, 0.0) > now]
        cooled.sort(key=lambda a: self._cooldowns.get(a, 0.0))
        return available + cooled

    def _resolve_token(self, account: str) -> str:
        """Resolve a usable access_token for ``account`` from 1P.

        Lock-free read on the hot path; refresh (which rotates the
        refresh_token at OpenAI) only under ``_auth_store_lock`` so it
        serializes with Hermes's chat-path refresher.
        """
        ct = _codex_tokens()
        tokens = ct.read_tokens(account)
        if not tokens or not tokens.get("access_token"):
            raise RuntimeError(f"no codex OAuth tokens in 1P for account {account!r}")
        access_token = tokens["access_token"]

        exp = ct.jwt_exp(access_token) or 0
        if exp - int(time.time()) <= self._refresh_skew:
            with _auth_store_lock(timeout_seconds=self._refresh_lock_timeout):
                # refresh_if_expiring re-reads 1P under the lock (picking up any
                # rotation a prior lock-holder already mirrored), refreshes only
                # if still expiring, and writes back with CAS.
                refreshed = ct.refresh_if_expiring(account, skew_seconds=self._refresh_skew)
            access_token = refreshed.get("access_token") or access_token
        return access_token

    def _credential(self, account: str, access_token: str) -> UpstreamCredential:
        ct = _codex_tokens()
        exp = ct.jwt_exp(access_token)
        expires_at = (
            datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else None
        )
        return UpstreamCredential(
            bearer=access_token,
            base_url=self._base_url,
            expires_at=expires_at,
            extra_headers=self._cloudflare_headers(access_token),
        )

    def _cloudflare_headers(self, access_token: str) -> Dict[str, str]:
        """Headers the Codex Cloudflare layer requires (else 403 regardless of auth).

        Mirrors ``agent.auxiliary_client._codex_cloudflare_headers``. Kept in
        sync by hand: if the upstream originator allow-list shifts, update both.
        The ``ChatGPT-Account-ID`` is pulled from the bearer's own JWT so it
        always matches the attached token's account, even after a failover.
        """
        headers = {
            "User-Agent": "codex_cli_rs/0.0.0 (Hermes Agent)",
            "originator": "codex_cli_rs",
        }
        try:
            claims = _codex_tokens().jwt_claims(access_token)
            acct_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
            if isinstance(acct_id, str) and acct_id:
                headers["ChatGPT-Account-ID"] = acct_id
        except Exception:
            # Malformed token: drop the account-id header rather than raise, so a
            # bad token surfaces as a clean 401 from upstream, not a 500 here.
            pass
        return headers

    def _account_for_token(self, bearer: str) -> Optional[str]:
        try:
            return _EMAIL_TO_ACCOUNT.get(_codex_tokens().jwt_email(bearer))
        except Exception:
            return None


__all__ = ["CodexAdapter"]
