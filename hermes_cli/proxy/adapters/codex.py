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

import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional, Tuple

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
        # account -> (polled_epoch, used_percent|None, reset_at_epoch|None)
        self._usage: Dict[str, tuple] = {}

        prio = os.getenv("HERMES_CODEX_PROXY_PRIORITY", "hotmail,gmail")
        ordered = tuple(a.strip() for a in prio.split(",") if a.strip() in _ACCOUNTS)
        self._priority = ordered or ("hotmail", "gmail")

        base = os.getenv("HERMES_CODEX_BASE_URL", "").strip().rstrip("/")
        # Loop guard: the adapter ALWAYS talks to the real Codex backend, never to
        # ourselves. If the env points at the proxy (e.g. a profile .env that cut a
        # consumer over leaked into this process), ignore it — otherwise we'd
        # forward to 127.0.0.1:8646 and recurse. A real codex base contains
        # "backend-api".
        if base and ("8646" in base or "backend-api" not in base):
            logger.warning(
                "codex proxy: ignoring HERMES_CODEX_BASE_URL=%r (not a codex backend / would loop); using default",
                base,
            )
            base = ""
        self._base_url = base or DEFAULT_CODEX_BASE_URL
        self._refresh_skew = int(os.getenv("HERMES_CODEX_PROXY_REFRESH_SKEW_SECONDS", "120"))
        self._cooldown_seconds = int(os.getenv("HERMES_CODEX_PROXY_COOLDOWN_SECONDS", "900"))
        self._cooldown_min = int(os.getenv("HERMES_CODEX_PROXY_COOLDOWN_MIN_SECONDS", "60"))
        self._cooldown_max = int(os.getenv("HERMES_CODEX_PROXY_COOLDOWN_MAX_SECONDS", "21600"))
        self._refresh_lock_timeout = float(
            os.getenv("HERMES_CODEX_PROXY_REFRESH_LOCK_TIMEOUT", "25")
        )
        # Proactive headroom routing: poll per-account usage and prefer the account
        # with more remaining capacity, so we avoid hitting a 429 wall on the
        # boundary request. On by default; fail-open (any poll error → priority
        # order). Kill switch: HERMES_CODEX_PROXY_PROACTIVE=0.
        self._proactive = os.getenv("HERMES_CODEX_PROXY_PROACTIVE", "1").strip().lower() not in (
            "0", "false", "no", "off",
        )
        self._usage_ttl = int(os.getenv("HERMES_CODEX_PROXY_USAGE_TTL_SECONDS", "60"))
        self._usage_timeout = float(os.getenv("HERMES_CODEX_PROXY_USAGE_TIMEOUT", "6"))

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
            order = self._account_order()
            tokens: Dict[str, str] = {}
            if self._proactive:
                order, tokens = self._proactive_order(order)
            last_exc: Optional[Exception] = None
            for account in order:
                try:
                    token = tokens.get(account) or self._resolve_token(account)
                except Exception as exc:
                    last_exc = exc
                    logger.warning("codex proxy: credential resolution failed for %s: %s", account, exc)
                    continue
                rem = self._usage.get(account)
                logger.info(
                    "codex proxy: serving via %s%s",
                    account,
                    f" ({rem[1]:.0f}% remaining)" if rem and rem[1] is not None else "",
                )
                return self._credential(account, token)
            raise RuntimeError(
                f"no usable codex account (tried {order}): {last_exc}"
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
                cd = self._cooldown_for(failed_account)
                self._cooldowns[failed_account] = time.time() + cd
                # The account is now at/over its cap — zero its cached headroom so
                # proactive routing won't pick it until a fresh poll says otherwise.
                prev = self._usage.get(failed_account)
                self._usage[failed_account] = (time.time(), 0.0, prev[2] if prev else None)
                logger.info(
                    "codex proxy: %s returned 429; cooling down %ds and failing over",
                    failed_account, cd,
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

    def _proactive_order(self, base: List[str]) -> Tuple[List[str], Dict[str, str]]:
        """Reorder the not-cooled accounts by remaining headroom (most first),
        polling per-account usage as needed. Fail-open: any error and we keep the
        priority-based ``base`` order. Returns (ordered_accounts, resolved_tokens)
        so the caller reuses the tokens we already resolved here.

        Only reorders when ≥2 accounts are actually selectable; with one healthy
        account there's nothing to balance.
        """
        now = time.time()
        not_cooled = [a for a in base if self._cooldowns.get(a, 0.0) <= now]
        if len(not_cooled) < 2:
            return base, {}

        tokens: Dict[str, str] = {}
        scored: List[Tuple[str, Optional[float]]] = []
        for account in not_cooled:
            try:
                tokens[account] = self._resolve_token(account)
            except Exception as exc:
                logger.warning("codex proxy: proactive resolve failed for %s: %s", account, exc)
                continue
            scored.append((account, self._remaining_headroom(account, tokens[account])))

        if not scored:
            return base, tokens

        # Known headroom sorts highest-first; unknown (None) sinks below known.
        scored.sort(key=lambda t: (t[1] if t[1] is not None else -1.0), reverse=True)
        ordered = [a for a, _ in scored]
        for account in base:  # append cooled / resolve-failed accounts, keep base order
            if account not in ordered:
                ordered.append(account)

        if any(r is not None for _, r in scored):
            logger.info(
                "codex proxy: proactive order %s (remaining%%: %s)",
                ordered,
                {a: (round(r, 1) if r is not None else None) for a, r in scored},
            )
        return ordered, tokens

    def _remaining_headroom(self, account: str, token: str) -> Optional[float]:
        """Remaining capacity 0..100 for ``account`` (100 = empty, 0 = capped),
        TTL-cached. ``None`` when usage can't be determined (fail-open)."""
        now = time.time()
        cached = self._usage.get(account)
        if cached and (now - cached[0]) < self._usage_ttl:
            return cached[1]
        remaining, reset_at = self._poll_usage(account, token)
        if remaining is not None:
            self._usage[account] = (now, remaining, reset_at)
            return remaining
        return cached[1] if cached else None  # stale-but-better-than-nothing, else None

    def _usage_headers(self, token: str) -> Dict[str, str]:
        """Headers for the usage endpoint. CRITICAL (verified live 2026-05-29):
        do NOT send the Cloudflare ``originator: codex_cli_rs`` header here — the
        usage endpoint 401s on it, even though the ``/responses`` endpoint
        *requires* it. Usage wants bearer + a plain UA + ``ChatGPT-Account-Id``
        only (matching ``agent.account_usage._fetch_codex_account_usage``,
        which works)."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "codex-cli",
        }
        try:
            claims = _codex_tokens().jwt_claims(token)
            acct_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
            if isinstance(acct_id, str) and acct_id:
                headers["ChatGPT-Account-Id"] = acct_id
        except Exception:
            pass
        return headers

    def _poll_usage(self, account: str, token: str) -> Tuple[Optional[float], Optional[float]]:
        """GET the Codex usage endpoint for ``account``. Returns
        (remaining_percent, reset_at_epoch), either may be None. Never raises.

        remaining = 100 - max(used_percent across all windows). The most-
        constrained window governs, since hitting either the session or weekly
        cap 429s you. ``reset_at`` is the soonest reset among the windows at/near
        the cap, so a 429 cooldown lands on the real recovery time.

        Live wire shape (verified 2026-05-29): top-level singular ``rate_limit``
        with ``primary_window``/``secondary_window`` sub-objects carrying
        ``used_percent`` (0..100) + ``reset_at`` (epoch). The plural model-keyed
        ``rate_limits`` shape is also accepted defensively in case it ever
        appears, so a shape flip can't silently blind the router.
        """
        try:
            req = urllib.request.Request(
                self._usage_url(), headers=self._usage_headers(token), method="GET"
            )
            with urllib.request.urlopen(req, timeout=self._usage_timeout) as r:
                payload = json.loads(r.read()) or {}
        except Exception as exc:
            logger.debug("codex proxy: usage poll failed for %s: %s", account, exc)
            return None, None

        windows = self._iter_usage_windows(payload)
        used = [u for (u, _r) in windows]
        if not used:
            logger.debug("codex proxy: usage poll for %s had no parseable windows", account)
            return None, None
        peak = max(used)
        remaining = max(0.0, 100.0 - peak)
        # reset_at = soonest reset among windows that are the binding constraint
        # (within 5% of peak usage). Those are what actually gate the next request.
        binding = [r for (u, r) in windows if r is not None and u >= peak - 5.0]
        reset_at = min(binding) if binding else None
        return remaining, reset_at

    @staticmethod
    def _iter_usage_windows(payload: dict) -> List[Tuple[float, Optional[float]]]:
        """Flatten the usage payload into ``(used_percent, reset_epoch|None)``
        pairs across both known wire shapes. Tolerant of missing keys."""
        out: List[Tuple[float, Optional[float]]] = []

        def add(window) -> None:
            if not isinstance(window, dict):
                return
            up = window.get("used_percent")
            if up is None:
                return
            try:
                used = float(up)
            except (TypeError, ValueError):
                return
            reset = None
            for rk in ("resets_at", "reset_at"):
                rv = window.get(rk)
                if rv is not None:
                    try:
                        reset = float(rv)
                        break
                    except (TypeError, ValueError):
                        pass
            out.append((used, reset))

        # Live shape (verified 2026-05-29): singular rate_limit ->
        # {primary_window, secondary_window}.
        rate_limit = payload.get("rate_limit")
        if isinstance(rate_limit, dict):
            for wk in ("primary_window", "secondary_window", "primary", "secondary"):
                add(rate_limit.get(wk))
        # Defensive: plural model-keyed rate_limits -> {primary, secondary}, in
        # case the endpoint ever returns the per-model breakdown at top level.
        rate_limits = payload.get("rate_limits")
        if not out and isinstance(rate_limits, dict):
            for per_model in rate_limits.values():
                if isinstance(per_model, dict):
                    for wk in ("primary", "secondary"):
                        add(per_model.get(wk))
        return out

    def _usage_url(self) -> str:
        """Codex usage endpoint derived from the backend base URL.
        ``…/backend-api/codex`` → ``…/backend-api/wham/usage`` (matches
        ``agent.account_usage._resolve_codex_usage_url``)."""
        base = self._base_url.rstrip("/")
        if base.endswith("/codex"):
            base = base[: -len("/codex")]
        if "/backend-api" in base:
            return base + "/wham/usage"
        return base + "/api/codex/usage"

    def _cooldown_for(self, account: str) -> int:
        """Cooldown seconds for a 429'd account: prefer the real ``reset_at`` from
        the usage cache (clamped to [min, max]); fall back to the fixed default."""
        cached = self._usage.get(account)
        reset_at = cached[2] if cached else None
        if reset_at:
            secs = int(reset_at - time.time())
            if secs > 0:
                return max(self._cooldown_min, min(self._cooldown_max, secs))
        return self._cooldown_seconds

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
